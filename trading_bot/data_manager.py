# OHLCV/지표 로드 헬퍼 (strategy에서 사용)
# data.py 및 DB 기반 래퍼
import pandas as pd
import numpy as np
from ta.volume import OnBalanceVolumeIndicator
from trading_bot.data import fetch_ohlcv_from_db


def load_ohlcv_from_db(ticker, timeframe, count=200):
    """DB에서 OHLCV 로드. 없으면 빈 DataFrame 반환."""
    df = fetch_ohlcv_from_db(ticker=ticker, interval=timeframe, count=count)
    return df if df is not None and len(df) > 0 else pd.DataFrame()


def load_higher_timeframe_indicators(ticker, timeframe, count=50, current_price=None):
    """
    상위 타임프레임(일봉) 지표: EMA 50 대비 현재가로 상승/하락 추세 판단.
    current_price가 일봉 EMA 50 위면 is_uptrend True, 아래면 False.
    current_price가 None이거나 데이터 부족 시 None 반환(필터 스킵).
    """
    if current_price is None or current_price <= 0:
        return None
    try:
        from trading_bot.data import fetch_ohlcv_from_db, fetch_ohlcv
        df_day = fetch_ohlcv_from_db(ticker=ticker, interval='day', count=max(count, 60))
        if df_day is None or len(df_day) < 50:
            from trading_bot.data import fetch_ohlcv as _fetch_ohlcv
            df_day = _fetch_ohlcv(ticker=ticker, interval='day', count=60, use_db_first=False)
        if df_day is None or len(df_day) < 50:
            return None
        if 'time' not in df_day.columns and df_day.index.name is not None:
            df_day = df_day.reset_index()
        close = df_day['close']
        ema50 = _ema(close, 50)
        last_ema = float(ema50.iloc[-1]) if len(ema50) and pd.notna(ema50.iloc[-1]) else None
        if last_ema is None or last_ema <= 0:
            return None
        is_uptrend = current_price >= last_ema
        return {
            'is_uptrend': is_uptrend,
            'timeframe': 'day',
            'current_price': float(current_price),
            'ema_long': last_ema,
        }
    except Exception:
        return None


