#!/usr/bin/env python3
"""
스케줄러에서 --once 로 주기 실행되는 매매 사이클 진입점.
사용: python -m trading_bot.tasks.auto_trader --once --mode paper
"""
import os
import sys
import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

# KST 타임존 강제 설정 (컨테이너 기본 UTC → 한국 표준시로 로그 시각 통일)
os.environ['TZ'] = 'Asia/Seoul'
time.tzset()

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# .env 로드 — override=True로 부모(스케줄러)에서 물려받은 TRADING_MODE=paper를 .env 값으로 덮어씀
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'trading_bot' / '.env', override=True)
except Exception:
    pass

# 로깅: auto_trader.log에 기록 (대시보드에서 조회)
LOG_DIR = ROOT / 'trading_bot' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'auto_trader.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# AI 전용 분석 로그 (trading_bot/logs/ai_debug.log) — 전략·매매 액션만 기록
try:
    from trading_bot.ai_logger import ai_logger
except Exception:
    ai_logger = logging.getLogger('trading_bot.ai')
# 서브프로세스/다른 CWD에서 실행 시 ai_debug.log 핸들러가 없을 수 있음 → 명시적으로 보강
_ai_log_file = ROOT / 'trading_bot' / 'logs' / 'ai_debug.log'
_ai_log_file.parent.mkdir(parents=True, exist_ok=True)
_has_ai_handler = any(
    getattr(h, 'baseFilename', None) and 'ai_debug.log' in str(getattr(h, 'baseFilename', ''))
    for h in getattr(ai_logger, 'handlers', [])
)
if not _has_ai_handler:
    _fh = logging.FileHandler(_ai_log_file, encoding='utf-8')
    _fh.setFormatter(logging.Formatter(fmt='%(asctime)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    ai_logger.addHandler(_fh)
    ai_logger.setLevel(logging.INFO)
    ai_logger.propagate = False

DEFAULT_INTERVAL = 'minute60'
DEFAULT_COUNT = 200
ACCOUNT_VALUE = float(os.environ.get('ACCOUNT_VALUE', '100000'))
# 업비트 최소 주문 금액(원). 이 금액 미만 보유 시 매도 신호 무시(무의미한 반복 로깅 방지)
MIN_ORDER_KRW = 5000

# [NEW] 동적 포지션 사이징 환경 변수 상수
RISK_PCT_BULL = float(os.environ.get('RISK_PCT_BULL', '0.05'))
RISK_PCT_BEAR = float(os.environ.get('RISK_PCT_BEAR', '0.02'))
MAX_PER_COIN_PCT = float(os.environ.get('MAX_PER_COIN_PCT', '0.20'))
ATR_SL_MULT = float(os.environ.get('ATR_SL_MULT', '2.0'))


# [NEW] 알림 레벨별 텔레그램 전송 헬퍼
def _notify(msg: str, level: str = 'TRADE'):
    """level이 TELEGRAM_ALERT_LEVEL 이상일 때만 텔레그램 전송. CRITICAL > TRADE > SUMMARY > OFF"""
    try:
        from trading_bot.config import TELEGRAM_ALERT_LEVEL
        priority = {'CRITICAL': 3, 'TRADE': 2, 'SUMMARY': 1, 'OFF': 0}
        if priority.get(level, 0) <= 0 or priority.get(TELEGRAM_ALERT_LEVEL, 2) <= 0:
            return
        if priority.get(level, 0) < priority.get(TELEGRAM_ALERT_LEVEL, 2):
            return
        from trading_bot.monitor import send_telegram
        send_telegram(msg)
    except Exception as e:
        logger.debug('텔레그램 전송 생략 (%s): %s', level, e)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--once', action='store_true', help='한 번 실행 후 종료')
    p.add_argument('--mode', default='paper', choices=['paper', 'live'], help='paper 또는 live')
    return p.parse_args()


def get_tickers():
    from trading_bot.data import get_all_krw_tickers
    return get_all_krw_tickers(use_db_fallback=True)


def get_executor(mode):
    from trading_bot.executor import PaperExecutor, LiveExecutor
    if mode == 'live':
        ex = LiveExecutor()
        if not getattr(ex, 'enabled', False):
            logger.warning('⚠️ LiveExecutor가 활성화되지 않았습니다. Paper 모드로 전환합니다.')
            logger.warning('   확인사항: LIVE_MODE=1, LIVE_CONFIRM="I CONFIRM LIVE", UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY 설정 확인')
            return PaperExecutor(initial_cash=ACCOUNT_VALUE), 'paper'
        return ex, 'live'
    return PaperExecutor(initial_cash=ACCOUNT_VALUE), 'paper'


# ---------------------------------------------------------------------------
# BTC 거시 장세 필터 (Global Market Filter): 대장주 하락 시 알트 매수 리스크 방지
# ---------------------------------------------------------------------------
def check_btc_global_trend(interval='day', count=50, ema_short=20, ema_long=50):
    """
    KRW-BTC의 장기 봉(일봉/4시간봉) 기준 EMA로 상승장 여부 판단.
    - 현재가 < EMA50 → 하락장(False)
    - 단기 이평이 장기 이평을 데드크로스한 상태(EMA20 < EMA50) → 하락장(False)
    - 그 외 → 상승/안전장(True)
    실패 시 안전하게 True 반환(필터 비적용).
    """
    import pandas as pd
    try:
        from trading_bot.data import fetch_ohlcv
        df = fetch_ohlcv(ticker='KRW-BTC', interval=interval, count=count, use_db_first=True)
        if df is None or len(df) < ema_long:
            return True
        close = df['close']
        ema_s = close.ewm(span=ema_short, adjust=False).mean()
        ema_l = close.ewm(span=ema_long, adjust=False).mean()
        current_price = float(close.iloc[-1])
        last_ema_s = float(ema_s.iloc[-1])
        last_ema_l = float(ema_l.iloc[-1])
        prev_ema_s = float(ema_s.iloc[-2]) if len(ema_s) >= 2 else last_ema_s
        prev_ema_l = float(ema_l.iloc[-2]) if len(ema_l) >= 2 else last_ema_l
        # 하락장 조건: 현재가가 EMA50 미만, 또는 데드크로스 상태(단기 < 장기)
        if current_price < last_ema_l:
            return False
        if last_ema_s < last_ema_l and prev_ema_s >= prev_ema_l:
            return False  # 방금 데드크로스
        if last_ema_s < last_ema_l:
            return False  # 이미 데드크로스된 상태
        return True
    except Exception as e:
        logger.debug('check_btc_global_trend 실패(필터 비적용): %s', e)
        return True


def compute_total_account_equity(executor, tickers):
    """
    현재 사이클 기준 총 계좌 평가액(KRW)을 근사 계산.
    - 가용 KRW + 각 티커 보유수량 * 현재가(Upbit 시세)
    """
    try:
        import pyupbit
    except Exception:
        # pyupbit 사용 불가 시 보수적으로 가용 현금만 사용
        return float(executor.get_available_cash())

    try:
        total = float(executor.get_available_cash() or 0)
    except Exception:
        total = 0.0

    seen = set()
    for t in tickers:
        if t in seen:
            continue
        seen.add(t)
        try:
            qty = float(executor.get_position_qty(t) or 0)
            if qty <= 0:
                continue
            price = pyupbit.get_current_price(t)
            if price is None:
                continue
            total += qty * float(price)
        except Exception:
            continue
    return float(total or 0.0)


def calculate_dynamic_size(total_equity, current_price, atr, size_pct, is_global_bull_market):
    """
    Regime-Dependent Dynamic Position Sizing (Risk Parity).
    반환: (final_buy_krw, risk_pct, sl_distance)
    """
    if current_price is None or current_price <= 0 or total_equity <= 0:
        return 0.0, 0.0, 0.0

    risk_pct = RISK_PCT_BULL if is_global_bull_market else RISK_PCT_BEAR  # [IMPROVED]
    risk_amount = total_equity * risk_pct

    try:
        atr_val = float(atr or 0)
    except Exception:
        atr_val = 0.0

    if not (atr_val and atr_val > 0):
        sl_distance = current_price * 0.05
    else:
        sl_distance = atr_val * ATR_SL_MULT  # [IMPROVED]

    if sl_distance <= 0:
        return 5000.0, risk_pct, 0.1

    target_quantity = risk_amount / sl_distance
    base_buy_krw = target_quantity * current_price
    max_per_coin = total_equity * MAX_PER_COIN_PCT  # [IMPROVED]
    bounded_buy_krw = min(max(base_buy_krw, MIN_ORDER_KRW), max_per_coin)

    try:
        sp = float(size_pct or 0)
    except Exception:
        sp = 0.0
    sp = max(0.0, min(1.0, sp)) or 1.0

    final_buy_krw = bounded_buy_krw * sp
    return float(final_buy_krw), float(risk_pct), float(sl_distance)


def analyze_ticker(ticker, executor, mode, defer_buy=False, is_global_bull_market=True):
    """
    티커 1개 분석 후 신호 처리.
    defer_buy=True(투패스 모드)일 때: sell은 즉시 실행, buy는 실행하지 않고 ('pending_buy', reason, data) 반환.
    is_global_bull_market=False이면 buy 신호를 hold로 덮어쓰고 매도만 수행.
    반환: (status, reason, data). data는 status=='pending_buy'일 때만 채워짐.
    """
    from trading_bot.data import fetch_ohlcv
    from trading_bot.data_manager import sync_indicators_for_ticker
    from trading_bot.strategy import generate_comprehensive_signal_with_logging
    from trading_bot.scale_out_manager import get_scale_out_state, set_scale_out_stage
    from trading_bot.param_manager import get_best_params

    try:
        df = fetch_ohlcv(ticker=ticker, interval=DEFAULT_INTERVAL, count=DEFAULT_COUNT)
    except Exception as e:
        logger.warning('[건너뜀] %s — OHLCV 조회 실패: %s', ticker, str(e))
        return 'skip', str(e), None

    if df is None or len(df) < 10:
        logger.warning('[건너뜀] %s — 데이터 부족 (len=%s)', ticker, len(df) if df is not None else 0)
        return 'skip', '데이터 부족', None

    try:
        sync_indicators_for_ticker(ticker, DEFAULT_INTERVAL, df_ohlcv=df)
    except Exception as e:
        logger.debug('[건너뜀] %s — 지표 동기화 실패: %s', ticker, str(e))

    try:
        current_price = float(df.iloc[-1]['close'])
    except Exception:
        current_price = None

    position_qty = executor.get_position_qty(ticker)
    avg_buy_price = executor.get_avg_buy_price(ticker)
    scale_out_stage = get_scale_out_state(ticker, avg_buy_price or 0.0, position_qty or 0.0)
    current_roi = ((current_price - avg_buy_price) / avg_buy_price * 100) if (avg_buy_price and float(avg_buy_price) > 0 and current_price is not None) else 0.0
    best_params = get_best_params()
    adx_trend_threshold = float(best_params.get('adx_trend_threshold', 25.0))

    try:
        result = generate_comprehensive_signal_with_logging(
            ticker=ticker,
            timeframe=DEFAULT_INTERVAL,
            current_price=current_price,
            account_value=ACCOUNT_VALUE,
            adx_trend_threshold=adx_trend_threshold,
            use_dynamic_risk=True,
            is_global_bull_market=is_global_bull_market,
            position_qty=position_qty or 0.0,
            current_roi=current_roi,
            scale_out_stage=scale_out_stage,
            avg_buy_price=avg_buy_price or 0.0,  # [NEW]
        )
    except Exception as e:
        logger.warning('[오류] %s — 신호 생성 중 예외: %s', ticker, str(e))
        logger.debug('[오류] %s — 상세', ticker, exc_info=True)
        return 'error', str(e), None

    signal = result.get('signal', 'hold')
    position_size = result.get('position_size') or 0.0
    reason = result.get('decision_reason', '')
    indicators = result.get('indicators') or {}
    adx = float(indicators.get('adx', 0) or 0)
    atr = float(indicators.get('atr', 0) or 0)
    regime = result.get('regime', '')
    vol_ratio = float(indicators.get('vol_ratio', 0) or 0)

    # ----- Balanced+: partial stop-loss (risk first), then DCA -----
    try:
        from trading_bot.balanced_plus import (
            PARTIAL_STOP_1_ROI_PCT, PARTIAL_STOP_1_SELL_PCT,
            PARTIAL_STOP_2_ROI_PCT, PARTIAL_STOP_2_SELL_PCT,
            is_in_partial_stop_cooldown, log_execution_event,
            TAG_PS1, TAG_PS2, TAG_EXEC_SELL,
            DCA_ENABLED, DCA_TRIGGER_ROI_PCT, DCA_SIZE_MULTIPLIER, DCA_MIN_VOL_RATIO,
            DCA_ALLOWED_REGIMES, DCA_MAX_PER_TICKER_PER_DAY, TAG_DCA_BUY,
            is_in_buy_cooldown, is_in_dca_cooldown, count_tag_last_24h,
        )
        pos = position_qty or 0.0
        full_exit = (signal == 'sell' and float(result.get('sell_size_pct', 1.0) or 1.0) >= 1.0)
        # Partial stop: do not block full exit (ATR/dead cross); only add partial sells in loss territory
        if pos > 0 and not full_exit and not is_in_partial_stop_cooldown(ticker):
            if current_roi <= PARTIAL_STOP_2_ROI_PCT:
                position_value_ps = (current_price or 0) * pos
                if position_value_ps >= MIN_ORDER_KRW:
                    try:
                        executor.place_order('sell', current_price, size_pct=PARTIAL_STOP_2_SELL_PCT, ticker=ticker)
                        logger.info('✅ %s 부분손절 PS2 실행 (ROI %.1f%%, 비중 %.0f%%)', ticker, current_roi, PARTIAL_STOP_2_SELL_PCT * 100)
                        ai_logger.info('[EXECUTE] %s | ACTION:SELL | PS2 | ROI:%.1f%%', ticker, current_roi)
                        log_execution_event(ticker, 'sell', TAG_PS2, current_price)
                        return 'executed', None, None
                    except Exception as e:
                        logger.warning('[오류] %s — PS2 실행 실패: %s', ticker, str(e))
            elif current_roi <= PARTIAL_STOP_1_ROI_PCT:
                position_value_ps = (current_price or 0) * pos
                if position_value_ps >= MIN_ORDER_KRW:
                    try:
                        executor.place_order('sell', current_price, size_pct=PARTIAL_STOP_1_SELL_PCT, ticker=ticker)
                        logger.info('✅ %s 부분손절 PS1 실행 (ROI %.1f%%, 비중 %.0f%%)', ticker, current_roi, PARTIAL_STOP_1_SELL_PCT * 100)
                        ai_logger.info('[EXECUTE] %s | ACTION:SELL | PS1 | ROI:%.1f%%', ticker, current_roi)
                        log_execution_event(ticker, 'sell', TAG_PS1, current_price)
                        return 'executed', None, None
                    except Exception as e:
                        logger.warning('[오류] %s — PS1 실행 실패: %s', ticker, str(e))
        # DCA: only in trend, loss, volume ok, bull, cooldowns ok, under daily cap
        if pos > 0 and DCA_ENABLED and regime in DCA_ALLOWED_REGIMES and is_global_bull_market:
            if (current_roi <= DCA_TRIGGER_ROI_PCT and vol_ratio >= DCA_MIN_VOL_RATIO
                    and not is_in_buy_cooldown(ticker) and not is_in_dca_cooldown(ticker)
                    and count_tag_last_24h(ticker, TAG_DCA_BUY) < DCA_MAX_PER_TICKER_PER_DAY):
                base_pct = max(0.01, min(1.0, position_size / ACCOUNT_VALUE)) if ACCOUNT_VALUE > 0 else 0.02
                dca_pct = max(0.01, min(1.0, base_pct * DCA_SIZE_MULTIPLIER))
                position_value_dca = (current_price or 0) * pos
                if position_value_dca >= MIN_ORDER_KRW and (ACCOUNT_VALUE * dca_pct) >= MIN_ORDER_KRW:
                    try:
                        executor.place_order('buy', current_price, size_pct=dca_pct, ticker=ticker)
                        logger.info('✅ %s DCA 매수 실행 (ROI %.1f%%, 비중 %.2f%%)', ticker, current_roi, dca_pct * 100)
                        ai_logger.info('[EXECUTE] %s | ACTION:BUY | DCA_BUY | ROI:%.1f%%', ticker, current_roi)
                        log_execution_event(ticker, 'buy', TAG_DCA_BUY, current_price)
                        return 'executed', None, None
                    except Exception as e:
                        logger.warning('[오류] %s — DCA 매수 실패: %s', ticker, str(e))
    except Exception as e:
        logger.debug('[Balanced+] %s — %s', ticker, str(e))

    # BTC 거시 필터·경주마 예외는 strategy.generate_comprehensive_signal_with_logging 내부에서 처리됨

    if '캐싱된 지표 데이터 없음' in reason:
        logger.debug('[건너뜀] %s — 캐싱된 지표 없음 (전략 미판단)', ticker)
        return 'skip', '캐싱된 지표 없음', None

    if '완성 봉 데이터 부족' in reason:
        logger.debug('[건너뜀] %s — 완성 봉 3봉 미만', ticker)
        return 'skip', reason, None

    if signal == 'hold':
        return 'hold', reason, None

    # 매수: defer_buy(투패스)일 때는 실행하지 않고 pending_buys용 데이터만 반환
    if signal == 'buy' and position_size > 0:
        # 전략에서 명시한 size_pct가 있으면 우선 사용, 없으면 position_size 기반으로 계산
        strategy_size_pct = result.get('size_pct')
        if strategy_size_pct is not None:
            try:
                size_pct = float(strategy_size_pct)
            except (TypeError, ValueError):
                size_pct = None
        else:
            size_pct = None
        if size_pct is None:
            size_pct = max(0.01, min(1.0, position_size / ACCOUNT_VALUE)) if ACCOUNT_VALUE > 0 else 0.02
        else:
            size_pct = max(0.01, min(1.0, size_pct))
        if defer_buy:
            estimated_spend = ACCOUNT_VALUE * size_pct if current_price else 0
            return 'pending_buy', reason, {
                'ticker': ticker,
                'adx': adx,
                'price': current_price,
                'position_size': position_size,
                'size_pct': size_pct,
                'estimated_spend': estimated_spend,
                'atr': atr,
            }
        try:
            executor.place_order('buy', current_price, size_pct=size_pct, ticker=ticker)
            logger.info('✅ %s 매수 신호 실행: 가격 %.0f, 비중 %.2f%%', ticker, current_price, size_pct * 100)
            alloc = ACCOUNT_VALUE * size_pct if current_price else 0
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(
                    event_type='EXECUTE', ticker=ticker, signal='buy',
                    price=current_price, avg_buy_price=avg_buy_price,
                    regime=regime, timeframe=DEFAULT_INTERVAL,
                    adx=adx, atr=atr, vol_ratio=vol_ratio,
                    position_size=alloc, size_pct=size_pct,
                    decision_reason=reason, roi=current_roi,
                    api_status='ok',
                )
            except Exception:
                pass
            return 'executed', None, None
        except Exception as e:
            logger.warning('[오류] %s — 매수 주문 실행 실패: %s', ticker, str(e))
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(event_type='ERROR', ticker=ticker, signal='buy',
                             price=current_price, decision_reason=str(e)[:200],
                             api_status='error', timeframe=DEFAULT_INTERVAL)
            except Exception:
                pass
            return 'error', str(e), None

    if signal == 'sell':
        position_qty_sell = executor.get_position_qty(ticker)
        if position_qty_sell <= 0:
            logger.debug('[매도 스킵] %s — 보유 잔고 없음', ticker)
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(event_type='SKIP', ticker=ticker, signal='sell',
                             price=current_price, decision_reason='보유잔고없음',
                             timeframe=DEFAULT_INTERVAL)
            except Exception:
                pass
            return 'hold', '보유 잔고 없음 (매도 스킵)', None
        position_value = (current_price or 0) * position_qty_sell
        if position_value < MIN_ORDER_KRW:
            logger.debug('[매도 스킵] %s — 보유 금액 %.0f원 < 최소 주문 %s원', ticker, position_value, MIN_ORDER_KRW)
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(event_type='SKIP', ticker=ticker, signal='sell',
                             price=current_price, decision_reason=f'최소주문미만(보유{position_value:.0f}원)',
                             timeframe=DEFAULT_INTERVAL)
            except Exception:
                pass
            return 'hold', '보유 잔고 없음 (매도 스킵)', None
        sell_size_pct = result.get('sell_size_pct', 1.0)
        next_scale_out_stage = result.get('next_scale_out_stage')
        try:
            executor.place_order('sell', current_price, size_pct=sell_size_pct, ticker=ticker)
            logger.info('✅ %s 매도 신호 실행: 가격 %.0f, 비중 %.0f%%', ticker, current_price, sell_size_pct * 100)
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(
                    event_type='EXECUTE', ticker=ticker, signal='sell',
                    price=current_price, avg_buy_price=avg_buy_price,
                    regime=regime, timeframe=DEFAULT_INTERVAL,
                    adx=adx, atr=atr, vol_ratio=vol_ratio,
                    size_pct=sell_size_pct,
                    decision_reason=reason, roi=current_roi,
                    api_status='ok',
                    extra={'scale_out_stage': next_scale_out_stage, 'qty': position_qty_sell},
                )
            except Exception:
                pass
            try:
                from trading_bot.balanced_plus import log_execution_event, TAG_EXEC_SELL
                log_execution_event(ticker, 'sell', TAG_EXEC_SELL, current_price)
            except Exception:
                pass
            if next_scale_out_stage is not None:
                set_scale_out_stage(ticker, next_scale_out_stage, avg_buy_price or 0.0)
            if sell_size_pct >= 1.0:
                get_scale_out_state(ticker, 0.0, 0.0)
            _notify(f'🔴 매도 체결: {ticker} @ {current_price:,.0f}원', level='TRADE')
            return 'executed', None, None
        except Exception as e:
            logger.warning('[오류] %s — 매도 주문 실행 실패: %s', ticker, str(e))
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(event_type='ERROR', ticker=ticker, signal='sell',
                             price=current_price, decision_reason=str(e)[:200],
                             api_status='error', timeframe=DEFAULT_INTERVAL)
            except Exception:
                pass
            return 'error', str(e), None

    return 'hold', reason, None


def run_cycle(mode):
    args = parse_args()
    tickers = get_tickers()
    executor, effective_mode = get_executor(mode)
    # 참고: Paper 모드에서는 실행기가 티커별 포지션이 아닌 단일 cash/position을 사용합니다.
    # 여러 티커 매수 시 시뮬레이션 정확도를 위해 TICKERS로 소수만 지정하거나, Live 시 티커별 포지션을 사용하세요.

    logger.info('✅ %s개의 KRW 마켓 코인 로드 완료', len(tickers))
    logger.info('✅ AutoTrader 초기화 완료 (모드: %s, 티커 수: %s, 실행기: %s)',
                effective_mode, len(tickers), type(executor).__name__)

    cycle_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    logger.info('')
    logger.info('=' * 80)
    logger.info('📊 트레이딩 사이클 시작 [%s]: %s', cycle_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    logger.info('모니터링 티커 수: %s개', len(tickers))
    logger.info('=' * 80)

    # Live 시 잔고 1회 조회 후 캐시 사용 → 티커별 get_position_qty 시 API 추가 호출 없음
    executor.refresh_balance_cache()
    try:
        cash = executor.get_available_cash()
        msg = (
            f'📊 트레이딩 사이클 시작 [{cycle_id}]\n'
            f'시각: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
            f'모드: {effective_mode}\n'
            f'티커: {len(tickers)}개\n'
            f'실행기: {type(executor).__name__}\n'
            f'가용 현금: {cash:,.0f}원'
        )
        _notify(msg, level='SUMMARY')  # [IMPROVED]
    except Exception as e:
        logger.debug('텔레그램(사이클 시작) 발송 생략: %s', e)

    start = datetime.now()
    stats = {'executed': 0, 'hold': 0, 'skip': 0, 'error': 0, 'pending_buy': 0}
    skip_reasons = []
    pending_buys = []  # Pass 1에서 수집한 매수 후보 (Pass 2에서 ADX 순 정렬 후 실행)
    cycle_error = None

    # ----- Pass 0: 보유 포지션 하드 스탑로스 체크 (신호 분석 전 최우선 실행) -----
    if hasattr(executor, 'check_hard_stop_loss'):
        try:
            import pyupbit
            for t in tickers:
                qty = executor.get_position_qty(t)
                if qty and qty > 0:
                    cur_p = pyupbit.get_current_price(t)
                    if cur_p:
                        triggered = executor.check_hard_stop_loss(t, cur_p)
                        if triggered:
                            logger.info('[HARD STOP-LOSS] %s 트리거 — 전량 매도 실행됨', t)
                            ai_logger.info('[EXECUTE] %s | ACTION:SELL | REASON:HARD_STOP_LOSS', t)
                            stats['executed'] = stats.get('executed', 0) + 1
            executor.refresh_balance_cache()  # 스탑로스 후 잔고 갱신
        except Exception as e:
            logger.warning('[HARD STOP-LOSS] 체크 중 오류 (계속 진행): %s', e)

    # ----- BTC 거시 장세 필터: 1회 조회 후 이번 사이클 매수 허용 여부 결정 -----
    is_global_bull_market = check_btc_global_trend(interval='day', count=50)
    if not is_global_bull_market:
        logger.info('🚨 BTC 하락 추세 감지: 이번 사이클은 신규 매수(Buy)를 전면 차단하고 매도(Sell)만 수행합니다.')
        _notify(
            '🚨 BTC 하락 추세 감지: 이번 사이클은 신규 매수(Buy)를 전면 차단하고 매도(Sell)만 수행합니다.',
            level='CRITICAL',
        )  # [IMPROVED]

    # ----- Pass 1: 분석 및 매도 즉시 실행, 매수는 pending_buys에만 수집 -----
    try:
        for ticker in tickers:
            try:
                out = analyze_ticker(ticker, executor, mode, defer_buy=True, is_global_bull_market=is_global_bull_market)
                status = out[0]
                reason = out[1] if len(out) > 1 else None
                data = out[2] if len(out) > 2 else None
                stats[status] = stats.get(status, 0) + 1
                if status == 'pending_buy' and data:
                    pending_buys.append(data)
                if status == 'skip' and reason and reason not in ('캐싱된 지표 없음',):
                    skip_reasons.append(f'{ticker}: {reason[:60]}')
                if status == 'error' and reason:
                    logger.warning('[오류 요약] %s — %s', ticker, reason[:80])
            except Exception as e:
                stats['error'] = stats.get('error', 0) + 1
                logger.warning('[오류] %s — 티커 처리 중 예외: %s', ticker, str(e))
                logger.debug('[오류] %s 상세', ticker, exc_info=True)
                skip_reasons.append(f'{ticker}: 예외={str(e)[:50]}')
            time.sleep(0.05)
    except Exception as e:
        cycle_error = e
        logger.exception('⚠️ 사이클 루프 중 예외 발생 (종료 로그는 아래에 출력): %s', str(e))

    # ----- Pass 2: 매수 우선순위(ADX 내림차순) 정렬 후 Regime 기반 동적 포지션 사이징으로 순차 매수 -----
    # Bear market에서도 strategy가 필터링한 예외(ADX>=40, Volume 1.5x 등 Racehorse)는 pending_buys에 포함되므로 Pass 2 실행.
    if not cycle_error and pending_buys:
        executor.refresh_balance_cache()
        remaining_cash = executor.get_available_cash()
        total_equity = compute_total_account_equity(executor, tickers)
        try:
            from trading_bot.balanced_plus import (
                MAX_BUYS_PER_CYCLE, MAX_OPEN_POSITIONS,
                is_in_buy_cooldown, count_open_positions, log_execution_event, TAG_EXEC_BUY,
            )
        except Exception:
            MAX_BUYS_PER_CYCLE = 2
            MAX_OPEN_POSITIONS = 6
            def is_in_buy_cooldown(_t): return False
            def count_open_positions(ex, tk): return sum(1 for t in tk if (ex.get_position_qty(t) or 0) > 0)
            def log_execution_event(*a, **k): pass
            TAG_EXEC_BUY = 'EXEC_BUY'
        buys_executed_this_cycle = 0

        # 정렬: ADX가 높을수록 추세가 강하므로 우선 매수 (내림차순)
        pending_buys_sorted = sorted(pending_buys, key=lambda x: float(x.get('adx', 0) or 0), reverse=True)
        try:
            tickers_str = ', '.join(item['ticker'] for item in pending_buys_sorted[:10])
            if len(pending_buys_sorted) > 10:
                tickers_str += f' 외 {len(pending_buys_sorted) - 10}건'
            _notify(f'🔔 매수 신호 발생: {len(pending_buys_sorted)}건 — {tickers_str}', level='SUMMARY')  # [IMPROVED]
        except Exception as e:
            logger.debug('텔레그램(매수 신호) 발송 생략: %s', e)

        logger.info('')
        logger.info('📋 매수 대기 큐 정렬 결과 (ADX 추세 강도 높은 순, %s건):', len(pending_buys_sorted))
        for i, item in enumerate(pending_buys_sorted[:20], 1):
            logger.info('  %s. %s — ADX=%.1f, 가격=%.0f, 예상비용=%.0f원', i, item['ticker'], item.get('adx', 0), item.get('price') or 0, item.get('estimated_spend', 0))
        if len(pending_buys_sorted) > 20:
            logger.info('  ... 외 %s건', len(pending_buys_sorted) - 20)

        # 가용 현금 및 Regime 기반 동적 포지션 사이징: 소액 시드도 5,000원 이상이면 실행.
        for item in pending_buys_sorted:
            if buys_executed_this_cycle >= MAX_BUYS_PER_CYCLE:
                logger.info('[Pass2] MAX_BUYS_PER_CYCLE(%s) 도달로 매수 중단', MAX_BUYS_PER_CYCLE)
                ai_logger.info('[SKIP] MAX_BUYS_PER_CYCLE reached (%s)', MAX_BUYS_PER_CYCLE)
                break
            if count_open_positions(executor, tickers) >= MAX_OPEN_POSITIONS:
                logger.info('[Pass2] MAX_OPEN_POSITIONS(%s) 도달로 매수 중단', MAX_OPEN_POSITIONS)
                ai_logger.info('[SKIP] MAX_OPEN_POSITIONS reached (%s)', MAX_OPEN_POSITIONS)
                break
            # [IMPROVED] 연속 손실 4회+ 시 포지션 크기 0 → 매수 스킵
            position_size = float(item.get('position_size', 0) or 0)
            if position_size <= 0:
                ticker = item.get('ticker', '')
                logger.info('[매수 스킵] %s — 연속 손실 쿨다운 (position_size=0)', ticker)
                ai_logger.info('[SKIP] %s | REASON:연속손실쿨다운', ticker)
                continue
            ticker = item['ticker']
            price = item.get('price')
            size_pct = item.get('size_pct', 0.02)
            atr = item.get('atr', 0.0)
            if price is None:
                continue
            if is_in_buy_cooldown(ticker):
                logger.info('[매수 스킵] %s — BUY_COOLDOWN (Balanced+)', ticker)
                ai_logger.info('[SKIP] %s | REASON:SKIP_BUY_COOLDOWN', ticker)
                continue

            # Regime-Dependent Dynamic Position Sizing
            final_buy_krw, risk_pct, sl_distance = calculate_dynamic_size(
                total_equity=total_equity,
                current_price=price,
                atr=atr,
                size_pct=size_pct,
                is_global_bull_market=is_global_bull_market,
            )
            if final_buy_krw <= 0:
                continue

            # 실제 매수할당액 = 계산된 금액과 가용 현금 중 작은 값
            alloc = min(final_buy_krw, remaining_cash)
            # 업비트 최소 주문 금액(5,000원) 미만이면 스킵
            if alloc < MIN_ORDER_KRW:
                if final_buy_krw < MIN_ORDER_KRW:
                    logger.info('[매수 스킵] %s — 최소 주문 금액(%s원) 미달로 매수 스킵 (예상비용 %.0f원)', ticker, MIN_ORDER_KRW, final_buy_krw)
                    ai_logger.info('[SKIP] %s | REASON:최소주문미달 | Amt:%.0fKRW', ticker, final_buy_krw)
                else:
                    logger.info('[매수 스킵] %s — 가용 현금 부족으로 매수 스킵 (가용 %.0f원 < 5,000원)', ticker, remaining_cash)
                    ai_logger.info('[SKIP] %s | REASON:현금부족 | Cash:%.0fKRW', ticker, remaining_cash)
                continue
            # ----- 중복 매수 방어: 이미 해당 티커를 5,000원 이상 보유 중이면 매수 스킵 -----
            # 1시간봉 직전 완성 캔들 기준 신호라 동일 조건이 1시간 유지되어, 5분 주기 시 같은 티커에 반복 매수되는 버그 방지.
            # 매도 후 찌꺼기 잔고(1~2원 등)만 있을 때는 5,000원 미만이므로 스킵하지 않고 정상 매수 허용.
            hold_qty = executor.get_position_qty(ticker)
            hold_value = (hold_qty or 0) * (price or 0)
            if hold_value >= MIN_ORDER_KRW:
                logger.info('[%s] 이미 포지션을 보유 중이므로 중복 매수 스킵', ticker)
                ai_logger.info('[SKIP] %s | REASON:중복매수방지(이미보유) | HoldValue:%.0fKRW', ticker, hold_value)
                continue
            # alloc 금액만큼 매수하기 위해 비율 계산 (executor는 size_pct로 잔고 대비 비율 사용)
            if remaining_cash <= 0:
                continue
            effective_pct = alloc / remaining_cash
            try:
                executor.place_order('buy', price, size_pct=effective_pct, ticker=ticker)
                logger.info(
                    '✅ %s 매수 실행 (ADX=%.1f): 가격 %.0f, 동적 배분 %.0f원 (RegimeRisk: %.1f%%, size_pct: %.2f)',
                    ticker,
                    item.get('adx', 0),
                    price,
                    alloc,
                    risk_pct * 100,
                    float(size_pct or 0.0),
                )
                try:
                    from trading_bot.ai_logger import log_ai_event
                    log_ai_event(
                        event_type='EXECUTE', ticker=ticker, signal='buy',
                        price=price, regime=None, timeframe=DEFAULT_INTERVAL,
                        adx=float(item.get('adx', 0) or 0),
                        atr=float(atr or 0),
                        position_size=alloc, size_pct=float(size_pct or 0),
                        decision_reason=f'Pass2 ADX순 매수',
                        api_status='ok',
                        extra={
                            'risk_pct': risk_pct, 'sl_distance': sl_distance,
                            'final_buy_krw': final_buy_krw,
                            'is_global_bull_market': is_global_bull_market,
                        },
                    )
                except Exception:
                    pass
                stats['executed'] = stats.get('executed', 0) + 1
                buys_executed_this_cycle += 1
                try:
                    log_execution_event(ticker, 'buy', TAG_EXEC_BUY, price)
                except Exception:
                    pass
                executor.refresh_balance_cache()
                remaining_cash = executor.get_available_cash()
                _notify(f'🟢 매수 체결: {ticker} @ {price:,.0f}원 (비중 {size_pct*100:.1f}%)', level='TRADE')
            except Exception as e:
                logger.warning('[오류] %s — 매수 주문 실행 실패: %s', ticker, str(e))
                try:
                    from trading_bot.ai_logger import log_ai_event
                    log_ai_event(event_type='ERROR', ticker=ticker, signal='buy',
                                 price=price, decision_reason=str(e)[:200],
                                 api_status='error', timeframe=DEFAULT_INTERVAL)
                except Exception:
                    pass
                stats['error'] = stats.get('error', 0) + 1

    elapsed = (datetime.now() - start).total_seconds()
    logger.info('')
    if cycle_error:
        logger.info('⚠️ 사이클 완료 [%s] (예외로 조기 종료)', cycle_id)
        logger.info('예외: %s', str(cycle_error))
    else:
        logger.info('✅ 사이클 완료 [%s]', cycle_id)
    logger.info('소요 시간: %.2f초', elapsed)
    logger.info('분석 완료: %s/%s개 티커', len(tickers), len(tickers))
    logger.info('신호 생성: %s개', stats.get('executed', 0) + stats.get('hold', 0))
    logger.info('거래 실행: %s개', stats.get('executed', 0))
    logger.info('건너뜀: %s개', stats.get('skip', 0))
    logger.info('오류: %s개', stats.get('error', 0))
    if skip_reasons:
        logger.info('')
        logger.info('📋 건너뛴 거래 내역 (%s건):', min(len(skip_reasons), 20))
        for r in skip_reasons[:20]:
            logger.info('  - %s', r)
    logger.info('=' * 80)
    # 버퍼 미플러시로 로그가 안 보이는 경우 방지
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass
    for h in getattr(ai_logger, 'handlers', []):
        try:
            h.flush()
        except Exception:
            pass


def main():
    args = parse_args()
    # .env의 TRADING_MODE 우선 (스케줄러가 --mode paper로 호출해도 live 적용)
    mode = os.environ.get('TRADING_MODE') or args.mode or 'paper'
    run_cycle(mode)


if __name__ == '__main__':
    main()

