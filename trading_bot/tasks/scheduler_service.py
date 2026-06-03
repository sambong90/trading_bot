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
from datetime import datetime, timedelta

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
    from logging.handlers import RotatingFileHandler as _RFHS
    _fh = _RFHS(SCHED_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=2, encoding='utf-8')
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

# ---------------------------------------------------------------------------
# 데이터 수집 Job (collectors 패키지)
# ---------------------------------------------------------------------------

_macro_consec_failures = 0   # None 반환 연속 카운터 (모듈 재시작 시 초기화)
_dom_consec_failures   = 0   # None 반환 연속 카운터


def collect_macro(is_retry: bool = False) -> None:
    """거시 지표 수집 (Yahoo Finance: DXY, NDX, Gold, Bond, JPY, Oil).

    실행 주기: 미장(22~06시) 매시 + 07:00 / 13:00 / 19:00 KST — 총 12회/일.
    MacroSnapshot → DB 저장. ratio_quality='stale'이면 EM-7 트리거 차단.
    None 반환(데이터 없음) 시 텔레그램 알림 + 30분 후 1회 자동 재시도.
    """
    global _macro_consec_failures
    label = ' (재시도)' if is_retry else ''

    # 미장 공휴일: 07:00/19:00 KST 외 수집 생략 (동일값 반복 저장 방지)
    try:
        from zoneinfo import ZoneInfo as _ZI
        from trading_bot.market_calendar import is_us_holiday as _is_holiday
        _now_kst = datetime.now(_ZI('Asia/Seoul'))
        if _is_holiday(_now_kst) and _now_kst.hour not in (7, 19):
            _log('[수집] 미장 공휴일 — collect_macro 생략 (허용: 07:00/19:00 KST)')
            return
    except Exception:
        pass

    _log(f'[수집] collect_macro 실행{label}')
    try:
        from trading_bot.collectors import macro as _macro
        result = _macro.collect()
        if result:
            _macro_consec_failures = 0
            _log(
                f'[수집] MacroSnapshot 완료 — '
                f'ratio={result.get("nasdaq_dxy_ratio","?"):.1f} '
                f'zone={result.get("nasdaq_dxy_zone","?")} '
                f'quality={result.get("ratio_quality","?")}'
            )
        else:
            _macro_consec_failures += 1
            _log(f'[수집] collect_macro 실패 (None 반환, 누적 {_macro_consec_failures}회)', 'warning')
            _notify_scheduler(f'⚠️ [Macro] 수집 실패: DXY/NDX 데이터 없음 (누적 {_macro_consec_failures}회)')
            if not is_retry:
                _schedule_macro_retry()
    except Exception as e:
        _macro_consec_failures += 1
        _log(f'[수집] collect_macro 예외: {e}', 'error')
        _notify_scheduler(f'[collect_macro] 오류: {e}')
        if not is_retry:
            _schedule_macro_retry()


def _schedule_macro_retry() -> None:
    """collect_macro 30분 후 1회 재시도 예약."""
    try:
        from datetime import datetime as _dt, timedelta as _td
        retry_time = _dt.now() + _td(minutes=30)
        sched.add_job(
            lambda: collect_macro(is_retry=True),
            'date',
            run_date=retry_time,
            id='collect_macro_retry',
            replace_existing=True,
        )
        _log('[수집] collect_macro 재시도 예약 (30분 후)')
    except Exception as e:
        _log(f'[수집] collect_macro 재시도 예약 실패: {e}', 'warning')


