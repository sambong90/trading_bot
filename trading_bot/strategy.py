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


# ---------------------------------------------------------------------------
# [M6 FIX] Module-level helper functions extracted from the monolithic
# generate_comprehensive_signal_with_logging function.
# Each function handles a single, well-defined responsibility.
# ---------------------------------------------------------------------------

def _should_scale_out(stage_target, avg_buy, cur_price, atr_v, so_stage):
    """ATR-based scale-out trigger with ROI fallback.

    Returns True when the price has moved far enough above avg_buy to justify
    a partial exit at stage_target (1 or 2).
    """
    from trading_bot.config import (
        SCALE_OUT_ATR_MULT_1, SCALE_OUT_ATR_MULT_2,
        SCALE_OUT_ROI_FALLBACK_1, SCALE_OUT_ROI_FALLBACK_2,
    )
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


def _volume_ok(vol_ratio_val, regime_name, is_bull, weakening=False, decoupling=False):
    """Volume gate: returns (passed: bool, required_threshold: float)."""
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


def _determine_regime(closed_candle, prev_closed_candle, adx_trend_threshold):
    """Classify market regime from ADX strength/slope and BB width.

    Returns:
        (regime, adx, adx_prev, adx_slope, bb_width, reason_part)
        regime: 'trend' | 'weakening_trend' | 'transition' | 'range'
    """
    adx = float(closed_candle.get('adx', 0))
    adx_prev = float(prev_closed_candle.get('adx', 0))
    adx_slope = adx - adx_prev
    bb_width = float(closed_candle.get('bb_width', 0) or 0)

    BB_SQUEEZE_MAX = 0.05
    BB_TREND_MIN = 0.02

    if adx <= adx_trend_threshold and bb_width > 0 and bb_width < BB_SQUEEZE_MAX:
        regime = 'range'
    elif adx > adx_trend_threshold and bb_width >= BB_TREND_MIN:
        regime = 'weakening_trend' if adx_slope < 0 else 'trend'
    else:
        regime = 'transition'

    reason_part = f"Regime:{regime} (ADX:{adx:.1f} slope:{adx_slope:+.1f} bb_width:{bb_width:.3f})"
    return regime, adx, adx_prev, adx_slope, bb_width, reason_part


def _compute_trailing_stop(ticker, timeframe, closed_candle, position_qty, avg_buy_price, current_roi):
    """Compute Chandelier ATR trailing-stop (recent high − ATR × multiplier).

    Returns:
        (trailing_stop_price | None, atr_val | None, reason_part | None)
        reason_part is non-None only when there is a valid stop and position_qty > 0.
    """
    from trading_bot.data_manager import load_ohlcv_from_db

    _entry_ts = None
    if position_qty > 0 and avg_buy_price > 0:
        try:
            from trading_bot.balanced_plus import last_buy_ts as _lbt
            _raw_ets = _lbt(ticker)
            if _raw_ets is not None:
                _ets_pd = pd.Timestamp(_raw_ets)
                if _ets_pd.tz is not None:
                    _entry_ts = _ets_pd.tz_convert('UTC').tz_localize(None)
                else:
                    _entry_ts = _ets_pd
        except Exception:
            pass

    df_ohlcv_20 = load_ohlcv_from_db(ticker, timeframe, count=20)
    try:
        if (_entry_ts is not None and df_ohlcv_20 is not None
                and not df_ohlcv_20.empty and 'high' in df_ohlcv_20.columns):
            _col_times = pd.to_datetime(df_ohlcv_20['time'])
            if _col_times.dt.tz is not None:
                _col_times = _col_times.dt.tz_convert('UTC').dt.tz_localize(None)
            _df_since = df_ohlcv_20[_col_times >= _entry_ts]
            recent_highest = (
                float(_df_since['high'].max()) if not _df_since.empty
                else float(df_ohlcv_20['high'].max())
            )
        else:
            recent_highest = (
                float(df_ohlcv_20['high'].max())
                if df_ohlcv_20 is not None and not df_ohlcv_20.empty and 'high' in df_ohlcv_20.columns
                else None
            )
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
    ts_reason = None
    if recent_highest is not None and atr_val is not None and atr_val > 0:
        # Stateful trailing high: ratchet 방식 (절대 내려가지 않음)
        if position_qty > 0:
            try:
                from trading_bot.scale_out_manager import get_trailing_high, update_trailing_high
                stored_high = get_trailing_high(ticker)
                if recent_highest > stored_high:
                    update_trailing_high(ticker, recent_highest)
                else:
                    recent_highest = stored_high  # DB에 저장된 고점이 더 높으면 사용
            except Exception:
                pass

        trailing_stop_price = recent_highest - (atr_val * ts_mult)

        # Breakeven Stop: ROI가 BREAKEVEN_ROI_PCT 이상이면 스탑 하한을 avg_buy로 고정
        if position_qty > 0 and avg_buy_price > 0:
            try:
                from trading_bot.config import BREAKEVEN_ROI_PCT
                if current_roi >= BREAKEVEN_ROI_PCT and trailing_stop_price < avg_buy_price:
                    trailing_stop_price = avg_buy_price
            except Exception:
                pass

        if position_qty > 0:
            ts_reason = f'TrailingStop: {trailing_stop_price:.0f} (최고가{recent_highest:.0f} - ATR×{ts_mult})'

    return trailing_stop_price, atr_val, ts_reason


