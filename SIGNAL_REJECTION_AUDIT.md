# SIGNAL_REJECTION_AUDIT.md
**분석 기준일: 2026-05-18 (KST)**
**분석 기간: 최근 30일 (실질적 데이터: 2026-05-16~18)**

---

## 데이터 현황 요약

| 지표 | 수치 |
|---|---|
| analysis_results 총 레코드 (유효기간 내) | 45,229건 |
| hold 비율 | 44,840건 (99.1%) |
| sell 신호 | 382건 (0.8%) |
| buy 신호 | 7건 (0.015%) |
| EXECUTE 이벤트 (30d) | 3건 (매수 1, 매도 2) |

> analysis_results의 hold 레코드는 db_maintenance가 7일 초과 시 삭제하므로, 실제 누적 기간은 2026-05-16~18 3일분 데이터임. buy/sell은 30일 보존.

---

## A. 필터 단계별 차단 통계

### A-1. L1 글로벌 필터 (MarketGuardian)

현재 상태: **L1 PASS — 완전 통과** (no_trade=False, sell_blocked=False)

**중요 이벤트: 2026-05-17 18:08~23:33 KST — 5시간 25분 전면 매수 차단**

```
[Guardian] 데이터 미비/Stale — ['MACRO_DATA_MISSING', 'DOMINANCE_DATA_MISSING']
🚨 매수 전면 차단: 이번 사이클은 매도(Sell)만 수행합니다. (regime=UNKNOWN)
```

- 원인: Guardian 기능이 처음 활성화된 시점에 MacroSnapshot / DominanceSnapshot 데이터가 아직 수집되지 않았음
- 해소: 2026-05-17 23:34 KST에 BULL_EARLY로 전환
- 그 이전 기간(~05/17 18:07): Guardian 로그 없음 → guardian_result=None fallback 동작 → is_global_bull_market=True 기본값 유지

**Guardian 도입 전(~05/17 18:07) 매수 차단 여부**  
Guardian 미활성 상태에서는 is_global_bull_market=True 기본값이 적용됨. L1에 의한 차단은 없었으나, L3 strategy 조건이 매수를 막고 있었음 (→ A-3 참조).

---

### A-2. L2 장세 분류 (현재 상태)

```
L2 regime=BULL_EARLY | cap=50% | new_entry=True | alt_block=False | size_mult=1.0
```

- **BULL_EARLY**: 신규 진입 허용, 포지션 상한 50%, 사이즈 배수 1.0x
- 5/16~17 동안 Guardian 이전: regime 판단 없음 (기본값 적용)

**개별 티커 regime 분포 (analysis_results 30일)**

| regime | 건수 | 비율 |
|---|---|---|
| transition | 17,569 | 38.8% |
| trend | 11,122 | 24.6% |
| weakening_trend | 11,045 | 24.4% |
| range | 5,493 | 12.1% |

trend(24.6%)와 range(12.1%)에서만 buy 조건 충족 가능. weakening_trend 24.4%는 buy 시그널 미발생.

---

### A-3. L3 전략 신호 생성 (strategy.py / analyze_ticker)

buy 신호 발생 조건:
- trend 장세: EMA 골든크로스 (단기 > 장기) + RSI 40~75 + 거래량 ≥ 0.8x
- range 장세: BB 하단 터치 + SmartVol 조건
- transition 장세: Transition EMA 골든크로스 + 거래량 조건

**최근 7일 buy 신호**: 7건 (전부 2026-05-18 — BULL_EARLY 전환 이후)  
**2026-05-16 ~ 05/17 23:33**: buy 신호 0건

이 기간(29,075건 분석)에서 buy가 0건인 이유:
1. EMA 단기 > 장기 조건 미충족 (BTC 횡보/약세 시장 지속)
2. RSI 40~75 범위 내 진입하는 티커 극소수
3. Guardian UNKNOWN 직전까지는 strategy 조건 자체가 차단

---

### A-4. DYN_THR 차단 (PatternRecognizer 강도 게이트)

**오늘(2026-05-18) buy 신호 7건 중 DYN_THR 차단 현황**

| 시간 | 티커 | str (강도) | thr (임계값) | 결과 |
|---|---|---|---|---|
| 03:01 | KRW-CVC | N/A | — | RSI_OVERBUY (71.7) → 차단 |
| 07:01 | KRW-XRP | 0.38 | 0.85 | DYN_THR → 차단 |
| 07:01 | KRW-VIRTUAL | 0.38 | 0.85 | DYN_THR → 차단 |
| 08:01 | KRW-DOGE | 0.00 | 0.85 | DYN_THR → 차단 |
| 09:01 | KRW-ORCA | 0.40 | 0.85 | DYN_THR → 차단 |
| 09:01 | KRW-ZBT | 0.45 | 0.85 | DYN_THR → 차단 |
| 11:01 | KRW-CFG | 0.44 | 0.85 | DYN_THR → 차단 |
| 14:10 | KRW-HYPER | — | — | **통과 → EXECUTE** |

- 7건 중 6건이 DYN_THR에서 차단 (1건은 RSI_OVERBUY)
- str 분포: 0.0 ~ 0.45 → threshold 0.85의 절반 수준
- 연속 손실 streak=0 → DYN_THR는 기본값 0.85 그대로 (패널티 없음)

---

