#!/usr/bin/env python3
"""
SQLite → PostgreSQL 데이터 마이그레이션 스크립트

사전 요건:
  - PostgreSQL이 실행 중이어야 함 (k8s: postgres StatefulSet Ready)
  - psycopg2-binary 설치: pip install psycopg2-binary
  - SQLite DB 파일 접근 가능 (로컬 또는 PVC 마운트)

사용법 (로컬):
  SQLITE_PATH=trading_bot/db/trading_bot.db \\
  DB_URL=postgresql://botuser:패스워드@localhost:5432/trading_bot \\
  python3 scripts/migrate_sqlite_to_postgres.py

사용법 (k8s Job):
  kubectl apply -f k8s/migrate-job.yaml

옵션:
  --dry-run   실제 쓰기 없이 읽기·카운트만 수행 (마이그레이션 미리보기)
  --force     이미 데이터가 있는 테이블도 강제 삽입 (ON CONFLICT DO NOTHING 적용)
"""
import sys
import os
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# ── 프로젝트 루트를 sys.path에 추가 ────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── 마이그레이션 순서 (참조 관계 고려) ─────────────────────────────────────
# backtests → equity_points, trades (backtest_id 참조)
# 나머지는 독립 테이블
MIGRATION_ORDER = [
    'ohlcv',
    'signals',
    'backtests',
    'equity_points',
    'trades',
    'orders',
    'technical_indicators',
    'analysis_results',
    'ticker_snapshots',
    'tuning_runs',
    'position_states',
]


def make_aware(dt):
    """naive datetime → UTC-aware datetime으로 변환 (PostgreSQL timezone 컬럼 대비)."""
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _clean_nan(obj):
    """dict/list 내부의 float NaN/Inf를 재귀적으로 None으로 교체.

    PostgreSQL JSON은 RFC 7159를 엄격히 따르므로 NaN/Infinity 불허.
    Python의 json.loads는 NaN을 허용하지만 PostgreSQL은 거부함.
    """
    import math
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    return obj


def sanitize_json(val):
    """SQLite JSON 컬럼 값을 PostgreSQL JSON 호환 형태로 변환.

    SQLite는 JSON을 TEXT로 저장하므로 검증 없이 아무 값이나 들어올 수 있음.
    - 이미 dict/list이면 NaN 정제 후 반환 (SQLAlchemy가 이미 파싱한 경우)
    - 문자열이면 json.loads 후 NaN 정제 → 실패 시 None 반환
    - 빈 문자열, 'None', 'null' 등은 None 처리
    """
    import json
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        # SQLAlchemy가 이미 파싱했지만 NaN float이 남아있을 수 있음
        return _clean_nan(val)
    if isinstance(val, str):
        stripped = val.strip()
        if stripped in ('', 'None', 'null', 'NULL'):
            return None
        try:
            # Python json.loads는 NaN 허용 → 파싱 후 반드시 _clean_nan 적용
            return _clean_nan(json.loads(stripped))
        except (json.JSONDecodeError, ValueError):
            logger.warning('    JSON 파싱 실패, NULL로 대체: %.80r', stripped)
            return None
    return val


def row_to_dict(table, row):
    """SQLAlchemy Row → dict 변환. datetime 타임존 보정 및 JSON 컬럼 정제 적용."""
    from sqlalchemy import JSON, Text
    d = {}
    for col in table.columns:
        val = getattr(row, col.name, None)
        # DateTime 컬럼: naive → aware 변환
        if val is not None and hasattr(val, 'tzinfo'):
            val = make_aware(val)
        # JSON 컬럼: 유효하지 않은 값 정제
        elif isinstance(col.type, JSON):
            val = sanitize_json(val)
        d[col.name] = val
    return d


def reset_sequence(pg_conn, table_name: str):
    """bulk insert 후 PostgreSQL 시퀀스를 현재 최댓값 + 1로 리셋.
    미리셋 시 다음 INSERT가 'duplicate key' 오류를 낼 수 있음."""
    from sqlalchemy import text
    pg_conn.execute(text(
        f"SELECT setval("
        f"  pg_get_serial_sequence('{table_name}', 'id'),"
        f"  COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1,"
        f"  false"
        f")"
    ))


def migrate_table(sqlite_engine, pg_engine, table, table_name: str,
                  dry_run: bool, force: bool, batch_size: int = 500) -> int:
    """단일 테이블을 SQLite에서 PostgreSQL로 마이그레이션.

    Returns:
        삽입(또는 스킵)된 행 수
    """
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    # ── SQLite에서 전체 행 읽기 ──────────────────────────────────────────
    with sqlite_engine.connect() as src:
        result = src.execute(table.select())
        rows = result.fetchall()

    if not rows:
        logger.info('  [%-25s] 행 없음 — 건너뜀', table_name)
        return 0

    logger.info('  [%-25s] SQLite 행 수: %d', table_name, len(rows))

    if dry_run:
        logger.info('  [%-25s] --dry-run: 쓰기 생략', table_name)
        return len(rows)

    # ── 이미 데이터가 있는 경우 확인 ────────────────────────────────────
    with pg_engine.connect() as dst:
        existing = dst.execute(text(f'SELECT COUNT(*) FROM {table_name}')).scalar()

    if existing > 0 and not force:
        logger.warning(
            '  [%-25s] PostgreSQL에 이미 %d 행 존재. '
            '--force 없이 건너뜀 (데이터 중복 방지)',
            table_name, existing
        )
        return 0

    # ── 배치 삽입 (ON CONFLICT DO NOTHING) ──────────────────────────────
    dicts = [row_to_dict(table, r) for r in rows]
    inserted = 0

    with pg_engine.begin() as dst:
        for i in range(0, len(dicts), batch_size):
            batch = dicts[i:i + batch_size]
            stmt = pg_insert(table).values(batch).on_conflict_do_nothing()
            result = dst.execute(stmt)
            inserted += result.rowcount if result.rowcount >= 0 else len(batch)
            logger.info(
                '  [%-25s] 배치 %d/%d 완료 (%d행)',
                table_name,
                min(i + batch_size, len(dicts)),
                len(dicts),
                len(batch),
            )
        # 시퀀스 리셋 (다음 INSERT의 id 충돌 방지)
        reset_sequence(dst, table_name)

    return inserted


