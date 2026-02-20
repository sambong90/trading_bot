# Cursor Actions for Trading Bot

This file contains example actions you can register in Cursor (or map to your own shortcuts).

Actions:
- Restart Bot: trading_bot/bin/restart_server.sh
- Collect Logs: trading_bot/bin/collect_logs.sh
- Capture Screenshot: trading_bot/bin/capture_screenshot.sh

How to register in Cursor:
1. Open Cursor and open folder: /Users/sambong.ai/.openclaw/workspace
2. Create a new Action and set the command to the script path (make sure it's executable)
   e.g. /Users/sambong.ai/.openclaw/workspace/trading_bot/bin/restart_server.sh
3. Bind a hotkey if desired.

Security: do NOT register actions that expose your .env or secrets. Keep TELEGRAM_BOT_TOKEN and UPBIT keys only in trading_bot/.env and never share.