def collect_dominance() -> None:
    """BTC/ETH 도미넌스 수집 (CoinGecko /global).

    실행 주기: 매시 HH:02 — 24회/일 (Guardian이 매 사이클 도미넌스를 읽으므로 1h 간격).
    DominanceSnapshot → DB 저장. event_signal로 임계값 크로스 감지.
    3회 연속 실패 시 텔레그램 알림.
    """
    global _dom_consec_failures
    _log('[수집] collect_dominance 실행')
    try:
        from trading_bot.collectors import dominance as _dom
        result = _dom.collect()
        if result:
            _dom_consec_failures = 0
            _log(
                f'[수집] DominanceSnapshot 완료 — '
                f'BTC.D={result.get("btc_dominance","?"):.2f}% '
                f'stage={result.get("bull_stage","?")} '
                f'event={result.get("event_signal") or "none"}'
            )
        else:
            _dom_consec_failures += 1
            _log(f'[수집] collect_dominance 실패 (None 반환, 누적 {_dom_consec_failures}회)', 'warning')
            if _dom_consec_failures >= 3:
                _notify_scheduler(f'⚠️ [Dominance] 수집 실패 {_dom_consec_failures}회 연속')
    except Exception as e:
        _dom_consec_failures += 1
        _log(f'[수집] collect_dominance 예외: {e}', 'error')
        _notify_scheduler(f'[collect_dominance] 오류: {e}')


def collect_kimp() -> None:
    """김치프리미엄 수집 (Upbit + Binance REST).

    실행 주기: 4회/일 (00:00, 06:00, 12:00, 18:00 KST) — Guardian 미소비, 표시 전용.
    KimpSnapshot → DB 저장. kimp_signal로 역프/바닥 신호 감지.
    """
    _log('[수집] collect_kimp 실행')
    try:
        from trading_bot.collectors import kimp as _kimp
        result = _kimp.collect()
        if result:
            _log(
                f'[수집] KimpSnapshot 완료 — '
                f'kimp={result.get("kimp_pct","?"):.2f}% '
                f'signal={result.get("kimp_signal","?")}'
            )
        else:
            _log('[수집] collect_kimp 실패 (None 반환)', 'warning')
    except Exception as e:
        _log(f'[수집] collect_kimp 예외: {e}', 'error')


def collect_fng() -> None:
    """Fear & Greed Index 수집 (alternative.me).

    실행 주기: 4회/일 (00:30, 06:30, 12:30, 18:30 KST).
    SentimentSnapshot → DB 저장. sentiment.py가 DB 우선 조회로 캐시 활용.
    """
    _log('[수집] collect_fng 실행')
    try:
        from trading_bot.collectors import sentiment as _senti
        result = _senti.collect()
        if result:
            _log(
                f'[수집] SentimentSnapshot 완료 — '
                f'FNG={result.get("value","?"):.0f} '
                f'label={result.get("label","?")}'
            )
        else:
            _log('[수집] collect_fng 실패 (None 반환)', 'warning')
            _notify_scheduler('⚠️ [FNG] 수집 실패 — live fallback으로 전환')
    except Exception as e:
        _log(f'[수집] collect_fng 예외: {e}', 'error')
        _notify_scheduler(f'[collect_fng] 오류: {e}')


def collect_btc_weekly() -> None:
    """BTC 주봉 200MA 수집 (pyupbit).

    실행 주기: 매일 08:05 KST 1회.
    BtcWeeklySnapshot → DB 저장. aggregator가 DB 우선 조회로 매 사이클 API 호출 제거.
    """
    _log('[수집] collect_btc_weekly 실행')
    try:
        from trading_bot.collectors import btc_weekly as _btc_w
        result = _btc_w.collect()
        if result:
            _log(
                f'[수집] BtcWeeklySnapshot 완료 — '
                f'MA200={result.get("ma200","?"):.0f} '
                f'price={result.get("current_price","?"):.0f} '
                f'above={result.get("above_ma200","?")}'
            )
        else:
            _log('[수집] collect_btc_weekly 실패 (None 반환)', 'warning')
            _notify_scheduler('⚠️ [BTC_W200MA] 수집 실패 — live fallback으로 전환')
    except Exception as e:
        _log(f'[수집] collect_btc_weekly 예외: {e}', 'error')
        _notify_scheduler(f'[collect_btc_weekly] 오류: {e}')


