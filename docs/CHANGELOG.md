# CHANGELOG.md

## 실시간 이상 감지 Watchdog (2026-05-24)

trading_bot/watchdog.py 신규. APScheduler 5분 간격 등록. 완전 읽기 전용.

시스템 이상:
- 스케줄러 heartbeat 10분 이상 미갱신 → 🚨 알림
- 매매 사이클 HH:01 기준 5분 이상 지연 → ⚠️ 알림 (ENABLE_AUTO_TRADING=1일 때만)
- DB 연결 실패 (SELECT 1) → 🚨 알림
- Upbit API 3회 연속 실패 (모듈 내 카운터) → 🚨 알림

매매 이상:
- 동일 종목 1시간 내 매수+매도 (ai_events EXECUTE) → ⚠️ 왕복 매매 알림
- Order.raw.signal_price vs fill_price 2% 초과 → ⚠️ 슬리피지 알림
- 일간 DD가 DD_DAILY_LIMIT_PCT×80% 도달 (system_state) → ⚠️ CB 사전 경고
- BULL_EARLY 이상 장세 24h 매수 체결 0건 → ℹ️ 무거래 알림

데이터 이상:
- kimp_snapshots 최신 레코드 12h 초과 → ⚠️ 알림
- sentiment_snapshots 6h 초과 + live fallback 실패 → ⚠️ FNG 알림

중복 방지: 동일 이상 1h 쿨다운 (메모리, pod 재시작 시 초기화).
TELEGRAM_ALERT_LEVEL 연동: CRITICAL=🚨만, TRADE=🚨+⚠️, SUMMARY=전부, OFF=없음.

변경 파일:
- trading_bot/watchdog.py: 신규 (이상 감지 10개 체크)
- trading_bot/tasks/auto_trader.py: run_cycle() 종료 시 system_state last_cycle_completed 기록
- trading_bot/tasks/scheduler_service.py: watchdog job 등록 (5분 간격, misfire_grace_time=60)

## Guardian L1 필터 보강 (2026-05-21)

GAP_ANALYSIS.md 기반 G-11/G-15 구현. 나머지(G-07 reserve_currency, G-12 패턴 완전 구현)는 데이터 구조상 불가 판정.

G-11 BUBBLE_STREAK (P3):
- 최근 5개 MacroSnapshot에서 BUBBLE 연속 발생 시 buy_size_multiplier 0.5 적용
- 임계값: config.G11_BUBBLE_STREAK_MIN=3, config.G11_BUBBLE_BUY_SIZE_MULT=0.5
- GuardianResult.flags에 G-11:BUBBLE_STREAK_n 추가
- 구현: market_guardian.py _bubble_streak(), _compute_bubble_streak()

G-15 BEAR_DIVERGENCE (P3):
- 최근 G15_DIVERGENCE_LOOKBACK(=60)개 MacroSnapshot을 전반/후반 분할
- 후반 DXY min > 전반 DXY min AND 후반 NASDAQ max < 전반 NASDAQ max → allow_new_entry=False
- 임계값: config.G15_DIVERGENCE_LOOKBACK=60 (12회/일 기준 약 5일)
- GuardianResult.flags에 G-15:BEAR_DIVERGENCE 추가
- 데이터 60개 미달 시 False 반환 (시스템 초기 안전 처리)
- 구현: market_guardian.py _check_g15_bear_divergence()

config.py 신규 상수:
- G11_BUBBLE_STREAK_MIN, G11_BUBBLE_BUY_SIZE_MULT, G15_DIVERGENCE_LOOKBACK

참조 문서: docs/GAP_ANALYSIS.md

## DYN_THR 장세별 차등 정책 (2026-05-19~)

Dynamic Signal Threshold는 GuardianResult.regime에 따라 차등 적용된다. 구현: config.DYN_THR_BY_REGIME.

- BULL_CLIMAX: 0.50 / BULL_CONFIRMED: 0.55 / BULL_EARLY: 0.60
- SIDEWAYS: 0.75 / BEAR_WARNING: 0.85 / BEAR_CONFIRMED: 0.90 / UNKNOWN: 1.00

연속 손실 streak 페널티(+0.02/회)는 기존과 동일하게 위 base에 누적 적용.
긴급 오버라이드: 환경변수 DYN_THR_OVERRIDE (전체 장세 무시, 단일값 강제).

DYN_THR 차단 시 ai_events에 SKIP 이벤트 기록 (extra.dyn_thr, extra.strength 포함) → analytics.py로 장세별 차단율 추적 가능.

## MacroSnapshot STALE_BUT_USABLE 정책 (2026-05-19~)

macro 데이터 나이 기준:
- 26h 이하: fresh — 정상 운영
- 26h~72h: STALE_BUT_USABLE — L1/L2 평가는 진행하되 buy_size_multiplier 0.5 적용
- 72h 초과: RATIO_STALE_EM7 — is_tradeable=False, 매수 전면 차단

이전의 평일(26h)/주말(72h) 이분법을 통합. 주말에도 72h 이내이면 STALE_BUT_USABLE로 거래 허용.

## 주요 버그 수정 이력 (재발 방지)

