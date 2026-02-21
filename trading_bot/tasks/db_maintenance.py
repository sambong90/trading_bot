#!/usr/bin/env python3
"""
DB 하우스키핑(Pruning): 장기 운용 시 용량·조회 속도 유지를 위해 오래된 데이터 정리.
스케줄러에서 매일 새벽 3시(또는 1회/일) 실행 권장.

마이그레이션 가이드 (스키마 변경 시):
- SQLite는 ALTER TABLE로 컬럼 추가만 지원. 새 컬럼 추가 시:
  ALTER TABLE analysis_results ADD COLUMN regime VARCHAR;
  ALTER TABLE analysis_results ADD COLUMN is_defensive_mode BOOLEAN;
- 인덱스는 기존 DB에도 생성 가능: CREATE INDEX idx_analysis_ticker_ts ON analysis_results(ticker, timestamp);
- 가장 간단한 방법: DB 파일 백업 후 삭제하고 앱 재시작 시 테이블 자동 생성(초기화).
  초기화 시: trading_bot/db/trading_bot.db 제거 후 스케줄러/앱 재실행.
"""
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)


def prune_old_data():
    """
    오래된 데이터 삭제로 DB 용량 및 조회 성능 유지.
    - TickerSnapshot: 7일 초과 분 삭제
    - AnalysisResult: 30일 초과 삭제, 또는 signal=='hold' 이면서 7일 초과 삭제
    - OHLCV, TechnicalIndicator: 90일 초과 삭제
    """
    from sqlalchemy import or_, and_
    from trading_bot.db import get_session
    from trading_bot.models import (
        TickerSnapshot,
        AnalysisResult,
        OHLCV,
        TechnicalIndicator,
    )

    now = datetime.utcnow()
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    cutoff_90d = now - timedelta(days=90)

    session = get_session()
    try:
        # TickerSnapshot: 7일 초과
        deleted_snap = session.query(TickerSnapshot).filter(TickerSnapshot.timestamp < cutoff_7d).delete(synchronize_session=False)
        logger.info('prune TickerSnapshot: %s rows (older than 7d)', deleted_snap)

        # AnalysisResult: 30일 초과 삭제 OR (signal=='hold' 이면서 7일 초과 삭제)
        deleted_ar = session.query(AnalysisResult).filter(
            or_(
                AnalysisResult.timestamp < cutoff_30d,
                and_(AnalysisResult.signal == 'hold', AnalysisResult.timestamp < cutoff_7d),
            )
        ).delete(synchronize_session=False)
        logger.info('prune AnalysisResult: %s rows (30d+ or hold 7d+)', deleted_ar)

        # OHLCV: 90일 초과
        deleted_ohlcv = session.query(OHLCV).filter(OHLCV.ts < cutoff_90d).delete(synchronize_session=False)
        logger.info('prune OHLCV: %s rows (older than 90d)', deleted_ohlcv)

        # TechnicalIndicator: 90일 초과
        deleted_tech = session.query(TechnicalIndicator).filter(TechnicalIndicator.ts < cutoff_90d).delete(synchronize_session=False)
        logger.info('prune TechnicalIndicator: %s rows (older than 90d)', deleted_tech)

        session.commit()
        return {
            'ticker_snapshots': deleted_snap,
            'analysis_results': deleted_ar,
            'ohlcv': deleted_ohlcv,
            'technical_indicators': deleted_tech,
        }
    except Exception as e:
        session.rollback()
        logger.exception('prune_old_data failed: %s', e)
        raise
    finally:
        session.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    prune_old_data()
    print('DB pruning completed.')


if __name__ == '__main__':
    main()
