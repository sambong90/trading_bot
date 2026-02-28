"""
Balanced+ position management: overtrading guards, DCA, partial stop-loss.
State is tracked via analysis_results.decision_reason tags (no schema change).
All helpers defensive: on DB error return safe default (no cooldown), never crash.
"""
import os
from datetime import datetime, timedelta
from typing import Optional

# ----- Env-overridable constants (no code edit needed to tune) -----
def _int(key: str, default: str) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)

def _float(key: str, default: str) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)

def _bool(key: str, default: str) -> bool:
    v = os.environ.get(key, default).lower()
    return v in ('1', 'true', 'yes')

MAX_BUYS_PER_CYCLE = _int('MAX_BUYS_PER_CYCLE', '2')
MAX_OPEN_POSITIONS = _int('MAX_OPEN_POSITIONS', '6')
BUY_COOLDOWN_MINUTES = _int('BUY_COOLDOWN_MINUTES', '60')
SELL_COOLDOWN_MINUTES = _int('SELL_COOLDOWN_MINUTES', '15')

DCA_ENABLED = _bool('DCA_ENABLED', 'true')
DCA_MAX_PER_TICKER_PER_DAY = _int('DCA_MAX_PER_TICKER_PER_DAY', '1')
DCA_COOLDOWN_MINUTES = _int('DCA_COOLDOWN_MINUTES', '120')
DCA_TRIGGER_ROI_PCT = _float('DCA_TRIGGER_ROI_PCT', '-3.0')
DCA_SIZE_MULTIPLIER = _float('DCA_SIZE_MULTIPLIER', '0.30')
DCA_MIN_VOL_RATIO = _float('DCA_MIN_VOL_RATIO', '1.0')
DCA_ALLOWED_REGIMES = {'trend'}

PARTIAL_STOP_1_ROI_PCT = _float('PARTIAL_STOP_1_ROI_PCT', '-5.0')
PARTIAL_STOP_1_SELL_PCT = _float('PARTIAL_STOP_1_SELL_PCT', '0.25')
PARTIAL_STOP_2_ROI_PCT = _float('PARTIAL_STOP_2_ROI_PCT', '-8.0')
PARTIAL_STOP_2_SELL_PCT = _float('PARTIAL_STOP_2_SELL_PCT', '0.25')
PARTIAL_STOP_COOLDOWN_MINUTES = _int('PARTIAL_STOP_COOLDOWN_MINUTES', '120')

TREND_BUY_MIN_VOL_RATIO = _float('TREND_BUY_MIN_VOL_RATIO', '1.0')
RANGE_BUY_MIN_VOL_RATIO = _float('RANGE_BUY_MIN_VOL_RATIO', '0.8')

TAG_DCA_BUY = 'DCA_BUY'
TAG_PS1 = 'PS1'
TAG_PS2 = 'PS2'
TAG_EXEC_BUY = 'EXEC_BUY'
TAG_EXEC_SELL = 'EXEC_SELL'


def last_buy_ts(ticker: str):
    """Latest AnalysisResult where ticker==ticker and signal=='buy'. Returns None on error."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AnalysisResult
        session = get_session()
        try:
            row = session.query(AnalysisResult).filter(
                AnalysisResult.ticker == ticker,
                AnalysisResult.signal == 'buy'
            ).order_by(AnalysisResult.timestamp.desc()).limit(1).first()
            return row.timestamp if row and row.timestamp else None
        finally:
            session.close()
    except Exception:
        return None


def last_sell_ts(ticker: str):
    """Latest AnalysisResult where ticker==ticker and signal=='sell'. Returns None on error."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AnalysisResult
        session = get_session()
        try:
            row = session.query(AnalysisResult).filter(
                AnalysisResult.ticker == ticker,
                AnalysisResult.signal == 'sell'
            ).order_by(AnalysisResult.timestamp.desc()).limit(1).first()
            return row.timestamp if row and row.timestamp else None
        finally:
            session.close()
    except Exception:
        return None


