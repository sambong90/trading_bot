import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_URL = os.environ.get('DB_URL', 'sqlite:///./trading_bot/db/trading_bot.db')

if DB_URL.startswith('sqlite'):
    # SQLite: 단일 파일 DB, 동시 접근 timeout 설정 (로컬 개발·테스트용)
    connect_args = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(DB_URL, connect_args=connect_args)
else:
    # PostgreSQL: 커넥션 풀링 + pre-ping(연결 단절 감지)
    engine = create_engine(
        DB_URL,
        pool_pre_ping=True,   # 끊긴 연결을 자동 감지·교체
        pool_size=5,          # 상시 유지 연결 수
        max_overflow=10,      # 피크 시 추가 허용 연결 수
        pool_recycle=1800,    # 30분마다 연결 재생성 (방화벽 타임아웃 방지)
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_session():
    return SessionLocal()


def ensure_tables():
    """Create all tables if they do not exist."""
    from trading_bot.models import Base
    Base.metadata.create_all(bind=engine)
