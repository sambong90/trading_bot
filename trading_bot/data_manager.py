# OHLCV/지표 로드 헬퍼 (strategy에서 사용)
# data.py 및 DB 기반 래퍼
import pandas as pd
import numpy as np
from datetime import timezone as _tz
from ta.volume import OnBalanceVolumeIndicator
from trading_bot.data import fetch_ohlcv_from_db


def load_ohlcv_from_db(ticker, timeframe, count=200):
    """DB에서 OHLCV 로드. 없으면 빈 DataFrame 반환."""
    df = fetch_ohlcv_from_db(ticker=ticker, interval=timeframe, count=count)
    return df if df is not None and len(df) > 0 else pd.DataFrame()


def load_higher_timeframe_indicators(ticker, timeframe, count=50, current_price=None, macro_ema_long=None):
    """
    상위 타임프레임(일봉) 지표: 일봉 EMA 대비 현재가로 상승/하락 추세 판단.
    macro_ema_long: 일봉 EMA 기간. None이면 config.MACRO_EMA_LONG(기본 50) 사용.
    current_price가 EMA 위면 is_uptrend True, 아래면 False.
    current_price가 None이거나 데이터 부족 시 None 반환(필터 스킵).
    """
    if current_price is None or current_price <= 0:
        return None
    if macro_ema_long is None:
        try:
            from trading_bot.config import MACRO_EMA_LONG
            macro_ema_long = MACRO_EMA_LONG
        except Exception:
            macro_ema_long = 50
    macro_ema_long = int(macro_ema_long)
    min_bars = macro_ema_long + 10  # EMA 워밍업 여유
    try:
        from trading_bot.data import fetch_ohlcv_from_db, fetch_ohlcv
        df_day = fetch_ohlcv_from_db(ticker=ticker, interval='day', count=max(count, min_bars))
        if df_day is None or len(df_day) < macro_ema_long:
            from trading_bot.data import fetch_ohlcv as _fetch_ohlcv
            df_day = _fetch_ohlcv(ticker=ticker, interval='day', count=min_bars, use_db_first=False)
        if df_day is None or len(df_day) < macro_ema_long:
            return None
        if 'time' not in df_day.columns and df_day.index.name is not None:
            df_day = df_day.reset_index()
        close = df_day['close']
        ema_macro = _ema(close, macro_ema_long)
        last_ema = float(ema_macro.iloc[-1]) if len(ema_macro) and pd.notna(ema_macro.iloc[-1]) else None
        if last_ema is None or last_ema <= 0:
            return None
        is_uptrend = current_price >= last_ema
        return {
            'is_uptrend': is_uptrend,
            'timeframe': 'day',
            'current_price': float(current_price),
            'ema_long': last_ema,
            'ema_period': macro_ema_long,  # 실제 사용된 기간 (로그 표시용)
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
    """
    [M3 FIX] Standard Wilder's ADX.

    Primary path  : TA-Lib talib.ADX() — industry-standard, matches TradingView.
    Fallback path : Wilder's EMA (alpha=1/period) applied to +DM, -DM, TR.
                    The previous rolling-sum approach underestimated ADX and did not
                    match external references; this corrects that.
    """
    # --- TA-Lib path (preferred) ---
    try:
        import talib
        h = high.to_numpy(dtype=float)
        l = low.to_numpy(dtype=float)
        c = close.to_numpy(dtype=float)
        adx_vals = talib.ADX(h, l, c, timeperiod=period)
        return pd.Series(adx_vals, index=high.index).fillna(0)
    except ImportError:
        pass
    except Exception:
        pass

    # --- Wilder's EMA fallback (correct smoothing) ---
    alpha = 1.0 / period
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    # Raw directional movement
    up_move = high - prev_high
    dn_move = prev_low - low
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Wilder's smoothing (EWM with alpha=1/period, adjust=False)
    tr_s = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=alpha, adjust=False).mean() / tr_s.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=alpha, adjust=False).mean() / tr_s.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
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


def _float_or_none(val):
    if val is None:
        return None
    try:
        v = float(val)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _normalize_ts(ts):
    """Datetime을 timezone-naive UTC로 정규화 (DB/pandas 비교용)."""
    if ts is None:
        return None
    if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
        try:
            return ts.astimezone(_tz.utc).replace(tzinfo=None)
        except Exception:
            pass
    return ts


