"""거래 성과 분석 — ai_events / execution_events 기반 실전 통계."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
_UTC = timezone.utc

_EXIT_EVENTS = ('STOP_LOSS', 'SCALE_OUT', 'DCA')
_TRADE_EVENTS = ('EXECUTE', 'STOP_LOSS', 'SCALE_OUT', 'DCA')

_RULE_KEYWORDS = [
    'PANIC_DIP', 'DRAGON', 'LOYALTY',
    'FIB_618', 'FIB_786', 'FIB_EXT', 'FIB',
    'ATR_TRAIL', 'SCALE_OUT_ATR', 'SCALE_OUT',
    'CB_50PCT', 'HARD_STOP',
    'STOP_LOSS', 'DCA',
]


def _parse_rule(reason: str) -> str:
    if not reason:
        return 'UNKNOWN'
    u = reason.upper()
    for kw in _RULE_KEYWORDS:
        if kw in u:
            return kw
    token = re.split(r'[\s|:,]', reason.strip())[0]
    return token[:20] if token else 'OTHER'


def get_trade_summary(days: int = 7) -> dict:
    """exit 이벤트 기반 거래 요약 통계."""
    from trading_bot.db import get_session
    from trading_bot.models import AiEvent

    since = datetime.now(_UTC) - timedelta(days=days)
    session = get_session()
    try:
        rows = session.query(AiEvent).filter(
            AiEvent.ts >= since,
            AiEvent.event.in_(_TRADE_EVENTS),
        ).order_by(AiEvent.ts).all()

        buys = [r for r in rows if r.event == 'EXECUTE' and (r.signal or '') == 'buy']
        exits = [r for r in rows if r.event in _EXIT_EVENTS and r.roi_pct is not None]
        wins = [r for r in exits if r.roi_pct > 0]
        losses = [r for r in exits if r.roi_pct < 0]

        avg_win = sum(r.roi_pct for r in wins) / len(wins) if wins else 0.0
        avg_loss = sum(r.roi_pct for r in losses) / len(losses) if losses else 0.0
        pf = round(avg_win / abs(avg_loss), 2) if avg_loss != 0 else None

        daily_roi: dict[str, float] = {}
        for r in exits:
            day = r.ts.astimezone(KST).date().isoformat()
            daily_roi[day] = round(daily_roi.get(day, 0.0) + (r.roi_pct or 0.0), 2)

        return {
            'period_days': days,
            'total_buys': len(buys),
            'total_exits': len(exits),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(len(wins) / len(exits) * 100, 1) if exits else 0.0,
            'avg_win_pct': round(avg_win, 2),
            'avg_loss_pct': round(avg_loss, 2),
            'profit_factor': pf,
            'max_win_pct': round(max((r.roi_pct for r in wins), default=0.0), 2),
            'max_loss_pct': round(min((r.roi_pct for r in losses), default=0.0), 2),
            'daily_roi': daily_roi,
        }
    finally:
        session.close()


def get_rule_performance(days: int = 30) -> dict:
    """규칙별 발동 횟수·승률·평균 수익률."""
    from trading_bot.db import get_session
    from trading_bot.models import AiEvent

    since = datetime.now(_UTC) - timedelta(days=days)
    session = get_session()
    try:
        exits = session.query(AiEvent).filter(
            AiEvent.ts >= since,
            AiEvent.event.in_(_EXIT_EVENTS),
            AiEvent.roi_pct.isnot(None),
        ).all()

        stats: dict[str, dict] = {}
        for r in exits:
            rule = _parse_rule(r.decision_reason)
            s = stats.setdefault(rule, {'count': 0, 'wins': 0, 'roi_sum': 0.0, 'rois': []})
            s['count'] += 1
            s['roi_sum'] += r.roi_pct
            s['rois'].append(r.roi_pct)
            if r.roi_pct > 0:
                s['wins'] += 1

        exit_rules = {
            rule: {
                'count': s['count'],
                'win_rate': round(s['wins'] / s['count'] * 100, 1),
                'avg_roi': round(s['roi_sum'] / s['count'], 2),
                'max_roi': round(max(s['rois']), 2),
                'min_roi': round(min(s['rois']), 2),
            }
            for rule, s in sorted(stats.items(), key=lambda x: -x[1]['count'])
        }

        buys = session.query(AiEvent).filter(
            AiEvent.ts >= since,
            AiEvent.event == 'EXECUTE',
            AiEvent.signal == 'buy',
        ).all()
        buy_rules: dict[str, int] = {}
        for r in buys:
            rule = _parse_rule(r.decision_reason)
            buy_rules[rule] = buy_rules.get(rule, 0) + 1

        return {'exit_rules': exit_rules, 'buy_rules': buy_rules, 'period_days': days}
    finally:
        session.close()


def get_risk_metrics(days: int = 30) -> dict:
    """MDD(근사), CB 발동 횟수, 연속 손실 스트릭."""
    from trading_bot.db import get_session
    from trading_bot.models import AiEvent, ExecutionEvent

    since = datetime.now(_UTC) - timedelta(days=days)
    session = get_session()
    try:
        exits = session.query(AiEvent).filter(
            AiEvent.ts >= since,
            AiEvent.event.in_(_EXIT_EVENTS),
            AiEvent.roi_pct.isnot(None),
        ).order_by(AiEvent.ts).all()

        cum = peak = mdd = 0.0
        for r in exits:
            cum += r.roi_pct
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > mdd:
                mdd = dd

        cb_count = session.query(ExecutionEvent).filter(
            ExecutionEvent.ts >= since,
            ExecutionEvent.tag == 'CB_SELL',
        ).count()

        consec_losses = 0
        try:
            from trading_bot.risk import get_system_state
            consec_losses = int(get_system_state('consec_losses', '0') or 0)
        except Exception:
            pass

        from trading_bot.models import PositionState
        open_positions = session.query(PositionState).filter(
            PositionState.avg_buy_price > 0
        ).count()

        return {
            'period_days': days,
            'mdd_approx_pct': round(mdd, 2),
            'cb_count': cb_count,
            'consec_losses': consec_losses,
            'open_positions': open_positions,
        }
    finally:
        session.close()


def get_hourly_analysis(days: int = 30) -> dict:
    """시간대·요일별 청산 빈도 및 평균 ROI."""
    from trading_bot.db import get_session
    from trading_bot.models import AiEvent

    since = datetime.now(_UTC) - timedelta(days=days)
    session = get_session()
    try:
        exits = session.query(AiEvent).filter(
            AiEvent.ts >= since,
            AiEvent.event.in_(_EXIT_EVENTS),
            AiEvent.roi_pct.isnot(None),
        ).all()

        hourly: dict[int, dict] = {h: {'count': 0, 'roi_sum': 0.0} for h in range(24)}
        weekday: dict[int, dict] = {d: {'count': 0, 'roi_sum': 0.0} for d in range(7)}

        for r in exits:
            kst = r.ts.astimezone(KST)
            h, d = kst.hour, kst.weekday()
            hourly[h]['count'] += 1
            hourly[h]['roi_sum'] += r.roi_pct
            weekday[d]['count'] += 1
            weekday[d]['roi_sum'] += r.roi_pct

        by_hour = {
            str(h): {
                'count': v['count'],
                'avg_roi': round(v['roi_sum'] / v['count'], 2) if v['count'] else 0.0,
            }
            for h, v in hourly.items()
        }
        day_names = ['월', '화', '수', '목', '금', '토', '일']
        by_weekday = {
            day_names[d]: {
                'count': v['count'],
                'avg_roi': round(v['roi_sum'] / v['count'], 2) if v['count'] else 0.0,
            }
            for d, v in weekday.items()
        }

        return {'period_days': days, 'by_hour': by_hour, 'by_weekday': by_weekday}
    finally:
        session.close()


def get_full_report(summary_days: int = 7, rule_days: int = 30, risk_days: int = 30) -> dict:
    """전체 성과 보고서 (API 통합 응답)."""
    return {
        'summary': get_trade_summary(summary_days),
        'rules': get_rule_performance(rule_days),
        'risk': get_risk_metrics(risk_days),
        'hourly': get_hourly_analysis(risk_days),
    }
