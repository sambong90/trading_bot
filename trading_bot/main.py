

from flask import Flask, request, jsonify
import os, sys
from pathlib import Path
# ensure workspace root on sys.path
ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
app = Flask(__name__)

@app.route('/panic', methods=['POST'])
def panic():
    try:
        # disable auto live immediately
        import dotenv, os
        dotenv_path='trading_bot/.env'
        # update env file (simple append toggle)
        with open(dotenv_path,'r') as f:
            lines=f.readlines()
        new=[]
        found=False
        for L in lines:
            if L.startswith('ENABLE_AUTO_LIVE='):
                new.append('ENABLE_AUTO_LIVE=0\n')
                found=True
            else:
                new.append(L)
        if not found:
            new.append('ENABLE_AUTO_LIVE=0\n')
        with open(dotenv_path,'w') as f:
            f.writelines(new)
        return jsonify({'ok':True,'msg':'AUTO LIVE disabled'})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)})


import os

# Simple dashboard routes
@app.route('/')
def index():
    return render_template('main.html')

from flask import render_template

@app.route('/decisions')
def decisions_page():
    return render_template('decisions.html')

@app.route('/account')
def account_page():
    return render_template('account.html')

@app.route('/logs')
def logs_page():
    return render_template('logs.html')

@app.route('/analysis')
def analysis_page():
    return render_template('analysis.html')

@app.route('/api/logs')
def api_logs():
    # return last lines of trading bot logs
    try:
        import json
        log_dir = os.path.join('trading_bot','logs')
        # 실제 유용한 로그 파일들
        files = ['auto_trader.log', 'scheduler_out.log']
        out = {}
        for f in files:
            path = os.path.join(log_dir,f)
            if os.path.exists(path):
                with open(path,'r',encoding='utf-8',errors='ignore') as fh:
                    data = fh.read()
                # 불필요한 경고 메시지 필터링
                lines = data.split('\n')
                filtered_lines = []
                for line in lines:
                    # urllib3 경고, NotOpenSSLWarning 등 필터링
                    if 'NotOpenSSLWarning' in line or 'urllib3' in line or 'warnings.warn' in line:
                        continue
                    # 리스크 필터 실패 로그 제외 (정상 필터링이므로 노이즈 감소)
                    if '리스크 필터 실패' in line or '필터 실패' in line:
                        continue
                    # 빈 줄이나 의미없는 줄 필터링
                    if line.strip() and not line.strip().startswith('•'):
                        filtered_lines.append(line)
                data = '\n'.join(filtered_lines)
                # limit size (최근 200KB만)
                if len(data) > 200000:
                    data = data[-200000:]
                out[f] = data
            else:
                out[f] = ''
        return jsonify({'ok':True,'logs':out})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)})

@app.route('/api/decisions')
def api_decisions():
    """최근 거래 결정 조회 (DB에서 최근 분석 결과 가져오기)"""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AnalysisResult, Order
        import pandas as pd
        
        limit = int(request.args.get('limit', '50'))
        
        session = get_session()
        
        # 최근 분석 결과 중 신호가 있는 것만 가져오기
        analysis_results = session.query(AnalysisResult)\
            .filter(AnalysisResult.signal.in_(['buy', 'sell']))\
            .order_by(AnalysisResult.timestamp.desc())\
            .limit(limit).all()
        
        # 최근 주문 내역도 가져오기
        orders = session.query(Order)\
            .order_by(Order.ts.desc())\
            .limit(limit).all()
        
        decisions = []
        
        # 분석 결과를 decisions 형식으로 변환
        for ar in analysis_results:
            decisions.append({
                'ticker': ar.ticker,
                'time': ar.timestamp.isoformat() if ar.timestamp else None,
                'price': ar.price or 0,
                'signal': 1 if ar.signal == 'buy' else -1 if ar.signal == 'sell' else 0,
                'action': '매수' if ar.signal == 'buy' else '매도' if ar.signal == 'sell' else '보류',
                'cash': 0,  # 분석 결과에는 없음
                'position': ar.position_size or 0
            })
        
        # 주문 내역도 추가
        for order in orders:
            # 중복 제거 (같은 시간의 분석 결과가 있으면 제외)
            existing = any(d.get('time') == order.ts.isoformat() if order.ts else False for d in decisions)
            if not existing:
                decisions.append({
                    'ticker': f"KRW-{order.side.upper()}" if order.side else 'UNKNOWN',
                    'time': order.ts.isoformat() if order.ts else None,
                    'price': order.price or 0,
                    'signal': 1 if order.side == 'buy' else -1 if order.side == 'sell' else 0,
                    'action': '매수' if order.side == 'buy' else '매도' if order.side == 'sell' else '보류',
                    'cash': 0,
                    'position': order.qty or 0
                })
        
        session.close()
        
        # 시간순 정렬
        decisions.sort(key=lambda x: x.get('time', ''), reverse=True)
        
        # last_decision.json도 확인 (하위 호환성)
        try:
            import json
            with open('trading_bot/logs/last_decision.json') as f:
                last_d = json.load(f)
                # 중복 확인 후 추가
                existing = any(d.get('ticker') == last_d.get('ticker') and 
                              abs((pd.to_datetime(d.get('time')) - pd.to_datetime(last_d.get('time'))).total_seconds()) < 60 
                              for d in decisions)
                if not existing and last_d.get('signal') != 0:
                    decisions.insert(0, {
                        'ticker': last_d.get('ticker', ''),
                        'time': last_d.get('time', ''),
                        'price': last_d.get('price', 0),
                        'signal': last_d.get('signal', 0),
                        'action': '매수' if last_d.get('signal') == 1 else '매도' if last_d.get('signal') == -1 else '보류',
                        'cash': last_d.get('cash', 0),
                        'position': last_d.get('position', 0)
                    })
        except:
            pass
        
        return jsonify({'decisions': decisions[:limit]})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'decisions':[], 'error':str(e)})

