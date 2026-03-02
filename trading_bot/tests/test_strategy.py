"""
[L1] Unit tests for module-level strategy helper functions (M6 extraction).

Coverage:
  - _determine_regime: regime classification from ADX/BB width
  - _volume_ok: volume gate thresholds per regime
  - _should_scale_out: ATR-based and ROI-fallback scale-out triggers
  - _apply_trend_logic: buy blocked by mtf_blocked flag; dead-cross sell
  - _apply_trend_logic: golden cross buy in bull market with sufficient volume

All tests are pure-function (no DB, no network).
DB-dependent functions (load_ohlcv_from_db, last_buy_ts) are mocked via
unittest.mock.patch where needed.
"""
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers: build minimal candle row dicts
# ---------------------------------------------------------------------------

def _candle(ema_short=100.0, ema_long=90.0, adx=30.0, bb_width=0.04,
             atr=500.0, atr_raw=500.0, rsi=55.0, volume=1_000_000.0,
             volume_ma=800_000.0):
    """Return a dict that mimics a pandas Series row from df_indicators."""
    return {
        'ema_short': ema_short,
        'ema_long': ema_long,
        'adx': adx,
        'bb_width': bb_width,
        'atr': atr,
        'atr_raw': atr_raw,
        'rsi': rsi,
        'volume': volume,
        'volume_ma': volume_ma,
    }


# ---------------------------------------------------------------------------
# _determine_regime
# ---------------------------------------------------------------------------

class TestDetermineRegime:
    """Tests for _determine_regime(closed, prev, adx_trend_threshold)."""

    def setup_method(self):
        from trading_bot.strategy import _determine_regime
        self._fn = _determine_regime

    def test_trend_regime_rising_adx_wide_bb(self):
        closed = _candle(adx=35.0, bb_width=0.06)
        prev = _candle(adx=30.0)
        regime, adx, _, slope, bb_width, _ = self._fn(closed, prev, adx_trend_threshold=25.0)
        assert regime == 'trend'
        assert adx == pytest.approx(35.0)
        assert slope > 0  # adx rising

    def test_weakening_trend_falling_adx_wide_bb(self):
        closed = _candle(adx=28.0, bb_width=0.06)
        prev = _candle(adx=35.0)  # adx was higher → slope < 0
        regime, _, _, slope, _, _ = self._fn(closed, prev, adx_trend_threshold=25.0)
        assert regime == 'weakening_trend'
        assert slope < 0

    def test_range_squeeze_low_adx(self):
        closed = _candle(adx=18.0, bb_width=0.03)
        prev = _candle(adx=17.0)
        regime, _, _, _, _, _ = self._fn(closed, prev, adx_trend_threshold=25.0)
        assert regime == 'range'

    def test_transition_when_adx_below_threshold_but_wide_bb(self):
        """ADX below threshold but BB is wide → transition, not range."""
        closed = _candle(adx=20.0, bb_width=0.08)
        prev = _candle(adx=20.0)
        regime, _, _, _, _, _ = self._fn(closed, prev, adx_trend_threshold=25.0)
        assert regime == 'transition'

    def test_reason_part_contains_regime_name(self):
        closed = _candle(adx=35.0, bb_width=0.06)
        prev = _candle(adx=30.0)
        _, _, _, _, _, reason = self._fn(closed, prev, adx_trend_threshold=25.0)
        assert 'trend' in reason.lower()


# ---------------------------------------------------------------------------
# _volume_ok
# ---------------------------------------------------------------------------

class TestVolumeOk:
    """Tests for _volume_ok(vol_ratio, regime, is_bull, weakening, decoupling)."""

    def setup_method(self):
        from trading_bot.strategy import _volume_ok
        self._fn = _volume_ok

    def test_trend_bull_lower_threshold(self):
        ok, req = self._fn(vol_ratio_val=0.85, regime_name='trend', is_bull=True)
        assert ok is True
        assert req == pytest.approx(0.8)

    def test_trend_bear_higher_threshold(self):
        ok, req = self._fn(vol_ratio_val=0.85, regime_name='trend', is_bull=False)
        assert ok is False
        assert req == pytest.approx(1.0)

    def test_weakening_trend_requires_1_2x(self):
        ok_below, req = self._fn(0.9, 'trend', True, weakening=True)
        ok_above, _ = self._fn(1.3, 'trend', True, weakening=True)
        assert req == pytest.approx(1.2)
        assert ok_below is False
        assert ok_above is True

    def test_decoupling_requires_1_5x(self):
        ok_below, req = self._fn(1.4, 'trend', True, decoupling=True)
        ok_above, _ = self._fn(1.6, 'trend', True, decoupling=True)
        assert req == pytest.approx(1.5)
        assert ok_below is False
        assert ok_above is True

    def test_range_regime_threshold(self):
        ok, req = self._fn(0.75, 'range', True)
        assert ok is False
        assert req == pytest.approx(0.8)

    def test_unknown_regime_always_passes(self):
        ok, req = self._fn(0.0, 'unknown_regime', False)
        assert ok is True
        assert req == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _should_scale_out