def collect_4h_ohlcv() -> None:
    """4h(minute240) OHLCV를 모니터링 전 종목에 대해 수집·DB 적재.

    실행 주기: 4h봉 마감 직후(KST 01/05/09/13/17/21 + 5분) — 6회/일.
    fetch_ohlcv(use_db_first=True)가 stale(>2h) 시에만 API 호출 후 DB 저장하므로
    호출당 API 부하는 1h 수집의 1/4 수준. load_4h_ema_state(EMA12/26, >=31봉)가
    전 종목에서 작동하도록 데이터만 채운다(진입/청산·confluence 로직 불변).
    31봉 미달 신규 종목은 load_4h_ema_state가 None 반환 → confluence 자동 스킵 유지.
    """
    _log('[수집] collect_4h_ohlcv 실행')
    try:
        from trading_bot.data import get_all_krw_tickers, get_all_krw_tickers_full, fetch_ohlcv
        from trading_bot.config import FOURH_OHLCV_COUNT, OHLCV_FULL_UNIVERSE
        tickers = (get_all_krw_tickers_full(use_db_fallback=True)
                   if OHLCV_FULL_UNIVERSE else get_all_krw_tickers(use_db_fallback=True))
        ok = 0
        fail = 0
        for _t in tickers:
            try:
                df = fetch_ohlcv(ticker=_t, interval='minute240',
                                 count=FOURH_OHLCV_COUNT, use_db_first=True)
                if df is not None and len(df) > 0:
                    ok += 1
                else:
                    fail += 1
            except Exception as _e:
                fail += 1
                _log(f'[수집] collect_4h_ohlcv {_t} 실패: {_e}', 'warning')
            time.sleep(0.1)  # Upbit rate-limit 여유
        _log(f'[수집] collect_4h_ohlcv 완료 — 성공 {ok} / 실패 {fail} / 대상 {len(tickers)}')
        if tickers and ok == 0:
            _notify_scheduler('⚠️ [4h OHLCV] 전 종목 수집 실패 — load_4h_ema_state 비활성 우려')
    except Exception as e:
        _log(f'[수집] collect_4h_ohlcv 예외: {e}', 'error')
        _notify_scheduler(f'[collect_4h_ohlcv] 오류: {e}')


def _collect_ohlcv_bulk(interval: str, count: int, label: str) -> None:
    """전 종목 OHLCV 단일 타임프레임 수집·DB 적재 (연구용 풀 수집 공통 루틴).

    fetch_ohlcv(use_db_first=True)가 DB 신선 시 최근 50봉만 API로 받아 병합하므로
    호출당 API 부하가 낮다. ON CONFLICT DO NOTHING으로 중복 무시. 매매 로직 불변.
    OHLCV_FULL_UNIVERSE=True면 거래 가능 KRW 전 종목, False면 거래대금 top-N.
    """
    try:
        from trading_bot.config import OHLCV_COLLECT_ENABLED, OHLCV_FULL_UNIVERSE, COLLECT_SLEEP_SEC
    except Exception:
        OHLCV_COLLECT_ENABLED, OHLCV_FULL_UNIVERSE, COLLECT_SLEEP_SEC = True, True, 0.15
    if not OHLCV_COLLECT_ENABLED:
        return
    _log(f'[수집] collect_ohlcv[{label}] 실행')
    try:
        from trading_bot.data import (get_all_krw_tickers, get_all_krw_tickers_full,
                                      fetch_ohlcv)
        tickers = (get_all_krw_tickers_full(use_db_fallback=True)
                   if OHLCV_FULL_UNIVERSE else get_all_krw_tickers(use_db_fallback=True))
        ok = 0
        fail = 0
        for _t in tickers:
            try:
                df = fetch_ohlcv(ticker=_t, interval=interval, count=count, use_db_first=True)
                if df is not None and len(df) > 0:
                    ok += 1
                else:
                    fail += 1
            except Exception as _e:
                fail += 1
                _log(f'[수집] collect_ohlcv[{label}] {_t} 실패: {_e}', 'warning')
            time.sleep(COLLECT_SLEEP_SEC)
        _log(f'[수집] collect_ohlcv[{label}] 완료 — 성공 {ok} / 실패 {fail} / 대상 {len(tickers)}')
        if tickers and ok == 0:
            _notify_scheduler(f'⚠️ [OHLCV {label}] 전 종목 수집 실패')
    except Exception as e:
        _log(f'[수집] collect_ohlcv[{label}] 예외: {e}', 'error')
        _notify_scheduler(f'[collect_ohlcv {label}] 오류: {e}')


