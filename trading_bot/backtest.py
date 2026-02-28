import pandas as pd
import math
from trading_bot.tasks.state_updater import update_phase


def compute_metrics(equity_curve, period_hours=60):
    # equity_curve: list of {'time':..., 'value':...} ordered by time
    # compute simple CAGR, MDD, annualized Sharpe (risk-free=0)
    if not equity_curve:
        return {'cagr': 0.0, 'mdd': 0.0, 'sharpe': 0.0}
    values = [float(x['value']) for x in equity_curve]
    times = [x['time'] for x in equity_curve]
    start = values[0]
    end = values[-1]
    n = len(values)
    # approximate years = n * (period_hours / 24) / 365
    years = max(1e-9, n * (period_hours / 24.0) / 365.0)
    cagr = (end / start) ** (1.0 / years) - 1.0 if start > 0 else 0.0
    # MDD
    peak = values[0]
    mdd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak>0 else 0.0
        if dd > mdd:
            mdd = dd
    # returns to compute sharpe
    rets = []
    for i in range(1, len(values)):
        if values[i-1] > 0:
            rets.append((values[i] / values[i-1]) - 1.0)
    if len(rets) >= 2:
        avg = sum(rets)/len(rets)
        std = (sum((r-avg)**2 for r in rets)/(len(rets)-1))**0.5
        # annualize factor: assume hourly period_hours between samples
        factor = math.sqrt(365.0 * 24.0 / period_hours)
        sharpe = (avg * factor) / std if std>0 else 0.0
    else:
        sharpe = 0.0
    return {'cagr': cagr, 'mdd': mdd, 'sharpe': sharpe}


def simple_backtest(df_signals, initial_cash=100000, fee_pct=0.0005, slippage_pct=0.0005):
    # initialize stages with weights
    stages = {
        'B.data_prep': {'weight': 10, 'progress': 100},
        'B.equity_curve': {'weight': 40, 'progress': 0},
        'B.metrics': {'weight': 30, 'progress': 0},
        'B.stop_logic': {'weight': 10, 'progress': 0},
        'B.output': {'weight': 10, 'progress': 0}
    }
    update_phase('B - 백테스트', status='in_progress', stages=stages)

    cash = initial_cash
    position = 0.0
    entry_price = 0.0
    trades = []
    equity_curve = []
    total = len(df_signals)
    for idx, row in enumerate(df_signals.itertuples()):
        sig = int(getattr(row, 'signal', 0) or 0)
        price = float(getattr(row, 'close'))
        equity = cash + position * price
        equity_curve.append({'time': str(getattr(row, 'time')), 'value': equity})
        # simulate trades
        if sig == 1 and position == 0:
            cost_per_unit = price * (1 + fee_pct + slippage_pct)
            if cost_per_unit <= 0:
                cost_per_unit = price
            qty = cash / cost_per_unit if cost_per_unit > 0 else 0
            cost = qty * cost_per_unit
            if cost > cash or qty <= 0:
                continue
            position = qty
            entry_price = price
            cash -= cost
            trades.append({'time': str(getattr(row, 'time')), 'type': 'buy', 'price': price, 'qty': qty, 'entry_price': entry_price})
        elif sig == -1 and position > 0:
            proceeds = position * price * (1 - fee_pct - slippage_pct)
            cash += proceeds
            trades.append({'time': str(getattr(row, 'time')), 'type': 'sell', 'price': price, 'qty': position, 'entry_price': entry_price})
            position = 0
            entry_price = 0.0
        # update equity progress every 5% of total or at end
        if total>0 and (idx % max(1, total//20) == 0 or idx==total-1):
            pct = int((idx+1)/total*100)
            stages['B.equity_curve']['progress'] = pct
            update_phase('B - 백테스트', status='in_progress', stages=stages)

    # 최종 자산 계산 (미청산 포지션 포함)
    # 루프 마지막 행에서 이미 equity_curve에 추가되었으므로, 미청산 포지션이 있을 때만
    # 최종 값을 업데이트 (중복 entry 방지)
    final_price = float(df_signals.iloc[-1]['close'])
    final_value = cash + (position * final_price if position > 0 else 0)
    if equity_curve:
        equity_curve[-1]['value'] = final_value  # 마지막 포인트 값만 갱신 (중복 추가 X)
    else:
        equity_curve.append({'time': str(df_signals.iloc[-1]['time']), 'value': final_value})
    stages['B.equity_curve']['progress'] = 100
    update_phase('B - 백테스트', status='in_progress', stages=stages)

    # compute metrics with intermediate updates
    stages['B.metrics']['progress'] = 10
    update_phase('B - 백테스트', status='in_progress', stages=stages)
    metrics = compute_metrics(equity_curve)

    # 추가 메트릭: total_return, win_rate
    total_return = (final_value - initial_cash) / initial_cash if initial_cash and initial_cash > 0 else 0.0
    sells = [t for t in trades if t.get('type') == 'sell']
    winning_sells = [t for t in sells if float(t.get('price', 0)) > float(t.get('entry_price', 0))]
    win_rate = len(winning_sells) / len(sells) if sells else 0.0

    # max_drawdown: compute_metrics()의 올바른 peak-to-trough MDD 사용
    # (기존: peak-min range 계산 오류 수정)
    metrics.update({
        'total_return': total_return,
        'max_drawdown': metrics['mdd'],  # compute_metrics()의 정확한 peak-to-trough MDD 사용
        'total_trades': len(trades),
        'win_rate': win_rate,
        'stop_loss_count': 0,
        'take_profit_count': 0,
        'signal_trades_count': len(trades),
    })
    
    stages['B.metrics']['progress'] = 100
    update_phase('B - 백테스트', status='in_progress', stages=stages)

    # finalize
    stages['B.output']['progress'] = 100
    update_phase('B - 백테스트', status='done', stages=stages, recent_actions=[f'백테스트 완료 final={final_value:.2f}'], tests={'backtest': 'done'})

    # persist results to DB and save equity/trades to file
    import json, time, os
    try:
        from trading_bot.db import get_session
        from trading_bot.models import Backtest, Trade, EquityPoint
        session = get_session()
        ts_now = int(pd.Timestamp.now().timestamp())
        run_name = f'simple_backtest_{ts_now}'
        # save equity+trades to file
        os.makedirs('trading_bot/logs/backtests', exist_ok=True)
        out_path = f'trading_bot/logs/backtests/{run_name}.json'
        with open(out_path, 'w') as f:
            json.dump({'equity_curve': equity_curve, 'trades': trades, 'metrics': metrics}, f, default=str)
        bt = Backtest(run_name=run_name, params={}, start_ts=None, end_ts=None, final_value=final_value, metrics=metrics, equity_ref=out_path)
        session.add(bt)
        session.flush()  # get bt.id
        # write trades
        for t in trades:
            tr = Trade(backtest_id=bt.id, ts=pd.to_datetime(t['time']).to_pydatetime(), side=t['type'], price=float(t['price']), qty=float(t['qty']), fee=0.0, raw={})
            session.add(tr)
        # write equity points
        for pt in equity_curve:
            ep = EquityPoint(backtest_id=bt.id, ts=pd.to_datetime(pt['time']).to_pydatetime(), value=float(pt['value']))
            session.add(ep)
        session.commit()
        session.close()
    except Exception as e:
        print('Failed to write backtest to DB:', e)

    return {'final_value': final_value, 'trades': trades, 'equity_curve': equity_curve, 'metrics': metrics}

