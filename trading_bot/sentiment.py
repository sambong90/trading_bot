"""
Fear & Greed Index fetcher (Alternative.me API).
무료, 인증 불필요, 일 1회 업데이트.
반환값: 0~100 (0=극단적 공포, 100=극단적 탐욕).
5분 메모리 캐시, 실패 시 50 (중립) 반환.
"""
import time
import logging

_logger = logging.getLogger(__name__)

_FNG_URL = 'https://api.alternative.me/fng/?limit=1&format=json'
_cache = {'value': 50, 'classification': 'Neutral', 'timestamp': '', 'fetched_at': 0}
_CACHE_TTL = 300  # 5분


def fetch_fear_greed_index():
    """Fear & Greed Index 조회.

    Returns:
        dict: {'value': int, 'classification': str, 'timestamp': str}
        실패 시 {'value': 50, 'classification': 'Neutral', 'timestamp': ''}
    """
    now = time.time()
    if now - _cache['fetched_at'] < _CACHE_TTL:
        return {
            'value': _cache['value'],
            'classification': _cache['classification'],
            'timestamp': _cache['timestamp'],
        }

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

            _logger.info('[FNG] Fear & Greed Index: %d (%s)', value, classification)
            return {'value': value, 'classification': classification, 'timestamp': timestamp}
    except Exception as e:
        _logger.debug('[FNG] 조회 실패 (중립값 사용): %s', e)

    return {'value': 50, 'classification': 'Neutral', 'timestamp': ''}
