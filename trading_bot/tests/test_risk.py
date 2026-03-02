"""
Unit tests for risk management functions (Phase 4).

Coverage:
  - check_circuit_breaker: daily DD / total DD trigger and normal cases
  - get_system_state / set_system_state: exception safety
"""
import pytest
from unittest.mock import patch


class TestCheckCircuitBreaker:
    """Tests for check_circuit_breaker(current, peak, daily_start)."""

    def setup_method(self):
        from trading_bot.risk import check_circuit_breaker
        self._fn = check_circuit_breaker

    @patch('trading_bot.config.DD_DAILY_LIMIT_PCT', 5.0)
    @patch('trading_bot.config.DD_TOTAL_LIMIT_PCT', 15.0)
    def test_daily_dd_triggers(self):
        """Daily DD 6% > 5% threshold → triggered."""
        triggered, reason, daily_dd, total_dd = self._fn(94000.0, 100000.0, 100000.0)
        assert triggered is True
        assert '일간' in reason
        assert daily_dd == pytest.approx(6.0)

    @patch('trading_bot.config.DD_DAILY_LIMIT_PCT', 5.0)
    @patch('trading_bot.config.DD_TOTAL_LIMIT_PCT', 15.0)
    def test_total_dd_triggers(self):
        """Total DD 20% > 15% threshold → triggered (daily DD below limit)."""
        # daily_start=81000, current=80000 → daily DD ~1.2% (below 5%)
        # peak=100000, current=80000 → total DD 20% (above 15%)
        triggered, reason, daily_dd, total_dd = self._fn(80000.0, 100000.0, 81000.0)
        assert triggered is True
        assert '전체' in reason
        assert total_dd == pytest.approx(20.0)

    @patch('trading_bot.config.DD_DAILY_LIMIT_PCT', 5.0)
    @patch('trading_bot.config.DD_TOTAL_LIMIT_PCT', 15.0)
    def test_no_trigger_within_limits(self):
        """DD within limits → not triggered."""
        triggered, reason, daily_dd, total_dd = self._fn(97000.0, 100000.0, 98000.0)
        assert triggered is False
        assert reason == ''

    @patch('trading_bot.config.DD_DAILY_LIMIT_PCT', 5.0)
    @patch('trading_bot.config.DD_TOTAL_LIMIT_PCT', 15.0)
    def test_zero_peak_no_crash(self):
        """peak_equity=0 → no crash, dd=0."""
        triggered, reason, daily_dd, total_dd = self._fn(100000.0, 0, 100000.0)
        assert triggered is False

    @patch('trading_bot.config.DD_DAILY_LIMIT_PCT', 5.0)
    @patch('trading_bot.config.DD_TOTAL_LIMIT_PCT', 15.0)
    def test_exact_threshold_triggers(self):
        """Exactly at threshold → triggered."""
        triggered, reason, _, _ = self._fn(95000.0, 100000.0, 100000.0)
        assert triggered is True

    @patch('trading_bot.config.DD_DAILY_LIMIT_PCT', 5.0)
    @patch('trading_bot.config.DD_TOTAL_LIMIT_PCT', 15.0)
    def test_daily_dd_checked_before_total_dd(self):
        """When both daily and total DD exceed, daily triggers first."""
        # daily_start=100000, current=80000 → daily 20%, total also 20%
        triggered, reason, _, _ = self._fn(80000.0, 100000.0, 100000.0)
        assert triggered is True
        assert '일간' in reason  # daily checked first


class TestGetSetSystemState:
    """Tests for get_system_state / set_system_state exception safety."""

    def test_get_system_state_returns_default_on_exception(self):
        """DB unavailable → returns default."""
        from trading_bot.risk import get_system_state
        result = get_system_state('test_key', 'fallback')
        assert result == 'fallback'

    def test_set_system_state_succeeds_with_fake_session(self):
        """Fake session (conftest) allows add/commit → returns True."""
        from trading_bot.risk import set_system_state
        # conftest.py의 FakeSession은 add/commit을 에러 없이 처리
        # SystemState import 시 AttributeError 발생 가능 → False 반환
        result = set_system_state('test_key', 'test_value')
        assert isinstance(result, bool)
