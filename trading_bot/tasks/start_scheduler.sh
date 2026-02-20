#!/usr/bin/env bash
cd "$(dirname "$0")/.."
# Activate workspace venv
VENV_PATH="/Users/sambong.ai/.openclaw/workspace/.venv"
if [ -f "$VENV_PATH/bin/activate" ]; then
  . "$VENV_PATH/bin/activate"
fi
mkdir -p trading_bot/logs
nohup python trading_bot/tasks/scheduler_service.py > trading_bot/logs/scheduler_out.log 2>&1 &
echo $! > trading_bot/logs/scheduler.pid
echo "Scheduler started, pid=$(cat trading_bot/logs/scheduler.pid)"
