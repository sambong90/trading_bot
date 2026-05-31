"""
watchdog.py — 실시간 이상 감지 알림 시스템.

APScheduler에 5분 간격으로 등록. 완전 읽기 전용 (알림 로그 제외).
watchdog 전체가 try/except로 래핑돼 매매 사이클에 영향을 주지 않는다.
"""
import json
import logging
import os
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 경로 / 임계값 상수
# ---------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parent.parent
HEARTBEAT_FILE = ROOT / 'trading_bot' / 'logs' / 'scheduler_heartbeat.json'

HEARTBEAT_STALE_MIN = 10      # heartbeat 무응답 기준 (분)
CYCLE_DELAY_MIN = 5           # 사이클 지연 기준 (분)
MACRO_OPEN_WAIT_HOURS = 1     # 미장 개장 후 이 시간 내 fresh 수집 없으면 경고
KIMP_STALE_HOURS = 12         # 김프 미갱신 기준
FNG_STALE_HOURS = 6           # FNG 미갱신 기준
SLIPPAGE_WARN_PCT = 2.0       # 슬리피지 경고 기준 (%)
CB_PREWARNING_RATIO = 0.80    # CB 기준의 80% 도달 시 경고
UPBIT_FAIL_THRESHOLD = 3      # Upbit API 연속 실패 기준 횟수
NO_TRADE_HOURS = 24           # 무거래 알림 기준 (시간)
COOLDOWN_HOURS = 1            # 동일 이상 알림 쿨다운 (시간)
LOSS_STREAK_WARN = 3          # 연속 손실 알림 기준 (회)
LOSS_STREAK_PERSIST_HOURS = 24  # 해당 스트릭이 이 시간 이상 지속 시 알림
SIZE_MULT_WARN = 0.5          # size_multiplier 알림 기준 (이하)

BULL_REGIMES = {'BULL_EARLY', 'BULL_CONFIRMED', 'BULL_CLIMAX'}

# ---------------------------------------------------------------------------
# 프로세스 상태 (pod 재시작 시 초기화 — 의도된 동작)
# ---------------------------------------------------------------------------
_cooldown: dict = {}          # { key: datetime }
_upbit_fail_count: int = 0

# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _elapsed_hours(ts) -> float:
    """DB timestamp(timezone-aware or naive) → 경과 시간(시간). 비교 기준 UTC."""
    if ts is None:
        return float('inf')
    try:
        if getattr(ts, 'tzinfo', None) is not None:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.utcnow()
        return (now - ts).total_seconds() / 3600
    except Exception:
        return float('inf')


def _should_alert(key: str) -> bool:
    """쿨다운 체크. COOLDOWN_HOURS 미경과면 False."""
    now = datetime.now()
    last = _cooldown.get(key)
    if last and (now - last) < timedelta(hours=COOLDOWN_HOURS):
        return False
    _cooldown[key] = now
    return True


def _send(msg: str) -> None:
    """TELEGRAM_ALERT_LEVEL 필터 후 텔레그램 발송."""
    try:
        from trading_bot.config import TELEGRAM_ALERT_LEVEL
    except Exception:
        TELEGRAM_ALERT_LEVEL = 'TRADE'

    if TELEGRAM_ALERT_LEVEL == 'OFF':
        return

    if '🚨' in msg:
        pass  # CRITICAL/TRADE/SUMMARY 모두 발송
    elif '⚠️' in msg:
        if TELEGRAM_ALERT_LEVEL == 'CRITICAL':
            return
    elif 'ℹ️' in msg:
        if TELEGRAM_ALERT_LEVEL in ('CRITICAL', 'TRADE'):
            return

    try:
        from trading_bot.monitor import send_telegram
        send_telegram(msg)
    except Exception as e:
        logger.error('[watchdog] 텔레그램 발송 실패: %s', e)


# ---------------------------------------------------------------------------
# 이상 감지 체크 — 시스템
# ---------------------------------------------------------------------------

def _check_heartbeat() -> None:
    """1. 스케줄러 heartbeat 무응답 (10분 이상 미갱신)."""
    try:
        if not HEARTBEAT_FILE.exists():
            if _should_alert('heartbeat_down'):
                _send('🚨 스케줄러 무응답 (heartbeat 파일 없음)')
            return
        data = json.loads(HEARTBEAT_FILE.read_text(encoding='utf-8'))
        ts = datetime.strptime(data.get('ts', ''), '%Y-%m-%d %H:%M:%S')
        elapsed_min = (datetime.now() - ts).total_seconds() / 60
        if elapsed_min >= HEARTBEAT_STALE_MIN:
            if _should_alert('heartbeat_down'):
                _send(
                    f'🚨 스케줄러 무응답 '
                    f'(마지막 heartbeat: {ts.strftime("%H:%M")}, {int(elapsed_min)}분 전)'
                )
    except Exception as e:
        logger.warning('[watchdog] heartbeat 체크 실패: %s', e)


