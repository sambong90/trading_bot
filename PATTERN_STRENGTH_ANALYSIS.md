# PATTERN_STRENGTH_ANALYSIS.md
**분석 기준일: 2026-05-19**

---

## 1. `_max_strength` 계산 흐름

`auto_trader.py:496`:
```python
_max_strength = max((_ps.strength for _ps in _signals), default=0.0)
```

`_signals`는 `PatternRecognizer.evaluate()`가 반환하는 **전체 PatternSignal 리스트** (buy + sell 포함, strength 내림차순 정렬).

흐름 요약:
- `PatternRecognizer(df, timeframe='minute60').evaluate()` → 7개 패턴 함수 순차 실행
- 각 함수는 `PatternSignal(strength=...)` 또는 `None` 반환
- `_max_strength = max(전체 신호의 strength)` — buy/sell 구분 없이 단순 최댓값
- `_high_conviction = _max_strength >= 0.85 OR (DRAGON_STRONG AND LOYALTY_PASS_STRONG 동시)`
- DYN_THR 체크: `if signal == 'buy': if _max_strength < _dyn_thr: return SKIP`

**중요:** `_max_strength`는 패턴들이 상가적(additive)으로 합산되지 않는다. 여러 패턴이 동시에 감지돼도 가장 높은 단일 패턴 strength만 사용.

---

## 2. 패턴별 strength 범위 및 최대값

| 패턴 ID | 레이블 | 신호 | 이론 최대 | 하드캡 | 실전 범위 | 주요 요소 |
|---|---|---|---|---|---|---|
| E-07 | CRAWL_BUY | buy | 1.00 | 없음 | 0.4~0.8 | vr×0.5 + hh×0.3 + slope×0.2 |
| E-06 | DRAGON_STRONG | buy | 1.00 | 없음 | **미발생** | body≥3×avg + vol≥1.5×avg |
| E-06 | DRAGON_WEAK | buy | **0.60** | 하드캡 0.60 | **미발생** | body≥2×avg (vol 무관) |
| E-03 | LOYALTY_PASS_STRONG | buy | 0.935 | 없음 | **미발생** | dd≤0.382 + vol≥1.2 + recovering |
| E-10 | LOYALTY_PASS_NORMAL | buy | **0.70** | 하드캡 0.70 | 0.35~0.55 | dd≤0.618 |
| X-02 | LOYALTY_FAIL | sell | 1.00 | 없음 | 가변 | dd>0.50 |
| 3-E | TAIJI_BIG_BOUNCE | buy | 1.00 | 없음 | 0.33/0.67/1.00 | score/3 (정수 3단계만) |
| E-15 | HIDDEN_BULL_DIV | buy | 1.00 | floor 0.30 | 0.30~0.55 | rsi_div×0.6 + price_rise×0.4 |
| E-19 | SIEGE_BUY_FAVORABLE | buy | **0.833** | 구조적 0.833 | 0.43~0.64 | days_ratio×0.5 + vol_shrink×0.5 |
| X-08 | ABANDONMENT_DECLINE | sell | 1.00 | 없음 | 가변 | vol_shrink×0.5 + cumul_drop×0.5 |

### SIEGE 1h 타임프레임 구조적 상한 상세

```
_SIEGE_MIN_CANDLES_1D = 7   # 일봉 기준 상수
1h 타임프레임 → is_4h=False → min_c = 7 (7시간!)
days_ratio = min(1.0, seg_len / (min_c × 1.5)) = 7 / 10.5 = 0.667 (고정)
strength = 0.667×0.5 + vol_shrink×0.5 = 0.333 + vol_shrink×0.5
최대값 = 0.333 + 0.5 = 0.833
```

`_SIEGE_MIN_CANDLES_1D=7`은 일봉 7일 기준 상수인데, 1h 타임프레임에 그대로 적용되면서 "7시간 횡보"로 조건이 낮아짐. `days_ratio`가 항상 0.667로 고정돼 strength 상단이 0.833을 넘을 수 없음.

---

## 3. str=0.56 (오늘 07:01 KRW-TRX) 역추적

현재 시점 TRX를 직접 분석한 결과:
```
패턴 신호: 0건 → _max_strength = 0.0
```

