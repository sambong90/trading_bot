#!/usr/bin/env python3
"""
Periodic Market Briefing: sends a summarized report to Telegram (BTC trend, account, 24h P&L, top ADX).
Invoked by the scheduler at 09:00 daily and every 4 hours (00:00, 04:00, 08:00, 12:00, 16:00, 20:00).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'trading_bot' / '.env')
except Exception:
    pass


def main():
    import time
    from trading_bot.telegram_bot import send_briefing
    ok = send_briefing()
    if not ok:
        # 1회 재시도 (Telegram API 일시 오류 대응)
        time.sleep(5)
        ok = send_briefing()
    if not ok:
        print("Market briefing send failed (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)", file=sys.stderr)
        sys.exit(1)
    print("Market briefing sent.")


if __name__ == "__main__":
    main()

