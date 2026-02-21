# 트레이딩 봇 전체 로직 검증 요약

최종 검토일: 2026-02

## 1. 데이터 흐름

| 단계 | 담당 | 설명 |
|------|------|------|
| 1 | `data.fetch_ohlcv()` | 업비트 API 또는 DB에서 OHLCV 로드, DB 저장 |
| 2 | `data_manager.sync_indicators_for_ticker()` | OHLCV → EMA/RSI/ATR/ADX/BB 계산 → `technical_indicators` 저장 |
| 3 | `strategy.load_cached_indicators()` | `technical_indicators`에서 지표 로드 |
| 4 | `strategy.generate_comprehensive_signal_with_logging()` | Regime 판단 → buy/sell/hold, DB 기록 |
| 5 | `executor.place_order()` | Paper/Live 주문 실행 |

**검증 결과:** 흐름 일치. 지표 동기화가 매 티커 분석 전에 호출되므로 캐시 부재 문제 해소됨.

---

## 2. 전략 로직 (strategy.py)

### 2.1 Regime 판단
- **ADX > 25 + 기울기 상승** → `trend` (강한 추세)
- **ADX > 25 + 기울기 하락** → `weakening_trend` (약세 추세)
- **ADX ≤ 25** → `range` (횡보)

### 2.2 신호 조건
- **추세장/약세 추세장:** EMA 골든크로스(매수), 데드크로스(매도). 매수 시 MTF 필터·RSI<70(추세장만) 적용.
- **횡보장:** 볼린저 하단 터치 매수, 상단 터치 매도. **유효성:** `current_price > 0`, `bb_lower`/`bb_upper` 존재, `bb_upper > bb_lower` 일 때만 사용.

### 2.3 수정·보완 사항
- `np.nan` 사용 구간에 `import numpy as np` 추가.
- EMA 크로스는 `(ema_short or ema_long)` 및 `pd.isna()` 검사로 유효한 지표일 때만 동작.
- 횡보장은 `current_price`, `bb_lower`, `bb_upper` 유효할 때만 신호 생성.
- 같은 봉에 대한 buy/sell 중복 기록 방지 (봉당 1회).

---

## 3. 지표 계산 (data_manager.py)

- **EMA:** pandas `ewm(span=12/26)`.
- **RSI:** 14기간, Wilder 방식.
- **ATR/ADX:** 14기간. ADX는 +DM/-DM/TR 기반.
- **BB:** 20기간 SMA ± 2σ.
- **저장:** 해당 ticker/timeframe의 기존 구간 삭제 후 최근 200봉만 삽입. ts 비교 시 시간대 정규화 적용.

---

## 4. 리스크 (risk.py)

- `calculate_adjusted_position_size`: 기본 포지션 크기(KRW) 및 고정 배율만 반환.
- **미구현:** 연속 손실·승률 기반 동적 조정, ATR 트레일링. 필요 시 `trades` 테이블 연동 확장.

---

## 5. 실행기 (executor.py)

- **PaperExecutor:** 단일 `cash`/`position` 시뮬레이션. **여러 티커를 돌리면 포지션이 티커별이 아니라 합산된 형태로 동작**하므로, Paper 테스트 시 티커 수를 제한하거나 단일 자산으로 해석하는 것이 맞음.
- **LiveExecutor:** 업비트 API, ENABLE_AUTO_LIVE·일일 손실 한도 등 env 플래그 적용.
- 매도 시 `position <= 0`이면 주문 스킵.

---

## 6. auto_trader.py

- 티커 순회 → fetch_ohlcv → sync_indicators_for_ticker → generate_comprehensive_signal → buy/sell이면 place_order.
- **매수:** `position_size > 0`일 때만 실행, `size_pct = max(0.01, min(1.0, position_size/ACCOUNT_VALUE))`로 하한 보장.
- **매도:** 포지션 유무는 실행기 내부에서 검사.
- Paper 모드 시 티커별 포지션이 아닌 단일 포지션 시뮬레이션임을 주석으로 명시.

---

## 7. 스케줄러 (scheduler_service.py)

- `ENABLE_AUTO_TRADING=1`일 때만 `run_trading_cycle` 등록.
- `max_instances=1` + 이전 자식 프로세스 `poll()` 체크로 **동시에 한 번만** auto_trader 실행.

---

## 8. 알려진 제한·개선 여지

| 항목 | 상태 | 비고 |
|------|------|------|
| MTF 필터 | 미구현 | `load_higher_timeframe_indicators` 항상 None → 매수만 보류, 로직은 유지 |
| 동적 리스크 | 스텁 | 연속 손실·승률 반영 시 risk.py 확장 필요 |
| Paper 다티커 | 제한 | 단일 포지션 시뮬레이션; 다티커 정확도는 TICKERS 축소 또는 Live 사용 권장 |
| 신호 중복 | 처리됨 | 같은 봉에 대한 buy/sell 1회만 기록 |

위 항목 반영 후 전체 로직은 일관되게 동작하는 것으로 검증됨.
