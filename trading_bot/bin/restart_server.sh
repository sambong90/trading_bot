#!/usr/bin/env bash
# Restart Flask server (main.py) and tail logs
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
# activate venv if exists
if [ -f "$DIR/../.venv/bin/activate" ]; then
  source "$DIR/../.venv/bin/activate"
fi
echo "Restarting trading_bot Flask server..."
# find and kill existing main.py
pkill -f 'trading_bot/main.py' || true
sleep 0.3
# start server in background
nohup ./.venv/bin/python trading_bot/main.py &>/tmp/trading_bot_flask.log &
sleep 0.5
echo "Server started. PID(s):"
ps aux | grep trading_bot/main.py | grep -v grep
echo "Tailing last 200 lines of daemon log:"
tail -n 200 trading_bot/logs/daemon.log
