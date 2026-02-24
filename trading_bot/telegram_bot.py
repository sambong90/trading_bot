
#!/usr/bin/env python3
"""
Interactive Telegram Chatbot for monitoring and controlling the trading bot.
Uses long polling (requests); compatible with one-way send_telegram() from monitor.py.
Loads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env. Only responds to the configured chat_id.
"""
import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timedelta

# workspace root on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except Exception:
    pass

import requests

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BASE_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None
LOG_DIR = ROOT / 'logs'
PROGRESS_FILE = LOG_DIR / 'progress.json'
ENV_PATH = ROOT / '.env'


def _send(text: str, chat_id: str = None) -> bool:
    chat_id = chat_id or CHAT_ID
    if not TOKEN or not chat_id:
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def cmd_start() -> str:
    return (
        "🤖 Trading Bot Chatbot\n\n"
        "명령어:\n"
        "/start — 이 도움말\n"
        "/status — 현재 단계·진행률\n"
        "/balance — KRW·보유 종목·ROI\n"
        "/panic — 자동 라이브 매매 즉시 중지 (ENABLE_AUTO_LIVE=0)\n"
        "/report — 오늘 체결·실현 P&L"
    )


def cmd_status() -> str:
    if not PROGRESS_FILE.exists():
        return "진행 정보 없음 (progress.json 없음)"
    try:
        with open(PROGRESS_FILE) as f:
            data = json.load(f)
        phase = data.get("phase", "idle")
        task = data.get("task") or "-"
        percent = data.get("percent", 0)
        msg = data.get("msg") or ""
        return f"📊 상태\n단계: {phase}\n작업: {task}\n진행률: {percent}%\n{msg}".strip()
    except Exception as e:
        return f"상태 조회 실패: {e}"


def cmd_balance() -> str:
    mode = os.environ.get("TRADING_MODE", "paper")
    try:
        from trading_bot.executor import PaperExecutor, LiveExecutor
        account_value = float(os.environ.get("ACCOUNT_VALUE", "100000"))
        if mode == "live":
            ex = LiveExecutor()
            if not getattr(ex, "enabled", False):
                return "Live 비활성화. TRADING_MODE=paper 또는 Live 설정 확인."
            ex.refresh_balance_cache()
        else:
            ex = PaperExecutor(initial_cash=account_value)
            # Paper는 positions만 있음; DB/캐시 없음. 포지션은 auto_trader 세션 기준이 아니라 로컬이라 여기선 0일 수 있음.
        krw = ex.get_available_cash()
        lines = [f"💵 KRW: {krw:,.0f}원", f"(모드: {mode})"]
        # Holdings: Paper는 positions, Live는 _balance_cache
        if mode == "paper":
            tickers = list(getattr(ex, "positions", {}).keys())
        else:
            cache = getattr(ex, "_balance_cache", {}) or {}
            tickers = [f"KRW-{c}" for c in cache if c != "KRW" and (cache.get(c) or 0) > 0]
        if tickers:
            try:
                import pyupbit
                for t in tickers[:20]:
                    qty = ex.get_position_qty(t)
                    if qty <= 0:
                        continue
                    avg = ex.get_avg_buy_price(t)
                    cur = pyupbit.get_current_price(t)
                    cur = float(cur) if cur is not None else 0.0
                    if avg and avg > 0 and cur > 0:
                        roi = (cur - avg) / avg * 100
                        val = qty * cur
                        lines.append(f"• {t}: {qty:.6f} @ {cur:,.0f} (ROI {roi:+.1f}%, 약 {val:,.0f}원)")
                    else:
                        lines.append(f"• {t}: {qty:.6f}")
            except Exception as e:
                lines.append(f"(보유 조회 오류: {e})")
        else:
            lines.append("보유 종목 없음")
        return "\n".join(lines)
    except Exception as e:
        return f"잔고 조회 실패: {e}"


