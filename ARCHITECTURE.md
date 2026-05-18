# Trading Bot — Architecture

## 1. 디렉토리 구조

```
workspace/
├── Dockerfile                        # Python 3.11-slim, non-root botuser
├── docker-compose.yml                # 로컬 개발용 (bot + postgres)
├── CLAUDE.md                         # AI 에이전트 운영 규칙
├── .github/
│   └── workflows/
│       └── docker-publish.yml        # CI/CD: main push → GHCR push → K8s rollout
├── k8s/
│   ├── namespace.yaml                # quant-bot
│   ├── deployment.yaml               # trading-bot (replicas=1, Recreate)
│   ├── service.yaml                  # Flask 대시보드 NodePort
│   ├── configmap.yaml                # 비민감 설정 (TRADING_MODE, TICKERS 등)
│   ├── secret.yaml                   # 민감 키 (UPBIT_*, TELEGRAM_*, DB_URL)
│   ├── postgres-statefulset.yaml     # PostgreSQL 16 (StatefulSet)
│   ├── postgres-service.yaml
│   ├── postgres-secret.yaml
│   ├── pvc.yaml                      # logs 전용 PVC
│   └── migrate-job.yaml              # 스키마 마이그레이션 Job
└── trading_bot/
    ├── requirements.txt
    ├── main.py                       # Flask 대시보드 진입점
    ├── config.py                     # 환경변수 파싱 및 전략 상수
    ├── models.py                     # SQLAlchemy ORM (17개 테이블)
    ├── db.py                         # 세션 팩토리 (get_session, ensure_tables)
    ├── data.py                       # Upbit 시세 조회, ticker 목록 관리
    ├── data_manager.py               # OHLCV upsert + 지표 계산 (EMA/RSI/ATR/ADX)
    ├── strategy.py                   # EMA 골든크로스 기반 매매 신호 생성
    ├── balanced_plus.py              # 전략 상수, 볼륨 필터, 포지션 사이징
    ├── pattern_recognizer.py         # 패턴 인식 (피보나치, DRAGON_STRONG, LOYALTY)
    ├── market_guardian.py            # L1 글로벌 필터 + L2 장세 분류 (MarketGuardian)
    ├── risk.py                       # Circuit Breaker, Trailing Stop, Exit Signal
    ├── executor.py                   # PaperExecutor / LiveExecutor (Upbit 주문)
    ├── scale_out_manager.py          # 분할 매도 상태 관리 (PositionState DB)
    ├── param_manager.py              # TuningRun에서 최적 파라미터 로드 (60s TTL 캐시)
    ├── sentiment.py                  # Fear & Greed Index 조회 (5분 캐시)
    ├── ai_logger.py                  # AiEvent DB 기록 + RotatingFileHandler
    ├── telegram_bot.py               # Telegram 명령 처리 (/balance, /pause, /panic 등)
    ├── dashboard.py                  # Flask 라우트 (포트폴리오 현황, 시그널 조회)
    ├── monitor.py                    # 계좌 모니터링 유틸
    ├── backtest/
    │   ├── engine.py                 # 백테스트 실행 엔진
    │   ├── data_loader.py            # 백테스트용 OHLCV 로더
    │   └── portfolio.py              # 백테스트 포트폴리오 관리
    ├── collectors/
    │   ├── macro.py                  # 거시 지표 수집 (yfinance: DXY, S&P500, BTC 일봉)
    │   ├── dominance.py              # BTC 도미넌스 수집
    │   ├── kimp.py                   # 김프(한국 프리미엄) 수집
    │   └── aggregator.py             # 수집 결과 집계
    ├── tasks/
    │   ├── scheduler_service.py      # APScheduler 진입점, 모든 주기 작업 등록
    │   ├── auto_trader.py            # 매매 사이클 메인 로직 (analyze_ticker, run_cycle)
    │   ├── auto_tuner.py             # Walk-Forward 파라미터 튜닝 (일요일 04:00)
    │   ├── ai_reviewer.py            # GPT-4o 기반 주간 전략 리뷰
    │   ├── market_briefing.py        # 장 시작 전 시장 브리핑 생성
    │   ├── db_maintenance.py         # 오래된 데이터 정리 (pruning, 매일 03:00)
    │   ├── state_updater.py          # 진행 상태(phase) DB 기록
    │   ├── auto_summary.py           # 자동 요약 생성
    │   └── progress.py               # 작업 진행률 추적
    ├── research/
    │   ├── scraper.py                # 전략 문서 스크래핑
    │   ├── image_downloader.py
    │   └── strategy_pipeline.py      # 전략 데이터 파이프라인
    └── tests/
        ├── test_risk.py
        ├── test_strategy.py
        └── test_sentiment.py
```

