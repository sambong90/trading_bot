#!/usr/bin/env python3
"""OHLCV 최대 소급 백필 (독립 실행 · 재개 안전).

각 ticker×timeframe에 대해 DB의 현재 가장 오래된 ts에서 더 과거로 pyupbit `to` 페이징
(200봉/호출)하며 ON CONFLICT DO NOTHING으로 적재. 빈 응답이면 해당 tf의 상장 한계로 보고 종료.
재실행 시 DB의 oldest에서 자동 재개(중단복구). 라이브 수집/매매와 독립 프로세스로 동작.

사용:
  python -m trading_bot.tasks.backfill_ohlcv --tickers KRW-BTC --timeframes day,minute60
  python -m trading_bot.tasks.backfill_ohlcv --tickers all --timeframes all --sleep 0.3
  python -m trading_bot.tasks.backfill_ohlcv --tickers KRW-BTC --timeframes minute1 --max-pages 20  # 테스트
  # 중간 공백 채우기(구간 지정): oldest 대신 --to에서 과거로 내려가며 --from에서 멈춤
  python -m trading_bot.tasks.backfill_ohlcv --tickers KRW-NEO --timeframes minute60 --from 2026-03-01 --to 2026-05-27

옵션:
  --tickers     'all' 또는 쉼표구분(KRW-...). 기본 all(거래가능 KRW 전 종목).
  --timeframes  'all' 또는 쉼표구분. 기본 all(cheap→expensive 순).
  --sleep       호출 간 sleep 초. 기본 0.3 (라이브 수집과 합산 rate-limit 여유).
  --max-pages   tf당 최대 페이지(테스트/제한용). 0=무제한(상장까지). 기본 0.
  --from        역수집 하한(YYYY-MM-DD, KST). 페이지 oldest가 이 시각 이하가 되면 종료.
                지정 시 DB oldest보다 최신에 있는 '중간 공백'을 채우는 용도(기본 None=상장한계까지).
  --to          역수집 시작 상한(YYYY-MM-DD, KST). DB oldest 대신 이 시각부터 과거로 페이징
                (기본 None=DB oldest부터). --from과 함께 [from, to] 구간만 채움.
"""
import os
import sys
import time
import argparse
import pathlib
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pyupbit
import requests
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


# Upbit 캔들 엔드포인트 경로 (tf -> (path, unit)). pyupbit와 동일 결과를 raw로 받기 위함.
_UPBIT_PATH = {
    'minute1': ('minutes', 1), 'minute3': ('minutes', 3), 'minute5': ('minutes', 5),
    'minute15': ('minutes', 15), 'minute30': ('minutes', 30), 'minute60': ('minutes', 60),
    'minute240': ('minutes', 240), 'day': ('days', None), 'week': ('weeks', None), 'month': ('months', None),
}
_EMPTY_DF = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
_KST = timezone(timedelta(hours=9))


def _to_df(data):
    """Upbit 캔들 JSON 리스트 → pyupbit와 동일 형식(index=KST naive, OHLCV)."""
    df = pd.DataFrame(data)
    df['time'] = pd.to_datetime(df['candle_date_time_kst'])  # naive KST (pyupbit 인덱스와 동일)
    df = df.rename(columns={'opening_price': 'open', 'high_price': 'high', 'low_price': 'low',
                            'trade_price': 'close', 'candle_acc_trade_volume': 'volume'})
    return df.set_index('time').sort_index()[['open', 'high', 'low', 'close', 'volume']]


def _fetch_with_backoff(ticker, tf, to_str, sleep_s, retries=60):
    """raw Upbit 호출. pyupbit가 429/빈응답을 모두 None으로 줘 '끝'으로 오인하던 문제를
    status code로 해결: 429는 끈질기게 백오프 재시도, 200+빈배열만 진짜 상장 한계로 종료.
    반환: DataFrame(데이터) / 빈DF(상장 한계) / None(재시도 소진=지속 차단, 재개로 이어감)."""
    path = _UPBIT_PATH.get(tf)
    if path is None:  # 미지원 tf는 pyupbit 폴백
        try:
            df = pyupbit.get_ohlcv(ticker, interval=tf, to=to_str, count=200)
            return df if df is not None else _EMPTY_DF
        except Exception:
            return None
    unit, n = path
    url = f"https://api.upbit.com/v1/candles/{unit}" + (f"/{n}" if n else "")
    params = {'market': ticker, 'count': 200}
    if to_str:
        params['to'] = to_str
    delay = max(sleep_s, 0.3)
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
        except Exception:
            time.sleep(min(delay * 2, 10)); delay = min(delay * 1.5, 10); continue
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                return None
            if not data:
                return _EMPTY_DF  # 200+빈배열 = 더 과거 없음(상장 한계)
            try:
                return _to_df(data)
            except Exception:
                return None
        if r.status_code == 429:  # 레이트리밋 → 끈질기게 재시도
            time.sleep(min(delay, 5)); delay = min(delay * 1.2, 5); continue
        time.sleep(min(delay * 2, 10)); delay = min(delay * 1.5, 10); continue  # 5xx 등
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


