import os
from trading_bot.data import fetch_ohlcv
from trading_bot.strategy import generate_sma_signals
from trading_bot.backtest import simple_backtest
from trading_bot.executor import PaperExecutor

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
