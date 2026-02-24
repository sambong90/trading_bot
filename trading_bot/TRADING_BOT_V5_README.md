## System Overview

**Quantitative Trading Bot V5.0**는 업비트 KRW 마켓을 대상으로 동작하는 **완전 자동화형 암호화폐 트레이딩 시스템**입니다.  
이 봇은 다음을 통합한 프로덕션급 아키텍처를 갖습니다:

- 1시간봉 기반 **레짐 인식(Trend / Weakening Trend / Range) 전략 엔진**
- **OBV + 볼린저 밴드 스퀴즈**를 활용한 스마트 머니(고래) 매집 감지
- BTC 글로벌 추세 필터와 고 ADX 디커플링 예외 규칙
- **Regime-Dependent Risk Parity** 기반 동적 포지션 사이징
- **Scale-Out(25–25–50)** 부분 익절 매니저 (SQLite 상태 유지)
- 주간 **Walk-Forward 튜닝 파이프라인**을 통한 자동 파라미터 최적화
- **Telegram 챗봇**과 스케줄된 마켓 브리핑을 통한 상시 모니터링
- APScheduler 기반 **완전 자동 스케줄러** (트레이딩, DB 하우스키핑, 튜너, 브리핑)

---

## Core Trading Strategies

### Trend Breakout with Smart Volume Filter

핵심 전략은 `strategy.py` 의 `generate_comprehensive_signal_with_logging()` 에 구현되어 있습니다.

- **타임프레임**: 1시간봉 (`minute60`)
- **주요 지표**
  - EMA: `ema_short`, `ema_long` (기본 12/26, `param_manager.py`를 통해 동적으로 조정)
  - ADX (추세 강도, regime 판별)
  - RSI (과매수/과매도 필터)
  - ATR (변동성 및 트레일링 스탑)
  - Bollinger Bands (20, 2σ)

**Regime 정의 (`ADX` 기반)**

- `trend`: `ADX > adx_trend_threshold` 이고 ADX 기울기 ≥ 0  
- `weakening_trend`: `ADX > adx_trend_threshold` 이고 ADX 기울기 < 0  
- `range`: `ADX ≤ adx_trend_threshold`

**Trend Regime 매수 로직 (EMA Breakout + Smart Volume)**

- 조건:
  - EMA 골든 크로스:  
    - 현재 봉: `ema_short > ema_long`  
    - 직전 봉: `ema_short_prev <= ema_long_prev`
  - 상위 타임프레임(일봉) 필터 통과:  
    - `load_higher_timeframe_indicators()` (일봉 EMA 50 기반)  
    - 상위 봉이 하락장일 경우 매수 신호 보류
  - Smart Volume 필터:
    - 현재 봉 거래량 `current_volume`
    - 20봉 거래량 평단 `volume_ma`
    - 상승장: `current_volume >= volume_ma * 0.8` (또는 Regime에 따라 1.2x 등)
  - 추가적으로 RSI < 70 (과매수 방지)

- 매도:
  - EMA 데드 크로스(단기 EMA가 장기 EMA 하회)
  - ATR 기반 트레일링 스탑:  
    - 최근 20봉 최고가 – 2.5 × ATR 아래로 가격이 내려갈 경우 **전량 청산**

**Smart Volume 디버깅 정보**

- 모든 결정에는 `vol_ratio = current_volume / volume_ma` 가 계산되어
  - `decision_reason`에 `"[Vol: 1.3x]"` 형태로 항상 기록됩니다.
- 이는 `analysis_results` 테이블과 AI 로그 (`ai_debug.log`) 에서 전략 동작을 정밀 디버깅하기 위함입니다.

---

### Smart Money (Whale) Accumulation Detection (OBV + BB Squeeze)

`data_manager.py` 와 `strategy.py` 에 구현된 **V5.0 Accumulation 전략**입니다.

**지표 확장 (`data_manager.py`)**