@app.route('/status')
def status_page():
    try:
        from trading_bot.tasks.progress import read_progress
        return jsonify({'status': read_progress()})
    except Exception as e:
        return jsonify({'status': {'phase':'unknown','task':None,'percent':0,'msg':str(e)}})

@app.route('/api/account/summary')
def api_account_summary():
    try:
        import pyupbit
        from dotenv import load_dotenv
        load_dotenv('trading_bot/.env')
        import os
        access=os.environ.get('UPBIT_ACCESS_KEY')
        secret=os.environ.get('UPBIT_SECRET_KEY')
        up = None
        if access and secret:
            try:
                up = pyupbit.Upbit(access, secret)
            except Exception:
                up = None
        bals = []
        if up:
            try:
                bals = up.get_balances()
            except Exception:
                bals = []
        # build simple summary
        summary=[]
        for b in bals:
            try:
                bal=float(b.get('balance') or 0)
            except Exception:
                bal=0.0
            if bal<=0:
                continue
            # avg_buy_price를 숫자로 변환
            avg_price = b.get('avg_buy_price')
            try:
                avg_price = float(avg_price) if avg_price else 0.0
            except:
                avg_price = 0.0
            
            cur = {
                'currency':b.get('currency'),
                'balance':bal,
                'avg_buy_price':avg_price
            }
            summary.append(cur)
        return jsonify({'balances':summary})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error':str(e)})


@app.route('/api/decision_detail')
def api_decision_detail():
    """선택된 결정의 상세 정보 및 OHLCV 데이터 반환"""
    try:
        import json
        from trading_bot.data import fetch_ohlcv
        import pandas as pd
        
        ticker = request.args.get('ticker')
        
        # ticker가 없으면 last_decision.json에서 가져오기
        if not ticker:
            try:
                with open('trading_bot/logs/last_decision.json') as f:
                    d = json.load(f)
                ticker = d.get('ticker')
            except:
                pass
        
        if not ticker:
            return jsonify({'error':'no ticker provided'})
        
        # fetch ohlcv
        try:
            df = fetch_ohlcv(ticker=ticker, interval='minute60', count=100)
            # ts 컬럼이 없으면 time 컬럼 사용
            if 'ts' not in df.columns and 'time' in df.columns:
                df['ts'] = df['time']
            # convert to dict
            ohlcv = df.tail(60).copy()
            # ts를 epoch seconds로 변환
            ohlcv['ts'] = pd.to_datetime(ohlcv['ts']).astype('int64') // 10**9
            ohlcv = ohlcv[['ts','open','high','low','close','volume']].to_dict(orient='records')
        except Exception as e:
            import traceback
            traceback.print_exc()
            ohlcv = []
        
        # 결정 정보 가져오기
        decision = {}
        try:
            with open('trading_bot/logs/last_decision.json') as f:
                decision = json.load(f)
        except:
            pass
        
        return jsonify({'decision':decision,'ohlcv':ohlcv})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error':str(e)})


# price ohlcv API with simple cache
from functools import lru_cache
import time, json
CACHE_DIR = 'trading_bot/logs/cache'
import os
os.makedirs(CACHE_DIR, exist_ok=True)

