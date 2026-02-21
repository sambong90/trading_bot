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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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

DEFAULT_INTERVAL = 'minute60'
DEFAULT_COUNT = 200
ACCOUNT_VALUE = float(os.environ.get('ACCOUNT_VALUE', '100000'))
# 업비트 최소 주문 금액(원). 이 금액 미만 보유 시 매도 신호 무시(무의미한 반복 로깅 방지)
MIN_ORDER_KRW = 5000


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


def analyze_ticker(ticker, executor, mode, defer_buy=False):
    """
    티커 1개 분석 후 신호 처리.
    defer_buy=True(투패스 모드)일 때: sell은 즉시 실행, buy는 실행하지 않고 ('pending_buy', reason, data) 반환.
    반환: (status, reason, data). data는 status=='pending_buy'일 때만 채워짐.
    """
    from trading_bot.data import fetch_ohlcv
    from trading_bot.data_manager import sync_indicators_for_ticker
    from trading_bot.strategy import generate_comprehensive_signal_with_logging

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

    try:
        result = generate_comprehensive_signal_with_logging(
            ticker=ticker,
            timeframe=DEFAULT_INTERVAL,
            current_price=current_price,
            account_value=ACCOUNT_VALUE,
            use_dynamic_risk=True,
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
        size_pct = max(0.01, min(1.0, position_size / ACCOUNT_VALUE)) if ACCOUNT_VALUE > 0 else 0.02
        if defer_buy:
            estimated_spend = ACCOUNT_VALUE * size_pct if current_price else 0
            return 'pending_buy', reason, {
                'ticker': ticker,
                'adx': adx,
                'price': current_price,
                'position_size': position_size,
                'size_pct': size_pct,
                'estimated_spend': estimated_spend,
            }
        try:
            executor.place_order('buy', current_price, size_pct=size_pct, ticker=ticker)
            logger.info('✅ %s 매수 신호 실행: 가격 %.0f, 비중 %.2f%%', ticker, current_price, size_pct * 100)
            return 'executed', None, None
        except Exception as e:
            logger.warning('[오류] %s — 매수 주문 실행 실패: %s', ticker, str(e))
            return 'error', str(e), None

    if signal == 'sell':
        position_qty = executor.get_position_qty(ticker)
        if position_qty <= 0:
            logger.info('[매도 스킵] %s — 보유 잔고 없음 (매도 스킵)', ticker)
            return 'hold', '보유 잔고 없음 (매도 스킵)', None
        position_value = (current_price or 0) * position_qty
        if position_value < MIN_ORDER_KRW:
            logger.info('[매도 스킵] %s — 보유 금액 %.0f원 < 최소 주문 %s원 (매도 스킵)', ticker, position_value, MIN_ORDER_KRW)
            return 'hold', '보유 잔고 없음 (매도 스킵)', None
        try:
            executor.place_order('sell', current_price, size_pct=1.0, ticker=ticker)
            logger.info('✅ %s 매도 신호 실행: 가격 %.0f', ticker, current_price)
            return 'executed', None, None
        except Exception as e:
            logger.warning('[오류] %s — 매도 주문 실행 실패: %s', ticker, str(e))
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

    start = datetime.now()
    stats = {'executed': 0, 'hold': 0, 'skip': 0, 'error': 0, 'pending_buy': 0}
    skip_reasons = []
    pending_buys = []  # Pass 1에서 수집한 매수 후보 (Pass 2에서 ADX 순 정렬 후 실행)
    cycle_error = None

    # ----- Pass 1: 분석 및 매도 즉시 실행, 매수는 pending_buys에만 수집 -----
    try:
        for ticker in tickers:
            try:
                out = analyze_ticker(ticker, executor, mode, defer_buy=True)
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

    # ----- Pass 2: 매수 우선순위(ADX 내림차순) 정렬 후 가용 현금 한도 내 순차 매수 -----
    # (1) 매도 완료 후 갱신된 가용 현금 조회 (2) pending_buys를 ADX 내림차순 정렬 (3) 정렬된 순으로
    # estimated_spend <= remaining_cash 일 때만 place_order('buy') 호출, 실행 후 remaining_cash에서 차감
    if not cycle_error and pending_buys:
        executor.refresh_balance_cache()
        remaining_cash = executor.get_available_cash()

        # 정렬: ADX가 높을수록 추세가 강하므로 우선 매수 (내림차순)
        pending_buys_sorted = sorted(pending_buys, key=lambda x: float(x.get('adx', 0) or 0), reverse=True)

        logger.info('')
        logger.info('📋 매수 대기 큐 정렬 결과 (ADX 추세 강도 높은 순, %s건):', len(pending_buys_sorted))
        for i, item in enumerate(pending_buys_sorted[:20], 1):
            logger.info('  %s. %s — ADX=%.1f, 가격=%.0f, 예상비용=%.0f원', i, item['ticker'], item.get('adx', 0), item.get('price') or 0, item.get('estimated_spend', 0))
        if len(pending_buys_sorted) > 20:
            logger.info('  ... 외 %s건', len(pending_buys_sorted) - 20)

        # 가용 현금 한도 내에서만 순차 매수; 부족 시 해당 티커는 스킵
        for item in pending_buys_sorted:
            ticker = item['ticker']
            estimated_spend = float(item.get('estimated_spend', 0) or 0)
            price = item.get('price')
            size_pct = item.get('size_pct', 0.02)
            if estimated_spend <= 0 or price is None:
                continue
            # 업비트 최소 주문 금액(5,000원) 미만 시 API 거절 방지 — 스킵 후 continue
            if estimated_spend < MIN_ORDER_KRW:
                logger.info('[매수 스킵] %s — 최소 주문 금액(%s원) 미달로 매수 스킵 (예상비용 %.0f원)', ticker, MIN_ORDER_KRW, estimated_spend)
                continue
            if remaining_cash < estimated_spend:
                logger.info('[매수 스킵] %s — 현금 부족으로 매수 스킵 (가용 %.0f원 < 필요 %.0f원, ADX=%.1f)', ticker, remaining_cash, estimated_spend, item.get('adx', 0))
                continue
            try:
                executor.place_order('buy', price, size_pct=size_pct, ticker=ticker)
                logger.info('✅ %s 매수 실행 (ADX=%.1f): 가격 %.0f, 비중 %.2f%%', ticker, item.get('adx', 0), price, size_pct * 100)
                stats['executed'] = stats.get('executed', 0) + 1
                remaining_cash -= estimated_spend
            except Exception as e:
                logger.warning('[오류] %s — 매수 주문 실행 실패: %s', ticker, str(e))
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


def main():
    args = parse_args()
    mode = args.mode or os.environ.get('TRADING_MODE', 'paper')
    run_cycle(mode)


if __name__ == '__main__':
    main()
