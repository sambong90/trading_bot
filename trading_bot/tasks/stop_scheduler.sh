#!/usr/bin/env bash
cd "$(dirname "$0")/.."
if [ -f trading_bot/logs/scheduler.pid ]; then
  pid=$(cat trading_bot/logs/scheduler.pid)
  kill $pid || true
  rm trading_bot/logs/scheduler.pid || true
  echo "Scheduler stopped"
else
  echo "No scheduler.pid found"
fi
