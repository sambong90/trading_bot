from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class OHLCV(Base):
    __tablename__ = 'ohlcv'
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
    __table_args__ = (UniqueConstraint('ticker','timeframe','ts', name='u_ticker_timeframe_ts'),)

class Signal(Base):
    __tablename__ = 'signals'
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

class TuningRun(Base):
    __tablename__ = 'tuning_runs'
    id = Column(Integer, primary_key=True, index=True)
    combo = Column(JSON)
    metrics = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