def _check_cycle_delay() -> None:
    """2. 매매 사이클 지연 (HH:01 기준 5분 초과)."""
    if os.environ.get('ENABLE_AUTO_TRADING', '0') != '1':
        return
    try:
        from trading_bot.risk import get_system_state
        ts_str = get_system_state('last_cycle_completed', '')
        if not ts_str:
            return  # 기동 초기, 사이클 미실행
        last = datetime.fromisoformat(ts_str)
        now = datetime.now()
        scheduled = now.replace(minute=1, second=0, microsecond=0)
        if now < scheduled:
            scheduled -= timedelta(hours=1)
        delay_sec = (now - scheduled).total_seconds()
        if last < scheduled and delay_sec >= CYCLE_DELAY_MIN * 60:
            if _should_alert('cycle_delay'):
                _send(
                    f'⚠️ 매매 사이클 지연 '
                    f'(예정 {scheduled.strftime("%H:%M")}, 마지막 완료 {last.strftime("%H:%M")})'
                )
    except Exception as e:
        logger.warning('[watchdog] 사이클 지연 체크 실패: %s', e)


def _check_db_connection() -> None:
    """3. DB 연결 실패."""
    try:
        from trading_bot.db import get_session
        from sqlalchemy import text
        session = get_session()
        try:
            session.execute(text('SELECT 1'))
        finally:
            session.close()
    except Exception:
        if _should_alert('db_down'):
            _send('🚨 DB 연결 실패')


def _check_upbit_api() -> None:
    """4. Upbit API 연속 실패 (3회 연속)."""
    global _upbit_fail_count
    try:
        import pyupbit
        price = pyupbit.get_current_price('KRW-BTC')
        if price and float(price) > 0:
            _upbit_fail_count = 0
            return
        _upbit_fail_count += 1
    except Exception:
        _upbit_fail_count += 1

    if _upbit_fail_count >= UPBIT_FAIL_THRESHOLD:
        if _should_alert('upbit_api_down'):
            _send(f'🚨 Upbit API 장애 ({_upbit_fail_count}회 연속 실패)')


# ---------------------------------------------------------------------------
# 이상 감지 체크 — 매매
# ---------------------------------------------------------------------------

def _check_roundtrip_trades() -> None:
    """5. 왕복 매매 감지 (동일 종목 1시간 내 매수+매도)."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AiEvent
        from sqlalchemy import and_

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        session = get_session()
        try:
            events = (
                session.query(AiEvent)
                .filter(
                    and_(
                        AiEvent.event == 'EXECUTE',
                        AiEvent.ts >= cutoff,
                    )
                )
                .order_by(AiEvent.ticker, AiEvent.ts)
                .all()
            )
        finally:
            session.close()

        ticker_evs: dict = {}
        for e in events:
            if e.ticker:
                ticker_evs.setdefault(e.ticker, []).append(e)

        for ticker, evs in ticker_evs.items():
            has_buy = any(e.signal == 'buy' for e in evs)
            has_sell = any(e.signal == 'sell' for e in evs)
            if has_buy and has_sell:
                buy_t = min(e.ts for e in evs if e.signal == 'buy')
                sell_t = max(e.ts for e in evs if e.signal == 'sell')
                # tz-aware → strftime 가능하도록 naive 변환
                buy_hm = buy_t.strftime('%H:%M') if buy_t else '?'
                sell_hm = sell_t.strftime('%H:%M') if sell_t else '?'
                if _should_alert(f'roundtrip_{ticker}'):
                    _send(
                        f'⚠️ 왕복 매매 감지: {ticker} '
                        f'(매수 {buy_hm} → 매도 {sell_hm})'
                    )
    except Exception as e:
        logger.warning('[watchdog] 왕복 매매 체크 실패: %s', e)


def _check_slippage() -> None:
    """6. 슬리피지 2% 초과 (Order.raw의 signal_price vs fill price)."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Order
        from sqlalchemy import and_

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        session = get_session()
        try:
            orders = (
                session.query(Order)
                .filter(Order.ts >= cutoff)
                .all()
            )
        finally:
            session.close()

        for order in orders:
            raw = order.raw if isinstance(order.raw, dict) else {}
            signal_price = raw.get('signal_price')
            fill_price = order.price
            if not signal_price or not fill_price or float(fill_price) <= 0:
                continue
            slippage_pct = abs(float(fill_price) - float(signal_price)) / float(signal_price) * 100
            if slippage_pct > SLIPPAGE_WARN_PCT:
                ticker = raw.get('ticker') or str(order.order_id or '?')
                if _should_alert(f'slippage_{ticker}'):
                    _send(f'⚠️ 높은 슬리피지: {ticker} {slippage_pct:.1f}%')
    except Exception as e:
        logger.warning('[watchdog] 슬리피지 체크 실패: %s', e)


