# 매매 전략 및 소스 구성

## 1. 매매 전략 요약

### 1.1 기본 구조

- **타임프레임:** 1시간봉(`minute60`) 기준.
- **지표:** EMA(12/26), RSI(14), ATR(14), ADX(14), 볼린저밴드(20, 2σ).
- **Regime(구간) 판단:** ADX로 추세/횡보 구분 후, 구간별로 다른 진입/청산 규칙 적용.

### 1.2 Regime(구간) 정의

| Regime | 조건 | 설명 |
|--------|------|------|
| **trend** | ADX > 25 이고 ADX 기울기 ≥ 0 | 추세가 강하고 유지되는 구간 |
| **weakening_trend** | ADX > 25 이고 ADX 기울기 < 0 | 추세는 있으나 약해지는 구간 (방어적 매수) |
| **range** | ADX ≤ 25 | 횡보 구간 |

### 1.3 구간별 신호 규칙

| Regime | 매수 조건 | 매도 조건 | 비고 |
|--------|-----------|-----------|------|
| **trend** | EMA 골든크로스 + MTF 통과 + RSI < 70 | EMA 데드크로스 | 상위봉 하락 시 매수 보류 |
| **weakening_trend** | EMA 골든크로스 + MTF 통과 | EMA 데드크로스 | 매수 시 포지션 크기 50% |
| **range** | 현재가 ≤ BB 하단×1.01 | 현재가 ≥ BB 상단×0.99 | BB·가격 유효할 때만 |

- **MTF(멀티 타임프레임) 필터:** 상위 타임프레임이 하락장이면 매수만 보류(현재 MTF 데이터는 미연동, 로직만 존재).
- **중복 방지:** 같은 1시간봉에 대해 buy/sell 신호는 각각 1회만 기록.

### 1.4 리스크·포지션

- **포지션 크기:** `account_value × risk_per_trade_pct / stop_loss_pct` (기본 2% 리스크, 5% 손절 가정).
- **약세 추세장 매수:** 위 포지션의 50%만 사용.
- **동적 리스크:** 연속 손실·승률 반영은 현재 스텁(추후 `trades` 연동 가능).

---

## 2. 소스 구성

### 2.1 디렉터리·파일 역할

```
trading_bot/
├── strategy.py          # 전략: Regime 판단, 신호 생성, signals/analysis_results 저장
├── data.py              # OHLCV: 업비트 API·DB 조회, get_all_krw_tickers
├── data_manager.py      # 지표 계산(EMA/RSI/ATR/ADX/BB), technical_indicators 동기화
├── risk.py              # 포지션 크기·일일손실·드로우다운 체크 (동적 조정은 스텁)
├── executor.py          # PaperExecutor / LiveExecutor, place_order
├── models.py            # DB 모델: OHLCV, Signal, Order, TechnicalIndicator, AnalysisResult 등
├── db.py                # SQLite 세션 (DB_URL)
├── monitor.py           # 텔레그램 발송 (send_telegram)
├── main.py              # Flask 대시보드: 로그/결정/계좌/OHLCV API, /panic
├── backtest.py          # 백테스트·메트릭 (simple_backtest, compute_metrics)
├── tuner.py             # 그리드 서치 튜닝
├── dashboard.py         # 대시보드 보조
│
├── tasks/
│   ├── scheduler_service.py  # APScheduler: 주기적으로 auto_trader 실행 (5분 간격 등)
│   ├── auto_trader.py        # 매매 사이클: 티커 순회 → fetch → 지표동기화 → 신호 → 주문
│   ├── auto_summary.py       # 상태 요약·텔레그램 (선택)
│   ├── state_updater.py      # current_phase.json 갱신
│   └── progress.py           # progress.json (진행률)
│
├── templates/           # 대시보드 HTML
├── logs/                # auto_trader.log, scheduler_out.log 등
└── db/                  # SQLite DB 파일
```

### 2.2 데이터 흐름 (한 사이클)

```
[스케줄러] 5분마다
    └─> tasks/auto_trader.py (한 번 실행 후 종료)

[auto_trader] 티커마다:
    1. data.fetch_ohlcv()           → OHLCV 수집·DB 저장
    2. data_manager.sync_indicators_for_ticker()  → 지표 계산·technical_indicators 저장
    3. strategy.generate_comprehensive_signal_with_logging()  → Regime·신호·DB 기록
    4. signal이 buy/sell이면 executor.place_order()  → Paper 또는 Live 주문
```

### 2.3 DB 테이블 용도

| 테이블 | 용도 |
|--------|------|
| **ohlcv** | 봉 데이터 (ticker, timeframe, ts, open/high/low/close/volume) |
| **technical_indicators** | 티커·타임프레임별 지표 캐시 (EMA, RSI, ADX, BB 등) |
| **signals** | 봉 단위 신호 기록 (ticker, ts, signal 1/-1/0) |
| **analysis_results** | 상세 분석(신호, 리스크 필터, decision_reason) |
| **orders** | 체결/주문 이력 (Paper·Live 공통) |

### 2.4 실행 모드

| 모드 | 설정 | 실행기 | 비고 |
|------|------|--------|------|
| **Paper** | `TRADING_MODE=paper` (기본) | PaperExecutor | 단일 cash/position 시뮬레이션 |
| **Live** | `TRADING_MODE=live`, `LIVE_MODE=1`, `LIVE_CONFIRM="I CONFIRM LIVE"`, Upbit 키 | LiveExecutor | 실제 주문, ENABLE_AUTO_LIVE·일일손실 한도 적용 |

### 2.5 환경 변수 요약

| 변수 | 설명 |
|------|------|
| `TRADING_MODE` | paper / live |
| `ENABLE_AUTO_TRADING` | 1이면 스케줄러가 auto_trader 주기 실행 |
| `TRADING_INTERVAL_MINUTES` | 매매 사이클 주기 (기본 5) |
| `TICKERS` | 쉼표 구분 티커 (없으면 KRW 전종목 또는 DB 폴백) |
| `ACCOUNT_VALUE` | Paper 계좌 가치 (기본 100000) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 알림 봇 |
| `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY` | Live 시 업비트 API |

---

## 3. 관련 문서

- **LOGIC_VERIFICATION.md** — 로직 검증·미흡 보완 내역
- **README.md** — 설치·실행·보안 요약