# ----- 지표 계산 (pandas만 사용, strategy와 동일 파라미터) -----
def _ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def _atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _adx(high, low, close, period=14):
    prev_close = close.shift(1)
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr = _atr(high, low, close, period=1)
    tr = tr.rolling(period).sum()
    plus_di = 100 * (plus_dm.rolling(period).sum() / tr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(period).sum() / tr.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(period).mean()
    return adx.fillna(0)


def compute_indicators(df, ema_short=None, ema_long=None, rsi_period=None, atr_period=None, bb_period=20, bb_std=2):
    """OHLCV DataFrame에 EMA, RSI, ATR, ADX, BB 추가. time 컬럼 필수. None인 인자는 get_best_params()로 채움."""
    if ema_short is None or ema_long is None or rsi_period is None or atr_period is None:
        from trading_bot.param_manager import get_best_params
        _p = get_best_params()
    else:
        _p = {}
    ema_short = ema_short if ema_short is not None else _p.get('ema_short', 12)
    ema_long = ema_long if ema_long is not None else _p.get('ema_long', 26)
    rsi_period = rsi_period if rsi_period is not None else _p.get('rsi_period', 14)
    atr_period = atr_period if atr_period is not None else _p.get('atr_period', 14)
    if df is None or len(df) < max(ema_long, rsi_period, bb_period) + 5:
        return None
    df = df.copy()
    if 'time' not in df.columns and df.index.name is not None:
        df = df.reset_index()
    close = df['close']
    high = df['high']
    low = df['low']
    df['ema_short'] = _ema(close, ema_short)
    df['ema_long'] = _ema(close, ema_long)
    df['rsi'] = _rsi(close, rsi_period)
    atr = _atr(high, low, close, atr_period)
    df['atr'] = atr
    df['atr_raw'] = atr
    df['adx'] = _adx(high, low, close, atr_period)

    sma20 = close.rolling(bb_period).mean()
    std20 = close.rolling(bb_period).std().fillna(0)
    df['bb_middle'] = sma20
    df['bb_upper'] = sma20 + bb_std * std20
    df['bb_lower'] = sma20 - bb_std * std20

    # Bollinger Band Width: 밴드 폭을 중앙선 대비 비율로 표현
    with np.errstate(divide='ignore', invalid='ignore'):
        bb_width = (df['bb_upper'] - df['bb_lower']) / df['bb_middle'].replace(0, np.nan)
    df['bb_width'] = bb_width.replace([np.inf, -np.inf], np.nan).fillna(0)

    df['volume_ma'] = df['volume'].rolling(20).mean()

    # OBV 및 OBV 이동평균 (20)
    try:
        indicator_obv = OnBalanceVolumeIndicator(close=df['close'], volume=df['volume'])
        df['obv'] = indicator_obv.on_balance_volume()
        df['obv_sma'] = df['obv'].rolling(window=20).mean()
    except Exception:
        df['obv'] = 0.0
        df['obv_sma'] = 0.0

    df['sma_short'] = close.rolling(ema_short).mean()
    df['sma_long'] = close.rolling(ema_long).mean()
    return df


def sync_indicators_for_ticker(ticker, timeframe, df_ohlcv=None):
    """OHLCV로 지표 계산 후 technical_indicators에 저장. df_ohlcv 없으면 DB에서 로드."""
    from trading_bot.db import get_session
    from trading_bot.models import TechnicalIndicator
    from trading_bot.param_manager import get_best_params

    if df_ohlcv is None or len(df_ohlcv) == 0:
        df_ohlcv = load_ohlcv_from_db(ticker, timeframe, count=300)
    if df_ohlcv is None or len(df_ohlcv) < 30:
        return False

    params = get_best_params()
    df = compute_indicators(
        df_ohlcv,
        ema_short=params.get('ema_short', 12),
        ema_long=params.get('ema_long', 26),
        rsi_period=params.get('rsi_period', 14),
        atr_period=params.get('atr_period', 14),
    )
    if df is None:
        return False

    def _float_or_none(val):
        if val is None:
            return None
        try:
            v = float(val)
            return v if np.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    session = get_session()
    try:
        to_save = df.tail(200)
        for idx in range(len(to_save)):
            row = to_save.iloc[idx]
            ts = row.get('time') or row.name
            if hasattr(ts, 'to_pydatetime'):
                ts = ts.to_pydatetime()
            try:
                if hasattr(ts, 'tzinfo') and ts.tzinfo:
                    ts = pd.Timestamp(ts).tz_localize(None).to_pydatetime()
            except Exception:
                pass
            # _float_or_none: NaN/Inf → None. 스칼라 컬럼은 or로 fallback, JSON 컬럼은 None 허용.
            ema_s = _float_or_none(row.get('ema_short')) or 0.0
            ema_l = _float_or_none(row.get('ema_long')) or 0.0
            rsi = _float_or_none(row.get('rsi')) or 50.0
            atr = _float_or_none(row.get('atr')) or 0.0
            vol_ma = _float_or_none(row.get('volume_ma')) or 0.0
            # indicators JSON 컬럼: NaN → None(null) → PostgreSQL JSON 허용
            adx = _float_or_none(row.get('adx')) or 0.0
            atr_raw = _float_or_none(row.get('atr_raw')) or _float_or_none(row.get('atr'))
            bb_l = _float_or_none(row.get('bb_lower'))
            bb_m = _float_or_none(row.get('bb_middle'))
            bb_u = _float_or_none(row.get('bb_upper'))
            obv = _float_or_none(row.get('obv'))
            obv_sma = _float_or_none(row.get('obv_sma'))
            bb_width = _float_or_none(row.get('bb_width'))
            indicators = {
                'adx': adx,
                'bb_lower': bb_l,
                'bb_middle': bb_m,
                'bb_upper': bb_u,
                'atr_raw': atr_raw,
                'obv': obv,
                'obv_sma': obv_sma,
                'bb_width': bb_width,
            }
            existing = session.query(TechnicalIndicator).filter(
                TechnicalIndicator.ticker == ticker,
                TechnicalIndicator.timeframe == timeframe,
                TechnicalIndicator.ts == ts,
            ).first()
            if existing:
                existing.sma_short = _float_or_none(row.get('sma_short'))
                existing.sma_long = _float_or_none(row.get('sma_long'))
                existing.ema_short = ema_s
                existing.ema_long = ema_l
                existing.rsi = rsi
                existing.atr = atr
                existing.volume_ma = vol_ma
                existing.indicators = indicators
            else:
                rec = TechnicalIndicator(
                    ticker=ticker,
                    timeframe=timeframe,
                    ts=ts,
                    sma_short=_float_or_none(row.get('sma_short')),
                    sma_long=_float_or_none(row.get('sma_long')),
                    ema_short=ema_s,
                    ema_long=ema_l,
                    rsi=rsi,
                    atr=atr,
                    volume_ma=vol_ma,
                    indicators=indicators,
                )
                session.add(rec)
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()

