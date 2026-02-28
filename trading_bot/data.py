# ---------------------------------------------------------------------------
# 라이브러리 설치: pip install pandas
# (pandas-ta는 strategy.py에서 기술적 지표 계산용으로 사용)
# ---------------------------------------------------------------------------
"""
[Data Flow - OHLCV 데이터 흐름]
1. Upbit API (pyupbit.get_ohlcv) → raw DataFrame (index=datetime, Open/High/Low/Close/Volume)
2. fetch_ohlcv() → 정규화된 pandas DataFrame (time, open, high, low, close, volume) 반환
3. strategy.ohlcv_to_dataframe() / generate_ema_regime_signals()에서 해당 DataFrame 수신 후
   pandas-ta로 지표 추가 및 매매 신호 생성
"""
import pyupbit
import pandas as pd
from trading_bot.tasks.state_updater import update_phase
from trading_bot.db import get_session
from trading_bot.models import OHLCV


# 거래대금 상위 N개만 순회 (TICKERS 미설정 시). API 호출 최소화·사이클 시간 단축용
DEFAULT_TOP_N_BY_TRADE_PRICE = 60


def get_all_krw_tickers(use_db_fallback=True):
    """KRW 마켓 티커 목록 반환.
    - env TICKERS(쉼표구분) 있으면 우선 사용.
    - 없으면 업비트 전체 KRW 티커 중 24h 거래대금(acc_trade_price_24h) 상위 60개만 반환 (API 1~2회로 배치 조회).
    - 실패 시 DB ohlcv 테이블 폴백, 최종 폴백은 기본 4종목.
    """
    import os
    env_tickers = os.environ.get('TICKERS', '').strip()
    if env_tickers:
        return [t.strip() for t in env_tickers.split(',') if t.strip().startswith('KRW-')]

    top_n = DEFAULT_TOP_N_BY_TRADE_PRICE
    try:
        top_n_env = os.environ.get('TICKER_TOP_N', '')
        if top_n_env.isdigit():
            top_n = max(1, min(200, int(top_n_env)))
    except Exception:
        pass

    try:
        # 1) KRW 마켓 티커 목록 (API 1회: /v1/market/all)
        all_krw = pyupbit.get_tickers(fiat='KRW')
        if not all_krw:
            raise RuntimeError('get_tickers(fiat=KRW) returned empty')

        # 2) 24h 거래대금 한 번에 조회: get_current_price(리스트, verbose=True) → /v1/ticker 배치 호출(최대 200개씩)
        #    응답 리스트 항목에 acc_trade_price_24h 포함
        raw_list = pyupbit.get_current_price(all_krw, verbose=True)
        if not raw_list or not isinstance(raw_list, list):
            raise RuntimeError('ticker batch response invalid')

        # 3) acc_trade_price_24h 기준 내림차순 정렬 후 상위 top_n개
        def _trade_price_24h(item):
            try:
                return float(item.get('acc_trade_price_24h') or 0)
            except (TypeError, ValueError):
                return 0.0

        sorted_list = sorted(raw_list, key=_trade_price_24h, reverse=True)
        tickers = [x.get('market') for x in sorted_list[:top_n] if x.get('market')]
        if tickers:
            return tickers
    except Exception as e:
        if use_db_fallback:
            pass
        else:
            raise

    if use_db_fallback:
        try:
            session = get_session()
            rows = session.query(OHLCV.ticker).distinct().all()
            session.close()
            tickers = [r[0] for r in rows if r[0] and r[0].startswith('KRW-')]
            if tickers:
                return sorted(set(tickers))[:top_n]
        except Exception:
            pass
    return ['KRW-BTC', 'KRW-ETH', 'KRW-XRP', 'KRW-SOL']