1. count_open_positions 오버카운트: _balance_cache 전체 non-KRW를 카운트하여 MAX_OPEN_POSITIONS 가짜 도달 → 봇 관리 티커만 카운트하도록 수정 (2026-03-13)
2. 수동 매수 시 CB 오발동: compute_total_account_equity가 수동 매수 자산 미포함 → 계좌 전체 자산 포함하도록 수정 (2026-03-13)
3. 매수 직후 CB 오발동: Upbit 정산 딜레이로 _balance_cache에 매수 자산 미반영 → _pending_buy_costs로 equity 보정 (2026-03-15)
4. CB 50% 매도 최소금액 에러: under_min_total_market_ask → 매도 전 5000원 미만 체크 추가
5. PatternRecognizer 불완전 캔들: get_ohlcv(minute60)이 HH:01 시점에 1분치 진행 중 캔들을 마지막 행으로 포함 → vol_ratio≈0.01로 DRAGON/LOYALTY 항상 실패, SIEGE 허위 감지. PatternRecognizer.__init__에서 df.iloc[:-1] 제거 (2026-05-19)
6. SIEGE 1h 타임프레임 오적용: _SIEGE_MIN_CANDLES_1D=7(일봉 상수)이 1h에도 적용되어 7시간 횡보로 조건 낮아짐 → _SIEGE_MIN_CANDLES_1H=168 추가, 타임프레임별 분기 (2026-05-19)
7. _max_strength에 sell 패턴 포함: ABANDONMENT/DRAGON_BEAR의 strength가 buy conviction 판단에 혼입 → buy 신호만 집계하도록 수정 (2026-05-19)
8. PositionState.avg_buy_price 항상 0: update_trailing_high()가 새 레코드를 avg=0으로 생성 + 이후 set_scale_out_stage() 미호출 → 매수 직후 set_scale_out_stage(ticker, 0, fill_price) 추가. 근거: HYPER/NEAR 모두 avg=0으로 count_open_positions=0, analytics 오표시 (2026-05-20)
9. HARD/TRAIL STOP 매도 ai_events 누락: analyze_ticker() HARD STOP, TRAIL STOP 경로 및 Pass 0 check_hard_stop_loss() 이후 log_ai_event() + log_execution_event() 미호출 → 3개 경로에 STOP_LOSS/SCALE_OUT 이벤트 기록 추가. 누락 시 sync_manual_trades()가 MANUAL_SELL로 오분류 (2026-05-20)

## 데이터 수집 체계 최적화 (2026-05-20)

### 현행 스케줄 (최적화 후)

- HH:00 (0,6,12,18): 김프 수집 — 4회/일
- HH:00 (22,23,0,1,2,3,4,5,6,7,13,19): 매크로 수집 — 12회/일
- HH:01: 매매 사이클 (auto_trader) — 24회/일
- HH:02: BTC 도미넌스 수집 — 24회/일
- HH:30 (0,6,12,18): FNG 수집 — 4회/일
- 08:05: BTC 주봉 200MA — 1회/일
- 03:00: DB 하우스키핑 — 1회/일
- 04:00 (일요일): Walk-Forward 튜닝 — 1회/주
- 09:01: 일일 브리핑 — 1회/일
- 5분 간격: Heartbeat — 288회/일

### 신규 파일

- collectors/sentiment.py — FNG 스케줄 수집기 (3회 재시도, DB 저장)
- collectors/btc_weekly.py — BTC 주봉 200MA 수집기 (DB 저장)

### 신규 DB 테이블 (create_all() 자동 생성)

- sentiment_snapshots — FNG 스냅샷 (value, label, indicator_type)
- btc_weekly_snapshots — BTC 주봉 200MA (ma200, current_price, above_ma200)

### 주요 변경 사항

- 매크로: 1회/일 → 12회/일 (미장 매시 + 평시 3회). None 반환 시 텔레그램 알림 + 30분 후 1회 자동 재시도.
- 도미넌스: 6회/일(4h) → 24회/일(매시). 나이 3h 초과 시 DOMINANCE_STALE, 12h 초과 시 DOMINANCE_DATA_MISSING (aggregator.py).
- 김프: 24회/일 → 4회/일 (Guardian 미소비, ExchangeRate-API 할당량 절약).
- FNG: 스케줄 없음(매 사이클 live) → 4회/일 스케줄 수집 + DB 저장. sentiment.py가 DB 우선 조회(6h 이내), 초과 시 live fallback.
- BTC 주봉 200MA: 매 사이클 pyupbit live → 1회/일 DB 캐싱. aggregator가 DB 조회(48h 이내), 초과 시 live fallback.
- 도미넌스 연속 실패 알림: 3회 연속 실패 시 텔레그램 알림.
- /health Telegram 명령 추가 — 5개 데이터 소스 나이·상태 표시.
- /api/health/data Flask 엔드포인트 추가 (JSON).

### Stale 임계값 요약 (aggregator.py 기준)

- macro: STALE 경고 26h (STALE_BUT_USABLE) / MISSING 차단 72h
- dominance: STALE 경고 3h / MISSING 차단 12h
- FNG: DB 조회 6h 초과 → live fallback (차단 없음)
- BTC 200MA: DB 조회 48h 초과 → live fallback (차단 없음)
