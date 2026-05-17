"""collectors — 거시/도미넌스/김프 데이터 수집 패키지.

모듈 구성:
  macro.py       — MacroCollector   (Yahoo Finance: DXY, NDX, Gold, Bond, JPY, Oil)
  dominance.py   — DominanceCollector (CoinGecko: BTC.D, ETH.D)
  kimp.py        — KimpCollector    (Upbit + Binance: 김치프리미엄)
  aggregator.py  — DataAggregator   (3개 컬렉터 통합 실행 + MarketContext 반환)
"""
