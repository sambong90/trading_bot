import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

def load_module(name):
    path = ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

print('Running quick unit tests...')

try:
    data_mod = load_module('data')
    strat_mod = load_module('strategy')
    bt_mod = load_module('backtest')
    print('Modules loaded OK')
except Exception as e:
    print('Module load FAIL', e)
    raise

# Test data fetch
try:
    df = data_mod.fetch_ohlcv(ticker='KRW-BTC', interval='minute60', count=50)
    print('data.fetch_ohlcv: OK, rows=', len(df))
except Exception as e:
    print('data.fetch_ohlcv: FAIL', e)

# Test strategy
try:
    df_s = strat_mod.generate_sma_signals(df, short=5, long=20)
    print('strategy.generate_sma_signals: OK, signals=', int(df_s['signal'].abs().sum()))
except Exception as e:
    print('strategy.generate_sma_signals: FAIL', e)

# Test backtest
try:
    res = bt_mod.simple_backtest(df_s, initial_cash=100000)
    print('backtest.simple_backtest: OK, final_value=', res.get('final_value'))
except Exception as e:
    print('backtest.simple_backtest: FAIL', e)

print('Unit tests complete.')
