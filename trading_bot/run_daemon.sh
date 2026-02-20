#!/usr/bin/env bash
# Wrapper to run trading_bot scheduler as a persistent daemon
# Usage: ./run_daemon.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
# load env
if [ -f "$DIR/.env" ]; then
  export $(grep -v '^#' "$DIR/.env" | xargs)
fi
# activate virtualenv if present
if [ -f "$DIR/../.venv/bin/activate" ]; then
  source "$DIR/../.venv/bin/activate"
fi
LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
exec "$DIR/../.venv/bin/python" "$DIR/tasks/scheduler_service.py" >> "$LOG_DIR/daemon.log" 2>&1