def sync_indicators_for_ticker(ticker, timeframe, df_ohlcv=None):
    """
    OHLCV로 지표 계산 후 technical_indicators에 저장.

    [H2 FIX] N+1 개별 SELECT → PostgreSQL UPSERT (1 쿼리) 또는
    두 단계 bulk INSERT/UPDATE (2 쿼리) 로 교체.
    기존: ticker당 200회 SELECT → 60 ticker × 200 = 12,000 쿼리/사이클.
    개선: ticker당 1~2 쿼리 → 60 ticker × 2 = 120 쿼리/사이클.
    """
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

    # --- 저장 행 목록 구성 (pandas 순회 — DB 호출 없음) ---
    to_save = df.tail(200)
    rows = []
    for idx in range(len(to_save)):
        row = to_save.iloc[idx]
        ts = row.get('time') or row.name
        if hasattr(ts, 'to_pydatetime'):
            ts = ts.to_pydatetime()
        ts = _normalize_ts(ts)  # timezone-naive UTC 정규화

        adx = _float_or_none(row.get('adx')) or 0.0
        atr_raw = _float_or_none(row.get('atr_raw')) or _float_or_none(row.get('atr'))
        indicators = {
            'adx': adx,
            'bb_lower': _float_or_none(row.get('bb_lower')),
            'bb_middle': _float_or_none(row.get('bb_middle')),
            'bb_upper': _float_or_none(row.get('bb_upper')),
            'atr_raw': atr_raw,
            'obv': _float_or_none(row.get('obv')),
            'obv_sma': _float_or_none(row.get('obv_sma')),
            'bb_width': _float_or_none(row.get('bb_width')),
        }
        rows.append({
            'ticker': ticker,
            'timeframe': timeframe,
            'ts': ts,
            'sma_short': _float_or_none(row.get('sma_short')),
            'sma_long': _float_or_none(row.get('sma_long')),
            'ema_short': _float_or_none(row.get('ema_short')) or 0.0,
            'ema_long': _float_or_none(row.get('ema_long')) or 0.0,
            'rsi': _float_or_none(row.get('rsi')) or 50.0,
            'atr': _float_or_none(row.get('atr')) or 0.0,
            'volume_ma': _float_or_none(row.get('volume_ma')) or 0.0,
            'indicators': indicators,
        })

    if not rows:
        return False

    session = get_session()
    try:
        # --- 1차 시도: PostgreSQL UPSERT (1 쿼리, K8s 프로덕션 경로) ---
        try:
            from sqlalchemy.dialects.postgresql import insert as _pg_insert
            _UPDATE_COLS = ['sma_short', 'sma_long', 'ema_short', 'ema_long',
                            'rsi', 'atr', 'volume_ma', 'indicators']
            stmt = _pg_insert(TechnicalIndicator).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint='u_tech_ticker_timeframe_ts',
                set_={col: stmt.excluded[col] for col in _UPDATE_COLS},
            )
            session.execute(stmt)
            session.commit()
            return True
        except ImportError:
            pass  # SQLite 환경 → 2단계 fallback
        except Exception:
            session.rollback()
            # PostgreSQL인데 실패한 경우 fallback 시도
            pass

        # --- 2차 시도: 2쿼리 bulk INSERT + bulk UPDATE (SQLite / fallback) ---
        # 기존 타임스탬프를 한 번에 조회 (1 SELECT)
        existing = {
            _normalize_ts(r.ts): r.id
            for r in session.query(TechnicalIndicator.ts, TechnicalIndicator.id).filter(
                TechnicalIndicator.ticker == ticker,
                TechnicalIndicator.timeframe == timeframe,
            ).all()
        }

        to_insert = []
        to_update = []
        for r in rows:
            norm_ts = _normalize_ts(r['ts'])
            if norm_ts in existing:
                to_update.append({'id': existing[norm_ts], **r})
            else:
                to_insert.append(r)

        if to_insert:
            session.bulk_insert_mappings(TechnicalIndicator, to_insert)
        if to_update:
            session.bulk_update_mappings(TechnicalIndicator, to_update)

        session.commit()
        return True

    except Exception:
        session.rollback()
        return False
    finally:
        session.close()