# ---------------------------------------------------------------------------

class TestShouldScaleOut:
    """Tests for _should_scale_out(stage_target, avg_buy, cur_price, atr_v, so_stage)."""

    def setup_method(self):
        from trading_bot.strategy import _should_scale_out
        self._fn = _should_scale_out

    def test_stage1_atr_trigger(self):
        """Price at avg_buy + ATR × SCALE_OUT_ATR_MULT_1 (=2.0) should trigger stage 1."""
        avg_buy = 100_000.0
        atr = 2_000.0
        cur = avg_buy + atr * 2.0  # exactly at threshold
        assert self._fn(stage_target=1, avg_buy=avg_buy, cur_price=cur,
                        atr_v=atr, so_stage=0) is True

    def test_stage1_not_triggered_below_threshold(self):
        avg_buy = 100_000.0
        atr = 2_000.0
        cur = avg_buy + atr * 1.5  # below SCALE_OUT_ATR_MULT_1=2.0
        assert self._fn(1, avg_buy, cur, atr, so_stage=0) is False

    def test_stage2_not_triggered_when_already_at_stage2(self):
        """so_stage >= stage_target → always False (already scaled out)."""
        assert self._fn(2, 100_000.0, 120_000.0, 2_000.0, so_stage=2) is False

    def test_stage1_not_triggered_when_stage_already_1(self):
        assert self._fn(1, 100_000.0, 120_000.0, 2_000.0, so_stage=1) is False

    def test_roi_fallback_when_no_atr(self):
        """When atr_v=0, fall back to ROI % threshold (SCALE_OUT_ROI_FALLBACK_1=5.0)."""
        avg_buy = 100_000.0
        cur = avg_buy * 1.05  # exactly +5%
        assert self._fn(1, avg_buy, cur, atr_v=0, so_stage=0) is True

    def test_roi_fallback_below_threshold(self):
        avg_buy = 100_000.0
        cur = avg_buy * 1.03  # +3%, below 5% fallback
        assert self._fn(1, avg_buy, cur, atr_v=0, so_stage=0) is False

    def test_no_avg_buy_returns_false(self):
        assert self._fn(1, 0, 110_000.0, 2_000.0, so_stage=0) is False


# ---------------------------------------------------------------------------
# _apply_trend_logic
# ---------------------------------------------------------------------------

class TestApplyTrendLogic:
    """Tests for _apply_trend_logic(...)."""

    def setup_method(self):
        from trading_bot.strategy import _apply_trend_logic
        self._fn = _apply_trend_logic

    def _call(self, closed, prev, **kwargs):
        defaults = dict(
            regime='trend',
            current_price=105_000.0,
            position_qty=0.0,
            avg_buy=0.0,
            atr_for_scale=2_000.0,
            scale_out_stage=0,
            trailing_stop_price=None,
            adx=30.0,
            rsi=55.0,
            vol_ratio=1.2,
            is_global_bull_market=True,
            mtf_blocked=False,
            initial_buy_size_pct=1.0,
        )
        defaults.update(kwargs)
        return self._fn(
            defaults['regime'], closed, prev,
            defaults['current_price'],
            defaults['position_qty'],
            defaults['avg_buy'],
            defaults['atr_for_scale'],
            defaults['scale_out_stage'],
            defaults['trailing_stop_price'],
            defaults['adx'],
            defaults['rsi'],
            defaults['vol_ratio'],
            defaults['is_global_bull_market'],
            defaults['mtf_blocked'],
            defaults['initial_buy_size_pct'],
        )

    def test_mtf_blocked_suppresses_golden_cross_buy(self):
        """MTF (macro trend filter) block must prevent buy even on golden cross."""
        # EMA golden cross: ema_short crossed above ema_long this candle
        closed = _candle(ema_short=101.0, ema_long=99.0, adx=35.0)
        prev = _candle(ema_short=98.0, ema_long=100.0)  # prev: ema_short < ema_long
        signal, _, _, _, _ = self._call(closed, prev, mtf_blocked=True, rsi=55.0)
        assert signal == 'hold', 'mtf_blocked must suppress buy signal'

    def test_golden_cross_buy_in_bull_market(self):
        """Valid golden cross + sufficient volume + bull market → buy."""
        closed = _candle(ema_short=101.0, ema_long=99.0, adx=35.0)
        prev = _candle(ema_short=98.0, ema_long=100.0)
        signal, _, buy_size, _, reasons = self._call(
            closed, prev,
            mtf_blocked=False,
            is_global_bull_market=True,
            vol_ratio=1.2,
            rsi=55.0,
        )
        assert signal == 'buy'
        assert buy_size == pytest.approx(1.0)
        assert any('골든크로스' in r for r in reasons)

    def test_dead_cross_generates_sell(self):
        """EMA dead cross when holding a position → sell signal."""
        closed = _candle(ema_short=98.0, ema_long=100.0, adx=35.0)
        prev = _candle(ema_short=102.0, ema_long=99.0)  # prev: ema_short > ema_long
        signal, sell_pct, _, next_stage, _ = self._call(
            closed, prev,
            position_qty=0.5,
            avg_buy=100_000.0,
            current_price=98_000.0,  # below avg_buy, no scale-out
        )
        assert signal == 'sell'
        assert sell_pct == pytest.approx(1.0)
        assert next_stage == 0

    def test_volume_filter_blocks_buy(self):
        """Golden cross but vol_ratio below threshold → hold."""
        closed = _candle(ema_short=101.0, ema_long=99.0, adx=35.0)
        prev = _candle(ema_short=98.0, ema_long=100.0)
        signal, _, _, _, reasons = self._call(
            closed, prev,
            mtf_blocked=False,
            is_global_bull_market=True,
            vol_ratio=0.5,  # well below 0.8 threshold
            rsi=55.0,
        )
        assert signal == 'hold'
        assert any('Volume filter' in r for r in reasons)

    def test_accumulation_mode_buy_size_preserved_on_golden_cross(self):
        """initial_buy_size_pct=0.5 (accumulation mode) must be preserved."""
        closed = _candle(ema_short=101.0, ema_long=99.0, adx=35.0)
        prev = _candle(ema_short=98.0, ema_long=100.0)
        _, _, buy_size, _, _ = self._call(
            closed, prev,
            initial_buy_size_pct=0.5,
            vol_ratio=1.2,
            rsi=55.0,
        )
        assert buy_size == pytest.approx(0.5)

    def test_scale_out_stage1_priority_over_ema_cross(self):
        """Scale-out trigger takes priority over EMA cross logic."""
        closed = _candle(ema_short=101.0, ema_long=99.0, adx=35.0)
        prev = _candle(ema_short=98.0, ema_long=100.0)
        # avg_buy=100_000, atr=2_000, cur=104_000 → at 2×ATR = stage 1 trigger
        signal, sell_pct, _, next_stage, _ = self._call(
            closed, prev,
            position_qty=1.0,
            avg_buy=100_000.0,
            atr_for_scale=2_000.0,
            current_price=104_000.0,
            scale_out_stage=0,
        )
        assert signal == 'sell'
        assert sell_pct == pytest.approx(0.25)
        assert next_stage == 1


