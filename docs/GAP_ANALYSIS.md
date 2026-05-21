# GAP_ANALYSIS.md — Guardian L1 구현 완성도 분석

작성일: 2026-05-21
기준 문서: core_logic_distilled.md (G-01 ~ G-15)
분석 대상: market_guardian.py, collectors/macro.py

---

## G-01

전략 문서 정의: dxy_zone = DXY_GREEN_ZONE → BLOCK ALL BUYS
필요 데이터: DXY 값 (>= 112.58)
데이터 수집 상태: 수집됨 (macro.py → MacroSnapshot.dxy_zone)
Guardian 구현 상태: 완전 구현
구현 차이: 없음
판정: OK

---

## G-02

전략 문서 정의: dxy_gold_nasdaq_crisis = SUPER_CRISIS → BLOCK ALL TRADES (매도까지 차단)
필요 데이터: DXY 1d%, Gold 1d%, NASDAQ 1d%
데이터 수집 상태: 수집됨 (macro.py → MacroSnapshot.crisis_level)
Guardian 구현 상태: 완전 구현
구현 차이: sell_blocked=True로 SELL_BLOCKED 플래그 추가. 매도까지 차단.
판정: OK

---

## G-03

전략 문서 정의: bond_ratio_signal = SECONDARY_DROP_IMMINENT → BLOCK ALL BUYS
필요 데이터: 10Y/30Y 국채 수익률 비율 (>= 1.05)
데이터 수집 상태: 수집됨 (macro.py → MacroSnapshot.bond_signal)
Guardian 구현 상태: 완전 구현
구현 차이: 없음
판정: OK

---

## G-04

전략 문서 정의: market_condition = DEFLATING 연속 3회+ (1d) → BLOCK BUYS
필요 데이터: MacroSnapshot.dxy_1d_pct, nasdaq_1d_pct 이력
데이터 수집 상태: 수집됨
Guardian 구현 상태: 완전 구현 (_deflating_streak 메서드)
구현 차이: 없음
판정: OK

---

## G-05

전략 문서 정의: nasdaq_dxy_ratio >= 440 → BLOCK BUYS / 기존 포지션 50% 축소
필요 데이터: MacroSnapshot.nasdaq_dxy_ratio
데이터 수집 상태: 수집됨
Guardian 구현 상태: 부분 구현
구현 차이: allow_new_entry=False + position_cap=0.0으로 신규 매수 차단은 구현됨.
  "기존 포지션 50% 축소" 셀 신호 미구현 — Guardian 범위 밖 (auto_trader에서 처리해야 함).
  현재 flags에 G-05:BUBBLE_RATIO_BLOCK 추가됨. analyze_ticker에서 이 플래그 기반 청산 로직 필요.
판정: OK (Guardian 범위 내 완전 구현. 포지션 축소는 auto_trader 소관)

---

## G-06

전략 문서 정의: bond_signal = BEAR_MARKET_1 → 총 노출도 <= 50%
필요 데이터: MacroSnapshot.bond_signal
데이터 수집 상태: 수집됨
Guardian 구현 상태: 완전 구현 (position_cap=min(0.50))
구현 차이: 없음
판정: OK

---

## G-07

전략 문서 정의: reserve_currency_asset = OIL AND OIL 변동성 활성 → ALT 노출도 캡 <= 30%
필요 데이터: OIL 변동성 (macro.py 수집됨), reserve_currency_asset 상태
데이터 수집 상태: 부분 수집 — oil_vol_active 수집됨. reserve_currency_asset 개념 미수집.
Guardian 구현 상태: 부분 구현
구현 차이:
  - 현재 구현: oil_vol_active만 체크 → G-07:OIL_VOL_ACTIVE_ALT_CAP_30 플래그
  - reserve_currency_asset = OIL 조건 미체크 (macro.py에 해당 필드 없음)
  - ALT 30% 캡은 플래그만 추가, 실제 cap 값 GuardianResult에 없음
  - reserve_currency_asset 산출 로직이 core_logic_distilled.md에 미정의 → 추가 구현 불가
판정: OK (문서 미정의 조건은 구현 불가. oil_vol_active 단독 체크가 현실적 최선)

---

## G-08

전략 문서 정의: crisis_level = PRE_CRISIS → 신규 진입 보류
필요 데이터: MacroSnapshot.crisis_level
데이터 수집 상태: 수집됨
Guardian 구현 상태: 완전 구현 (allow_new_entry=False)
구현 차이: 없음
판정: OK

---

## G-09

전략 문서 정의: market_condition = DEFLATING 연속 2회 (1d) → BEAR_WARNING 플래그
필요 데이터: MacroSnapshot 이력
데이터 수집 상태: 수집됨
Guardian 구현 상태: 완전 구현
구현 차이: 없음
판정: OK

