"""DominanceCollector — BTC/ETH 도미넌스 수집 및 DominanceSnapshot DB 저장.

Source: CoinGecko /api/v3/global (무료, API키 불필요)
수집 주기: 매 4시간 (APScheduler)

핵심 임계값 (core_logic_distilled.md):
  63.75 → ALT_ENTRY_CONFIRMED    (R-13, 4h 하향 돌파)
  60.00 → BULL_FULLY_CONFIRMED   (R-12, 4h 하향 돌파)
  58.85 → BULL_START_TRIGGER     (R-09, 4h 하향 돌파)
  50.00 → ALT_MASSACRE 기준      (G-14)
  41.55 → DOM_REVERSAL_UP        (X-05, 4h 상향 돌파)
  40.00 → BULL_CLIMAX_ZONE       (R-15)

event_signal: 직전 DominanceSnapshot 대비 임계값 크로스 여부 감지.
"""
import logging
import time
from datetime import datetime, timezone

import urllib.request
import json

logger = logging.getLogger(__name__)

_COINGECKO_GLOBAL_URL = 'https://api.coingecko.com/api/v3/global'
_REQUEST_TIMEOUT = 10  # seconds

# ── 도미넌스 임계값 ─────────────────────────────────────────────────
_THRESHOLDS = {
    'gap_to_63_75': 63.75,  # ALT_ENTRY_CONFIRMED
    'gap_to_60_00': 60.00,  # BULL_FULLY_CONFIRMED
    'gap_to_58_85': 58.85,  # BULL_START_TRIGGER
    'gap_to_50_00': 50.00,  # ALT_MASSACRE
    'gap_to_41_55': 41.55,  # DOM_REVERSAL_UP
    'gap_to_40_00': 40.00,  # BULL_CLIMAX_ZONE
}

# 크로스 이벤트: (필드명, 방향, 신호명)
# 방향 'down' = 이전 양수 → 현재 음수 (하향 돌파)
# 방향 'up'   = 이전 음수 → 현재 양수 (상향 돌파)
_CROSS_EVENTS = [
    ('gap_to_58_85', 'down', 'BULL_START_TRIGGER'),
    ('gap_to_60_00', 'down', 'BULL_FULLY_CONFIRMED'),
    ('gap_to_63_75', 'down', 'ALT_ENTRY_CONFIRMED'),
    ('gap_to_41_55', 'up',   'DOM_REVERSAL_UP'),
]


def _fetch_global() -> dict | None:
    """CoinGecko /global 응답 반환. 실패 시 None."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                _COINGECKO_GLOBAL_URL,
                headers={'User-Agent': 'trading-bot/1.0'},
            )
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                body = resp.read().decode('utf-8')
            return json.loads(body).get('data', {})
        except Exception as e:
            logger.warning('CoinGecko 요청 실패 (시도 %d/3): %s', attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def _classify_bull_stage(btc_dom: float) -> str:
    if btc_dom < 40.0:
        return 'BULL_CLIMAX_ZONE'
    if btc_dom < 41.55:
        return 'BULL_CONFIRMED'
    if btc_dom < 58.85:
        return 'BULL_EARLY'
    if btc_dom < 60.0:
        return 'BULL_WATCHING'
    return 'NO_BULL'


def _detect_cross_event(prev: dict | None, curr_gaps: dict) -> str | None:
    """직전 snapshot 대비 임계값 크로스 이벤트 감지."""
    if prev is None:
        return None
    for field, direction, signal in _CROSS_EVENTS:
        prev_gap = prev.get(field)
        curr_gap = curr_gaps.get(field)
        if prev_gap is None or curr_gap is None:
            continue
        if direction == 'down' and prev_gap > 0 and curr_gap <= 0:
            return signal
        if direction == 'up' and prev_gap < 0 and curr_gap >= 0:
            return signal
    return None


def collect(timeframe: str = '4h', session=None) -> dict | None:
    """도미넌스 수집 → DominanceSnapshot DB 저장 후 dict 반환."""
    data = _fetch_global()
    if data is None:
        logger.error('CoinGecko 데이터 수집 실패')
        return None

    market_cap_pct: dict = data.get('market_cap_percentage', {})
    btc_dom = market_cap_pct.get('btc')
    eth_dom = market_cap_pct.get('eth')

    if btc_dom is None:
        logger.error('BTC 도미넌스 값 없음')
        return None

    btc_dom = round(float(btc_dom), 4)
    eth_dom = round(float(eth_dom), 4) if eth_dom is not None else 0.0
    alt_dom = round(100.0 - btc_dom - eth_dom, 4)

    gaps = {field: round(btc_dom - level, 4) for field, level in _THRESHOLDS.items()}
    bull_stage = _classify_bull_stage(btc_dom)

    prev = get_latest(session=session)
    event_signal = _detect_cross_event(prev, gaps)

    now_utc = datetime.now(timezone.utc)
    snapshot_data = {
        'ts':            now_utc,
        'timeframe':     timeframe,
        'btc_dominance': btc_dom,
        'eth_dominance': eth_dom,
        'alt_dominance': alt_dom,
        **gaps,
        'bull_stage':    bull_stage,
        'event_signal':  event_signal,
        'data_source':   'coingecko',
    }

    _save(snapshot_data, session)
    logger.info(
        'DominanceSnapshot 저장 — BTC.D=%.2f%% stage=%s event=%s',
        btc_dom, bull_stage, event_signal,
    )
    return snapshot_data


def _save(data: dict, session=None) -> None:
    from trading_bot.db import get_session
    from trading_bot.models import DominanceSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        snap = DominanceSnapshot(**data)
        session.add(snap)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error('DominanceSnapshot DB 저장 실패: %s', e)
        raise
    finally:
        if own_session:
            session.close()


def get_latest(session=None) -> dict | None:
    """DB에서 가장 최근 DominanceSnapshot 반환. 없으면 None."""
    from trading_bot.db import get_session
    from trading_bot.models import DominanceSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        row = (
            session.query(DominanceSnapshot)
            .order_by(DominanceSnapshot.ts.desc())
            .first()
        )
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}
    except Exception as e:
        logger.error('DominanceSnapshot 조회 실패: %s', e)
        return None
    finally:
        if own_session:
            session.close()