def cmd_panic() -> str:
    """Same logic as main.py /panic: set ENABLE_AUTO_LIVE=0 in .env."""
    if not ENV_PATH.exists():
        return ".env 없음 — 패닉 적용 불가"
    try:
        with open(ENV_PATH) as f:
            lines = f.readlines()
        new_lines = []
        found = False
        for L in lines:
            if L.strip().startswith("ENABLE_AUTO_LIVE="):
                new_lines.append("ENABLE_AUTO_LIVE=0\n")
                found = True
            else:
                new_lines.append(L)
        if not found:
            new_lines.append("ENABLE_AUTO_LIVE=0\n")
        with open(ENV_PATH, "w") as f:
            f.writelines(new_lines)
        return "🛑 ENABLE_AUTO_LIVE=0 적용됨. 자동 라이브 매매가 중지됩니다."
    except Exception as e:
        return f"패닉 적용 실패: {e}"


def cmd_report() -> str:
    """Today's orders from Order table: count and realized P&L (sell - buy)."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Order
        session = get_session()
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = session.query(Order).filter(Order.ts >= today_start).all()
        session.close()
        buys = [r for r in rows if (r.side or "").lower() == "buy"]
        sells = [r for r in rows if (r.side or "").lower() == "sell"]
        buy_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in buys)
        sell_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in sells)
        pnl = sell_sum - buy_sum
        return (
            f"📋 오늘 체결\n"
            f"매수: {len(buys)}건, {buy_sum:,.0f}원\n"
            f"매도: {len(sells)}건, {sell_sum:,.0f}원\n"
            f"실현 P&L: {pnl:+,.0f}원"
        )
    except Exception as e:
        return f"리포트 조회 실패: {e}"


def _btc_global_trend() -> str:
    """Bull or Bear from KRW-BTC EMA trend (same logic as auto_trader.check_btc_global_trend)."""
    try:
        from trading_bot.tasks.auto_trader import check_btc_global_trend
        is_bull = check_btc_global_trend(interval="day", count=50)
        return "🟢 Bull" if is_bull else "🔴 Bear"
    except Exception:
        return "—"


def _account_value_and_roi() -> tuple:
    """Returns (total_value_krw, roi_pct) or (None, None) on error."""
    try:
        from trading_bot.executor import PaperExecutor, LiveExecutor
        account_value = float(os.environ.get("ACCOUNT_VALUE", "100000"))
        mode = os.environ.get("TRADING_MODE", "paper")
        if mode == "live":
            ex = LiveExecutor()
            if not getattr(ex, "enabled", False):
                return None, None
            ex.refresh_balance_cache()
        else:
            ex = PaperExecutor(initial_cash=account_value)
        krw = ex.get_available_cash()
        if mode == "paper":
            tickers = list(getattr(ex, "positions", {}).keys())
        else:
            cache = getattr(ex, "_balance_cache", {}) or {}
            tickers = [f"KRW-{c}" for c in cache if c != "KRW" and (cache.get(c) or 0) > 0]
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
        if cost_basis <= 0:
            roi = 0.0
        else:
            roi = (total_value - cost_basis) / cost_basis * 100
        return total_value, roi
    except Exception:
        return None, None


def _pnl_last_24h() -> tuple:
    """Returns (buy_sum, sell_sum, pnl, trade_count) from Trade table for last 24h."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Trade
        session = get_session()
        cutoff = datetime.utcnow() - timedelta(hours=24)
        rows = session.query(Trade).filter(Trade.ts >= cutoff).all()
        session.close()
        buys = [r for r in rows if (r.side or "").lower() == "buy"]
        sells = [r for r in rows if (r.side or "").lower() == "sell"]
        buy_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in buys)
        sell_sum = sum(float(r.price or 0) * float(r.qty or 0) for r in sells)
        return buy_sum, sell_sum, sell_sum - buy_sum, len(rows)
    except Exception:
        return 0.0, 0.0, 0.0, 0


