"""pattern_recognizer.py — L3 패턴 인식기.

core_logic_distilled.md Tactical Execution 수식 구현:
  TS-1  꾸물꾸물   (Sluggish Crawl)          → E-07
  TS-2  용틀임     (Dragon Breakout Candle)  → E-02, E-06
  TS-3  배째기     (Abandonment Decline)     → X-08
  TS-4  충성심테스트 (Loyalty Test / Retest)  → E-03, E-10, X-02
  TS-5  태극문양   (Taiji Trend Reversal)    → 3-E, Section 12
  TS-6  숨은다이버전스 (Hidden Bull Divergence) → E-15
  TS-7  매물대포위전  (Siege Timing)           → E-19

FibonacciManager: Section 6 피보나치 체인 A/B/C 실시간 판별.

수치 기준 출처: master_strategy_filtered.md TS-1~4 + core_logic_distilled.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# TS-1 꾸물꾸물 상수 (master_strategy_filtered.md TS-1)
# ────────────────────────────────────────────────────────────────────────────
_CRAWL_LOOKBACK        = 20
_CRAWL_VOL_RATIO_MAX   = 0.70   # RANGE: 0.50~0.80
_CRAWL_SLOPE_MIN_PCT   = 0.05   # RANGE: 0.03~0.10  (%/캔들)
_CRAWL_SLOPE_MAX_PCT   = 0.30   # RANGE: 0.20~0.50
_CRAWL_WINDOW          = 5
_CRAWL_MIN_HH          = 3      # RANGE: 2~4

# ────────────────────────────────────────────────────────────────────────────
# TS-2 용틀임 상수 (master_strategy_filtered.md TS-2)
# ────────────────────────────────────────────────────────────────────────────
_DRAGON_LOOKBACK         = 20
_DRAGON_BODY_STRONG      = 3.0   # RANGE: 2.5~3.5
_DRAGON_BODY_WEAK        = 2.0   # RANGE: 1.8~2.5
_DRAGON_SHADOW_MAX       = 0.30  # RANGE: 0.20~0.40
_DRAGON_VOL_MIN          = 1.5   # RANGE: 1.3~2.0
_DRAGON_ATR_MIN          = 2.0   # RANGE: 1.8~2.5 (보조 확인용)

# ────────────────────────────────────────────────────────────────────────────
# TS-3 배째기 상수 (master_strategy_filtered.md TS-3)
# ────────────────────────────────────────────────────────────────────────────
_ABN_LOOKBACK        = 20
_ABN_VOL_MAX         = 0.50   # RANGE: 0.30~0.60
_ABN_DROP_MAX        = 0.03   # RANGE: 0.02~0.05  단일 캔들 최대 낙폭
_ABN_PANIC_THRESH    = 0.05   # RANGE: 0.04~0.07  패닉 셀 구분 기준
_ABN_MIN_CONSEC      = 3      # RANGE: 2~5
_ABN_CUMUL_MIN       = 0.05   # RANGE: 0.03~0.08

# ────────────────────────────────────────────────────────────────────────────
# TS-4 충성심 테스트 상수 (master_strategy_filtered.md TS-4)
# ────────────────────────────────────────────────────────────────────────────
_LYL_FIB_STRONG  = 0.382   # RANGE: 0.33~0.42
_LYL_FIB_LIMIT   = 0.618   # RANGE: 0.57~0.67
_LYL_FAIL_THRESH = 0.50    # RANGE: 0.45~0.55
_LYL_VOL_MIN     = 1.2     # RANGE: 1.0~1.5
_LYL_CANDLES_4H  = 30      # RANGE: 20~40
_LYL_CANDLES_1D  = 14      # RANGE: 7~21
_LYL_MIN_DD      = 0.05    # RANGE: 0.03~0.08

# ────────────────────────────────────────────────────────────────────────────
# TS-5 태극문양 상수 (Section 12 — 수식 도출)
# ────────────────────────────────────────────────────────────────────────────
_TAIJI_LOOKBACK      = 20
_TAIJI_WICK_MIN_PCT  = 0.005  # 꼬리 깊이 ≥ 0.5% → 강도 +1
_TAIJI_VOL_CONFIRM   = 1.2    # 반전 캔들 거래량 ≥ 평균 1.2배 → 강도 +1

# ────────────────────────────────────────────────────────────────────────────
# TS-6 숨은 다이버전스 (Section 30)
# ────────────────────────────────────────────────────────────────────────────
_HDIV_LOOKBACK   = 30   # 비교 구간
_HDIV_RSI_PERIOD = 14
_HDIV_MIN_GAP    = 3    # 두 저점 사이 최소 캔들 수

# ────────────────────────────────────────────────────────────────────────────
# TS-7 매물대 포위전 (Section 15)
# ────────────────────────────────────────────────────────────────────────────
_SIEGE_MIN_CANDLES_4H = 42   # 7일 × 6(4h봉/일)
_SIEGE_MIN_CANDLES_1H = 168  # 7일 × 24(1h봉/일)
_SIEGE_MIN_CANDLES_1D = 7
_SIEGE_ZONE_PCT       = 0.03  # 가격 범위 ±3% = "같은 구간"으로 간주

# ────────────────────────────────────────────────────────────────────────────
# 피보나치 체인 (Section 6)
# ────────────────────────────────────────────────────────────────────────────
_FIB_LEVELS = [0.114, 0.236, 0.382, 0.500, 0.618, 0.786, 0.886]
_FIB_EXT    = [1.272, 1.414, 1.618]
_FIB_CHAIN  = {
    'A': {'entry': [0.382, 0.618], 'target': 1.618},  # 표준 조정 후 목표 확장
    'B': {'entry': [0.114, 0.886], 'target': 1.414},  # 극단 되돌림 + 확장
    'C': {'entry': [0.236, 0.786], 'target': 1.272},  # 약한 조정 후 중간 확장
}
# 레벨 → 체인 역방향 맵
_LEVEL_TO_CHAIN: dict[float, str] = {
    0.114: 'B', 0.236: 'C', 0.382: 'A',
    0.618: 'A', 0.786: 'C', 0.886: 'B',
}
_FIB_TOLERANCE = 0.03   # 레벨 인식 허용 오차 ±3%


# ============================================================================
# 데이터 클래스
# ============================================================================

@dataclass
class PatternSignal:
    """단일 패턴 감지 결과."""
    pattern_id: str        # "E-07", "E-06", "3-E", ...
    label: str             # "CRAWL_BUY", "DRAGON_STRONG", ...
    signal: str            # "buy" | "sell" | "watch"
    strength: float        # 0.0 ~ 1.0 (충족 조건 비율)
    timeframe: str         # "4h" | "1d"
    meta: dict = field(default_factory=dict)  # 수치 증거

    def __str__(self) -> str:
        return (
            f'[{self.pattern_id}] {self.label} '
            f'strength={self.strength:.2f} '
            f'meta={self.meta}'
        )


# ============================================================================
# FibonacciManager
# ============================================================================

class FibonacciManager:
    """Section 6 피보나치 체인 A/B/C 실시간 계산·판별기."""

    def __init__(self, swing_high: float, swing_low: float):
        if swing_high <= swing_low:
            raise ValueError(f'swing_high({swing_high}) must be > swing_low({swing_low})')
        self.high = swing_high
        self.low  = swing_low
        self._range = swing_high - swing_low

    # ── 레벨 계산 ────────────────────────────────────────────────────────────

    def levels(self) -> dict[str, float]:
        """모든 되돌림·확장 레벨을 가격으로 반환."""
        result: dict[str, float] = {}
        for r in _FIB_LEVELS:
            result[f'r{int(r*1000):04d}'] = round(self.high - self._range * r, 2)
        for e in _FIB_EXT:
            result[f'e{int(e*1000):04d}'] = round(self.low + self._range * e, 2)
        return result

    def retrace_ratio(self, price: float) -> float:
        """현재 가격의 되돌림 비율 r ∈ [0, 1+]."""
        if self._range == 0:
            return 0.0
        return (self.high - price) / self._range

    # ── 활성 체인 판별 ───────────────────────────────────────────────────────

    def active_chain(self, price: float) -> str:
        """현재 가격이 어느 체인의 영향권인지 반환. 허용 오차 ±3%.

        로직: retrace_ratio 기준으로 가장 가까운 체인 레벨에 매핑.
        """
        r = self.retrace_ratio(price)
        best_level: Optional[float] = None
        best_dist = float('inf')
        for lvl in _LEVEL_TO_CHAIN:
            dist = abs(r - lvl)
            if dist < best_dist:
                best_dist = dist
                best_level = lvl
        if best_dist <= _FIB_TOLERANCE and best_level is not None:
            return _LEVEL_TO_CHAIN[best_level]
        return 'UNKNOWN'

    def zone(self, price: float) -> str:
        """현재 가격 구간을 core_logic_distilled.md 3-C 기준으로 반환."""
        r = self.retrace_ratio(price)
        if r <= 0:
            return 'ABOVE_SWING'
        if r <= 0.13:
            return 'HOLD_STRONG'      # 0.114 RANGE
        if r <= 0.26:
            return 'HOLD'             # 0.236 RANGE
        if r <= 0.42:
            return 'BUY_ZONE'         # 0.382 황금 진입
        if r <= 0.55:
            return 'PIVOT_ZONE'       # 0.500 핵심 분기점
        if r <= 0.67:
            return 'LAST_BUY_CHANCE'  # 0.618 마지노선
        if r <= 0.82:
            return 'REDUCE_50PCT'     # 0.786 추세 약화
        if r <= 1.00:
            return 'EXIT_ZONE'        # 0.886+
        return 'BELOW_SWING'

    def is_buy_zone(self, price: float) -> bool:
        return self.zone(price) in ('BUY_ZONE', 'LAST_BUY_CHANCE')

    def extension_target(self, chain: str) -> Optional[float]:
        """체인별 확장 목표가 반환."""
        if chain not in _FIB_CHAIN:
            return None
        e = _FIB_CHAIN[chain]['target']
        return round(self.low + self._range * e, 2)

    # ── 스윙 자동 감지 (staticmethod) ────────────────────────────────────────

    @staticmethod
    def detect_swing(df: pd.DataFrame, lookback: int = 50) -> tuple[float, float]:
        """DataFrame 마지막 lookback개 캔들에서 스윙 고/저 탐지.

        Returns:
            (swing_high, swing_low) — 둘 다 양수인 경우만 반환.
            데이터 부족 시 (0.0, 0.0).
        """
        if df is None or len(df) < 10:
            return 0.0, 0.0
        tail = df.tail(lookback)
        try:
            h = float(tail['high'].max())
            l = float(tail['low'].min())
            return (h, l) if h > l > 0 else (0.0, 0.0)
        except Exception:
            return 0.0, 0.0


# ============================================================================
# PatternRecognizer
# ============================================================================

class PatternRecognizer:
    """OHLCV DataFrame → L3 패턴 신호 리스트 생성기.

    사용법:
        pr = PatternRecognizer(df, timeframe='minute60')
        signals = pr.evaluate()
        # signals: list[PatternSignal], strength 내림차순 정렬
    """

    def __init__(self, df: pd.DataFrame, timeframe: str = 'minute60'):
        # pyupbit get_ohlcv는 현재 진행 중인 불완전 캔들을 마지막 행으로 포함.
        # 패턴 분석에는 완성된 캔들만 사용한다.
        self.df = df.iloc[:-1].copy() if df is not None and len(df) > 1 else df
        self.timeframe = timeframe
        self._tf_label = '4h' if '240' in timeframe or '4h' in timeframe else (
            '1d' if 'day' in timeframe else '1h'
        )
        self.fib: Optional[FibonacciManager] = None
        self._ok = self._precompute()

    # ── 초기화 ───────────────────────────────────────────────────────────────

    def _precompute(self) -> bool:
        """공통 통계 캐시. 최소 25개 캔들 필요."""
        if self.df is None or len(self.df) < 25:
            return False
        df = self.df
        try:
            self._closes  = df['close'].to_numpy(dtype=float)
            self._opens   = df['open'].to_numpy(dtype=float)
            self._highs   = df['high'].to_numpy(dtype=float)
            self._lows    = df['low'].to_numpy(dtype=float)
            self._volumes = df['volume'].to_numpy(dtype=float)

            # 평균 몸통 (abs(close - open)), N=20
            bodies = np.abs(self._closes - self._opens)
            self._avg_body   = float(np.mean(bodies[-_DRAGON_LOOKBACK:]))
            self._avg_volume = float(np.mean(self._volumes[-_DRAGON_LOOKBACK:]))
            ranges = self._highs - self._lows
            self._avg_range  = float(np.mean(ranges[-_CRAWL_LOOKBACK:]))

            # ATR14
            prev_c = np.roll(self._closes, 1)
            prev_c[0] = self._closes[0]
            tr = np.maximum(
                self._highs - self._lows,
                np.maximum(
                    np.abs(self._highs - prev_c),
                    np.abs(self._lows  - prev_c),
                )
            )
            self._atr14 = float(np.mean(tr[-14:]))

            # RSI14 (숨은 다이버전스용)
            self._rsi_series = self._calc_rsi(self._closes, _HDIV_RSI_PERIOD)

            # 피보나치 스윙
            h, l = FibonacciManager.detect_swing(self.df, lookback=50)
            if h > l > 0:
                self.fib = FibonacciManager(h, l)

            return True
        except Exception as e:
            logger.debug('[PatternRecognizer] precompute 실패: %s', e)
            return False

    @staticmethod
    def _calc_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
        delta = np.diff(closes, prepend=closes[0])
        gain  = np.where(delta > 0, delta, 0.0)
        loss  = np.where(delta < 0, -delta, 0.0)
        avg_gain = np.convolve(gain, np.ones(period) / period, mode='full')[:len(closes)]
        avg_loss = np.convolve(loss, np.ones(period) / period, mode='full')[:len(closes)]
        with np.errstate(divide='ignore', invalid='ignore'):
            rs = np.where(avg_loss != 0, avg_gain / avg_loss, 100.0)
        return 100.0 - (100.0 / (1.0 + rs))

    # ── 통합 평가 ────────────────────────────────────────────────────────────

    def evaluate(self) -> list[PatternSignal]:
        """모든 패턴을 평가하고 strength 내림차순으로 정렬된 신호 리스트 반환."""
        if not self._ok:
            return []

        detectors = [
            self._crawl,
            self._dragon,
            self._abandonment,
            self._loyalty,
            self._taiji,
            self._hidden_div,
            self._siege,
        ]
        results: list[PatternSignal] = []
        for fn in detectors:
            try:
                sig = fn()
                if sig is not None:
                    results.append(sig)
            except Exception as e:
                logger.debug('[PatternRecognizer] %s 실패: %s', fn.__name__, e)

        results.sort(key=lambda s: s.strength, reverse=True)
        return results

    # ========================================================================
    # TS-1  꾸물꾸물 (Sluggish Crawl)  — E-07
    # ========================================================================

    def _crawl(self) -> Optional[PatternSignal]:
        """
        조건:
          VR  = range_t / avg_range(20) < 0.70
          HH  = window(5)캔들 중 HH ≥ 3
          Slope = (H_t - H_{t-w}) / (avg_H_w × w) × 100  ∈ [0.05, 0.30]
        """
        N = _CRAWL_LOOKBACK
        w = _CRAWL_WINDOW
        if len(self._closes) < N + w:
            return None

        highs   = self._highs
        ranges  = self._highs - self._lows
        avg_r   = float(np.mean(ranges[-N:]))
        if avg_r == 0:
            return None

        vr = ranges[-1] / avg_r
        if vr >= _CRAWL_VOL_RATIO_MAX:
            return None

        # HH count in last w candles
        hh_count = int(sum(
            1 for i in range(-w, -1) if highs[i + 1] > highs[i]
        ))
        if hh_count < _CRAWL_MIN_HH:
            return None

        # 정규화 기울기
        avg_h  = float(np.mean(highs[-w:]))
        if avg_h == 0:
            return None
        slope_pct = (highs[-1] - highs[-w]) / (avg_h * w) * 100

        if not (_CRAWL_SLOPE_MIN_PCT <= slope_pct <= _CRAWL_SLOPE_MAX_PCT):
            return None

        # strength: 기울기가 중간값(0.175)에 가까울수록 1.0
        slope_center = (_CRAWL_SLOPE_MIN_PCT + _CRAWL_SLOPE_MAX_PCT) / 2
        slope_norm = 1.0 - abs(slope_pct - slope_center) / slope_center
        strength = round(
            (1 - vr / _CRAWL_VOL_RATIO_MAX) * 0.5
            + (hh_count / w) * 0.3
            + max(0.0, slope_norm) * 0.2,
            3,
        )

        return PatternSignal(
            pattern_id='E-07',
            label='CRAWL_BUY',
            signal='buy',
            strength=min(1.0, strength),
            timeframe=self._tf_label,
            meta={
                'vol_ratio':  round(vr, 3),
                'hh_count':   hh_count,
                'slope_pct':  round(slope_pct, 4),
            },
        )

    # ========================================================================
    # TS-2  용틀임 (Dragon Breakout Candle)  — E-02, E-06
    # ========================================================================

    def _dragon(self) -> Optional[PatternSignal]:
        """
        양봉 한정:
          BodyRatio  = body / avg_body(20)
          ShadowRatio= (H - C) / body  ≤ 0.30
          VolRatio   = V / avg_vol(20) ≥ 1.5
        STRONG: BodyRatio ≥ 3.0 AND shadow ≤ 0.30 AND vol ≥ 1.5
        WEAK  : BodyRatio ≥ 2.0
        BEAR  : 음봉 용틀임(X-06) → signal='sell'
        """
        o, h, c, v = self._opens[-1], self._highs[-1], self._closes[-1], self._volumes[-1]

        body = abs(c - o)
        if self._avg_body == 0 or body == 0:
            return None

        body_ratio   = body / self._avg_body
        vol_ratio    = v / self._avg_volume if self._avg_volume > 0 else 0.0
        upper_shadow = h - max(c, o)
        shadow_ratio = upper_shadow / body

        is_bull = c > o
        is_bear = c < o

        # 음봉 용틀임 (장대음봉 + 거래량) → sell 신호 (X-06)
        if is_bear and body_ratio >= _DRAGON_BODY_STRONG and vol_ratio >= _DRAGON_VOL_MIN:
            lower_shadow = min(c, o) - self._lows[-1]
            bear_shadow_ratio = lower_shadow / body
            if bear_shadow_ratio <= _DRAGON_SHADOW_MAX:
                return PatternSignal(
                    pattern_id='X-06',
                    label='DRAGON_BEAR_STRONG',
                    signal='sell',
                    strength=round(min(1.0, (body_ratio / _DRAGON_BODY_STRONG) * 0.6 + (vol_ratio / 3.0) * 0.4), 3),
                    timeframe=self._tf_label,
                    meta={'body_ratio': round(body_ratio, 2), 'vol_ratio': round(vol_ratio, 2)},
                )

        if not is_bull:
            return None

        # ATR 보조 확인
        atr_ratio = (h - self._lows[-1]) / self._atr14 if self._atr14 > 0 else 0.0

        if body_ratio >= _DRAGON_BODY_STRONG and shadow_ratio <= _DRAGON_SHADOW_MAX and vol_ratio >= _DRAGON_VOL_MIN:
            label    = 'DRAGON_STRONG'
            pid      = 'E-06'
            strength = round(min(1.0,
                (body_ratio / (_DRAGON_BODY_STRONG * 1.5)) * 0.4
                + (1 - shadow_ratio / _DRAGON_SHADOW_MAX) * 0.3
                + (vol_ratio / 3.0) * 0.3
            ), 3)
        elif body_ratio >= _DRAGON_BODY_WEAK:
            label    = 'DRAGON_WEAK'
            pid      = 'E-06'
            strength = round(min(0.6, body_ratio / (_DRAGON_BODY_STRONG * 1.5)), 3)
        else:
            return None

        return PatternSignal(
            pattern_id=pid,
            label=label,
            signal='buy',
            strength=strength,
            timeframe=self._tf_label,
            meta={
                'body_ratio':   round(body_ratio, 2),
                'shadow_ratio': round(shadow_ratio, 3),
                'vol_ratio':    round(vol_ratio, 2),
                'atr_ratio':    round(atr_ratio, 2),
            },
        )

    # ========================================================================
    # TS-3  배째기 (Abandonment Decline)  — X-08
    # ========================================================================

    def _abandonment(self) -> Optional[PatternSignal]:
        """
        연속 n캔들 (n-1개 이상) 동시 충족:
          VolRatio_i = V_i / avg_vol(20) ≤ 0.50
          Drop_i     = (C_{i-1} - C_i) / C_{i-1}  ∈ (0, 0.03]
          패닉 즉시 구분: Drop > 0.05 AND VolRatio > 1.5 → PANIC_SELL
          누적 하락: (C_{t-n} - C_t) / C_{t-n} ≥ 0.05
        """
        n  = _ABN_MIN_CONSEC
        lb = _ABN_LOOKBACK
        if len(self._closes) < n + lb:
            return None

        avg_vol = float(np.mean(self._volumes[-lb:]))
        if avg_vol == 0:
            return None

        low_vol_count   = 0
        small_drop_count = 0

        for i in range(-n, 0):
            vr   = self._volumes[i] / avg_vol
            drop = ((self._closes[i - 1] - self._closes[i]) / self._closes[i - 1]
                    if self._closes[i - 1] > 0 else 0.0)

            if drop > _ABN_PANIC_THRESH and vr > 1.5:
                return None  # PANIC_SELL — 배째기 아님

            if vr <= _ABN_VOL_MAX:
                low_vol_count += 1
            if 0 < drop <= _ABN_DROP_MAX:
                small_drop_count += 1

        cumul_drop = ((self._closes[-n] - self._closes[-1]) / self._closes[-n]
                      if self._closes[-n] > 0 else 0.0)

        min_c = n - 1
        if not (low_vol_count >= min_c and small_drop_count >= min_c
                and cumul_drop >= _ABN_CUMUL_MIN):
            return None

        avg_vr = sum(
            self._volumes[i] / avg_vol for i in range(-n, 0)
        ) / n

        strength = round(min(1.0,
            (1 - avg_vr / _ABN_VOL_MAX) * 0.5
            + (cumul_drop / 0.10) * 0.5
        ), 3)

        return PatternSignal(
            pattern_id='X-08',
            label='ABANDONMENT_DECLINE',
            signal='sell',
            strength=strength,
            timeframe=self._tf_label,
            meta={
                'low_vol_count':    low_vol_count,
                'small_drop_count': small_drop_count,
                'cumul_drop_pct':   round(cumul_drop * 100, 2),
                'avg_vol_ratio':    round(avg_vr, 3),
            },
        )

    # ========================================================================
    # TS-4  충성심 테스트 (Loyalty Test / Retest)  — E-03, E-10, X-02
    # ========================================================================

    def _loyalty(self) -> Optional[PatternSignal]:
        """피보나치 0.382/0.618 연동 지지 확인 패턴.

        breakout_high: 최근 50캔들 고점 (스윙 고점)
        current_low  : 최근 저점 (되돌림 최저가)
        recovery_vol_ratio: 마지막 캔들 거래량 / avg_vol

        PASS_STRONG: drawdown ≤ 0.382 AND vol_ok
        PASS_NORMAL: drawdown ≤ 0.618
        WATCH      : 0.618~0.50 경계
        FAIL       : drawdown > 0.50
        """
        if self.fib is None:
            return None

        breakout_high = self.fib.high
        if breakout_high <= 0:
            return None

        # 되돌림 최저점: 최근 10캔들 저점
        current_low = float(np.min(self._lows[-10:]))
        drawdown = (breakout_high - current_low) / breakout_high

        if drawdown < _LYL_MIN_DD:
            return None  # 유효 테스트 미시작

        if drawdown > _LYL_FAIL_THRESH:
            return PatternSignal(
                pattern_id='X-02',
                label='LOYALTY_FAIL',
                signal='sell',
                strength=round(min(1.0, drawdown / 0.70), 3),
                timeframe=self._tf_label,
                meta={'drawdown_pct': round(drawdown * 100, 2)},
            )

        vol_ratio = self._volumes[-1] / self._avg_volume if self._avg_volume > 0 else 0.0
        vol_ok    = vol_ratio >= _LYL_VOL_MIN

        # 회복 여부: 마지막 캔들이 직전 저점 대비 상승 반전 중인지
        recovering = self._closes[-1] > self._closes[-2]

        if drawdown <= _LYL_FIB_STRONG and vol_ok and recovering:
            label, pid, sig = 'LOYALTY_PASS_STRONG', 'E-03', 'buy'
            strength = round(min(1.0,
                (1 - drawdown / _LYL_FIB_STRONG) * 0.5
                + (vol_ratio / 2.0) * 0.3
                + 0.2
            ), 3)
        elif drawdown <= _LYL_FIB_LIMIT:
            label, pid, sig = 'LOYALTY_PASS_NORMAL', 'E-10', 'buy'
            strength = round(min(0.7,
                (1 - drawdown / _LYL_FIB_LIMIT) * 0.5
                + (vol_ratio / 2.0) * 0.2
            ), 3)
        else:
            label, pid, sig = 'LOYALTY_WATCH', 'E-10', 'watch'
            strength = 0.3

        return PatternSignal(
            pattern_id=pid,
            label=label,
            signal=sig,
            strength=strength,
            timeframe=self._tf_label,
            meta={
                'drawdown_pct': round(drawdown * 100, 2),
                'vol_ratio':    round(vol_ratio, 2),
                'fib_zone':     self.fib.zone(current_low),
                'recovering':   recovering,
            },
        )

    # ========================================================================
    # TS-5  태극문양 (Taiji Trend Reversal)  — 3-E, Section 12
    # ========================================================================
    #
    #  [음양 전환 로직]
    #  하락 추세 → 저점 갱신 시도 → 실패(꼬리만 하방) → 종가 회복 + 중앙선 돌파
    #  ┌──────────────────────────────────────────────────────┐
    #  │  center_N = (max(H[-N:]) + min(L[-N:])) / 2         │
    #  │  attempted_break = L_t < min(L[-N:-1])              │  ← 음 에너지 시도
    #  │  close_recovery  = C_t > min(L[-N:-1])              │  ← 양 에너지 반격
    #  │  center_break    = C_t > center_N                   │  ← 음양 전환 확정
    #  │                                                      │
    #  │  BIG_BOUNCE = attempted_break ∧ close_recovery      │
    #  │               ∧ center_break                        │
    #  └──────────────────────────────────────────────────────┘
    #  강도 가중치:
    #    wick_depth = (prior_low - L_t) / C_t ≥ 0.005  → +1점
    #    vol_ratio  = V_t / avg_vol           ≥ 1.2    → +1점
    #    최대 3점 → strength = score / 3
    # ========================================================================

    def _taiji(self) -> Optional[PatternSignal]:
        N = _TAIJI_LOOKBACK
        if len(self._closes) < N + 1:
            return None

        H_slice = self._highs[-N:]
        L_slice = self._lows[-N:]

        # 중앙선 (N봉 고저 범위의 중심)
        recent_high  = float(np.max(H_slice))
        recent_low   = float(np.min(L_slice))
        center_N     = (recent_high + recent_low) / 2.0

        # 직전 저점 (현재 봉 제외)
        prior_low    = float(np.min(self._lows[-N:-1]))

        L_t = self._lows[-1]
        C_t = self._closes[-1]
        V_t = self._volumes[-1]

        # 세 가지 조건
        attempted_break = L_t < prior_low       # 꼬리가 이전 저점 하향 돌파
        close_recovery  = C_t > prior_low        # 종가는 이전 저점 위에서 회복 (음봉 탈출)
        center_break    = C_t > center_N         # 종가가 N봉 중앙선 위 (음양 전환)

        if not (attempted_break and close_recovery and center_break):
            return None

        # 강도 점수
        score = 1  # 기본: 3개 조건 모두 충족

        wick_depth = (prior_low - L_t) / C_t if C_t > 0 else 0.0
        if wick_depth >= _TAIJI_WICK_MIN_PCT:
            score += 1  # 꼬리 깊이 충분 → 음 에너지 강도 증명

        vol_ratio = V_t / self._avg_volume if self._avg_volume > 0 else 0.0
        if vol_ratio >= _TAIJI_VOL_CONFIRM:
            score += 1  # 반전 캔들 거래량 확인 → 양 에너지 진입 확인

        strength = round(score / 3.0, 3)

        # 피보나치 위치 교차 확인 (BUY ZONE이면 신뢰도 추가)
        fib_zone = self.fib.zone(C_t) if self.fib else 'UNKNOWN'

        return PatternSignal(
            pattern_id='3-E',
            label='TAIJI_BIG_BOUNCE',
            signal='buy',
            strength=strength,
            timeframe=self._tf_label,
            meta={
                'center_N':      round(center_N, 2),
                'prior_low':     round(prior_low, 2),
                'wick_depth_pct': round(wick_depth * 100, 3),
                'vol_ratio':     round(vol_ratio, 2),
                'score':         score,
                'fib_zone':      fib_zone,
            },
        )

    # ========================================================================
    # TS-6  숨은 다이버전스 (Hidden Bullish Divergence)  — E-15, Section 30
    # ========================================================================

    def _hidden_div(self) -> Optional[PatternSignal]:
        """가격 고점 상승 + RSI 고점 하락 동시 = 숨은 강세 다이버전스.

        탐지: 최근 lookback 내에서 두 저점을 찾아 비교.
          price_t-k < price_t (가격 저점 상승)
          RSI_t-k   > RSI_t   (RSI 저점 하락)
        """
        N = _HDIV_LOOKBACK
        gap = _HDIV_MIN_GAP
        if len(self._closes) < N or self._rsi_series is None or len(self._rsi_series) < N:
            return None

        closes = self._closes[-N:]
        rsi    = self._rsi_series[-N:]

        # 최근 저점 (기준): 마지막 5캔들 중 최저
        curr_low_idx = int(np.argmin(closes[-5:])) + (N - 5)
        curr_low     = closes[curr_low_idx]
        curr_rsi     = rsi[curr_low_idx]

        # 이전 저점 탐색: curr_low_idx - gap 이전 구간
        search_end = curr_low_idx - gap
        if search_end < 5:
            return None

        prev_low_idx = int(np.argmin(closes[:search_end]))
        prev_low     = closes[prev_low_idx]
        prev_rsi     = rsi[prev_low_idx]

        # 숨은 강세 다이버전스 조건
        price_higher_low = curr_low > prev_low    # 가격 저점 상승 (추세 강화)
        rsi_lower_low    = curr_rsi < prev_rsi    # RSI 저점 하락 (모멘텀 약화처럼 보임)

        if not (price_higher_low and rsi_lower_low):
            return None

        # 강도: RSI 차이와 가격 차이 비율로 계산
        rsi_div    = float(prev_rsi - curr_rsi)
        price_rise = float((curr_low - prev_low) / prev_low) if prev_low > 0 else 0.0
        strength   = round(min(1.0, (rsi_div / 30.0) * 0.6 + (price_rise * 10) * 0.4), 3)

        return PatternSignal(
            pattern_id='E-15',
            label='HIDDEN_BULL_DIV',
            signal='buy',
            strength=max(0.3, strength),
            timeframe=self._tf_label,
            meta={
                'curr_low':      round(float(curr_low), 2),
                'prev_low':      round(float(prev_low), 2),
                'curr_rsi':      round(float(curr_rsi), 1),
                'prev_rsi':      round(float(prev_rsi), 1),
                'rsi_divergence': round(rsi_div, 1),
                'candles_apart':  curr_low_idx - prev_low_idx,
            },
        )

    # ========================================================================
    # TS-7  매물대 포위전 (Siege Timing)  — E-19, Section 15
    # ========================================================================

    def _siege(self) -> Optional[PatternSignal]:
        """가격이 좁은 구간에 7일+ 횡보 + 거래량 감소 추세 = 매수세 시간 끌기 승리.

        zone_pct  = ±3% 이내 = "같은 구간"
        min_candles: 4h봉 42개(7일), 1d봉 7개
        VolSlope  = (avg_vol[-3:] - avg_vol[-7:-3]) / avg_vol[-7:-3] < 0
        """
        is_4h = 'minute240' in self.timeframe or '4h' in self.timeframe
        is_1h = 'minute60' in self.timeframe or '1h' in self.timeframe
        if is_4h:
            min_c = _SIEGE_MIN_CANDLES_4H
        elif is_1h:
            min_c = _SIEGE_MIN_CANDLES_1H
        else:
            min_c = _SIEGE_MIN_CANDLES_1D
        lb    = max(min_c + 5, _CRAWL_LOOKBACK)

        if len(self._closes) < lb:
            return None

        seg     = self._closes[-min_c:]
        seg_mid = float(np.mean(seg))
        if seg_mid == 0:
            return None

        # 전체 구간이 ±3% 이내인지 확인
        seg_max  = float(np.max(seg))
        seg_min  = float(np.min(seg))
        zone_spread = (seg_max - seg_min) / seg_mid
        if zone_spread > _SIEGE_ZONE_PCT * 2:
            return None  # 구간이 너무 넓음 → 횡보 아님

        # 거래량 감소 추세
        vols  = self._volumes[-min_c:]
        mid   = len(vols) // 2
        avg_early = float(np.mean(vols[:mid])) if mid > 0 else 1.0
        avg_late  = float(np.mean(vols[mid:]))
        if avg_early == 0:
            return None

        vol_slope = (avg_late - avg_early) / avg_early
        if vol_slope >= 0:
            return None  # 거래량 증가 → 포위전 아님

        # 강도: 횡보 기간이 길고, 거래량 감소가 클수록 강함
        days_ratio  = min(1.0, len(seg) / (min_c * 1.5))
        vol_shrink  = min(1.0, abs(vol_slope))
        strength    = round(days_ratio * 0.5 + vol_shrink * 0.5, 3)

        return PatternSignal(
            pattern_id='E-19',
            label='SIEGE_BUY_FAVORABLE',
            signal='buy',
            strength=strength,
            timeframe=self._tf_label,
            meta={
                'zone_spread_pct': round(zone_spread * 100, 2),
                'candles_in_zone': len(seg),
                'vol_slope_pct':   round(vol_slope * 100, 2),
                'seg_mid_price':   round(seg_mid, 2),
            },
        )