def backfill_one(ticker, tf, sleep_s, max_pages=0, from_dt=None, to_dt=None):
    """ticker×tf를 DB oldest(또는 to_dt)에서 상장 한계(또는 from_dt)까지 과거로 페이징 적재.
    from_dt/to_dt 미지정 시 기존 동작과 100% 동일(oldest→상장한계).
    (시도행, 페이지수, oldest) 반환."""
    session = get_session()
    try:
        # 시작 상한: --to 지정 시 그 시각부터, 아니면 DB의 현재 oldest에서.
        to = to_dt if to_dt is not None else _oldest_ts(session, ticker, tf)
        # KST 벽시계(naive)로 통일. Upbit `to`는 offset 없으면 UTC로 해석되므로
        # 호출 시 '+09:00'을 붙여 9h 어긋남을 막는다(이게 빠지면 과거로 안 가고 1페이지서 멈춤).
        if to is not None:
            if to.tzinfo is None:
                to = to.replace(tzinfo=_KST)
            to = to.astimezone(_KST).replace(tzinfo=None)
        pages = 0
        total = 0
        while True:
            if max_pages and pages >= max_pages:
                break
            to_str = to.strftime('%Y-%m-%dT%H:%M:%S+09:00') if to is not None else None
            df = _fetch_with_backoff(ticker, tf, to_str, sleep_s)
            if df is None or len(df) == 0:
                break  # 상장 한계(더 과거 없음)
            new_oldest = df.index.min()
            total += _insert(session, ticker, tf, df)
            pages += 1
            # 진행 없음(같은/더 최신만 반환) → 한계 도달로 종료(무한루프 방지)
            if to is not None and new_oldest >= to:
                break
            # 구간 하한 도달 → 종료(중간 공백 채우기: from_dt 이하로는 더 안 내려감)
            if from_dt is not None and new_oldest <= from_dt:
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
    ap.add_argument('--from', dest='from_date', default=None,
                    help='역수집 하한(YYYY-MM-DD, KST). oldest가 이 시각 이하면 종료(중간 공백용).')
    ap.add_argument('--to', dest='to_date', default=None,
                    help='역수집 시작 상한(YYYY-MM-DD, KST). DB oldest 대신 이 시각부터 과거로.')
    args = ap.parse_args()

    from_dt = datetime.strptime(args.from_date, '%Y-%m-%d') if args.from_date else None
    to_dt = datetime.strptime(args.to_date, '%Y-%m-%d') if args.to_date else None

    if args.tickers.strip().lower() == 'all':
        tickers = get_all_krw_tickers_full(use_db_fallback=True)
    else:
        tickers = [t.strip() for t in args.tickers.split(',') if t.strip().startswith('KRW-')]
    tfs = TF_ALL if args.timeframes.strip().lower() == 'all' else \
        [x.strip() for x in args.timeframes.split(',') if x.strip()]

    rng = ''
    if from_dt is not None or to_dt is not None:
        rng = f", range=[{args.from_date or '상장'}..{args.to_date or 'oldest'}]"
    _log(f"start: {len(tickers)} tickers × {len(tfs)} tf, sleep={args.sleep}s, max_pages={args.max_pages or '∞'}{rng}")
    grand = 0
    for i, tk in enumerate(tickers, 1):
        for tf in tfs:
            try:
                n, p, oldest = backfill_one(tk, tf, args.sleep, args.max_pages, from_dt=from_dt, to_dt=to_dt)
                grand += n
                _log(f"[{i}/{len(tickers)}] {tk} {tf}: +{n} rows, {p} pages, oldest={str(oldest)[:19]} (누적 {grand})")
            except Exception as e:
                _log(f"[{i}/{len(tickers)}] {tk} {tf}: ERROR {e}")
            time.sleep(args.sleep)
    _log(f"DONE: 총 시도행 {grand}")


if __name__ == '__main__':
    main()
