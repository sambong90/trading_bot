import pyupbit
import pandas as pd
from trading_bot.tasks.state_updater import update_phase
from trading_bot.db import get_session
from trading_bot.models import OHLCV


def fetch_ohlcv(ticker='KRW-BTC', interval='minute60', count=200, retry=3, backoff=1):
    """Fetch OHLCV from Upbit and return DataFrame with columns time, open, high, low, close, volume
    Implements simple retry with exponential backoff and cache fallback (trading_bot/logs/cache).
    """
    import time, os, json
    cache_dir = os.path.join('trading_bot','logs','cache')
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{ticker}_{interval}_{count}.json")

    update_phase('A - 데이터 수집', status='in_progress', percent=10, recent_actions=[f'fetch start {ticker} {interval}'], next_steps=['데이터 정합성 검사'])
    df = None
    last_exc = None
    for attempt in range(1, retry+1):
        try:
            df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
            if df is not None and len(df):
                break
            last_exc = RuntimeError('No data returned from pyupbit')
        except Exception as e:
            last_exc = e
        time.sleep(backoff * (2 ** (attempt-1)))

    if df is None or len(df)==0:
        # increment failure counter
        try:
            fc_path = os.path.join('trading_bot','logs','fail_count.json')
            fc = {'count':0}
            if os.path.exists(fc_path):
                try:
                    with open(fc_path,'r',encoding='utf-8') as f:
                        fc = json.load(f)
                except Exception:
                    fc = {'count':0}
            fc['count'] = fc.get('count',0) + 1
            with open(fc_path,'w',encoding='utf-8') as f:
                json.dump(fc,f)
            # if >=3, send telegram alert (best-effort)
            if fc['count'] >= 3:
                try:
                    from trading_bot.monitor import send_telegram
                    send_telegram(f"[Alert] pyupbit returned no data {fc['count']} times in a row on host")
                except Exception:
                    pass
        except Exception:
            pass

        # try cache fallback (only accept cache younger than 12 hours)
        if os.path.exists(cache_file):
            try:
                with open(cache_file,'r',encoding='utf-8') as f:
                    cached = json.load(f)
                # build DataFrame
                df = pd.DataFrame(cached)
                # convert time if needed
                if 'time' in df.columns:
                    df['time'] = pd.to_datetime(df['time'])
                # check cache freshness
                try:
                    max_ts = df['time'].max()
                    if pd.Timestamp.now(tz=max_ts.tz) - max_ts > pd.Timedelta(hours=12):
                        raise RuntimeError('cache too old')
                except Exception:
                    update_phase('A - 데이터 수집', status='failed', percent=0, issues=['pyupbit returned no data', 'cache too old'])
                    raise last_exc if last_exc else RuntimeError('No data and cache too old')
                update_phase('A - 데이터 수집', status='done', percent=50, recent_actions=[f'fetch used cache {cache_file}'])
            except Exception:
                update_phase('A - 데이터 수집', status='failed', percent=0, issues=['pyupbit returned no data', 'cache read failed'])
                raise last_exc if last_exc else RuntimeError('No data and cache read failed')
        else:
            update_phase('A - 데이터 수집', status='failed', percent=0, issues=['pyupbit returned no data'])
            raise last_exc if last_exc else RuntimeError('No data returned from pyupbit')
    else:
        # save cache
        try:
            out = df.reset_index().rename(columns={'index':'time'})
            # serialize time as ISO
            out['time'] = out['time'].astype(str)
            with open(cache_file,'w',encoding='utf-8') as f:
                json.dump(out.to_dict(orient='records'), f, ensure_ascii=False)
        except Exception:
            pass

    df = df.reset_index().rename(columns={'index':'time'})
    # ensure timezone-aware (Upbit returns KST index)
    if not pd.api.types.is_datetime64_any_dtype(df['time']):
        df['time'] = pd.to_datetime(df['time'])
    try:
        df['time'] = df['time'].dt.tz_localize('Asia/Seoul')
    except Exception:
        try:
            df['time'] = df['time'].dt.tz_convert('Asia/Seoul')
        except Exception:
            pass
    update_phase('A - 데이터 수집', status='done', percent=100, recent_actions=[f'fetch complete rows={len(df)}'])
    # write to DB (upsert-like: try insert, ignore on conflict for sqlite simple approach)
    # Try DB-specific upsert when possible to avoid IntegrityError noise.
    try:
        session = get_session()
        engine = session.get_bind()
        dialect = engine.dialect.name
        # If sqlite, use INSERT OR IGNORE for performance
        if dialect == 'sqlite':
            conn = engine.connect()
            for _, row in df.iterrows():
                ins = f"INSERT OR IGNORE INTO ohlcv (ticker, timeframe, ts, open, high, low, close, volume, source) VALUES (:ticker, :timeframe, :ts, :open, :high, :low, :close, :volume, :source)"
                params = {
                    'ticker': ticker,
                    'timeframe': interval,
                    'ts': row['time'].to_pydatetime(),
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': float(row['volume']),
                    'source': 'upbit'
                }
                try:
                    conn.execute(ins, params)
                except Exception:
                    continue
            conn.close()
        else:
            # generic fallback: attempt insert with flush and ignore duplicates
            from sqlalchemy.exc import IntegrityError
            for _, row in df.iterrows():
                o = OHLCV(ticker=ticker, timeframe=interval, ts=row['time'].to_pydatetime(), open=float(row['open']), high=float(row['high']), low=float(row['low']), close=float(row['close']), volume=float(row['volume']), source='upbit')
                session.add(o)
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    continue
        session.commit()
    except Exception as e:
        print('db write failed', e)
    finally:
        try:
            session.close()
        except Exception:
            pass
    return df


def save_csv(df, path):
    df.to_csv(path, index=False)