- `compute_indicators()`에서 추가 계산:
  - **OBV**:  
    - `OnBalanceVolumeIndicator(close=df['close'], volume=df['volume'])`
    - `df['obv'] = indicator_obv.on_balance_volume()`
  - **OBV SMA (20)**:
    - `df['obv_sma'] = df['obv'].rolling(window=20).mean()`
  - **Bollinger Band Width**:
    - `df['bb_width'] = (bb_upper - bb_lower) / bb_middle`
- 이 값들은 `technical_indicators.indicators` JSON 필드에 `obv`, `obv_sma`, `bb_width` 로 저장되고,
  `strategy.load_cached_indicators()` 에서 다시 로드됩니다.

**Accumulation 조건 (`strategy.generate_comprehensive_signal_with_logging`)**

1. **Squeeze (레이인지 + 극단적 압축)**  
   - `regime == 'range'` 또는 `adx < adx_trend_threshold`
   - `bb_width < 0.05`  
   → 밴드 폭이 5% 미만인 “숨 고르기” 구간

2. **Smart Money Flow (OBV 상승)**  
   - `obv > obv_sma`  
   → 가격은 크게 움직이지 않지만 거래량 흐름(OBV)이 우상향 → 조용한 매집

3. **Safe Entry (하단부 진입)**  
   - `current_price <= bb_mid`  
   → 스퀴즈 구간의 하단/중단 이하에서 진입 (리스크 대비 유리한 가격)

**Accumulation 신호 처리**

- 위 3가지 조건이 모두 충족되면:
  - `signal = 'buy'`
  - `accumulation_mode = True`
  - `decision_reason`에 다음 메시지 추가:
    - `"[Accumulation Detected] BB_Width: 0.031 < 0.05, OBV > OBV_SMA. Pre-empting breakout with 50% size."`
  - 이후 리스크 엔진에서 **포지션 크기를 절반(50%)** 으로 자동 축소:
    - `position_size = position_size * 0.5`  
      → 선발대(Probe) 진입으로 추세 돌파 전 조기 포지셔닝

이 로직은 **브레이크아웃 전략과 독립적인 “평행 전략”** 으로 동작하며,  
조용한 매집+스퀴즈 구간에서 **강한 트렌드 전 “프론트 런”** 을 수행합니다.

---

### Bear Market Decoupling (Racehorse Bypass with High ADX)

BTC가 하락장일 때, 일반적으로 알트 신규 매수를 차단하지만,  
특정 종목이 **고 ADX(강한 독립 추세)** 를 보이는 경우 **“경주마”로 간주해 예외 매수**를 허용합니다.

`strategy.py` 주요 로직:

- BTC 글로벌 장세 필터(`is_global_bull_market == False`) 인 상황에서:
  - 기본적으로 모든 **buy 신호 → hold** (매수 차단)
  - 단,  
    - 해당 종목의 ADX ≥ 40  
    - 거래량이 평단 대비 충분히 높은 경우 (예: `volume >= volume_ma * 1.5`)  
  → **예외적으로 50% 축소 포지션으로 buy 허용**

이 로직을 통해:

- 전체 시장이 하락장(Bear)일 때도
- **극소수의 초강세 종목(디커플링된 경주마)** 에는 제한된 규모로 진입하여 기회를 포착합니다.

---

## Dynamic Risk Management

### Regime-Dependent Dynamic Position Sizing (Risk Parity)

동적 포지션 사이징은 `tasks/auto_trader.py` 에 구현되어 있으며,  
BTC 레짐 + 개별 코인 ATR에 기반해 **리스크 균형 포지션**을 자동 산출합니다.

**총 계좌 평가액 계산**

```python
total_equity = compute_total_account_equity(executor, tickers)
# = 가용 KRW + Σ(각 티커 보유수량 × 현재가)
```

**리스크 한도 (Regime Dependent)**

- BTC가 상승장(`is_global_bull_market=True`)일 때:
  - `risk_pct = 0.05` (계좌의 5%를 한 트레이드 리스크 허용)
- BTC가 하락/애매한 장세일 때:
  - `risk_pct = 0.02` (보수적 2% 리스크)

```python
risk_amount = total_equity * risk_pct
```

**변동성 기반 손절 거리 (ATR 사용)**

