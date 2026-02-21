import json
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple


def _json_default(obj):
    """JSON 직렬화 시 numpy/pd 타입 변환 (TypeError 방지)."""
    if isinstance(obj, (np.integer, np.int64, np.int32)):
        return int(obj)
    if isinstance(obj, (np.floating, np.float64, np.float32)):
        v = float(obj)
        return v if np.isfinite(v) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (np.ndarray, pd.Series)):
        return obj.tolist()
    raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')


def sma(series, window):
    return series.rolling(window).mean()

def generate_sma_signals(df, short=10, long=50):
    df = df.copy()
    df['sma_s'] = sma(df['close'], short)
    df['sma_l'] = sma(df['close'], long)
    df['signal'] = 0
    cond_long = (df['sma_s'] > df['sma_l']) & (df['sma_s'].shift(1) <= df['sma_l'].shift(1))
    cond_exit = (df['sma_s'] < df['sma_l']) & (df['sma_s'].shift(1) >= df['sma_l'].shift(1))
    df.loc[cond_long, 'signal'] = 1
    df.loc[cond_exit, 'signal'] = -1
    return df[['time','open','high','low','close','volume','sma_s','sma_l','signal']]


# ---------------------------------------------------------------------------
# 4) 종합 신호 판단 및 로깅 (DB 캐싱된 지표 활용)
# ---------------------------------------------------------------------------

def load_cached_indicators(ticker: str, timeframe: str, count: int = 200) -> pd.DataFrame:
    """
    technical_indicators 테이블에서 캐싱된 지표 로드
    
    Parameters:
    - ticker: 코인 티커
    - timeframe: 시간 간격
    - count: 가져올 최근 데이터 개수
    
    Returns:
    - DataFrame (time, ema_short, ema_long, rsi, atr, adx, bb_lower, bb_middle, bb_upper 등)
    """
    try:
        from trading_bot.db import get_session
        from trading_bot.models import TechnicalIndicator
        import json
        
        session = get_session()
        records = session.query(TechnicalIndicator)\
            .filter(TechnicalIndicator.ticker == ticker)\
            .filter(TechnicalIndicator.timeframe == timeframe)\
            .order_by(TechnicalIndicator.ts.desc())\
            .limit(count).all()
        session.close()
        
        if not records:
            return pd.DataFrame()
        
        data = []
        for record in reversed(records):  # 시간순 정렬
            row = {
                'time': record.ts,
                'ema_short': record.ema_short,
                'ema_long': record.ema_long,
                'rsi': record.rsi,
                'atr': record.atr,
                'volume_ma': record.volume_ma,
            }
            
            # indicators JSON에서 ADX, BB 추출
            if record.indicators:
                if isinstance(record.indicators, str):
                    indicators = json.loads(record.indicators)
                else:
                    indicators = record.indicators
                
                if 'adx' in indicators:
                    row['adx'] = indicators['adx']
                if 'bb_lower' in indicators:
                    row['bb_lower'] = indicators['bb_lower']
                    row['bb_middle'] = indicators.get('bb_middle', np.nan)
                    row['bb_upper'] = indicators.get('bb_upper', np.nan)
                if 'atr_raw' in indicators:
                    row['atr_raw'] = indicators['atr_raw']  # 원본 ATR 로드
            
            data.append(row)
        
        df = pd.DataFrame(data)
        if len(df) > 0:
            df['time'] = pd.to_datetime(df['time'])
        return df
    except Exception as e:
        print(f'⚠️ 캐싱된 지표 로드 실패 ({ticker}, {timeframe}): {e}')
        return pd.DataFrame()