07:01 시점의 str=0.56은 해당 캔들 마감 직후 상태에서 발생. 지금은 조건이 사라짐.

ZBT(str=0.55)의 실시간 분석으로 역추적:
```
[E-19] SIEGE_BUY_FAVORABLE | str=0.643
  zone_spread_pct=2.64, candles_in_zone=7, vol_slope_pct=-61.98

[E-10] LOYALTY_PASS_NORMAL  | str=0.413
  drawdown_pct=10.8, vol_ratio=0.01, fib_zone=EXIT_ZONE
```

TRX str=0.56도 SIEGE 단독 감지였을 가능성이 높음.

**0.60을 넘기려면 필요한 조건:**

SIEGE 단독으로는 최대 0.833이 이론 상한이지만, 현재 7캔들(7시간) 고정 구조에서 실제 달성 가능한 범위는 0.43~0.67.

0.60 돌파 가능한 경로:
- SIEGE: `vol_slope_pct ≤ -80%` (현재 -62%) → str ≈ 0.73
- TAIJI: score=2/3 → str=0.667, score=3/3 → str=1.000 (가장 확실한 경로)
- CRAWL: 꾸물꾸물 조건 + HH count=5 + slope 중앙값 → str ≈ 0.75
- DRAGON_STRONG/LOYALTY_PASS_STRONG: 아래 C 판단 참조

---

## 4. 이론적 최대 strength

이론 최대: **1.0** (CRAWL, DRAGON_STRONG, TAIJI, HIDDEN_DIV)

**실전에서 0.85 이상이 나오려면:**

- DRAGON_STRONG: 장대양봉(body≥3×avg) + 윗꼬리≤30% + 거래량≥1.5배 동시
  → 명확한 breakout 캔들 필요 (강세 추세 초입, 지지선 이탈 후 급반등)
- LOYALTY_PASS_STRONG: 스윙 고점 대비 3.8% 이하 되돌림 + vol≥1.2×avg + 종가 상승 반전
  → 0.382 피보나치 진입 구간에 bounce
- TAIJI 3/3: 이전 저점 하향 돌파 → 종가 회복 → N봉 중앙선 돌파 + 꼬리 + 거래량
  → False breakdown 패턴 완성 시점
- _high_conviction = DRAGON_STRONG AND LOYALTY_PASS_STRONG 동시: 0.85 기준 예외적 허용

현재 시장 조건(BULL_EARLY, FNG=28 Fear, ADX 12~23, range/transition):
breakout 캔들 없음 → DRAGON 미발생, fib_zone=EXIT_ZONE → LOYALTY 최적 구간 아님.

---

## 5. DYN_THR SKIP strength 히스토그램

**ai_debug.log 전체 이력 (15건):**

```
날짜        티커         str     thr
2026-05-18  KRW-XRP     0.38   0.85
2026-05-18  KRW-VIRTUAL 0.38   0.85
2026-05-18  KRW-DOGE    0.00   0.85
2026-05-18  KRW-ORCA    0.40   0.85
2026-05-18  KRW-ZBT     0.45   0.85
2026-05-18  KRW-CFG     0.44   0.85
2026-05-19  KRW-NEAR    0.51   0.85   ← old 코드
2026-05-19  KRW-FF      0.53   0.85   ← old 코드
2026-05-19  KRW-TRX     0.00   0.85   ← old 코드
2026-05-19  KRW-TRX     0.56   0.60   ← new 코드 (regime=BULL_EARLY)
2026-05-19  KRW-LINK    0.47   0.60
2026-05-19  KRW-ZBT     0.55   0.60
2026-05-19  KRW-TRX     0.00   0.60
2026-05-19  KRW-FF      0.40   0.60
2026-05-19  KRW-XRP     0.43   0.60
2026-05-19  KRW-TRX     0.00   0.60
```

**구간별 분포 (0.1 단위):**

```
0.0~0.1 : #### (4건)   ← 패턴 전혀 미감지
0.1~0.2 : (0건)
0.2~0.3 : (0건)
0.3~0.4 : (0건)
0.4~0.5 : ######## (7건)  ← SIEGE 단독, 낮은 vol_shrink
0.5~0.6 : ##### (5건)    ← SIEGE 단독, 높은 vol_shrink
0.6~    : (0건)           ← 0.60 돌파 없음
```

