from __future__ import annotations

"""US market calendar — NYSE 휴장일·개장 시간 판정.

공개 API:
  is_us_market_open(dt)   — 개장일 여부 (주말+공휴일 제외)
  is_us_holiday(dt)       — 평일 공휴일만 반환 (주말 제외)
  get_holiday_name(dt)    — 공휴일 이름, 아니면 None
  is_us_market_hours(dt)  — 정규장 시간(09:30~16:00 ET) 여부
  get_last_trading_day()  — 가장 최근 개장일 (date)
  hours_since_last_close() — 마지막 마감(16:00 ET) 이후 경과 시간(h)

exchange_calendars 설치 시 자동 활용, 미설치 시 수동 목록 fallback.
서머타임은 zoneinfo('America/New_York')가 자동 반영.
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo('America/New_York')

# NYSE 공휴일 수동 목록 (exchange_calendars 미설치 시 fallback)
_US_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 1),   date(2025, 1, 20),  date(2025, 2, 17),
    date(2025, 4, 18),  date(2025, 5, 26),  date(2025, 6, 19),
    date(2025, 7, 4),   date(2025, 9, 1),   date(2025, 11, 27),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1),   date(2026, 1, 19),  date(2026, 2, 16),
    date(2026, 4, 3),   date(2026, 5, 25),  date(2026, 6, 19),
    date(2026, 7, 3),   date(2026, 9, 7),   date(2026, 11, 26),
    date(2026, 12, 25),
}

_HOLIDAY_NAMES: dict[date, str] = {
    date(2025, 1, 1):   "New Year's Day",
    date(2025, 1, 20):  "MLK Day",
    date(2025, 2, 17):  "Presidents' Day",
    date(2025, 4, 18):  "Good Friday",
    date(2025, 5, 26):  "Memorial Day",
    date(2025, 6, 19):  "Juneteenth",
    date(2025, 7, 4):   "Independence Day",
    date(2025, 9, 1):   "Labor Day",
    date(2025, 11, 27): "Thanksgiving",
    date(2025, 12, 25): "Christmas",
    date(2026, 1, 1):   "New Year's Day",
    date(2026, 1, 19):  "MLK Day",
    date(2026, 2, 16):  "Presidents' Day",
    date(2026, 4, 3):   "Good Friday",
    date(2026, 5, 25):  "Memorial Day",
    date(2026, 6, 19):  "Juneteenth",
    date(2026, 7, 3):   "Independence Day (observed)",
    date(2026, 9, 7):   "Labor Day",
    date(2026, 11, 26): "Thanksgiving",
    date(2026, 12, 25): "Christmas",
}


def _to_et_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET).date()


def _exchange_calendars_open(et_date: date) -> bool | None:
    """exchange_calendars로 개장일 판정. 미설치/오류 시 None 반환."""
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar('XNYS')
        return bool(cal.is_session(et_date.isoformat()))
    except ImportError:
        return None
    except Exception as e:
        logger.debug('[calendar] exchange_calendars 오류: %s', e)
        return None


def is_us_market_open(dt: datetime) -> bool:
    """ET 날짜 기준 미국 시장 개장일 여부 (주말+공휴일 제외).

    dt는 KST/UTC/naive 무관하게 내부에서 ET 날짜로 변환.
    exchange_calendars 설치 시 우선 사용, 아니면 수동 목록 fallback.
    """
    et_date = _to_et_date(dt)
    ec = _exchange_calendars_open(et_date)
    if ec is not None:
        return ec
    if et_date.weekday() >= 5:
        return False
    return et_date not in _US_HOLIDAYS


def is_us_holiday(dt: datetime) -> bool:
    """평일 미국 공휴일 여부만 반환 (주말 제외).

    주말이면 False. 평일이지만 공휴일이면 True.
    """
    et_date = _to_et_date(dt)
    if et_date.weekday() >= 5:
        return False
    ec = _exchange_calendars_open(et_date)
    if ec is not None:
        return not ec
    return et_date in _US_HOLIDAYS


def get_holiday_name(dt: datetime) -> str | None:
    """해당 ET 날짜의 공휴일 이름 반환. 공휴일이 아니면 None."""
    et_date = _to_et_date(dt)
    return _HOLIDAY_NAMES.get(et_date)


def is_us_market_hours(dt: datetime) -> bool:
    """ET 기준 정규장 시간 09:30~16:00 이내 여부 (개장일 여부 미포함)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et_time = dt.astimezone(_ET).time()
    return time(9, 30) <= et_time < time(16, 0)


def get_last_trading_day(dt: datetime | None = None) -> date:
    """가장 최근 미장 개장일 반환."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    candidate = _to_et_date(dt)
    for _ in range(14):
        probe = datetime(candidate.year, candidate.month, candidate.day, 12, 0, tzinfo=_ET)
        if is_us_market_open(probe):
            return candidate
        candidate -= timedelta(days=1)
    return candidate


def hours_since_last_close(dt: datetime | None = None) -> float:
    """마지막 미장 마감(16:00 ET) 이후 경과 시간(h)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    last_td = get_last_trading_day(dt)
    last_close = datetime(last_td.year, last_td.month, last_td.day, 16, 0, tzinfo=_ET)
    delta = dt - last_close.astimezone(timezone.utc)
    return max(0.0, delta.total_seconds() / 3600)
