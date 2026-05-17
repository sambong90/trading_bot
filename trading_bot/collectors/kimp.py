"""KimpCollector — 김치프리미엄 수집 및 KimpSnapshot DB 저장.

Sources:
  Upbit REST   — KRW-BTC 현재가 (API키 불필요)
  Binance REST — BTC/USDT 현재가 (API키 불필요)
  환율          — ExchangeRate-API (무료, API키 불필요)
                  fallback: USDJPY × JPY/KRW 역산 또는 고정값 1350

김치프리미엄:
  kimp_pct = ((btc_krw / (btc_usd × usdkrw)) - 1) × 100

kimp_signal 분류 (master_strategy_filtered.md):
  KOREAN_REVERSE_PREMIUM_BUY  : kimp_pct < -1.0  (E-04: 역프 → 즉각 매수)
  BOTTOM_LIKELY               : kimp_pct < -0.5  (E-11/E-20 보조)
  PREMIUM                     : kimp_pct > 5.0   (과열 주의)
  NEUTRAL                     : 그 외
"""
import logging
import time
import json
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 8  # seconds

_UPBIT_TICKER_URL   = 'https://api.upbit.com/v1/ticker?markets=KRW-BTC'
_BINANCE_PRICE_URL  = 'https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT'
_EXCHANGERATE_URL   = 'https://open.er-api.com/v6/latest/USD'

_KIMP_REVERSE_BUY   = -1.0   # E-04: KOREAN_REVERSE_PREMIUM_BUY
_KIMP_BOTTOM_LIKELY = -0.5   # E-11/E-20 보조 신호
_KIMP_PREMIUM_HIGH  = 5.0    # 과열 경계


def _fetch_json(url: str, retry: int = 3) -> dict | list | None:
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'trading-bot/1.0'})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            logger.warning('HTTP 요청 실패 %s (시도 %d/%d): %s', url, attempt + 1, retry, e)
            if attempt < retry - 1:
                time.sleep(1.5 ** attempt)
    return None


def _fetch_btc_krw() -> float | None:
    data = _fetch_json(_UPBIT_TICKER_URL)
    if isinstance(data, list) and data:
        price = data[0].get('trade_price')
        return float(price) if price else None
    return None


def _fetch_btc_usd() -> float | None:
    data = _fetch_json(_BINANCE_PRICE_URL)
    if isinstance(data, dict):
        price = data.get('price')
        return float(price) if price else None
    return None


def _fetch_usdkrw() -> float | None:
    """USD/KRW 환율. ExchangeRate-API 실패 시 1350 고정값 fallback."""
    data = _fetch_json(_EXCHANGERATE_URL)
    if isinstance(data, dict) and data.get('result') == 'success':
        rates = data.get('rates', {})
        krw = rates.get('KRW')
        if krw:
            return float(krw)
    logger.warning('환율 API 실패 — 1350 고정값 사용')
    return 1350.0


def _classify_kimp_signal(kimp_pct: float) -> str:
    if kimp_pct < _KIMP_REVERSE_BUY:
        return 'KOREAN_REVERSE_PREMIUM_BUY'
    if kimp_pct < _KIMP_BOTTOM_LIKELY:
        return 'BOTTOM_LIKELY'
    if kimp_pct > _KIMP_PREMIUM_HIGH:
        return 'PREMIUM'
    return 'NEUTRAL'


def collect(session=None) -> dict | None:
    """김치프리미엄 수집 → KimpSnapshot DB 저장 후 dict 반환."""
    from datetime import datetime, timezone

    btc_krw = _fetch_btc_krw()
    btc_usd = _fetch_btc_usd()
    usdkrw  = _fetch_usdkrw()

    if not btc_krw or not btc_usd or not usdkrw:
        logger.error(
            '김프 수집 실패 — btc_krw=%s btc_usd=%s usdkrw=%s',
            btc_krw, btc_usd, usdkrw,
        )
        return None

    fair_value = btc_usd * usdkrw
    kimp_pct   = round((btc_krw / fair_value - 1) * 100, 4) if fair_value else 0.0
    kimp_signal = _classify_kimp_signal(kimp_pct)

    snapshot_data = {
        'ts':          datetime.now(timezone.utc),
        'btc_krw':     round(btc_krw, 0),
        'btc_usd':     round(btc_usd, 2),
        'usdkrw':      round(usdkrw, 2),
        'kimp_pct':    kimp_pct,
        'kimp_signal': kimp_signal,
        'data_source': 'upbit_binance',
    }

    _save(snapshot_data, session)
    logger.info('KimpSnapshot 저장 — kimp=%.2f%% signal=%s', kimp_pct, kimp_signal)
    return snapshot_data


def _save(data: dict, session=None) -> None:
    from trading_bot.db import get_session
    from trading_bot.models import KimpSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        snap = KimpSnapshot(**data)
        session.add(snap)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error('KimpSnapshot DB 저장 실패: %s', e)
        raise
    finally:
        if own_session:
            session.close()


def get_latest(session=None) -> dict | None:
    """DB에서 가장 최근 KimpSnapshot 반환. 없으면 None."""
    from trading_bot.db import get_session
    from trading_bot.models import KimpSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        row = (
            session.query(KimpSnapshot)
            .order_by(KimpSnapshot.ts.desc())
            .first()
        )
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}
    except Exception as e:
        logger.error('KimpSnapshot 조회 실패: %s', e)
        return None
    finally:
        if own_session:
            session.close()
