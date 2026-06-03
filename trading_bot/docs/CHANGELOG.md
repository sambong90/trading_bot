# CHANGELOG

## 2026-06-03 — 라이브 수집 tz 병합 버그 수정 + 5분봉 라이브 추가

### data.py fetch_ohlcv 병합 버그 (라이브 최대 2h 지연·구멍 원인)
- fresh-path에서 신규 봉 병합 시 df_api(tz-naive KST) vs df_db(tz-aware UTC) 비교 → TypeError → except 폴백으로
  신규 봉을 못 붙임. DB가 stale(>2h) 될 때만 full-fetch로 따라잡아 톱니형 최대 2h 지연 + 최근 구간 구멍 발생(예: BTC 1m 10:02~13:18).
- 수정: df_api·df_db 시간을 양쪽 UTC로 정규화 후 비교·병합. 매 5분 실행 시 최신 50봉을 정상 append → 전진 구멍 없음.
- 영향: 1m/5m/15m/30m/60m 라이브 수집 전부 실시간화. 검증: 병합이 fresh 50봉 정상 append 확인.

### 5분봉 라이브 추가
- config FIVE_MIN_OHLCV_COUNT, scheduler collect_5m(매 5분, :45초 오프셋). 1m과 동급 라이브 수집.



### 보관일 전면 상향
- config.py: OHLCV_PRUNE_DAYS_1M/INTRADAY/DEFAULT 기본값 모두 0(무기한)으로. 외장 2TB 이전 후 전 타임프레임 영구 보존.
  디스크 압박 시 env로 특정 tf만 롤링 제한 가능(예: OHLCV_PRUNE_DAYS_1M=540). db_maintenance는 0=keep로 이미 처리.

### Upbit 소급 한계 (BTC 프로브)
- minute1/15/30/60/240/week/month 모두 상장 시점(BTC ~2017-09)까지 제공 확인. 1분봉도 풀 히스토리 가능.
- day는 프로브 일시 글리치(None)였으나 정상 제공(week/month 2017+ 및 DB 기존 데이터로 확인).

### 백필 스크립트 (tasks/backfill_ohlcv.py)
- ticker×tf를 DB의 oldest에서 과거로 pyupbit `to` 페이징(200/호출), ON CONFLICT DO NOTHING, 빈 응답이면 상장 한계로 종료.
- 재실행 시 DB oldest에서 자동 재개(중단복구 안전). 429 지수 백오프. 진행률 로그. tz(KST) 부여, source='upbit-backfill'.
- 라이브 수집/매매와 독립 실행(별도 프로세스/Job). 점진 확장(1→10→전 종목). sleep 기본 0.3s로 라이브와 rate-limit 합산 여유.
- 규모: 1분봉 전 종목 풀백필은 누적 수억 봉·수일 소요 → 독립 백그라운드로 진행, 외장 용량 추적 병행.

### 이전 결과
- OrbStack VM 디스크(data.img.raw, sparse 15GB)를 외장 APFS SSD(/Volumes/OrbStackSSD)로 이전 후 심볼릭 링크.
- 1차 시도는 lock_data_image 타임아웃으로 실패 → 원인은 OrbStack에 macOS 전체 디스크 접근(이동식 볼륨) 권한 미부여.
  FDA 부여 후 정상 부팅. DB 무손실(부팅 백필로 갭 메움). 내장 원본은 data.bak로 보존(수일 후 정리).
- 외장 결혼식 원본 5.9GB는 재포맷 전 내장 ~/external_rescue/로 md5 검증 복사.

### Phase 5 안정화 (호스트 측, 컨테이너 불변)
- scripts/host/db_backup.sh: 일 1회(04:30 KST) pg_dump -Fc → ~/db_backups/daily, 일요일 weekly 복제,
  로테이션(일7/주4), 실패 시 텔레그램 CRITICAL. launchd com.tradingbot.dbbackup.
- scripts/host/ext_ssd_monitor.sh: 2분마다 /Volumes/OrbStackSSD 마운트 감시, 드롭 시 텔레그램 CRITICAL + orb stop.
  launchd com.tradingbot.extmonitor.
