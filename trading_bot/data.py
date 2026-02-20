import pyupbit
import pandas as pd
from trading_bot.tasks.state_updater import update_phase
from trading_bot.db import get_session
from trading_bot.models import OHLCV


def fetch_ohlcv(ticker='KRW-BTC', interval='minute60', count=200):
    """Fetch OHLCV from Upbit and return DataFrame with columns time, open, high, low, close, volume"""
    update_phase('A - 데이터 수집', status='in_progress', percent=10, recent_actions=[f'fetch start {ticker} {interval}'], next_steps=['데이터 정합성 검사'])
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
    if df is None:
        update_phase('A - 데이터 수집', status='failed', percent=0, issues=['pyupbit returned no data'])
        raise RuntimeError('No data returned from pyupbit')
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