def _apply_trend_logic(regime, closed_candle, prev_closed_candle, current_price,
                        position_qty, avg_buy, atr_for_scale, scale_out_stage,
                        trailing_stop_price, adx, rsi, vol_ratio,
                        is_global_bull_market, mtf_blocked,
                        initial_buy_size_pct=1.0):
    """Buy/sell signal logic for 'trend' and 'weakening_trend' regimes.

    Scale-out and trailing stop take priority over EMA cross signals.
    The weakening_trend variant applies a stricter volume gate (1.2x).

    Returns:
        (signal, sell_size_pct, buy_size_pct, next_scale_out_stage, reasons)
    """
    from trading_bot.config import (
        RSI_BUY_MIN, RSI_BUY_MAX,
        SCALE_OUT_ATR_MULT_1, SCALE_OUT_ATR_MULT_2,
        SCALE_OUT_ROI_FALLBACK_1, SCALE_OUT_ROI_FALLBACK_2,
    )
    signal = 'hold'
    sell_size_pct = 1.0
    buy_size_pct = initial_buy_size_pct  # preserve accumulation-mode override
    next_scale_out_stage = None
    reasons = []
    is_weakening = (regime == 'weakening_trend')

    ema_short = closed_candle.get('ema_short', 0)
    ema_long = closed_candle.get('ema_long', 0)

    # Priority 1: Scale-out stage 2 (33% sell)
    if position_qty > 0 and _should_scale_out(2, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
        signal = 'sell'
        sell_size_pct = 0.33
        next_scale_out_stage = 2
        reasons.append(f'Scale-Out Stage 2: ATR 배수 {SCALE_OUT_ATR_MULT_2}x 도달 (폴백ROI {SCALE_OUT_ROI_FALLBACK_2}%)')

    # Priority 2: Scale-out stage 1 (25% sell)
    elif position_qty > 0 and _should_scale_out(1, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
        signal = 'sell'
        sell_size_pct = 0.25
        next_scale_out_stage = 1
        reasons.append(f'Scale-Out Stage 1: ATR 배수 {SCALE_OUT_ATR_MULT_1}x 도달 (폴백ROI {SCALE_OUT_ROI_FALLBACK_1}%)')

    # Priority 3: ATR trailing stop (full sell)
    elif (current_price is not None and current_price > 0
            and trailing_stop_price is not None and current_price < trailing_stop_price):
        signal = 'sell'
        sell_size_pct = 1.0
        next_scale_out_stage = 0
        reasons.append(f"ATR Trailing Stop triggered (Price {current_price:.0f} < Stop Line {trailing_stop_price:.0f})")

    # Priority 4: EMA cross
    elif (ema_short or ema_long) and not (pd.isna(ema_short) or pd.isna(ema_long)):
        prev_ema_short = prev_closed_candle.get('ema_short', 0)
        prev_ema_long = prev_closed_candle.get('ema_long', 0)
        if pd.isna(prev_ema_short):
            prev_ema_short = 0
        if pd.isna(prev_ema_long):
            prev_ema_long = 0

        # Golden cross → buy
        if (ema_short > ema_long and prev_ema_short <= prev_ema_long
                and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX):
            if not mtf_blocked:
                ok, req = _volume_ok(vol_ratio, regime, is_global_bull_market, weakening=is_weakening)
                if ok:
                    signal = 'buy'
                    label = '약세 추세장' if is_weakening else '추세장'
                    suffix = ', 비중 50%' if is_weakening else ''
                    reasons.append(f'{label}(ADX={adx:.1f}) 완성봉 기준 EMA 골든크로스{suffix}')
                    reasons.append(f'EMA 단기({ema_short:.0f}) > 장기({ema_long:.0f})')
                    reasons.append(f'Smart Volume (Vol {vol_ratio:.1f}x >= {req}x)')
                    reasons.append(f'RSI({rsi:.1f}) 필터 통과 [{RSI_BUY_MIN}~{RSI_BUY_MAX}]')
                else:
                    signal = 'hold'
                    reasons.append(f'Volume filter failed (Vol ratio: {vol_ratio:.1f}x < req {req}x)')
            # mtf_blocked → remain hold; mtf reason already in decision_reason_parts

        # Dead cross → sell
        elif ema_short < ema_long and prev_ema_short >= prev_ema_long:
            signal = 'sell'
            sell_size_pct = 1.0
            next_scale_out_stage = 0
            label = '약세 추세장' if is_weakening else '추세장'
            reasons.append(f'{label}(ADX={adx:.1f}) 완성봉 기준 EMA 데드크로스')
            reasons.append(f'EMA 단기({ema_short:.0f}) < 장기({ema_long:.0f})')

    return signal, sell_size_pct, buy_size_pct, next_scale_out_stage, reasons


def _apply_transition_logic(closed_candle, prev_closed_candle, current_price,
                             position_qty, avg_buy, atr_for_scale, scale_out_stage,
                             trailing_stop_price, rsi, vol_ratio,
                             is_global_bull_market, mtf_blocked,
                             initial_buy_size_pct=1.0):
    """Buy/sell signal logic for 'transition' regime.

    Exit logic mirrors trend, but new buys require a stronger confirmation
    and are limited to 50% position size.

    Returns:
        (signal, sell_size_pct, buy_size_pct, next_scale_out_stage, reasons)
    """
    from trading_bot.config import RSI_BUY_MIN, RSI_BUY_MAX
    signal = 'hold'
    sell_size_pct = 1.0
    buy_size_pct = initial_buy_size_pct
    next_scale_out_stage = None
    reasons = []

    ema_short = closed_candle.get('ema_short', 0)
    ema_long = closed_candle.get('ema_long', 0)

    if position_qty > 0:
        if _should_scale_out(2, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
            signal = 'sell'
            sell_size_pct = 0.33
            next_scale_out_stage = 2
            reasons.append("Scale-Out Stage 2 (transition)")
        elif _should_scale_out(1, avg_buy, current_price or 0, atr_for_scale, scale_out_stage):
            signal = 'sell'
            sell_size_pct = 0.25
            next_scale_out_stage = 1
            reasons.append("Scale-Out Stage 1 (transition)")
        elif current_price and trailing_stop_price and current_price < trailing_stop_price:
            signal = 'sell'
            sell_size_pct = 1.0
            next_scale_out_stage = 0
    elif (ema_short or ema_long) and not (pd.isna(ema_short) or pd.isna(ema_long)):
        prev_ema_short = prev_closed_candle.get('ema_short', 0)
        prev_ema_long = prev_closed_candle.get('ema_long', 0)
        if pd.isna(prev_ema_short):
            prev_ema_short = 0
        if pd.isna(prev_ema_long):
            prev_ema_long = 0
        if (ema_short > ema_long and prev_ema_short <= prev_ema_long
                and not mtf_blocked and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX):
            ok, req = _volume_ok(vol_ratio, 'transition', is_global_bull_market)
            if ok:
                signal = 'buy'
                buy_size_pct = 0.5
                reasons.append(f'Transition EMA 골든크로스 (비중 50%, Vol {vol_ratio:.1f}x >= {req}x)')
                reasons.append(f'RSI({rsi:.1f}) 필터 통과 [{RSI_BUY_MIN}~{RSI_BUY_MAX}]')
            else:
                reasons.append(f'Transition: Volume filter (Vol {vol_ratio:.1f}x < req {req}x)')

    return signal, sell_size_pct, buy_size_pct, next_scale_out_stage, reasons


def _apply_mean_reversion_logic(current_price, bb_lower, bb_upper, rsi, adx,
                                 vol_ratio, is_global_bull_market, mtf_blocked,
                                 ticker, timeframe, initial_buy_size_pct=1.0):
    """Buy/sell signal logic for 'range' (Bollinger Band mean-reversion) regime.

    Buy on BB lower-band touch; sell on BB upper-band touch event (new touch only).
    No scale-out or trailing stop — those are trend concepts.

    Returns:
        (signal, sell_size_pct, buy_size_pct, next_scale_out_stage, reasons)
    """
    from trading_bot.config import RSI_BUY_MIN, RSI_BUY_MAX
    from trading_bot.data_manager import load_ohlcv_from_db

    signal = 'hold'
    sell_size_pct = 1.0
    buy_size_pct = initial_buy_size_pct
    next_scale_out_stage = None
    reasons = []

    if not (current_price and current_price > 0 and bb_lower and bb_upper and bb_upper > bb_lower):
        return signal, sell_size_pct, buy_size_pct, next_scale_out_stage, reasons

    # BB lower touch → buy
    if current_price <= bb_lower * 1.01 and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX:
        if not mtf_blocked:
            ok, req = _volume_ok(vol_ratio, 'range', is_global_bull_market)
            if ok:
                signal = 'buy'
                reasons.append(f'횡보장(ADX={adx:.1f}) BB하단 터치(완성봉 밴드+실시간가격)')
                reasons.append(f'현재가({current_price:.0f}) <= 하단({bb_lower:.0f})')
                reasons.append(f'Smart Volume (Vol {vol_ratio:.1f}x >= {req}x)')
                reasons.append(f'RSI({rsi:.1f}) 필터 통과 [{RSI_BUY_MIN}~{RSI_BUY_MAX}]')
            else:
                reasons.append(f'Volume filter failed (Vol ratio: {vol_ratio:.1f}x < req {req}x)')
        # mtf_blocked → remain hold
    else:
        # BB upper touch event: sell only on first touch (prev candle did not touch)
        df_ohlcv = load_ohlcv_from_db(ticker, timeframe, count=5)
        if len(df_ohlcv) >= 3:
            closed_high = float(df_ohlcv.iloc[-2].get('high', 0) or 0)
            prev_high = float(df_ohlcv.iloc[-3].get('high', 0) or 0)
            if closed_high >= bb_upper * 0.99 and prev_high < bb_upper * 0.99:
                signal = 'sell'
                sell_size_pct = 1.0
                next_scale_out_stage = 0
                reasons.append(f'횡보장(ADX={adx:.1f}) 완성봉 고가 BB상단 터치 이벤트')
                reasons.append(f'고가({closed_high:.0f}) >= 상단({bb_upper:.0f}), 직전봉 미터치')

    return signal, sell_size_pct, buy_size_pct, next_scale_out_stage, reasons


def _adjust_position_size(signal, regime, account_value, use_dynamic_risk,
                           accumulation_mode, df_indicators, atr_val):
    """Compute final position size applying accumulation, regime, and volatility adjustments.

    Returns:
        (position_size, risk_adjustments, extra_reasons)
    """
    from trading_bot.risk import calculate_adjusted_position_size
    from trading_bot.config import VOL_SCALE_HIGH, VOL_SCALE_MID

    if use_dynamic_risk:
        position_size, risk_adjustments = calculate_adjusted_position_size(
            account_value=account_value,
            risk_per_trade_pct=0.02,
            stop_loss_pct=0.05,
            use_dynamic_adjustment=True,
        )
    else:
        position_size = account_value * 0.02 / 0.05
        risk_adjustments = {'position_size_multiplier': 1.0, 'is_defensive_mode': False}

    extra_reasons = []

    # Accumulation mode: pre-emptive 50% entry
    if accumulation_mode and position_size > 0:
        position_size = position_size * 0.5

    # Weakening trend or transition buy: reduce to 50%
    if (regime in ('weakening_trend', 'transition')) and signal == 'buy':
        position_size = position_size * 0.5
        risk_adjustments['weakening_trend_reduction'] = True
        risk_adjustments['position_size_multiplier'] = (
            risk_adjustments.get('position_size_multiplier', 1.0) * 0.5
        )

    # Volatility scaling based on ATR ratio vs 20-bar average
    vol_scale_multiplier = 1.0
    try:
        atr_series = df_indicators['atr'].dropna().tail(20)
        avg_atr_20 = float(atr_series.mean()) if len(atr_series) >= 5 else 0.0
    except Exception:
        avg_atr_20 = 0.0
    if avg_atr_20 > 0 and atr_val and atr_val > 0:
        atr_ratio = atr_val / avg_atr_20
        if atr_ratio >= VOL_SCALE_HIGH:
            vol_scale_multiplier = 0.5
            extra_reasons.append(f'변동성 급등(ATR {atr_ratio:.1f}x) → 포지션 50% 축소')
        elif atr_ratio >= VOL_SCALE_MID:
            vol_scale_multiplier = 0.75
            extra_reasons.append(f'변동성 상승(ATR {atr_ratio:.1f}x) → 포지션 25% 축소')
    if signal == 'buy' and vol_scale_multiplier < 1.0:
        position_size = position_size * vol_scale_multiplier
        risk_adjustments['vol_scale_multiplier'] = vol_scale_multiplier

    return position_size, risk_adjustments, extra_reasons


def _apply_btc_bear_filter(signal, is_global_bull_market, adx, vol_ratio,
                            position_size, risk_adjustments):
    """BTC bear-market filter with ADX>=40 decoupling exception.

    In a bear market, only allow buys when ADX >= 40 (strong decoupling)
    with volume >= 1.5x; reduce position to 50% in that case.

    Returns:
        (signal, position_size, risk_adjustments, extra_reasons)
    """
    extra_reasons = []
    if not is_global_bull_market and signal == 'buy':
        if adx >= 40.0:
            ok, req = _volume_ok(vol_ratio, 'trend', False, decoupling=True)
            if ok:
                position_size = position_size * 0.5
                risk_adjustments['btc_bear_decoupling_bypass'] = True
                risk_adjustments['position_size_multiplier'] = (
                    risk_adjustments.get('position_size_multiplier', 1.0) * 0.5
                )
                extra_reasons.append('BTC 하락장이지만 초강세(ADX>=40) 감지로 예외 매수 (비중 50% 축소)')
                extra_reasons.append(f'Smart Volume (Vol {vol_ratio:.1f}x >= {req}x)')
            else:
                signal = 'hold'
                extra_reasons.append(f'Volume filter failed (Vol ratio: {vol_ratio:.1f}x < req {req}x)')
        else:
            signal = 'hold'
            extra_reasons.append('BTC 하락장 필터에 의한 매수 보류')
    return signal, position_size, risk_adjustments, extra_reasons


def _persist_signal_and_analysis(ticker, timeframe, ts_now, bucket_seconds,
                                   signal, current_price, decision_reason, regime,
                                   adx, adx_slope, adx_prev, ema_short, ema_long, rsi,
                                   position_size, risk_adjustments, mtf_info, mtf_blocked,
                                   closed_candle, bb_lower, bb_upper, adx_trend_threshold):
    """Write Signal and AnalysisResult to DB; deduplicate within the same candle bucket.

    Returns:
        (signal, decision_reason) — signal may become 'hold' if dedup triggers.
    """
    from trading_bot.db import get_session
    from trading_bot.models import Signal as SignalModel, AnalysisResult

    session = get_session()
    try:
        # --- Duplicate-signal guard (same candle, same direction) ---
        skip_duplicate_write = False
        try:
            if signal in ('buy', 'sell'):
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
                        if ex.signal == signal:
                            signal = 'hold'
                            decision_reason = (decision_reason or '') + ' | (같은 봉 중복 신호 방지)'
                            skip_duplicate_write = True
                        break
        except Exception:
            pass

        if not skip_duplicate_write:
            # --- Write Signal record ---
            try:
                signal_value = 1 if signal == 'buy' else (-1 if signal == 'sell' else 0)
                signal_record = SignalModel(
                    ticker=ticker,
                    timeframe=timeframe,
                    ts=ts_now,
                    signal=signal_value,
                    algo_version='ema_regime_v1',
                    params=json.dumps({
                        'adx_trend_threshold': adx_trend_threshold,
                        'regime': regime,
                    }, default=_json_default),
                    meta=json.dumps({
                        'decision_reason': decision_reason,
                        'adx': adx,
                        'ema_short': float(ema_short),
                        'ema_long': float(ema_long),
                        'rsi': float(rsi),
                    }, default=_json_default),
                )
                session.add(signal_record)
                session.commit()
            except Exception as e:
                print(f'⚠️ Signal 저장 실패: {e}')
                session.rollback()

            # --- Write AnalysisResult record ---
            try:
                analysis_record = AnalysisResult(
                    ticker=ticker,
                    timestamp=ts_now,
                    signal=signal,
                    price=current_price,
                    change_rate=0.0,
                    position_size=position_size,
                    regime=regime,
                    is_defensive_mode=bool(risk_adjustments.get('is_defensive_mode', False)),
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
                    decision_reason=decision_reason,
                )
                session.add(analysis_record)
                session.commit()
            except Exception as e:
                print(f'⚠️ AnalysisResult 저장 실패: {e}')
                session.rollback()
    finally:
        session.close()

    return signal, decision_reason


def _fmt_num(v):
    """Format a number for AI log output. Tiny values (< 10) use 4 decimal places."""
    if v is None or (isinstance(v, float) and (v != v)):
        return "0"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "0"
    return f"{x:.4f}" if abs(x) < 10 else f"{x:.0f}"


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

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
    avg_buy_price: float = 0.0,
    macro_ema_long: int = 50,
    fng_value: int = 50,
) -> Dict:
    """
    종합 신호 판단 및 상세 로깅

    [M6 FIX] Refactored into a thin orchestrator that delegates each
    responsibility to a dedicated module-level function:
      _determine_regime        → ADX/BB regime classification
      _compute_trailing_stop   → Chandelier ATR exit price
      _apply_trend_logic       → trend / weakening_trend EMA signals
      _apply_transition_logic  → transition regime signals
      _apply_mean_reversion_logic → range (BB) signals
      _adjust_position_size    → accumulation / vol-scaling
      _apply_btc_bear_filter   → macro bear-market gate
      _persist_signal_and_analysis → DB writes + dedup

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
    - is_global_bull_market: BTC 거시 상승장 여부

    Returns:
    - Dict with keys: signal, regime, decision_reason, position_size,
      risk_adjustments, sell_size_pct, size_pct, next_scale_out_stage, indicators
    """
    try:
        from trading_bot.data_manager import load_ohlcv_from_db, load_higher_timeframe_indicators
        from trading_bot.config import RSI_SELL_MIN

        # 1) Load cached indicators -----------------------------------------
        df_indicators = load_cached_indicators(ticker, timeframe, count=200)
        if df_indicators.empty:
            return {
                'signal': 'hold', 'regime': 'unknown',
                'decision_reason': '캐싱된 지표 데이터 없음',
                'position_size': 0.0, 'risk_adjustments': {}, 'indicators': {},
            }

        # Intra-bar whipsaw prevention: use closed (not live) candle
        # iloc[-1] = in-progress candle  iloc[-2] = last fully closed candle
        if len(df_indicators) < 3:
            return {
                'signal': 'hold', 'regime': 'unknown',
                'decision_reason': '완성 봉 데이터 부족(최소 3봉 필요)',
                'position_size': 0.0, 'risk_adjustments': {}, 'indicators': {},
            }
        closed_candle = df_indicators.iloc[-2]
        prev_closed_candle = df_indicators.iloc[-3]

        if current_price is None:
            df_ohlcv = load_ohlcv_from_db(ticker, timeframe, count=1)
            current_price = float(df_ohlcv.iloc[-1]['close']) if not df_ohlcv.empty else 0.0

        # 2) Regime determination -------------------------------------------
        regime, adx, adx_prev, adx_slope, bb_width, regime_reason = _determine_regime(
            closed_candle, prev_closed_candle, adx_trend_threshold
        )
        decision_reason_parts = [regime_reason]

        # Shared indicator values
        ema_short = closed_candle.get('ema_short', 0)
        ema_long = closed_candle.get('ema_long', 0)
        rsi = closed_candle.get('rsi', 50)
        current_volume = float(closed_candle.get('volume', 0))
        vol_ma = max(1.0, float(closed_candle.get('volume_ma', 1)))
        vol_ratio = current_volume / vol_ma if vol_ma > 0 else 0.0
        bb_lower = closed_candle.get('bb_lower', 0)
        bb_upper = closed_candle.get('bb_upper', 0)
        bb_middle = closed_candle.get('bb_middle', 0)
        obv = float(closed_candle.get('obv', 0) or 0)
        obv_sma = float(closed_candle.get('obv_sma', 0) or 0)

        # 3) MTF (daily EMA) macro trend filter -----------------------------
        mtf_info = load_higher_timeframe_indicators(
            ticker, timeframe, count=max(50, macro_ema_long + 10),
            current_price=current_price, macro_ema_long=macro_ema_long,
        )
        mtf_blocked = False
        if mtf_info and mtf_info.get('is_uptrend') is False:
            mtf_blocked = True
            _ema_period = mtf_info.get('ema_period', macro_ema_long)
            decision_reason_parts.append(f"상위 타임프레임({mtf_info['timeframe']}) 하락장으로 매수 보류")
            decision_reason_parts.append(
                f"상위 현재가({mtf_info['current_price']:.0f}) < EMA{_ema_period}({mtf_info['ema_long']:.0f})"
            )

        # 4) ATR chandelier trailing stop -----------------------------------
        trailing_stop_price, atr_val, ts_reason = _compute_trailing_stop(
            ticker, timeframe, closed_candle, position_qty, avg_buy_price, current_roi
        )
        if ts_reason:
            decision_reason_parts.append(ts_reason)

        # 5) Whale accumulation detection (pre-empts regime logic) ----------
        signal = 'hold'
        sell_size_pct = 1.0
        buy_size_pct = 1.0
        next_scale_out_stage = None
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

        # 6) Regime-specific signal logic -----------------------------------
        avg_buy = float(avg_buy_price or 0.0)
        atr_for_scale = float(atr_val or 0.0) if atr_val is not None else 0.0

        if regime in ('trend', 'weakening_trend'):
            sig, sp, bp, nso, reasons = _apply_trend_logic(
                regime, closed_candle, prev_closed_candle, current_price,
                position_qty, avg_buy, atr_for_scale, scale_out_stage,
                trailing_stop_price, adx, rsi, vol_ratio,
                is_global_bull_market, mtf_blocked,
                initial_buy_size_pct=buy_size_pct,
            )
        elif regime == 'transition':
            sig, sp, bp, nso, reasons = _apply_transition_logic(
                closed_candle, prev_closed_candle, current_price,
                position_qty, avg_buy, atr_for_scale, scale_out_stage,
                trailing_stop_price, rsi, vol_ratio,
                is_global_bull_market, mtf_blocked,
                initial_buy_size_pct=buy_size_pct,
            )
        else:  # range
            sig, sp, bp, nso, reasons = _apply_mean_reversion_logic(
                current_price, bb_lower, bb_upper, rsi, adx,
                vol_ratio, is_global_bull_market, mtf_blocked,
                ticker, timeframe,
                initial_buy_size_pct=buy_size_pct,
            )

        signal = sig
        sell_size_pct = sp
        buy_size_pct = bp
        if nso is not None:
            next_scale_out_stage = nso
        decision_reason_parts.extend(reasons)

        # 6b) Multi-TF 4h Confluence: buy 신호 강도 조정 ---------------
        try:
            from trading_bot.config import MTF_4H_ENABLED
            if MTF_4H_ENABLED and signal == 'buy':
                from trading_bot.data_manager import load_4h_ema_state
                from trading_bot.param_manager import get_best_params as _gp4h
                _p4h = _gp4h()
                _4h_state = load_4h_ema_state(
                    ticker,
                    ema_short_period=_p4h.get('ema_short', 12),
                    ema_long_period=_p4h.get('ema_long', 26),
                )
                if _4h_state is not None:
                    _4h_golden, _4h_es, _4h_el = _4h_state
                    if _4h_golden:
                        # 1h + 4h 모두 골든크로스 → confluence 1.0 (변경 없음)
                        decision_reason_parts.append(
                            f'4h Confluence: 골든크로스 (EMA{_4h_es:.0f}>{_4h_el:.0f}) → 강한 매수'
                        )
                    else:
                        # 1h 골든크로스 + 4h 데드크로스 → 약한 매수, 포지션 50%
                        buy_size_pct = min(buy_size_pct, 0.5)
                        decision_reason_parts.append(
                            f'4h Confluence: 데드크로스 (EMA{_4h_es:.0f}<{_4h_el:.0f}) → 비중 50%'
                        )
        except Exception:
            pass

        # 6c) Panic Dip-Buy: MTF 하락장 + Extreme Fear + 평균회귀 시그널 → MTF 바이패스
        if signal == 'hold' and mtf_blocked and position_qty <= 0:
            try:
                from trading_bot.config import FNG_EXTREME_FEAR
                if fng_value <= FNG_EXTREME_FEAR:
                    # Mean-reversion trigger: RSI <= 30 또는 BB 하단 터치
                    _rsi_panic = float(rsi) if not pd.isna(rsi) else 50
                    _bb_low = float(bb_lower or 0)
                    _price = float(current_price or 0)
                    panic_trigger = False
                    panic_reasons = []

                    if _rsi_panic <= 30:
                        panic_trigger = True
                        panic_reasons.append(f'RSI({_rsi_panic:.1f}) <= 30')
                    if _bb_low > 0 and _price > 0 and _price <= _bb_low * 1.01:
                        panic_trigger = True
                        panic_reasons.append(f'BB하단 터치 (가격 {_price:.0f} <= BB하단 {_bb_low:.0f})')

                    if panic_trigger:
                        signal = 'buy'
                        buy_size_pct = 1.0  # auto_trader에서 PANIC_DIP_BUY_SIZE_PCT로 오버라이드됨
                        trigger_detail = ' + '.join(panic_reasons)
                        decision_reason_parts.append(
                            f'Panic Dip-Buy (MTF Bypassed due to Extreme Fear, FNG={fng_value}): {trigger_detail}'
                        )
            except Exception:
                pass

        # RSI overbought sell reinforcement
        if signal == 'sell' and position_qty > 0 and rsi >= RSI_SELL_MIN:
            decision_reason_parts.append(f'RSI({rsi:.1f}) 과매수 구간 매도 강화')

        decision_reason_parts.append(f'[Vol: {vol_ratio:.1f}x]')
        decision_reason = ' | '.join(decision_reason_parts) if decision_reason_parts else '신호 없음'

        # 7) Position sizing + volatility scaling ---------------------------
        position_size, risk_adjustments, size_reasons = _adjust_position_size(
            signal, regime, account_value, use_dynamic_risk,
            accumulation_mode, df_indicators, atr_val,
        )
        if size_reasons:
            decision_reason_parts.extend(size_reasons)
            decision_reason = ' | '.join(decision_reason_parts)

        # 8) BTC bear-market filter -----------------------------------------
        signal, position_size, risk_adjustments, bear_reasons = _apply_btc_bear_filter(
            signal, is_global_bull_market, adx, vol_ratio, position_size, risk_adjustments,
        )
        if bear_reasons:
            decision_reason_parts.extend(bear_reasons)
            decision_reason = ' | '.join(decision_reason_parts)

        # 9) Dedup check + DB persistence -----------------------------------
        ts_now = closed_candle['time']
        if hasattr(ts_now, 'to_pydatetime'):
            ts_now = ts_now.to_pydatetime()
        try:
            from datetime import timezone as _tz
            if hasattr(ts_now, 'tzinfo') and ts_now.tzinfo is not None:
                ts_now = ts_now.astimezone(_tz.utc).replace(tzinfo=None)
        except Exception:
            pass
        bucket_seconds = _candle_bucket_seconds(timeframe)

        signal, decision_reason = _persist_signal_and_analysis(
            ticker, timeframe, ts_now, bucket_seconds,
            signal, current_price, decision_reason, regime,
            adx, adx_slope, adx_prev, ema_short, ema_long, rsi,
            position_size, risk_adjustments, mtf_info, mtf_blocked,
            closed_candle, bb_lower, bb_upper, adx_trend_threshold,
        )

        # 10) AI event log (buy/sell only) ----------------------------------
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