def _check_disk_capacity() -> None:
    """OHLCV 풀 수집 안전판: DB 데이터 디스크 사용률 80% 초과 시 1회 알림.

    1분봉 전 종목 누적은 디스크 소모가 크므로 임계 도달 시 보관일(env) 하향 유도.
    """
    try:
        import shutil
        total, used, free = shutil.disk_usage('/')
        pct = used / total * 100 if total else 0
        _log(f'[용량] 디스크 사용률 {pct:.1f}% (free {free/1e9:.1f}GB / total {total/1e9:.1f}GB)')
        if pct >= 80:
            _notify_scheduler(
                f'⚠️ [용량] 디스크 사용률 {pct:.0f}% — OHLCV 보관일(OHLCV_PRUNE_DAYS_1M 등) 하향 검토'
            )
    except Exception as e:
        _log(f'[용량] 디스크 체크 실패: {e}', 'warning')


def run_db_maintenance() -> None:
    """DB 하우스키핑(Pruning): 오래된 데이터 삭제. 매일 1회 실행."""
    _log('[스케줄러] db_maintenance 실행')
    _run_subprocess(DB_MAINTENANCE_CMD, 'db_maintenance', timeout_seconds=600)


def run_auto_tuner() -> None:
    """Walk-Forward 파라미터 튜닝: KRW-BTC/SOL 30일 1h 그리드 서치."""
    _log('[스케줄러] auto_tuner 실행')
    _run_subprocess(AUTO_TUNER_CMD, 'auto_tuner', timeout_seconds=1800)


def run_daily_briefing() -> None:
    """일일 브리핑 — 09:01 KST 발송. 전일 성과 + 장세 + 포트폴리오 요약."""
    try:
        from trading_bot.telegram_bot import send_daily_briefing
        send_daily_briefing()
        _log('[스케줄러] daily_briefing 발송 완료')
    except Exception as e:
        _log(f'[스케줄러] daily_briefing 실패: {e}', 'warning')


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

# ── 데이터 수집 Job ─────────────────────────────────────────────────────────

# 거시 지표 수집: 미장(22~06시) 매시 + 07:00 / 13:00 / 19:00 — 총 12회/일
# 미장 시간대 (22,23,0,1,2,3,4,5,6) + 평시 보완 (7,13,19)
sched.add_job(collect_macro, 'cron', hour='22,23,0,1,2,3,4,5,6,7,13,19',
              minute=0, id='collect_macro', misfire_grace_time=300)
_log('거시 지표 수집 스케줄 등록 (12회/일 — 미장 매시 + 07:00/13:00/19:00 KST)')

# 도미넌스 수집: 매시 HH:02 — 24회/일 (Guardian 매 사이클 소비)
sched.add_job(collect_dominance, 'cron', minute=2,
              id='collect_dominance', misfire_grace_time=120)
_log('도미넌스 수집 스케줄 등록 (매시 02분 — 24회/일)')

# 김치프리미엄 수집: 4회/일 (Guardian 미소비, 표시 전용)
sched.add_job(collect_kimp, 'cron', hour='0,6,12,18', minute=0,
              id='collect_kimp', misfire_grace_time=120)
_log('김프 수집 스케줄 등록 (4회/일 — 00:00/06:00/12:00/18:00 KST)')

# FNG 수집: 4회/일 (00:30, 06:30, 12:30, 18:30 KST)
sched.add_job(collect_fng, 'cron', hour='0,6,12,18', minute=30,
              id='collect_fng', misfire_grace_time=120)
_log('FNG 수집 스케줄 등록 (4회/일 — HH:30 at 00/06/12/18 KST)')

# BTC 주봉 200MA 수집: 매일 08:05 KST (전일 주봉 확정 후)
sched.add_job(collect_btc_weekly, 'cron', hour=8, minute=5,
              id='collect_btc_weekly', misfire_grace_time=300)
