#!/usr/bin/env python3
"""
스케줄러에서 --once 로 주기 실행되는 매매 사이클 진입점.
사용: python -m trading_bot.tasks.auto_trader --once --mode paper
"""
import os
import sys
import argparse
import logging
import threading
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

# 매수 직후 Upbit 정산 딜레이 동안 자산이 _balance_cache에 반영되기 전까지
# 매수 비용을 equity에 보정하기 위한 임시 저장소
# { asset: (cost_krw, bought_at_timestamp) }
_pending_buy_costs: dict = {}
# 상장폐지 확인된 티커 캐시 — 최초 감지 시 저장, 이후 API 호출 스킵
_delisted_tickers: set = set()
_UPBIT_CODE_NOT_FOUND = 'Code not found'
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
def check_btc_global_trend(interval='day', count=50, ema_short=5, ema_long=20):
    """
    KRW-BTC 일봉 기준 EMA 크로스로 상승장 여부 판단.
    - EMA5 > EMA20 (골든크로스 상태) → 상승장(True)
    - EMA5 < EMA20 (데드크로스 상태) → 하락장(False)
    EMA20>EMA50 대비 반응이 빠르면서, 단순 현재가>EMA 대비 노이즈가 적음.
    (며칠간 상승 모멘텀 유지돼야 True 반환)
    실패 시 안전하게 True 반환(필터 비적용).
    """
    try:
        from trading_bot.data import fetch_ohlcv
        df = fetch_ohlcv(ticker='KRW-BTC', interval=interval, count=count, use_db_first=True)
        if df is None or len(df) < ema_long:
            return True
        close = df['close']
        ema_s = close.ewm(span=ema_short, adjust=False).mean()
        ema_l = close.ewm(span=ema_long, adjust=False).mean()
        last_ema_s = float(ema_s.iloc[-1])
        last_ema_l = float(ema_l.iloc[-1])
        # EMA5 < EMA20 → 데드크로스 → 하락장
        if last_ema_s < last_ema_l:
            return False
        return True
    except Exception as e:
        logger.debug('check_btc_global_trend 실패(필터 비적용): %s', e)
        return True


def _record_manual_order(ticker: str, side: str, price: float, qty: float):
    """수동 거래를 Order 테이블에 기록하여 P&L 계산에 반영."""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Order
        import datetime as _dt
        if not price or not qty:
            return
        session = get_session()
        try:
            now = _dt.datetime.now(_dt.timezone.utc)
            o = Order(
                order_id=f'manual_{side}_{ticker}_{int(now.timestamp())}',
                ts=now,
                side=side,
                price=float(price),
                qty=float(qty),
                status='done',
                fee=0.0,
                raw={'manual': True, 'ticker': ticker},
            )
            session.add(o)
            session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.debug('[_record_manual_order] 오류 (무시): %s', e)


