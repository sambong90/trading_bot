#!/usr/bin/env python3
"""
스케줄러 서비스 — trading bot 주기 실행 관리.

개선 사항:
  - 캔들 마감 동기화: cron HH:MM (CANDLE_SYNC_OFFSET_SEC 초 후) 실행
    -> 1h봉 종가 확정 후 분석 보장 (기본 HH:01:00)
  - PID 파일 락: auto_trader.pid 로 중복 실행 방지 + 스케줄러 재시작 후 stale 락 복구
  - bot_control.json 체크: /pause 명령 시 해당 사이클 건너뜀
  - heartbeat: 5분마다 logs/scheduler_heartbeat.json 기록
  - graceful shutdown: SIGTERM/SIGINT -> 진행 중 trading 종료 대기 후 정리
"""
import os
import sys
import pathlib
import logging
import json
import signal
import time
import threading
from datetime import datetime

# KST 타임존 강제 설정 (컨테이너 기본 UTC → 한국 표준시로 로그 시각 통일)
os.environ['TZ'] = 'Asia/Seoul'
time.tzset()

# workspace root
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'trading_bot' / '.env')
except Exception:
    pass

from apscheduler.schedulers.background import BackgroundScheduler
import subprocess

# ---------------------------------------------------------------------------
# 로거 설정
# ---------------------------------------------------------------------------
LOG_DIR = ROOT / 'trading_bot' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

SCHED_LOG_FILE = LOG_DIR / 'scheduler_out.log'
_sched_logger = logging.getLogger('scheduler')
_sched_logger.setLevel(logging.INFO)
if not _sched_logger.handlers:
    _fh = logging.FileHandler(SCHED_LOG_FILE, encoding='utf-8')
    _fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    _sched_logger.addHandler(_fh)
    _sched_logger.propagate = False


def _log(msg: str, level: str = 'info') -> None:
    getattr(_sched_logger, level)(msg)
    print(msg)


# ---------------------------------------------------------------------------
# 경로 / 파일 상수
# ---------------------------------------------------------------------------
PYTHON = str(ROOT / '.venv' / 'bin' / 'python')
AUTO_SUMMARY_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'auto_summary.py')]
DB_MAINTENANCE_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'db_maintenance.py')]
AUTO_TUNER_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'auto_tuner.py')]
AI_REVIEWER_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'ai_reviewer.py')]
TELEGRAM_BOT_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'telegram_bot.py')]
MARKET_BRIEFING_CMD = [PYTHON, str(ROOT / 'trading_bot' / 'tasks' / 'market_briefing.py')]
AUTO_TRADER_SCRIPT = str(ROOT / 'trading_bot' / 'tasks' / 'auto_trader.py')

PID_FILE = LOG_DIR / 'auto_trader.pid'
BOT_CONTROL_FILE = LOG_DIR / 'bot_control.json'
HEARTBEAT_FILE = LOG_DIR / 'scheduler_heartbeat.json'


# ---------------------------------------------------------------------------
# 서브프로세스 유틸
# ---------------------------------------------------------------------------

def _run_subprocess(cmd, name: str, timeout_seconds: int = 300, cwd=None, env=None) -> int:
    """서브프로세스 실행 + 종료 코드/타임아웃 로깅. 실패 시 텔레그램 알림."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd or str(ROOT),
            env=env or {**os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            _log(f'[{name}] 타임아웃 ({timeout_seconds}초) 초과로 강제 종료', 'error')
            _notify_scheduler(f'[{name}] 실행 타임아웃 ({timeout_seconds}초)')
            return proc.returncode
        if proc.returncode != 0:
            err_text = (stderr or b'').decode('utf-8', errors='ignore')[-500:]
            _log(f'[{name}] 비정상 종료 (exit={proc.returncode}): {err_text}', 'error')
            _notify_scheduler(f'[{name}] 비정상 종료 (exit={proc.returncode})')
        else:
            _log(f'[{name}] 정상 완료 (exit=0)')
        return proc.returncode
    except Exception as e:
        _log(f'[{name}] 실행 실패: {e}', 'error')
        _notify_scheduler(f'[{name}] 실행 실패: {e}')
        return -1


def _notify_scheduler(msg: str) -> None:
    """스케줄러 레벨 알림. 텔레그램 전송 실패해도 무시."""
    try:
        from trading_bot.monitor import send_telegram
        send_telegram(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PID 파일 기반 락 (중복 실행 방지 + 재시작 복구)
# ---------------------------------------------------------------------------

def _is_pid_alive(pid: int) -> bool:
    """프로세스가 살아있는지 확인 (kill -0 방식)."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _read_pid() -> int:
    """PID 파일에서 PID 읽기. 없거나 오류 시 -1 반환."""
    try:
        if PID_FILE.exists():
            return int(PID_FILE.read_text(encoding='utf-8').strip())
    except Exception:
        pass
    return -1