def generate_comprehensive_signal_with_logging(
    ticker: str,
    timeframe: str = 'minute60',
    current_price: float = None,
    account_value: float = 100000,
    adx_trend_threshold: float = 25.0,
    use_dynamic_risk: bool = True,
) -> Dict:
    """
    종합 신호 판단 및 상세 로깅
    
    [Data Flow]
    1. technical_indicators 테이블에서 캐싱된 지표 로드
    2. ADX 기준 Regime 판단 (ADX > 25: 추세장, ADX <= 25: 횡보장)
    3. Regime에 따라 신호 생성:
       - 추세장: EMA 골든/데드 크로스
       - 횡보장: 볼린저 밴드 하단/상단 터치
    4. 동적 리스크 조정 적용 (연속 손실 기반)
    5. signals 테이블에 신호 저장
    6. analysis_results 테이블에 상세 분석 결과 저장
    
    Parameters:
    - ticker: 코인 티커
    - timeframe: 시간 간격
    - current_price: 현재 가격 (None이면 DB에서 최신 close 사용)
    - account_value: 계좌 총 가치
    - adx_trend_threshold: ADX 추세 임계값 (기본 25.0)
    - use_dynamic_risk: 동적 리스크 조정 사용 여부
    
    Returns:
    - Dict with keys:
      - signal: 'buy', 'sell', 'hold'
      - regime: 'trend' or 'range'
      - decision_reason: 판단 근거
      - position_size: 조정된 포지션 크기
      - risk_adjustments: 리스크 조정 정보
      - indicators: 지표 값들
    """
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Signal, AnalysisResult
        from trading_bot.risk import calculate_adjusted_position_size
        from trading_bot.data_manager import load_ohlcv_from_db, load_higher_timeframe_indicators
        import json
        
        # 1) 캐싱된 지표 로드
        df_indicators = load_cached_indicators(ticker, timeframe, count=200)
        if df_indicators.empty:
            return {
                'signal': 'hold',
                'regime': 'unknown',
                'decision_reason': '캐싱된 지표 데이터 없음',
                'position_size': 0.0,
                'risk_adjustments': {},
                'indicators': {}
            }
        
        # ---------- Intra-bar 휩쏘 방지: 완성된 봉만 기준으로 신호 판단 ----------
        # iloc[-1] = 진행 중인 미완성 캔들(5분마다 값이 요동) → 사용하지 않음.
        # iloc[-2] = 가장 최근에 완전히 닫힌 1봉 (closed_candle)
        # iloc[-3] = 그 이전 완성 봉 (prev_closed_candle)
        # Regime / EMA 크로스 / ADX 기울기는 모두 closed_candle vs prev_closed_candle 기준.
        if len(df_indicators) < 3:
            return {
                'signal': 'hold',
                'regime': 'unknown',
                'decision_reason': '완성 봉 데이터 부족(최소 3봉 필요)',
                'position_size': 0.0,
                'risk_adjustments': {},
                'indicators': {}
            }
        closed_candle = df_indicators.iloc[-2]   # 가장 최근 완성 봉
        prev_closed_candle = df_indicators.iloc[-3]  # 그 이전 완성 봉
        
        # 현재 가격 가져오기 (실시간; 볼린저 터치 판단 시 사용)
        if current_price is None:
            df_ohlcv = load_ohlcv_from_db(ticker, timeframe, count=1)
            if not df_ohlcv.empty:
                current_price = float(df_ohlcv.iloc[-1]['close'])
            else:
                current_price = 0.0
        
        # 2) Regime 판단 — 완성 봉(closed_candle, prev_closed_candle) 기준
        adx = float(closed_candle.get('adx', 0))
        adx_prev = float(prev_closed_candle.get('adx', 0))
        adx_slope = adx - adx_prev
        if adx > adx_trend_threshold:
            if adx_slope < 0:
                regime = 'weakening_trend'
            else:
                regime = 'trend'
        else:
            regime = 'range'
        
        # 3) 신호 생성: EMA/크로스는 완성 봉 기준, BB 터치는 closed_candle의 BB + 실시간 current_price
        signal = 'hold'
        decision_reason_parts = []
        
        ema_short = closed_candle.get('ema_short', 0)
        ema_long = closed_candle.get('ema_long', 0)
        rsi = closed_candle.get('rsi', 50)
        # 볼린저: 완성 봉의 밴드 값 사용. 터치 여부는 실시간 current_price로 판단(기민한 반응)
        bb_lower = closed_candle.get('bb_lower', 0)
        bb_upper = closed_candle.get('bb_upper', 0)
        
        mtf_info = load_higher_timeframe_indicators(ticker, timeframe, count=50, current_price=current_price)
        mtf_blocked = False
        if mtf_info and mtf_info.get('is_uptrend') is False:
            mtf_blocked = True
            decision_reason_parts.append(f"상위 타임프레임({mtf_info['timeframe']}) 하락장으로 매수 보류")
            decision_reason_parts.append(f"상위 현재가({mtf_info['current_price']:.0f}) < EMA50({mtf_info['ema_long']:.0f})")
        
        # 추세장: 완성 봉 기준 EMA 골든/데드 크로스 (iloc[-2] vs iloc[-3])
        if regime == 'trend':
            if (ema_short or ema_long) and not (pd.isna(ema_short) or pd.isna(ema_long)):
                prev_ema_short = prev_closed_candle.get('ema_short', 0)
                prev_ema_long = prev_closed_candle.get('ema_long', 0)
                if pd.isna(prev_ema_short):
                    prev_ema_short = 0
                if pd.isna(prev_ema_long):
                    prev_ema_long = 0
                if ema_short > ema_long and prev_ema_short <= prev_ema_long:
                    if not mtf_blocked:
                        signal = 'buy'
                        decision_reason_parts.append(f'추세장(ADX={adx:.1f}) 완성봉 기준 EMA 골든크로스')
                        decision_reason_parts.append(f'EMA 단기({ema_short:.0f}) > 장기({ema_long:.0f})')
                        if rsi < 70:
                            decision_reason_parts.append(f'RSI({rsi:.1f}) 과매수 구간 아님')
                    else:
                        signal = 'hold'
                elif ema_short < ema_long and prev_ema_short >= prev_ema_long:
                    signal = 'sell'
                    decision_reason_parts.append(f'추세장(ADX={adx:.1f}) 완성봉 기준 EMA 데드크로스')
                    decision_reason_parts.append(f'EMA 단기({ema_short:.0f}) < 장기({ema_long:.0f})')
        elif regime == 'weakening_trend':
            if (ema_short or ema_long) and not (pd.isna(ema_short) or pd.isna(ema_long)):
                prev_ema_short = prev_closed_candle.get('ema_short', 0)
                prev_ema_long = prev_closed_candle.get('ema_long', 0)
                if pd.isna(prev_ema_short):
                    prev_ema_short = 0
                if pd.isna(prev_ema_long):
                    prev_ema_long = 0
                if ema_short > ema_long and prev_ema_short <= prev_ema_long:
                    if not mtf_blocked:
                        signal = 'buy'
                        decision_reason_parts.append(f'약세 추세장(ADX={adx:.1f}) 완성봉 기준 EMA 골든크로스, 비중 50%')
                    else:
                        signal = 'hold'
                elif ema_short < ema_long and prev_ema_short >= prev_ema_long:
                    signal = 'sell'
                    decision_reason_parts.append(f'약세 추세장(ADX={adx:.1f}) 완성봉 기준 EMA 데드크로스')
        else:
            # 횡보장: BB는 완성 봉(closed_candle) 값, 가격은 실시간 current_price로 터치 판단
            if current_price and current_price > 0 and bb_lower and bb_upper and bb_upper > bb_lower:
                if current_price <= bb_lower * 1.01:
                    if not mtf_blocked:
                        signal = 'buy'
                        decision_reason_parts.append(f'횡보장(ADX={adx:.1f}) BB하단 터치(완성봉 밴드+실시간가격)')
                        decision_reason_parts.append(f'현재가({current_price:.0f}) <= 하단({bb_lower:.0f})')
                    else:
                        signal = 'hold'
                elif current_price >= bb_upper * 0.99:
                    signal = 'sell'
                    decision_reason_parts.append(f'횡보장(ADX={adx:.1f}) BB상단 터치(완성봉 밴드+실시간가격)')
                    decision_reason_parts.append(f'현재가({current_price:.0f}) >= 상단({bb_upper:.0f})')
        
        decision_reason = ' | '.join(decision_reason_parts) if decision_reason_parts else '신호 없음'
        
        # 4) 동적 리스크 조정 (약세 추세장일 경우 추가 축소)
        if use_dynamic_risk:
            position_size, risk_adjustments = calculate_adjusted_position_size(
                account_value=account_value,
                risk_per_trade_pct=0.02,
                stop_loss_pct=0.05,
                use_dynamic_adjustment=True
            )
        else:
            position_size = account_value * 0.02 / 0.05  # 기본 계산
            risk_adjustments = {'position_size_multiplier': 1.0, 'is_defensive_mode': False}
        
        # 약세 추세장일 경우 포지션 크기 추가 축소 (50%)
        if regime == 'weakening_trend' and signal == 'buy':
            position_size = position_size * 0.5
            risk_adjustments['weakening_trend_reduction'] = True
            risk_adjustments['position_size_multiplier'] = risk_adjustments.get('position_size_multiplier', 1.0) * 0.5
        
        # 같은 봉에 대해 buy/sell 신호 중복 방지 (기준: 완성 봉 시각)
        ts_now = closed_candle['time']
        if hasattr(ts_now, 'to_pydatetime'):
            ts_now = ts_now.to_pydatetime()
        session = get_session()
        try:
            skip_duplicate_write = False
            try:
                if signal in ('buy', 'sell'):
                    # 이 봉(ts_now)에 대해 이미 buy/sell 기록이 있는지 확인
                    existing = session.query(AnalysisResult).filter(
                        AnalysisResult.ticker == ticker,
                        AnalysisResult.signal.in_(['buy', 'sell'])
                    ).order_by(AnalysisResult.timestamp.desc()).limit(50).all()
                    def _norm(t):
                        if t is None:
                            return None
                        try:
                            return int(pd.Timestamp(t).timestamp()) // 3600  # 1시간 봉 기준 동일 봉
                        except Exception:
                            return None
                    ts_key = _norm(ts_now)
                    for ex in existing:
                        if ex.timestamp and _norm(ex.timestamp) == ts_key:
                            # 같은 봉에서 동일 신호만 막음. 매도(sell)는 시간 락 무시하고 즉시 허용 (익절/손절)
                            if ex.signal == signal:
                                signal = 'hold'
                                decision_reason = (decision_reason or '') + ' | (같은 봉 중복 신호 방지)'
                                skip_duplicate_write = True
                            break
            except Exception:
                pass
            if not skip_duplicate_write:
                try:
                    signal_value = 1 if signal == 'buy' else (-1 if signal == 'sell' else 0)
                    signal_record = Signal(
                        ticker=ticker,
                        timeframe=timeframe,
                        ts=ts_now,
                        signal=signal_value,
                        algo_version='ema_regime_v1',
                        params=json.dumps({
                            'adx_trend_threshold': adx_trend_threshold,
                            'regime': regime
                        }, default=_json_default),
                        meta=json.dumps({
                            'decision_reason': decision_reason,
                            'adx': adx,
                            'ema_short': float(ema_short),
                            'ema_long': float(ema_long),
                            'rsi': float(rsi),
                        }, default=_json_default)
                    )
                    session.add(signal_record)
                    session.commit()
                except Exception as e:
                    print(f'⚠️ Signal 저장 실패: {e}')
                    session.rollback()
            # 6) analysis_results 테이블에 상세 로깅 (중복 봉이면 스킵)
            if not skip_duplicate_write:
                try:
                    analysis_record = AnalysisResult(
                        ticker=ticker,
                        timestamp=ts_now,
                        signal=signal,
                        price=current_price,
                        change_rate=0.0,  # 필요시 계산
                        position_size=position_size,
                        regime=regime,  # 쿼리 통계용 독립 컬럼 (추세/횡보장)
                        is_defensive_mode=bool(risk_adjustments.get('is_defensive_mode', False)),  # 쿼리 통계용
                        risk_filters=json.dumps({
                            'is_defensive_mode': risk_adjustments.get('is_defensive_mode', False),
                            'consecutive_losses': risk_adjustments.get('consecutive_losses', 0),
                            'win_rate': risk_adjustments.get('win_rate', 0.0),
                            'position_size_multiplier': risk_adjustments.get('position_size_multiplier', 1.0),
                            'atr_trailing_multiplier': risk_adjustments.get('atr_trailing_multiplier', 2.0),
                            'weakening_trend': regime == 'weakening_trend',
                            'adx_slope': float(adx_slope),
                            'mtf_blocked': mtf_blocked,
                            'mtf_timeframe': mtf_info.get('timeframe') if mtf_info else None,
                            'mtf_is_uptrend': mtf_info.get('is_uptrend') if mtf_info else None,
                        }, default=_json_default),
                        analysis_data=json.dumps({
                            'regime': regime,
                            'adx': adx,
                            'adx_slope': float(adx_slope),
                            'adx_prev': float(adx_prev),
                            'ema_short': float(ema_short),
                            'ema_long': float(ema_long),
                            'rsi': float(rsi),
                            'atr': float(closed_candle.get('atr', 0)),
                            'atr_raw': float(closed_candle.get('atr_raw', closed_candle.get('atr', 0))),
                            'bb_lower': float(bb_lower),
                            'bb_middle': float(closed_candle.get('bb_middle', 0)),
                            'bb_upper': float(bb_upper),
                            'mtf_info': mtf_info if mtf_info else None,
                        }, default=_json_default),
                        decision_reason=decision_reason
                    )
                    session.add(analysis_record)
                    session.commit()
                except Exception as e:
                    print(f'⚠️ AnalysisResult 저장 실패: {e}')
                    session.rollback()
        finally:
            session.close()
        
        return {
            'signal': signal,
            'regime': regime,
            'decision_reason': decision_reason,
            'position_size': position_size,
            'risk_adjustments': risk_adjustments,
            'indicators': {
                'adx': adx,
                'ema_short': float(ema_short),
                'ema_long': float(ema_long),
                'rsi': float(rsi),
                'atr': float(closed_candle.get('atr', 0)),
                'bb_lower': float(bb_lower),
                'bb_upper': float(bb_upper),
            }
        }
    except Exception as e:
        print(f'⚠️ 종합 신호 생성 실패 ({ticker}): {e}')
        return {
            'signal': 'hold',
            'regime': 'unknown',
            'decision_reason': f'오류: {str(e)}',
            'position_size': 0.0,
            'risk_adjustments': {},
            'indicators': {}
        }