def count_tag_last_24h(ticker: str, tag: str) -> int:
    """Number of AnalysisResult rows in last 24h where ticker==ticker and decision_reason contains tag."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AnalysisResult
        since = datetime.utcnow() - timedelta(hours=24)
        session = get_session()
        try:
            rows = session.query(AnalysisResult).filter(
                AnalysisResult.ticker == ticker,
                AnalysisResult.timestamp >= since
            ).all()
            return sum(1 for r in rows if r.decision_reason and tag in r.decision_reason)
        finally:
            session.close()
    except Exception:
        return 0


def _last_ts_with_tag(ticker: str, tag: str):
    """Latest AnalysisResult.timestamp where decision_reason contains tag. None on error."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AnalysisResult
        session = get_session()
        try:
            rows = session.query(AnalysisResult).filter(
                AnalysisResult.ticker == ticker
            ).order_by(AnalysisResult.timestamp.desc()).limit(200).all()
            for r in rows:
                if r.decision_reason and tag in r.decision_reason and r.timestamp:
                    return r.timestamp
            return None
        finally:
            session.close()
    except Exception:
        return None


def is_in_buy_cooldown(ticker: str) -> bool:
    """True if last_buy_ts within BUY_COOLDOWN_MINUTES. On error returns False (no cooldown)."""
    ts = last_buy_ts(ticker)
    if ts is None:
        return False
    try:
        if hasattr(ts, 'timestamp'):
            t = ts.timestamp()
        else:
            t = (ts - datetime(1970, 1, 1)).total_seconds()
        return (datetime.utcnow().timestamp() - t) < (BUY_COOLDOWN_MINUTES * 60)
    except Exception:
        return False


def is_in_dca_cooldown(ticker: str) -> bool:
    """True if last DCA_BUY event within DCA_COOLDOWN_MINUTES."""
    ts = _last_ts_with_tag(ticker, TAG_DCA_BUY)
    if ts is None:
        return False
    try:
        if hasattr(ts, 'timestamp'):
            t = ts.timestamp()
        else:
            t = (ts - datetime(1970, 1, 1)).total_seconds()
        return (datetime.utcnow().timestamp() - t) < (DCA_COOLDOWN_MINUTES * 60)
    except Exception:
        return False


def is_in_partial_stop_cooldown(ticker: str) -> bool:
    """True if last PS1/PS2 event within PARTIAL_STOP_COOLDOWN_MINUTES."""
    ts1 = _last_ts_with_tag(ticker, TAG_PS1)
    ts2 = _last_ts_with_tag(ticker, TAG_PS2)
    ts = ts1 or ts2
    if ts is None:
        return False
    try:
        if hasattr(ts, 'timestamp'):
            t = ts.timestamp()
        else:
            t = (ts - datetime(1970, 1, 1)).total_seconds()
        return (datetime.utcnow().timestamp() - t) < (PARTIAL_STOP_COOLDOWN_MINUTES * 60)
    except Exception:
        return False


def count_open_positions(executor, tickers: list) -> int:
    """Count tickers with position qty > 0. Paper: executor.positions; Live: _balance_cache non-KRW with balance > 0."""
    try:
        if hasattr(executor, 'positions'):
            return sum(1 for t, p in getattr(executor, 'positions', {}).items() if (p.get('qty') or 0) > 0)
        if hasattr(executor, '_balance_cache'):
            cache = getattr(executor, '_balance_cache', {}) or {}
            return sum(1 for cur, bal in cache.items() if cur != 'KRW' and float(bal or 0) > 0)
        return sum(1 for t in tickers if (executor.get_position_qty(t) or 0) > 0)
    except Exception:
        return 0


def log_execution_event(ticker: str, signal: str, tag: str, price: float = None):
    """Best-effort insert AnalysisResult row for executed action (EXEC_BUY/EXEC_SELL/DCA_BUY/PS1/PS2). Never crash."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AnalysisResult
        session = get_session()
        try:
            rec = AnalysisResult(
                ticker=ticker,
                timestamp=datetime.utcnow(),
                signal=signal,
                price=price,
                decision_reason=tag,
                regime=None,
                position_size=None,
            )
            session.add(rec)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()
    except Exception:
        pass
