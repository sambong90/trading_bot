"""tilt.py — H002 자본배분 틸트 가중 w(regime, dd_30d). (라이브 봇)

⚠️ 엔진(진입/청산/건당ROI) 무수정 — 자본배분 층 전용. 건당 % 엣지는 그대로 두고
주문크기(sized_order_krw)에만 w를 곱한다. 백테스트(quant-research backtester/tilt.py)는
건당 ROI에 동일 w를 자본가중 → 백테스트↔페이퍼 대칭.

근거값(임의 아님, quant-research 리포트):
  - dd 임계 near>=-15 / deep<-30: DD30D_AGE_REDUNDANCY (old×near 4.93 vs old×deep 1.35)
  - dd 승수 near 2.0 / mid 1.0 / deep 0.5: H002_LEVER_ANALYSIS 현실 틸트(자본가중 avgROI 1.56x)
  - regime 승수 bear 0.5 / 그외 1.0: REGIME_GATE 소프트 틸트(bear×0.5, PF 1.34); SIZE_TILT서 결합 PF 1.54
  - regime 정의: BTC 일봉 EMA5/20 ±0.5% 전일(causal) — _btc_regime()와 동일

⚠️ quant-research backtester/tilt.py와 **정의 동일 유지**(드리프트 금지). 수정 시 양쪽 동시.
"""
DD_NEAR = -15.0
DD_DEEP = -30.0
W_DD = {'near': 2.0, 'mid': 1.0, 'deep': 0.5}
W_REGIME = {'bull': 1.0, 'sideways': 1.0, 'bear': 0.5}


def dd_bucket(dd_30d):
    if dd_30d is None:
        return 'mid'
    if dd_30d >= DD_NEAR:
        return 'near'
    if dd_30d < DD_DEEP:
        return 'deep'
    return 'mid'


def w_dd(dd_30d):
    return W_DD[dd_bucket(dd_30d)]


def w_regime(regime):
    return W_REGIME.get(regime, 1.0)


def weight(regime, dd_30d):
    """자본배분 가중 = w_regime × w_dd (독립, DD30D_AGE_REDUNDANCY서 stack 확인)."""
    return w_regime(regime) * w_dd(dd_30d)
