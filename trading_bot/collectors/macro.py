"""MacroCollector — 거시 지표 수집 및 MacroSnapshot DB 저장.

수집 대상 (Yahoo Finance):
  ^DXY   — 달러 인덱스
  ^NDX   — 나스닥100
  GC=F   — 금 선물
  ^TNX   — 미국 10년물 국채 수익률 (%)
  ^TYX   — 미국 30년물 국채 수익률 (%)
  USDJPY=X — 달러/엔
  CL=F   — WTI 원유 선물

교환비 구간 (master_strategy_filtered.md Section 50+196):
  NEUTRAL      215~244  (1:230 기준, 정상 수준)
  NORMAL       245~364
  ELEVATED     365~439  (G-10: ELEVATED_MACRO)
  BUBBLE       440~559  (G-05: BLOCK BUYS)
  EXTREME_BUBBLE ≥560

주의: ratio_quality='stale' 시 EM-7에 따라 트리거 실행 금지.
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ── 교환비 구간 임계값 ──────────────────────────────────────────────
# master_strategy_filtered.md Section 50+196 기준
_RATIO_EXTREME_BUBBLE = 560   # RANGE: 560~600
_RATIO_BUBBLE         = 440   # RANGE: 440~480  (G-05 발동)
_RATIO_ELEVATED       = 365   # RANGE: 365~400  (G-10 발동)
_RATIO_NORMAL_HI      = 245   # NEUTRAL 상단
_RATIO_NEUTRAL_LO     = 215   # NEUTRAL 하단 (1:230 기준)

# ── DXY 구간 임계값 ─────────────────────────────────────────────────
# master_strategy_filtered.md Section 3-2 / 8-2
_DXY_GREEN_ZONE_LOW  = 112.58   # G-01: 초록 구간 하단
_DXY_BULL_TRIGGER    = 103.821  # 장투 허용선 아래
_DXY_SUPER_CRISIS    = 104.6    # G-02 슈퍼크라이시스 보조 레벨

# ── 채권 역전 임계값 ────────────────────────────────────────────────
# master_strategy_filtered.md Section 36
_BOND_BULL      = 0.90   # ratio < 0.90 → BULL_MARKET
_BOND_BEAR1     = 1.00   # ratio < 1.00 → BEAR_MARKET_1
_BOND_SEC_DROP  = 1.05   # ratio ≥ 1.05 → SECONDARY_DROP_IMMINENT

# ── 금/엔화 변동 임계값 ─────────────────────────────────────────────
_GOLD_CRISIS_RISE_PCT   = 2.0   # G-13: 금 1일 상승률 ≥2% 위기 신호
_JPY_INSTABILITY_PCT    = 1.5   # G-12: 달러엔 1일 상승률 ≥1.5% 급락
_OIL_VOL_PCT            = 3.0   # G-07: 원유 변동률 ≥3% 변동성 활성


def _classify_nasdaq_dxy_zone(ratio: float) -> str:
    if ratio >= _RATIO_EXTREME_BUBBLE:
        return 'EXTREME_BUBBLE'
    if ratio >= _RATIO_BUBBLE:
        return 'BUBBLE'
    if ratio >= _RATIO_ELEVATED:
        return 'ELEVATED'
    if ratio >= _RATIO_NORMAL_HI:
        return 'NORMAL'
    if ratio >= _RATIO_NEUTRAL_LO:
        return 'NEUTRAL'
    return 'LOW'


def _classify_dxy_zone(dxy: float) -> str:
    if dxy >= _DXY_GREEN_ZONE_LOW:
        return 'DXY_GREEN_ZONE'
    if dxy >= _DXY_BULL_TRIGGER:
        return 'CAUTION'
    return 'WEAK'


def _classify_bond_signal(us10y: float, us30y: float) -> tuple[float, str]:
    """10Y/30Y 비율로 채권 신호 반환. (ratio, signal)"""
    if us30y <= 0:
        return 0.0, 'UNKNOWN'
    ratio = us10y / us30y
    if ratio >= _BOND_SEC_DROP:
        return ratio, 'SECONDARY_DROP_IMMINENT'
    if ratio >= _BOND_BEAR1:
        return ratio, 'BEAR_MARKET_1'
    if ratio < _BOND_BULL:
        return ratio, 'BULL_MARKET'
    return ratio, 'NEUTRAL'


def _classify_gold_crisis(gold_1d_pct: float, dxy_1d_pct: float) -> str:
    """G-13: DXY↑ AND Gold↑ 동시 = SEVERE_CRISIS."""
    if gold_1d_pct >= _GOLD_CRISIS_RISE_PCT and dxy_1d_pct > 0:
        return 'SEVERE_CRISIS'
    if gold_1d_pct >= _GOLD_CRISIS_RISE_PCT * 0.5:
        return 'PRE_CRISIS'
    return 'NORMAL'


def _classify_crisis_level(dxy_1d_pct: float, gold_1d_pct: float, nasdaq_1d_pct: float) -> str:
    """G-02: dxy_gold_nasdaq_crisis — SUPER_CRISIS / PRE_CRISIS / NORMAL."""
    dxy_surge   = dxy_1d_pct    >= 1.0
    gold_surge  = gold_1d_pct   >= _GOLD_CRISIS_RISE_PCT
    nasdaq_drop = nasdaq_1d_pct <= -2.0
    if dxy_surge and gold_surge and nasdaq_drop:
        return 'SUPER_CRISIS'
    if (dxy_surge and gold_surge) or (gold_surge and nasdaq_drop):
        return 'PRE_CRISIS'
    return 'NORMAL'


def _is_market_stale(ts: datetime) -> bool:
    """주말/미국 공휴일 여부 — 단순 요일 판정 (토=5, 일=6).

    실제 공휴일 캘린더는 추후 pandas_market_calendars로 교체 가능.
    """
    weekday = ts.weekday()
    return weekday >= 5  # 토요일(5), 일요일(6)


def _pct_change(prev: float, curr: float) -> float:
    if prev and prev != 0:
        return round((curr - prev) / abs(prev) * 100, 4)
    return 0.0


def collect(session=None) -> dict | None:
    """거시 지표를 수집하여 MacroSnapshot을 DB에 저장하고 dict를 반환.

    Args:
        session: SQLAlchemy 세션. None이면 get_session()으로 생성.

    Returns:
        저장된 MacroSnapshot의 필드 dict, 실패 시 None.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error('yfinance 미설치. pip install yfinance')
        return None

    tickers = ['^DXY', '^NDX', 'GC=F', '^TNX', '^TYX', 'USDJPY=X', 'CL=F']
    try:
        raw = yf.download(
            tickers,
            period='5d',
            interval='1d',
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error('yfinance download 실패: %s', e)
        return None

    close = raw.get('Close') if hasattr(raw, 'get') else raw['Close']
    if close is None or close.empty:
        logger.error('yfinance Close 데이터 없음')
        return None

    def _last(col: str) -> float | None:
        try:
            s = close[col].dropna()
            return float(s.iloc[-1]) if len(s) else None
        except Exception:
            return None

    def _prev(col: str) -> float | None:
        try:
            s = close[col].dropna()
            return float(s.iloc[-2]) if len(s) >= 2 else None
        except Exception:
            return None

    dxy      = _last('^DXY')
    nasdaq   = _last('^NDX')
    gold     = _last('GC=F')
    us10y    = _last('^TNX')
    us30y    = _last('^TYX')
    usdjpy   = _last('USDJPY=X')
    oil      = _last('CL=F')

    if not all([dxy, nasdaq]):
        logger.error('DXY 또는 NDX 데이터 없음 — MacroSnapshot 저장 중단')
        return None

    dxy_prev    = _prev('^DXY')
    nasdaq_prev = _prev('^NDX')
    gold_prev   = _prev('GC=F')
    usdjpy_prev = _prev('USDJPY=X')
    oil_prev    = _prev('CL=F')

    dxy_1d_pct    = _pct_change(dxy_prev, dxy)
    nasdaq_1d_pct = _pct_change(nasdaq_prev, nasdaq)
    gold_1d_pct   = _pct_change(gold_prev, gold) if gold else 0.0
    usdjpy_1d_pct = _pct_change(usdjpy_prev, usdjpy) if usdjpy else 0.0
    oil_1d_pct    = _pct_change(oil_prev, oil) if oil else 0.0

    nasdaq_dxy_ratio = round(nasdaq / dxy, 4)
    nasdaq_dxy_zone  = _classify_nasdaq_dxy_zone(nasdaq_dxy_ratio)
    dxy_zone_val     = _classify_dxy_zone(dxy)
    gold_crisis      = _classify_gold_crisis(gold_1d_pct, dxy_1d_pct)
    crisis_level     = _classify_crisis_level(dxy_1d_pct, gold_1d_pct, nasdaq_1d_pct)
    bond_ratio, bond_signal = (
        _classify_bond_signal(us10y, us30y) if us10y and us30y else (0.0, 'UNKNOWN')
    )
    jpy_signal   = 'ASIA_INSTABILITY' if usdjpy and usdjpy_1d_pct >= _JPY_INSTABILITY_PCT else 'STABLE'
    oil_vol_flag = abs(oil_1d_pct) >= _OIL_VOL_PCT if oil else False

    now_utc      = datetime.now(timezone.utc)
    ratio_quality = 'stale' if _is_market_stale(now_utc) else 'fresh'

    snapshot_data = {
        'ts':               now_utc,
        'dxy_value':        dxy,
        'nasdaq_value':     nasdaq,
        'gold_value':       gold,
        'us10y_yield':      us10y,
        'us30y_yield':      us30y,
        'usdjpy_value':     usdjpy,
        'oil_value':        oil,
        'dxy_1d_pct':       dxy_1d_pct,
        'nasdaq_1d_pct':    nasdaq_1d_pct,
        'gold_1d_pct':      gold_1d_pct,
        'usdjpy_1d_pct':    usdjpy_1d_pct,
        'nasdaq_dxy_ratio': nasdaq_dxy_ratio,
        'nasdaq_dxy_zone':  nasdaq_dxy_zone,
        'bond_ratio':       round(bond_ratio, 4),
        'bond_signal':      bond_signal,
        'dxy_zone':         dxy_zone_val,
        'gold_crisis_signal': gold_crisis,
        'crisis_level':     crisis_level,
        'jpy_signal':       jpy_signal,
        'oil_vol_active':   oil_vol_flag,
        'ratio_quality':    ratio_quality,
        'data_source':      'yahoo_finance',
    }

    _save(snapshot_data, session)
    logger.info(
        'MacroSnapshot 저장 — ratio=%.1f zone=%s quality=%s crisis=%s',
        nasdaq_dxy_ratio, nasdaq_dxy_zone, ratio_quality, crisis_level,
    )
    return snapshot_data


def _save(data: dict, session=None) -> None:
    from trading_bot.db import get_session
    from trading_bot.models import MacroSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        snap = MacroSnapshot(**data)
        session.add(snap)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error('MacroSnapshot DB 저장 실패: %s', e)
        raise
    finally:
        if own_session:
            session.close()


def get_latest(session=None) -> dict | None:
    """DB에서 가장 최근 MacroSnapshot 반환. 없으면 None."""
    from trading_bot.db import get_session
    from trading_bot.models import MacroSnapshot

    own_session = session is None
    if own_session:
        session = get_session()
    try:
        row = (
            session.query(MacroSnapshot)
            .order_by(MacroSnapshot.ts.desc())
            .first()
        )
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}
    except Exception as e:
        logger.error('MacroSnapshot 조회 실패: %s', e)
        return None
    finally:
        if own_session:
            session.close()
