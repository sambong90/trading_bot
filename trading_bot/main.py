
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
    return 'Trading bot dashboard (local) - available routes: /decisions /account /api/decisions /api/account/summary'

from flask import render_template

@app.route('/decisions')
def decisions_page():
    return render_template('decisions.html')

@app.route('/account')
def account_page():
    return render_template('account.html')

@app.route('/api/decisions')
def api_decisions():
    # return last_decision.json if exists
    try:
        import json
        with open('trading_bot/logs/last_decision.json') as f:
            d = json.load(f)
        return jsonify({'decisions':[d]})
    except Exception as e:
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
            cur = {'currency':b.get('currency'),'balance':bal,'avg_buy_price':b.get('avg_buy_price')}
            summary.append(cur)
        return jsonify({'balances':summary})
    except Exception as e:
        return jsonify({'error':str(e)})


@app.route('/api/decision_detail')
def api_decision_detail():
    # returns last_decision + recent OHLCV for the ticker
    try:
        import json
        from trading_bot.data import fetch_ohlcv
        with open('trading_bot/logs/last_decision.json') as f:
            d = json.load(f)
        ticker = d.get('ticker')
        if not ticker:
            return jsonify({'error':'no ticker in last decision'})
        # fetch ohlcv
        try:
            df = fetch_ohlcv(ticker=ticker, interval='minute60', count=100)
            # convert to dict
            ohlcv = df.tail(60)[['ts','open','high','low','close','volume']].to_dict(orient='records')
        except Exception as e:
            ohlcv = []
        return jsonify({'decision':d,'ohlcv':ohlcv})
    except Exception as e:
        return jsonify({'error':str(e)})


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
        executor.place_order('buy', float(last['close']))
    elif last['signal'] == -1:
        executor.place_order('sell', float(last['close']))
    return backtest_res, executor

if __name__ == '__main__':
    res, execu = run_paper_cycle()
    print('Backtest final value:', res['final_value'])
    print('Executor cash, position:', execu.cash, execu.position)
