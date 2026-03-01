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
# 명령 핸들러
# ---------------------------------------------------------------------------

def cmd_help() -> str:
    lines = [
        '<b>🤖 Trading Bot 명령어</b>',
        '',
        '/help   — 이 도움말',
        '/status — 스케줄러 상태 및 사이클 진행률',
        '/balance — KRW·보유 종목·개별 ROI',
        '/report — 오늘 체결 및 실현 P&L',
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
    lines = ['<b>📊 봇 상태</b>', '']

    # 사이클 진행률
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            phase = html.escape(str(data.get('phase', 'idle')))
            task = html.escape(str(data.get('task') or '-'))
            percent = data.get('percent', 0)
            bar_filled = int(percent / 10)
            bar = '█' * bar_filled + '░' * (10 - bar_filled)
            lines.append(f'단계: <b>{phase}</b>')
            lines.append(f'작업: {task}')
            lines.append(f'진행: [{bar}] {percent}%')
            updated = data.get('updated_at', '')
            if updated:
                lines.append(f'갱신: <code>{html.escape(updated)}</code>')
        except Exception as e:
            lines.append(f'진행 파일 읽기 실패: {html.escape(str(e))}')
    else:
        lines.append('진행 정보 없음 (progress.json 없음)')

    # pause 상태
    ctrl = _read_control()
    paused = ctrl.get('paused', False)
    lines.append('')
    if paused:
        since = ctrl.get('paused_at', '')
        lines.append(f'⏸ <b>매매 일시 정지 중</b>' + (f' (<code>{html.escape(since)}</code>부터)' if since else ''))
    else:
        lines.append('▶️ 매매 정상 작동 중')

    # heartbeat
    hb_file = LOG_DIR / 'scheduler_heartbeat.json'
    if hb_file.exists():
        try:
            with open(hb_file, 'r', encoding='utf-8') as f:
                hb = json.load(f)
            last_hb = hb.get('ts', '')
            lines.append(f'💓 Heartbeat: <code>{html.escape(last_hb)}</code>')
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
                for t in tickers[:15]:
                    qty = ex.get_position_qty(t)
                    if qty <= 0:
                        continue
                    avg = ex.get_avg_buy_price(t)
                    cur = pyupbit.get_current_price(t)
                    cur = float(cur) if cur is not None else 0.0
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
            except Exception as e:
                lines.append(f'보유 조회 오류: {html.escape(str(e))}')
        else:
            lines.append('보유 종목 없음')
    except Exception as e:
        lines.append(f'잔고 조회 실패: {html.escape(str(e))}')
    return '\n'.join(lines)


def cmd_report() -> str:
    lines = [f'<b>📋 오늘 체결 리포트</b> (<code>{datetime.now().strftime("%Y-%m-%d")}</code>)', '']
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Order
        session = get_session()
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = session.query(Order).filter(Order.ts >= today_start).all()
        session.close()
        buys = [r for r in rows if (r.side or '').lower() == 'buy']
        sells = [r for r in rows if (r.side or '').lower() == 'sell']
        buy_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in buys)
        sell_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in sells)
        pnl = sell_sum - buy_sum
        pnl_str = f'+{pnl:,.0f}원' if pnl >= 0 else f'{pnl:,.0f}원'
        lines.append(f'매수: <b>{len(buys)}건</b>  {buy_sum:,.0f}원')
        lines.append(f'매도: <b>{len(sells)}건</b>  {sell_sum:,.0f}원')
        lines.append(f'실현 P&L: <b>{pnl_str}</b>')
    except Exception as e:
        lines.append(f'리포트 조회 실패: {html.escape(str(e))}')
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
    if ADMIN_USER_ID and from_user_id != ADMIN_USER_ID:
        return '🚫 권한 없음: 관리자만 패닉 버튼을 사용할 수 있습니다.'
    results = []

    # 1) .env ENABLE_AUTO_LIVE=0
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            new_lines = []
            found = False
            for L in lines:
                if L.strip().startswith('ENABLE_AUTO_LIVE='):
                    new_lines.append('ENABLE_AUTO_LIVE=0\n')
                    found = True
                else:
                    new_lines.append(L)
            if not found:
                new_lines.append('ENABLE_AUTO_LIVE=0\n')
            with open(ENV_PATH, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            os.environ['ENABLE_AUTO_LIVE'] = '0'
            results.append('✅ ENABLE_AUTO_LIVE=0 적용')
        except Exception as e:
            results.append(f'⚠️ .env 수정 실패: {html.escape(str(e))}')
    else:
        results.append('⚠️ .env 파일 없음 — ENABLE_AUTO_LIVE 수정 건너뜀')

    # 2) bot_control.json paused=true
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
        f'재개하려면 .env에서 <code>ENABLE_AUTO_LIVE=1</code>로 변경 후 /resume 을 보내세요.'
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
            for t in tickers:
                qty = ex.get_position_qty(t)
                if qty <= 0:
                    continue
                avg = ex.get_avg_buy_price(t)
                cur = pyupbit.get_current_price(t)
                cur = float(cur) if cur is not None else 0.0
                cost_basis += (avg or 0) * qty
                total_value += cur * qty
        except Exception:
            pass
        roi = (total_value - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0.0
        return total_value, roi
    except Exception:
        return None, None


def _pnl_last_24h() -> tuple:
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Trade
        session = get_session()
        cutoff = datetime.utcnow() - timedelta(hours=24)
        rows = session.query(Trade).filter(
            Trade.ts >= cutoff,
            Trade.backtest_id == None,  # 백테스트 시뮬레이션 제외, 실제 라이브 거래만
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
    if cmd == '/balance':
        return cmd_balance()
    if cmd == '/report':
        return cmd_report()
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
                    _send(reply, chat_id=chat_id)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f'Poll error: {e}')
            time.sleep(5)
    print('Telegram bot stopped.')


if __name__ == '__main__':
    main()
