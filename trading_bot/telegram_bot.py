#!/usr/bin/env python3
"""
Interactive Telegram Chatbot — trading bot 모니터링·제어.
long polling(requests) 방식. monitor.py의 send_telegram()과 동일 봇 사용 (송수신 독립).

보안:
  - TELEGRAM_CHAT_ID: 허용 채팅방 (필수)
  - TELEGRAM_ADMIN_USER_ID: 관리자 user_id (설정 시 해당 사용자만 제어 명령 허용)

명령:
  /help    — 전체 명령어 목록
  /status  — 스케줄러 상태·사이클 진행률
  /balance — KRW·보유 종목·ROI
  /report  — 오늘 체결 및 실현 P&L
  /pause   — 매매 사이클 일시 정지 (bot_control.json)
  /resume  — 매매 사이클 재개
  /panic   — 자동 매매 즉시 전면 중지 (ENABLE_AUTO_LIVE=0 + paused)
"""
import os
import sys
import json
import time
import html
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'trading_bot' / '.env')
except Exception:
    pass

import requests

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
ADMIN_USER_ID = os.environ.get('TELEGRAM_ADMIN_USER_ID', '').strip()
BASE_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

LOG_DIR = ROOT / 'trading_bot' / 'logs'
PROGRESS_FILE = LOG_DIR / 'progress.json'
BOT_CONTROL_FILE = LOG_DIR / 'bot_control.json'
ENV_PATH = ROOT / 'trading_bot' / '.env'

# 제어 명령 (admin 전용)
_CONTROL_COMMANDS = {'/pause', '/resume', '/panic'}


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------

def _is_authorized(chat_id: str, from_user_id: str, command: str) -> bool:
    """채팅방 + (제어 명령이면) admin user_id 검증."""
    if CHAT_ID and chat_id != CHAT_ID:
        return False
    if command in _CONTROL_COMMANDS and ADMIN_USER_ID:
        if from_user_id != ADMIN_USER_ID:
            return False
    return True


# ---------------------------------------------------------------------------
# 저수준 전송
# ---------------------------------------------------------------------------

def _send(text: str, chat_id: str = None, parse_mode: str = 'HTML') -> bool:
    chat_id = chat_id or CHAT_ID
    if not TOKEN or not chat_id:
        return False
    payload = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    try:
        r = requests.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# bot_control.json 헬퍼
# ---------------------------------------------------------------------------

