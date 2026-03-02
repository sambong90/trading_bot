"""
Unit tests for sentiment module (Fear & Greed Index).
"""
import pytest
from unittest.mock import patch, MagicMock
import json


class TestFetchFearGreedIndex:
    """Tests for fetch_fear_greed_index()."""

    def setup_method(self):
        # Reset cache between tests
        from trading_bot import sentiment
        sentiment._cache['fetched_at'] = 0
        self._mod = sentiment

    def test_returns_neutral_on_network_failure(self):
        """Network failure → returns neutral (50)."""
        with patch('urllib.request.urlopen', side_effect=Exception('timeout')):
            result = self._mod.fetch_fear_greed_index()
        assert result['value'] == 50
        assert result['classification'] == 'Neutral'

    def test_parses_valid_response(self):
        """Valid API response → correct parsing."""
        mock_data = json.dumps({
            'data': [{'value': '15', 'value_classification': 'Extreme Fear', 'timestamp': '1700000000'}]
        }).encode('utf-8')

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = self._mod.fetch_fear_greed_index()

        assert result['value'] == 15
        assert result['classification'] == 'Extreme Fear'

    def test_cache_prevents_repeated_calls(self):
        """Second call within TTL uses cache, not network."""
        # Simulate a cached value
        import time
        self._mod._cache['value'] = 42
        self._mod._cache['classification'] = 'Fear'
        self._mod._cache['timestamp'] = '123'
        self._mod._cache['fetched_at'] = time.time()  # just now

        result = self._mod.fetch_fear_greed_index()
        assert result['value'] == 42
        assert result['classification'] == 'Fear'

    def test_empty_data_returns_neutral(self):
        """Empty data array → returns neutral."""
        mock_data = json.dumps({'data': []}).encode('utf-8')

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = self._mod.fetch_fear_greed_index()

        assert result['value'] == 50
