#!/usr/bin/env python3
import os
import sys
import pathlib
# ensure workspace root is on path
ROOT = pathlib.Path(__file__).resolve().parents[2]
# 라이브 모드 등 env를 위해 .env 로드 (있으면 적용)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'trading_bot' / '.env')
except Exception:
    pass

from apscheduler.schedulers.background import BackgroundScheduler
import time
import subprocess

PYTHON = str(ROOT / '.venv' / 'bin' / 'python')
AUTO_SUMMARY_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'auto_summary.py')]
DB_MAINTENANCE_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'db_maintenance.py')]
AUTO_TUNER_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'auto_tuner.py')]
TELEGRAM_BOT_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'telegram_bot.py')]
MARKET_BRIEFING_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'market_briefing.py')]

# 모드는 run_trading_cycle() 호출 시점에 매번 읽음 (env 변경/ .env 반영)
AUTO_TRADER_SCRIPT = str(ROOT / 'trading_bot' / 'tasks' / 'auto_trader.py')

sched = BackgroundScheduler()

# Telegram Chatbot: .env의 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 사용
_telegram_bot_proc = None
def start_telegram_bot():
    global _telegram_bot_proc
    if not os.environ.get('TELEGRAM_BOT_TOKEN') or not os.environ.get('TELEGRAM_CHAT_ID'):
        print('ℹ️ Telegram 봇 비활성화 (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정)')
        return
    try:
        _telegram_bot_proc = subprocess.Popen(
            TELEGRAM_BOT_CMD,
            cwd=str(ROOT),
            env={**os.environ},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print('✅ Telegram 봇 기동 (채팅 명령 수신)')
    except Exception as e:
        print('Telegram 봇 기동 실패:', e)

# Run auto_summary.py as a separate process each interval to ensure latest code is used
def run_summary():
    try:
        subprocess.Popen(AUTO_SUMMARY_CMD)
    except Exception as e:
        print('Failed to start auto_summary subprocess:', e)

# Run auto_trader.py for real-time trading cycles (한 번에 하나만 실행되도록 락)
_trading_lock = None

def run_trading_cycle():
    global _trading_lock
    try:
        if _trading_lock is not None and _trading_lock.poll() is None:
            return
        # 호출 시점 env에서 모드 읽기 → .env 변경/라이브 전환 시 반영
        mode = os.environ.get('TRADING_MODE', 'paper')
        cmd = [PYTHON, AUTO_TRADER_SCRIPT, '--once', '--mode', mode]
        _trading_lock = subprocess.Popen(cmd)
    except Exception as e:
        print('Failed to start auto_trader subprocess:', e)
        _trading_lock = None

# 상태 요약: 5분마다
# NOTE: auto_summary job disabled per user request to stop periodic 'fetch complete' Telegram messages.
# sched.add_job(run_summary, 'interval', minutes=5, id='auto_summary')

# 실시간 매매 사이클: 5분마다 (환경 변수로 제어 가능)
trading_interval = int(os.environ.get('TRADING_INTERVAL_MINUTES', '5'))
if os.environ.get('ENABLE_AUTO_TRADING', '0') == '1':
    sched.add_job(run_trading_cycle, 'interval', minutes=trading_interval, id='auto_trader', max_instances=1)
    print(f'✅ 자동 매매 활성화 (간격: {trading_interval}분)')
else:
    print('ℹ️ 자동 매매 비활성화 (ENABLE_AUTO_TRADING=1로 설정하여 활성화)')


def run_db_maintenance():
    """DB 하우스키핑(Pruning): 오래된 데이터 삭제. 매일 1회 실행."""
    try:
        subprocess.Popen(DB_MAINTENANCE_CMD)
    except Exception as e:
        print('Failed to start db_maintenance subprocess:', e)


def run_auto_tuner():
    """Walk-Forward 파라미터 튜닝 (V4.0): KRW-BTC/SOL 30일 1h 그리드 서치 후 최적 조합 TuningRun 저장."""
    try:
        subprocess.Popen(AUTO_TUNER_CMD)
    except Exception as e:
        print('Failed to start auto_tuner subprocess:', e)


def run_market_briefing():
    """Periodic Market Briefing: BTC 추세, 계좌·ROI, 24h P&L, ADX 상위 3 → Telegram 전송."""
    try:
        subprocess.Popen(MARKET_BRIEFING_CMD, cwd=str(ROOT), env={**os.environ})
    except Exception as e:
        print('Failed to start market_briefing subprocess:', e)


# DB 하우스키핑: 매일 새벽 3시 (용량·조회 속도 유지)
sched.add_job(run_db_maintenance, 'cron', hour=3, minute=0, id='db_maintenance')
print('✅ DB 하우스키핑 스케줄 등록 (매일 03:00)')

# Walk-Forward 튜닝: 매주 일요일 04:00
sched.add_job(run_auto_tuner, 'cron', hour=4, minute=0, day_of_week='sun', id='auto_tuner')
print('✅ Walk-Forward 튜너 스케줄 등록 (매주 일요일 04:00)')

# Market Briefing: 09:00 (업비트 일일 리셋) + 4시간마다 (00, 04, 08, 12, 16, 20)
sched.add_job(run_market_briefing, 'cron', hour='0,4,8,9,12,16,20', minute=0, id='market_briefing')
print('✅ Market Briefing 스케줄 등록 (09:00 + 4시간마다)')

if __name__ == '__main__':
    start_telegram_bot()
    sched.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()