def _read_control() -> dict:
    try:
        if BOT_CONTROL_FILE.exists():
            with open(BOT_CONTROL_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {'paused': False}


def _write_control(data: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(BOT_CONTROL_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 포맷 유틸
# ---------------------------------------------------------------------------

def _kst_now():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Asia/Seoul'))


def _fmt_krw(val: float) -> str:
    return f'₩{val:,.0f}'


def _time_ago_str(ts) -> str:
    """datetime 또는 'YYYY-MM-DD HH:MM:SS' 문자열 → 'N분 전' 포맷."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('Asia/Seoul')
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        delta = datetime.now(tz) - ts.astimezone(tz)
        m = max(0, int(delta.total_seconds() / 60))
        if m < 1:   return '방금'
        if m < 60:  return f'{m}분 전'
        h = m // 60
        return f'{h}시간 {m % 60}분 전' if m % 60 else f'{h}시간 전'
    except Exception:
        return str(ts)


def _send_multipart(text: str, chat_id: str = None) -> bool:
    """4000자 초과 시 분할 발송."""
    MAX = 4000
    if len(text) <= MAX:
        return _send(text, chat_id=chat_id)
    ok = True
    parts = [text[i:i + MAX] for i in range(0, len(text), MAX)]
    for p in parts:
        if not _send(p, chat_id=chat_id):
            ok = False
    return ok


# ---------------------------------------------------------------------------
# 명령 핸들러
# ---------------------------------------------------------------------------

def cmd_help() -> str:
    lines = [
        '<b>🤖 Trading Bot 명령어</b>',
        '',
        '/status    — 봇 상태 · 장세 · 포트폴리오 · 오늘 실적',
        '/today     — 오늘 매매 상세 이벤트 내역',
        '/week      — 주간 성과 리포트 (regime · 규칙 · 트레이드)',
        '/positions — 보유 포지션 상세 (ROI · trailing · scale-out)',
        '/guardian  — 매크로 L1/L2 필터 상세',
        '/health    — 데이터 수집 건강 체크 (나이·신선도)',
        '/signals   — 최근 6시간 신호 요약',
        '/balance   — KRW · 보유 종목 · 개별 ROI',
        '/report    — /week 별칭',
        '',
        '<b>⚙️ 제어 명령 (관리자 전용)</b>',
        '/pause  — 다음 사이클부터 매매 일시 정지',
        '/resume — 매매 재개',
        '/panic  — 자동 매매 즉시 전면 중지',
    ]
    if ADMIN_USER_ID:
        lines.append('')
        lines.append(f'<i>관리자 인증 활성화 (user_id: {html.escape(ADMIN_USER_ID)})</i>')
    return '\n'.join(lines)


def cmd_status() -> str:
    now = _kst_now()
    lines = [f'<b>🤖 봇 상태</b>  <code>{now.strftime("%m/%d %H:%M KST")}</code>', '']

    # ── 스케줄러 heartbeat ──────────────────────────────────────────────
    hb_file = LOG_DIR / 'scheduler_heartbeat.json'
    try:
        with open(hb_file, 'r', encoding='utf-8') as f:
            hb = json.load(f)
        last_ts_str = hb.get('ts', '')
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo('Asia/Seoul')
        hb_dt = datetime.fromisoformat(last_ts_str)
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=_tz)
        is_alive = (now - hb_dt.astimezone(_tz)).total_seconds() < 600
        icon = '✅' if is_alive else '❌'
        lines.append(f'스케줄러: {icon} {"Running" if is_alive else "STOPPED"} ({_time_ago_str(hb_dt)})')
    except Exception:
        lines.append('스케줄러: ❓ heartbeat 없음')

    ctrl = _read_control()
    mode = os.environ.get('TRADING_MODE', 'paper')
    mode_icon = '🔴 Live' if mode == 'live' else '🟡 Paper'
    paused = ctrl.get('paused', False)
    pause_str = '  ⏸ 정지중' if paused else ''
    lines.append(f'모드: {mode_icon}{pause_str}')

    # 마지막 사이클 시각
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AiEvent
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo('Asia/Seoul')
        _s = get_session()
        try:
            _last = _s.query(AiEvent).order_by(AiEvent.ts.desc()).limit(1).first()
        finally:
            _s.close()
        if _last and _last.ts:
            _lk = _last.ts.astimezone(_tz)
            lines.append(f'마지막 사이클: {_lk.strftime("%H:%M")} ({_time_ago_str(_lk)})')
    except Exception:
        pass

    # ── 장세 판단 ──────────────────────────────────────────────────────
    lines += ['', '<b>📊 장세 판단</b>']
    try:
        from trading_bot.collectors.aggregator import get_market_context
        ctx = get_market_context()
        tradeable = ctx.get('is_tradeable', False)
        stale     = ctx.get('stale_but_usable', False)
        brs       = ctx.get('block_reasons', [])

        if tradeable:
            lines.append('L1: ✅ PASS')
        else:
            lines.append(f'L1: ❌ BLOCKED ({html.escape(", ".join(brs[:2]) or "?")})')

        macro_str = ('⚠️ STALE_BUT_USABLE' if stale
                     else ('🚨 EM7 (3일+)' if 'RATIO_STALE_EM7' in brs
                           else ('🚨 데이터 없음' if brs else 'NORMAL')))
        lines.append(f'매크로: {macro_str}')

        # Regime from SystemState (guardian global regime, not per-ticker)
        try:
            from trading_bot.config import DYN_THR_BY_REGIME
            from trading_bot.risk import get_system_state
            regime = get_system_state('last_guardian_regime', 'UNKNOWN') or 'UNKNOWN'
            _cap_map = {'BEAR_CONFIRMED': 0, 'BEAR_WARNING': 0, 'SIDEWAYS': 20,
                        'BULL_EARLY': 50, 'BULL_CONFIRMED': 70, 'BULL_CLIMAX': 80}
            cap = _cap_map.get(regime, 0)
            consec   = int(get_system_state('consec_losses', '0') or 0)
            base_thr = DYN_THR_BY_REGIME.get(regime, 1.0)
            dyn_thr  = min(0.99, base_thr + consec * 0.02)
            lines.append(f'L2: <b>{html.escape(regime)}</b> (cap {cap}%)')
            streak_str = f' + {consec}×0.02' if consec > 0 else ''
            lines.append(f'DYN_THR: <b>{dyn_thr:.2f}</b> (base {base_thr:.2f}{streak_str})')
        except Exception as _e:
            lines.append(f'L2: 조회 실패 ({html.escape(str(_e)[:50])})')
    except Exception as _e:
        lines.append(f'장세 조회 실패: {html.escape(str(_e)[:80])}')

    # ── 포트폴리오 ────────────────────────────────────────────────────
    lines += ['', '<b>💰 포트폴리오</b>']
    try:
        total_val, roi = _account_value_and_roi()
        if total_val is not None:
            roi_icon = '🟢' if roi >= 0 else '🔴'
            lines.append(f'총 평가금: <b>{_fmt_krw(total_val)}</b>  {roi_icon} {roi:+.1f}%')

        from trading_bot.executor import PaperExecutor, LiveExecutor
        _acv = float(os.environ.get('ACCOUNT_VALUE', '1000000'))
        _ex  = LiveExecutor() if mode == 'live' else PaperExecutor(initial_cash=_acv)
        if mode == 'live':
            _ex.refresh_balance_cache()
        krw = _ex.get_available_cash()
        cash_pct = krw / total_val * 100 if (total_val and total_val > 0) else 0.0
        lines.append(f'현금: {_fmt_krw(krw)} ({cash_pct:.1f}%)')

        from trading_bot.db import get_session
        from trading_bot.models import PositionState
        _s = get_session()
        try:
            _pos = _s.query(PositionState).filter(PositionState.avg_buy_price > 0).all()
        finally:
            _s.close()
        if _pos:
            _ts = ' '.join(
                f'<code>{html.escape(p.ticker.replace("KRW-", ""))}</code>'
                for p in _pos[:6]
            )
            lines.append(f'포지션: {len(_pos)}개 ({_ts})')
        else:
            lines.append('포지션: 없음')
    except Exception as _e:
        lines.append(f'포트폴리오 조회 실패: {html.escape(str(_e)[:80])}')

    # ── 오늘 실적 ─────────────────────────────────────────────────────
    lines += ['', '<b>📈 오늘 실적</b>']
    try:
        from trading_bot.analytics import get_dyn_thr_stats_today
        st = get_dyn_thr_stats_today()
        lines.append(f'매수: {st["buy_count"]}건 / 매도: {st["sell_count"]}건')
        lines.append(f'DYN_THR 통과/차단: {st["buy_count"]}/{st["skip_count"]}건')
        if st['sell_count'] > 0:
            icon = '🟢' if st['realized_roi_sum'] >= 0 else '🔴'
            lines.append(f'실현 ROI합: {icon} {st["realized_roi_sum"]:+.2f}%')
    except Exception:
        pass

    return '\n'.join(lines)


def cmd_balance() -> str:
    mode = os.environ.get('TRADING_MODE', 'paper')
    lines = [f'<b>💰 잔고 조회</b> (모드: <code>{html.escape(mode)}</code>)', '']
    try:
        from trading_bot.executor import PaperExecutor, LiveExecutor
        account_value = float(os.environ.get('ACCOUNT_VALUE', '1000000'))
        if mode == 'live':
            ex = LiveExecutor()
            if not getattr(ex, 'enabled', False):
                return '<b>💰 잔고 조회</b>\n\nLive 비활성화. TRADING_MODE=paper 또는 Live 설정 확인.'
            ex.refresh_balance_cache()
        else:
            ex = PaperExecutor(initial_cash=account_value)

        krw = ex.get_available_cash()
        lines.append(f'KRW 가용: <b>{krw:,.0f}원</b>')

        if mode == 'paper':
            tickers = list(getattr(ex, 'positions', {}).keys())
        else:
            cache = getattr(ex, '_balance_cache', {}) or {}
            tickers = [f'KRW-{c}' for c in cache if c != 'KRW' and (cache.get(c) or 0) > 0]

        if tickers:
            try:
                import pyupbit
                import json as _json
                from trading_bot.risk import get_system_state
                known_delisted = set(_json.loads(get_system_state('known_delisted_tickers', '[]') or '[]'))
            except Exception:
                pyupbit = None
                known_delisted = set()
            if pyupbit:
                for t in tickers[:15]:
                    if t in known_delisted:
                        continue
                    qty = ex.get_position_qty(t)
                    if qty <= 0:
                        continue
                    avg = ex.get_avg_buy_price(t)
                    try:
                        cur = pyupbit.get_current_price(t)
                        cur = float(cur) if cur is not None else 0.0
                    except Exception:
                        cur = 0.0
                    if avg and avg > 0 and cur > 0:
                        roi = (cur - avg) / avg * 100
                        val = qty * cur
                        roi_str = f'+{roi:.1f}%' if roi >= 0 else f'{roi:.1f}%'
                        lines.append(
                            f'• <code>{html.escape(t)}</code>: {cur:,.0f}원 '
                            f'({roi_str}, 평가 {val:,.0f}원)'
                        )
                    else:
                        lines.append(f'• <code>{html.escape(t)}</code>: {qty:.6f}')
            else:
                lines.append('보유 조회 오류: pyupbit 로드 실패')
        else:
            lines.append('보유 종목 없음')
    except Exception as e:
        lines.append(f'잔고 조회 실패: {html.escape(str(e))}')
    return '\n'.join(lines)


def cmd_report() -> str:
    lines = [f'<b>📊 주간 성과 리포트</b> (<code>{datetime.now().strftime("%Y-%m-%d")}</code>)', '']
    try:
        from trading_bot.analytics import get_trade_summary, get_risk_metrics
        s = get_trade_summary(7)
        r = get_risk_metrics(7)

        lines.append(f'기간: 최근 7일')
        lines.append(f'매수: <b>{s["total_buys"]}건</b> / 청산: <b>{s["total_exits"]}건</b>')
        if s['total_exits']:
            lines.append(f'승률: <b>{s["win_rate"]}%</b> ({s["wins"]}승 {s["losses"]}패)')
            lines.append(f'평균 수익: <b>+{s["avg_win_pct"]}%</b> / 평균 손실: <b>{s["avg_loss_pct"]}%</b>')
            if s['profit_factor']:
                lines.append(f'손익비: <b>{s["profit_factor"]}</b>')
            lines.append(f'최대 수익: +{s["max_win_pct"]}% / 최대 손실: {s["max_loss_pct"]}%')
        else:
            lines.append('(청산 기록 없음)')

        lines.append('')
        lines.append(f'🛡 MDD(근사): <b>-{r["mdd_approx_pct"]}%</b> | CB: {r["cb_count"]}회')
        lines.append(f'연속 손실: {r["consec_losses"]}회 | 오픈 포지션: {r["open_positions"]}')

        if s['daily_roi']:
            lines.append('')
            lines.append('<b>일별 ROI 합계</b>')
            for day, roi in sorted(s['daily_roi'].items()):
                sign = '+' if roi >= 0 else ''
                lines.append(f'{day}: {sign}{roi}%')
    except Exception as e:
        lines.append(f'리포트 조회 실패: {html.escape(str(e))}')
    return '\n'.join(lines)


def cmd_today() -> str:
    """오늘 매매 이벤트 상세 내역."""
    now = _kst_now()
    lines = [f'<b>📋 오늘 매매 내역</b>  <code>{now.strftime("%m/%d")}</code>', '']
    try:
        from trading_bot.analytics import get_today_events
        events = get_today_events()
        if not events:
            lines.append('오늘 이벤트 없음')
            return '\n'.join(lines)

        _TRADE_EVENTS = ('EXECUTE', 'STOP_LOSS', 'SCALE_OUT', 'DCA')
        _EVENT_ICON = {
            'EXECUTE': '✅', 'STOP_LOSS': '🛑', 'SCALE_OUT': '📤', 'DCA': '🔄',
        }

        # 실제 체결 이벤트만 상세 출력 (SKIP/STRATEGY 제외)
        trades = [e for e in events if e['event'] in _TRADE_EVENTS]
        skips  = [e for e in events if e['event'] == 'SKIP']

        if trades:
            for e in trades:
                ev  = e['event']
                sig = e['signal']
                tick = html.escape(e['ticker'].replace('KRW-', '') if e['ticker'] else '-')
                icon = _EVENT_ICON.get(ev, '•')
                ts_str = e['ts'].strftime('%H:%M')

                if ev == 'EXECUTE' and sig == 'buy':
                    label = '매수'
                elif ev == 'EXECUTE' and sig == 'sell':
                    label = '매도'
                elif ev == 'STOP_LOSS':
                    label = '손절'
                elif ev == 'SCALE_OUT':
                    label = '분할매도'
                elif ev == 'DCA':
                    label = 'DCA'
                else:
                    label = ev

                roi_str = ''
                if e['roi_pct'] is not None:
                    roi_icon = '🟢' if e['roi_pct'] >= 0 else '🔴'
                    roi_str = f'  {roi_icon} {e["roi_pct"]:+.1f}%'

                price_str = f'  {_fmt_krw(e["price"])}' if e['price'] else ''
                reason_short = html.escape((e['decision_reason'] or '')[:40])

                lines.append(f'{ts_str} {icon} <code>{tick}</code> {label}{price_str}{roi_str}')
                if reason_short:
                    lines.append(f'  └ {reason_short}')
        else:
            lines.append('오늘 체결 없음')

        # SKIP 요약 (개별 출력 대신 집계)
        if skips:
            lines.append('')
            lines.append(f'⏭ DYN_THR 차단: <b>{len(skips)}건</b>')
            blocked_tickers = list(dict.fromkeys(
                e['ticker'].replace('KRW-', '') for e in skips if e['ticker']
            ))[:10]
            if blocked_tickers:
                ticker_str = ' '.join(
                    f'<code>{html.escape(t)}</code>' for t in blocked_tickers
                )
                lines.append(f'주요: {ticker_str}')

    except Exception as _e:
        lines.append(f'조회 실패: {html.escape(str(_e)[:100])}')
    return '\n'.join(lines)


def cmd_week() -> str:
    """주간 상세 리포트 (기존 /report 확장)."""
    lines = [f'<b>📊 주간 성과 리포트</b>  <code>{_kst_now().strftime("%Y-%m-%d")}</code>', '']
    try:
        from trading_bot.analytics import (
            get_trade_summary, get_risk_metrics, get_rule_performance,
            get_regime_history, get_weekly_dyn_thr_by_day, get_best_worst_trades,
        )
        s = get_trade_summary(7)
        r = get_risk_metrics(7)

        lines.append(f'기간: 최근 7일')
        lines.append(f'매수: <b>{s["total_buys"]}건</b> / 청산: <b>{s["total_exits"]}건</b>')
        if s['total_exits']:
            lines.append(f'승률: <b>{s["win_rate"]}%</b> ({s["wins"]}승 {s["losses"]}패)')
            lines.append(f'평균 수익: <b>+{s["avg_win_pct"]}%</b> / 평균 손실: <b>{s["avg_loss_pct"]}%</b>')
            if s['profit_factor']:
                lines.append(f'손익비: <b>{s["profit_factor"]}</b>')
        else:
            lines.append('(청산 기록 없음)')

        lines.append(f'🛡 MDD(근사): <b>-{r["mdd_approx_pct"]}%</b>  CB: {r["cb_count"]}회  연속손실: {r["consec_losses"]}회')

        # 일별 ROI
        if s['daily_roi']:
            lines.append('')
            lines.append('<b>일별 ROI</b>')
            for day, roi in sorted(s['daily_roi'].items()):
                icon = '🟢' if roi >= 0 else '🔴'
                lines.append(f'{day}: {icon} {roi:+.2f}%')

        # DYN_THR 일별 통과율
        dyn_days = get_weekly_dyn_thr_by_day(7)
        if dyn_days:
            lines.append('')
            lines.append('<b>DYN_THR 일별 통과/차단</b>')
            for day in sorted(dyn_days):
                d = dyn_days[day]
                total = d['execute'] + d['skip']
                pass_rate = d['execute'] / total * 100 if total else 0
                lines.append(f'{day}: ✅{d["execute"]} / ⏭{d["skip"]} ({pass_rate:.0f}%)')

        # 규칙별 Top5 (매수)
        rp = get_rule_performance(7)
        buy_rules = sorted(rp.get('buy_rules', {}).items(), key=lambda x: -x[1])[:5]
        if buy_rules:
            lines.append('')
            lines.append('<b>매수 규칙 Top5</b>')
            for rule, cnt in buy_rules:
                lines.append(f'• {html.escape(rule)}: {cnt}회')

        # 규칙별 Top5 (청산)
        exit_rules = list(rp.get('exit_rules', {}).items())[:5]
        if exit_rules:
            lines.append('')
            lines.append('<b>청산 규칙 Top5</b>')
            for rule, st in exit_rules:
                lines.append(
                    f'• {html.escape(rule)}: {st["count"]}회  '
                    f'승률{st["win_rate"]}%  avg{st["avg_roi"]:+.1f}%'
                )

        # Best / Worst 트레이드
        bw = get_best_worst_trades(7)
        if bw:
            lines.append('')
            lines.append('<b>Best / Worst</b>')
            if 'best' in bw:
                b = bw['best']
                lines.append(
                    f'🟢 Best: <code>{html.escape(b["ticker"] or "-")}</code> '
                    f'{b["roi_pct"]:+.2f}%  ({b["ts"].strftime("%m/%d")})'
                )
            if 'worst' in bw:
                w = bw['worst']
                lines.append(
                    f'🔴 Worst: <code>{html.escape(w["ticker"] or "-")}</code> '
                    f'{w["roi_pct"]:+.2f}%  ({w["ts"].strftime("%m/%d")})'
                )

        # 장세 전환 이력
        regime_hist = get_regime_history(7)
        if regime_hist:
            lines.append('')
            lines.append('<b>장세 전환 이력</b>')
            for entry in regime_hist[-8:]:  # 최근 8건
                lines.append(
                    f'{entry["ts"].strftime("%m/%d %H:%M")}  {html.escape(entry["regime"])}'
                )

    except Exception as _e:
        lines.append(f'리포트 조회 실패: {html.escape(str(_e)[:80])}')
    return '\n'.join(lines)


def cmd_positions() -> str:
    """보유 포지션 상세 — ROI · trailing · scale-out 단계."""
    lines = ['<b>📌 보유 포지션 상세</b>', '']
    try:
        import pyupbit
        from trading_bot.db import get_session
        from trading_bot.models import PositionState

        session = get_session()
        try:
            positions = session.query(PositionState).filter(
                PositionState.avg_buy_price > 0
            ).order_by(PositionState.updated_at.desc()).all()
        finally:
            session.close()

        if not positions:
            lines.append('보유 포지션 없음')
            return '\n'.join(lines)

        import json as _json
        from trading_bot.risk import get_system_state
        known_del = set(_json.loads(get_system_state('known_delisted_tickers', '[]') or '[]'))

        for p in positions:
            if p.ticker in known_del:
                continue
            avg = float(p.avg_buy_price or 0)
            if avg <= 0:
                continue

            try:
                cur = float(pyupbit.get_current_price(p.ticker) or 0)
            except Exception:
                cur = 0.0

            roi = (cur - avg) / avg * 100 if (cur > 0 and avg > 0) else 0.0
            roi_icon = '🟢' if roi >= 0 else '🔴'
            trail_high = float(p.trailing_high or 0)
            stage = int(p.stage or 0)

            # 보유 기간 — AiEvent 최근 EXECUTE(buy) ts 조회
            holding_str = ''
            try:
                from trading_bot.db import get_session
                from trading_bot.models import AiEvent
                _s = get_session()
                try:
                    _buy = _s.query(AiEvent).filter(
                        AiEvent.ticker == p.ticker,
                        AiEvent.event == 'EXECUTE',
                        AiEvent.signal == 'buy',
                    ).order_by(AiEvent.ts.desc()).limit(1).first()
                finally:
                    _s.close()
                if _buy and _buy.ts:
                    holding_str = f'  보유 {_time_ago_str(_buy.ts.astimezone(_kst_now().tzinfo))}'
            except Exception:
                pass

            tick_label = html.escape(p.ticker.replace('KRW-', ''))
            lines.append(f'<b><code>{tick_label}</code></b>  {roi_icon} {roi:+.1f}%')
            lines.append(f'  매수가: {_fmt_krw(avg)}  현재가: {_fmt_krw(cur) if cur > 0 else "조회실패"}')
            lines.append(f'  Scale-out: {stage}/2단계{holding_str}')
            if trail_high > 0:
                trail_drop = (trail_high - cur) / trail_high * 100 if cur > 0 else 0
                lines.append(f'  Trailing: 고점 {_fmt_krw(trail_high)}  ({trail_drop:.1f}% 하락중)')
            lines.append('')

    except Exception as _e:
        lines.append(f'조회 실패: {html.escape(str(_e)[:80])}')
    return '\n'.join(lines).rstrip()


def cmd_guardian() -> str:
    """매크로 L1/L2 필터 상세."""
    lines = ['<b>🛡 MarketGuardian 상세</b>', '']
    try:
        from trading_bot.collectors.aggregator import get_market_context
        from trading_bot.market_guardian import MarketGuardian
        ctx = get_market_context()
        result = MarketGuardian().evaluate()

        macro  = ctx.get('macro') or {}
        dom    = ctx.get('dominance') or {}
        stale  = ctx.get('stale_but_usable', False)
        brs    = ctx.get('block_reasons', [])

        # L1 평가
        if result.tradeable:
            lines.append('L1: ✅ PASS')
        else:
            lines.append(f'L1: ❌ BLOCKED')
            for br in result.block_reasons:
                lines.append(f'  • {html.escape(br)}')
        if result.flags:
            lines.append(f'플래그: {html.escape(", ".join(result.flags))}')

        # 매크로 상세
        lines += ['', '<b>매크로 지표</b>']
        dxy = macro.get('dxy_value')
        ndx = macro.get('nasdaq_value')
        ratio = macro.get('nasdaq_dxy_ratio')
        gold = macro.get('gold_value')
        bond_sig = macro.get('bond_signal', '-')
        crisis = macro.get('crisis_level', '-')
        lines.append(f'DXY: {f"{dxy:.2f}" if dxy else "-"}  NDX: {f"{ndx:.0f}" if ndx else "-"}')
        lines.append(f'교환비: {f"{ratio:.1f}" if ratio else "-"}  zone: {macro.get("nasdaq_dxy_zone", "-")}')
        lines.append(f'Gold: {f"{gold:.1f}" if gold else "-"}  bond: {bond_sig}  crisis: {crisis}')
        lines.append(f'JPY: {macro.get("jpy_signal", "-")}  oil_vol: {"ON" if macro.get("oil_vol_active") else "OFF"}')

        # 데이터 신선도
        lines += ['', '<b>데이터 신선도</b>']
        ts = macro.get('ts')
        if ts:
            lines.append(f'macro 최종 수집: {html.escape(str(ts)[:16])} ({_time_ago_str(str(ts)[:19])})')
        if stale:
            lines.append('⚠️ STALE_BUT_USABLE — 사이즈 ×0.5 적용')
        elif brs:
            lines.append(f'🚨 차단: {html.escape(", ".join(brs))}')
        else:
            lines.append('✅ FRESH (26h 이내)')

        # L2 판단
        lines += ['', '<b>L2 장세 분류</b>']
        lines.append(f'regime: <b>{html.escape(result.regime)}</b>')
        lines.append(f'position_cap: {int(result.position_cap * 100)}%')
        lines.append(f'buy_size_multiplier: {result.buy_size_multiplier:.1f}×')
        lines.append(f'allow_new_entry: {"✅" if result.allow_new_entry else "❌"}')
        lines.append(f'block_alt_buys: {"🚫" if result.block_alt_buys else "✅"}')

        btc_dom = dom.get('btc_dominance')
        bull_stage = dom.get('bull_stage', '-')
        if btc_dom:
            lines.append(f'BTC.D: {btc_dom:.1f}%  bull_stage: {html.escape(bull_stage)}')

    except Exception as _e:
        lines.append(f'조회 실패: {html.escape(str(_e)[:100])}')
    return '\n'.join(lines)


def cmd_health() -> str:
    """데이터 수집 건강 체크 — 각 데이터의 나이와 상태 표시."""
    from datetime import timezone as _tz, datetime as _dt
    import html as _html

    now_kst = _kst_now()
    lines = [f'<b>🏥 데이터 건강</b>  <code>{now_kst.strftime("%m/%d %H:%M KST")}</code>', '']

    def _age_h(ts) -> float:
        if ts is None:
            return float('inf')
        try:
            import pandas as pd
            ts_pd = pd.Timestamp(ts)
            if ts_pd.tzinfo is None:
                ts_pd = ts_pd.tz_localize('UTC')
            else:
                ts_pd = ts_pd.tz_convert('UTC')
            return (_dt.now(_tz.utc) - ts_pd.to_pydatetime()).total_seconds() / 3600
        except Exception:
            return float('inf')

    def _status(age_h: float, warn_h: float, crit_h: float) -> str:
        if age_h == float('inf'):
            return '🚨 MISSING'
        if age_h > crit_h:
            return '🚨 MISSING'
        if age_h > warn_h:
            return '⚠️ STALE'
        return '✅ FRESH'

    try:
        from trading_bot.collectors import macro as _mac
        from trading_bot.collectors import dominance as _dom
        from trading_bot.collectors import kimp as _kimp_col
        from trading_bot.collectors import btc_weekly as _btcw
        from trading_bot.collectors.sentiment import get_latest as _fng_latest

        macro_row = _mac.get_latest()
        dom_row   = _dom.get_latest()
        kimp_row  = _kimp_col.get_latest()
        btcw_row  = _btcw.get_latest()
        fng_row   = _fng_latest()

        # 매크로 (26h STALE_BUT_USABLE, 72h MISSING)
        mac_age   = _age_h(macro_row.get('ts') if macro_row else None)
        mac_status = _status(mac_age, 26, 72)
        mac_zone   = macro_row.get('nasdaq_dxy_zone', '-') if macro_row else '-'
        lines.append(f'매크로:     {mac_status} ({mac_age:.1f}h)  zone={_html.escape(mac_zone)}')

        # 도미넌스 (3h STALE, 12h MISSING)
        dom_age    = _age_h(dom_row.get('ts') if dom_row else None)
        dom_status = _status(dom_age, 3, 12)
        dom_stage  = dom_row.get('bull_stage', '-') if dom_row else '-'
        lines.append(f'도미넌스:   {dom_status} ({dom_age:.1f}h)  stage={_html.escape(dom_stage)}')

        # 김프 (12h STALE, 48h MISSING)
        kmp_age    = _age_h(kimp_row.get('ts') if kimp_row else None)
        kmp_status = _status(kmp_age, 12, 48)
        kmp_pct    = f"{kimp_row.get('kimp_pct', 0):.2f}%" if kimp_row else '-'
        lines.append(f'김프:       {kmp_status} ({kmp_age:.1f}h)  kimp={kmp_pct}')

        # FNG (6h STALE, 24h MISSING)
        fng_age    = _age_h(fng_row.get('ts') if fng_row else None)
        fng_status = _status(fng_age, 6, 24)
        fng_val    = f"{int(fng_row.get('value', 0))} {_html.escape(fng_row.get('label', ''))}" if fng_row else '-'
        lines.append(f'FNG:        {fng_status} ({fng_age:.1f}h)  val={fng_val}')

        # BTC 주봉 200MA (24h STALE, 48h MISSING)
        btcw_age    = _age_h(btcw_row.get('ts') if btcw_row else None)
        btcw_status = _status(btcw_age, 24, 48)
        btcw_above  = ('above' if btcw_row.get('above_ma200') else 'below') if btcw_row else '-'
        lines.append(f'BTC 200MA:  {btcw_status} ({btcw_age:.1f}h)  {btcw_above}')

    except Exception as e:
        lines.append(f'조회 실패: {_html.escape(str(e)[:100])}')

    return '\n'.join(lines)


def cmd_signals() -> str:
    """최근 6시간 신호 요약."""
    now = _kst_now()
    lines = [f'<b>📡 최근 신호 요약</b>  <code>{now.strftime("%H:%M")} 기준</code>', '']
    try:
        from trading_bot.analytics import get_recent_signals
        sig = get_recent_signals(hours=6)

        lines.append(f'분석 종목: {sig["analyzed_count"]}개 (최근 6시간)')

        if sig['buy_pass']:
            tickers = ', '.join(
                f'<code>{html.escape(t.replace("KRW-",""))}</code>'
                for t in sig['buy_pass'][:8]
            )
            lines.append(f'✅ 매수 통과: {tickers}')

        if sig['buy_block']:
            tickers = ', '.join(
                f'<code>{html.escape(t.replace("KRW-",""))}</code>'
                for t in sig['buy_block'][:8]
            )
            lines.append(f'⏭ DYN_THR 차단: {tickers}')

        if sig['sell_list']:
            tickers = ', '.join(
                f'<code>{html.escape(t.replace("KRW-",""))}</code>'
                for t in sig['sell_list'][:8]
            )
            lines.append(f'📤 매도: {tickers}')

        if not sig['buy_pass'] and not sig['buy_block'] and not sig['sell_list']:
            lines.append('신호 없음 (패턴 미감지 또는 전체 스킵)')

        if sig['strength_top3']:
            lines.append('')
            lines.append('<b>Pattern Strength Top3</b>')
            for i, (t, s) in enumerate(sig['strength_top3'], 1):
                lines.append(
                    f'{i}. <code>{html.escape(t.replace("KRW-",""))}</code>  str={s:.3f}'
                )

    except Exception as _e:
        lines.append(f'조회 실패: {html.escape(str(_e)[:80])}')
    return '\n'.join(lines)


def cmd_pause(from_user_id: str) -> str:
    if ADMIN_USER_ID and from_user_id != ADMIN_USER_ID:
        return '🚫 권한 없음: 관리자만 매매를 일시 정지할 수 있습니다.'
    try:
        ctrl = _read_control()
        if ctrl.get('paused'):
            return '⏸ 이미 일시 정지 상태입니다. /resume 으로 재개하세요.'
        ctrl['paused'] = True
        ctrl['paused_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ctrl['paused_by'] = from_user_id
        _write_control(ctrl)
        return (
            '⏸ <b>매매 일시 정지 요청됨</b>\n\n'
            '다음 사이클부터 매매가 건너뜁니다.\n'
            '재개하려면 /resume 을 보내세요.'
        )
    except Exception as e:
        return f'일시 정지 실패: {html.escape(str(e))}'


def cmd_resume(from_user_id: str) -> str:
    if ADMIN_USER_ID and from_user_id != ADMIN_USER_ID:
        return '🚫 권한 없음: 관리자만 매매를 재개할 수 있습니다.'
    try:
        ctrl = _read_control()
        if not ctrl.get('paused'):
            return '▶️ 이미 정상 작동 중입니다.'
        ctrl['paused'] = False
        ctrl['resumed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ctrl['resumed_by'] = from_user_id
        _write_control(ctrl)
        return '▶️ <b>매매 재개됨</b>\n\n다음 사이클부터 정상 매매합니다.'
    except Exception as e:
        return f'재개 실패: {html.escape(str(e))}'


def cmd_panic(from_user_id: str) -> str:
    """
    [H1 FIX] .env 파일 쓰기 제거 → DB SystemState에 영구 저장.
    K8s Pod 재시작 시 Ephemeral Storage의 .env 수정은 소실되지만,
    PostgreSQL SystemState 테이블 기록은 재시작 후에도 유지된다.
    LiveExecutor._reload_env_flags()가 30초 TTL로 이 값을 읽는다.
    """
    if ADMIN_USER_ID and from_user_id != ADMIN_USER_ID:
        return '🚫 권한 없음: 관리자만 패닉 버튼을 사용할 수 있습니다.'
    results = []

    # 1) DB SystemState에 ENABLE_AUTO_LIVE=0 영구 저장 (pod 재시작 후에도 유지)
    try:
        from trading_bot.db import get_session, engine
        from trading_bot.models import Base, SystemState
        try:
            Base.metadata.create_all(engine, tables=[SystemState.__table__])
        except Exception:
            pass
        session = get_session()
        try:
            row = session.query(SystemState).filter(SystemState.key == 'enable_auto_live').first()
            if row:
                row.value = '0'
            else:
                session.add(SystemState(key='enable_auto_live', value='0'))
            session.commit()
        finally:
            session.close()
        os.environ['ENABLE_AUTO_LIVE'] = '0'
        results.append('✅ ENABLE_AUTO_LIVE=0 DB 영구 저장 (pod 재시작 후에도 유지)')
    except Exception as e:
        results.append(f'⚠️ DB 저장 실패: {html.escape(str(e))}')

    # 2) bot_control.json paused=true (매매 사이클 즉시 정지)
    try:
        ctrl = _read_control()
        ctrl['paused'] = True
        ctrl['paused_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ctrl['paused_by'] = from_user_id
        ctrl['panic'] = True
        _write_control(ctrl)
        results.append('✅ 매매 사이클 일시 정지 플래그 설정')
    except Exception as e:
        results.append(f'⚠️ bot_control 저장 실패: {html.escape(str(e))}')

    status_block = '\n'.join(results)
    return (
        f'🛑 <b>패닉 버튼 실행됨</b>\n\n'
        f'{status_block}\n\n'
        f'자동 라이브 매매가 중지됩니다.\n'
        f'재개하려면 관리자가 DB에서 <code>enable_auto_live=1</code>로 변경 후 /resume 을 보내세요.'
    )


# ---------------------------------------------------------------------------
# 공개 briefing 함수 (market_briefing.py에서 호출)
# ---------------------------------------------------------------------------

def _btc_global_trend() -> str:
    try:
        from trading_bot.tasks.auto_trader import check_btc_global_trend
        is_bull = check_btc_global_trend(interval='day', count=50)
        return '🟢 Bull' if is_bull else '🔴 Bear'
    except Exception:
        return '—'


def _account_value_and_roi() -> tuple:
    try:
        from trading_bot.executor import PaperExecutor, LiveExecutor
        account_value = float(os.environ.get('ACCOUNT_VALUE', '1000000'))
        mode = os.environ.get('TRADING_MODE', 'paper')
        if mode == 'live':
            ex = LiveExecutor()
            if not getattr(ex, 'enabled', False):
                return None, None
            ex.refresh_balance_cache()
        else:
            ex = PaperExecutor(initial_cash=account_value)
        krw = ex.get_available_cash()
        if mode == 'paper':
            tickers = list(getattr(ex, 'positions', {}).keys())
        else:
            cache = getattr(ex, '_balance_cache', {}) or {}
            tickers = [f'KRW-{c}' for c in cache if c != 'KRW' and (cache.get(c) or 0) > 0]
        cost_basis = krw
        total_value = krw
        try:
            import pyupbit
            import json as _json
            from trading_bot.risk import get_system_state
            known_delisted = set(_json.loads(get_system_state('known_delisted_tickers', '[]') or '[]'))
        except Exception:
            pyupbit = None
            known_delisted = set()
        if pyupbit:
            for t in tickers:
                if t in known_delisted:
                    continue
                qty = ex.get_position_qty(t)
                if qty <= 0:
                    continue
                avg = ex.get_avg_buy_price(t)
                try:
                    cur = pyupbit.get_current_price(t)
                    cur = float(cur) if cur is not None else float(avg or 0)
                except Exception:
                    cur = float(avg or 0)
                cost_basis += (avg or 0) * qty
                total_value += cur * qty
        roi = (total_value - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0.0
        return total_value, roi
    except Exception:
        return None, None


def _pnl_last_24h() -> tuple:
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Order  # 실제 라이브/페이퍼 체결은 Order 테이블 (Trade는 백테스트 전용)
        session = get_session()
        cutoff = datetime.utcnow() - timedelta(hours=24)
        rows = session.query(Order).filter(
            Order.ts >= cutoff,
        ).all()
        session.close()
        buys = [r for r in rows if (r.side or '').lower() == 'buy']
        sells = [r for r in rows if (r.side or '').lower() == 'sell']
        buy_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in buys)
        sell_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in sells)
        return buy_sum, sell_sum, sell_sum - buy_sum, len(rows)
    except Exception:
        return 0.0, 0.0, 0.0, 0


def _top3_adx_tickers() -> list:
    try:
        from trading_bot.db import get_session
        from trading_bot.models import TechnicalIndicator
        session = get_session()
        rows = (
            session.query(TechnicalIndicator)
            .filter(TechnicalIndicator.timeframe == 'minute60')
            .order_by(TechnicalIndicator.ts.desc())
            .limit(500)
            .all()
        )
        session.close()
        seen = {}
        for r in rows:
            if r.ticker in seen:
                continue
            ind = r.indicators if isinstance(r.indicators, dict) else {}
            adx = ind.get('adx')
            if adx is not None:
                try:
                    seen[r.ticker] = float(adx)
                except (TypeError, ValueError):
                    pass
        return sorted(seen.items(), key=lambda x: -x[1])[:3]
    except Exception:
        return []


def send_briefing(chat_id: str = None) -> bool:
    """주기 시장 브리핑 전송 (market_briefing.py 에서 호출)."""
    lines = ['<b>📰 Market Briefing</b>', f'<code>{datetime.now().strftime("%Y-%m-%d %H:%M")}</code>', '']
    lines.append(f'• BTC 추세: {_btc_global_trend()}')
    total_val, roi = _account_value_and_roi()
    if total_val is not None:
        roi_str = f'+{roi:.1f}%' if roi >= 0 else f'{roi:.1f}%'
        lines.append(f'• 계좌 총액: <b>{total_val:,.0f}원</b> (ROI {roi_str})')
    else:
        lines.append('• 계좌: 조회 불가 (Live 비활성 등)')
    buy_24, sell_24, pnl_24, n_24 = _pnl_last_24h()
    pnl_str = f'+{pnl_24:,.0f}원' if pnl_24 >= 0 else f'{pnl_24:,.0f}원'
    lines.append(f'• 최근 24h: {n_24}건 | 매수 {buy_24:,.0f} / 매도 {sell_24:,.0f} | P&L <b>{pnl_str}</b>')
    top3 = _top3_adx_tickers()
    if top3:
        top3_str = ', '.join(f'<code>{html.escape(t)}</code>({a:.0f})' for t, a in top3)
        lines.append(f'• ADX 상위 3: {top3_str}')
    else:
        lines.append('• ADX 상위: 데이터 없음')
    return _send('\n'.join(lines), chat_id=chat_id or CHAT_ID)


def send_daily_briefing(chat_id: str = None) -> bool:
    """일일 브리핑 (매일 09:01 KST 스케줄러 호출)."""
    now = _kst_now()
    lines = [f'<b>📅 일일 브리핑</b>  <code>{now.strftime("%Y-%m-%d %H:%M KST")}</code>', '']

    # 전일 성과 요약
    try:
        from trading_bot.analytics import get_trade_summary, get_risk_metrics
        s = get_trade_summary(1)
        r = get_risk_metrics(1)
        lines.append('<b>전일 요약</b>')
        lines.append(f'매수: {s["total_buys"]}건 / 청산: {s["total_exits"]}건')
        if s['total_exits']:
            icon = '🟢' if s['avg_win_pct'] >= 0 else '🔴'
            lines.append(f'승률: {s["win_rate"]}%  {icon} avg {s["avg_win_pct"]:+.2f}%')
        lines.append(f'MDD(근사): -{r["mdd_approx_pct"]}%  CB: {r["cb_count"]}회')
    except Exception:
        lines.append('전일 요약 조회 실패')

    # 장세 판단
    lines.append('')
    lines.append('<b>장세 판단</b>')
    try:
        from trading_bot.collectors.aggregator import get_market_context
        from trading_bot.config import DYN_THR_BY_REGIME
        from trading_bot.risk import get_system_state
        ctx = get_market_context()
        tradeable = ctx.get('is_tradeable', False)
        stale     = ctx.get('stale_but_usable', False)
        brs       = ctx.get('block_reasons', [])
        l1 = '✅ PASS' if tradeable else f'❌ {", ".join(brs[:2])}'
        lines.append(f'L1: {l1}')
        macro_str = '⚠️ STALE' if stale else ('🚨 EM7' if brs else 'NORMAL')
        lines.append(f'매크로: {macro_str}')
        regime = get_system_state('last_guardian_regime', 'UNKNOWN') or 'UNKNOWN'
        _cap_map = {'BEAR_CONFIRMED': 0, 'BEAR_WARNING': 0, 'SIDEWAYS': 20,
                    'BULL_EARLY': 50, 'BULL_CONFIRMED': 70, 'BULL_CLIMAX': 80}
        cap = _cap_map.get(regime, 0)
        consec   = int(get_system_state('consec_losses', '0') or 0)
        base_thr = DYN_THR_BY_REGIME.get(regime, 1.0)
        dyn_thr  = min(0.99, base_thr + consec * 0.02)
        lines.append(f'L2: {html.escape(regime)} (cap {cap}%  DYN_THR {dyn_thr:.2f})')
    except Exception:
        lines.append('장세 조회 실패')

    # 포트폴리오 요약
    lines.append('')
    lines.append('<b>포트폴리오</b>')
    try:
        total_val, roi = _account_value_and_roi()
        if total_val is not None:
            roi_icon = '🟢' if roi >= 0 else '🔴'
            lines.append(f'총 평가금: {_fmt_krw(total_val)}  {roi_icon} {roi:+.1f}%')
        from trading_bot.db import get_session
        from trading_bot.models import PositionState
        _s = get_session()
        try:
            _pos = _s.query(PositionState).filter(PositionState.avg_buy_price > 0).all()
        finally:
            _s.close()
        if _pos:
            _ts = ' '.join(
                f'<code>{html.escape(p.ticker.replace("KRW-",""))}</code>'
                for p in _pos[:6]
            )
            lines.append(f'오픈 포지션: {len(_pos)}개 ({_ts})')
        else:
            lines.append('오픈 포지션: 없음')
    except Exception:
        lines.append('포트폴리오 조회 실패')

    return _send_multipart('\n'.join(lines), chat_id=chat_id or CHAT_ID)


def notify_regime_change(old_regime: str, new_regime: str,
                         position_cap: float, dyn_thr: float,
                         chat_id: str = None) -> None:
    """장세 전환 즉시 알림 (market_guardian.py에서 호출)."""
    msg = (
        f'🔄 <b>장세 전환</b>\n'
        f'{html.escape(old_regime)} → <b>{html.escape(new_regime)}</b>\n'
        f'cap {int(position_cap * 100)}%  |  DYN_THR {dyn_thr:.2f}'
    )
    _send(msg, chat_id=chat_id or CHAT_ID)


# ---------------------------------------------------------------------------
# 메시지 라우팅
# ---------------------------------------------------------------------------

def handle_message(text: str, chat_id: str, from_user_id: str) -> str:
    if not text or not text.strip():
        return ''
    cmd = text.strip().split()[0].lower()

    if not _is_authorized(chat_id, from_user_id, cmd):
        if CHAT_ID and chat_id != CHAT_ID:
            return ''  # 다른 채팅방은 조용히 무시
        return '🚫 권한 없음: 관리자만 이 명령을 사용할 수 있습니다.'

    if cmd in ('/help', '/start'):
        return cmd_help()
    if cmd == '/status':
        return cmd_status()
    if cmd == '/today':
        return cmd_today()
    if cmd in ('/week', '/report'):
        return cmd_week()
    if cmd == '/positions':
        return cmd_positions()
    if cmd == '/guardian':
        return cmd_guardian()
    if cmd == '/health':
        return cmd_health()
    if cmd == '/signals':
        return cmd_signals()
    if cmd == '/balance':
        return cmd_balance()
    if cmd == '/pause':
        return cmd_pause(from_user_id)
    if cmd == '/resume':
        return cmd_resume(from_user_id)
    if cmd == '/panic':
        return cmd_panic(from_user_id)
    if cmd.startswith('/'):
        return '알 수 없는 명령. /help 로 도움말 확인.'
    return ''


# ---------------------------------------------------------------------------
# Long polling
# ---------------------------------------------------------------------------

def poll_once(offset: int) -> tuple:
    """Returns (next_offset, list of (chat_id, from_user_id, text))."""
    if not TOKEN:
        return offset, []
    try:
        r = requests.get(
            f"{BASE_URL}/getUpdates",
            params={'offset': offset, 'timeout': 30},
            timeout=35,
        )
        if r.status_code != 200:
            return offset, []
        data = r.json()
        if not data.get('ok'):
            return offset, []
        updates = data.get('result') or []
        out = []
        next_offset = offset
        for u in updates:
            next_offset = u.get('update_id', 0) + 1
            msg = u.get('message') or {}
            chat_id = str(msg.get('chat', {}).get('id', ''))
            from_id = str(msg.get('from', {}).get('id', ''))
            text = (msg.get('text') or '').strip()
            if chat_id and text:
                out.append((chat_id, from_id, text))
        return next_offset, out
    except Exception:
        return offset, []


def main():
    if not TOKEN or not CHAT_ID:
        print('TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 없습니다. trading_bot/.env를 확인하세요.')
        sys.exit(1)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    offset = 0
    auth_info = f'admin_user_id={ADMIN_USER_ID}' if ADMIN_USER_ID else '인증=CHAT_ID만'
    print(f'Telegram bot polling (chat_id={CHAT_ID}, {auth_info}). Ctrl+C to stop.')
    while True:
        try:
            offset, updates = poll_once(offset)
            for chat_id, from_user_id, text in updates:
                reply = handle_message(text, chat_id, from_user_id)
                if reply:
                    _send_multipart(reply, chat_id=chat_id)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f'Poll error: {e}')
            time.sleep(5)
    print('Telegram bot stopped.')


if __name__ == '__main__':
    main()