def _check_cb_prewarning() -> None:
    """7. 일간 DD CB 발동 기준의 80% 도달 시 사전 경고."""
    try:
        from trading_bot.risk import get_system_state
        from trading_bot.config import DD_DAILY_LIMIT_PCT

        daily_start = float(get_system_state('daily_start_equity', '0') or 0)
        current = float(get_system_state('prev_cycle_equity', '0') or 0)
        if daily_start <= 0 or current <= 0:
            return

        daily_dd = (daily_start - current) / daily_start * 100
        warn_threshold = DD_DAILY_LIMIT_PCT * CB_PREWARNING_RATIO

        if daily_dd >= warn_threshold:
            if _should_alert('cb_prewarning'):
                _send(
                    f'⚠️ 일간 DD {daily_dd:.1f}% 도달 '
                    f'(CB 발동 기준 {DD_DAILY_LIMIT_PCT:.0f}%)'
                )
    except Exception as e:
        logger.warning('[watchdog] CB 사전 경고 체크 실패: %s', e)


def _check_no_trade() -> None:
    """8. BULL_EARLY 이상 장세에서 24시간 매수 체결 0건."""
    try:
        from trading_bot.risk import get_system_state
        from trading_bot.db import get_session
        from trading_bot.models import AiEvent
        from sqlalchemy import and_

        regime = get_system_state('last_guardian_regime', 'UNKNOWN') or 'UNKNOWN'
        if regime not in BULL_REGIMES:
            return

        cutoff = datetime.now(timezone.utc) - timedelta(hours=NO_TRADE_HOURS)
        session = get_session()
        try:
            buy_count = (
                session.query(AiEvent)
                .filter(
                    and_(
                        AiEvent.event == 'EXECUTE',
                        AiEvent.signal == 'buy',
                        AiEvent.ts >= cutoff,
                    )
                )
                .count()
            )
        finally:
            session.close()

        if buy_count == 0:
            if _should_alert('no_trade'):
                _send(f'ℹ️ {regime} 장세 {NO_TRADE_HOURS}h 무거래')
    except Exception as e:
        logger.warning('[watchdog] 무거래 체크 실패: %s', e)


def _check_loss_streak() -> None:
    """8b. 연속 손실 페널티 장기 지속/사이즈 축소 감지 (데드락 조기 경보)."""
    try:
        from trading_bot.risk import get_consecutive_losses, get_system_state
        from trading_bot.config import DYN_THR_BY_REGIME, SIZE_MULT_BY_STREAK, SIZE_MULT_FLOOR
        from trading_bot.db import get_session
        from trading_bot.models import Order

        consec = get_consecutive_losses()
        if consec < LOSS_STREAK_WARN:
            return

        mult = max(SIZE_MULT_FLOOR, SIZE_MULT_BY_STREAK.get(consec, SIZE_MULT_FLOOR))
        # 사이즈 축소 진입 알림 (1회)
        if mult <= SIZE_MULT_WARN and _should_alert('size_mult_low'):
            _send(f'⚠️ 포지션 사이즈 배수 {mult:.2f} (연속손실 {consec}회) — 진입 축소 중')

        # 현재 스트릭의 가장 오래된 손실 시각 → 지속 시간 계산
        session = get_session()
        try:
            rows = session.query(Order).filter(Order.side == 'sell').order_by(Order.ts.desc()).limit(50).all()
        finally:
            session.close()
        streak_start_ts = None
        for r in rows:
            raw = r.raw if isinstance(r.raw, dict) else {}
            entry = float(raw.get('entry_price', 0) or 0)
            sell_price = float(r.price or 0)
            if entry <= 0:
                continue
            if sell_price < entry:
                streak_start_ts = r.ts  # 루프 종료 시 가장 오래된 손실
            else:
                break
        persisted_h = _elapsed_hours(streak_start_ts)
        if persisted_h < LOSS_STREAK_PERSIST_HOURS:
            return

        regime = get_system_state('last_guardian_regime', 'UNKNOWN') or 'UNKNOWN'
        base_thr = DYN_THR_BY_REGIME.get(regime, 1.0)
        penalty = consec * 0.02
        dyn_thr = min(0.99, base_thr + penalty)
        if _should_alert('loss_streak'):
            _send(
                f'⚠️ 연속손실 {consec}회, {persisted_h:.0f}h 지속 — '
                f'DYN_THR +{penalty:.2f}(={dyn_thr:.2f}), size×{mult:.2f} 적용 중'
            )
    except Exception as e:
        logger.warning('[watchdog] 연속손실 체크 실패: %s', e)