- 기본: `sl_distance = atr * 2.0` (2 ATR 손절 가정)
- 안전 폴백:
  - ATR이 0이거나 NaN일 경우: `sl_distance = current_price * 0.05` (5% 손절 가정)

**목표 포지션 크기 (위험 기준)**

```python
target_quantity = risk_amount / sl_distance
base_buy_krw = target_quantity * current_price
```

**안전 범위 + 전략 가중치 적용**

- 코인당 최대 할당: `max_per_coin = total_equity * 0.20` (20%)
- 최소 주문 금액: 5,000 KRW (업비트 룰)
- 경계 적용:
  - `bounded_buy_krw = min(max(base_buy_krw, 5000), max_per_coin)`
- 전략 가중치(size_pct, 예: 0.5 = Accumulation, 1.0 = Breakout):
  - `final_buy_krw = bounded_buy_krw * size_pct`

실제 주문은 `final_buy_krw` 와 현재 가용 현금 중 작은 값으로 제한되며,
`ai_debug.log` 에 다음과 같이 상세하게 기록됩니다:

```text
[Dynamic Sizing] KRW-BTC | RegimeRisk: 5.0% | ATR: 123000.00 | SL_Dist: 246000.00 | CalcKRW: 150000.00 | size_pct: 1.00
```

---

### Asymmetric Circuit Breaker for Extreme Volatility

**여러 레벨의 “비대칭 서킷 브레이커”** 가 존재합니다:

- **BTC 글로벌 필터 (auto_trader.check_btc_global_trend)**  
  - BTC가 EMA 50 하회 또는 EMA20/EMA50 데드크로스 상태일 때
    - **모든 신규 매수 차단** (매도만 허용)
  - 장세가 회복되면 자동으로 매수 재개

- **LiveExecutor 일일 손실 한도 (`executor.LiveExecutor._daily_loss_exceeded`)**
  - `MAX_DAILY_LOSS_KRW` (기본 50,000원, 환경 변수로 조정)
  - 당일 실현/미실현 손익 + 추가 진입 예정 금액을 고려하여
    - 계좌가 특정 손실 한도를 넘으면 **추가 매수 전면 차단**

- **Regime 기반 Risk Parity**
  - Bull: 트레이드당 최대 5% 위험
  - Bear: 2%로 자동 축소
  - Bear 상태에서는 신규 매수 자체를 막거나, 예외 경주마 전략에만 제한적으로 허용

이 세 가지가 합쳐져, **급변 장세에서 계좌가 한 번에 무너지지 않도록**  
“완만하게 진입하고 빠르게 방어하는” 구조를 형성합니다.

---

### Scale-Out Manager (25–25–50 Profit Taking)

부분 익절(Scale-Out)은 `scale_out_manager.py` 와 `strategy.py` 의 협업으로 구현됩니다.

- 상태 저장 테이블: `position_states` (SQLite)
  - 컬럼: `ticker`, `stage`, `avg_buy_price`, `updated_at`
  - stage:
    - 0: 미스케일
    - 1: 1차 익절(25% 청산 완료)
    - 2: 2차 익절(추가 25% 청산 완료)

**전략 로직 (대략)**

- 현재 ROI 기반:
  - `current_roi >= 5%` & stage < 1:
    - `signal = 'sell'`, `sell_size_pct ≈ 0.25`, `next_scale_out_stage = 1`
  - `current_roi >= 10%` & stage < 2:
    - `signal = 'sell'`, `sell_size_pct ≈ 0.33` (남은 75% 중 약 25%),
      `next_scale_out_stage = 2`
- 전량 청산 (트레일링 스탑 or EMA Dead Cross) 시:
  - stage를 0으로 리셋

이를 통해 **익절 시점은 분할·보수적으로, 손절은 일괄·신속하게** 수행되는  
**비대칭 Profit Taking 구조**를 구현합니다.

---

## Self-Evolving AI Engine

### Weekly Walk-Forward Optimization (V4.0 튜너)

**자동 튜닝 파이프라인**