---

## 2. 기술 스택

**언어 / 런타임**
- Python 3.11 (ARM64, python:3.11-slim 컨테이너)

**웹 프레임워크**
- Flask 3.x — 포트폴리오 대시보드 및 REST API

**스케줄러**
- APScheduler 3.10+ (BackgroundScheduler, timezone=Asia/Seoul)

**데이터베이스**
- PostgreSQL 16 (K8s StatefulSet) + SQLAlchemy 2.0 ORM
- 로컬 개발: SQLite 폴백 지원

**시장 데이터**
- pyupbit — 업비트 REST API (시세, 주문, 잔고)
- yfinance — 거시 지표 (DXY, S&P500, BTC 일봉)
- Alternative.me API — Fear & Greed Index

**기술 분석**
- ta (Technical Analysis library) — EMA, RSI, ATR, ADX, Bollinger Bands
- numpy, pandas — 지표 계산 및 DataFrame 처리

**AI / 리뷰**
- openai (GPT-4o, GitHub Copilot API) — 주간 전략 리뷰

**알림**
- Telegram Bot API — 거래 알림, 관리자 명령

**인프라**
- OrbStack Kubernetes (로컬 Mac Mini)
- GitHub Actions + GHCR — CI/CD (main push → 자동 빌드/배포)
- Self-hosted GitHub Actions runner (Mac Mini)

---

## 3. 핵심 모듈별 역할

**진입점 / 스케줄러**
- `tasks/scheduler_service.py` — APScheduler 기동, 모든 주기 작업(매매·수집·튜닝·정리) 등록, heartbeat·PID 관리
- `tasks/auto_trader.py` — 매 시간 매매 사이클 실행 (티커별 분석 → 신호 → 주문)
- `main.py` — Flask 대시보드 서버 기동

**시세 / 데이터**
- `data.py` — Upbit에서 KRW 티커 목록·OHLCV 조회, 스테이블코인·상폐 티커 필터
- `data_manager.py` — OHLCV DB upsert + EMA/RSI/ATR/ADX 지표 계산 및 저장

**전략 / 시그널**
- `strategy.py` — EMA 골든크로스 + RSI 40~75 + 거래량 필터 기반 매매 신호 생성
- `balanced_plus.py` — 전략 상수, 볼륨 정규화, 동적 포지션 사이징 로직
- `pattern_recognizer.py` — 피보나치 되돌림 구간 + DRAGON/LOYALTY 패턴 강도 계산

**리스크 / 필터**
- `market_guardian.py` — L1 글로벌 차단(BTC 약세·도미넌스·DXY) + L2 장세 분류(BULL_EARLY 등), 포지션 상한 결정
- `risk.py` — Circuit Breaker(일간 DD 5%), ATR Trailing Stop, Fibonacci Hard Stop, 시스템 상태 KV 저장소

**주문 실행**
- `executor.py` — PaperExecutor(시뮬레이션) / LiveExecutor(실제 Upbit 주문), 슬리피지 가드(3%)
- `scale_out_manager.py` — ATR 배수 기반 분할 매도 단계(0→1→2) 상태 관리

**파라미터 / 튜닝**
- `param_manager.py` — TuningRun 최신 레코드에서 최적 파라미터 로드 (60초 TTL 캐시)
- `tasks/auto_tuner.py` — Walk-Forward Grid Search, 매주 일요일 04:00 실행
- `tuner.py` — Grid Search 엔진

**데이터 수집**
- `collectors/macro.py` — 거시 지표 (DXY, S&P500, BTC 일봉) 수집, 매일 07:00
- `collectors/dominance.py` — BTC 도미넌스, 6시간 간격
- `collectors/kimp.py` — 한국 프리미엄(김프) 수집, 매 시간

**로깅 / 알림**
- `ai_logger.py` — 전략 결정을 AiEvent DB에 기록 + RotatingFileHandler(10MB×2)
- `telegram_bot.py` — 거래 알림 발송, /balance·/pause·/panic·/resume 명령 처리

**유지보수**
- `tasks/db_maintenance.py` — 오래된 데이터 정리 (OHLCV 90일, AiEvent 90일, EquityPoint 30일 등)
- `models.py` — SQLAlchemy ORM, 17개 테이블 정의 및 자동 생성 (ensure_tables)

