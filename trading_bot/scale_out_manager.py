"""
Scale-Out (Partial Sell) state persistence via SQLite.
Stage: 0 = no partial sell, 1 = 5% ROI 25% sold, 2 = 10% ROI another 25% sold.
Uses position_states table for concurrency safety and consistency with the rest of the bot.
"""
from trading_bot.db import get_session, ensure_tables
from trading_bot.models import PositionState


def get_scale_out_state(ticker: str, current_avg_buy_price: float, position_qty: float) -> int:
    """
    Return scale-out stage for ticker: 0, 1, or 2.
    - If position_qty <= 0: delete record for ticker and return 0.
    - If no record: return 0.
    - Water-riding: if avg_buy_price changed > 1% (additional buys), reset stage to 0 and return 0.
    - Otherwise return record.stage.
    """
    ensure_tables()
    session = get_session()
    try:
        if position_qty is None or position_qty <= 0:
            session.query(PositionState).filter(PositionState.ticker == ticker).delete()
            session.commit()
            return 0

        record = session.query(PositionState).filter(PositionState.ticker == ticker).first()
        if record is None:
            return 0

        current = float(current_avg_buy_price or 0.0)
        if current <= 0:
            return record.stage

        stored = float(record.avg_buy_price or 0.0)
        if stored <= 0:
            return record.stage

        change_pct = abs(record.avg_buy_price - current_avg_buy_price) / current_avg_buy_price
        if change_pct > 0.01:
            record.stage = 0
            record.avg_buy_price = current_avg_buy_price
            session.commit()
            return 0

        return max(0, min(2, int(record.stage)))
    finally:
        session.close()


def set_scale_out_stage(ticker: str, new_stage: int, current_avg_buy_price: float) -> None:
    """Upsert PositionState for ticker with new_stage and current_avg_buy_price."""
    ensure_tables()
    session = get_session()
    try:
        stage = max(0, min(2, int(new_stage)))
        price = float(current_avg_buy_price or 0.0)

        record = session.query(PositionState).filter(PositionState.ticker == ticker).first()
        if record:
            record.stage = stage
            record.avg_buy_price = price
        else:
            session.add(PositionState(ticker=ticker, stage=stage, avg_buy_price=price))
        session.commit()
    finally:
        session.close()


def get_trailing_high(ticker: str) -> float:
    """DB에서 저장된 trailing_high 조회. 없으면 0.0."""
    try:
        session = get_session()
        try:
            record = session.query(PositionState).filter(PositionState.ticker == ticker).first()
            return float(record.trailing_high or 0.0) if record else 0.0
        finally:
            session.close()
    except Exception:
        return 0.0


def update_trailing_high(ticker: str, new_high: float) -> None:
    """trailing_high를 ratchet 방식으로 갱신 (올라갈 때만 업데이트)."""
    ensure_tables()
    session = get_session()
    try:
        record = session.query(PositionState).filter(PositionState.ticker == ticker).first()
        if record:
            current = float(record.trailing_high or 0.0)
            if new_high > current:
                record.trailing_high = new_high
                session.commit()
        else:
            session.add(PositionState(ticker=ticker, stage=0, avg_buy_price=0.0, trailing_high=new_high))
            session.commit()
    finally:
        session.close()


def reset_trailing_high(ticker: str) -> None:
    """포지션 청산 시 trailing_high 리셋."""
    try:
        session = get_session()
        try:
            record = session.query(PositionState).filter(PositionState.ticker == ticker).first()
            if record:
                record.trailing_high = 0.0
                session.commit()
        finally:
            session.close()
    except Exception:
        pass