def sync_manual_trades(executor, tickers):
    """refresh_balance_cache() 직후 호출. 이전 사이클 잔고와 비교해 수동 거래를 감지하고
    ExecutionEvent(MANUAL_BUY/MANUAL_SELL)에 기록. last_buy_ts()가 이를 읽어 쿨다운 적용."""
    try:
        from trading_bot.risk import get_system_state, set_system_state
        from trading_bot.balanced_plus import (
            log_execution_event, TAG_EXEC_BUY, TAG_EXEC_SELL,
            TAG_DCA_BUY, TAG_CB_SELL, TAG_PS1, TAG_PS2,
            TAG_MANUAL_BUY, TAG_MANUAL_SELL,
        )
        from trading_bot.db import get_session
        from trading_bot.models import ExecutionEvent
        import json
        import datetime as _dt

        cache = getattr(executor, '_balance_cache', {}) or {}
        avg_cache = getattr(executor, '_avg_buy_price_cache', {}) or {}

        prev_json = get_system_state('balance_snapshot', '{}')
        try:
            prev = json.loads(prev_json)
        except Exception:
            prev = {}

        if prev:
            managed_assets = {t.split('-')[1]: t for t in tickers if '-' in t}
            bot_tags = (TAG_EXEC_BUY, TAG_EXEC_SELL, TAG_DCA_BUY, TAG_CB_SELL, TAG_PS1, TAG_PS2)
            for asset, ticker in managed_assets.items():
                prev_qty = float(prev.get(asset, 0) or 0)
                curr_qty = float(cache.get(asset, 0) or 0)
                if abs(curr_qty - prev_qty) < 1e-10:
                    continue
                # 봇 주문 여부: 최근 2분 내 ExecutionEvent 확인
                session = get_session()
                try:
                    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=2)
                    recent_bot = session.query(ExecutionEvent).filter(
                        ExecutionEvent.ticker == ticker,
                        ExecutionEvent.ts >= cutoff,
                        ExecutionEvent.tag.in_(bot_tags),
                    ).first()
                finally:
                    session.close()
                if recent_bot:
                    continue
                if curr_qty > prev_qty:
                    avg_price = float(avg_cache.get(asset, 0) or 0)
                    delta_qty = curr_qty - prev_qty
                    logger.info('[수동 거래 감지] %s 매수 %.6f→%.6f (avg=%.0f원)', ticker, prev_qty, curr_qty, avg_price)
                    log_execution_event(ticker, 'buy', TAG_MANUAL_BUY, avg_price)
                    _record_manual_order(ticker, 'buy', avg_price, delta_qty)
                else:
                    delta_qty = prev_qty - curr_qty
                    # 매도 시 평균매수가를 fill price로 근사 (실제 체결가 미조회)
                    avg_price = float(avg_cache.get(asset, 0) or 0)
                    logger.info('[수동 거래 감지] %s 매도 %.6f→%.6f', ticker, prev_qty, curr_qty)
                    log_execution_event(ticker, 'sell', TAG_MANUAL_SELL, avg_price)
                    _record_manual_order(ticker, 'sell', avg_price, delta_qty)

        set_system_state('balance_snapshot', json.dumps(dict(cache)))
    except Exception as e:
        logger.debug('[sync_manual_trades] 오류 (무시): %s', e)


