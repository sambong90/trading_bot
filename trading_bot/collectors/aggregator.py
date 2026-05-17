"""DataAggregator — 3개 컬렉터 통합 실행 및 MarketContext 조합.

collect_all()     : MacroCollector + DominanceCollector + KimpCollector 순차 실행.
get_market_context() : DB 최신값 통합 → L1/L2 평가에 필요한 단일 dict 반환.
validate_freshness()  : ratio_quality 체크 — 'stale' 시 EM-7 트리거 차단.

MarketContext 구조:
  {
    'macro':       dict | None,   # MacroSnapshot 최신
    'dominance':   dict | None,   # DominanceSnapshot 최신
    'kimp':        dict | None,   # KimpSnapshot 최신
    'btc_weekly_200_above': bool, # BTC 현재가 > 주봉 200MA
    'is_tradeable': bool,         # ratio_quality='fresh' AND 필수 데이터 존재
    'block_reasons': list[str],   # 트레이딩 차단 이유 목록
  }
"""
import logging

from trading_bot.collectors import macro as _macro
from trading_bot.collectors import dominance as _dominance
from trading_bot.collectors import kimp as _kimp

logger = logging.getLogger(__name__)


def collect_all(run_macro: bool = True,
                run_dominance: bool = True,
                run_kimp: bool = True) -> dict:
    """3개 컬렉터를 순차 실행. 개별 실패는 경고만 남기고 계속 진행.

    Returns:
        {
          'macro':      dict | None,
          'dominance':  dict | None,
          'kimp':       dict | None,
        }
    """
    results = {}

    if run_macro:
        try:
            results['macro'] = _macro.collect()
        except Exception as e:
            logger.error('MacroCollector 실패: %s', e)
            results['macro'] = None
    else:
        results['macro'] = _macro.get_latest()

    if run_dominance:
        try:
            results['dominance'] = _dominance.collect()
        except Exception as e:
            logger.error('DominanceCollector 실패: %s', e)
            results['dominance'] = None
    else:
        results['dominance'] = _dominance.get_latest()

    if run_kimp:
        try:
            results['kimp'] = _kimp.collect()
        except Exception as e:
            logger.error('KimpCollector 실패: %s', e)
            results['kimp'] = None
    else:
        results['kimp'] = _kimp.get_latest()

    return results


def get_market_context() -> dict:
    """DB 최신 스냅샷 로드 + BTC 주봉200MA 계산 후 MarketContext dict 반환.

    is_tradeable=False 조건:
      - macro.ratio_quality == 'stale'   (EM-7: 주말 데이터로 트리거 금지)
      - macro 또는 dominance 데이터 없음
    """
    macro     = _macro.get_latest()
    dominance = _dominance.get_latest()
    kimp      = _kimp.get_latest()

    btc_weekly_200_above = _check_btc_weekly_200()

    block_reasons: list[str] = []
    is_tradeable = True

    if macro is None:
        block_reasons.append('MACRO_DATA_MISSING')
        is_tradeable = False
    elif macro.get('ratio_quality') == 'stale':
        block_reasons.append('RATIO_STALE_EM7')
        is_tradeable = False

    if dominance is None:
        block_reasons.append('DOMINANCE_DATA_MISSING')
        is_tradeable = False

    return {
        'macro':                macro,
        'dominance':            dominance,
        'kimp':                 kimp,
        'btc_weekly_200_above': btc_weekly_200_above,
        'is_tradeable':         is_tradeable,
        'block_reasons':        block_reasons,
    }


def validate_freshness(context: dict) -> bool:
    """MarketContext의 is_tradeable을 반환. False면 EM-7 위반."""
    tradeable = context.get('is_tradeable', False)
    if not tradeable:
        reasons = context.get('block_reasons', [])
        logger.warning('트레이딩 차단 — 이유: %s', reasons)
    return tradeable


def _check_btc_weekly_200() -> bool:
    """BTC 현재가가 주봉 200MA 위에 있으면 True (G-14, R-02 조건).

    주봉 200MA = 200개 주봉 close 단순평균.
    데이터 부족 시 False 반환 (보수적 처리).
    """
    try:
        import pyupbit
        import pandas as pd
        import numpy as np

        df = pyupbit.get_ohlcv('KRW-BTC', interval='week', count=210)
        if df is None or len(df) < 200:
            logger.warning('BTC 주봉 데이터 부족 (%d개) — weekly_200 False 처리', len(df) if df is not None else 0)
            return False

        close = df['close'].dropna()
        ma200 = float(close.tail(200).mean())
        current = float(close.iloc[-1])
        return current > ma200
    except Exception as e:
        logger.warning('BTC 주봉 200MA 계산 실패: %s', e)
        return False