- 실행 파일: `tasks/auto_tuner.py`
- 전략:
  - KRW-BTC, KRW-SOL 에 대해 최근 30일(720시간) 1h OHLCV 수집
  - `tuner.grid_search()` 를 사용해
    - EMA 파라미터(예: short/long)
    - ADX 임계값 등 조합을 그리드 서치
  - 각 조합에 대해:
    - `_strategy_fn_ema_regime()` → 신호 생성
    - `backtest.simple_backtest()` → 최종 자산, MDD, Sharpe 등 평가
  - 가장 좋은 조합을 `TuningRun` 테이블에 저장

**동적 파라미터 공급 (`param_manager.py`)**

- `get_best_params()`:
  - `TuningRun` 을 `created_at DESC` 순으로 조회
  - 최신 run 의 `combo` 를 기본 파라미터에 merge
  - 실패 시 안전하게 `_DEFAULT_PARAMS` (EMA 12/26, RSI 14, ATR 14, ADX 임계 25) 사용

**스케줄링 (`scheduler_service.py`)**

```python
# Walk-Forward 튜닝: 매주 일요일 04:00
sched.add_job(run_auto_tuner, 'cron', hour=4, minute=0, day_of_week='sun', id='auto_tuner')
```

→ 매주 일요일 새벽 4시에 **자동으로 튜너를 작동**시켜  
전략 파라미터가 **시장 환경에 맞게 지속적으로 진화**하도록 합니다.

---

## Interactive Telegram Assistant

Telegram 봇은 `telegram_bot.py` 에 구현되어 있으며,  
**장기 폴링 방식(long polling)** 으로 사용자 명령을 처리합니다.

### 핵심 명령어

- `/start`  
  - 봇 소개 및 사용 가능한 명령어 목록 출력
- `/status`  
  - `progress.json` 기반 현재 단계/진행률 표시
- `/balance`  
  - Paper / Live 모드에 따라:
    - KRW 잔고
    - 보유 종목 수량 / 현재가 / ROI / 평가액
- `/report`  
  - 오늘 하루 체결 건수 및 실현 P&L 요약
- `/panic`  
  - `.env` 내 `ENABLE_AUTO_LIVE=0` 으로 강제 설정
  - 실거래 모드에서 **자동 매매 즉시 중지용 Panic 버튼**

### Scheduled Market Briefings

- **Market Briefing 함수**: `telegram_bot.send_briefing()`
  - BTC 글로벌 레짐 (`🟢 Bull / 🔴 Bear`)
  - 계좌 총액 및 ROI
  - 최근 24시간 매매 건수 및 P&L (Trade 테이블 기준)
  - ADX 상위 3개 티커 안내

- **스케줄링 (`tasks/market_briefing.py` + `scheduler_service.py`)**

```python
# Market Briefing: 09:00 (업비트 일일 리셋) + 4시간마다 (00, 04, 08, 12, 16, 20)
sched.add_job(run_market_briefing, 'cron', hour='0,4,8,9,12,16,20', minute=0, id='market_briefing')
```

→ 전략이 백그라운드에서 돌아가는 동안, 사용자는 **텔레그램으로 상태, 계좌, 브리핑을 실시간 확인**할 수 있습니다.

---

## Project Structure