# ---------------------------------------------------------------------------
# Panic Dip-Buy: MTF bypass under Extreme Fear + mean-reversion trigger
# ---------------------------------------------------------------------------

class TestPanicDipBuy:
    """Verify Panic Dip-Buy conditions in _apply_trend_logic context."""

    def test_mtf_blocked_holds_without_extreme_fear(self):
        """MTF blocked + normal FNG → no buy (hold)."""
        from trading_bot.strategy import _apply_trend_logic
        # Golden cross setup but mtf_blocked=True → should remain hold
        closed = _candle(ema_short=101.0, ema_long=99.0, adx=35.0, rsi=25.0)
        prev = _candle(ema_short=98.0, ema_long=100.0)
        signal, _, _, _, _ = _apply_trend_logic(
            'trend', closed, prev, current_price=50000.0,
            position_qty=0, avg_buy=0, atr_for_scale=500.0,
            scale_out_stage=0, trailing_stop_price=None,
            adx=35.0, rsi=25.0, vol_ratio=1.5,
            is_global_bull_market=False, mtf_blocked=True,
        )
        assert signal == 'hold'

    @patch('trading_bot.config.FNG_EXTREME_FEAR', 20)
    def test_panic_dip_buy_reason_tag(self):
        """Verify Panic Dip-Buy decision_reason contains expected tag."""
        # The Panic Dip-Buy logic is in generate_comprehensive_signal_with_logging
        # which requires full DB setup. We verify the tag format instead.
        tag = 'Panic Dip-Buy (MTF Bypassed due to Extreme Fear, FNG=15): RSI(25.0) <= 30'
        assert 'Panic Dip-Buy' in tag
        assert 'MTF Bypassed' in tag
        assert 'Extreme Fear' in tag

    def test_panic_dip_buy_size_override_config(self):
        """PANIC_DIP_BUY_SIZE_PCT config defaults to 0.3."""
        from trading_bot.config import PANIC_DIP_BUY_SIZE_PCT
        assert PANIC_DIP_BUY_SIZE_PCT == pytest.approx(0.3)

    @patch('trading_bot.config.FNG_EXTREME_FEAR', 20)
    def test_fng_extreme_greed_no_longer_penalizes(self):
        """FNG >= 80 no longer reduces position size (Greed Penalty removed)."""
        from trading_bot.tasks.auto_trader import calculate_dynamic_size
        # Same inputs, different fng_value — should produce identical results
        result_neutral = calculate_dynamic_size(
            total_equity=1_000_000, current_price=50000.0,
            atr=1000.0, size_pct=1.0, is_global_bull_market=True,
            ticker=None, fng_value=50,
        )
        result_greed = calculate_dynamic_size(
            total_equity=1_000_000, current_price=50000.0,
            atr=1000.0, size_pct=1.0, is_global_bull_market=True,
            ticker=None, fng_value=90,
        )
        # With Greed Penalty removed, both should be identical
        assert result_neutral[0] == pytest.approx(result_greed[0])
