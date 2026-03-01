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
    'macro_ema_long': 50,  # 일봉 거시 트렌드 EMA 기간; auto_tuner가 최적값으로 덮어씀
}


def get_best_params():
    """
    Return the latest parameter combo from TuningRun (order by created_at desc).
    If no record exists, return default dict.
    MACRO_EMA_LONG env var overrides the hardcoded default (tuned value takes final precedence).
    """
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
            return defaults
        out = defaults.copy()
        out.update(record.combo)
        return out
    except Exception:
        return defaults.copy()
    finally:
        session.close()

