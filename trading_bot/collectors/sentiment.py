"""SentimentCollector — Fear & Greed Index 수집 및 SentimentSnapshot DB 저장.

Source: alternative.me FNG API (무료, API키 불필요, 일 1회 업데이트)
수집 주기: 4회/일 (00:30, 06:30, 12:30, 18:30 KST)

3회 재시도 (지수 백오프). 실패 시 None 반환.
"""
import logging
import time
import json
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_FNG_URL = 'https://api.alternative.me/fng/?limit=1&format=json'
_TIMEOUT = 10  # seconds


def _fetch_fng() -> dict | None:
    """alternative.me API 호출 — 실패 시 None."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(_FNG_URL, headers={'User-Agent': 'trading-bot/1.0'})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            if data and 'data' in data and data['data']:
                entry = data['data'][0]
                return {
                    'value': int(entry.get('value', 50)),
                    'label': entry.get('value_classification', 'Neutral'),
                }
        except Exception as e:
            logger.warning('FNG API 요청 실패 (시도 %d/3): %s', attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def collect(session=None) -> dict | None:
    """FNG 수집 → SentimentSnapshot DB 저장 후 dict 반환."""
    raw = _fetch_fng()
    if raw is None:
        logger.error('FNG 수집 실패 — 3회 재시도 모두 실패')
        return None

    snapshot_data = {
        'ts':             datetime.now(timezone.utc),
        'indicator_type': 'FNG',
        'value':          float(raw['value']),
        'label':          raw['label'],
        'data_source':    'alternative.me',
    }

    _save(snapshot_data, session)
    logger.info('SentimentSnapshot 저장 — FNG=%d (%s)', raw['value'], raw['label'])
    return snapshot_data


def _save(data: dict, session=None) -> None:
    from trading_bot.db import get_session
    from trading_bot.models import SentimentSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        snap = SentimentSnapshot(**data)
        session.add(snap)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error('SentimentSnapshot DB 저장 실패: %s', e)
        raise
    finally:
        if own_session:
            session.close()


def get_latest(session=None) -> dict | None:
    """DB에서 가장 최근 SentimentSnapshot 반환 (FNG만). 없으면 None."""
    from trading_bot.db import get_session
    from trading_bot.models import SentimentSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        row = (
            session.query(SentimentSnapshot)
            .filter(SentimentSnapshot.indicator_type == 'FNG')
            .order_by(SentimentSnapshot.ts.desc())
            .first()
        )
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}
    except Exception as e:
        logger.error('SentimentSnapshot 조회 실패: %s', e)
        return None
    finally:
        if own_session:
            session.close()