def _write_pid(pid: int) -> None:
    try:
        PID_FILE.write_text(str(pid), encoding='utf-8')
    except Exception as e:
        _log(f'[PID] PID 파일 쓰기 실패: {e}', 'warning')


def _clear_pid() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


def _recover_stale_pid() -> None:
    """스케줄러 기동 시: stale PID 파일 감지 및 정리."""
    pid = _read_pid()
    if pid <= 0:
        return
    if _is_pid_alive(pid):
        _log(f'[복구] auto_trader 프로세스(PID={pid}) 실행 중 감지 — 종료 대기 중...', 'warning')
        # 최대 30초 대기
        for _ in range(30):
            time.sleep(1)
            if not _is_pid_alive(pid):
                break
        if _is_pid_alive(pid):
            _log(f'[복구] PID={pid} SIGTERM 전송', 'warning')
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        _notify_scheduler(f'[복구] 이전 auto_trader(PID={pid}) 잔류 -> 재시작으로 정리됨')
    else:
        _log(f'[복구] stale PID 파일(PID={pid}) 감지 -> 정리함')
    _clear_pid()


# ---------------------------------------------------------------------------
# bot_control.json — pause/resume 체크
# ---------------------------------------------------------------------------

def _is_paused() -> bool:
    """bot_control.json의 paused 플래그 확인."""
    try:
        if BOT_CONTROL_FILE.exists():
            with open(BOT_CONTROL_FILE, 'r', encoding='utf-8') as f:
                ctrl = json.load(f)
            return bool(ctrl.get('paused', False))
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def _write_heartbeat() -> None:
    """스케줄러 생존 신호 기록 (5분마다)."""
    try:
        with open(HEARTBEAT_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'pid': os.getpid(),
            }, f, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 매매 사이클
# ---------------------------------------------------------------------------

_trading_proc = None  # 현재 실행 중인 auto_trader 서브프로세스