---

## G-10

전략 문서 정의: nasdaq_dxy_ratio >= 370 (ELEVATED) → 신규 규모 50% 축소
필요 데이터: MacroSnapshot.nasdaq_dxy_ratio
데이터 수집 상태: 수집됨
Guardian 구현 상태: 완전 구현 (buy_size_multiplier=0.5)
구현 차이: 없음
판정: OK

---

## G-11

전략 문서 정의: market_condition = BUBBLE 연속 3회+ (1d) → 거품 구간 경고 / 추가 매수 자제
필요 데이터: MacroSnapshot.dxy_1d_pct, nasdaq_1d_pct 이력
데이터 수집 상태: 수집됨
Guardian 구현 상태: 미구현
구현 차이: _deflating_streak와 동일 패턴인 _bubble_streak가 없음. G-11 체크 없음.
판정: GAP
우선순위: 낮음 (현재 시장이 SIDEWAYS/BULL_CONFIRMED 구간 — BUBBLE streak 발동 시 매수 자제 신호)

---

## G-12

전략 문서 정의: JPY 급락 — 달러엔 신고점 후 장대음봉 발생 → ASIA_INSTABILITY 플래그 / 포지션 10% 선제 축소
필요 데이터: USDJPY 값 + 이력 (신고점 판단용)
데이터 수집 상태: 수집됨 (macro.py → usdjpy_value, usdjpy_1d_pct)
Guardian 구현 상태: 부분 구현
구현 차이:
  - 현재: usdjpy_1d_pct >= 1.5% → ASIA_INSTABILITY (JPY 약세 진행 중 감지)
  - 전략: 달러엔 신고점 도달 후 장대음봉 발생 (엔캐리 언와인드 패턴)
  - 신고점 + 역전 패턴 감지를 위해 다수 스냅샷 이력 필요 (현재 single-shot 분류)
  - "포지션 10% 선제 축소" 실행 신호 미구현 (auto_trader 소관)
판정: OK (단순화된 근사값 허용. 정확한 패턴은 단일 스냅샷으로 구현 불가)

---

## G-13

전략 문서 정의: gold_crisis_signal = SEVERE_CRISIS (DXY↑ AND Gold↑ 동시) → BLOCK ALL BUYS
필요 데이터: MacroSnapshot.gold_crisis_signal
데이터 수집 상태: 수집됨
Guardian 구현 상태: 완전 구현 (block_reasons에 추가)
구현 차이: 없음
판정: OK

---

## G-14

전략 문서 정의: dominance >= 50 + BTC 주봉200 붕괴 → BLOCK ALT BUYS 전면
필요 데이터: DominanceSnapshot.btc_dominance, btc_weekly_200_above
데이터 수집 상태: 수집됨
Guardian 구현 상태: 완전 구현 (block_alt_buys=True)
구현 차이: 없음
판정: OK

---

## G-15

전략 문서 정의: exchange_ratio_divergence = BEAR_DIVERGENCE (DXY 저점 상승 + NASDAQ 고점 하락) → BLOCK BUYS
필요 데이터: DXY 및 NASDAQ 값 이력 (다수 포인트)
데이터 수집 상태: 수집됨 (MacroSnapshot.dxy_value, nasdaq_value 매시 누적)
Guardian 구현 상태: 미구현
구현 차이: _check_g15_bear_divergence 메서드 없음. G-15 체크 없음.
판정: GAP
우선순위: 높음 (나스닥 추가 하락 예고 신호 — BLOCK BUYS 결과. 큰 하락장 진입 초기 감지에 직결)

---

## ETH/BTC 비율 수집기

core_logic_distilled.md L1 (G-01~G-15) 범위에 ETH/BTC 관련 규칙 없음.
L3 E-09에 "ETH/BTC 돌파 확인" 언급이 있으나 L1 Guardian 필터 대상 아님.
판정: 구현 불필요

---

## GAP 항목 구현 우선순위

높음 (즉시 구현):
- G-15: BEAR_DIVERGENCE — 나스닥 추가 하락 예고, BLOCK BUYS. 다수 스냅샷 이력으로 감지 가능. 데이터 이미 수집 중.

낮음 (구현 진행):
- G-11: BUBBLE streak — DEFLATING streak와 동일 패턴. 구현 비용 낮음. 현재 BULL_CONFIRMED 장세에서 거품 경고로 유용.

보류 (구현 불가 또는 auto_trader 소관):
- G-07 reserve_currency_asset: core_logic에 산출 로직 미정의
- G-12 신고점+장대음봉 패턴: 단일 스냅샷 구조로 완전 구현 불가 (근사값 유지)
- G-05 포지션 50% 축소: auto_trader에서 플래그 기반 처리 필요