def fetch_ohlcv_from_db(ticker='KRW-BTC', interval='minute60', count=200):
    """
    DB에서 OHLCV 데이터 가져오기 (과거 데이터 포함)
    
    Parameters:
    - ticker: 티커
    - interval: 시간 간격
    - count: 가져올 데이터 개수
    
    Returns:
    - DataFrame 또는 None
    """
    try:
        session = get_session()
        from trading_bot.models import OHLCV
        
        # 최근 count개 데이터 가져오기
        ohlcv_records = session.query(OHLCV)\
            .filter(OHLCV.ticker == ticker)\
            .filter(OHLCV.timeframe == interval)\
            .order_by(OHLCV.ts.desc())\
            .limit(count).all()
        
        session.close()
        
        if not ohlcv_records:
            return None
        
        # DataFrame으로 변환
        data = []
        for record in reversed(ohlcv_records):  # 시간순 정렬
            data.append({
                'time': record.ts,
                'open': record.open,
                'high': record.high,
                'low': record.low,
                'close': record.close,
                'volume': record.volume
            })
        
        df = pd.DataFrame(data)
        if len(df) > 0:
            df['time'] = pd.to_datetime(df['time'])
            return df
        
        return None
    except Exception as e:
        print(f'⚠️ DB에서 OHLCV 데이터 가져오기 실패: {e}')
        return None