- scripts/host/launchd/*.plist: launchd 정의 사본(버전관리용). 인증은 ~/db_backups/.telegram.env(0600, k8s secret 주입).
- docs/OPERATIONS_EXTERNAL_SSD.md: 부팅/절전 순서, FDA 요구, 물리 안정성, data.bak 정리 시점, 롤백·복구 절차 명문화.
- 백업은 내장(외장 아님). 클라우드 1부는 rclone 설정 시 자동(현재 미구성, 보완 권장).



### 목적
무엣지 확정 후 신규 전략 설계용 데이터 축적. 매매 중단 상태라 수집 부하가 매매에 영향 없음.
기존 수집은 top-60(거래대금) + 1h/4h/day만 → 신규는 거래 가능 KRW 전 종목(262개) ×
1m/15m/30m/1h/4h/day/week/month 전 타임프레임. 매매·청산 로직 불변, 수집 잡만 추가.

### 종목 커버리지 진단
- Upbit KRW 거래 가능: 262종목. 기존 ohlcv 누적 distinct: 232종목(1h=227, day=227, 4h=94).
- 누락 ~30종목은 3개월간 top-N 밖이라 미수집 → 풀 수집으로 전 종목 커버.
- DB 디스크: PVC 명목 4Gi(local-path 미강제) 실제 노드 디스크 127GB 중 115GB free. ohlcv 현 92MB.

### 변경 파일
- config.py: OHLCV_COLLECT_ENABLED/OHLCV_FULL_UNIVERSE 토글, 타임프레임별 봉 수(ONE_MIN_OHLCV_COUNT 등),
  COLLECT_SLEEP_SEC, 타임프레임별 보관일(OHLCV_PRUNE_DAYS_1M=180/INTRADAY=365/DEFAULT=0=무기한).
- data.py: get_all_krw_tickers_full() 추가 — top-N 절단 없이 거래 가능 KRW 전 종목 반환(스테이블·known_delisted 제거).
- tasks/scheduler_service.py: _collect_ohlcv_bulk(interval,count) 공통 루틴 + 잡 등록.
  · 1m 5분마다(최근200봉 묶음, 매분 호출 대비 1/5), 15m/30m 봉마감+오프셋, 1h 전종목 보완(매시 03분),
    day/week/month 1일 1회. 부팅 +60~600초 시차 백필 7종. collect_4h_ohlcv도 전 종목으로 전환.
  · _check_disk_capacity: 6시간마다 디스크 사용률 점검, 80% 초과 시 텔레그램 알림(안전판).
- tasks/db_maintenance.py: OHLCV 보관을 타임프레임별로 분리. 1분봉만 180일 롤링, 15m/30m 365일,
  1h/4h/day/week/month 무기한 보존(기존 일괄 90일 삭제 → 저해상도 장기 축적 가능).

### 용량·rate-limit
- 1분봉 전 종목 ≈ 298MB/일(792B/row 실측), 180일 plateau ≈ 54GB. 15m/30m 365일 ≈ 11GB. 저해상도 ~2GB/년.
  총 plateau ≈ 65GB < free 115GB. 80% 알림이 가드레일.
- Upbit quotation 10req/s·600req/min. 1m 262종목을 종목당 sleep+내부지연으로 ~2분 분산 → <3req/s. 매매 사이클과 합산도 한도 내.


## 2026-06-03 — 연구 모드: 신규 매수 중단 (청산·데이터수집 유지)

### 목적
8개 백테스트로 현 1h 추세추종의 구조적 무엣지 확정(REGIME_UNIVERSE_BACKTEST.md 등).
실거래 신규 매수만 중단하되, 데이터 수집·관측은 유지해 향후 가설 검증 기반 보존.
"봇 종료"가 아니라 "매매만 끄고 연구 인프라 가동". 환경변수/DB로 재배포 없이 재개 가능.

### 변경 파일
- config.py: NEW_BUY_ENABLED(env, 기본 False) + is_new_buy_enabled() 헬퍼.
  우선순위 DB(system_state 'new_buy_enabled') > env > 기본 False. enable_auto_live와 동일 패턴(런타임 토글).
- balanced_plus.py: TAG_PAPER_BUY 추가 + _BUY_EXEC_TAGS에 포함(페이퍼 신호 60분 중복 방지 쿨다운).
- tasks/auto_trader.py: 신규 매수 3경로 차단.
  · Pass2 매수루프: is_new_buy_enabled False면 실주문 대신 PAPER_SIGNAL(ai_events) 기록 + PAPER_BUY 쿨다운 마커 + skip.
  · DCA 조건에 is_new_buy_enabled() 추가(보유분 추가매수도 자본투입이라 차단).
  · 비투패스 직접 진입 경로 방어 가드(실주문 생략).
  매도/청산(HARD_STOP/TRAIL/FIB/스케일아웃)·로테이션 매도는 불변 → 보유분 정상 관리.
- telegram_bot.py: /status·일일브리핑에 '🔬 연구 모드(신규 매수 중단)' 표시.

### 토글 방법 (재배포 불필요)
- 매매 재개: system_state 'new_buy_enabled'='1' (예: UPDATE/INSERT) → 다음 사이클부터 매수 재개.
- 중단 유지: 미설정 또는 '0'(기본). env NEW_BUY_ENABLED=1은 pod 재시작 시 폴백.

### 데이터 수집·청산은 불변 (연구 기반)
- OHLCV(1h/4h/일봉)·매크로/도미넌스/FNG/BTC주봉 수집, PATTERN_DETECTED·analysis_results 로깅 전부 유지.
- 청산 로직 미변경 — 보유 포지션은 기존 TRAIL/FIB/HARD_STOP으로 자연 청산.

### 검증
- py_compile PASS(config/balanced_plus/auto_trader/telegram).
- 배포 후 확인: PAPER_SIGNAL 기록 여부, 실매수 0건, 데이터 수집 지속, 매도 정상.
  SELECT count(*), max(ts) FROM ai_events WHERE event='PAPER_SIGNAL';
- 되돌리기: system_state 'new_buy_enabled'='1' 후 EXECUTE 매수 재개 확인.
- 참고: 현재 계좌 잔액 ~0(사용자 수동 정리), 거래가능 포지션 0 → 실매수는 자본부족으로도 불가하나
  게이트는 자본 재투입 시 자동매매 방지용으로 유효.

## 2026-06-01 — 4h(minute240) 전 종목 수집 확대

### 목적
4h confluence(load_4h_ema_state, 4h 데드크로스 시 buy_size_pct ×0.5)가 구현돼 있으나
minute240 데이터가 47/230종목에만 존재(전용 수집 잡 부재, load_4h_ema_state의 on-demand
fetch로만 우연히 적재) → 183종목(80%)에서 4h confluence가 침묵하고 항상 100% 사이즈로 진입.
4h 데이터를 모니터링 전 종목에 채워 기존 confluence 로직을 전 종목에서 작동시킨다.
(MTF_STRUCTURE_ANALYSIS.md 1순위 권고)

### 변경 파일
- config.py: FOURH_OHLCV_COUNT(기본 100) 추가 — 종목별 4h 확보 봉 수(EMA26+5=31봉 이상).
- tasks/scheduler_service.py: collect_4h_ohlcv() 추가 — get_all_krw_tickers() 전 종목에
  fetch_ohlcv(interval='minute240', count=FOURH_OHLCV_COUNT, use_db_first=True) 호출로
  DB 적재(fetch_ohlcv가 ON CONFLICT DO NOTHING으로 영속화). 4h봉 마감 직후 cron
  (KST 01/05/09/13/17/21 +5분, 6회/일) + 부팅 +45초 초기 백필 1회 등록.
  timedelta 상위 import 추가.

### 제약 준수
- 진입/청산 로직·confluence(×0.5) 미변경 — 데이터만 채움.
- load_4h_ema_state(count=100, EMA12/26, None 반환 시 스킵) 미변경 →
  31봉 미달 신규 종목은 기존대로 confluence 자동 스킵, 데이터 쌓이면 자동 활성화.
- 별도 cron(BackgroundScheduler 스레드풀)이라 1h 매매 사이클(auto_trader Popen) 미지연.
- use_db_first=True + stale 2h 정책으로 4h봉 미변경 시 DB 캐시 → API는 1h 수집의 1/4 빈도.

### 검증 (배포 후 스케줄러 실행 시점)
- minute240 보유 종목 47 → 약 60(유니버스)로 증가.
- load_4h_ema_state가 전 종목에서 None 아닌 (golden, es, el) 반환.
- 4h 데드크로스 종목 매수 시 decision_reason에 '4h Confluence: 데드크로스 → 비중 50%' 기록.
- 정적 검증: py_compile PASS. 런타임 검증은 배포 후 아래 쿼리로 확인.
  SELECT count(DISTINCT ticker) FROM ohlcv WHERE timeframe='minute240';

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
