"""MarketGuardian — L1 Global Filter + L2 Market Regime 평가기.

core_logic_distilled.md DECISION FLOW 구현:
  evaluate_L1() → P1 BLOCK 하나라도 → NO_TRADE 반환 (L2 평가 생략)
               → P2 RESTRICT → L2 이동 (포지션 상한 조정)
               → PASS → evaluate_L2() → regime + position_cap 결정

사용법:
  result = MarketGuardian().evaluate()
  if not result.tradeable:
      # 매수 로직 전면 스킵

  # G-02 SUPER_CRISIS → sell도 차단, 나머지 P1 → buy만 차단
  is_global_bull_market = result.tradeable and result.regime not in ('BEAR_CONFIRMED', 'BEAR_WARNING')
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── L2 포지션 상한 테이블 (core_logic_distilled.md 장세별 허용 행동 매트릭스) ──
_REGIME_POSITION_CAP: dict[str, float] = {
    'BEAR_CONFIRMED': 0.0,
    'BEAR_WARNING':   0.0,
    'SIDEWAYS':       0.20,
    'BULL_EARLY':     0.50,
    'BULL_CONFIRMED': 0.70,
    'BULL_CLIMAX':    0.80,
}

# ── market_condition 임계값 (master_strategy_filtered.md Section 0) ──
_MC_EPS = 0.05   # 0.05% 미만 = flat


@dataclass
class GuardianResult:
    """MarketGuardian.evaluate() 반환값."""
    tradeable: bool              # True = L1 전부 통과 + 데이터 fresh
    regime: str                  # BEAR_CONFIRMED | BEAR_WARNING | SIDEWAYS | BULL_EARLY | BULL_CONFIRMED | BULL_CLIMAX
    position_cap: float          # 0.0 ~ 0.80 — 허용 최대 포지션 비율 (L2 결정)
    allow_new_entry: bool        # G-05/G-08 발동 시 False
    block_alt_buys: bool         # G-14 발동 시 True (알트 전면 차단)
    buy_size_multiplier: float   # G-10: 0.5 / 기본 1.0
    flags: list[str] = field(default_factory=list)         # 발동된 모든 플래그
    block_reasons: list[str] = field(default_factory=list) # P1 차단 이유 (tradeable=False 시)


class MarketGuardian:
    """L1/L2 평가기. 매 사이클 1회 인스턴스 생성 후 evaluate() 호출."""

    def evaluate(self) -> GuardianResult:
        """전체 L1+L2 평가 수행. 실패 안전(fail-safe): 데이터 없으면 보수적으로 반환."""
        from trading_bot.collectors.aggregator import get_market_context
        ctx = get_market_context()

        macro            = ctx.get('macro') or {}
        dominance        = ctx.get('dominance') or {}
        btc_w200         = ctx.get('btc_weekly_200_above', False)
        stale_but_usable = ctx.get('stale_but_usable', False)

        flags: list[str] = []
        block_reasons: list[str] = []

        # ── EM-7 + 데이터 부재 체크 ────────────────────────────────────────
        if not ctx.get('is_tradeable', False):
            for r in ctx.get('block_reasons', []):
                block_reasons.append(r)
            logger.warning('[Guardian] 데이터 미비/Stale — %s', block_reasons)
            return GuardianResult(
                tradeable=False, regime='UNKNOWN', position_cap=0.0,
                allow_new_entry=False, block_alt_buys=True,
                buy_size_multiplier=1.0,
                flags=['DATA_UNAVAILABLE'], block_reasons=block_reasons,
            )

        # ── DEFLATING 연속 카운트 (G-04, G-09) ────────────────────────────
        deflating_streak = self._deflating_streak()

        # ════════════════════════════════════════════════════════════════════
        # L1-A  절대 차단 P1 — 하나라도 해당 시 즉시 NO_TRADE 반환
        # ════════════════════════════════════════════════════════════════════
        sell_blocked = False  # G-02 전용: True면 매도까지 차단

        # G-01: DXY 초록 구간 진입
        if macro.get('dxy_zone') == 'DXY_GREEN_ZONE':
            block_reasons.append('G-01:DXY_GREEN_ZONE')

        # G-02: 달러+금+나스닥 동시 위기 (매도까지 차단)
        if macro.get('crisis_level') == 'SUPER_CRISIS':
            block_reasons.append('G-02:SUPER_CRISIS')
            sell_blocked = True

        # G-03: 채권 2차하락 임박
        if macro.get('bond_signal') == 'SECONDARY_DROP_IMMINENT':
            block_reasons.append('G-03:BOND_SECONDARY_DROP')

        # G-04: DEFLATING 연속 3회+
        if deflating_streak >= 3:
            block_reasons.append(f'G-04:DEFLATING_STREAK_{deflating_streak}')
            flags.append(f'DEFLATING_STREAK_{deflating_streak}')

        # G-13: DXY↑ AND Gold↑ 동시 (리만급 전조)
        if macro.get('gold_crisis_signal') == 'SEVERE_CRISIS':
            block_reasons.append('G-13:GOLD_SEVERE_CRISIS')

        if block_reasons:
            regime = 'NO_TRADE'
            logger.warning('[L1] P1 차단 — %s', block_reasons)
            return GuardianResult(
                tradeable=False, regime=regime, position_cap=0.0,
                allow_new_entry=False, block_alt_buys=True,
                buy_size_multiplier=1.0,
                flags=flags + (['SELL_BLOCKED'] if sell_blocked else []),
                block_reasons=block_reasons,
            )

        # ════════════════════════════════════════════════════════════════════
        # L1-B  노출도 제한 P2 — 차단 없이 position_cap / 플래그 조정
        # ════════════════════════════════════════════════════════════════════
        position_cap        = 1.0   # L2에서 최종 결정
        allow_new_entry     = True
        block_alt_buys      = False
        buy_size_multiplier = 1.0

        ratio = macro.get('nasdaq_dxy_ratio', 0.0) or 0.0

        # G-05: 교환비 ≥ 440 (BUBBLE 구간)
        if ratio >= 440:
            allow_new_entry = False
            position_cap    = 0.0
            flags.append('G-05:BUBBLE_RATIO_BLOCK')
            logger.info('[L1-B] G-05 발동 — nasdaq_dxy_ratio=%.1f → 신규 매수 전면 차단', ratio)

        # G-06: 채권 약세장 1단계
        elif macro.get('bond_signal') == 'BEAR_MARKET_1':
            position_cap = min(position_cap, 0.50)
            flags.append('G-06:BEAR_MARKET_1')

        # G-08: 위기 전조 — 신규 진입 보류
        if macro.get('crisis_level') == 'PRE_CRISIS':
            allow_new_entry = False
            flags.append('G-08:PRE_CRISIS')

        # G-09: DEFLATING 연속 2회
        if deflating_streak >= 2:
            flags.append(f'G-09:BEAR_WARNING_DEFLATING_{deflating_streak}')

        # G-10: 교환비 ≥ 370 (ELEVATED 구간) — 매수 규모 50%
        if ratio >= 370 and ratio < 440:
            buy_size_multiplier = 0.5
            flags.append('G-10:ELEVATED_MACRO_50PCT')

        # G-07: 원유 변동성 활성 → ALT 노출도 캡 30% (플래그만; 실행은 analyze_ticker에서)
        if macro.get('oil_vol_active'):
            flags.append('G-07:OIL_VOL_ACTIVE_ALT_CAP_30')

        # G-12: 엔화 급락
        if macro.get('jpy_signal') == 'ASIA_INSTABILITY':
            flags.append('G-12:ASIA_INSTABILITY')

        # G-14: 도미 ≥ 50 + BTC 주봉200 붕괴 → 알트 대학살 자리
        btc_dom = dominance.get('btc_dominance', 0.0) or 0.0
        if btc_dom >= 50.0 and not btc_w200:
            block_alt_buys = True
            flags.append('G-14:ALT_MASSACRE_ZONE')
            logger.info('[L1-B] G-14 발동 — BTC.D=%.1f%% 주봉200붕괴 → ALT 매수 전면 차단', btc_dom)

        # G-11: BUBBLE 연속 3회+ → 거품 경고 / 추가 매수 자제 (P3)
        from trading_bot.config import G11_BUBBLE_STREAK_MIN, G11_BUBBLE_BUY_SIZE_MULT
        bubble_streak = self._bubble_streak()
        if bubble_streak >= G11_BUBBLE_STREAK_MIN:
            buy_size_multiplier = min(buy_size_multiplier, G11_BUBBLE_BUY_SIZE_MULT)
            flags.append(f'G-11:BUBBLE_STREAK_{bubble_streak}')
            logger.info(
                '[L1-B] G-11 발동 — BUBBLE streak=%d → buy_size_mult=%.1f',
                bubble_streak, G11_BUBBLE_BUY_SIZE_MULT,
            )

        # G-15: DXY 저점 상승 + NASDAQ 고점 하락 → BEAR_DIVERGENCE / 신규 매수 차단 (P3)
        if self._check_g15_bear_divergence():
            allow_new_entry = False
            flags.append('G-15:BEAR_DIVERGENCE')
            logger.warning('[L1-B] G-15 발동 — DXY 저점상승+NDX 고점하락 → 신규 매수 차단')

        # ════════════════════════════════════════════════════════════════════
        # L2  장세 분류 — DominanceSnapshot.bull_stage + BTC EMA 추세 결합
        # ════════════════════════════════════════════════════════════════════
        regime = self._evaluate_l2(dominance)

        # L2 position_cap을 L1-B cap과 교집합(더 낮은 값 적용)
        l2_cap = _REGIME_POSITION_CAP.get(regime, 0.0)
        position_cap = min(position_cap, l2_cap)

        # G-05/G-08 으로 이미 allow_new_entry=False면 position_cap도 0
        if not allow_new_entry:
            position_cap = 0.0

        # ── STALE_BUT_USABLE: 신규 진입 사이즈 50% 축소 ────────────────────
        if stale_but_usable:
            flags.append('STALE_BUT_USABLE')
            buy_size_multiplier = min(buy_size_multiplier, 0.5)
            logger.info('[Guardian] STALE_BUT_USABLE — buy_size_multiplier capped at 0.5')

        logger.info(
            '[Guardian] L1 PASS | L2 regime=%s cap=%.0f%% new_entry=%s alt_block=%s size_mult=%.1f flags=%s',
            regime, position_cap * 100, allow_new_entry, block_alt_buys,
            buy_size_multiplier, flags or 'none',
        )

        # ── 장세 전환 감지 → 텔레그램 즉시 알림 ──────────────────────────────
        try:
            from trading_bot.risk import get_system_state, set_system_state
            last_regime = get_system_state('last_guardian_regime', '') or ''
            if last_regime and last_regime != regime:
                from trading_bot.config import DYN_THR_BY_REGIME
                dyn_thr = DYN_THR_BY_REGIME.get(regime, 1.0)
                try:
                    from trading_bot.telegram_bot import notify_regime_change
                    notify_regime_change(last_regime, regime, position_cap, dyn_thr)
                except Exception:
                    pass
            set_system_state('last_guardian_regime', regime)
        except Exception:
            pass

        return GuardianResult(
            tradeable=True,
            regime=regime,
            position_cap=position_cap,
            allow_new_entry=allow_new_entry,
            block_alt_buys=block_alt_buys,
            buy_size_multiplier=buy_size_multiplier,
            flags=flags,
            block_reasons=[],
        )

    # ── L2 내부 메서드 ───────────────────────────────────────────────────────

    def _evaluate_l2(self, dominance: dict) -> str:
        """DominanceSnapshot.bull_stage + BTC EMA 추세 → L2 regime 반환."""
        bull_stage = dominance.get('bull_stage', 'NO_BULL') or 'NO_BULL'
        btc_bull   = self._check_btc_trend()  # True=골든크로스, False=데드크로스

        # R-15: BULL_CLIMAX_ZONE (도미 < 40)
        if bull_stage == 'BULL_CLIMAX_ZONE':
            return 'BULL_CLIMAX'

        # R-12: BULL_CONFIRMED (도미 41.55~58.85 하단)
        if bull_stage == 'BULL_CONFIRMED':
            return 'BULL_CONFIRMED'

        # R-09/R-10: BULL_EARLY
        if bull_stage == 'BULL_EARLY':
            return 'BULL_EARLY'

        # R-06: BULL_WATCHING + 중립 추세 → 횡보 매집
        if bull_stage == 'BULL_WATCHING':
            return 'SIDEWAYS'

        # NO_BULL 구간
        if bull_stage == 'NO_BULL':
            # R-01: BTC 데드크로스 + NO_BULL → BEAR_CONFIRMED
            if not btc_bull:
                return 'BEAR_CONFIRMED'
            # BTC는 골든크로스지만 도미 NO_BULL → 경고 (R-04/R-05)
            return 'BEAR_WARNING'

        # fallback
        return 'BEAR_CONFIRMED'

    def _check_btc_trend(self) -> bool:
        """KRW-BTC 일봉 EMA5 > EMA20 여부. 실패 시 True (필터 비적용 원칙)."""
        try:
            from trading_bot.data import fetch_ohlcv
            df = fetch_ohlcv(ticker='KRW-BTC', interval='day', count=60, use_db_first=True)
            if df is None or len(df) < 20:
                return True
            close = df['close']
            ema5  = close.ewm(span=5,  adjust=False).mean()
            ema20 = close.ewm(span=20, adjust=False).mean()
            return float(ema5.iloc[-1]) >= float(ema20.iloc[-1])
        except Exception as e:
            logger.debug('[Guardian] BTC EMA 추세 체크 실패(필터 비적용): %s', e)
            return True

    # ── DEFLATING 연속 카운트 ────────────────────────────────────────────────

    def _deflating_streak(self) -> int:
        """최근 5개 MacroSnapshot에서 DEFLATING 연속 개수를 계산해 반환.

        SystemState('deflating_streak')에 캐시 → 이번 사이클은 DB 읽기 1회.
        market_condition(dxy_1d_pct, nasdaq_1d_pct) 결과로 DEFLATING 판정.
        """
        try:
            streak = self._compute_deflating_streak()
            # SystemState 캐시 업데이트 (best-effort)
            try:
                from trading_bot.risk import set_system_state
                set_system_state('deflating_streak', str(streak))
            except Exception:
                pass
            return streak
        except Exception as e:
            logger.debug('[Guardian] DEFLATING streak 계산 실패: %s', e)
            # 실패 시 캐시값 복원 (없으면 0)
            try:
                from trading_bot.risk import get_system_state
                return int(get_system_state('deflating_streak', '0') or 0)
            except Exception:
                return 0

    def _compute_deflating_streak(self) -> int:
        from trading_bot.db import get_session
        from trading_bot.models import MacroSnapshot

        session = get_session()
        try:
            rows = (
                session.query(MacroSnapshot)
                .order_by(MacroSnapshot.ts.desc())
                .limit(5)
                .all()
            )
        finally:
            session.close()

        if not rows:
            return 0

        # 최신순으로 온 rows를 시간 순으로 뒤집어 연속 판정
        rows = list(reversed(rows))
        streak = 0
        for row in reversed(rows):  # 가장 최근부터 역순으로 연속 체크
            cond = _market_condition(
                dxy_change_pct=row.dxy_1d_pct or 0.0,
                asset_change_pct=row.nasdaq_1d_pct or 0.0,
            )
            if cond == 'DEFLATING':
                streak += 1
            else:
                break  # 연속 끊김
        return streak

    # ── G-11 BUBBLE 연속 카운트 ──────────────────────────────────────────────

    def _bubble_streak(self) -> int:
        """최근 5개 MacroSnapshot에서 BUBBLE 연속 개수를 계산해 반환 (G-11).

        SystemState('bubble_streak')에 캐시 → 이번 사이클은 DB 읽기 1회.
        """
        try:
            streak = self._compute_bubble_streak()
            try:
                from trading_bot.risk import set_system_state
                set_system_state('bubble_streak', str(streak))
            except Exception:
                pass
            return streak
        except Exception as e:
            logger.debug('[Guardian] BUBBLE streak 계산 실패: %s', e)
            try:
                from trading_bot.risk import get_system_state
                return int(get_system_state('bubble_streak', '0') or 0)
            except Exception:
                return 0

    def _compute_bubble_streak(self) -> int:
        from trading_bot.db import get_session
        from trading_bot.models import MacroSnapshot

        session = get_session()
        try:
            rows = (
                session.query(MacroSnapshot)
                .order_by(MacroSnapshot.ts.desc())
                .limit(5)
                .all()
            )
        finally:
            session.close()

        if not rows:
            return 0

        streak = 0
        for row in rows:  # 최신순 — BUBBLE 연속 체크
            cond = _market_condition(
                dxy_change_pct=row.dxy_1d_pct or 0.0,
                asset_change_pct=row.nasdaq_1d_pct or 0.0,
            )
            if cond == 'BUBBLE':
                streak += 1
            else:
                break
        return streak

    # ── G-15 BEAR_DIVERGENCE 감지 ────────────────────────────────────────────

    def _check_g15_bear_divergence(self) -> bool:
        """G-15: DXY 저점 상승(higher lows) + NASDAQ 고점 하락(lower highs) 동시 감지.

        최근 G15_DIVERGENCE_LOOKBACK 개 MacroSnapshot을 전반/후반으로 분할:
          후반 DXY min > 전반 DXY min  AND  후반 NASDAQ max < 전반 NASDAQ max → True.
        데이터 부족(lookback 미달) 시 False (필터 비적용 원칙).
        """
        try:
            from trading_bot.config import (
                G15_DIVERGENCE_LOOKBACK,
                G15_NDX_MIN_DROP_PCT,
                G15_DXY_MIN_RISE_PCT,
            )
            from trading_bot.db import get_session
            from trading_bot.models import MacroSnapshot

            session = get_session()
            try:
                rows = (
                    session.query(
                        MacroSnapshot.dxy_value,
                        MacroSnapshot.nasdaq_value,
                        MacroSnapshot.ratio_quality,
                    )
                    .filter(MacroSnapshot.dxy_value.isnot(None))
                    .filter(MacroSnapshot.nasdaq_value.isnot(None))
                    .filter(MacroSnapshot.ratio_quality == 'fresh')
                    .order_by(MacroSnapshot.ts.desc())
                    .limit(G15_DIVERGENCE_LOOKBACK)
                    .all()
                )
            finally:
                session.close()

            if len(rows) < G15_DIVERGENCE_LOOKBACK:
                return False

            rows = list(reversed(rows))  # oldest → newest
            half = len(rows) // 2

            dxy_prev   = [float(r[0]) for r in rows[:half]]
            dxy_recent = [float(r[0]) for r in rows[half:]]
            ndx_prev   = [float(r[1]) for r in rows[:half]]
            ndx_recent = [float(r[1]) for r in rows[half:]]

            dxy_higher_lows = min(dxy_recent) > min(dxy_prev) * (1 + G15_DXY_MIN_RISE_PCT / 100)
            ndx_lower_highs = max(ndx_recent) < max(ndx_prev) * (1 - G15_NDX_MIN_DROP_PCT / 100)

            return dxy_higher_lows and ndx_lower_highs
        except Exception as e:
            logger.debug('[Guardian] G-15 divergence 체크 실패(필터 비적용): %s', e)
            return False


# ── 모듈 수준 헬퍼 ───────────────────────────────────────────────────────────

def _market_condition(dxy_change_pct: float, asset_change_pct: float) -> str:
    """master_strategy_filtered.md Section 0: 7-조건 시장 국면 분류기.

    TIMEFRAME: 1d — dxy_change_pct, asset_change_pct 모두 일봉 전일 대비 % 변화.
    """
    eps = _MC_EPS
    dxy_up   = dxy_change_pct  >  eps
    dxy_down = dxy_change_pct  < -eps
    dxy_flat = not dxy_up and not dxy_down
    a_up     = asset_change_pct >  eps
    a_down   = asset_change_pct < -eps

    if dxy_up   and a_up:    return 'BUBBLE'
    if dxy_up   and not a_up and not a_down: return 'HOLDING'
    if dxy_flat and a_up:    return 'BUBBLE'
    if dxy_flat and a_down:  return 'DEFLATING'
    if dxy_down and not a_up and not a_down: return 'DEFLATING'
    if dxy_down and a_down:  return 'DEFLATING'
    if dxy_down and a_up:    return 'BULLISH'
    return 'NEUTRAL'