```text
trading_bot/
├── strategy.py            # Regime 판단, 신호 생성, signals/analysis_results 기록
├── data.py                # OHLCV 수집 (Upbit API), DB 저장, 티커 목록
├── data_manager.py        # EMA/RSI/ATR/ADX/BB/OBV/BB Width 계산, technical_indicators 동기화
├── risk.py                # 리스크 유틸 (포지션 크기 기본 계산 등)
├── executor.py            # PaperExecutor / LiveExecutor, 실제/가상 주문 실행
├── models.py              # SQLAlchemy 모델 (OHLCV, Signal, Order, TechnicalIndicator, AnalysisResult, TuningRun, PositionState 등)
├── db.py                  # DB 세션/엔진 (SQLite + timeout 설정)
├── monitor.py             # 단방향 Telegram 알림(send_telegram)
├── telegram_bot.py        # 양방향 Telegram 챗봇 (명령/브리핑)
├── main.py                # Flask 대시보드 (/status, /decisions, /account, /logs 등)
├── backtest.py            # 간단 백테스트 및 메트릭(CAGR, MDD, Sharpe)
├── tuner.py               # 그리드 서치 튜너 (Walk-Forward 엔진에서 사용)
├── ai_logger.py           # AI/전략 디버깅 전용 로그 (ai_debug.log)
│
├── tasks/
│   ├── scheduler_service.py  # APScheduler 메인: auto_trader, db_maintenance, auto_tuner, market_briefing
│   ├── auto_trader.py        # 1회 매매 사이클: 티커 루프, 신호 생성, 주문/통계/텔레그램
│   ├── auto_tuner.py         # 주간 Walk-Forward 튜너 (일요일 04:00)
│   ├── market_briefing.py    # 텔레그램 마켓 브리핑 트리거
│   ├── db_maintenance.py     # DB 하우스키핑 (7/30/90일 초과 데이터 정리)
│   ├── auto_summary.py       # (옵션) 상태 요약 및 알림
│   ├── state_updater.py      # current_phase.json 관리
│   └── progress.py           # progress.json 관리
│
├── logs/                    # auto_trader.log, scheduler_out.log, ai_debug.log, cache 등
├── db/                      # SQLite DB 파일 (trading_bot.db)
├── templates/               # Flask 대시보드 HTML
└── requirements.txt         # Python 패키지 의존성 목록
```

---

## Getting Started / Setup Requirements

### 환경 준비

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -r trading_bot/requirements.txt
```

### `.env` 설정 (`trading_bot/.env`)

최소 필요 변수:

```env
# 모드 / 주기
TRADING_MODE=paper            # 또는 live
ENABLE_AUTO_TRADING=1         # 스케줄러에서 auto_trader 실행 허용
TRADING_INTERVAL_MINUTES=5    # 매매 사이클 간격 (분)

# Paper 모드 가상 계좌 가치
ACCOUNT_VALUE=100000

# Upbit API (Live 모드용)
UPBIT_ACCESS_KEY=...
UPBIT_SECRET_KEY=...
LIVE_MODE=0                   # 1로 설정 시 LiveExecutor 활성화
LIVE_CONFIRM=I CONFIRM LIVE   # 실수 방지용 수기 플래그
ENABLE_AUTO_LIVE=0            # 1이면 Live auto-trading 허용

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# 선택: 티커 및 튜닝/리스크 파라미터
# TICKERS=KRW-BTC,KRW-ETH,KRW-SOL
# MAX_DAILY_LOSS_KRW=50000
```

> **실거래(Live) 활성화 전에는** 반드시 Paper 모드에서 로그/전략 동작을 충분히 검증하십시오.

### 스케줄러 & 대시보드 실행

**1) Flask 대시보드 (선택)**

```bash
cd trading_bot
python main.py
# 기본 포트 5000 → http://localhost:5000 에서 상태/로그/분석 결과 확인
```

**2) 스케줄러 (실제 자동 매매 루프)**

```bash
cd /Users/sambong.ai/.openclaw/workspace
.venv/bin/python -m trading_bot.tasks.scheduler_service
```

- 이 프로세스는:
  - `ENABLE_AUTO_TRADING=1` 이면 5분마다 `auto_trader.py` 실행
  - 매일 03:00 DB 하우스키핑
  - 매주 일요일 04:00 Walk-Forward 튜닝
  - 지정된 시간대에 Market Briefing 전송

### Telegram 봇 실행

```bash
cd trading_bot
python telegram_bot.py
# /start, /status, /balance, /report, /panic 명령 사용 가능
```

---

이 README는 현재 V5.0 코드베이스(`strategy.py`, `auto_trader.py`, `data_manager.py`, `param_manager.py`, `telegram_bot.py`, `scheduler_service.py` 등)를 기준으로 작성되었습니다.  
실거래 전에는 반드시 **Paper 모드에서 충분한 기간 테스트 및 검증 후** 사용하세요.

