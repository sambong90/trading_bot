#!/usr/bin/env python3
"""
Walk-Forward Auto Tuner (V5.1): fetches 30 days of 1h OHLCV for KRW-BTC and KRW-SOL,
runs IS/OOS grid search (70/30 split) to find the best param combo by composite score
(Sharpe + return - MDD), and saves the best to TuningRun.
Designed to be run by scheduler (e.g. every Sunday 04:00).

V5.1 추가사항 (L4 FIX):
  - IS/OOS 70/30 walk-forward split: 과적합 방지
  - Composite score = 0.4 × total_return + 0.4 × sharpe_norm − 0.2 × mdd
    (final_value 단독 선택에서 Sharpe/MDD 통합 평가로 교체)
  - OOS quality gate: OOS 복합점수 < IS 복합점수 × 0.40 이면 경고 (저장은 유지)

V5.0 추가사항:
  - macro_ema_long [5, 20, 30, 50, 100] 을 param_grid에 추가
  - make_strategy_fn(daily_df_btc): 일봉 BTC 데이터를 클로저로 캡처,
    1h 백테스트 중 각 봉의 시점에서 Macro Trend Filter를 시뮬레이션
    (현재가 < 일봉 EMA(macro_ema_long) → 매수 차단)
"""
import itertools
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
from trading_bot.backtest import simple_backtest
from trading_bot.db import get_session
from trading_bot.models import TuningRun

COUNT_30D_1H = 30 * 24   # 720 bars (1h 전략 평가용)
COUNT_DAY = 200           # 일봉 200개 (macro_ema_long=100 기준 충분한 워밍업)
TICKERS = ['KRW-BTC', 'KRW-SOL']

IS_RATIO = 0.70           # In-Sample 비율 (70%)
OOS_GATE = 0.40           # OOS 복합점수가 IS의 40% 미만이면 과적합 경고
INITIAL_CASH = 100_000.0
FEE_PCT = 0.0005
SLIPPAGE_PCT = 0.0005


def _composite_score(metrics: dict, final_value: float) -> float:
    """[L4 FIX] Composite tuning objective: 0.4×return + 0.4×sharpe_norm − 0.2×mdd.

    - total_return: (final_value / INITIAL_CASH) - 1  (e.g. 0.15 for 15% gain)
    - sharpe_norm:  raw Sharpe / 4.0  (normalises typical [-2, 4] range to [-0.5, 1])
    - mdd:          absolute max-drawdown fraction (e.g. 0.20 for 20% drawdown)
    Returns 0.0 if metrics is empty / None.
    """
    if not metrics:
        return 0.0
    total_return = (float(final_value) / INITIAL_CASH) - 1.0
    sharpe = float(metrics.get('sharpe') or 0.0)
    mdd = abs(float(metrics.get('mdd') or 0.0))
    sharpe_norm = sharpe / 4.0
    return 0.4 * total_return + 0.4 * sharpe_norm - 0.2 * mdd


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


def _run_backtest(df_signals):
    """Run simple_backtest and return (final_value, metrics)."""
    if df_signals is None or len(df_signals) == 0:
        return INITIAL_CASH, {}
    result = simple_backtest(df_signals, initial_cash=INITIAL_CASH,
                             fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT)
    return float(result.get('final_value', INITIAL_CASH)), result.get('metrics') or {}