@app.route('/api/price_ohlcv')
def api_price_ohlcv():
    try:
        from trading_bot.data import fetch_ohlcv
        ticker = request.args.get('ticker')
        interval = request.args.get('interval','minute60')
        count = int(request.args.get('count','200'))
        if not ticker:
            return jsonify({'error':'ticker required'}), 400
        key = f"{ticker}_{interval}_{count}"
        cache_file = os.path.join(CACHE_DIR, key + '.json')
        ttl = 15
        now = time.time()
        if os.path.exists(cache_file) and now - os.path.getmtime(cache_file) < ttl:
            with open(cache_file) as f:
                cached = json.load(f)
            # if cached ohlcv is empty, ignore cache and refetch
            if cached.get('ohlcv'):
                return jsonify(cached)
        df = fetch_ohlcv(ticker=ticker, interval=interval, count=count)
        # Build a clean OHLCV list explicitly from known columns
        ohlcv = []
        try:
            for _, row in df.iterrows():
                try:
                    ts_val = None
                    if 'time' in row and row['time'] is not None:
                        ts_val = row['time']
                    elif 'ts' in row and row['ts'] is not None:
                        ts_val = row['ts']
                    # convert to epoch seconds
                    import pandas as pd
                    if ts_val is not None:
                        ts_epoch = int(pd.to_datetime(ts_val).timestamp())
                    else:
                        continue
                    ohlcv.append({'ts': ts_epoch, 'open': float(row['open']), 'high': float(row['high']), 'low': float(row['low']), 'close': float(row['close']), 'volume': float(row.get('volume', 0))})
                except Exception:
                    continue
        except Exception:
            ohlcv = []
        out = {'ticker':ticker,'interval':interval,'ohlcv':ohlcv}
        with open(cache_file,'w') as f:
            json.dump(out, f, default=str)
        return jsonify(out)
    except Exception as e:
        return jsonify({'error':str(e)})

