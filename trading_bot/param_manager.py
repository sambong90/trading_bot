
"""
Dynamic parameter source for strategy and indicators (V4.0).
Reads the latest TuningRun by created_at desc; falls back to defaults if none.
"""
from trading_bot.db import get_session
from trading_bot.models import TuningRun

_DEFAULT_PARAMS = {
    'ema_short': 12,
    'ema_long': 26,
    'rsi_period': 14,
    'atr_period': 14,
    'adx_trend_threshold': 25.0,
}


def get_best_params():
    """
    Return the latest parameter combo from TuningRun (order by created_at desc).
    If no record exists, return default dict.
    """
    session = get_session()
    try:
        record = session.query(TuningRun).order_by(TuningRun.created_at.desc()).limit(1).first()
        if not record or not isinstance(record.combo, dict):
            return _DEFAULT_PARAMS.copy()
        out = _DEFAULT_PARAMS.copy()
        out.update(record.combo)
        return out
    except Exception:
        return _DEFAULT_PARAMS.copy()
    finally:
        session.close()

