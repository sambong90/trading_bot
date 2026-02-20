import pandas as pd

def sma(series, window):
    return series.rolling(window).mean()

def generate_sma_signals(df, short=10, long=50):
    df = df.copy()
    df['sma_s'] = sma(df['close'], short)
    df['sma_l'] = sma(df['close'], long)
    df['signal'] = 0
    cond_long = (df['sma_s'] > df['sma_l']) & (df['sma_s'].shift(1) <= df['sma_l'].shift(1))
    cond_exit = (df['sma_s'] < df['sma_l']) & (df['sma_s'].shift(1) >= df['sma_l'].shift(1))
    df.loc[cond_long, 'signal'] = 1
    df.loc[cond_exit, 'signal'] = -1
    return df[['time','open','high','low','close','volume','sma_s','sma_l','signal']]