def _top3_adx_tickers() -> list:
    """Top 3 tickers by latest ADX (minute60). Returns list of (ticker, adx)."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import TechnicalIndicator
        session = get_session()
        rows = (
            session.query(TechnicalIndicator)
            .filter(TechnicalIndicator.timeframe == "minute60")
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
            adx = ind.get("adx")
            if adx is not None:
                try:
                    seen[r.ticker] = float(adx)
                except (TypeError, ValueError):
                    pass
        sorted_tickers = sorted(seen.items(), key=lambda x: -x[1])[:3]
        return sorted_tickers
    except Exception:
        return []


def send_briefing(chat_id: str = None) -> bool:
    """
    Build and send a periodic market briefing to Telegram.
    - BTC global trend (Bull/Bear)
    - Total account value and portfolio ROI
    - Last 24h P&L from Trade table
    - Top 3 tickers by ADX (high-priority monitoring)
    Sends via Bot API (same as _send); no context.bot needed.
    """
    lines = ["📰 Market Briefing", ""]
    # 1) BTC trend
    trend = _btc_global_trend()
    lines.append(f"• BTC 추세: {trend}")
    # 2) Account value & ROI
    total_val, roi = _account_value_and_roi()
    if total_val is not None:
        lines.append(f"• 계좌 총액: {total_val:,.0f}원 (ROI {roi:+.1f}%)")
    else:
        lines.append("• 계좌: 조회 불가 (Live 비활성 등)")
    # 3) Last 24h P&L (Trade table)
    buy_24, sell_24, pnl_24, n_24 = _pnl_last_24h()
    lines.append(f"• 최근 24h 체결: {n_24}건 | 매수 {buy_24:,.0f} / 매도 {sell_24:,.0f} | P&L {pnl_24:+,.0f}원")
    # 4) Top 3 ADX
    top3 = _top3_adx_tickers()
    if top3:
        lines.append("• ADX 상위 3 (모니터링): " + ", ".join(f"{t}({a:.0f})" for t, a in top3))
    else:
        lines.append("• ADX 상위: 데이터 없음")
    msg = "\n".join(lines)
    return _send(msg, chat_id=chat_id or CHAT_ID)


def handle_message(text: str) -> str:
    if not text or not text.strip():
        return ""
    text = text.strip()
    if text == "/start":
        return cmd_start()
    if text == "/status":
        return cmd_status()
    if text == "/balance":
        return cmd_balance()
    if text == "/panic":
        return cmd_panic()
    if text == "/report":
        return cmd_report()
    if text.startswith("/"):
        return "알 수 없는 명령. /start 로 도움말 확인."
    return ""


def poll_once(offset: int) -> tuple:
    """Returns (next_offset, list of (chat_id, text))."""
    if not TOKEN:
        return offset, []
    try:
        r = requests.get(
            f"{BASE_URL}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        if r.status_code != 200:
            return offset, []
        data = r.json()
        if not data.get("ok"):
            return offset, []
        updates = data.get("result") or []
        out = []
        next_offset = offset
        for u in updates:
            next_offset = u.get("update_id", 0) + 1
            msg = u.get("message") or {}
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if chat_id and text:
                out.append((chat_id, text))
        return next_offset, out
    except Exception:
        return offset, []


def main():
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 없습니다. .env를 확인하세요.")
        sys.exit(1)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    offset = 0
    print("Telegram bot polling (chat_id=%s). Ctrl+C to stop." % CHAT_ID)
    while True:
        try:
            offset, updates = poll_once(offset)
            for chat_id, text in updates:
                if CHAT_ID and chat_id != CHAT_ID:
                    continue
                reply = handle_message(text)
                if reply:
                    _send(reply, chat_id=chat_id)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Poll error:", e)
            time.sleep(5)
    print("Telegram bot stopped.")


if __name__ == "__main__":
    main()

