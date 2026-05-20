"""
Fear & Greed Index fetcher (Alternative.me API).
무료, 인증 불필요, 일 1회 업데이트.
반환값: 0~100 (0=극단적 공포, 100=극단적 탐욕).

조회 우선순위:
  1. 메모리 캐시 (5분 TTL)
  2. DB 최신값 (나이 6h 이내)
  3. live API 호출
  4. fallback: 50 (Neutral)
"""
import time
import logging
from datetime import datetime, timezone, timedelta

_logger = logging.getLogger(__name__)

_FNG_URL = 'https://api.alternative.me/fng/?limit=1&format=json'
_cache = {'value': 50, 'classification': 'Neutral', 'timestamp': '', 'fetched_at': 0}
_CACHE_TTL = 300    # 5분 메모리 캐시
_DB_MAX_AGE_H = 6   # DB 값 최대 허용 나이 (시간)


def _db_age_hours(ts) -> float:
    """ts (datetime 또는 pandas Timestamp)의 현재까지 경과 시간(h)."""
    if ts is None:
        return float('inf')
    try:
        import pandas as pd
        ts_pd = pd.Timestamp(ts)
        if ts_pd.tzinfo is None:
            ts_pd = ts_pd.tz_localize('UTC')
        else:
            ts_pd = ts_pd.tz_convert('UTC')
        return (datetime.now(timezone.utc) - ts_pd.to_pydatetime()).total_seconds() / 3600
    except Exception:
        return float('inf')


def _get_from_db() -> dict | None:
    """DB에서 최신 FNG 조회. 나이 6h 초과 시 None 반환."""
    try:
        from trading_bot.collectors.sentiment import get_latest
        row = get_latest()
        if row and _db_age_hours(row.get('ts')) <= _DB_MAX_AGE_H:
            return {
                'value': int(row['value']),
                'classification': row.get('label', 'Neutral'),
                'timestamp': '',
            }
    except Exception as e:
        _logger.debug('[FNG] DB 조회 실패: %s', e)
    return None


def fetch_fear_greed_index():
    """Fear & Greed Index 조회.

    Returns:
        dict: {'value': int, 'classification': str, 'timestamp': str}
        실패 시 {'value': 50, 'classification': 'Neutral', 'timestamp': ''}
    """
    now = time.time()

    # 1. 메모리 캐시
    if now - _cache['fetched_at'] < _CACHE_TTL:
        return {
            'value': _cache['value'],
            'classification': _cache['classification'],
            'timestamp': _cache['timestamp'],
        }

    # 2. DB 조회 (나이 6h 이내)
    db_result = _get_from_db()
    if db_result:
        _cache['value'] = db_result['value']
        _cache['classification'] = db_result['classification']
        _cache['timestamp'] = db_result['timestamp']
        _cache['fetched_at'] = now
        _logger.debug('[FNG] DB 캐시 사용 — %d (%s)', db_result['value'], db_result['classification'])
        return db_result

    # 3. live API 호출
    try:
        import urllib.request
        import json

        req = urllib.request.Request(_FNG_URL, headers={'User-Agent': 'TradingBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        if data and 'data' in data and len(data['data']) > 0:
            entry = data['data'][0]
            value = int(entry.get('value', 50))
            classification = entry.get('value_classification', 'Neutral')
            timestamp = entry.get('timestamp', '')

            _cache['value'] = value
            _cache['classification'] = classification
            _cache['timestamp'] = timestamp
            _cache['fetched_at'] = now

            _logger.info('[FNG] live 조회 — %d (%s)', value, classification)
            return {'value': value, 'classification': classification, 'timestamp': timestamp}
    except Exception as e:
        _logger.debug('[FNG] live 조회 실패 (중립값 사용): %s', e)

    # 4. fallback
    return {'value': 50, 'classification': 'Neutral', 'timestamp': ''}