### A-5. MAX_OPEN_POSITIONS 차단

2026-03-13: `[SKIP] MAX_OPEN_POSITIONS reached (6)` 2건 확인  
현재: 오픈 포지션 0건 (HYPER 완전 청산됨)  
최근 7일: MAX_OPEN 차단 없음

---

### A-6. 기타 cycle-level 차단

| 유형 | 최근 7일 발생 |
|---|---|
| BUY_COOLDOWN (60분) | 로그에서 확인 불가 |
| CASH_RULE | 로그에서 확인 불가 |
| 중복매수방지 | 로그에서 확인 불가 |
| Circuit Breaker | 0회 (system_state 미설정) |

---

## B. 현재 장세 판단 확인

**MarketGuardian 현재 출력**
```
L1: PASS (글로벌 매수 허용)
L2: regime=BULL_EARLY, cap=50%, new_entry=True, alt_block=False, size_mult=1.0
```

**ENABLE_AUTO_TRADING / ENABLE_AUTO_LIVE 확인**
```
ENABLE_AUTO_TRADING = 1  ✅ 활성
ENABLE_AUTO_LIVE = 1     ✅ 활성
TRADING_MODE = live      ✅ 실거래 모드
```

**MacroSnapshot 최신값**
- ratio_quality: stale (2026-05-18 07:00 수집)
- crisis_level: NORMAL
- nasdaq_1d_pct: -1.54% (전일 나스닥 소폭 하락)
- dxy_1d_pct: +0.39%

> MacroSnapshot의 `ratio_quality=stale`이 Guardian을 다시 UNKNOWN으로 전환할 가능성 있음. collectors가 오늘 정상 실행됐는지 확인 필요.

**분석 대상 티커**
- 설정: 60개 KRW 마켓
- known_delisted_tickers: 5개 제외 (KRW-PCI, KRW-APENFT, KRW-ETHW, KRW-ETHF, KRW-SPACE)
- 실제 분석 대상: 55~60개 (사이클마다 동적)

---

## C. 최근 매수 1건 상세

**KRW-HYPER 트레이드 (2026-05-18)**

| 구분 | 내용 |
|---|---|
| 매수 시각 | 2026-05-18 14:10 KST |
| 매수 가격 | 166원 |
| 매수 사유 | Pass2 ADX순 매수 (transition 장세, ADX=14.2, RSI=63.2) |
| 1차 매도 | 18:01 KST / 183원 / ROI +10.24% (Scale-Out ATR 2.0x, RSI 84.8 과매수) |
| 2차 매도 | 20:01 KST / 176원 / ROI +6.02% (ATR Trailing Stop, 최고가 190 - ATR×2.0) |
| 최종 상태 | **포지션 완전 청산 (미실현 손익 없음)** |
| 예상 실현 이익 | 약 3,210원 (포지션 크기 기준 근사) |

---

## 종합 판단: 차단이 과도한가, 적절한가?

### 1. L1/L2: 적절

현재 Guardian은 BULL_EARLY → 정상 작동. 이전 UNKNOWN 5.5시간 차단은 Guardian 첫 가동 시 데이터 미비에 의한 일회성 이벤트로 판단. MacroSnapshot ratio_quality=stale이 지속될 경우 재발 가능성 있음.

### 2. L3 strategy 필터: 적절 (단 시장 조건 의존성 높음)

EMA 골든크로스 + RSI 40~75 조건이 엄격하지만, 이는 설계 의도. 5월 16~17일 buy=0건은 시장 조건(개별 알트코인들이 EMA 조건을 충족하지 못한 상태) 반영. BULL_EARLY 전환 직후 바로 7건 발생한 것은 전략이 정상 작동한다는 증거.

### 3. DYN_THR 0.85: **과도하게 엄격 — 조정 검토 필요**

가장 큰 문제. 오늘 buy 신호 7건 중 6건이 DYN_THR에서 차단. 관측된 str 값은 0.0~0.45로 임계값의 절반 수준.

**근거:**
- PatternRecognizer가 지속적으로 str=0.3~0.5 수준 신호만 생성하는 상황에서 0.85 threshold는 사실상 "매수 불가" 상태와 동일
- BULL_EARLY 장세 진입 후에도 조건이 충족되지 않아 기회 손실
- KRW-XRP(0.38), KRW-VIRTUAL(0.38), KRW-ZBT(0.45), KRW-ORCA(0.40), KRW-CFG(0.44) 모두 전략 조건을 통과한 종목들임에도 차단

**제안:**
- BULL_EARLY 장세에서 DYN_THR을 0.65~0.70으로 완화하거나
- GuardianResult.regime이 BULL_EARLY일 때 DYN_THR을 별도 값으로 적용하는 파라미터 추가

### 4. UNKNOWN 재발 위험

MacroSnapshot의 `ratio_quality=stale` 상태 지속 중. collectors(macro/dominance) 정상 실행 여부를 점검해야 함. stale 상태가 Guardian threshold를 초과하면 다시 UNKNOWN → 전면 차단으로 돌아갈 수 있음.

---

**결론:** L1/L2/L3(전략)는 정상. **DYN_THR 0.85가 현재 시장에서 주된 매수 차단 요인**이며 BULL_EARLY 장세 진입 시점에서 완화를 검토해야 한다. MacroSnapshot stale 상태에 대한 점검도 시급하다.
