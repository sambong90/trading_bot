def check_daily_loss(cash, starting_cash, daily_loss_limit_pct=0.03):
    draw = (starting_cash - cash) / starting_cash
    return draw <= daily_loss_limit_pct

def check_total_drawdown(current_value, peak_value, max_drawdown_pct=0.15):
    draw = (peak_value - current_value) / peak_value
    return draw <= max_drawdown_pct


# [NEW] Order 테이블에서 실제 연속 손실 횟수 계산 (Paper/Live 공통)
def get_consecutive_losses() -> int:
    """
    연속 손실 횟수의 단일 소스. orders 기반 raw 스트릭 + 시간 감쇠 적용.
    손실 = sell 체결가 < raw.entry_price.
    시간 감쇠(데드락 출구 경로):
      - 마지막 손실로부터 STREAK_DECAY_HOURS(기본 24h)마다 1 감소
      - 마지막 거래로부터 STREAK_RESET_HOURS(기본 48h) 무거래 시 0으로 리셋
    DYN_THR 페널티와 size_multiplier 모두 이 함수만 참조해야 한다.
    계산 실패 시 0 반환.
    """
    try:
        from datetime import datetime, timezone
        from trading_bot.db import get_session
        from trading_bot.models import Order
        from trading_bot.config import STREAK_DECAY_HOURS, STREAK_RESET_HOURS
        session = get_session()
        try:
            rows = session.query(Order).filter(Order.side == 'sell').order_by(Order.ts.desc()).limit(50).all()
            if not rows:
                return 0
            raw_streak = 0
            last_loss_ts = None
            for r in rows:
                raw = r.raw if isinstance(r.raw, dict) else {}
                if raw.get('exit_reason') == 'CB_FORCED':
                    continue  # CB 강제매도는 전략 진입 실패가 아님 → 스트릭 제외(투명)
                entry_price = float(raw.get('entry_price', 0) or 0)
                sell_price = float(r.price or 0)
                if entry_price <= 0:
                    continue  # entry_price 미기록 행은 스킵 (streak 유지)
                if sell_price < entry_price:
                    raw_streak += 1
                    if last_loss_ts is None:
                        last_loss_ts = r.ts
                else:
                    break
            if raw_streak == 0:
                return 0
            last_order = session.query(Order).order_by(Order.ts.desc()).first()
            last_trade_ts = last_order.ts if last_order else last_loss_ts
        finally:
            session.close()

        now = datetime.now(timezone.utc)

        def _hours(ts):
            if ts is None:
                return 0.0
            if getattr(ts, 'tzinfo', None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (now - ts).total_seconds() / 3600.0

        # 마지막 거래 후 무거래가 길면 스트릭 자체를 리셋
        if STREAK_RESET_HOURS > 0 and _hours(last_trade_ts) >= STREAK_RESET_HOURS:
            return 0
        # 마지막 손실 경과 시간만큼 단계적 감쇠
        decay = int(_hours(last_loss_ts) // STREAK_DECAY_HOURS) if STREAK_DECAY_HOURS > 0 else 0
        return max(0, raw_streak - decay)
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
            valid = 0
            for r in rows:
                raw = r.raw if isinstance(r.raw, dict) else {}
                if raw.get('exit_reason') == 'CB_FORCED':
                    continue  # CB 강제매도는 전략 성과가 아님 → 승률 계산 제외
                entry_price = float(raw.get('entry_price', 0) or 0)
                sell_price = float(r.price or 0)
                if entry_price <= 0:
                    continue  # entry_price 미기록 행은 분모/분자 모두 제외
                valid += 1
                if sell_price >= entry_price:
                    wins += 1
            return wins / valid if valid > 0 else 0.5
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

    from trading_bot.config import SIZE_MULT_BY_STREAK, SIZE_MULT_FLOOR

    # 연속 손실 → 사이즈 배수 (절대 0 금지: floor 적용으로 데드락 방지)
    multiplier = SIZE_MULT_BY_STREAK.get(consecutive_losses, SIZE_MULT_FLOOR)
    multiplier = max(SIZE_MULT_FLOOR, multiplier)
    is_defensive = consecutive_losses >= 2
    if win_rate < 0.4:
        multiplier = min(multiplier, 0.75)
        is_defensive = True
    multiplier = max(SIZE_MULT_FLOOR, multiplier)

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


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4: 자금 관리 및 생존 전략
# ══════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass

@dataclass
class ExitSignal:
    action: str   # 'sell_all' | 'sell_half' | 'hold'
    reason: str
    urgency: str  # 'hard' | 'trail' | 'none'


# Opportunity Cost Swap 우선순위 가중치 (피보존 우선, ROI 2순위)
_FIB_ZONE_SWAP_SCORE: dict[str, int] = {
    'EXIT_ZONE': 5, 'BELOW_SWING': 5,
    'LAST_BUY_CHANCE': 4,
    'REDUCE_50PCT': 3,
    'PIVOT_ZONE': 2,
    'BUY_ZONE': 1, 'HOLD': 1, 'HOLD_STRONG': 1, 'ABOVE_SWING': 0,
}

# EM-3 CASH_RULE — 장세별 현금 하한선
_CASH_FLOOR_BY_REGIME: dict[str, float] = {
    'BEAR_CONFIRMED': 1.00, 'BEAR_WARNING': 1.00,
    'NO_TRADE': 1.00,       'UNKNOWN': 1.00,
    'SIDEWAYS': 0.50,       'BULL_EARLY': 0.50,
    'BULL_CONFIRMED': 0.30, 'BULL_CLIMAX': 0.20,
}


def compute_cash_ratio(executor, total_equity: float) -> float:
    """가용 현금(KRW) / 총 평가액. total_equity <= 0 이면 1.0 반환."""
    if total_equity <= 0:
        return 1.0
    try:
        cash = float(executor.get_available_cash() or 0)
        return cash / total_equity
    except Exception:
        return 1.0


def check_cash_floor(
    regime: str,
    cash_ratio: float,
    is_panic_dip: bool = False,
    fib_zone: str = '',
    fib_retrace: float = 1.0,
    max_signal_strength: float = 0.0,
    fng_value: int = 50,
) -> tuple:
    """EM-3 CASH_RULE 체크.

    Returns: (buy_allowed: bool, cash_floor: float, is_panic_mode: bool)
    """
    floor = _CASH_FLOOR_BY_REGIME.get(regime, 0.50)

    # BEAR 장세: 매수 전면 차단
    if floor >= 1.0:
        return False, floor, False

    # BULL_CONFIRMED/CLIMAX: EM-3 면제 (position_cap이 상한 역할)
    if regime in ('BULL_CONFIRMED', 'BULL_CLIMAX'):
        return True, floor, False

    # Panic Dip 예외: 4개 AND 조건 충족 시 현금 15%까지 소진 허용
    panic_ok = (
        is_panic_dip                                                        # FNG <= 20
        and fib_zone in ('BUY_ZONE', 'PIVOT_ZONE', 'HOLD_STRONG', 'HOLD')  # 피보 매수 구간
        and fib_retrace <= 0.618                                            # 마지노선 위
        and max_signal_strength >= 0.70                                     # 신호 최소 강도
    )
    if panic_ok:
        return True, 0.15, True

    if cash_ratio < floor:
        return False, floor, False
    return True, floor, False


def get_trailing_peak(ticker: str) -> float:
    """SystemState에서 ticker trailing peak 조회. 없으면 0.0."""
    try:
        return float(get_system_state(f'trailing_peak_{ticker}', '0') or 0)
    except Exception:
        return 0.0


def update_trailing_peak(ticker: str, current_price: float) -> float:
    """current_price > peak 이면 갱신. 현재 peak(갱신 or 유지) 반환."""
    peak = get_trailing_peak(ticker)
    if current_price > peak:
        set_system_state(f'trailing_peak_{ticker}', str(current_price))
        return current_price
    return peak


def reset_trailing_peak(ticker: str) -> None:
    """전량 매도 또는 신규 진입 시 trailing peak 초기화."""
    set_system_state(f'trailing_peak_{ticker}', '0')


def evaluate_trailing_stop(
    ticker: str,
    current_price: float,
    current_roi: float,
    trail_pct: float = 0.04,
) -> tuple:
    """수익 구간(roi > 5%)에서 고점 대비 trail_pct 하락 시 50% 매도 신호.

    Returns: (should_sell_half: bool, reason: str)
    """
    if current_roi <= 8.0:
        update_trailing_peak(ticker, current_price)
        return False, ''

    peak = update_trailing_peak(ticker, current_price)
    if peak <= 0:
        return False, ''

    trail_price = peak * (1.0 - trail_pct)
    if current_price < trail_price:
        return (
            True,
            f'TrailingStop: {current_price:.0f} < peak{trail_pct*100:.0f}%({trail_price:.0f})',
        )
    return False, ''


def evaluate_exit_signal(
    pattern_signals: list,
    fib_manager,
    current_price: float,
    avg_buy_price: float,
    sell_blocked: bool = False,
) -> 'ExitSignal':
    """X-02 Hard Stop + Fib 0.618/0.786 마지노선 판정.

    sell_blocked=True (G-02 SUPER_CRISIS) 면 모든 exit 억제.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    if sell_blocked:
        return ExitSignal(action='hold', reason='G-02:SELL_BLOCKED', urgency='none')

    # X-02: LOYALTY_FAIL 패턴 감지
    loyalty_fail = any(
        getattr(s, 'signal', '') == 'FAIL' or 'LOYALTY_FAIL' in getattr(s, 'label', '')
        for s in (pattern_signals or [])
    )

    # Fib 0.618 마지노선 이탈 (종가 기준)
    fib_retrace = 0.0
    fib_618_breach = False
    fib_786_breach = False
    if fib_manager is not None:
        try:
            fib_retrace = float(fib_manager.retrace_ratio(current_price))
            fib_618_breach = fib_retrace > 0.618
            fib_786_breach = fib_retrace > 0.786
        except Exception as e:
            _log.debug('[ExitSignal] fib 계산 실패: %s', e)

    if fib_786_breach:
        return ExitSignal(
            action='sell_all',
            reason=f'FIB_786_BREACH(r={fib_retrace:.3f}) — 추세 사실상 종료',
            urgency='hard',
        )
    if loyalty_fail and fib_618_breach:
        return ExitSignal(
            action='sell_all',
            reason=f'X-02:LOYALTY_FAIL + FIB_618_BREACH(r={fib_retrace:.3f})',
            urgency='hard',
        )
    if loyalty_fail:
        return ExitSignal(action='sell_all', reason='X-02:LOYALTY_FAIL', urgency='hard')
    if fib_618_breach:
        return ExitSignal(
            action='sell_all',
            reason=f'FIB_618_BREACH(r={fib_retrace:.3f}) — 마지노선 이탈',
            urgency='hard',
        )

    return ExitSignal(action='hold', reason='', urgency='none')


def get_swap_score(current_roi: float, fib_zone: str) -> float:
    """Opportunity Cost Swap 우선순위 점수. 높을수록 교체 우선 대상."""
    zone_score = _FIB_ZONE_SWAP_SCORE.get(fib_zone, 1)
    loss_component = max(0.0, -current_roi)
    return zone_score * 10 + loss_component


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
