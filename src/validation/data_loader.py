import os
import pandas as pd
import numpy as np
import math
from datetime import datetime, timezone, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

def _get_aligned_dataframe(start_time: datetime, end_time: datetime, client: StockHistoricalDataClient, timeframe_str: str = "15m"):
    if timeframe_str == "1h":
        tf = TimeFrame(1, TimeFrameUnit.Hour)
    else:
        tf = TimeFrame(15, TimeFrameUnit.Minute)
        
    spy_req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=tf, start=start_time, end=end_time)
    spy_resp = client.get_stock_bars(spy_req)
    spy_raw = spy_resp.data.get("SPY", [])
    
    qqq_req = StockBarsRequest(symbol_or_symbols="QQQ", timeframe=tf, start=start_time, end=end_time)
    qqq_resp = client.get_stock_bars(qqq_req)
    qqq_raw = qqq_resp.data.get("QQQ", [])
    
    df_spy = pd.DataFrame([{
        "timestamp": b.timestamp,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
        "trade_count": int(getattr(b, "trade_count", 0) or 0)
    } for b in spy_raw])
    
    df_qqq = pd.DataFrame([{
        "timestamp": b.timestamp,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
        "trade_count": int(getattr(b, "trade_count", 0) or 0)
    } for b in qqq_raw])
    
    if df_spy.empty or df_qqq.empty:
        return pd.DataFrame()
        
    df_spy = df_spy.set_index("timestamp")
    df_qqq = df_qqq.set_index("timestamp")
    
    merged = df_spy.join(df_qqq, how="inner", lsuffix="_spy", rsuffix="_qqq")
    
    # Calculate RSC
    merged["rsc"] = merged["close_qqq"] / merged["close_spy"]
    merged["rsc_mean_100"] = merged["rsc"].rolling(window=100).mean()
    merged["rsc_std_100"] = merged["rsc"].rolling(window=100).std()
    merged["z_rsc"] = (merged["rsc"] - merged["rsc_mean_100"]) / merged["rsc_std_100"]
    
    merged["z_rsc"] = merged["z_rsc"].fillna(0.0)
    merged["rsc_std_100"] = merged["rsc_std_100"].fillna(0.001)
    
    peak = merged["rsc"].cummax()
    merged["max_dd_rsc"] = (merged["rsc"] - peak) / peak * 100
    merged["max_dd_rsc"] = merged["max_dd_rsc"].fillna(0.0)
    
    for suffix in ["_spy", "_qqq"]:
        h = merged[f"high{suffix}"]
        l = merged[f"low{suffix}"]
        c_prev = merged[f"close{suffix}"].shift(1)
        tr = pd.concat([h - l, (h - c_prev).abs(), (c_prev - l).abs()], axis=1).max(axis=1)
        merged[f"atr_14{suffix}"] = tr.rolling(window=14).mean().fillna(0.0)
        
    return merged

def fetch_alpaca_bars(symbol: str, start_time: datetime, end_time: datetime, client: StockHistoricalDataClient, timeframe_str: str = "15m") -> list[dict]:
    merged = _get_aligned_dataframe(start_time, end_time, client, timeframe_str)
    if merged.empty:
        return []
        
    normalized_bars = []
    fetch_ts = datetime.now(timezone.utc).isoformat()
    
    for ts, row in merged.iterrows():
        if symbol == "SPY":
            normalized_bars.append({
                "timestamp": ts,
                "open": float(row["open_spy"]),
                "high": float(row["high_spy"]),
                "low": float(row["low_spy"]),
                "close": float(row["close_spy"]),
                "volume": float(row["volume_spy"]),
                "trade_count": int(row["trade_count_spy"]),
                "sma_macro": float(row["close_spy"]),
                "vwap": float(row["close_spy"]),
                "obv": 0.0,
                "obv_sma20": 0.0,
                "z_rsc": float(row["z_rsc"]),
                "rsc": float(row["rsc"]),
                "rsc_std_100": float(row["rsc_std_100"]),
                "max_dd_rsc": float(row["max_dd_rsc"]),
                "close_paired": float(row["close_qqq"]),
                "atr_paired": float(row["atr_14_qqq"]),
                "atr_14": float(row["atr_14_spy"]),
                "data_provenance": {
                    "source": "alpaca_historical",
                    "fetch_timestamp": fetch_ts,
                    "normalization_version": "v2_hard_norm"
                }
            })
        else: # QQQ
            normalized_bars.append({
                "timestamp": ts,
                "open": float(row["open_qqq"]),
                "high": float(row["high_qqq"]),
                "low": float(row["low_qqq"]),
                "close": float(row["close_qqq"]),
                "volume": float(row["volume_qqq"]),
                "trade_count": int(row["trade_count_qqq"]),
                "sma_macro": float(row["close_qqq"]),
                "vwap": float(row["close_qqq"]),
                "obv": 0.0,
                "obv_sma20": 0.0,
                "z_rsc": float(row["z_rsc"]),
                "rsc": float(row["rsc"]),
                "rsc_std_100": float(row["rsc_std_100"]),
                "max_dd_rsc": float(row["max_dd_rsc"]),
                "close_paired": float(row["close_spy"]),
                "atr_paired": float(row["atr_14_spy"]),
                "atr_14": float(row["atr_14_qqq"]),
                "data_provenance": {
                    "source": "alpaca_historical",
                    "fetch_timestamp": fetch_ts,
                    "normalization_version": "v2_hard_norm"
                }
            })
            
    return normalized_bars