_log('BTC 주봉 200MA 수집 스케줄 등록 (매일 08:05 KST)')

# 4h(minute240) 전 종목 수집: 4h봉 마감 직후 — 6회/일 (1h 매매 사이클과 별도)
# Upbit minute240은 UTC 00시(=KST 09시) 정렬 → KST 01/05/09/13/17/21 마감.
sched.add_job(collect_4h_ohlcv, 'cron', hour='1,5,9,13,17,21', minute=5,
              id='collect_4h_ohlcv', misfire_grace_time=600)
_log('4h OHLCV 전 종목 수집 스케줄 등록 (6회/일 — KST 01/05/09/13/17/21 +5분)')
# 부팅 직후 1회 백필: 데이터 없는 종목 즉시 채워 다음 매수 사이클부터 confluence 작동.
sched.add_job(collect_4h_ohlcv, 'date',
              run_date=datetime.now() + timedelta(seconds=45),
              id='collect_4h_ohlcv_initial', misfire_grace_time=600)
_log('4h OHLCV 초기 백필 1회 예약 (부팅 +45초)')

# ── 연구 모드: 전 종목 × 전 타임프레임 OHLCV 풀 수집 ─────────────────────────
# 매매 무엣지 확정 후 신규 전략 설계용 데이터 축적. 매매/청산 사이클과 독립 실행.
if os.environ.get('OHLCV_COLLECT_ENABLED', '1').strip().lower() in ('1', 'true', 'yes', 'on'):
    from trading_bot.config import (ONE_MIN_OHLCV_COUNT, M15_OHLCV_COUNT, M30_OHLCV_COUNT,
                                    H1_FULL_OHLCV_COUNT, DAY_OHLCV_COUNT, WEEK_OHLCV_COUNT,
                                    MONTH_OHLCV_COUNT)

    # 1분봉: 5분마다 최근 N봉 묶음 수집 (매분 호출 대비 API 1/5). 정시 충돌 회피 위해 +30초.
    sched.add_job(lambda: _collect_ohlcv_bulk('minute1', ONE_MIN_OHLCV_COUNT, '1m'),
                  'cron', minute='*/5', second=30, id='collect_1m',
                  max_instances=1, misfire_grace_time=240)
    _log('1분봉 전 종목 수집 스케줄 등록 (5분마다 — 매매 사이클과 독립)')

    # 15분봉: 15분봉 마감 직후(+2분). 30분봉: 30분봉 마감 직후(+3분).
    sched.add_job(lambda: _collect_ohlcv_bulk('minute15', M15_OHLCV_COUNT, '15m'),
                  'cron', minute='2,17,32,47', id='collect_15m',
                  max_instances=1, misfire_grace_time=300)
    sched.add_job(lambda: _collect_ohlcv_bulk('minute30', M30_OHLCV_COUNT, '30m'),
                  'cron', minute='3,33', id='collect_30m',
                  max_instances=1, misfire_grace_time=300)
    _log('15m/30m 전 종목 수집 스케줄 등록')

    # 1시간봉 전 종목 보완: 매매 사이클은 top-60만 커버 → 나머지 종목을 매시 +3분에 채움.
    sched.add_job(lambda: _collect_ohlcv_bulk('minute60', H1_FULL_OHLCV_COUNT, '1h_full'),
                  'cron', minute=3, id='collect_60m_full',
                  max_instances=1, misfire_grace_time=300)
    _log('1h 전 종목 보완 수집 스케줄 등록 (매시 03분)')

    # 일/주/월봉: 1일 1회 (일봉 마감 09:00 KST 직후). 주·월봉은 단일 호출로 장기 백필.
    sched.add_job(lambda: _collect_ohlcv_bulk('day', DAY_OHLCV_COUNT, 'day'),
                  'cron', hour=9, minute=10, id='collect_day_full', misfire_grace_time=600)
    sched.add_job(lambda: _collect_ohlcv_bulk('week', WEEK_OHLCV_COUNT, 'week'),
                  'cron', hour=8, minute=12, id='collect_week', misfire_grace_time=600)
    sched.add_job(lambda: _collect_ohlcv_bulk('month', MONTH_OHLCV_COUNT, 'month'),
                  'cron', hour=8, minute=14, id='collect_month', misfire_grace_time=600)
    _log('일/주/월봉 전 종목 수집 스케줄 등록 (1일 1회)')

    # 디스크 용량 안전판: 6시간마다 사용률 점검, 80% 초과 시 알림.
    sched.add_job(_check_disk_capacity, 'interval', hours=6, id='disk_capacity',
                  misfire_grace_time=600)

    # 부팅 직후 단계적 백필(정시 충돌·rate-limit 분산 위해 시차 배치).
    for _delay, _iv, _cnt, _lb in (
        (60, 'day', DAY_OHLCV_COUNT, 'day'),
        (120, 'week', WEEK_OHLCV_COUNT, 'week'),
        (180, 'month', MONTH_OHLCV_COUNT, 'month'),
        (240, 'minute30', M30_OHLCV_COUNT, '30m'),
        (360, 'minute15', M15_OHLCV_COUNT, '15m'),
        (480, 'minute60', H1_FULL_OHLCV_COUNT, '1h_full'),
        (600, 'minute1', ONE_MIN_OHLCV_COUNT, '1m'),
    ):
        sched.add_job(
            (lambda iv, cnt, lb: (lambda: _collect_ohlcv_bulk(iv, cnt, lb)))(_iv, _cnt, _lb),
            'date', run_date=datetime.now() + timedelta(seconds=_delay),
            id=f'collect_{_lb}_initial', misfire_grace_time=900)
    _log('OHLCV 풀 수집 초기 백필 7종 예약 (부팅 +60~600초 시차)')
