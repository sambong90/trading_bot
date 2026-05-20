"""BtcWeeklyCollector — BTC 주봉 200MA 수집 및 BtcWeeklySnapshot DB 저장.

Source: pyupbit get_ohlcv('KRW-BTC', interval='week', count=210)
수집 주기: 매일 08:05 KST 1회 (주봉 200MA는 일 단위 변화만)

aggregator.py의 _check_btc_weekly_200()이 DB 최신값을 읽어 사용.
DB 값 나이 48h 초과 시 aggregator가 live fallback으로 전환.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def collect(session=None) -> dict | None:
    """BTC 주봉 200MA 수집 → BtcWeeklySnapshot DB 저장 후 dict 반환."""
    try:
        import pyupbit
    except ImportError:
        logger.error('pyupbit 미설치')
        return None

    try:
        df = pyupbit.get_ohlcv('KRW-BTC', interval='week', count=210)
    except Exception as e:
        logger.error('pyupbit get_ohlcv 실패: %s', e)
        return None

    if df is None or len(df) < 200:
        logger.error('BTC 주봉 데이터 부족 (%d개)', len(df) if df is not None else 0)
        return None

    try:
        close = df['close'].dropna()
        ma200 = float(close.tail(200).mean())
        current = float(close.iloc[-1])
    except Exception as e:
        logger.error('200MA 계산 실패: %s', e)
        return None

    snapshot_data = {
        'ts':            datetime.now(timezone.utc),
        'ma200':         round(ma200, 0),
        'current_price': round(current, 0),
        'above_ma200':   current > ma200,
        'data_source':   'upbit_pyupbit',
    }

    _save(snapshot_data, session)
    logger.info(
        'BtcWeeklySnapshot 저장 — MA200=%.0f current=%.0f above=%s',
        ma200, current, snapshot_data['above_ma200'],
    )
    return snapshot_data


def _save(data: dict, session=None) -> None:
    from trading_bot.db import get_session
    from trading_bot.models import BtcWeeklySnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        snap = BtcWeeklySnapshot(**data)
        session.add(snap)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error('BtcWeeklySnapshot DB 저장 실패: %s', e)
        raise
    finally:
        if own_session:
            session.close()


def get_latest(session=None) -> dict | None:
    """DB에서 가장 최근 BtcWeeklySnapshot 반환. 없으면 None."""
    from trading_bot.db import get_session
    from trading_bot.models import BtcWeeklySnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        row = (
            session.query(BtcWeeklySnapshot)
            .order_by(BtcWeeklySnapshot.ts.desc())
            .first()
        )
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}
    except Exception as e:
        logger.error('BtcWeeklySnapshot 조회 실패: %s', e)
        return None
    finally:
        if own_session:
            session.close()