def compute_total_account_equity(executor, tickers):
    """
    현재 사이클 기준 총 계좌 평가액(KRW)을 근사 계산.
    - 가용 KRW + 계좌 내 모든 non-KRW 자산 * 현재가(Upbit 시세)
    - 봇 관리 티커 외 수동 매수 자산도 포함하여 서킷 브레이커 오발동 방지
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

    # asset → ticker 매핑 (봇 관리 티커 우선)
    asset_to_ticker = {}
    for t in tickers:
        if '-' in t:
            asset_to_ticker[t.split('-')[1]] = t

    # 계좌 내 모든 non-KRW 자산 포함 (수동 매수 자산도 포함)
    cache = getattr(executor, '_balance_cache', {}) or {}
    avg_cache = getattr(executor, '_avg_buy_price_cache', {}) or {}
    seen = set()
    for asset, bal in cache.items():
        if asset == 'KRW' or asset in seen:
            continue
        seen.add(asset)
        qty = float(bal or 0)
        if qty <= 0:
            continue
        ticker = asset_to_ticker.get(asset, f'KRW-{asset}')
        if ticker in _delisted_tickers:
            logger.debug('[equity] %s 상장폐지 캐시 → 제외', ticker)
            continue
        try:
            price = pyupbit.get_current_price(ticker)
            if price is None:
                price = float(avg_cache.get(asset) or 0)
        except Exception as _price_exc:
            err_msg = str(_price_exc)
            if _UPBIT_CODE_NOT_FOUND in err_msg:
                _delisted_tickers.add(ticker)
                logger.warning('[equity] %s 상장폐지 감지 → equity 제외', ticker)
                # 최초 감지 시에만 알림 (DB에 없는 경우)
                try:
                    from trading_bot.risk import get_system_state, set_system_state
                    import json as _json
                    known = set(_json.loads(get_system_state('known_delisted_tickers', '[]') or '[]'))
                    if ticker not in known:
                        known.add(ticker)
                        set_system_state('known_delisted_tickers', _json.dumps(list(known)))
                        threading.Thread(
                            target=_notify,
                            args=(f'⚠️ 상장폐지 코인 감지: {ticker}\n계좌에 보유 중이나 Upbit에서 거래 불가 상태입니다. 수동 확인 필요.',),
                            kwargs={'level': 'CRITICAL'},
                            daemon=True,
                        ).start()
                except Exception:
                    pass
                continue
            logger.warning('[equity] %s 시세 조회 예외 → avg_buy_price fallback: %s', ticker, _price_exc)
            price = float(avg_cache.get(asset) or 0)
        if price and price > 0:
            total += qty * float(price)

    # 매수 직후 Upbit 정산 딜레이 보정:
    # _balance_cache에 아직 반영 안 된 매수 비용을 equity에 더해 CB 오발동 방지
    now_ts = __import__('time').time()
    stale_assets = []
    for asset, (cost_krw, bought_at) in list(_pending_buy_costs.items()):
        age = now_ts - bought_at
        if age > 300:  # 5분 초과 → 정산 완료로 간주, 제거
            stale_assets.append(asset)
            continue
        qty_in_cache = float(cache.get(asset) or 0)
        if qty_in_cache <= 0:
            # 아직 _balance_cache에 미반영 → 매수 비용만큼 equity 보정
            total += cost_krw
    for a in stale_assets:
        _pending_buy_costs.pop(a, None)

    return float(total or 0.0)


def calculate_dynamic_size(total_equity, current_price, atr, size_pct, is_global_bull_market,
                           ticker=None, fng_value=50):  # fng_value kept for backward compat, unused
    """
    Regime-Dependent Dynamic Position Sizing (Risk Parity + Volatility Targeting).
    FNG 조정은 제거됨 — Panic Dip-Buy는 Pass 2에서 별도 처리.
    반환: (final_buy_krw, risk_pct, sl_distance)
    """
    if current_price is None or current_price <= 0 or total_equity <= 0:
        return 0.0, 0.0, 0.0

    risk_pct = RISK_PCT_BULL if is_global_bull_market else RISK_PCT_BEAR
    risk_amount = total_equity * risk_pct

    try:
        atr_val = float(atr or 0)
    except Exception:
        atr_val = 0.0

    if not (atr_val and atr_val > 0):
        sl_distance = current_price * 0.05
    else:
        sl_distance = atr_val * ATR_SL_MULT

    if sl_distance <= 0:
        return 5000.0, risk_pct, 0.1

    target_quantity = risk_amount / sl_distance
    base_buy_krw = target_quantity * current_price

    # Volatility Targeting: realized vol이 높으면 포지션 축소
    vol_scale = 1.0
    if ticker:
        try:
            from trading_bot.data_manager import compute_realized_vol
            from trading_bot.config import TARGET_VOL_PCT
            realized_vol = compute_realized_vol(ticker)
            if realized_vol > 0:
                vol_scale = min(1.5, max(0.3, TARGET_VOL_PCT / realized_vol))
        except Exception:
            pass
    base_buy_krw *= vol_scale

    # Fear & Greed Index: Greed Penalty 제거 (추세추종 전략에서 탐욕장 매수 억제는 역효과)
    # Extreme Fear 시 Panic Dip-Buy는 strategy.py에서 MTF 바이패스로 처리됨
    # → auto_trader Pass 2에서 'Panic Dip-Buy' 시그널 감지 시 보수적 사이즈로 오버라이드

    max_per_coin = total_equity * MAX_PER_COIN_PCT
    bounded_buy_krw = min(max(base_buy_krw, MIN_ORDER_KRW), max_per_coin)

    try:
        sp = float(size_pct or 0)
    except Exception:
        sp = 0.0
    sp = max(0.0, min(1.0, sp)) or 1.0

    final_buy_krw = bounded_buy_krw * sp
    return float(final_buy_krw), float(risk_pct), float(sl_distance)


def analyze_ticker(ticker, executor, mode, defer_buy=False, is_global_bull_market=True, fng_value=50):
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
    macro_ema_long = int(best_params.get('macro_ema_long', 50))

    try:
        result = generate_comprehensive_signal_with_logging(
            ticker=ticker,
            timeframe=DEFAULT_INTERVAL,
            current_price=current_price,
            account_value=ACCOUNT_VALUE,
            adx_trend_threshold=adx_trend_threshold,
            macro_ema_long=macro_ema_long,
            use_dynamic_risk=True,
            is_global_bull_market=is_global_bull_market,
            position_qty=position_qty or 0.0,
            current_roi=current_roi,
            scale_out_stage=scale_out_stage,
            avg_buy_price=avg_buy_price or 0.0,
            fng_value=fng_value,
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
                        if not log_execution_event(ticker, 'sell', TAG_PS2, current_price):
                            logger.warning('[PS2] %s — 쿨다운 태그 DB 기록 실패: 다음 사이클 중복 실행 위험', ticker)
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
                        if not log_execution_event(ticker, 'sell', TAG_PS1, current_price):
                            logger.warning('[PS1] %s — 쿨다운 태그 DB 기록 실패: 다음 사이클 중복 실행 위험', ticker)
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
                        if not log_execution_event(ticker, 'buy', TAG_DCA_BUY, current_price):
                            logger.warning('[DCA] %s — 쿨다운 태그 DB 기록 실패: 다음 사이클 중복 실행 위험', ticker)
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
                'reason': reason,
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
            fill_price_sell = executor.place_order('sell', current_price, size_pct=sell_size_pct, ticker=ticker) or current_price
            logger.info('✅ %s 매도 신호 실행: 가격 %.0f, 비중 %.0f%%', ticker, fill_price_sell, sell_size_pct * 100)
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
                try:
                    from trading_bot.scale_out_manager import reset_trailing_high
                    reset_trailing_high(ticker)
                except Exception:
                    pass
            _notify(f'🔴 매도 체결: {ticker} @ {fill_price_sell:,.0f}원', level='TRADE')
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


def _try_rotation(executor, tickers: list, new_item: dict) -> bool:
    """
    포지션 만석 시 약세 수익 포지션을 매도하고 강세 신규 신호로 교체.
    성공(매도 완료) 시 True, 조건 불충족 or 실패 시 False.
    """
    try:
        from trading_bot.balanced_plus import (
            ROTATION_ENABLED, ROTATION_MIN_NEW_ADX, ROTATION_ADX_GAP,
            ROTATION_MIN_VICTIM_ROI, TAG_ROTATION_SELL,
            is_in_rotation_cooldown, get_latest_adx, log_execution_event,
        )
        from trading_bot.scale_out_manager import get_scale_out_state, get_trailing_high
    except Exception:
        return False

    if not ROTATION_ENABLED:
        return False

    new_adx = float(new_item.get('adx', 0) or 0)
    if new_adx < ROTATION_MIN_NEW_ADX:
        return False

    # 교체 후보 수집: 수익 중 + scale-out 미진행 + rotation 쿨다운 아님
    candidates = []
    for t in tickers:
        if t == new_item.get('ticker'):
            continue
        qty = executor.get_position_qty(t)
        if qty <= 0:
            continue
        avg = executor.get_avg_buy_price(t) or 0.0
        try:
            import pyupbit
            cur = pyupbit.get_current_price(t)
            cur = float(cur) if cur else avg
        except Exception:
            cur = avg
        if avg <= 0 or cur <= 0:
            continue
        roi = (cur - avg) / avg * 100
        if roi < ROTATION_MIN_VICTIM_ROI:
            continue
        so_stage = get_scale_out_state(t, avg, qty)
        if so_stage > 0:
            continue
        if is_in_rotation_cooldown(t):
            continue
        victim_adx = get_latest_adx(t)
        candidates.append({'ticker': t, 'adx': victim_adx, 'price': cur, 'roi': roi})

    if not candidates:
        return False

    # 가장 ADX 낮은 포지션을 교체 대상으로 선정
    victim = min(candidates, key=lambda x: x['adx'])
    if new_adx - victim['adx'] < ROTATION_ADX_GAP:
        logger.debug(
            '[Rotation] 스킵: 신규 ADX(%.1f) - victim ADX(%.1f) = %.1f < 임계값 %.1f',
            new_adx, victim['adx'], new_adx - victim['adx'], ROTATION_ADX_GAP,
        )
        return False

    # 매도 실행
    victim_ticker = victim['ticker']
    victim_price = victim['price']
    try:
        fill_price = executor.place_order('sell', victim_price, size_pct=1.0, ticker=victim_ticker) or victim_price
        log_execution_event(victim_ticker, 'sell', TAG_ROTATION_SELL, fill_price)
        # scale-out 상태 초기화
        get_scale_out_state(victim_ticker, 0.0, 0.0)
        logger.info(
            '🔄 [Rotation] %s 매도(ADX %.1f, ROI %.1f%%) → %s 매수(ADX %.1f)',
            victim_ticker, victim['adx'], victim['roi'],
            new_item.get('ticker'), new_adx,
        )
        _notify(
            f'🔄 로테이션: {victim_ticker} 매도(ADX {victim["adx"]:.0f}, ROI {victim["roi"]:+.1f}%)'
            f' → {new_item.get("ticker")} 매수(ADX {new_adx:.0f})',
            level='TRADE',
        )
        executor.refresh_balance_cache()
        return True
    except Exception as e:
        logger.warning('[Rotation] %s 매도 실패: %s', victim_ticker, e)
        return False


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
    sync_manual_trades(executor, tickers)
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

    # ----- Circuit Breaker: 일간/전체 DD 초과 시 매수 차단 + 포지션 50% 축소 -----
    circuit_breaker_active = False
    try:
        from trading_bot.risk import check_circuit_breaker, get_system_state, set_system_state
        from trading_bot.balanced_plus import log_execution_event as _cb_log, TAG_CB_SELL
        from datetime import date as _date_cls

        current_equity = compute_total_account_equity(executor, tickers)
        peak_equity = float(get_system_state('peak_equity', '0') or 0)
        daily_start_equity = float(get_system_state('daily_start_equity', '0') or 0)
        last_daily_date = get_system_state('daily_start_date', '')

        today_str = _date_cls.today().isoformat()

        # 일자 변경 시 daily_start 갱신
        if last_daily_date != today_str:
            set_system_state('daily_start_equity', str(current_equity))
            set_system_state('daily_start_date', today_str)
            daily_start_equity = current_equity

        # peak 갱신 (최초 또는 신고점)
        if current_equity > peak_equity:
            set_system_state('peak_equity', str(current_equity))
            peak_equity = current_equity

        # daily_start가 0이면 초기화
        if daily_start_equity <= 0:
            set_system_state('daily_start_equity', str(current_equity))
            daily_start_equity = current_equity

        # ── 단일 사이클 급변 감지: 입출금으로 인한 CB 오발동 방지 ──────────────
        # 1분 사이클 내 20% 이상 equity 변화 → 매매가 아닌 입출금으로 간주 → 기준값 리셋
        _SUDDEN_CHANGE_THRESHOLD = float(os.environ.get('SUDDEN_CHANGE_PCT', '0.20'))
        prev_cycle_equity = float(get_system_state('prev_cycle_equity', '0') or 0)
        if prev_cycle_equity > 0:
            cycle_change_pct = abs(current_equity - prev_cycle_equity) / prev_cycle_equity
            if cycle_change_pct > _SUDDEN_CHANGE_THRESHOLD:
                logger.info(
                    '[CB] 단일 사이클 equity 급변 (%.1f%%) — 입출금 감지, CB 기준값 리셋',
                    cycle_change_pct * 100,
                )
                set_system_state('daily_start_equity', str(current_equity))
                set_system_state('peak_equity', str(current_equity))
                daily_start_equity = current_equity
                peak_equity = current_equity
        set_system_state('prev_cycle_equity', str(current_equity))

        cb_triggered, cb_reason, daily_dd, total_dd = check_circuit_breaker(
            current_equity, peak_equity, daily_start_equity
        )
        if cb_triggered:
            circuit_breaker_active = True
            logger.info('🚨 [CIRCUIT BREAKER] %s (일간DD=%.1f%%, 전체DD=%.1f%%)', cb_reason, daily_dd, total_dd)
            ai_logger.info('[CIRCUIT_BREAKER] %s | daily_dd=%.1f%% | total_dd=%.1f%%', cb_reason, daily_dd, total_dd)
            _notify(f'🚨 Circuit Breaker 발동!\n{cb_reason}\n일간DD: {daily_dd:.1f}%\n전체DD: {total_dd:.1f}%', level='CRITICAL')

            # 보유 포지션 50% 강제 축소
            import pyupbit
            for t in tickers:
                qty = executor.get_position_qty(t)
                if qty and qty > 0:
                    cur_p = pyupbit.get_current_price(t)
                    if cur_p and cur_p * qty * 0.5 >= MIN_ORDER_KRW:
                        try:
                            executor.place_order('sell', cur_p, size_pct=0.5, ticker=t)
                            logger.info('🔴 [CB] %s 포지션 50%% 축소 실행 (가격 %.0f)', t, cur_p)
                            ai_logger.info('[EXECUTE] %s | ACTION:SELL | CB_50PCT | price:%.0f', t, cur_p)
                            _cb_log(t, 'sell', TAG_CB_SELL, cur_p)
                            stats['executed'] = stats.get('executed', 0) + 1
                        except Exception as e:
                            logger.warning('[CB] %s 축소 실행 실패: %s', t, e)
            executor.refresh_balance_cache()
        else:
            logger.info('✅ Circuit Breaker 정상 (일간DD=%.1f%%, 전체DD=%.1f%%)', daily_dd, total_dd)
    except Exception as e:
        logger.warning('[CIRCUIT BREAKER] 체크 중 오류 (계속 진행): %s', e)

    # ----- BTC 거시 장세 필터: 1회 조회 후 이번 사이클 매수 허용 여부 결정 -----
    from trading_bot.param_manager import get_best_params as _get_best_params_cycle
    _cycle_params = _get_best_params_cycle()
    _macro_ema_long = int(_cycle_params.get('macro_ema_long', 50))
    # check_btc_global_trend은 ema_short(20) vs ema_long(50) 고정 사용.
    # macro_ema_long은 strategy.py 단일 EMA 필터 전용이므로 여기서 넘기면
    # macro_ema_long < ema_short(20) 시 단기/장기 역전으로 오판 발생.
    is_global_bull_market = check_btc_global_trend(interval='day', count=60)
    if not is_global_bull_market:
        logger.info('🚨 BTC 하락 추세 감지: 이번 사이클은 신규 매수(Buy)를 전면 차단하고 매도(Sell)만 수행합니다.')

    # ----- Fear & Greed Index: 1회 조회 후 이번 사이클 매수 사이징에 반영 -----
    fng_value = 50
    try:
        from trading_bot.sentiment import fetch_fear_greed_index
        fng_data = fetch_fear_greed_index()
        fng_value = fng_data.get('value', 50)
        fng_class = fng_data.get('classification', 'Neutral')
        logger.info('📊 Fear & Greed Index: %d (%s)', fng_value, fng_class)
    except Exception as e:
        logger.debug('[FNG] 조회 실패 (중립값 사용): %s', e)

    # ----- Pass 1: 분석 및 매도 즉시 실행, 매수는 pending_buys에만 수집 -----
    try:
        for ticker in tickers:
            try:
                out = analyze_ticker(ticker, executor, mode, defer_buy=True, is_global_bull_market=is_global_bull_market, fng_value=fng_value)
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
    if circuit_breaker_active and pending_buys:
        logger.info('🚨 [CIRCUIT BREAKER] 매수 %s건 전면 차단 (DD 초과)', len(pending_buys))
        ai_logger.info('[CIRCUIT_BREAKER] 매수 %s건 차단', len(pending_buys))
        pending_buys.clear()
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
        rotation_done_this_cycle = False

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
                if not rotation_done_this_cycle and not circuit_breaker_active:
                    rotated = _try_rotation(executor, tickers, item)
                    if rotated:
                        rotation_done_this_cycle = True
                        remaining_cash = executor.get_available_cash()
                        # 매도 완료 → 아래 매수 로직으로 낙하
                    else:
                        logger.info('[Pass2] MAX_OPEN_POSITIONS(%s) 도달, 로테이션 불가 → 매수 중단', MAX_OPEN_POSITIONS)
                        ai_logger.info('[SKIP] MAX_OPEN_POSITIONS reached, rotation failed')
                        break
                else:
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

            # Panic Dip-Buy: 보수적 포지션 사이즈 오버라이드 (falling knife 리스크)
            is_panic_dip_buy = 'Panic Dip-Buy' in (item.get('reason') or '')
            if is_panic_dip_buy:
                try:
                    from trading_bot.config import PANIC_DIP_BUY_SIZE_PCT
                    panic_size = PANIC_DIP_BUY_SIZE_PCT
                except Exception:
                    panic_size = 0.3
                final_buy_krw = total_equity * panic_size
                risk_pct = panic_size
                sl_distance = (atr * ATR_SL_MULT) if atr and atr > 0 else (price * 0.05)
                logger.info('[Panic Dip-Buy] %s — 보수적 사이즈 적용 (%.0f%% = %.0f원)', ticker, panic_size * 100, final_buy_krw)
                ai_logger.info('[PANIC_DIP_BUY] %s | size=%.0f%% | amt=%.0fKRW', ticker, panic_size * 100, final_buy_krw)
            else:
                # Regime-Dependent Dynamic Position Sizing (+ Volatility Targeting)
                final_buy_krw, risk_pct, sl_distance = calculate_dynamic_size(
                    total_equity=total_equity,
                    current_price=price,
                    atr=atr,
                    size_pct=size_pct,
                    is_global_bull_market=is_global_bull_market,
                    ticker=ticker,
                    fng_value=fng_value,
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
                fill_price_buy = executor.place_order('buy', price, size_pct=effective_pct, ticker=ticker) or price
                logger.info(
                    '✅ %s 매수 실행 (ADX=%.1f): 가격 %.0f, 동적 배분 %.0f원 (RegimeRisk: %.1f%%, size_pct: %.2f)',
                    ticker,
                    item.get('adx', 0),
                    fill_price_buy,
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
                # 매수 비용을 pending에 등록 → Upbit 정산 딜레이 동안 equity 보정
                try:
                    import time as _time
                    asset = ticker.split('-')[1] if '-' in ticker else ticker
                    _pending_buy_costs[asset] = (float(final_buy_krw), _time.time())
                except Exception:
                    pass
                executor.refresh_balance_cache()
                remaining_cash = executor.get_available_cash()
                _notify(f'🟢 매수 체결: {ticker} @ {fill_price_buy:,.0f}원 (비중 {size_pct*100:.1f}%)', level='TRADE')
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

