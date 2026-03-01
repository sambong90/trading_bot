"""
Balanced+ position management: overtrading guards, DCA, partial stop-loss.
State is tracked via analysis_results.decision_reason tags (no schema change).
All helpers defensive: on DB error return safe default (no cooldown), never crash.
"""
import os
from datetime import datetime, timedelta, timezone
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
DCA_TRIGGER_ROI_PCT = _float('DCA_TRIGGER_ROI_PCT', '-5.0')
DCA_SIZE_MULTIPLIER = _float('DCA_SIZE_MULTIPLIER', '0.30')
DCA_MIN_VOL_RATIO = _float('DCA_MIN_VOL_RATIO', '1.0')
DCA_ALLOWED_REGIMES = {'trend'}

PARTIAL_STOP_1_ROI_PCT = _float('PARTIAL_STOP_1_ROI_PCT', '-8.0')
PARTIAL_STOP_1_SELL_PCT = _float('PARTIAL_STOP_1_SELL_PCT', '0.25')
PARTIAL_STOP_2_ROI_PCT = _float('PARTIAL_STOP_2_ROI_PCT', '-12.0')
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
    """Number of ExecutionEvent rows in last 24h where ticker==ticker and tag==tag.
    [L3 FIX] Uses indexed ExecutionEvent table instead of scanning AnalysisResult.decision_reason text."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import ExecutionEvent
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        session = get_session()
        try:
            return session.query(ExecutionEvent).filter(
                ExecutionEvent.ticker == ticker,
                ExecutionEvent.tag == tag,
                ExecutionEvent.ts >= since,
            ).count()
        finally:
            session.close()
    except Exception:
        return 0


def _last_ts_with_tag(ticker: str, tag: str):
    """Latest ExecutionEvent.ts where ticker==ticker and tag==tag. None on error.
    [L3 FIX] Direct indexed query on ExecutionEvent instead of scanning 200 AnalysisResult rows."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import ExecutionEvent
        session = get_session()
        try:
            row = session.query(ExecutionEvent).filter(
                ExecutionEvent.ticker == ticker,
                ExecutionEvent.tag == tag,
            ).order_by(ExecutionEvent.ts.desc()).limit(1).first()
            return row.ts if row and row.ts else None
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
        return (datetime.now(timezone.utc).timestamp() - t) < (BUY_COOLDOWN_MINUTES * 60)
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
        return (datetime.now(timezone.utc).timestamp() - t) < (DCA_COOLDOWN_MINUTES * 60)
    except Exception:
        return False


def is_in_partial_stop_cooldown(ticker: str) -> bool:
    """True if last PS1/PS2 event within PARTIAL_STOP_COOLDOWN_MINUTES."""
    ts1 = _last_ts_with_tag(ticker, TAG_PS1)
    ts2 = _last_ts_with_tag(ticker, TAG_PS2)
    # 가장 최근 이벤트 기준으로 쿨다운 판단 (ts1 or ts2는 ts2가 더 최근이어도 ts1 반환하는 버그)
    if ts1 is None and ts2 is None:
        return False
    if ts1 is None:
        ts = ts2
    elif ts2 is None:
        ts = ts1
    else:
        ts = ts1 if ts1 >= ts2 else ts2
    if ts is None:
        return False
    try:
        if hasattr(ts, 'timestamp'):
            t = ts.timestamp()
        else:
            t = (ts - datetime(1970, 1, 1)).total_seconds()
        return (datetime.now(timezone.utc).timestamp() - t) < (PARTIAL_STOP_COOLDOWN_MINUTES * 60)
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


def log_execution_event(ticker: str, signal: str, tag: str, price: float = None) -> bool:
    """실행 이벤트(EXEC_BUY/EXEC_SELL/DCA_BUY/PS1/PS2)를 ExecutionEvent 테이블에 기록.
    [L3 FIX] AnalysisResult.decision_reason 텍스트 태깅 방식에서 전용 테이블로 교체.
    성공 시 True, 실패 시 False 반환 (절대 예외 미발생).
    주의: False 반환 시 쿨다운 태그가 DB에 없어 다음 사이클에서 중복 실행될 수 있음."""
    import logging
    _logger = logging.getLogger(__name__)
    try:
        from trading_bot.db import get_session
        from trading_bot.models import ExecutionEvent
        session = get_session()
        try:
            rec = ExecutionEvent(
                ticker=ticker,
                tag=tag,
                signal=signal,
                price=price,
                ts=datetime.now(timezone.utc),
            )
            session.add(rec)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            _logger.error(
                '[log_execution_event] DB 커밋 실패 (%s/%s): %s — 쿨다운 태그 미기록, 다음 사이클 중복 실행 위험',
                ticker, tag, e,
            )
            return False
        finally:
            session.close()
    except Exception as e:
        _logger.error(
            '[log_execution_event] 세션 획득 실패 (%s/%s): %s — 쿨다운 태그 미기록',
            ticker, tag, e,
        )
        return False
