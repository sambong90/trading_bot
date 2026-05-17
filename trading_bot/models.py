"""
SQLite 스키마. 기존 DB에 컬럼/인덱스 추가 시 마이그레이션 가이드:

[가장 간단] DB 초기화 (데이터 삭제됨):
  - trading_bot/db/trading_bot.db 파일 삭제 또는 이동 후 앱/스케줄러 재시작
  - 테이블이 없으면 create_all() 등으로 자동 생성

[데이터 보존] ALTER TABLE (컬럼 추가만):
  sqlite3 trading_bot/db/trading_bot.db
  ALTER TABLE analysis_results ADD COLUMN regime VARCHAR;
  ALTER TABLE analysis_results ADD COLUMN is_defensive_mode BOOLEAN;

[인덱스만 추가]
  CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_ts ON ohlcv(ticker, ts);
  CREATE INDEX IF NOT EXISTS idx_signals_ticker_ts ON signals(ticker, ts);
  CREATE INDEX IF NOT EXISTS idx_tech_ticker_ts ON technical_indicators(ticker, ts);
  CREATE INDEX IF NOT EXISTS idx_analysis_ticker_ts ON analysis_results(ticker, timestamp);
  CREATE INDEX IF NOT EXISTS idx_snapshot_ticker_ts ON ticker_snapshots(ticker, timestamp);
"""
from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text, UniqueConstraint, Index, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class OHLCV(Base):
    __tablename__ = 'ohlcv'
    __table_args__ = (
        UniqueConstraint('ticker', 'timeframe', 'ts', name='u_ticker_timeframe_ts'),
        Index('idx_ohlcv_ticker_ts', 'ticker', 'ts'),
    )
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, index=True, nullable=False)
    timeframe = Column(String, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    source = Column(String)
    inserted_at = Column(DateTime(timezone=True), server_default=func.now())


class Signal(Base):
    __tablename__ = 'signals'
    __table_args__ = (Index('idx_signals_ticker_ts', 'ticker', 'ts'),)
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, index=True)
    timeframe = Column(String)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    signal = Column(Integer)
    algo_version = Column(String)
    params = Column(JSON)
    meta = Column(JSON)
    inserted_at = Column(DateTime(timezone=True), server_default=func.now())