def _grid_search_is(strategy_fn, df_is, param_grid):
    """[L4 FIX] IS grid search returning (best_combo, best_is_score, best_is_metrics).

    Iterates all valid (ema_short < ema_long) combos on the IS slice and picks
    the one with the highest composite score.
    """
    combos = list(itertools.product(
        param_grid['ema_short'],
        param_grid['ema_long'],
        param_grid['adx_trend_threshold'],
        param_grid['macro_ema_long'],
    ))
    best_score = float('-inf')
    best_combo = None
    best_metrics = {}
    best_fv = INITIAL_CASH

    for ema_short, ema_long, adx_thresh, macro_ema_long in combos:
        if ema_short >= ema_long:
            continue  # invalid — skip
        params = {
            'ema_short': ema_short,
            'ema_long': ema_long,
            'adx_trend_threshold': adx_thresh,
            'macro_ema_long': macro_ema_long,
        }
        try:
            df_sig = strategy_fn(df_is.copy(), **params)
            fv, metrics = _run_backtest(df_sig)
            score = _composite_score(metrics, fv)
            if score > best_score:
                best_score = score
                best_combo = params
                best_metrics = metrics
                best_fv = fv
        except Exception:
            continue

    return best_combo, best_score, best_metrics, best_fv


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
    valid_combos = sum(
        1 for es, el in itertools.product(param_grid['ema_short'], param_grid['ema_long'])
        if es < el
    ) * len(param_grid['adx_trend_threshold']) * len(param_grid['macro_ema_long'])
    print(f'[auto_tuner] Grid: {param_grid}')
    print(f'[auto_tuner] Valid combos (ema_short < ema_long): {valid_combos}')

    all_results = []
    for ticker in TICKERS:
        print(f'[auto_tuner] Fetching {COUNT_30D_1H} bars 1h for {ticker}...')
        df = fetch_ohlcv(ticker=ticker, interval='minute60', count=COUNT_30D_1H, use_db_first=True)
        if df is None or len(df) < 100:
            print(f'[auto_tuner] Skip {ticker}: insufficient data')
            continue
        if 'time' not in df.columns and df.index.name is not None:
            df = df.reset_index()

        # ── [L4 FIX] IS/OOS 70/30 walk-forward split ─────────────────────────
        n = len(df)
        is_end = int(n * IS_RATIO)
        df_is = df.iloc[:is_end].copy()
        df_oos = df.iloc[is_end:].copy()
        print(f'[auto_tuner] {ticker}: {n} bars total → IS={is_end}, OOS={n - is_end}')

        # ── IS grid search (composite score) ─────────────────────────────────
        best_combo, is_score, is_metrics, is_fv = _grid_search_is(strategy_fn, df_is, param_grid)
        if best_combo is None:
            print(f'[auto_tuner] {ticker}: no valid IS result, skipping')
            continue
        print(
            f'[auto_tuner] {ticker} IS best: {best_combo} '
            f'(score={is_score:.4f}, fv={is_fv:.0f}, '
            f'sharpe={is_metrics.get("sharpe", 0):.2f}, mdd={is_metrics.get("mdd", 0):.2%})'
        )

        # ── OOS validation ────────────────────────────────────────────────────
        oos_score = 0.0
        oos_metrics = {}
        oos_fv = INITIAL_CASH
        try:
            df_sig_oos = strategy_fn(df_oos.copy(), **best_combo)
            oos_fv, oos_metrics = _run_backtest(df_sig_oos)
            oos_score = _composite_score(oos_metrics, oos_fv)
        except Exception as e:
            print(f'[auto_tuner] {ticker} OOS backtest failed: {e}')

        print(
            f'[auto_tuner] {ticker} OOS:  score={oos_score:.4f}, fv={oos_fv:.0f}, '
            f'sharpe={oos_metrics.get("sharpe", 0):.2f}, mdd={oos_metrics.get("mdd", 0):.2%}'
        )
        gate_threshold = is_score * OOS_GATE
        if is_score > 0 and oos_score < gate_threshold:
            print(
                f'[auto_tuner] WARNING: {ticker} OOS score ({oos_score:.4f}) < '
                f'IS×{OOS_GATE} ({gate_threshold:.4f}) — possible overfit; saving anyway'
            )

        all_results.append({
            'ticker': ticker,
            'combo': best_combo,
            'is_score': is_score,
            'is_fv': is_fv,
            'is_metrics': is_metrics,
            'oos_score': oos_score,
            'oos_fv': oos_fv,
            'oos_metrics': oos_metrics,
        })

    if not all_results:
        print('[auto_tuner] No results.')
        return

    # ── Select best combo across tickers (by IS composite score) ────────────
    best = max(all_results, key=lambda x: float(x.get('is_score') or 0))
    session = get_session()
    try:
        tr = TuningRun(
            combo=best['combo'],
            metrics={
                'is_score': best['is_score'],
                'is_final_value': best['is_fv'],
                'is_sharpe': best['is_metrics'].get('sharpe'),
                'is_mdd': best['is_metrics'].get('mdd'),
                'oos_score': best['oos_score'],
                'oos_final_value': best['oos_fv'],
                'oos_sharpe': best['oos_metrics'].get('sharpe'),
                'oos_mdd': best['oos_metrics'].get('mdd'),
                'ticker': best['ticker'],
            },
        )
        session.add(tr)
        session.commit()
        print(
            f'[auto_tuner] Best combo saved: {best["combo"]} '
            f'(IS score={best["is_score"]:.4f}, OOS score={best["oos_score"]:.4f}, '
            f'ticker={best["ticker"]})'
        )
        print(f'[auto_tuner] macro_ema_long selected: {best["combo"].get("macro_ema_long", "N/A")}')
    except Exception as e:
        print('[auto_tuner] Failed to save best to TuningRun:', e)
        session.rollback()
    finally:
        session.close()

    # [H3] 새 파라미터가 저장됐으므로 param_manager 캐시를 즉시 무효화.
    # 다음 get_best_params() 호출 시 방금 저장된 최신 combo를 반영한다.
    try:
        from trading_bot.param_manager import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    # ── AI Reviewer 순차 실행: 튜닝 완료 직후 브리핑 생성 ──────────────────
    # 독립 cron 대신 여기서 직접 호출하여 레이스 컨디션 완전 제거.
    # 예외 발생 시 튜닝 결과에 영향 없이 오류만 출력.
    try:
        from trading_bot.tasks.ai_reviewer import run_ai_reviewer
        print('[auto_tuner] AI Reviewer 순차 실행 시작...')
        run_ai_reviewer()
        print('[auto_tuner] AI Reviewer 완료')
    except Exception as e:
        print(f'[auto_tuner] AI Reviewer 실행 실패 (튜닝 결과에는 영향 없음): {e}')


if __name__ == '__main__':
    main()
