"""H002 깊은이격 평균회귀 — 페이퍼 검증 (연구 모드, 실주문 절대 없음).

quant-research reports/H002_MEANREVERSION.md 백테스트 엣지(1h, PF 1.27~1.46)를
실시간 가상체결로 검증한다. 백테스트가 못 본 3가지를 측정:
  ① 슬리피지 — 급락 저유동 코인 실호가(진입 직후 라이브 호가창 walk)
  ② 생존편향 — 진입 코인의 반등/추가급락/상폐 추적
  ③ OOS>IS — 실시간 표본의 가상 PF

신호(백테스트 통과분만): close<ma20-5% | close<ma50-10% | 12봉<-15%(급락).
청산(MR 전용, 추세 트레일 재탕 금지): STOP -> MA20복귀 TP -> TIME(max_hold).
얕은 과매도(RSI/BB) 단독 진입은 무엣지 확정 -> 사용 안 함.

기록만: PaperMrPosition 테이블 + PAPER_SIGNAL ai_event. 실주문/잔고변경 없음.
스케줄러에서 1h봉 마감 후 호출. 어떤 예외도 스케줄러 코어로 전파시키지 않는다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from trading_bot.config import (
    PAPER_MR_ENABLED, PAPER_MR_TIMEFRAME, PAPER_MR_MA20_DEV, PAPER_MR_MA50_DEV,
    PAPER_MR_CRASH_PCT, PAPER_MR_CRASH_LOOKBACK, PAPER_MR_STOP_PCT, PAPER_MR_MAX_HOLD,
    PAPER_MR_FEE_PCT, PAPER_MR_SLIP_PCT, PAPER_MR_ORDER_KRW, PAPER_MR_COOLDOWN_BARS,
)

logger = logging.getLogger('paper_mr')

_FEE = PAPER_MR_FEE_PCT / 100.0
_SLIP = PAPER_MR_SLIP_PCT / 100.0


# ── 지표 (백테스트 add_mr_indicators와 동일 정의) ───────────────────────────────
def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    roll_down = down.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df['close']
    df['ma20'] = c.rolling(20, min_periods=20).mean()
    df['ma50'] = c.rolling(50, min_periods=50).mean()
    df['rsi'] = _wilder_rsi(c, 14)
    df['ret12'] = c / c.shift(PAPER_MR_CRASH_LOOKBACK) - 1.0
    return df


def _signal_for_row(row) -> str | None:
    """최신 마감봉이 트리거하는 신호 (깊은 순 우선). 없으면 None."""
    close = row['close']
    ma20 = row.get('ma20')
    ma50 = row.get('ma50')
    ret12 = row.get('ret12')
    if ret12 is not None and np.isfinite(ret12) and ret12 < -PAPER_MR_CRASH_PCT / 100.0:
        return 'crash'
    if ma50 is not None and np.isfinite(ma50) and close < ma50 * (1.0 - PAPER_MR_MA50_DEV / 100.0):
        return 'ma50_dev'
    if ma20 is not None and np.isfinite(ma20) and close < ma20 * (1.0 - PAPER_MR_MA20_DEV / 100.0):
        return 'ma20_dev'
    return None


# ── BTC 일봉 추세 국면 (백테스트 regime: EMA5/20) ──────────────────────────────
def _btc_regime() -> str:
    try:
        from trading_bot.data import fetch_ohlcv
        d = fetch_ohlcv(ticker='KRW-BTC', interval='day', count=60, use_db_first=True)
        if d is None or len(d) < 21:
            return 'unknown'
        c = d['close']
        ema5 = c.ewm(span=5, adjust=False).mean().iloc[-1]
        ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
        diff = (ema5 - ema20) / ema20
        if diff > 0.005:
            return 'bull'
        if diff < -0.005:
            return 'bear'
        return 'sideways'
    except Exception as e:
        logger.warning('[paper_mr] BTC regime 계산 실패: %s', e)
        return 'unknown'


# ── 호가창 스냅샷 (슬리피지 실측) ───────────────────────────────────────────────
def _orderbook_snapshot(ticker: str, order_krw: float) -> dict | None:
    """진입 직후 라이브 호가. ask-walk로 order_krw 시장가 매수 체결추정가 산출."""
    try:
        import pyupbit
        ob = pyupbit.get_orderbook(ticker)
    except Exception as e:
        logger.warning('[paper_mr] %s 호가 조회 실패: %s', ticker, e)
        return None
    try:
        if isinstance(ob, list):
            ob = ob[0] if ob else None
        if not ob:
            return None
        units = ob.get('orderbook_units') or []
        if not units:
            return None
        ask1 = float(units[0]['ask_price'])
        bid1 = float(units[0]['bid_price'])
        if ask1 <= 0 or bid1 <= 0:
            return None
        mid = (ask1 + bid1) / 2.0
        spread_pct = (ask1 - bid1) / mid * 100.0
        ask_depth_krw = sum(float(u['ask_price']) * float(u['ask_size']) for u in units)
        # ask-walk: order_krw를 매도호가 위로 채우며 평균 체결가
        remaining, cost, qty = order_krw, 0.0, 0.0
        for u in units:
            lvl_krw = float(u['ask_price']) * float(u['ask_size'])
            take = min(remaining, lvl_krw)
            if take <= 0:
                break
            qty += take / float(u['ask_price'])
            cost += take
            remaining -= take
            if remaining <= 0:
                break
        fill = (cost / qty) if qty > 0 else ask1
        return {
            'ask1': ask1, 'bid1': bid1, 'spread_pct': spread_pct,
            'ask_depth_krw': ask_depth_krw, 'fill': fill,
            'partial': remaining > 0,  # 호가창이 주문크기를 못 채움(저유동)
        }
    except Exception as e:
        logger.warning('[paper_mr] %s 호가 파싱 실패: %s', ticker, e)
        return None


def _roi(entry_px: float, exit_px: float, entry_slip: float, exit_slip: float) -> float:
    buy = entry_px * (1.0 + entry_slip) * (1.0 + _FEE)
    sell = exit_px * (1.0 - exit_slip) * (1.0 - _FEE)
    return (sell / buy - 1.0) * 100.0


# ── 가상 청산 (OPEN 포지션 전진 평가 — simulate_mr와 동일 순서) ──────────────────
def _resolve_open(session) -> int:
    from trading_bot.models import PaperMrPosition
    from trading_bot.data import fetch_ohlcv
    opens = session.query(PaperMrPosition).filter(PaperMrPosition.status == 'OPEN').all()
    closed = 0
    fetch_n = PAPER_MR_MAX_HOLD + 80
    for pos in opens:
        try:
            df = fetch_ohlcv(ticker=pos.ticker, interval=PAPER_MR_TIMEFRAME,
                             count=fetch_n, use_db_first=True)
            if df is None or len(df) < 21:
                continue
            df = _add_indicators(df)
            if 'time' not in df.columns:
                df = df.reset_index().rename(columns={'index': 'time'})
            df['time'] = pd.to_datetime(df['time'], utc=True)
            entry_ts = pd.Timestamp(pos.entry_ts)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize('UTC')
            fwd = df[df['time'] > entry_ts].reset_index(drop=True)
            if len(fwd) == 0:
                continue
            stop = pos.entry_close * (1.0 + PAPER_MR_STOP_PCT / 100.0)
            exit_px = exit_reason = exit_ts = None
            hold = 0
            for k in range(len(fwd)):
                if k >= PAPER_MR_MAX_HOLD:
                    break
                bar = fwd.iloc[k]
                hold = k + 1
                if float(bar['low']) <= stop:
                    exit_px, exit_reason, exit_ts = stop, 'STOP', bar['time']
                    break
                tgt = bar['ma20']
                if tgt is not None and np.isfinite(tgt) and float(bar['close']) >= float(tgt):
                    exit_px, exit_reason, exit_ts = float(bar['close']), 'TP', bar['time']
                    break
            if exit_reason is None:
                # max_hold 도달 시 TIME 청산, 아직 미도달이면 OPEN 유지
                if len(fwd) >= PAPER_MR_MAX_HOLD:
                    bar = fwd.iloc[PAPER_MR_MAX_HOLD - 1]
                    exit_px, exit_reason, exit_ts, hold = float(bar['close']), 'TIME', bar['time'], PAPER_MR_MAX_HOLD
                else:
                    continue  # 아직 진행 중
            pos.status = 'CLOSED'
            pos.exit_reason = exit_reason
            pos.exit_close = float(exit_px)
            pos.exit_ts = pd.Timestamp(exit_ts).to_pydatetime()
            pos.hold_bars = int(hold)
            pos.roi_signal_pct = round(_roi(pos.entry_close, exit_px, _SLIP, _SLIP), 4)
            if pos.entry_fill and pos.entry_fill > 0:
                # 실측 진입체결(슬리피지 내재) -> 진입 slip=0(이미 반영), exit는 가정치
                pos.roi_realistic_pct = round(_roi(pos.entry_fill, exit_px, 0.0, _SLIP), 4)
            session.commit()
            closed += 1
            logger.info('[paper_mr] CLOSE %s %s hold=%d roi_sig=%.2f%% roi_real=%s',
                        pos.ticker, exit_reason, hold, pos.roi_signal_pct,
                        f'{pos.roi_realistic_pct:.2f}%' if pos.roi_realistic_pct is not None else 'na')
        except Exception as e:
            session.rollback()
            logger.warning('[paper_mr] %s 청산 평가 실패: %s', pos.ticker, e)
    return closed


def _has_block(session, ticker: str, now_utc: datetime) -> bool:
    """중복 진입 차단: 동일 티커 OPEN 존재 또는 쿨다운 이내 청산."""
    from trading_bot.models import PaperMrPosition
    if session.query(PaperMrPosition).filter(
            PaperMrPosition.ticker == ticker, PaperMrPosition.status == 'OPEN').first():
        return True
    if PAPER_MR_COOLDOWN_BARS > 0:
        cutoff = now_utc - timedelta(hours=PAPER_MR_COOLDOWN_BARS)
        recent = session.query(PaperMrPosition).filter(
            PaperMrPosition.ticker == ticker,
            PaperMrPosition.status == 'CLOSED',
            PaperMrPosition.exit_ts >= cutoff).first()
        if recent:
            return True
    return False


# ── 신호 스캔 (신규 가상 진입) ──────────────────────────────────────────────────
def _scan_entries(session, regime: str) -> int:
    from trading_bot.models import PaperMrPosition
    from trading_bot.data import get_all_krw_tickers_full, fetch_ohlcv
    try:
        tickers = get_all_krw_tickers_full()
    except Exception as e:
        logger.warning('[paper_mr] 티커 목록 조회 실패: %s', e)
        return 0
    now_utc = datetime.now(timezone.utc)
    opened = 0
    for ticker in tickers:
        try:
            if _has_block(session, ticker, now_utc):
                continue
            df = fetch_ohlcv(ticker=ticker, interval=PAPER_MR_TIMEFRAME, count=80, use_db_first=True)
            if df is None or len(df) < 51:
                continue
            df = _add_indicators(df)
            if 'time' not in df.columns:
                df = df.reset_index().rename(columns={'index': 'time'})
            row = df.iloc[-1]
            sig = _signal_for_row(row)
            if sig is None:
                continue
            entry_close = float(row['close'])
            if not np.isfinite(entry_close) or entry_close <= 0:
                continue
            entry_ts = pd.to_datetime(row['time'], utc=True).to_pydatetime()
            ma20 = float(row['ma20']) if np.isfinite(row['ma20']) else None
            ma50 = float(row['ma50']) if np.isfinite(row['ma50']) else None
            rsi = float(row['rsi']) if np.isfinite(row['rsi']) else None
            ret12 = float(row['ret12']) if np.isfinite(row['ret12']) else None
            dev20 = (entry_close / ma20 - 1.0) * 100.0 if ma20 else None

            ob = _orderbook_snapshot(ticker, PAPER_MR_ORDER_KRW)
            entry_fill = slippage_pct = ask1 = bid1 = spread = depth = None
            note = None
            if ob:
                ask1, bid1 = ob['ask1'], ob['bid1']
                spread, depth = ob['spread_pct'], ob['ask_depth_krw']
                entry_fill = ob['fill']
                slippage_pct = (entry_fill / entry_close - 1.0) * 100.0
                if ob['partial']:
                    note = '호가창이 가상주문 크기 미충족(저유동)'
            else:
                note = '진입시 호가 스냅샷 실패'

            pos = PaperMrPosition(
                ticker=ticker, entry_ts=entry_ts, entry_signal=sig,
                timeframe=PAPER_MR_TIMEFRAME, entry_close=entry_close,
                rsi=round(rsi, 1) if rsi is not None else None,
                dev20_pct=round(dev20, 2) if dev20 is not None else None,
                ma20=ma20, ma50=ma50,
                ret12_pct=round(ret12 * 100.0, 2) if ret12 is not None else None,
                regime=regime,
                entry_ask1=ask1, entry_bid1=bid1,
                spread_pct=round(spread, 4) if spread is not None else None,
                ob_ask_depth_krw=round(depth, 0) if depth is not None else None,
                entry_fill=entry_fill,
                slippage_entry_pct=round(slippage_pct, 4) if slippage_pct is not None else None,
                status='OPEN', note=note,
            )
            session.add(pos)
            session.commit()
            opened += 1
            logger.info('[paper_mr] OPEN %s %s close=%.4g dev20=%s slip=%s%% spread=%s%%',
                        ticker, sig, entry_close,
                        f'{dev20:.1f}' if dev20 is not None else 'na',
                        f'{slippage_pct:.3f}' if slippage_pct is not None else 'na',
                        f'{spread:.3f}' if spread is not None else 'na')
            # PAPER_SIGNAL 기록 (기존 연구모드 인프라 확장)
            try:
                from trading_bot.ai_logger import log_ai_event
                log_ai_event(
                    event_type='PAPER_SIGNAL', ticker=ticker, signal='buy',
                    price=entry_close, rsi=rsi, regime=regime, timeframe=PAPER_MR_TIMEFRAME,
                    decision_reason=f'H002_MR:{sig}',
                    extra={'strategy': 'H002_MR', 'mode': 'RESEARCH', 'mr_signal': sig,
                           'dev20_pct': round(dev20, 2) if dev20 is not None else None,
                           'ret12_pct': round(ret12 * 100.0, 2) if ret12 is not None else None,
                           'slippage_entry_pct': round(slippage_pct, 4) if slippage_pct is not None else None,
                           'spread_pct': round(spread, 4) if spread is not None else None},
                )
            except Exception as e:
                logger.warning('[paper_mr] %s PAPER_SIGNAL 기록 실패: %s', ticker, e)
        except Exception as e:
            session.rollback()
            logger.warning('[paper_mr] %s 진입 스캔 실패: %s', ticker, e)
    return opened


def run_scan() -> None:
    """스케줄러 진입점. 1h봉 마감 후 호출. 절대 예외를 밖으로 던지지 않는다."""
    if not PAPER_MR_ENABLED:
        return
    try:
        from trading_bot.db import get_session, ensure_tables
        ensure_tables()  # paper_mr_positions 테이블 보장 (멱등, create_all checkfirst)
        session = get_session()
        try:
            closed = _resolve_open(session)
            regime = _btc_regime()
            opened = _scan_entries(session, regime)
            logger.info('[paper_mr] 스캔 완료 — 신규진입 %d, 청산 %d, regime=%s',
                        opened, closed, regime)
        finally:
            session.close()
    except Exception as e:
        logger.warning('[paper_mr] run_scan 실패(스케줄러 영향 없음): %s', e)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run_scan()
