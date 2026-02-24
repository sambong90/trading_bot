
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from urllib.parse import urlparse

DB_URL = os.environ.get('DB_URL', 'sqlite:///./trading_bot/db/trading_bot.db')

# SQLite 동시 접근 시 DB Lock 문제를 줄이기 위해 timeout을 명시적으로 부여
if DB_URL.startswith('sqlite'):
    connect_args = {"check_same_thread": False, "timeout": 30}
else:
    connect_args = {}

engine = create_engine(DB_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_session():
    return SessionLocal()


def ensure_tables():
    """Create all tables (including position_states) if they do not exist."""
    from trading_bot.models import Base
    Base.metadata.create_all(bind=engine)