def run_trading_cycle() -> None:
    global _trading_proc

    # 1) pause 체크
    if _is_paused():
        _log('[스케줄러] 일시 정지 중 — 사이클 건너뜀')
        return

    # 2) PID 파일 락 — 이전 사이클이 아직 살아있으면 건너뜀
    pid = _read_pid()
    if pid > 0 and _is_pid_alive(pid):
        _log(f'[스케줄러] auto_trader.py 이전 사이클 아직 실행 중(PID={pid}) — 건너뜀')
        return

    # 3) stale PID — 이전 사이클 비정상 종료 감지
    if pid > 0 and not _is_pid_alive(pid):
        _log(f'[스케줄러] stale PID({pid}) 감지 — 이전 사이클 비정상 종료 추정', 'warning')
        _notify_scheduler(f'auto_trader 이전 사이클 비정상 종료 감지(PID={pid})')
        _clear_pid()

    # 4) Popen 메모리 내 이중 체크
    if _trading_proc is not None and _trading_proc.poll() is None:
        _log('[스케줄러] auto_trader.py Popen 프로세스 실행 중 — 건너뜀')
        return

    mode = os.environ.get('TRADING_MODE', 'paper')
    cmd = [PYTHON, AUTO_TRADER_SCRIPT, '--once', '--mode', mode]
    _log('')
    _log('=' * 60)
    _log('[스케줄러] auto_trader.py 실행 시작: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    _log(f'[스케줄러] 모드: {mode}')
    _log('=' * 60)

    try:
        _trading_proc = subprocess.Popen(cmd, cwd=str(ROOT), env={**os.environ})
        _write_pid(_trading_proc.pid)
        _log(f'[스케줄러] PID={_trading_proc.pid} 기록')

        # 비동기 감시 스레드: 완료 시 PID 파일 정리 + 비정상 종료 알림
        def _wait_and_clear(proc):
            try:
                proc.wait()
                rc = proc.returncode
                if rc is not None and rc != 0:
                    _log(f'[스케줄러] auto_trader 비정상 종료 (exit={rc})', 'warning')
                    _notify_scheduler(f'auto_trader 비정상 종료 (exit={rc})')
            except Exception:
                pass
            finally:
                _clear_pid()

        threading.Thread(target=_wait_and_clear, args=(_trading_proc,), daemon=True).start()

    except Exception as e:
        _log(f'auto_trader 실행 실패: {e}', 'warning')
        _notify_scheduler(f'auto_trader 실행 실패: {e}')
        _clear_pid()
        _trading_proc = None


# ---------------------------------------------------------------------------
# 서브태스크
# ---------------------------------------------------------------------------

def run_db_maintenance() -> None:
    """DB 하우스키핑(Pruning): 오래된 데이터 삭제. 매일 1회 실행."""
    _log('[스케줄러] db_maintenance 실행')
    _run_subprocess(DB_MAINTENANCE_CMD, 'db_maintenance', timeout_seconds=600)


def run_auto_tuner() -> None:
    """Walk-Forward 파라미터 튜닝: KRW-BTC/SOL 30일 1h 그리드 서치."""
    _log('[스케줄러] auto_tuner 실행')
    _run_subprocess(AUTO_TUNER_CMD, 'auto_tuner', timeout_seconds=1800)


def run_market_briefing() -> None:
    """Periodic Market Briefing: BTC 추세, 계좌·ROI, 24h P&L, ADX 상위 3 -> Telegram."""
    # 파드 재시작 시 동일 시간대 중복 발송 방지: DB에 발송 이력 원자적 기록
    from datetime import datetime as _dt
    period_key = f'briefing_{_dt.now().strftime("%Y-%m-%d %H:00")}'
    try:
        import psycopg2 as _pg
        _conn = _pg.connect(os.environ.get('DB_URL', ''))
        _cur = _conn.cursor()
        _cur.execute(
            "INSERT INTO system_state(key, value) VALUES(%s, %s) ON CONFLICT(key) DO NOTHING",
            (period_key, '1')
        )
        inserted = _cur.rowcount
        _conn.commit()
        _conn.close()
        if not inserted:
            _log(f'[스케줄러] market_briefing 중복 방지 — {period_key} 이미 발송됨')
            return
    except Exception as _e:
        _log(f'[스케줄러] market_briefing 중복 방지 DB 체크 실패 → 발송 건너뜀: {_e}')
        return
    _log('[스케줄러] market_briefing 실행')
    _run_subprocess(MARKET_BRIEFING_CMD, 'market_briefing', timeout_seconds=120)


def run_ai_reviewer() -> None:
    """AI Reviewer: Walk-Forward 파라미터 변경 분석 + 주간 성과 → Claude 브리핑 → Telegram."""
    _log('[스케줄러] ai_reviewer 실행')
    _run_subprocess(AI_REVIEWER_CMD, 'ai_reviewer', timeout_seconds=120)


# ---------------------------------------------------------------------------
# Telegram Bot 프로세스
# ---------------------------------------------------------------------------

_telegram_bot_proc = None


def start_telegram_bot() -> None:
    global _telegram_bot_proc
    if not os.environ.get('TELEGRAM_BOT_TOKEN') or not os.environ.get('TELEGRAM_CHAT_ID'):
        _log('Telegram 봇 비활성화 (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정)')
        return
    try:
        _telegram_bot_proc = subprocess.Popen(
            TELEGRAM_BOT_CMD,
            cwd=str(ROOT),
            env={**os.environ},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log(f'Telegram 봇 기동 (PID={_telegram_bot_proc.pid})')
    except Exception as e:
        _log('Telegram 봇 기동 실패: ' + str(e), 'warning')


# ---------------------------------------------------------------------------
# Graceful Shutdown
# ---------------------------------------------------------------------------

def _graceful_shutdown(signum=None, frame=None) -> None:
    global _trading_proc, _telegram_bot_proc
    _log('[스케줄러] Graceful shutdown 시작...')
    try:
        sched.shutdown(wait=False)
    except Exception:
        pass

    # trading 프로세스 종료 대기 (최대 15초)
    if _trading_proc is not None and _trading_proc.poll() is None:
        _log('[스케줄러] 진행 중인 trading 사이클 종료 대기 (15초)...')
        try:
            _trading_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _log('[스케줄러] trading 프로세스 강제 종료', 'warning')
            _trading_proc.kill()
    _clear_pid()

    if _telegram_bot_proc is not None and _telegram_bot_proc.poll() is None:
        _telegram_bot_proc.terminate()

    _notify_scheduler('스케줄러가 안전하게 종료되었습니다.')
    _log('[스케줄러] Graceful shutdown 완료')


# ---------------------------------------------------------------------------
# 스케줄러 설정
# ---------------------------------------------------------------------------

sched = BackgroundScheduler(timezone='Asia/Seoul')

# 캔들 마감 동기화: CANDLE_SYNC_OFFSET_SEC 기반 cron 시간 계산
# 기본 60초 -> 매시 01분 00초 실행 (1h봉 마감 후 60초 대기)
_offset_sec = int(os.environ.get('CANDLE_SYNC_OFFSET_SEC', '60'))
_cron_minute = _offset_sec // 60
_cron_second = _offset_sec % 60

if os.environ.get('ENABLE_AUTO_TRADING', '0') == '1':
    sched.add_job(
        run_trading_cycle,
        'cron',
        minute=f'{_cron_minute}-59/1',
        second=_cron_second,
        id='auto_trader',
        max_instances=1,
        misfire_grace_time=60,  # 스케줄 지연 시 1분 이내면 재실행 허용
    )
    _log(f'자동 매매 활성화 (실시간 스탑로스 모니터: 매시 {_cron_minute:02d}분부터 1분 간격)')
    _log(f'   -> 1h봉 마감 {_offset_sec}초 후 시작, 이후 1분마다 반복 (CANDLE_SYNC_OFFSET_SEC={_offset_sec})')
else:
    _log('자동 매매 비활성화 (ENABLE_AUTO_TRADING=1로 설정하여 활성화)')

# DB 하우스키핑: 매일 새벽 3시 (용량·조회 속도 유지)
sched.add_job(run_db_maintenance, 'cron', hour=3, minute=0, id='db_maintenance')
_log('DB 하우스키핑 스케줄 등록 (매일 03:00)')

# Walk-Forward 튜닝: 매주 일요일 04:00
sched.add_job(run_auto_tuner, 'cron', hour=4, minute=0, day_of_week='sun', id='auto_tuner')
_log('Walk-Forward 튜너 스케줄 등록 (매주 일요일 04:00)')

# AI Reviewer: auto_tuner 완료 후 순차 실행 (auto_tuner.py 내부에서 직접 호출)
# → 레이스 컨디션 방지를 위해 독립 cron 제거

# Market Briefing: 09:00 (업비트 일일 리셋) + 4시간마다 (00, 04, 08, 12, 16, 20)
sched.add_job(
    run_market_briefing, 'cron',
    hour='0,4,8,9,12,16,20', minute=0,
    id='market_briefing',
)
_log('Market Briefing 스케줄 등록 (09:00 + 4시간마다)')

# Heartbeat: 5분마다 (외부 모니터링용)
sched.add_job(_write_heartbeat, 'interval', minutes=5, id='heartbeat')
_log('Heartbeat 스케줄 등록 (5분마다)')

# NOTE: auto_summary job disabled per user request to stop periodic fetch-complete Telegram messages.
# sched.add_job(run_summary, 'interval', minutes=5, id='auto_summary')


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    _log('=' * 60)
    _log('스케줄러 기동: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    _log(f'PID: {os.getpid()}')
    _log('=' * 60)

    # 재시작 복구: stale PID 정리
    _recover_stale_pid()

    start_telegram_bot()
    sched.start()
    _write_heartbeat()

    _notify_scheduler(
        f'스케줄러 기동\n'
        f'시각: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'매매: {"활성" if os.environ.get("ENABLE_AUTO_TRADING","0")=="1" else "비활성"}\n'
        f'캔들 오프셋: {_offset_sec}초 후'
    )

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        _graceful_shutdown()
