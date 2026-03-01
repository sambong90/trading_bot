"""
Dynamic parameter source for strategy and indicators (V4.0).
Reads the latest TuningRun by created_at desc; falls back to defaults if none.

Cache: get_best_params() is called ~60 times per trading cycle (once per ticker).
A 60-second module-level TTL cache reduces DB round-trips from 60 → 1 per cycle.
"""
import time as _time
from trading_bot.db import get_session
from trading_bot.models import TuningRun

_DEFAULT_PARAMS = {
    'ema_short': 12,
    'ema_long': 26,
    'rsi_period': 14,
    'atr_period': 14,
    'adx_trend_threshold': 25.0,
    'macro_ema_long': 50,  # 일봉 거시 트렌드 EMA 기간; auto_tuner가 최적값으로 덮어씀
}

# Module-level TTL cache — shared across all callers in the same process.
_PARAM_TTL_SECONDS = 60
_param_cache: dict = {'params': None, 'expires_at': 0.0}


def get_best_params() -> dict:
    """
    Return the latest parameter combo from TuningRun (order by created_at desc).
    If no record exists, return default dict.
    MACRO_EMA_LONG env var overrides the hardcoded default (tuned value takes final precedence).

    Results are cached for _PARAM_TTL_SECONDS (default 60s) to avoid 60 identical
    DB queries per trading cycle.
    """
    now = _time.monotonic()
    if _param_cache['params'] is not None and now < _param_cache['expires_at']:
        return _param_cache['params'].copy()

    try:
        from trading_bot.config import MACRO_EMA_LONG
        defaults = _DEFAULT_PARAMS.copy()
        defaults['macro_ema_long'] = MACRO_EMA_LONG
    except Exception:
        defaults = _DEFAULT_PARAMS.copy()

    session = get_session()
    try:
        record = session.query(TuningRun).order_by(TuningRun.created_at.desc()).limit(1).first()
        if not record or not isinstance(record.combo, dict):
            result = defaults
        else:
            result = defaults.copy()
            result.update(record.combo)
    except Exception:
        result = defaults.copy()
    finally:
        session.close()

    _param_cache['params'] = result
    _param_cache['expires_at'] = now + _PARAM_TTL_SECONDS
    return result.copy()


def invalidate_cache() -> None:
    """Force cache expiry on the next get_best_params() call (e.g. after a tuning run)."""
    _param_cache['params'] = None
    _param_cache['expires_at'] = 0.0
