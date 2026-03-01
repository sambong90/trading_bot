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
    """Scale-out stage and avg buy price per ticker. Used for 25-25-50 partial sell state."""
    __tablename__ = 'position_states'
    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, unique=True, index=True, nullable=False)
    stage = Column(Integer, default=0)
    avg_buy_price = Column(Float, default=0.0)
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

