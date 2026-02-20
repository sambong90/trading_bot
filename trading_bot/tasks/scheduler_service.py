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
JOB_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'auto_summary.py')]

sched = BackgroundScheduler()
# Run auto_summary.py as a separate process each interval to ensure latest code is used
def run_job():
    try:
        subprocess.Popen(JOB_CMD)
    except Exception as e:
        print('Failed to start auto_summary subprocess:', e)

sched.add_job(run_job, 'interval', minutes=5, id='auto_summary')

if __name__ == '__main__':
    sched.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