def main():
    parser = argparse.ArgumentParser(description='SQLite → PostgreSQL 데이터 마이그레이션')
    parser.add_argument(
        '--sqlite-path',
        default=os.environ.get('SQLITE_PATH', 'trading_bot/db/trading_bot.db'),
        help='SQLite DB 파일 경로 (기본: SQLITE_PATH 환경변수 또는 trading_bot/db/trading_bot.db)',
    )
    parser.add_argument(
        '--db-url',
        default=os.environ.get('DB_URL', ''),
        help='PostgreSQL 연결 URL (기본: DB_URL 환경변수)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='실제 쓰기 없이 행 수만 출력',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='이미 데이터가 있는 테이블도 강제 삽입 (ON CONFLICT DO NOTHING 적용)',
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=500,
        help='배치 삽입 행 수 (기본: 500)',
    )
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        logger.error('SQLite DB 파일이 없습니다: %s', sqlite_path)
        sys.exit(1)

    pg_url = args.db_url
    if not pg_url:
        logger.error(
            'PostgreSQL URL이 필요합니다. --db-url 또는 DB_URL 환경변수를 설정하세요.\n'
            '  예: DB_URL=postgresql://botuser:패스워드@postgres:5432/trading_bot'
        )
        sys.exit(1)

    if not pg_url.startswith('postgresql'):
        logger.error('DB_URL이 PostgreSQL URL이 아닙니다: %s', pg_url)
        sys.exit(1)

    # ── 엔진 생성 ──────────────────────────────────────────────────────
    from sqlalchemy import create_engine, inspect
    from trading_bot.models import Base

    logger.info('SQLite 연결: %s', sqlite_path)
    sqlite_engine = create_engine(
        f'sqlite:///{sqlite_path}',
        connect_args={'check_same_thread': False, 'timeout': 30},
    )

    logger.info('PostgreSQL 연결: %s', pg_url.split('@')[-1])  # 패스워드 숨김
    pg_engine = create_engine(pg_url, pool_pre_ping=True)

    # ── PostgreSQL 연결 확인 ────────────────────────────────────────────
    try:
        with pg_engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text('SELECT 1'))
        logger.info('PostgreSQL 연결 성공')
    except Exception as e:
        logger.error('PostgreSQL 연결 실패: %s', e)
        sys.exit(1)

    # ── PostgreSQL 테이블 생성 (없으면) ────────────────────────────────
    if not args.dry_run:
        logger.info('PostgreSQL 테이블 생성 (CREATE TABLE IF NOT EXISTS) ...')
        Base.metadata.create_all(pg_engine)
        logger.info('테이블 준비 완료')

    # ── SQLite 테이블 목록 확인 ─────────────────────────────────────────
    sqlite_inspector = inspect(sqlite_engine)
    sqlite_tables = set(sqlite_inspector.get_table_names())

    # ── SQLAlchemy 메타데이터에서 테이블 객체 가져오기 ──────────────────
    meta = Base.metadata
    # reflect SQLite 스키마를 메타데이터에 바인딩
    meta.bind = sqlite_engine

    # ── 마이그레이션 실행 ───────────────────────────────────────────────
    logger.info('')
    logger.info('=== 마이그레이션 시작 %s ===', '(DRY RUN)' if args.dry_run else '')
    logger.info('')

    total_rows = 0
    results = {}

    for table_name in MIGRATION_ORDER:
        # SQLAlchemy Table 객체 찾기
        table = None
        for t in meta.sorted_tables:
            if t.name == table_name:
                table = t
                break

        if table is None:
            logger.warning('  [%-25s] 모델 정의 없음 — 건너뜀', table_name)
            continue

        if table_name not in sqlite_tables:
            logger.warning('  [%-25s] SQLite에 테이블 없음 — 건너뜀', table_name)
            continue

        try:
            count = migrate_table(
                sqlite_engine, pg_engine, table, table_name,
                dry_run=args.dry_run, force=args.force, batch_size=args.batch_size,
            )
            results[table_name] = ('OK', count)
            total_rows += count
        except Exception as e:
            logger.error('  [%-25s] 오류: %s', table_name, e)
            results[table_name] = ('ERROR', str(e))

    # ── 결과 요약 ─────────────────────────────────────────────────────
    logger.info('')
    logger.info('=== 마이그레이션 결과 요약 ===')
    logger.info('%-25s  %-6s  %s', '테이블', '상태', '행 수 / 오류')
    logger.info('-' * 55)
    for tbl, (status, val) in results.items():
        logger.info('%-25s  %-6s  %s', tbl, status, val)
    logger.info('-' * 55)
    logger.info('총 처리 행 수: %d', total_rows)
    logger.info('')

    errors = [t for t, (s, _) in results.items() if s == 'ERROR']
    if errors:
        logger.error('오류 발생 테이블: %s', ', '.join(errors))
        sys.exit(1)

    if args.dry_run:
        logger.info('--dry-run 완료. 실제 데이터는 변경되지 않았습니다.')
    else:
        logger.info('마이그레이션 완료.')
        logger.info('다음 단계: trading-bot 파드를 재시작하고 PostgreSQL로 정상 동작하는지 확인하세요.')


if __name__ == '__main__':
    main()
