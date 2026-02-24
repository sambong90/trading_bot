#!/usr/bin/env python3
"""
Walk-Forward Auto Tuner (V4.0): fetches 30 days of 1h OHLCV for KRW-BTC and KRW-SOL,
runs grid_search to find the best param combo by final_value, and saves the best to TuningRun.
Designed to be run by scheduler (e.g. every Sunday 04:00).
"""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'trading_bot' / '.env')
except Exception:
    pass

import pandas as pd
from trading_bot.data import fetch_ohlcv
from trading_bot.data_manager import compute_indicators
from trading_bot.tuner import grid_search
from trading_bot.backtest import simple_backtest
from trading_bot.db import get_session
from trading_bot.models import TuningRun

COUNT_30D_1H = 30 * 24  # 720 bars
TICKERS = ['KRW-BTC', 'KRW-SOL']


def _strategy_fn_ema_regime(df, ema_short=12, ema_long=26, adx_trend_threshold=25.0, **kwargs):
    """
    Backtest strategy: compute indicators with params, then signal 1 on EMA golden cross in trend,
    -1 on EMA dead cross. Used by grid_search; returns DataFrame with time, close, signal.
    """
    if df is None or len(df) < max(ema_long, 30):
        return pd.DataFrame()
    df = df.copy()
    if 'time' not in df.columns and df.index.name is not None:
        df = df.reset_index()
    df = compute_indicators(
        df,
        ema_short=ema_short,
        ema_long=ema_long,
        rsi_period=kwargs.get('rsi_period', 14),
        atr_period=kwargs.get('atr_period', 14),
    )
    if df is None or 'adx' not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df['signal'] = 0
    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        adx = float(row.get('adx', 0) or 0)
        es, el = row.get('ema_short'), row.get('ema_long')
        pes, pel = prev.get('ema_short'), prev.get('ema_long')
        if pd.isna(es) or pd.isna(el) or pd.isna(pes) or pd.isna(pel):
            continue
        if adx <= adx_trend_threshold:
            continue
        if es > el and pes <= pel:
            df.loc[df.index[i], 'signal'] = 1
        elif es < el and pes >= pel:
            df.loc[df.index[i], 'signal'] = -1
    return df[['time', 'close', 'signal']].copy()


def _backtest_fn(df_signals, fee_pct=0.0005, slippage_pct=0.0005):
    if df_signals is None or len(df_signals) == 0:
        return {'final_value': 0.0, 'trades': []}
    return simple_backtest(df_signals, initial_cash=100000, fee_pct=fee_pct, slippage_pct=slippage_pct)


def main():
    param_grid = {
        'ema_short': [9, 12, 15],
        'ema_long': [20, 26, 30],
        'adx_trend_threshold': [20, 25, 30],
    }

    all_results = []
    for ticker in TICKERS:
        print(f'[auto_tuner] Fetching {COUNT_30D_1H} bars 1h for {ticker}...')
        df = fetch_ohlcv(ticker=ticker, interval='minute60', count=COUNT_30D_1H, use_db_first=True)
        if df is None or len(df) < 100:
            print(f'[auto_tuner] Skip {ticker}: insufficient data')
            continue
        if 'time' not in df.columns and df.index.name is not None:
            df = df.reset_index()
        print(f'[auto_tuner] Grid search on {ticker} ({len(df)} rows)...')
        df_res = grid_search(
            _strategy_fn_ema_regime,
            df,
            param_grid,
            _backtest_fn,
            fee_pct=0.0005,
            slippage_pct=0.0005,
        )
        for _, row in df_res.iterrows():
            params = row.get('params') if isinstance(row.get('params'), dict) else {}
            fv = row.get('final_value')
            all_results.append({'ticker': ticker, 'combo': params, 'final_value': fv})

    if not all_results:
        print('[auto_tuner] No results.')
        return

    best = max(all_results, key=lambda x: float(x.get('final_value') or 0))
    session = get_session()
    try:
        tr = TuningRun(
            combo=best['combo'],
            metrics={'final_value': best['final_value'], 'ticker': best['ticker']},
        )
        session.add(tr)
        session.commit()
        print(f'[auto_tuner] Best combo saved to TuningRun: {best["combo"]} (final_value={best["final_value"]}, ticker={best["ticker"]})')
    except Exception as e:
        print('[auto_tuner] Failed to save best to TuningRun:', e)
        session.rollback()
    finally:
        session.close()


if __name__ == '__main__':
    main()