def generate_mock_bars(symbol: str, length: int = 100, anomaly: str = None) -> list[dict]:
    # Mock data aligned QQQ and SPY
    bars_spy = []
    bars_qqq = []
    base_time = datetime(2026, 5, 18, 16, 30, tzinfo=timezone.utc)
    
    for i in range(length):
        timestamp = base_time + timedelta(minutes=15 * i)
        spy_close = 100.0 + i * 0.1 + math.sin(i * 0.1) * 2.0
        qqq_close = 100.0 + i * 0.15 + math.sin(i * 0.15) * 5.0
        
        bars_spy.append({
            "timestamp": timestamp,
            "open": spy_close - 0.1,
            "high": spy_close + 0.5,
            "low": spy_close - 0.5,
            "close": spy_close,
            "volume": 10000.0 + i * 10,
            "trade_count": 100 + i
        })
        
        bars_qqq.append({
            "timestamp": timestamp,
            "open": qqq_close - 0.2,
            "high": qqq_close + 1.0,
            "low": qqq_close - 1.0,
            "close": qqq_close,
            "volume": 20000.0 + i * 20,
            "trade_count": 200 + i
        })
        
    df_spy = pd.DataFrame(bars_spy).set_index("timestamp")
    df_qqq = pd.DataFrame(bars_qqq).set_index("timestamp")
    
    merged = df_spy.join(df_qqq, how="inner", lsuffix="_spy", rsuffix="_qqq")
    
    merged["rsc"] = merged["close_qqq"] / merged["close_spy"]
    merged["rsc_mean_100"] = merged["rsc"].rolling(window=100).mean().fillna(merged["rsc"])
    merged["rsc_std_100"] = merged["rsc"].rolling(window=100).std().fillna(0.001)
    merged["z_rsc"] = (merged["rsc"] - merged["rsc_mean_100"]) / merged["rsc_std_100"]
    merged["z_rsc"] = merged["z_rsc"].fillna(0.0)
    
    peak = merged["rsc"].cummax()
    merged["max_dd_rsc"] = (merged["rsc"] - peak) / peak * 100
    merged["max_dd_rsc"] = merged["max_dd_rsc"].fillna(0.0)
    
    for suffix in ["_spy", "_qqq"]:
        h = merged[f"high{suffix}"]
        l = merged[f"low{suffix}"]
        c_prev = merged[f"close{suffix}"].shift(1)
        tr = pd.concat([h - l, (h - c_prev).abs(), (c_prev - l).abs()], axis=1).max(axis=1)
        merged[f"atr_14{suffix}"] = tr.rolling(window=14).mean().fillna(0.1)
        
    normalized_bars = []
    fetch_ts = base_time.isoformat()
    
    for ts, row in merged.iterrows():
        if symbol == "SPY":
            normalized_bars.append({
                "timestamp": ts,
                "open": float(row["open_spy"]),
                "high": float(row["high_spy"]),
                "low": float(row["low_spy"]),
                "close": float(row["close_spy"]),
                "volume": float(row["volume_spy"]),
                "trade_count": int(row["trade_count_spy"]),
                "sma_macro": float(row["close_spy"]),
                "vwap": float(row["close_spy"]),
                "obv": 0.0,
                "obv_sma20": 0.0,
                "z_rsc": float(row["z_rsc"]),
                "rsc": float(row["rsc"]),
                "rsc_std_100": float(row["rsc_std_100"]),
                "max_dd_rsc": float(row["max_dd_rsc"]),
                "close_paired": float(row["close_qqq"]),
                "atr_paired": float(row["atr_14_qqq"]),
                "atr_14": float(row["atr_14_spy"]),
                "data_provenance": {
                    "source": "mock_generator",
                    "fetch_timestamp": fetch_ts,
                    "normalization_version": "v2_hard_norm"
                }
            })
        else:
            normalized_bars.append({
                "timestamp": ts,
                "open": float(row["open_qqq"]),
                "high": float(row["high_qqq"]),
                "low": float(row["low_qqq"]),
                "close": float(row["close_qqq"]),
                "volume": float(row["volume_qqq"]),
                "trade_count": int(row["trade_count_qqq"]),
                "sma_macro": float(row["close_qqq"]),
                "vwap": float(row["close_qqq"]),
                "obv": 0.0,
                "obv_sma20": 0.0,
                "z_rsc": float(row["z_rsc"]),
                "rsc": float(row["rsc"]),
                "rsc_std_100": float(row["rsc_std_100"]),
                "max_dd_rsc": float(row["max_dd_rsc"]),
                "close_paired": float(row["close_spy"]),
                "atr_paired": float(row["atr_14_spy"]),
                "atr_14": float(row["atr_14_qqq"]),
                "data_provenance": {
                    "source": "mock_generator",
                    "fetch_timestamp": fetch_ts,
                    "normalization_version": "v2_hard_norm"
                }
            })
            
    return normalized_bars