---

## 4. 데이터 흐름

```
[Upbit API]
    │
    ▼  pyupbit.get_ohlcv()
data.py::fetch_ohlcv()
    │  ├─ DB에 최신 봉 있고 2h 이내 → DB 캐시 사용 (gap-fill: 최근 50봉 API 보완)
    │  └─ stale or 없음 → Upbit API 전체 재조회
    │
    ▼
data_manager.py::sync_indicators_for_ticker()
    │  ├─ OHLCV upsert → DB (u_ticker_timeframe_ts constraint)
    │  └─ 지표 계산 (EMA/RSI/ATR/ADX/BB) → TechnicalIndicator DB 저장
    │
    ▼
market_guardian.py::MarketGuardian().evaluate()
    │  ├─ L1: BTC 약세·도미넌스·DXY·SUPER_CRISIS 체크 → NO_TRADE 시 전면 차단
    │  └─ L2: 장세 분류(BEAR~BULL_CLIMAX) → 포지션 상한(0~80%) 결정
    │
    ▼  (L1 통과 시)
tasks/auto_trader.py::analyze_ticker()
    │
    ├─ [포지션 보유 시]
    │   ├─ risk.py::evaluate_exit_signal()  → HARD STOP (Fibonacci 이탈 or ROI -10%)
    │   └─ risk.py::evaluate_trailing_stop() → ATR Trailing Stop (50% 분할 매도)
    │
    ├─ pattern_recognizer.py::analyze()
    │   └─ _max_strength 계산 (DRAGON_STRONG=0.95, LOYALTY=0.90, 피보나치 구간 등)
    │
    ├─ strategy.py::generate_comprehensive_signal_with_logging()
    │   ├─ EMA 골든크로스 + RSI 40~75 + 거래량 ≥ 0.8x → 'buy' / 'sell' / 'hold'
    │   └─ AnalysisResult DB 저장
    │
    ├─ [DYN_THR 게이트] _max_strength < 0.85 → SKIP (패턴 미확인 진입 차단)
    │
    ├─ [매수 조건 충족 시]
    │   ├─ calculate_dynamic_size() → Kelly + ATR 변동성 기반 포지션 사이징
    │   └─ executor.place_order('buy', ...) → PaperExecutor or LiveExecutor
    │
    └─ ai_logger.py::log_ai_event() → AiEvent DB + ai_debug.log


[주문 실행]
executor.py::LiveExecutor
    │  ├─ 슬리피지 가드: 현재가 vs 주문가 3% 초과 시 거부
    │  ├─ pyupbit.Upbit.buy_market_order() / sell_market_order()
    │  └─ 잔고 캐시(_balance_cache) 갱신

    │
    ▼
[로깅 & 알림]
    ├─ AiEvent DB (EXECUTE/STOP_LOSS/SCALE_OUT/DCA → 영구 보존)
    ├─ auto_trader.log (RotatingFileHandler, 20MB×3)
    └─ telegram_bot.py::_notify() → Telegram 메시지 발송
```

---

## 5. 외부 연동

**거래소**
- Upbit REST API (pyupbit)
  - 시세 조회: `get_ohlcv`, `get_current_price`, `get_tickers`
  - 계좌/잔고: `Upbit.get_balances()`
  - 주문: `Upbit.buy_market_order()`, `Upbit.sell_market_order()`
  - 인증: `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`

**거시 데이터**
- yfinance — DXY(달러인덱스), ^GSPC(S&P500), BTC-USD 일봉
- Alternative.me API — Fear & Greed Index (무인증, 일 1회 업데이트)

**AI 리뷰**
- GitHub Copilot API (`https://api.githubcopilot.com`) — GPT-4o 기반 주간 전략 리뷰
  - 인증: `COPILOT_ACCESS_TOKEN` (OAuth token)

