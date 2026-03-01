#!/usr/bin/env python3
"""
Walk-Forward Auto Tuner (V5.0): fetches 30 days of 1h OHLCV for KRW-BTC and KRW-SOL,
runs grid_search to find the best param combo by final_value, and saves the best to TuningRun.
Designed to be run by scheduler (e.g. every Sunday 04:00).

V5.0 추가사항:
  - macro_ema_long [5, 20, 30, 50, 100] 을 param_grid에 추가
  - make_strategy_fn(daily_df_btc): 일봉 BTC 데이터를 클로저로 캡처,
    1h 백테스트 중 각 봉의 시점에서 Macro Trend Filter를 시뮬레이션
    (현재가 < 일봉 EMA(macro_ema_long) → 매수 차단)
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

COUNT_30D_1H = 30 * 24   # 720 bars (1h 전략 평가용)
COUNT_DAY = 200           # 일봉 200개 (macro_ema_long=100 기준 충분한 워밍업)
TICKERS = ['KRW-BTC', 'KRW-SOL']


def make_strategy_fn(daily_df_btc):
    """
    1h 전략 함수를 반환하는 팩토리.
    daily_df_btc(일봉 KRW-BTC DataFrame)를 클로저로 캡처하여
    그리드 서치 중 각 1h 봉 시점의 Macro Trend Filter를 시뮬레이션한다.

    시뮬레이션 로직:
      - 각 1h 봉의 시간 이전(≤) 마지막 일봉의 close vs daily EMA(macro_ema_long) 비교
      - close < EMA → 하락장 → 매수 신호 차단 (sell은 허용)
    """
    # 일봉 데이터를 날짜(자정) 기준 인덱스로 전처리 (한 번만 수행)
    _daily_close = None
    if daily_df_btc is not None and len(daily_df_btc) > 5:
        _df = daily_df_btc.copy()
        if 'time' not in _df.columns and _df.index.name is not None:
            _df = _df.reset_index().rename(columns={'index': 'time'})
        _df['time'] = pd.to_datetime(_df['time']).dt.normalize()  # 자정으로 정규화
        _df = _df.drop_duplicates(subset='time').set_index('time').sort_index()
        _daily_close = _df['close']

    def _strategy_fn(df, ema_short=12, ema_long=26, adx_trend_threshold=25.0,
                     macro_ema_long=50, **kwargs):
        """
        EMA 골든/데드크로스 + ADX 레짐 + Macro Trend Filter 백테스트 전략.
        macro_ema_long: 일봉 EMA 기간 (grid_search가 param_grid에서 주입)
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

        # ── 일봉 Macro EMA 사전 계산 ──────────────────────────────────────────
        # macro_ema_long에 따라 매번 재계산 (그리드별로 다른 값)
        macro_bull_series = None
        if _daily_close is not None and len(_daily_close) >= macro_ema_long:
            daily_ema = _daily_close.ewm(span=macro_ema_long, adjust=False).mean()
            # True: 해당 날짜 일봉 close ≥ EMA → 상승장
            macro_bull_series = (_daily_close >= daily_ema)

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

            # ── Macro Trend Filter: 매수 신호만 차단, 매도는 항상 허용 ─────────
            is_macro_bull = True
            if macro_bull_series is not None:
                try:
                    bar_date = pd.Timestamp(row.get('time') or row.name).normalize()
                    past = macro_bull_series.loc[:bar_date]
                    if len(past) > 0:
                        is_macro_bull = bool(past.iloc[-1])
                except Exception:
                    is_macro_bull = True

            if es > el and pes <= pel and is_macro_bull:
                df.loc[df.index[i], 'signal'] = 1   # 매수: 상승장만
            elif es < el and pes >= pel:
                df.loc[df.index[i], 'signal'] = -1  # 매도: 장세 무관

        return df[['time', 'close', 'signal']].copy()

    return _strategy_fn


def _backtest_fn(df_signals, fee_pct=0.0005, slippage_pct=0.0005):
    if df_signals is None or len(df_signals) == 0:
        return {'final_value': 0.0, 'trades': []}
    return simple_backtest(df_signals, initial_cash=100000, fee_pct=fee_pct, slippage_pct=slippage_pct)


def main():
    # ── 일봉 BTC 데이터 사전 로드 (Macro EMA 시뮬레이션용) ──────────────────
    print(f'[auto_tuner] Fetching {COUNT_DAY} daily bars for KRW-BTC (macro trend filter)...')
    daily_df_btc = None
    try:
        daily_df_btc = fetch_ohlcv(ticker='KRW-BTC', interval='day', count=COUNT_DAY, use_db_first=True)
        if daily_df_btc is not None:
            print(f'[auto_tuner] Daily BTC loaded: {len(daily_df_btc)} bars')
        else:
            print('[auto_tuner] WARNING: daily BTC fetch returned None — macro filter disabled in backtest')
    except Exception as e:
        print(f'[auto_tuner] WARNING: daily BTC fetch failed ({e}) — macro filter disabled in backtest')

    strategy_fn = make_strategy_fn(daily_df_btc)

    param_grid = {
        'ema_short': [9, 12, 15],
        'ema_long': [20, 26, 30],
        'adx_trend_threshold': [20, 25, 30],
        'macro_ema_long': [5, 20, 30, 50, 100],
    }
    total_combos = 3 * 3 * 3 * 5  # = 135
    print(f'[auto_tuner] Grid: {param_grid}')
    print(f'[auto_tuner] Total combos: {total_combos}')

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
            strategy_fn,
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
        print(
            f'[auto_tuner] Best combo saved to TuningRun: {best["combo"]} '
            f'(final_value={best["final_value"]:.2f}, ticker={best["ticker"]})'
        )
        print(f'[auto_tuner] macro_ema_long selected: {best["combo"].get("macro_ema_long", "N/A")}')
    except Exception as e:
        print('[auto_tuner] Failed to save best to TuningRun:', e)
        session.rollback()
    finally:
        session.close()


if __name__ == '__main__':
    main()