def fetch_ohlcv(ticker='KRW-BTC', interval='minute60', count=200, retry=3, backoff=1, use_db_first=True):
    """Fetch OHLCV from Upbit and return DataFrame with columns time, open, high, low, close, volume
    Implements simple retry with exponential backoff and cache fallback (trading_bot/logs/cache).
    If use_db_first=True, tries to fetch from DB first, then fills gaps with API data.
    """
    import time, os, json
    cache_dir = os.path.join('trading_bot','logs','cache')
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{ticker}_{interval}_{count}.json")

    update_phase('A - 데이터 수집', status='in_progress', percent=10, recent_actions=[f'fetch start {ticker} {interval}'], next_steps=['데이터 정합성 검사'])
    df = None
    last_exc = None
    
    # DB에서 먼저 데이터 가져오기 시도
    if use_db_first:
        df_db = fetch_ohlcv_from_db(ticker=ticker, interval=interval, count=count)
        if df_db is not None and len(df_db) >= count * 0.8:  # 80% 이상 데이터가 있으면 사용
            # 최신 데이터만 API에서 가져와서 보완
            try:
                import random
                time.sleep(random.uniform(0.1, 0.3))
                df_api = pyupbit.get_ohlcv(ticker, interval=interval, count=min(50, count // 4))
                if df_api is not None and len(df_api) > 0:
                    # DB 데이터와 병합 (중복 제거)
                    df_api = df_api.reset_index().rename(columns={'index':'time'})
                    df_db_latest = df_db['time'].max()
                    df_api_new = df_api[df_api['time'] > df_db_latest]
                    if len(df_api_new) > 0:
                        df = pd.concat([df_db, df_api_new], ignore_index=True).sort_values('time').tail(count)
                    else:
                        df = df_db.tail(count)
                else:
                    df = df_db.tail(count)
            except Exception:
                df = df_db.tail(count)
    
    # DB 데이터가 없거나 부족하면 API에서 가져오기
    if df is None or len(df) < count * 0.5:
        # API 호출 간격 조정 (rate limiting 방지)
        # 업비트 API는 초당 10회 제한이 있으므로 최소 0.1초 간격 유지
        import random
        time.sleep(random.uniform(0.1, 0.3))
        
        for attempt in range(1, retry+1):
            try:
                df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
                if df is not None and len(df):
                    # 성공 시 실패 카운터 리셋
                    try:
                        fc_path = os.path.join('trading_bot','logs','fail_count.json')
                        if os.path.exists(fc_path):
                            with open(fc_path,'w',encoding='utf-8') as f:
                                json.dump({'count':0}, f)
                    except Exception:
                        pass
                    break
                last_exc = RuntimeError('No data returned from pyupbit')
            except Exception as e:
                last_exc = e
                # API 에러인 경우 더 긴 대기 시간
                if '429' in str(e) or 'rate limit' in str(e).lower() or 'too many' in str(e).lower():
                    wait_time = backoff * (2 ** attempt) * 2  # rate limit일 경우 더 길게 대기
                    print(f'⚠️ Rate limit 감지, {wait_time}초 대기 후 재시도...')
                    time.sleep(wait_time)
                else:
                    time.sleep(backoff * (2 ** (attempt-1)))
        
        if df is None or len(df)==0:
            # increment failure counter
            try:
                fc_path = os.path.join('trading_bot','logs','fail_count.json')
                fc = {'count':0, 'last_failure': None}
                if os.path.exists(fc_path):
                    try:
                        with open(fc_path,'r',encoding='utf-8') as f:
                            fc = json.load(f)
                    except Exception:
                        fc = {'count':0, 'last_failure': None}
                
                fc['count'] = fc.get('count',0) + 1
                fc['last_failure'] = time.strftime('%Y-%m-%d %H:%M:%S')
                fc['last_error'] = str(last_exc) if last_exc else 'No data returned'
                
                with open(fc_path,'w',encoding='utf-8') as f:
                    json.dump(fc,f, indent=2)
                
                # 로깅 개선
                print(f'⚠️ API 호출 실패 ({fc["count"]}회 연속): {ticker} - {fc.get("last_error", "Unknown error")}')
                
                # if >=3, send telegram alert (best-effort)
                if fc['count'] >= 3:
                    try:
                        from trading_bot.monitor import send_telegram
                        msg = f"[⚠️ Alert] 업비트 API 호출 실패 {fc['count']}회 연속\n티커: {ticker}\n에러: {fc.get('last_error', 'Unknown')}\n마지막 실패: {fc.get('last_failure', 'N/A')}"
                        success, error_msg = send_telegram(msg)
                        if not success:
                            print(f'⚠️ 텔레그램 알림 전송 실패: {error_msg or "알 수 없는 오류"}')
                    except Exception as e:
                        print(f'⚠️ 텔레그램 알림 전송 중 오류: {e}')
            except Exception as e:
                print(f'⚠️ 실패 카운터 저장 실패: {e}')

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
            out = df.reset_index().rename(columns={'index':'time'}) if 'index' in df.columns else df.copy()
            if 'time' not in out.columns:
                out = out.reset_index().rename(columns={'index':'time'})
            # serialize time as ISO
            out['time'] = out['time'].astype(str)
            with open(cache_file,'w',encoding='utf-8') as f:
                json.dump(out.to_dict(orient='records'), f, ensure_ascii=False)
        except Exception:
            pass

    df = df.reset_index().rename(columns={'index':'time'}) if 'index' in df.columns else df.copy()
    if 'time' not in df.columns:
        df = df.reset_index().rename(columns={'index':'time'})
    # 컬럼명 소문자 정규화 (Open/High/Low/Close/Volume → open/high/low/close/volume)
    col_lower = {c: c.lower() for c in df.columns if c.lower() in ('open', 'high', 'low', 'close', 'volume')}
    if col_lower:
        df = df.rename(columns=col_lower)
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
    session = None
    conn = None
    try:
        session = get_session()
        engine = session.get_bind()
        dialect = engine.dialect.name
        if dialect == 'sqlite':
            from sqlalchemy import text
            conn = engine.connect()
            try:
                for _, row in df.iterrows():
                    ins = text("INSERT OR IGNORE INTO ohlcv (ticker, timeframe, ts, open, high, low, close, volume, source) VALUES (:ticker, :timeframe, :ts, :open, :high, :low, :close, :volume, :source)")
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
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        continue
            finally:
                conn.close()
                conn = None
        else:
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
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    return df


def save_csv(df, path):
    df.to_csv(path, index=False)