class Backtest(Base):
    __tablename__ = 'backtests'
    id = Column(Integer, primary_key=True, index=True)
    run_name = Column(String)
    params = Column(JSON)
    start_ts = Column(DateTime(timezone=True))
    end_ts = Column(DateTime(timezone=True))
    final_value = Column(Float)
    metrics = Column(JSON)
    equity_ref = Column(String)  # filepath or reference
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class EquityPoint(Base):
    __tablename__ = 'equity_points'
    id = Column(Integer, primary_key=True, index=True)
    backtest_id = Column(Integer, index=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    value = Column(Float)

class Trade(Base):
    __tablename__ = 'trades'
    id = Column(Integer, primary_key=True, index=True)
    backtest_id = Column(Integer)
    ts = Column(DateTime(timezone=True), nullable=False)
    side = Column(String)
    price = Column(Float)
    qty = Column(Float)
    fee = Column(Float)
    raw = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String, index=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    side = Column(String)
    price = Column(Float)
    qty = Column(Float)
    status = Column(String)
    fee = Column(Float)
    raw = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class TechnicalIndicator(Base):
    __tablename__ = 'technical_indicators'
    __table_args__ = (
        UniqueConstraint('ticker', 'timeframe', 'ts', name='u_tech_ticker_timeframe_ts'),
        Index('idx_tech_ticker_ts', 'ticker', 'ts'),
    )
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, index=True, nullable=False)
    timeframe = Column(String, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    sma_short = Column(Float)
    sma_long = Column(Float)
    ema_short = Column(Float)
    ema_long = Column(Float)
    rsi = Column(Float)
    atr = Column(Float)
    volume_ma = Column(Float)
    indicators = Column(JSON)  # adx, bb_lower, bb_middle, bb_upper, atr_raw 등
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AnalysisResult(Base):
    __tablename__ = 'analysis_results'
    __table_args__ = (Index('idx_analysis_ticker_ts', 'ticker', 'timestamp'),)
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, index=True, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    signal = Column(String)
    price = Column(Float)
    change_rate = Column(Float)
    change_price = Column(Float)
    high_24h = Column(Float)
    low_24h = Column(Float)
    volume_24h = Column(Float)
    trade_price_24h = Column(Float)
    analysis_data = Column(JSON)
    risk_filters = Column(JSON)
    position_size = Column(Float)
    decision_reason = Column(Text)
    regime = Column(String)  # 추세/횡보장 여부 (쿼리 통계용)
    is_defensive_mode = Column(Boolean)  # 방어 모드 여부 (쿼리 통계용)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TickerSnapshot(Base):
    __tablename__ = 'ticker_snapshots'
    __table_args__ = (Index('idx_snapshot_ticker_ts', 'ticker', 'timestamp'),)
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, index=True, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    current_price = Column(Float)
    change_rate = Column(Float)
    change_price = Column(Float)
    high_24h = Column(Float)
    low_24h = Column(Float)
    volume_24h = Column(Float)
    trade_price_24h = Column(Float)
    prev_closing_price = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TuningRun(Base):
    __tablename__ = 'tuning_runs'
    id = Column(Integer, primary_key=True, index=True)
    combo = Column(JSON)
    metrics = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PositionState(Base):
    """Scale-out stage, avg buy price, and trailing high per ticker."""
    __tablename__ = 'position_states'
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, unique=True, index=True, nullable=False)
    stage = Column(Integer, default=0)
    avg_buy_price = Column(Float, default=0.0)
    trailing_high = Column(Float, default=0.0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SystemState(Base):
    """Persistent key-value store for system-wide control flags.

    Survives Kubernetes pod restarts (unlike .env file writes to ephemeral storage).
    Used by the panic endpoint and LiveExecutor env-watcher to persist and read
    the ENABLE_AUTO_LIVE flag across process restarts.

    Common keys:
      'enable_auto_live' — '1' (trading active) or '0' (panic / halted)
    """
    __tablename__ = 'system_state'
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ExecutionEvent(Base):
    """Tracks trade execution events for cooldown management.

    [L3 FIX] Replaces the decision_reason tag-scanning on AnalysisResult.
    Tags: EXEC_BUY, EXEC_SELL, DCA_BUY, PS1, PS2.
    Indexed on (ticker, ts) and tag for efficient recency/count queries.

    Migration note:
      CREATE TABLE execution_events (...) — handled by create_all() on first run.
      Existing DB: trading_bot/db/trading_bot.db 삭제 후 재시작, 또는
        sqlite3 trading_bot.db < schema_add_execution_events.sql
    """
    __tablename__ = 'execution_events'
    __table_args__ = (Index('idx_exec_events_ticker_ts', 'ticker', 'ts'),)
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, index=True, nullable=False)
    tag = Column(String, index=True, nullable=False)
    signal = Column(String)
    price = Column(Float)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    meta = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MacroSnapshot(Base):
    """거시 지표 일봉 스냅샷 — L1 글로벌 필터(G-01~G-15) 평가용.

    수집 주기: 매일 KST 07:00 (NYSE 정규장 종가 확정 후)
    ratio_quality='stale' 시 EM-7 규칙에 따라 트리거 실행 금지.

    Migration:
      PostgreSQL: CREATE TABLE macro_snapshots (...) — create_all()로 자동 생성.
      컬럼 추가 시: ALTER TABLE macro_snapshots ADD COLUMN <name> <type>;
    """
    __tablename__ = 'macro_snapshots'
    __table_args__ = (
        UniqueConstraint('ts', name='u_macro_ts'),
        Index('idx_macro_ts', 'ts'),
    )
    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)

    # ── Raw values (Yahoo Finance 종가) ──────────────────────────────
    dxy_value = Column(Float)           # ^DXY
    nasdaq_value = Column(Float)        # ^NDX
    gold_value = Column(Float)          # GC=F
    us10y_yield = Column(Float)         # ^TNX (%)
    us30y_yield = Column(Float)         # ^TYX (%)
    usdjpy_value = Column(Float)        # USDJPY=X
    oil_value = Column(Float)           # CL=F

    # ── 1d 변동률 (%) ────────────────────────────────────────────────
    dxy_1d_pct = Column(Float)
    nasdaq_1d_pct = Column(Float)
    gold_1d_pct = Column(Float)
    usdjpy_1d_pct = Column(Float)

    # ── 교환비 (nasdaq_composite / dxy) ─────────────────────────────
    # 기준: master_strategy_filtered.md Section 50+196
    # NEUTRAL=215~244(1:230), NORMAL=245~364, ELEVATED=365~439,
    # BUBBLE=440~559, EXTREME_BUBBLE≥560
    nasdaq_dxy_ratio = Column(Float)
    nasdaq_dxy_zone = Column(String)    # LOW|NEUTRAL|NORMAL|ELEVATED|BUBBLE|EXTREME_BUBBLE

    # ── 채권 신호 ────────────────────────────────────────────────────
    bond_ratio = Column(Float)          # us10y / us30y (역전 감지)
    bond_signal = Column(String)        # BULL_MARKET|BEAR_MARKET_1|SECONDARY_DROP_IMMINENT

    # ── 위기 복합 신호 ───────────────────────────────────────────────
    dxy_zone = Column(String)           # DXY_GREEN_ZONE|NEUTRAL|WEAK (G-01)
    gold_crisis_signal = Column(String) # SEVERE_CRISIS|PRE_CRISIS|NORMAL (G-13)
    crisis_level = Column(String)       # SUPER_CRISIS|PRE_CRISIS|NORMAL (G-02, dxy_gold_nasdaq)
    jpy_signal = Column(String)         # ASIA_INSTABILITY|STABLE (G-12)
    oil_vol_active = Column(Boolean, default=False)  # G-07

    # ── 데이터 품질 ──────────────────────────────────────────────────
    ratio_quality = Column(String, default='fresh')  # fresh|stale (EM-7)
    data_source = Column(String, default='yahoo_finance')
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DominanceSnapshot(Base):
    """BTC 도미넌스 4h 스냅샷 — L2 장세 분류(R-09~R-16) 평가용.

    수집 주기: 매 4시간 (APScheduler)
    event_signal: 직전 snapshot 대비 임계값 크로스 여부 — dominance_event_signal() 구현 핵심.

    Migration: create_all()로 자동 생성.
    """
    __tablename__ = 'dominance_snapshots'
    __table_args__ = (
        UniqueConstraint('ts', 'timeframe', name='u_dominance_ts_tf'),
        Index('idx_dominance_ts', 'ts'),
    )
    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    timeframe = Column(String, default='4h')  # 4h|1d

    # ── Raw dominance (%) ────────────────────────────────────────────
    btc_dominance = Column(Float)       # e.g., 55.20
    eth_dominance = Column(Float)       # e.g., 12.10
    alt_dominance = Column(Float)       # = 100 - btc - eth

    # ── 갭 계산 (양수=임계값 위, 음수=하향 돌파) ─────────────────────
    # 임계값 출처: core_logic_distilled.md R-09~R-16, X-05
    gap_to_63_75 = Column(Float)        # ALT_ENTRY_CONFIRMED (R-13)
    gap_to_60_00 = Column(Float)        # BULL_FULLY_CONFIRMED (R-12)
    gap_to_58_85 = Column(Float)        # BULL_START_TRIGGER (R-09)
    gap_to_50_00 = Column(Float)        # ALT_MASSACRE 기준 (G-14)
    gap_to_41_55 = Column(Float)        # DOM_REVERSAL_UP / ALT EXIT (X-05)
    gap_to_40_00 = Column(Float)        # BULL_CLIMAX_ZONE (R-15)

    # ── 단계 분류 ────────────────────────────────────────────────────
    bull_stage = Column(String)         # NO_BULL|BULL_WATCHING|BULL_EARLY|BULL_CONFIRMED|BULL_CLIMAX_ZONE
    event_signal = Column(String)       # 직전 snapshot 대비 크로스 이벤트 (None 가능)
    # BULL_START_TRIGGER|BULL_FULLY_CONFIRMED|ALT_ENTRY_CONFIRMED|DOM_REVERSAL_UP

    data_source = Column(String, default='coingecko')
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class KimpSnapshot(Base):
    """김치프리미엄 실시간 스냅샷 — E-04/E-11/E-20 kimp_signal() 평가용.

    수집 주기: 매 거래 사이클 (60분)
    kimp_pct = ((btc_krw / (btc_usd × usdkrw)) - 1) × 100

    Migration: create_all()로 자동 생성.
    """
    __tablename__ = 'kimp_snapshots'
    __table_args__ = (Index('idx_kimp_ts', 'ts'),)
    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)

    # ── Raw prices ───────────────────────────────────────────────────
    btc_krw = Column(Float)             # Upbit KRW-BTC 현재가
    btc_usd = Column(Float)             # Binance BTC/USDT 현재가
    usdkrw = Column(Float)              # USD/KRW 환율

    # ── 파생 지표 ────────────────────────────────────────────────────
    kimp_pct = Column(Float)            # 김치프리미엄 %
    kimp_signal = Column(String)        # KOREAN_REVERSE_PREMIUM_BUY|BOTTOM_LIKELY|NEUTRAL|PREMIUM

    data_source = Column(String, default='upbit_binance')
    created_at = Column(DateTime(timezone=True), server_default=func.now())

