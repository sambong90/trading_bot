# CHANGELOG

## 2026-05-31 — HARD_STOP 임계값 출처 통일 + CB 강제매도 스트릭 분리

### 목적
데드락 수정 후속 정적 점검 2건 정리.
1) HARD_STOP 임계값이 executor에서 env 직접참조(폴백 -10.0)라 config(-15.0)와 분기 →
   env 누락 시 의도보다 빡빡한 -10% 조기 손절 위험.
2) CB(DD 5/15%) 강제 50% 매도 손실이 orders에 남아 연속손실 스트릭을 오염 →
   "시장 급락(시스템 방어)"을 "전략 진입 실패"로 오인해 다음 진입에 페널티 전가.

### 변경 파일
- executor.py: _get_hard_stop_loss_pct()가 config.HARD_STOP_LOSS_PCT 단일 소스 참조
  (env override는 config가 이미 흡수: env > -15.0). 하드코딩 -10.0 폴백 제거.
  PaperExecutor/LiveExecutor place_order에 reason 파라미터 추가 → 매도 시
  orders.raw.exit_reason에 기록(예: CB_FORCED).
- tasks/auto_trader.py: CB 강제 50% 매도 호출에 reason='CB_FORCED' 전달.
- risk.py: get_consecutive_losses()·get_win_rate()가 raw.exit_reason=='CB_FORCED' 매도를
  계산에서 스킵(투명). 일반 손절(HARD_STOP/FIB/TRAIL, 무태그)은 그대로 포함.

### 검증
- env HARD_STOP_LOSS_PCT 제거 시 config -15.0 적용(우선순위 env > -15.0) — 정적 확인.
- 로직 시뮬레이션: CB_FORCED 손실/이익 모두 스트릭·승률에서 제외(투명), 무태그 손절은 계속 카운트.
- 과거 CB_SELL 2건(최종 2026-03-15)은 태그 이전이라 소급 제외 안 됨 — 전향적 적용.
- 단위 테스트: 변경 전후 동일(13 pass / 23 fail, 기존 환경 namespace 이슈로 무관).
- 제약 준수: 데드락 수정(floor 0.3, 시간감쇠) 미변경, CB(5/15%)·HARD_STOP(-15%) 임계값 자체 미변경.

---

## 2026-05-31 — 연속 손실 자기강화 잠금(데드락) 해소

### 목적
연속 손실 스트릭이 DYN_THR 상향 + position_size 0을 동시에 일으켜
손실 후 회복 진입을 스스로 봉쇄하는 데드락 제거.
(손실→스트릭↑→사이즈0+임계값↑→매수불가→청산없음→승리없음→스트릭 영구고정)
TRADE_BLOCKAGE_TRACE.md 추적으로 확정: 48h buy 신호 16건 전량 미체결,
13건 DYN_THR(0.66) 차단, position_size_multiplier=0.0(연속손실4) 잠재 데드락.

### 변경 파일
- config.py: SIZE_MULT_FLOOR(0.3), SIZE_MULT_BY_STREAK(0:1.0,1:0.8,2:0.6,3:0.45),
  STREAK_DECAY_HOURS(24), STREAK_RESET_HOURS(48) 상수 추가.
- risk.py: get_consecutive_losses()를 단일 소스로 통일 + 시간 감쇠 통합
  (마지막 손실 후 24h마다 1 감소, 마지막 거래 후 48h 무거래 시 0 리셋).
  calculate_adjusted_position_size: multiplier 0.0 제거, floor 0.3 적용.
- tasks/auto_trader.py: DYN_THR/Kelly 페널티가 get_consecutive_losses() 단일 소스 참조.
  buy 신호 통과 후 position_size<=0 시 FILTER_BLOCK(ZERO_SIZE) 기록 + WARNING(무성 드롭 제거).
  _update_consec_losses는 표시용 legacy로 격하(의사결정 비참조).
- analytics.py, telegram_bot.py: 표시용 연속손실도 get_consecutive_losses() 단일 소스로 전환.
- watchdog.py: _check_loss_streak 추가 — 연속손실>=3, 24h 지속 시 / size_mult<=0.5 진입 시 알림.

### 검증
- 실데이터(orders): raw streak 4, 마지막 거래 120h 전 → 신규 로직 effective=0(48h 리셋).
  size_multiplier 0.0→1.0, DYN_THR(BULL_EARLY) 0.66→0.60. CPOOL(str 0.645) 통과 가능.
- 단위 테스트: 변경 전후 동일(13 pass / 23 fail, 기존 환경 namespace 이슈로 무관).
- 제약 준수: 페널티 개념 유지, DYN_THR_BY_REGIME base 미변경, HARD_STOP/CB 미수정.

---

## 2026-05-26 — 패턴 감지 및 필터 차단 관측성 복원

