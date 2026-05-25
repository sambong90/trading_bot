# CHANGELOG

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