# ---------------------------------------------------------------------------
# 이상 감지 체크 — 데이터
# ---------------------------------------------------------------------------

def _check_macro_staleness() -> None:
    """9. 미장 개장 후 macro fresh 데이터 미수집 감지. 공휴일/주말이면 알림 없음."""
    try:
        from trading_bot.market_calendar import is_us_market_open, hours_since_last_close
        from trading_bot.db import get_session
        from trading_bot.models import MacroSnapshot

        now_utc = datetime.now(timezone.utc)
        if not is_us_market_open(now_utc):
            return  # 휴장일/주말 — 알림 없음

        if hours_since_last_close(now_utc) < MACRO_OPEN_WAIT_HOURS:
            return  # 개장 직후 — 수집 대기 시간 이내

        session = get_session()
        try:
            latest = (
                session.query(MacroSnapshot)
                .filter(MacroSnapshot.ratio_quality == 'fresh')
                .order_by(MacroSnapshot.ts.desc())
                .first()
            )
        finally:
            session.close()

        elapsed = _elapsed_hours(latest.ts if latest else None)
        if elapsed >= 3:
            if _should_alert('macro_stale_post_open'):
                _send(f'⚠️ 미장 개장 후 macro 갱신 없음 ({elapsed:.0f}h 경과)')
    except Exception as e:
        logger.warning('[watchdog] macro staleness 체크 실패: %s', e)


def _check_kimp_staleness() -> None:
    """10. 김프 수집 12시간 이상 미갱신."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import KimpSnapshot

        session = get_session()
        try:
            latest = (
                session.query(KimpSnapshot)
                .order_by(KimpSnapshot.ts.desc())
                .first()
            )
        finally:
            session.close()

        elapsed = _elapsed_hours(latest.ts if latest else None)
        if elapsed >= KIMP_STALE_HOURS:
            if _should_alert('kimp_stale'):
                _send('⚠️ 김프 데이터 12h+ 미갱신')
    except Exception as e:
        logger.warning('[watchdog] 김프 신선도 체크 실패: %s', e)


def _check_fng_staleness() -> None:
    """11. FNG 6시간 이상 미갱신이고 live fallback도 실패 중."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import SentimentSnapshot

        session = get_session()
        try:
            latest = (
                session.query(SentimentSnapshot)
                .order_by(SentimentSnapshot.ts.desc())
                .first()
            )
        finally:
            session.close()

        elapsed = _elapsed_hours(latest.ts if latest else None)
        if elapsed < FNG_STALE_HOURS:
            return  # DB 데이터 신선 — 이상 없음

        # DB 스테일 → live fallback 시도
        try:
            import requests
            r = requests.get('https://api.alternative.me/fng/', timeout=5)
            r.raise_for_status()
            if r.json().get('data'):
                return  # live fallback 성공 — 알림 불필요
        except Exception:
            pass

        if _should_alert('fng_stale'):
            _send('⚠️ FNG 데이터 확보 불가 (fallback=50 적용 중)')
    except Exception as e:
        logger.warning('[watchdog] FNG 신선도 체크 실패: %s', e)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def run_watchdog() -> None:
    """APScheduler에서 5분 간격으로 호출. 예외가 매매 사이클에 전파되지 않는다."""
    try:
        _check_heartbeat()
        _check_cycle_delay()
        _check_db_connection()
        _check_upbit_api()
        _check_roundtrip_trades()
        _check_slippage()
        _check_cb_prewarning()
        _check_no_trade()
        _check_loss_streak()
        _check_macro_staleness()
        _check_kimp_staleness()
        _check_fng_staleness()
    except Exception as e:
        logger.error('[watchdog] 실행 중 예외: %s', e, exc_info=True)
