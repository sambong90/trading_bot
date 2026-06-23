"""golive_status.py — H002 페이퍼 GO_LIVE 기준 현황 집계 (읽기 전용, 측정만).

매매로직(진입/청산/사이즈) 무관 — paper_mr_positions를 읽어 GO_LIVE 3기준 충족 여부를
한눈에 출력한다. 기준은 reports/GO_LIVE_CRITERIA.md(2026-06-21 국면-fill 개정) 그대로.

실행: 파드 내  python scripts/golive_status.py
  (kubectl exec -n quant-bot <trading-bot-pod> -- python scripts/golive_status.py)
"""
import os
import sys
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from trading_bot.db import get_session

# ── GO_LIVE 기준 (reports/GO_LIVE_CRITERIA.md, 2026-06-21 국면-fill 개정) ──
K = {'bull': 10, 'sideways': 5, 'bear': 10}   # 기준1: 국면별 CLOSED 최소
PF_OVERALL_MIN = 1.2                           # 기준3: 전체 PF
PF_REGIME_MIN = 1.0                            # 기준3: 각 국면 PF (bear 생존)
MAXFILL0_MAX = 0.20                            # 기준2: max_fill=0 비율 상한
SLIP_MED_MAX = 0.30                            # 기준2: sized_slip 중앙값 상한 (%)
REGIMES = ['bull', 'sideways', 'bear']


def pf(rois):
    rois = [r for r in rois if r is not None]
    gp = sum(r for r in rois if r > 0)
    gl = -sum(r for r in rois if r < 0)
    return (gp / gl) if gl > 0 else (float('inf') if gp > 0 else 0.0)


def pf_w(pairs):
    """자본가중 PF (페이퍼 틸트와 일관). pairs = [(roi, w), ...]."""
    pairs = [(r, w) for r, w in pairs if r is not None]
    gp = sum(w * r for r, w in pairs if r > 0)
    gl = -sum(w * r for r, w in pairs if r < 0)
    return (gp / gl) if gl > 0 else (float('inf') if gp > 0 else 0.0)


def fmt_pf(v):
    return '  inf' if v == float('inf') else f'{v:5.3f}'


def main():
    s = get_session()
    try:
        closed = s.execute(text(
            "SELECT regime, roi_realistic_pct, roi_signal_pct, exit_reason, tilt_w "
            "FROM paper_mr_positions WHERE status='CLOSED'")).fetchall()
        allrows = s.execute(text(
            "SELECT max_fill_krw, sized_slippage_pct, slippage_entry_pct, size_capped "
            "FROM paper_mr_positions")).fetchall()
    finally:
        s.close()

    n_total = len(allrows)
    n_closed = len(closed)
    # roi: 실측(roi_realistic) 우선, 없으면 signal
    def roi_of(r):
        return r[1] if r[1] is not None else r[2]

    def w_of(r):
        return r[4] if r[4] is not None else 1.0   # 틸트 배포前 구행은 균등(1.0)

    print("=" * 60)
    print(f"  H002 페이퍼 GO_LIVE 현황  (총 {n_total}행 / CLOSED {n_closed})")
    print("=" * 60)

    # ── 기준 1: 국면별 표본 (국면-fill) ──
    print("\n[기준1] 국면별 CLOSED 표본 (bull>=10 · sideways>=5 · bear>=10)")
    by_reg = {rg: [r for r in closed if (r[0] or '') == rg] for rg in REGIMES}
    other = [r for r in closed if (r[0] or '') not in REGIMES]
    c1_ok = True
    for rg in REGIMES:
        n = len(by_reg[rg]); need = K[rg]; ok = n >= need
        c1_ok &= ok
        print(f"   {rg:9s} {n:4d} / {need:<3d} {'OK' if ok else 'X'}")
    if other:
        print(f"   (regime 미분류/NULL: {len(other)})")
    print(f"   => 기준1 {'충족' if c1_ok else '미충족'}")

    # ── 기준 2: 체결 현실성 ──
    print("\n[기준2] 체결 현실성 (max_fill=0 <=20% · sized_slip중앙값 <=0.30%)")
    maxfill0 = sum(1 for r in allrows if r[0] == 0)
    mf_ratio = maxfill0 / n_total if n_total else 0
    slips = [r[1] for r in allrows if r[1] is not None]
    med_slip = median(slips) if slips else None
    capped = [(r[1], r[2]) for r in allrows if r[3] and r[1] is not None and r[2] is not None]
    capped_ok = all(sz < intd for sz, intd in capped) if capped else None
    c2_mf = mf_ratio <= MAXFILL0_MAX
    c2_slip = (med_slip is not None and med_slip <= SLIP_MED_MAX)
    print(f"   max_fill=0     {maxfill0}/{n_total} = {mf_ratio*100:.0f}%  (<=20%) {'OK' if c2_mf else 'X'}")
    print(f"   sized_slip중앙 {med_slip if med_slip is None else round(med_slip,4)}%  (<=0.30%) {'OK' if c2_slip else 'X'}")
    print(f"   사이즈축소 효과 (capped서 sized<intended): {capped_ok if capped is not None else 'n/a(capped 없음)'}")
    c2_ok = c2_mf and c2_slip
    print(f"   => 기준2 {'충족' if c2_ok else '미충족'}")

    # ── 기준 3: PF 자본가중(tilt_w, 페이퍼 틸트와 일관) — 균등 병기 ──
    print("\n[기준3] PF 자본가중(tilt_w)=판정 (전체 >=1.2 · 각 국면 >=1.0)")
    pf_all = pf_w([(roi_of(r), w_of(r)) for r in closed])     # 자본가중 = 판정
    pf_all_eq = pf([roi_of(r) for r in closed])               # 균등 = 참고
    c3_all = pf_all >= PF_OVERALL_MIN
    print(f"   전체     n={n_closed:4d}  PF(자본가중)={fmt_pf(pf_all)}  (>=1.2) {'OK' if c3_all else 'X'}   [균등 {fmt_pf(pf_all_eq)}]")
    c3_reg = True
    for rg in REGIMES:
        rows = by_reg[rg]
        if not rows:
            print(f"   {rg:9s} n=   0  PF=  n/a  (표본 없음 — 미충족)")
            c3_reg = False
            continue
        p = pf_w([(roi_of(r), w_of(r)) for r in rows]); p_eq = pf([roi_of(r) for r in rows])
        ok = p >= PF_REGIME_MIN
        c3_reg &= ok
        print(f"   {rg:9s} n={len(rows):4d}  PF(자본가중)={fmt_pf(p)}  (>=1.0) {'OK' if ok else 'X'}   [균등 {fmt_pf(p_eq)}]")
    c3_ok = c3_all and c3_reg
    print(f"   => 기준3 {'충족' if c3_ok else '미충족'}  (자본가중 PF 판정, 페이퍼 틸트와 일관)")

    # ── STOP 분포 (참고: 손실 관측 여부) ──
    reasons = {}
    for r in closed:
        reasons[r[3]] = reasons.get(r[3], 0) + 1
    print("\n[참고] 청산 사유: " + " ".join(f"{k}={v}" for k, v in sorted(reasons.items())))

    print("\n" + "=" * 60)
    verdict = c1_ok and c2_ok and c3_ok
    print(f"  GO_LIVE: {'★ 전 기준 충족 (소액 라이브 검토)' if verdict else '미충족 — 대기'}"
          f"  [1:{'O' if c1_ok else 'X'} 2:{'O' if c2_ok else 'X'} 3:{'O' if c3_ok else 'X'}]")
    print("=" * 60)


if __name__ == '__main__':
    main()