@app.route('/api/analysis_results')
def api_analysis_results():
    """분석 결과 조회 API"""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import AnalysisResult
        import pandas as pd
        
        ticker = request.args.get('ticker')
        limit = int(request.args.get('limit', '100'))
        
        session = get_session()
        query = session.query(AnalysisResult).order_by(AnalysisResult.timestamp.desc())
        
        if ticker:
            query = query.filter(AnalysisResult.ticker == ticker)
        
        results = query.limit(limit).all()
        
        analysis_list = []
        for r in results:
            analysis_list.append({
                'id': r.id,
                'ticker': r.ticker,
                'timestamp': r.timestamp.isoformat() if r.timestamp else None,
                'signal': r.signal,
                'price': r.price,
                'change_rate': r.change_rate,
                'change_price': r.change_price,
                'high_24h': r.high_24h,
                'low_24h': r.low_24h,
                'volume_24h': r.volume_24h,
                'trade_price_24h': r.trade_price_24h,
                'analysis_data': r.analysis_data or {},
                'risk_filters': r.risk_filters or {},
                'position_size': r.position_size,
                'created_at': r.created_at.isoformat() if r.created_at else None
            })
        
        session.close()
        
        return jsonify({'results': analysis_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tickers')
def api_tickers():
    """모든 KRW 마켓 티커 목록 반환"""
    try:
        from trading_bot.data import get_all_krw_tickers
        tickers = get_all_krw_tickers()
        return jsonify({'tickers': tickers, 'count': len(tickers)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/technical_indicators')
def api_technical_indicators():
    """기술적 지표 데이터 조회"""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import TechnicalIndicator
        
        ticker = request.args.get('ticker')
        timeframe = request.args.get('timeframe', 'minute60')
        limit = int(request.args.get('limit', '100'))
        
        session = get_session()
        query = session.query(TechnicalIndicator).order_by(TechnicalIndicator.ts.desc())
        
        if ticker:
            query = query.filter(TechnicalIndicator.ticker == ticker)
        if timeframe:
            query = query.filter(TechnicalIndicator.timeframe == timeframe)
        
        results = query.limit(limit).all()
        
        indicators_list = []
        for r in results:
            indicators_list.append({
                'id': r.id,
                'ticker': r.ticker,
                'timeframe': r.timeframe,
                'ts': r.ts.isoformat() if r.ts else None,
                'sma_short': r.sma_short,
                'sma_long': r.sma_long,
                'ema_short': r.ema_short,
                'ema_long': r.ema_long,
                'rsi': r.rsi,
                'atr': r.atr,
                'volume_ma': r.volume_ma,
                'indicators': r.indicators or {},
                'created_at': r.created_at.isoformat() if r.created_at else None
            })
        
        session.close()
        
        return jsonify({'indicators': indicators_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ticker_snapshots')
def api_ticker_snapshots():
    """티커 스냅샷 데이터 조회"""
    try:
        from trading_bot.db import get_session
        from trading_bot.models import TickerSnapshot
        
        ticker = request.args.get('ticker')
        limit = int(request.args.get('limit', '100'))
        
        session = get_session()
        query = session.query(TickerSnapshot).order_by(TickerSnapshot.timestamp.desc())
        
        if ticker:
            query = query.filter(TickerSnapshot.ticker == ticker)
        
        results = query.limit(limit).all()
        
        snapshots_list = []
        for r in results:
            snapshots_list.append({
                'id': r.id,
                'ticker': r.ticker,
                'timestamp': r.timestamp.isoformat() if r.timestamp else None,
                'current_price': r.current_price,
                'change_rate': r.change_rate,
                'change_price': r.change_price,
                'high_24h': r.high_24h,
                'low_24h': r.low_24h,
                'volume_24h': r.volume_24h,
                'trade_price_24h': r.trade_price_24h,
                'prev_closing_price': r.prev_closing_price,
                'created_at': r.created_at.isoformat() if r.created_at else None
            })
        
        session.close()
        
        return jsonify({'snapshots': snapshots_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ticker_info')
def api_ticker_info():
    """현재 가격, 변동률, 고가/저가/거래량 정보 반환"""
    try:
        import pyupbit
        ticker = request.args.get('ticker', 'KRW-BTC')
        if not ticker:
            return jsonify({'error':'ticker required'}), 400
        
        # 현재 가격
        current_price = pyupbit.get_current_price(ticker)
        if current_price is None:
            return jsonify({'error':'Failed to fetch price'}), 500
        
        # 24시간 틱커 정보
        ticker_info = pyupbit.get_ticker(ticker)
        
        # 최근 OHLCV 데이터 (24시간 고가/저가용)
        from trading_bot.data import fetch_ohlcv
        df = fetch_ohlcv(ticker=ticker, interval='day', count=2)
        
        high_24h = float(ticker_info.get('high_price', 0)) if ticker_info else 0
        low_24h = float(ticker_info.get('low_price', 0)) if ticker_info else 0
        volume_24h = float(ticker_info.get('acc_trade_volume_24h', 0)) if ticker_info else 0
        trade_price_24h = float(ticker_info.get('acc_trade_price_24h', 0)) if ticker_info else 0
        
        # 전일 대비 변동률
        prev_closing_price = float(ticker_info.get('prev_closing_price', current_price)) if ticker_info else current_price
        change_rate = ((current_price - prev_closing_price) / prev_closing_price * 100) if prev_closing_price > 0 else 0
        change_price = current_price - prev_closing_price
        
        # 최근 24시간 데이터로 스파크라인 생성
        df_24h = fetch_ohlcv(ticker=ticker, interval='minute60', count=24)
        sparkline_data = []
        if len(df_24h) > 0:
            sparkline_data = df_24h['close'].tolist()
        
        return jsonify({
            'ticker': ticker,
            'current_price': current_price,
            'change_rate': change_rate,
            'change_price': change_price,
            'high_24h': high_24h,
            'low_24h': low_24h,
            'volume_24h': volume_24h,
            'trade_price_24h': trade_price_24h,
            'prev_closing_price': prev_closing_price,
            'sparkline': sparkline_data
        })
    except Exception as e:
        return jsonify({'error':str(e)}), 500


if __name__ == '__main__':
    # run Flask local server
    app.run(host='127.0.0.1', port=5000)

def run_paper_cycle(ticker='KRW-BTC', interval='minute60', count=500, short=10, long=50, initial_cash=100000):
    df = fetch_ohlcv(ticker=ticker, interval=interval, count=count)
    df_signals = generate_sma_signals(df, short=short, long=long)
    backtest_res = simple_backtest(df_signals, initial_cash=initial_cash)
    executor = PaperExecutor(initial_cash=initial_cash)
    # simulate executing last signals
    last = df_signals.iloc[-1]
    if last['signal'] == 1:
        executor.place_order('buy', float(last['close']), ticker=ticker)
    elif last['signal'] == -1:
        executor.place_order('sell', float(last['close']), ticker=ticker)
    return backtest_res, executor

if __name__ == '__main__':
    res, execu = run_paper_cycle()
    print('Backtest final value:', res['final_value'])
    print('Executor cash, positions:', execu.cash, execu.positions)