else:
    _log('OHLCV 풀 수집 비활성 (OHLCV_COLLECT_ENABLED=1로 활성화)')

# ── 기존 유지보수 Job ────────────────────────────────────────────────────────

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

# 일일 브리핑: 매일 09:01 KST (market_briefing 직후, 전일 요약 + 장세 + 포트폴리오)
sched.add_job(run_daily_briefing, 'cron', hour=9, minute=1, id='daily_briefing',
              misfire_grace_time=120)
_log('일일 브리핑 스케줄 등록 (매일 09:01 KST)')

# Heartbeat: 5분마다 (외부 모니터링용)
sched.add_job(_write_heartbeat, 'interval', minutes=5, id='heartbeat')
_log('Heartbeat 스케줄 등록 (5분마다)')

# Watchdog: 5분마다 이상 감지 알림
def _run_watchdog() -> None:
    try:
        from trading_bot.watchdog import run_watchdog
        run_watchdog()
    except Exception as e:
        _log(f'[watchdog] 실행 실패: {e}', 'error')

sched.add_job(_run_watchdog, 'interval', minutes=5, id='watchdog', misfire_grace_time=60)
_log('Watchdog 스케줄 등록 (5분마다)')

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
    _log('=' * 60)
    _log('등록된 스케줄 목록:')
    _log(f'  {"작업":<25} {"다음 실행 (KST)"}')
    _log(f'  {"-"*24} {"-"*22}')
    for _job in sched.get_jobs():
        _nrt = _job.next_run_time.strftime('%m/%d %H:%M') if _job.next_run_time else 'N/A'
        _log(f'  {_job.id:<25} {_nrt}')
    _log('=' * 60)

    try:
        from trading_bot.risk import get_system_state as _gss_boot
        _boot_equity = float(_gss_boot('prev_cycle_equity', '0') or 0)
        _equity_line = f'계좌 평가액: {_boot_equity:,.0f}원\n' if _boot_equity > 0 else ''
    except Exception:
        _equity_line = ''
    _notify_scheduler(
        f'스케줄러 기동\n'
        f'시각: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'매매: {"활성" if os.environ.get("ENABLE_AUTO_TRADING","0")=="1" else "비활성"}\n'
        f'{_equity_line}'
        f'캔들 오프셋: {_offset_sec}초 후'
    )

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        _graceful_shutdown()
