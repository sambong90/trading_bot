#!/usr/bin/env python3
import os
import sys
import pathlib
# ensure workspace root is on path
ROOT = pathlib.Path(__file__).resolve().parents[2]

from apscheduler.schedulers.background import BackgroundScheduler
import time
import subprocess

PYTHON = str(ROOT / '.venv' / 'bin' / 'python')
AUTO_SUMMARY_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'auto_summary.py')]

# 환경 변수에서 모드 확인
TRADING_MODE = os.environ.get('TRADING_MODE', 'paper')
AUTO_TRADER_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'auto_trader.py'), '--once', '--mode', TRADING_MODE]

sched = BackgroundScheduler()

# Run auto_summary.py as a separate process each interval to ensure latest code is used
def run_summary():
    try:
        subprocess.Popen(AUTO_SUMMARY_CMD)
    except Exception as e:
        print('Failed to start auto_summary subprocess:', e)

# Run auto_trader.py for real-time trading cycles
def run_trading_cycle():
    try:
        subprocess.Popen(AUTO_TRADER_CMD)
    except Exception as e:
        print('Failed to start auto_trader subprocess:', e)

# 상태 요약: 5분마다
# NOTE: auto_summary job disabled per user request to stop periodic 'fetch complete' Telegram messages.
# sched.add_job(run_summary, 'interval', minutes=5, id='auto_summary')

# 실시간 매매 사이클: 5분마다 (환경 변수로 제어 가능)
trading_interval = int(os.environ.get('TRADING_INTERVAL_MINUTES', '5'))
if os.environ.get('ENABLE_AUTO_TRADING', '0') == '1':
    sched.add_job(run_trading_cycle, 'interval', minutes=trading_interval, id='auto_trader')
    print(f'✅ 자동 매매 활성화 (간격: {trading_interval}분)')
else:
    print('ℹ️ 자동 매매 비활성화 (ENABLE_AUTO_TRADING=1로 설정하여 활성화)')

if __name__ == '__main__':
    sched.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
