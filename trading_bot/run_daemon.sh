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
# Redirect scheduler stdout/stderr into both scheduler_out.log and daemon.log (tee)
PY="$DIR/../.venv/bin/python"
SCRIPT="$DIR/tasks/scheduler_service.py"
# Use tee to write to both logs
exec "$PY" "$SCRIPT" 2>&1 | tee -a "$LOG_DIR/scheduler_out.log" | tee -a "$LOG_DIR/daemon.log"
