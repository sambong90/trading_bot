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


def _candle_bucket_seconds(timeframe: str) -> int:
    """Convert timeframe string to candle bucket size in seconds for duplicate-signal detection."""
    if not timeframe:
        return 3600
    tf = str(timeframe).strip().lower()
    if tf == 'day':
        return 86400
    if tf.startswith('minute'):
        try:
            n = int(tf.replace('minute', '').strip() or 60)
            return max(60, n * 60)
        except ValueError:
            pass
    return 3600


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
            
            # indicators JSON에서 ADX, BB, OBV, BB Width 추출
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
                if 'obv' in indicators:
                    row['obv'] = indicators['obv']
                if 'obv_sma' in indicators:
                    row['obv_sma'] = indicators['obv_sma']
                if 'bb_width' in indicators:
                    row['bb_width'] = indicators['bb_width']
            
            data.append(row)

        df = pd.DataFrame(data)
        if len(df) == 0:
            return df
        df['time'] = pd.to_datetime(df['time'])
        # Merge OHLCV volume so vol_ratio is meaningful (TechnicalIndicator has no volume)
        try:
            from trading_bot.data_manager import load_ohlcv_from_db
            df_ohlcv = load_ohlcv_from_db(ticker, timeframe, count=count)
            if df_ohlcv is not None and not df_ohlcv.empty and 'volume' in df_ohlcv.columns and 'time' in df_ohlcv.columns:
                ohlcv_sub = df_ohlcv[['time', 'volume']].copy()
                ohlcv_sub['time'] = pd.to_datetime(ohlcv_sub['time'])
                df['_time'] = pd.to_datetime(df['time'])
                if hasattr(df['_time'].dtype, 'tz') and df['_time'].dt.tz is not None:
                    df['_time'] = df['_time'].dt.tz_localize(None)
                if hasattr(ohlcv_sub['time'].dtype, 'tz') and ohlcv_sub['time'].dt.tz is not None:
                    ohlcv_sub['time'] = ohlcv_sub['time'].dt.tz_localize(None)
                df = df.merge(ohlcv_sub.rename(columns={'time': '_time'}), on='_time', how='left')
                df.drop(columns=['_time'], inplace=True)
        except Exception:
            pass
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
    is_global_bull_market: bool = True,
    position_qty: float = 0.0,
    current_roi: float = 0.0,
    scale_out_stage: int = 0,
    avg_buy_price: float = 0.0,  # [NEW] ATR Scale-Out 기준 계산용
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
    5. BTC 거시 필터: 하락장일 때 buy → ADX>=40이면 예외 매수(비중 50%), 아니면 hold
    6. signals 테이블에 신호 저장
    7. analysis_results 테이블에 상세 분석 결과 저장
    
    Parameters:
    - ticker: 코인 티커
    - timeframe: 시간 간격
    - current_price: 현재 가격 (None이면 DB에서 최신 close 사용)
    - account_value: 계좌 총 가치
    - adx_trend_threshold: ADX 추세 임계값 (기본 25.0)
    - use_dynamic_risk: 동적 리스크 조정 사용 여부
    - is_global_bull_market: BTC 거시 상승장 여부. False이면 buy 시 ADX>=40일 때만 예외 매수(50% 비중)
    
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
        from trading_bot.config import (
            RSI_BUY_MIN, RSI_BUY_MAX, RSI_SELL_MIN,
            SCALE_OUT_ATR_MULT_1, SCALE_OUT_ATR_MULT_2,
            SCALE_OUT_ROI_FALLBACK_1, SCALE_OUT_ROI_FALLBACK_2,
            VOL_SCALE_HIGH, VOL_SCALE_MID,
        )
        import json

        # [IMPROVED] ATR 기반 동적 Scale-Out (폴백: ROI 고정값)
        def _should_scale_out(stage_target, avg_buy, cur_price, atr_v, so_stage):
            if so_stage >= stage_target:
                return False
            if avg_buy and avg_buy > 0 and atr_v and atr_v > 0:
                mult = SCALE_OUT_ATR_MULT_1 if stage_target == 1 else SCALE_OUT_ATR_MULT_2
                target_price = avg_buy + atr_v * mult
                return cur_price >= target_price
            if avg_buy and avg_buy > 0:
                roi = (cur_price - avg_buy) / avg_buy * 100
                th = SCALE_OUT_ROI_FALLBACK_1 if stage_target == 1 else SCALE_OUT_ROI_FALLBACK_2
                return roi >= th
            return False
        
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
        
        # 2) Regime 판단 — ADX, slope, BB width (squeeze) to reduce whipsaws; add transition
        adx = float(closed_candle.get('adx', 0))
        adx_prev = float(prev_closed_candle.get('adx', 0))
        adx_slope = adx - adx_prev
        bb_width = float(closed_candle.get('bb_width', 0) or 0)
        # thresholds: squeeze = small BB width; avoid calling trend in low-vol chop
        BB_SQUEEZE_MAX = 0.05
        BB_TREND_MIN = 0.02
        ADX_NEAR_MARGIN = 3.0
        if adx <= adx_trend_threshold and bb_width > 0 and bb_width < BB_SQUEEZE_MAX:
            regime = 'range'
        elif adx > adx_trend_threshold and bb_width >= BB_TREND_MIN:
            if adx_slope < 0:
                regime = 'weakening_trend'
            else:
                regime = 'trend'
        else:
            regime = 'transition'
        decision_reason_parts = []
        decision_reason_parts.append(f"Regime:{regime} (ADX:{adx:.1f} slope:{adx_slope:+.1f} bb_width:{bb_width:.3f})")

        # 3) 신호 생성: EMA/크로스는 완성 봉 기준, BB 터치는 closed_candle의 BB + 실시간 current_price
        # Scale-Out 및 정규 매도 시 sell_size_pct, next_scale_out_stage (반환용)
        signal = 'hold'
        sell_size_pct = 1.0
        buy_size_pct = 1.0
        next_scale_out_stage = None

        ema_short = closed_candle.get('ema_short', 0)
        ema_long = closed_candle.get('ema_long', 0)
        rsi = closed_candle.get('rsi', 50)
        # 볼륨 비율(현재 봉 거래량 / 거래량 MA)은 신호 종류와 무관하게 항상 계산하여 디버깅에 활용
        current_volume = float(closed_candle.get('volume', 0))
        vol_ma = max(1.0, float(closed_candle.get('volume_ma', 1)))
        vol_ratio = current_volume / vol_ma if vol_ma > 0 else 0.0

        def volume_ok(vol_ratio_val, regime_name, is_bull, weakening=False, decoupling=False):
            """Single volume gate: returns (passed, required_threshold). Use same vol_ratio everywhere."""
            if decoupling:
                req = 1.5
                return (vol_ratio_val >= req, req)
            if weakening:
                req = 1.2
                return (vol_ratio_val >= req, req)
            if regime_name == 'trend':
                req = 0.8 if is_bull else 1.0
                return (vol_ratio_val >= req, req)
            if regime_name == 'range':
                req = 0.8
                return (vol_ratio_val >= req, req)
            if regime_name == 'transition':
                req = 1.0
                return (vol_ratio_val >= req, req)
            return (True, 0.0)

        # 볼린저: 완성 봉의 밴드 값 사용. 터치 여부는 실시간 current_price로 판단(기민한 반응)
        bb_lower = closed_candle.get('bb_lower', 0)
        bb_upper = closed_candle.get('bb_upper', 0)
        bb_middle = closed_candle.get('bb_middle', 0)
        # bb_width already set in regime block above
        # OBV 기반 Smart Money 흐름
        obv = float(closed_candle.get('obv', 0) or 0)
        obv_sma = float(closed_candle.get('obv_sma', 0) or 0)
        
        mtf_info = load_higher_timeframe_indicators(ticker, timeframe, count=50, current_price=current_price)
        mtf_blocked = False
        if mtf_info and mtf_info.get('is_uptrend') is False:
            mtf_blocked = True
            decision_reason_parts.append(f"상위 타임프레임({mtf_info['timeframe']}) 하락장으로 매수 보류")
            decision_reason_parts.append(f"상위 현재가({mtf_info['current_price']:.0f}) < EMA50({mtf_info['ema_long']:.0f})")

        # ATR Trailing Stop (Chandelier Exit): recent highest high - 2.5*ATR
        df_ohlcv_20 = load_ohlcv_from_db(ticker, timeframe, count=20)
        try:
            recent_highest = float(df_ohlcv_20['high'].max()) if df_ohlcv_20 is not None and not df_ohlcv_20.empty and 'high' in df_ohlcv_20.columns else None
            if recent_highest is not None and (pd.isna(recent_highest) or recent_highest <= 0):
                recent_highest = None
        except (TypeError, ValueError, KeyError):
            recent_highest = None
        _atr_raw = closed_candle.get('atr_raw') or closed_candle.get('atr') or 0
        atr_val = None
        try:
            if _atr_raw is not None and not (isinstance(_atr_raw, float) and (pd.isna(_atr_raw) or _atr_raw <= 0)):
                atr_val = float(_atr_raw)
        except (TypeError, ValueError):
            pass
        # [IMPROVED] 수익 구간별 ATR 배수로 트레일링 스탑 계산
        try:
            from trading_bot.config import TS_MULT_LOW, TS_MULT_MID, TS_MULT_HIGH
        except Exception:
            TS_MULT_LOW, TS_MULT_MID, TS_MULT_HIGH = 3.0, 2.0, 1.5
        if current_roi >= 15.0:
            ts_mult = TS_MULT_HIGH
        elif current_roi >= 5.0:
            ts_mult = TS_MULT_MID
        else:
            ts_mult = TS_MULT_LOW
        trailing_stop_price = None
        if recent_highest is not None and atr_val is not None and atr_val > 0:
            trailing_stop_price = recent_highest - (atr_val * ts_mult)
            if position_qty > 0:
                decision_reason_parts.append(
                    f'TrailingStop: {trailing_stop_price:.0f} (최고가{recent_highest:.0f} - ATR×{ts_mult})'
                )

        # ----- Whale Accumulation (Smart Money) Detection -----
        accumulation_mode = False
        bb_mid_val = float(bb_middle or 0)
        squeeze = (regime == 'range' or adx < adx_trend_threshold) and bb_width > 0 and bb_width < 0.05
        smart_flow = obv_sma is not None and obv > obv_sma
        safe_entry = current_price is not None and bb_mid_val > 0 and current_price <= bb_mid_val
        if squeeze and smart_flow and safe_entry:
            accumulation_mode = True
            buy_size_pct = 0.5
            signal = 'buy'
            decision_reason_parts.append(
                f"[Accumulation Detected] BB_Width: {bb_width:.3f} < 0.05, OBV > OBV_SMA. "
                f"Pre-empting breakout with 50% size."
            )
        
        # 추세장: Scale-Out(25-25-50) → ATR Trailing Stop → EMA 골든/데드 크로스
        avg_buy = float(avg_buy_price or 0.0)
        atr_for_scale = float(atr_val or 0.0) if atr_val is not None else 0.0
        if regime == 'trend':
            if position_qty > 0 and _should_scale_out(2, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
                signal = 'sell'
                sell_size_pct = 0.33
                next_scale_out_stage = 2
                decision_reason_parts.append(
                    f'Scale-Out Stage 2: ATR 배수 {SCALE_OUT_ATR_MULT_2}x 도달 (폴백ROI {SCALE_OUT_ROI_FALLBACK_2}%)'
                )
            elif position_qty > 0 and _should_scale_out(1, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
                signal = 'sell'
                sell_size_pct = 0.25
                next_scale_out_stage = 1
                decision_reason_parts.append(
                    f'Scale-Out Stage 1: ATR 배수 {SCALE_OUT_ATR_MULT_1}x 도달 (폴백ROI {SCALE_OUT_ROI_FALLBACK_1}%)'
                )
            elif current_price is not None and current_price > 0 and trailing_stop_price is not None and current_price < trailing_stop_price:
                signal = 'sell'
                sell_size_pct = 1.0
                next_scale_out_stage = 0
                decision_reason_parts.append(f"ATR Trailing Stop triggered (Price {current_price:.0f} < Stop Line {trailing_stop_price:.0f})")
            elif (ema_short or ema_long) and not (pd.isna(ema_short) or pd.isna(ema_long)):
                prev_ema_short = prev_closed_candle.get('ema_short', 0)
                prev_ema_long = prev_closed_candle.get('ema_long', 0)
                if pd.isna(prev_ema_short):
                    prev_ema_short = 0
                if pd.isna(prev_ema_long):
                    prev_ema_long = 0
                # [IMPROVED] RSI가 과매도 탈출~과매수 미만 구간일 때만 매수
                if (ema_short > ema_long and prev_ema_short <= prev_ema_long
                        and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX):
                    if not mtf_blocked:
                        ok, req = volume_ok(vol_ratio, 'trend', is_global_bull_market, weakening=False, decoupling=False)
                        if ok:
                            signal = 'buy'
                            decision_reason_parts.append(f'추세장(ADX={adx:.1f}) 완성봉 기준 EMA 골든크로스')
                            decision_reason_parts.append(f'EMA 단기({ema_short:.0f}) > 장기({ema_long:.0f})')
                            decision_reason_parts.append(f'Smart Volume (Vol {vol_ratio:.1f}x >= {req}x)')
                            decision_reason_parts.append(f'RSI({rsi:.1f}) 필터 통과 [{RSI_BUY_MIN}~{RSI_BUY_MAX}]')
                        else:
                            signal = 'hold'
                            decision_reason_parts.append(f'Volume filter failed (Vol ratio: {vol_ratio:.1f}x < req {req}x)')
                    else:
                        signal = 'hold'
                elif ema_short < ema_long and prev_ema_short >= prev_ema_long:
                    signal = 'sell'
                    sell_size_pct = 1.0
                    next_scale_out_stage = 0
                    decision_reason_parts.append(f'추세장(ADX={adx:.1f}) 완성봉 기준 EMA 데드크로스')
                    decision_reason_parts.append(f'EMA 단기({ema_short:.0f}) < 장기({ema_long:.0f})')
        elif regime == 'weakening_trend':
            if position_qty > 0 and _should_scale_out(2, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
                signal = 'sell'
                sell_size_pct = 0.33
                next_scale_out_stage = 2
                decision_reason_parts.append(
                    f'Scale-Out Stage 2: ATR 배수 {SCALE_OUT_ATR_MULT_2}x 도달 (폴백ROI {SCALE_OUT_ROI_FALLBACK_2}%)'
                )
            elif position_qty > 0 and _should_scale_out(1, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
                signal = 'sell'
                sell_size_pct = 0.25
                next_scale_out_stage = 1
                decision_reason_parts.append(
                    f'Scale-Out Stage 1: ATR 배수 {SCALE_OUT_ATR_MULT_1}x 도달 (폴백ROI {SCALE_OUT_ROI_FALLBACK_1}%)'
                )
            elif current_price is not None and current_price > 0 and trailing_stop_price is not None and current_price < trailing_stop_price:
                signal = 'sell'
                sell_size_pct = 1.0
                next_scale_out_stage = 0
                decision_reason_parts.append(f"ATR Trailing Stop triggered (Price {current_price:.0f} < Stop Line {trailing_stop_price:.0f})")
            elif (ema_short or ema_long) and not (pd.isna(ema_short) or pd.isna(ema_long)):
                prev_ema_short = prev_closed_candle.get('ema_short', 0)
                prev_ema_long = prev_closed_candle.get('ema_long', 0)
                if pd.isna(prev_ema_short):
                    prev_ema_short = 0
                if pd.isna(prev_ema_long):
                    prev_ema_long = 0
                if (ema_short > ema_long and prev_ema_short <= prev_ema_long
                        and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX):
                    if not mtf_blocked:
                        ok, req = volume_ok(vol_ratio, 'weakening_trend', is_global_bull_market, weakening=True, decoupling=False)
                        if ok:
                            signal = 'buy'
                            decision_reason_parts.append(f'약세 추세장(ADX={adx:.1f}) 완성봉 기준 EMA 골든크로스, 비중 50%')
                            decision_reason_parts.append(f'Smart Volume (Vol {vol_ratio:.1f}x >= {req}x)')
                            decision_reason_parts.append(f'RSI({rsi:.1f}) 필터 통과 [{RSI_BUY_MIN}~{RSI_BUY_MAX}]')
                        else:
                            signal = 'hold'
                            decision_reason_parts.append(f'Volume filter failed (Vol ratio: {vol_ratio:.1f}x < req {req}x)')
                    else:
                        signal = 'hold'
                elif ema_short < ema_long and prev_ema_short >= prev_ema_long:
                    signal = 'sell'
                    sell_size_pct = 1.0
                    next_scale_out_stage = 0
                    decision_reason_parts.append(f'약세 추세장(ADX={adx:.1f}) 완성봉 기준 EMA 데드크로스')
        elif regime == 'transition':
            # Default hold in transition; allow only strong confirmation with reduced size
            if position_qty > 0:
                if _should_scale_out(2, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
                    signal = 'sell'
                    sell_size_pct = 0.33
                    next_scale_out_stage = 2
                    decision_reason_parts.append("Scale-Out Stage 2 (transition)")
                elif _should_scale_out(1, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
                    signal = 'sell'
                    sell_size_pct = 0.25
                    next_scale_out_stage = 1
                    decision_reason_parts.append("Scale-Out Stage 1 (transition)")
                elif current_price and trailing_stop_price and current_price < trailing_stop_price:
                    signal = 'sell'
                    sell_size_pct = 1.0
                    next_scale_out_stage = 0
            # Optional: allow buy in transition only with strong confirmation and reduced size
            elif (ema_short or ema_long) and not (pd.isna(ema_short) or pd.isna(ema_long)):
                prev_ema_short = prev_closed_candle.get('ema_short', 0)
                prev_ema_long = prev_closed_candle.get('ema_long', 0)
                if pd.isna(prev_ema_short): prev_ema_short = 0
                if pd.isna(prev_ema_long): prev_ema_long = 0
                if (ema_short > ema_long and prev_ema_short <= prev_ema_long and not mtf_blocked
                        and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX):
                    ok, req = volume_ok(vol_ratio, 'transition', is_global_bull_market, weakening=False, decoupling=False)
                    if ok:
                        signal = 'buy'
                        buy_size_pct = 0.5
                        decision_reason_parts.append(f'Transition EMA 골든크로스 (비중 50%, Vol {vol_ratio:.1f}x >= {req}x)')
                        decision_reason_parts.append(f'RSI({rsi:.1f}) 필터 통과 [{RSI_BUY_MIN}~{RSI_BUY_MAX}]')
                    elif not ok:
                        decision_reason_parts.append(f'Transition: Volume filter (Vol {vol_ratio:.1f}x < req {req}x)')
        else:
            # 횡보장(range): 매수=BB하단 터치 + RSI 필터 + volume gate
            if current_price and current_price > 0 and bb_lower and bb_upper and bb_upper > bb_lower:
                # [IMPROVED] BB 하단 터치 + RSI 필터
                if current_price <= bb_lower * 1.01 and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX:
                    if not mtf_blocked:
                        ok, req = volume_ok(vol_ratio, 'range', is_global_bull_market, weakening=False, decoupling=False)
                        if ok:
                            signal = 'buy'
                            decision_reason_parts.append(f'횡보장(ADX={adx:.1f}) BB하단 터치(완성봉 밴드+실시간가격)')
                            decision_reason_parts.append(f'현재가({current_price:.0f}) <= 하단({bb_lower:.0f})')
                            decision_reason_parts.append(f'Smart Volume (Vol {vol_ratio:.1f}x >= {req}x)')
                            decision_reason_parts.append(f'RSI({rsi:.1f}) 필터 통과 [{RSI_BUY_MIN}~{RSI_BUY_MAX}]')
                        else:
                            decision_reason_parts.append(f'Volume filter failed (Vol ratio: {vol_ratio:.1f}x < req {req}x)')
                    else:
                        signal = 'hold'
                else:
                    # 매도: 완성봉의 고가(High)가 BB 상단을 강하게 터치한 '이벤트' 시점만 (직전 봉은 미터치)
                    df_ohlcv = load_ohlcv_from_db(ticker, timeframe, count=5)
                    if len(df_ohlcv) >= 3:
                        closed_high = float(df_ohlcv.iloc[-2].get('high', 0) or 0)
                        prev_high = float(df_ohlcv.iloc[-3].get('high', 0) or 0)
                        if closed_high >= bb_upper * 0.99 and prev_high < bb_upper * 0.99:
                            signal = 'sell'
                            sell_size_pct = 1.0
                            next_scale_out_stage = 0
                            decision_reason_parts.append(f'횡보장(ADX={adx:.1f}) 완성봉 고가 BB상단 터치 이벤트')
                            decision_reason_parts.append(f'고가({closed_high:.0f}) >= 상단({bb_upper:.0f}), 직전봉 미터치')
                    # (기존: current_price >= bb_upper 상태만으로 매도 → 제거하여 반복 sell 방지)

        # [NEW] RSI 과매수 구간 매도 강화 (보유 포지션 있고, 이미 sell 신호 발생 시)
        if signal == 'sell' and position_qty > 0 and rsi >= RSI_SELL_MIN:
            decision_reason_parts.append(f'RSI({rsi:.1f}) 과매수 구간 매도 강화')
        
        # 어떤 신호가 나왔든, 디버깅을 위해 마지막에 항상 볼륨 비율 정보를 부가
        decision_reason_parts.append(f'[Vol: {vol_ratio:.1f}x]')
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

        # Accumulation 모드일 때는 기본 포지션 크기를 50%로 축소 (선발대 진입)
        if accumulation_mode and position_size > 0:
            position_size = position_size * 0.5
        
        # 약세 추세장 또는 transition 매수 시 포지션 크기 축소 (50%)
        if (regime == 'weakening_trend' or regime == 'transition') and signal == 'buy':
            position_size = position_size * 0.5
            risk_adjustments['weakening_trend_reduction'] = True
            risk_adjustments['position_size_multiplier'] = risk_adjustments.get('position_size_multiplier', 1.0) * 0.5

        # [NEW] 변동성 기반 포지션 자동 축소
        try:
            atr_series = df_indicators['atr'].dropna().tail(20)
            avg_atr_20 = float(atr_series.mean()) if len(atr_series) >= 5 else 0.0
        except Exception:
            avg_atr_20 = 0.0
        vol_scale_multiplier = 1.0
        if avg_atr_20 > 0 and atr_val and atr_val > 0:
            atr_ratio = atr_val / avg_atr_20
            if atr_ratio >= VOL_SCALE_HIGH:
                vol_scale_multiplier = 0.5
                decision_reason_parts.append(f'변동성 급등(ATR {atr_ratio:.1f}x) → 포지션 50% 축소')
            elif atr_ratio >= VOL_SCALE_MID:
                vol_scale_multiplier = 0.75
                decision_reason_parts.append(f'변동성 상승(ATR {atr_ratio:.1f}x) → 포지션 25% 축소')
        if signal == 'buy' and vol_scale_multiplier < 1.0:
            position_size = position_size * vol_scale_multiplier
            risk_adjustments['vol_scale_multiplier'] = vol_scale_multiplier

        # BTC 거시 장세 필터 + 경주마(디커플링) 예외: 하락장에서도 ADX>=40 초강세는 예외 매수(비중 50%). 볼륨 1.5x 필수.
        if not is_global_bull_market and signal == 'buy':
            if adx >= 40.0:
                ok, req = volume_ok(vol_ratio, 'trend', False, weakening=False, decoupling=True)
                if ok:
                    position_size = position_size * 0.5
                    risk_adjustments['btc_bear_decoupling_bypass'] = True
                    risk_adjustments['position_size_multiplier'] = risk_adjustments.get('position_size_multiplier', 1.0) * 0.5
                    decision_reason_parts.append('BTC 하락장이지만 초강세(ADX>=40) 감지로 예외 매수 (비중 50% 축소)')
                    decision_reason_parts.append(f'Smart Volume (Vol {vol_ratio:.1f}x >= {req}x)')
                    decision_reason = ' | '.join(decision_reason_parts) if decision_reason_parts else '신호 없음'
                else:
                    signal = 'hold'
                    decision_reason_parts.append(f'Volume filter failed (Vol ratio: {vol_ratio:.1f}x < req {req}x)')
                    decision_reason = ' | '.join(decision_reason_parts) if decision_reason_parts else '신호 없음'
            else:
                signal = 'hold'
                decision_reason_parts.append('BTC 하락장 필터에 의한 매수 보류')
                decision_reason = ' | '.join(decision_reason_parts) if decision_reason_parts else '신호 없음'
        
        # 같은 봉에 대해 buy/sell 신호 중복 방지 (timeframe별 봉 버킷 사용)
        ts_now = closed_candle['time']
        if hasattr(ts_now, 'to_pydatetime'):
            ts_now = ts_now.to_pydatetime()
        bucket_seconds = _candle_bucket_seconds(timeframe)
        session = get_session()
        try:
            skip_duplicate_write = False
            try:
                if signal in ('buy', 'sell'):
                    # 이 봉(ts_now)에 대해 이미 buy/sell 기록이 있는지 확인 (동일 방향만 블록)
                    existing = session.query(AnalysisResult).filter(
                        AnalysisResult.ticker == ticker,
                        AnalysisResult.signal.in_(['buy', 'sell'])
                    ).order_by(AnalysisResult.timestamp.desc()).limit(50).all()
                    def _norm(t):
                        if t is None:
                            return None
                        try:
                            return int(pd.Timestamp(t).timestamp()) // bucket_seconds
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

        # AI 전용 분석 로그: 매수/매도 신호 발생 시에만 기록 (| 구분 정형 포맷)
        # 엽전 코인(SHIB, PEPE 등): 10 미만이면 소수 4자리, 이상이면 정수로 출력
        def _fmt_num(v):
            if v is None or (isinstance(v, float) and (v != v)):
                return "0"
            try:
                x = float(v)
            except (TypeError, ValueError):
                return "0"
            return f"{x:.4f}" if abs(x) < 10 else f"{x:.0f}"

        if signal in ('buy', 'sell'):
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(
                    event_type='STRATEGY',
                    ticker=ticker,
                    signal=signal,
                    price=current_price,
                    avg_buy_price=avg_buy_price,
                    regime=regime,
                    timeframe=timeframe,
                    adx=adx,
                    rsi=float(rsi) if not pd.isna(rsi) else None,
                    atr=float(closed_candle.get('atr', 0)),
                    vol_ratio=float(vol_ratio),
                    position_size=position_size,
                    size_pct=buy_size_pct if signal == 'buy' else sell_size_pct,
                    decision_reason=decision_reason,
                    roi=current_roi if current_roi else None,
                    extra={
                        'ema_short': float(ema_short) if not pd.isna(ema_short) else 0,
                        'ema_long': float(ema_long) if not pd.isna(ema_long) else 0,
                        'bb_lower': float(bb_lower),
                        'bb_upper': float(bb_upper),
                        'scale_out_stage': scale_out_stage,
                        'is_global_bull_market': is_global_bull_market,
                        'mtf_blocked': mtf_blocked,
                    },
                )
            except Exception:
                pass

        return {
            'signal': signal,
            'regime': regime,
            'decision_reason': decision_reason,
            'position_size': position_size,
            'risk_adjustments': risk_adjustments,
            'sell_size_pct': sell_size_pct,
            'size_pct': buy_size_pct,
            'next_scale_out_stage': next_scale_out_stage,
            'indicators': {
                'adx': adx,
                'ema_short': float(ema_short),
                'ema_long': float(ema_long),
                'rsi': float(rsi),
                'atr': float(closed_candle.get('atr', 0)),
                'bb_lower': float(bb_lower),
                'bb_upper': float(bb_upper),
                'vol_ratio': float(vol_ratio),
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

