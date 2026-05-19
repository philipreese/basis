import os
import math
import datetime
import pandas as pd
import numpy as np
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

def classify_regime_for_period(start_date_str: str, end_date_str: str, client: StockHistoricalDataClient) -> pd.DataFrame:
    """
    Fetches daily SPY bars including a 300-day warm-up period, calculates rolling SPY metrics,
    and returns a DataFrame containing the daily regime classifications and transition zone tags
    for the dates between start_date_str and end_date_str (inclusive).
    """
    start_dt = datetime.datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.datetime.strptime(end_date_str, "%Y-%m-%d")
    
    # Warm-up of 300 calendar days
    warmup_start = start_dt - datetime.timedelta(days=300)
    
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Day,
        start=warmup_start,
        end=end_dt
    )
    
    try:
        res = client.get_stock_bars(req)
        bars = res.data.get("SPY", [])
    except Exception as e:
        print(f"[!] Error fetching daily SPY bars for regime classification: {e}")
        bars = []
        
    if not bars:
        return pd.DataFrame()
        
    df = pd.DataFrame([{
        "date": b.timestamp.date(),
        "close": float(b.close),
        "high": float(b.high),
        "low": float(b.low),
        "open": float(b.open),
        "volume": float(b.volume)
    } for b in bars])
    
    df = df.sort_values("date").reset_index(drop=True)
    
    # Calculate rolling SPY metrics
    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    df["rolling_vol"] = df["log_return"].rolling(window=20).std() * math.sqrt(252) * 100.0
    df["sma_50"] = df["close"].rolling(window=50).mean()
    df["sma_200"] = df["close"].rolling(window=200).mean()
    df["rolling_return_20"] = ((df["close"] - df["close"].shift(20)) / df["close"].shift(20)) * 100.0
    df["max_252"] = df["close"].rolling(window=252).max()
    df["drawdown_252"] = ((df["close"] - df["max_252"]) / df["max_252"]) * 100.0
    
    # Perform raw classifications
    regimes = []
    for idx, row in df.iterrows():
        # Handle NaN warm-ups
        if pd.isna(row["rolling_vol"]) or pd.isna(row["sma_200"]) or pd.isna(row["drawdown_252"]):
            regimes.append("CHOPPY_ROTATIONAL")
            continue
            
        vol = row["rolling_vol"]
        close = row["close"]
        sma_50 = row["sma_50"]
        sma_200 = row["sma_200"]
        ret_20 = row["rolling_return_20"]
        
        if vol > 22.0:
            regimes.append("HIGH_VOLATILITY")
        elif close < sma_200 and ret_20 < -3.0:
            regimes.append("TRENDING_BEAR")
        elif close > sma_50 and sma_50 > sma_200:
            if ret_20 > 4.0:
                regimes.append("MOMENTUM_EXPANSION")
            else:
                regimes.append("TRENDING_BULL")
        else:
            regimes.append("CHOPPY_ROTATIONAL")
            
    df["regime"] = regimes
    
    # Regime transition zone tagging (first 10% of a continuous regime segment)
    # Identify continuous blocks of the same regime
    df["regime_change"] = df["regime"] != df["regime"].shift(1)
    df["segment_id"] = df["regime_change"].cumsum()
    
    is_transition_list = []
    # Group by segments to tag transition zones
    for seg_id, group in df.groupby("segment_id"):
        n = len(group)
        transition_cutoff = max(1, int(math.ceil(0.10 * n)))
        for i in range(n):
            if i < transition_cutoff:
                is_transition_list.append(True)
            else:
                is_transition_list.append(False)
                
    df["is_transition"] = is_transition_list
    
    # Filter back to target range
    target_start_date = start_dt.date()
    target_end_date = end_dt.date()
    df_filtered = df[(df["date"] >= target_start_date) & (df["date"] <= target_end_date)].copy()
    
    return df_filtered.reset_index(drop=True)