**알림**
- Telegram Bot API — 거래 체결·이상 알림, 관리자 명령(/balance, /positions, /pause, /resume, /panic)
  - 인증: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_USER_ID`

**데이터베이스**
- PostgreSQL 16 (K8s StatefulSet, `postgres` 서비스명)
  - 연결: `DB_URL` (SQLAlchemy DSN, Secret 주입)
  - ORM: SQLAlchemy 2.0, Alembic 미사용 (ensure_tables()로 자동 생성)

**CI/CD**
- GitHub Container Registry (GHCR) — Docker 이미지 저장
- GitHub Actions (ubuntu-latest + self-hosted Mac Mini runner)

---

## 6. 설정 / 환경 구조

### 환경변수 계층 (우선순위 높은 순)

```
K8s Secret (trading-bot-secret)         ← 민감 정보
    UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ADMIN_USER_ID
    DB_URL                              (postgresql://user:pass@postgres:5432/db)
    COPILOT_ACCESS_TOKEN

K8s Secret (trading-bot-secrets)        ← envFrom 통째 주입
    FLASK_API_KEY

K8s ConfigMap (trading-bot-config)      ← 비민감 운영 설정
    TRADING_MODE         = "live"        # paper | live
    ENABLE_AUTO_TRADING  = "0"           # "1"로 바꿔야 매매 사이클 실행
    ENABLE_AUTO_LIVE     = "0"           # /panic 명령 시 "0"으로 자동 변경
    CANDLE_SYNC_OFFSET_SEC = "60"        # HH:01:00에 실행
    HARD_STOP_LOSS_PCT   = "-10.0"
    SLIPPAGE_GUARD_PCT   = "3.0"
    ACCOUNT_VALUE        = "1000000"     # Paper 모드 초기 자금
    TELEGRAM_ALERT_LEVEL = "TRADE"       # CRITICAL | TRADE | SUMMARY | OFF
    TICKERS              = ""            # 비어있으면 거래대금 상위 60개 자동 선택
    TICKER_TOP_N         = "60"
```

### config.py 주요 상수 (환경변수로 오버라이드 가능)

```
RSI_BUY_MIN/MAX          = 40 / 75      # 매수 RSI 유효 구간
RSI_SELL_MIN             = 80           # 과매수 매도 강화 임계
HARD_STOP_LOSS_PCT       = -15.0        # ROI 기반 하드 스탑
SCALE_OUT_ATR_MULT_1/2   = 2.0 / 3.5   # ATR 배수 분할 매도 기준
TS_MULT_LOW/MID/HIGH     = 3.0/2.0/1.5 # ROI 구간별 Trailing Stop 타이트니스
DD_DAILY_LIMIT_PCT       = 5.0          # 일간 DD Circuit Breaker
DD_TOTAL_LIMIT_PCT       = 15.0         # 전체 DD Circuit Breaker
BREAKEVEN_ROI_PCT        = 3.0          # 손익분기 이후 트레일링 스탑 하한 고정
TARGET_VOL_PCT           = 0.02         # 변동성 타겟팅 (포지션 사이징)
FNG_EXTREME_FEAR         = 20           # Panic Dip-Buy 발동 임계
```

### 스케줄 (APScheduler, KST 기준)

```
HH:01:00 (매 시간)  — auto_trader.py 매매 사이클 (캔들 마감 60초 후)
HH:00:00 (매 시간)  — 김프(kimp) 수집
00,04,08,12,16,20시:02분 — BTC 도미넌스 수집
07:00               — 거시 지표 수집 (DXY, S&P500, BTC 일봉)
03:00 (매일)        — DB 하우스키핑 (오래된 데이터 pruning)
04:00 (일요일)      — Walk-Forward 파라미터 튜닝
09:00 + 4시간 간격  — 시장 브리핑 생성
5분 간격            — Heartbeat (scheduler_heartbeat.json 갱신)
```

### 스토리지 구조

```
PostgreSQL (StatefulSet PVC)
├── ohlcv                — OHLCV 봉 데이터 (90일 보관)
├── technical_indicators — EMA/RSI/ATR/ADX/BB 지표
├── analysis_results     — 전략 분석 결과
├── ai_events            — 매매 결정 로그 (EXECUTE 계열 영구 보존)
├── position_states      — 분할 매도 단계, trailing peak
├── system_state         — KV 저장소 (CB 상태, consec_losses, known_delisted 등)
├── tuning_runs          — Walk-Forward 튜닝 결과
├── macro_snapshots      — DXY, S&P500, BTC 일봉 스냅샷
├── dominance_snapshots  — BTC 도미넌스
├── kimp_snapshots       — 김프
└── equity_points        — 자산 곡선 포인트 (30일 보관)

PVC (trading-bot-data / logs/)
├── auto_trader.log      — 매매 사이클 로그 (20MB×3 Rotating)
├── ai_debug.log         — 전략 결정 상세 로그 (10MB×2 Rotating)
├── scheduler_out.log    — 스케줄러 로그 (10MB×2 Rotating)
├── scheduler_heartbeat.json — Liveness Probe 기준 (5분마다 갱신)
└── auto_trader.pid      — 중복 실행 방지 PID 락
```
