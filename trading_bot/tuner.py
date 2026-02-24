import itertools
import pandas as pd
from copy import deepcopy
import time
from trading_bot.tasks.state_updater import update_phase

def grid_search(strategy_fn, df, param_grid, backtest_fn, fee_pct=0.0005, slippage_pct=0.0005):
    keys = list(param_grid.keys())
    combos = list(itertools.product(*[param_grid[k] for k in keys]))
    total = len(combos)
    stages = {
        'T.grid_prepare': {'weight':20, 'progress':100},
        'T.evaluate': {'weight':70, 'progress':0},
        'T.output': {'weight':10, 'progress':0}
    }
    update_phase('T - 튜너', status='in_progress', stages=stages)
    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        df_signals = strategy_fn(df, **params)
        res = backtest_fn(df_signals, fee_pct=fee_pct, slippage_pct=slippage_pct)
        results.append({'params': params, 'final_value': res.get('final_value'), 'trades': len(res.get('trades', []))})
        pct = int((i+1)/total*100)
        stages['T.evaluate']['progress'] = pct
        update_phase('T - 튜너', status='in_progress', stages=stages)
        time.sleep(0.01)
    stages['T.output']['progress'] = 100
    update_phase('T - 튜너', status='done', stages=stages)
    df_res = pd.DataFrame(results)
    df_res.to_csv('trading_bot/logs/tuning_results.csv', index=False)

    # persist tuning runs
    try:
        from trading_bot.db import get_session
        from trading_bot.models import TuningRun
        session = get_session()
        for r in results:
            tr = TuningRun(combo=r['params'] if 'params' in r else r['combo'], metrics={'final_value': r.get('final_value')})
            session.add(tr)
        session.commit()
        session.close()
    except Exception as e:
        print('Failed to write tuning runs to DB:', e)

    return df_res

