#!/usr/bin/env python3
"""OHLCV 최대 소급 백필 (독립 실행 · 재개 안전).

각 ticker×timeframe에 대해 DB의 현재 가장 오래된 ts에서 더 과거로 pyupbit `to` 페이징
(200봉/호출)하며 ON CONFLICT DO NOTHING으로 적재. 빈 응답이면 해당 tf의 상장 한계로 보고 종료.
재실행 시 DB의 oldest에서 자동 재개(중단복구). 라이브 수집/매매와 독립 프로세스로 동작.

사용:
  python -m trading_bot.tasks.backfill_ohlcv --tickers KRW-BTC --timeframes day,minute60
  python -m trading_bot.tasks.backfill_ohlcv --tickers all --timeframes all --sleep 0.3
  python -m trading_bot.tasks.backfill_ohlcv --tickers KRW-BTC --timeframes minute1 --max-pages 20  # 테스트

옵션:
  --tickers     'all' 또는 쉼표구분(KRW-...). 기본 all(거래가능 KRW 전 종목).
  --timeframes  'all' 또는 쉼표구분. 기본 all(cheap→expensive 순).
  --sleep       호출 간 sleep 초. 기본 0.3 (라이브 수집과 합산 rate-limit 여유).
  --max-pages   tf당 최대 페이지(테스트/제한용). 0=무제한(상장까지). 기본 0.
"""
import os
import sys
import time
import argparse
import pathlib
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pyupbit
import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from trading_bot.db import get_session
from trading_bot.models import OHLCV
from trading_bot.data import get_all_krw_tickers_full

# 저렴(소수 호출)→고비용(다수 호출) 순. 1분봉을 마지막에 둬 조기 가치 확보.
TF_ALL = ['month', 'week', 'day', 'minute240', 'minute60', 'minute30', 'minute15', 'minute1']


def _log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [backfill] {msg}", flush=True)


def _oldest_ts(session, ticker, tf):
    return session.query(func.min(OHLCV.ts)).filter(
        OHLCV.ticker == ticker, OHLCV.timeframe == tf
    ).scalar()


def _fetch_with_backoff(ticker, tf, to_str, sleep_s, retries=5):
    """pyupbit 호출 + 429/오류 지수 백오프. 실패 시 None."""
    delay = max(sleep_s, 0.2)
    for _ in range(retries):
        try:
            return pyupbit.get_ohlcv(ticker, interval=tf, to=to_str, count=200)
        except Exception as e:
            s = str(e).lower()
            if '429' in s or 'too many' in s or 'rate' in s:
                time.sleep(delay * 4)
                delay *= 2
            else:
                time.sleep(delay * 2)
    return None


def _insert(session, ticker, tf, df):
    """tz(KST) 부여 후 배치 upsert(중복 무시). 삽입 시도 행수 반환."""
    if df is None or len(df) == 0:
        return 0
    idx = df.index
    try:
        idx = idx.tz_localize('Asia/Seoul')
    except (TypeError, AttributeError):
        try:
            idx = idx.tz_convert('Asia/Seoul')
        except Exception:
            pass
    recs = []
    for ts, row in zip(idx, df.itertuples(index=False)):
        recs.append({
            'ticker': ticker, 'timeframe': tf, 'ts': ts.to_pydatetime(),
            'open': float(row.open), 'high': float(row.high), 'low': float(row.low),
            'close': float(row.close), 'volume': float(row.volume), 'source': 'upbit-backfill',
        })
    stmt = pg_insert(OHLCV).values(recs).on_conflict_do_nothing(
        index_elements=['ticker', 'timeframe', 'ts'])
    session.execute(stmt)
    session.commit()
    return len(recs)


def backfill_one(ticker, tf, sleep_s, max_pages=0):
    """ticker×tf를 DB oldest에서 상장 한계까지 과거로 페이징 적재. (시도행, 페이지수, oldest)"""
    session = get_session()
    try:
        to = _oldest_ts(session, ticker, tf)   # None이면 최신부터 시작
        # DB는 tz-aware(Asia/Seoul), pyupbit는 naive KST → naive(KST 벽시계)로 통일해 비교/포맷.
        if to is not None:
            to = to.replace(tzinfo=None)
        pages = 0
        total = 0
        while True:
            if max_pages and pages >= max_pages:
                break
            to_str = to.strftime('%Y-%m-%d %H:%M:%S') if to is not None else None
            df = _fetch_with_backoff(ticker, tf, to_str, sleep_s)
            if df is None or len(df) == 0:
                break  # 상장 한계(더 과거 없음)
            new_oldest = df.index.min()
            total += _insert(session, ticker, tf, df)
            pages += 1
            # 진행 없음(같은/더 최신만 반환) → 한계 도달로 종료(무한루프 방지)
            if to is not None and new_oldest >= to:
                break
            to = new_oldest
            time.sleep(sleep_s)
        final_oldest = _oldest_ts(session, ticker, tf)
        return total, pages, final_oldest
    finally:
        session.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', default='all')
    ap.add_argument('--timeframes', default='all')
    ap.add_argument('--sleep', type=float, default=0.3)
    ap.add_argument('--max-pages', type=int, default=0)
    args = ap.parse_args()

    if args.tickers.strip().lower() == 'all':
        tickers = get_all_krw_tickers_full(use_db_fallback=True)
    else:
        tickers = [t.strip() for t in args.tickers.split(',') if t.strip().startswith('KRW-')]
    tfs = TF_ALL if args.timeframes.strip().lower() == 'all' else \
        [x.strip() for x in args.timeframes.split(',') if x.strip()]

    _log(f"start: {len(tickers)} tickers × {len(tfs)} tf, sleep={args.sleep}s, max_pages={args.max_pages or '∞'}")
    grand = 0
    for i, tk in enumerate(tickers, 1):
        for tf in tfs:
            try:
                n, p, oldest = backfill_one(tk, tf, args.sleep, args.max_pages)
                grand += n
                _log(f"[{i}/{len(tickers)}] {tk} {tf}: +{n} rows, {p} pages, oldest={str(oldest)[:19]} (누적 {grand})")
            except Exception as e:
                _log(f"[{i}/{len(tickers)}] {tk} {tf}: ERROR {e}")
            time.sleep(args.sleep)
    _log(f"DONE: 총 시도행 {grand}")


if __name__ == '__main__':
    main()
