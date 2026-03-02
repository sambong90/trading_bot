def check_daily_loss(cash, starting_cash, daily_loss_limit_pct=0.03):
    draw = (starting_cash - cash) / starting_cash
    return draw <= daily_loss_limit_pct

def check_total_drawdown(current_value, peak_value, max_drawdown_pct=0.15):
    draw = (peak_value - current_value) / peak_value
    return draw <= max_drawdown_pct


# [NEW] Order 테이블에서 실제 연속 손실 횟수 계산 (Paper/Live 공통)
def get_consecutive_losses() -> int:
    """
    가장 최근 체결(Order) 기준으로 연속 손실 횟수 반환.
    손실 = sell 체결가 < raw.entry_price.
    계산 실패 시 0 반환.
    """
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Order
        session = get_session()
        try:
            rows = session.query(Order).filter(Order.side == 'sell').order_by(Order.ts.desc()).limit(50).all()
            if not rows:
                return 0
            consecutive = 0
            for r in rows:
                raw = r.raw if isinstance(r.raw, dict) else {}
                entry_price = float(raw.get('entry_price', 0) or 0)
                sell_price = float(r.price or 0)
                if entry_price <= 0:
                    break
                if sell_price < entry_price:
                    consecutive += 1
                else:
                    break
            return consecutive
        finally:
            session.close()
    except Exception:
        return 0


# [NEW] 최근 lookback 건의 sell 체결 중 수익 비율 반환
def get_win_rate(lookback: int = 20) -> float:
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Order
        session = get_session()
        try:
            rows = session.query(Order).filter(Order.side == 'sell').order_by(Order.ts.desc()).limit(lookback).all()
            if not rows:
                return 0.5
            wins = 0
            for r in rows:
                raw = r.raw if isinstance(r.raw, dict) else {}
                entry_price = float(raw.get('entry_price', 0) or 0)
                sell_price = float(r.price or 0)
                if entry_price > 0 and sell_price >= entry_price:
                    wins += 1
            return wins / len(rows)
        finally:
            session.close()
    except Exception:
        return 0.5


def calculate_adjusted_position_size(
    account_value,
    risk_per_trade_pct=0.02,
    stop_loss_pct=0.05,
    use_dynamic_adjustment=True,
):
    """포지션 크기(KRW) 및 리스크 조정 정보 반환. (strategy 호환)"""
    # [IMPROVED] 실제 연속 손실 / 승률 기반 포지션 조정
    consecutive_losses = get_consecutive_losses() if use_dynamic_adjustment else 0
    win_rate = get_win_rate() if use_dynamic_adjustment else 0.5

    multiplier = 1.0
    is_defensive = False

    if consecutive_losses >= 4:
        multiplier = 0.0
        is_defensive = True
    elif consecutive_losses == 3:
        multiplier = 0.5
        is_defensive = True
    elif consecutive_losses == 2:
        multiplier = 0.75
        is_defensive = True
    elif win_rate < 0.4:
        multiplier = 0.75
        is_defensive = True

    base_size = account_value * risk_per_trade_pct / (stop_loss_pct or 0.05)
    adjusted = base_size * multiplier

    risk_adjustments = {
        'position_size_multiplier': multiplier,
        'is_defensive_mode': is_defensive,
        'consecutive_losses': consecutive_losses,
        'win_rate': round(win_rate, 3),
        'atr_trailing_multiplier': 2.0,
    }
    return adjusted, risk_adjustments


def check_circuit_breaker(current_equity, peak_equity, daily_start_equity):
    """Drawdown Circuit Breaker: 일간/전체 DD 임계값 초과 시 발동.

    Returns:
        (triggered: bool, reason: str, daily_dd_pct: float, total_dd_pct: float)
    """
    from trading_bot.config import DD_DAILY_LIMIT_PCT, DD_TOTAL_LIMIT_PCT

    daily_dd_pct = 0.0
    total_dd_pct = 0.0

    if daily_start_equity and daily_start_equity > 0:
        daily_dd_pct = (daily_start_equity - current_equity) / daily_start_equity * 100
    if peak_equity and peak_equity > 0:
        total_dd_pct = (peak_equity - current_equity) / peak_equity * 100

    if daily_dd_pct >= DD_DAILY_LIMIT_PCT:
        return (True, f'일간 DD {daily_dd_pct:.1f}% >= 임계값 {DD_DAILY_LIMIT_PCT}%',
                daily_dd_pct, total_dd_pct)
    if total_dd_pct >= DD_TOTAL_LIMIT_PCT:
        return (True, f'전체 DD {total_dd_pct:.1f}% >= 임계값 {DD_TOTAL_LIMIT_PCT}%',
                daily_dd_pct, total_dd_pct)
    return (False, '', daily_dd_pct, total_dd_pct)


def get_system_state(key, default=None):
    """system_state 테이블에서 key 값 조회. 실패 시 default 반환."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import SystemState
        session = get_session()
        try:
            row = session.query(SystemState).filter(SystemState.key == key).first()
            return row.value if row else default
        finally:
            session.close()
    except Exception:
        return default


def set_system_state(key, value):
    """system_state 테이블에 key=value upsert. 실패 시 False 반환."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import SystemState
        session = get_session()
        try:
            row = session.query(SystemState).filter(SystemState.key == key).first()
            if row:
                row.value = str(value)
            else:
                session.add(SystemState(key=key, value=str(value)))
            session.commit()
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    except Exception:
        return False