### 목적
PATTERN 로그가 debug 레벨로 억제되어 DRAGON/LOYALTY 감지 여부 확인 불가.
RSI_OVERBUY 등 필터 차단도 로깅 없이 신호가 소멸되던 문제 해결.
전략 수정 판단에 필요한 데이터를 DB에 영구 수집.

### 변경 파일
- tasks/auto_trader.py: 패턴 감지 시 PATTERN_DETECTED 이벤트 ai_events 저장 (strength>0인 건만).
  EMA_FILTER / RSI_OVERBUY / BUY_COOLDOWN / CASH_RULE / MAX_OPEN_POSITIONS 차단 시 FILTER_BLOCK 이벤트 저장.
- analytics.py: get_pattern_stats(days) 추가 — 패턴별 감지 수, 필터별 차단 수.
- telegram_bot.py: /signals 응답에 24h 패턴/필터 현황 블록 추가.

---

## 2026-05-25 — auto_tuner OOS 검증력 강화

### 목적
OOS=0.0 파라미터가 실전에 무조건 적용되던 문제 수정.
OOS 기간 9일(30일 데이터)에서 EMA 15/30 크로스 신호가 발생하지 않아 검증 불가했던 구조 개선.

### 변경 파일
- tasks/auto_tuner.py: COUNT_30D_1H(720) → COUNT_60D_1H(1440). OOS 기간 9일 → 18일.
  튜닝 완료 후 텔레그램 알림 (IS/OOS score, 파라미터, 적용 여부).
- param_manager.py: OOS score gate 추가.
  최신 TuningRun의 oos_score <= 0 이면 스킵, 이전 레코드 중 oos_score > 0인 것 사용.
  모든 레코드 미달이면 config.py 기본값 fallback.

---

## 2026-05-25 — 시장 캘린더 데이터 구축

### 목적
메모리얼데이(평일 공휴일) 휴장으로 봇이 '오늘 미장이 열렸는지'를 모르던 문제 해결.
ratio_quality에 'holiday' 값 추가로 stale(주말)과 구분.

### 신규 파일
- market_calendar.py: NYSE 개장일/공휴일/장시간 판정 모듈
  - is_us_market_open(), is_us_holiday(), get_holiday_name()
  - is_us_market_hours(), get_last_trading_day(), hours_since_last_close()
  - exchange_calendars 설치 시 자동 활용, 미설치 시 수동 목록(2025~2026) fallback

### 변경 파일
- collectors/macro.py: _is_market_stale() → _ratio_quality() 교체
  fresh(개장일) / holiday(평일 공휴일) / stale(주말) 3단계 분류
- collectors/aggregator.py: ratio_quality='holiday' 시 age 체크 생략
  hours_since_last_close() 기준으로만 stale_but_usable 판정
- market_guardian.py: G-15 ratio_quality 필터 '!= stale' → '== fresh'
  (holiday 데이터도 G-15 lookback에서 제외)
- tasks/scheduler_service.py: collect_macro()에 휴장일 skip 로직 추가
  공휴일에는 07:00/19:00 KST 2회만 수집 (12회 → 2회)
- watchdog.py: _check_macro_staleness() 추가 (공휴일/주말 자동 면제)
- telegram_bot.py: /health, /guardian, 일일 브리핑에 HOLIDAY 상태 표시
- requirements.txt: exchange_calendars>=4.0 추가 (soft dependency)

---

## 2026-05-25 — G-15 오발동 수정 (A+C)

### 원인
- 휴장일(토/일) macro_snapshots에 stale 행 27/30개 누적
- NDX lower highs 판정이 0.11%(28pt) 차이(동일 금요일 장중 틱)로 발동

### 변경 파일
- config.py: G15_NDX_MIN_DROP_PCT=0.5, G15_DXY_MIN_RISE_PCT=0.3 상수 추가
- market_guardian.py _check_g15_bear_divergence():
  - MacroSnapshot 쿼리에 ratio_quality != 'stale' 필터 추가 (수정 A)
  - ndx_lower_highs 조건: max(ndx_recent) < max(ndx_prev) * (1 - 0.5/100) (수정 C)
  - dxy_higher_lows 조건: min(dxy_recent) > min(dxy_prev) * (1 + 0.3/100) (수정 C)

### 정책
- stale 제외 후 fresh 행 수가 G15_DIVERGENCE_LOOKBACK(60) 미만이면 return False (필터 비적용)
- G15_NDX_MIN_DROP_PCT, G15_DXY_MIN_RISE_PCT 환경변수로 오버라이드 가능

---

## DYN_THR 정책 (참조)

BULL_CLIMAX=0.50, BULL_CONFIRMED=0.55, BULL_EARLY=0.60,
SIDEWAYS=0.75, BEAR_WARNING=0.85, BEAR_CONFIRMED=0.90, UNKNOWN=1.00

---

## 수집 스케줄 (참조)

macro: 12회/일 (2시간 간격)
dominance: 24회/일 (1시간 간격)
kimp: 4회/일 (6시간 간격)
FNG: 4회/일
BTC 주봉 200MA: 1회/일