str=0.00 (4건): `evaluate()`가 빈 리스트 반환 — 25개 미만 캔들 또는 전 패턴 조건 미충족.
str=0.40~0.55 (12건): 대부분 SIEGE_BUY_FAVORABLE 단독 감지.
0.60 이상 없음: 14일간 단 한 번도 없음.

---

## 6. 종합 판단

### **판정: C) + B) 복합**

#### C) 특정 패턴이 구조적으로 작동하지 않음

**DRAGON_STRONG / LOYALTY_PASS_STRONG 사실상 불가:**

`PatternRecognizer`는 `df`의 마지막 캔들(`[-1]`)로 volume을 체크한다.
봇 스케줄러는 `HH:01:00`에 실행되며, pyupbit `get_ohlcv(minute60, count=N)`은
**현재 진행 중인 불완전 캔들(incomplete candle)을 마지막 행으로 포함**한다.

`HH:01` 시점의 마지막 1h 캔들 거래량 ≈ 1분치 → `vol_ratio ≈ 1/60 ≈ 0.016`.

결과:
- `DRAGON_STRONG`: vol_ratio 0.016 << 1.5 → **항상 실패**
- `LOYALTY_PASS_STRONG`: vol_ratio 0.016 << 1.2 → **항상 실패**
- `SIEGE`: `vol_slope`를 계산할 때 마지막 캔들(≈0 volume)이 후반부에 포함 → `vol_slope`가 인위적으로 크게 음수 → **SIEGE가 허위 발생**

실측: FF vol_ratio=0.03, ZBT vol_ratio=0.01 — 모두 1분치 불완전 캔들 거래량.

#### B) 계산 구조상 0.6 이상 나오기 어려움

`SIEGE` 패턴이 1h 타임프레임에서 `_SIEGE_MIN_CANDLES_1D=7` (일봉용 상수)을 사용.
`is_4h` 체크가 `minute60`을 커버하지 않아 7시간 횡보로 조건이 낮아짐.
`days_ratio = 7/10.5 = 0.667` 고정 → strength 상단 0.333 + 0.5 = **0.833에 묶임**.

SIEGE가 spurious하게 빈번히 감지되면서 str 0.40~0.55를 채우고,
강도 있는 패턴(DRAGON, TAIJI, CRAWL)이 genuine하게 감지될 때 억압 효과는 없지만
(max 구조이므로 상쇄하지 않음) 결국 SIEGE 이외 패턴이 발생하지 않는 상황.

---

## 수정 필요 사항 (우선순위 순)

**1순위 — Incomplete candle 제거 (즉시 효과)**

`PatternRecognizer.__init__`에서 `self.df`를 `df.iloc[:-1]`로 슬라이싱하거나,
`fetch_ohlcv` 호출 시 `count+1`을 받아 마지막 행을 제거.

효과: DRAGON/LOYALTY vol_ratio가 완성된 캔들 기준으로 계산 → genuine volume 신호 감지 가능.
SIEGE vol_slope 허위 음수도 해소.

**2순위 — SIEGE 1h 최소 캔들 수 추가**

```python
_SIEGE_MIN_CANDLES_1H = 168  # 7일 × 24h
# _siege() 내부:
is_1h = 'minute60' in self.timeframe or '1h' in self.timeframe
if is_1h:
    min_c = _SIEGE_MIN_CANDLES_1H
elif is_4h:
    min_c = _SIEGE_MIN_CANDLES_4H
else:
    min_c = _SIEGE_MIN_CANDLES_1D
```

효과: 1h 타임프레임에서 SIEGE는 168개 완성 캔들(7일치) 기준으로 판단.
`days_ratio`가 실제 기간 비율을 반영. 7시간 허위 발생 제거.

**3순위 — `_max_strength`를 buy 신호만으로 제한 (선택적)**

현재 sell 패턴(LOYALTY_FAIL, DRAGON_BEAR_STRONG)이 높은 strength를 가져도
`_max_strength`에 반영됨. buy 신호 판단에 sell 패턴 강도가 포함되는 것은 의미 없음.

```python
_buy_signals = [s for s in _signals if s.signal == 'buy']
_max_strength = max((_ps.strength for _ps in _buy_signals), default=0.0)
```
