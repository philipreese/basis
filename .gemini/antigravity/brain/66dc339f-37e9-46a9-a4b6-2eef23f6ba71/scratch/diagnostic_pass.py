import os
import json
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Ensure workspace root is in path
workspace_root = os.getcwd()
if workspace_root not in sys.path:
    sys.path.insert(0, workspace_root)

from src.validation.data_loader import fetch_alpaca_bars
from alpaca.data.historical import StockHistoricalDataClient

REGIMES = [
    {
        "id": 1,
        "name": "REGIME_1_2020 (Corona Crash & Recovery)",
        "start_date": "2020-02-15",
        "end_date": "2020-06-15",
        "volatility": "high"
    },
    {
        "id": 2,
        "name": "REGIME_2_2022 (Sustained Bear Market)",
        "start_date": "2022-01-01",
        "end_date": "2022-12-31",
        "volatility": "high"
    },
    {
        "id": 3,
        "name": "REGIME_3_2023 (Bull / Tech Recovery)",
        "start_date": "2023-02-15",
        "end_date": "2023-07-15",
        "volatility": "low"
    },
    {
        "id": 4,
        "name": "REGIME_4_2018 (Q4 Market Correction)",
        "start_date": "2018-10-01",
        "end_date": "2018-12-31",
        "volatility": "high"
    },
    {
        "id": 5,
        "name": "REGIME_5_2024 (Late Summer Chop / Pre-Election)",
        "start_date": "2024-07-01",
        "end_date": "2024-10-31",
        "volatility": "low"
    }
]

def adf_test_approx(series):
    """
    Performs a simplified Dickey-Fuller test using OLS:
    dY_t = beta * Y_{t-1} + c + e_t
    Returns beta, t-statistic, and whether it rejects the unit root at 5% significance (DF critical value approx -2.89).
    """
    y = series.values
    dy = np.diff(y)
    y_lag = y[:-1]
    
    # OLS regression: Y_new = X * beta
    # X has two columns: y_lag and a column of ones for intercept
    X = np.vstack([y_lag, np.ones_like(y_lag)]).T
    Y = dy
    
    # beta = (X^T * X)^-1 * X^T * Y
    try:
        beta_vec, residuals, rank, s = np.linalg.lstsq(X, Y, rcond=None)
        beta = float(beta_vec[0])
        c = float(beta_vec[1])
        
        # Residuals standard error
        n = len(Y)
        k = X.shape[1]
        df_deg = n - k
        e = Y - X.dot(beta_vec)
        rss = np.sum(e**2)
        s2 = rss / df_deg
        
        # Standard error of beta
        cov = s2 * np.linalg.inv(X.T.dot(X))
        se_beta = np.sqrt(cov[0, 0])
        
        t_stat = float(beta / se_beta)
        # Critical value for DF test with constant (no trend) at 5% is approx -2.88
        is_stationary = bool(t_stat < -2.88)
        
        # Half life: -ln(2)/ln(1+beta) or -ln(2)/beta if beta is small
        if beta < 0:
            half_life = float(-np.log(2) / np.log(1 + beta) if (1 + beta) > 0 else -np.log(2) / beta)
        else:
            half_life = float(np.inf)
            
        return beta, t_stat, is_stationary, half_life
    except Exception as err:
        print(f"ADF approximation failed: {err}")
        return 0.0, 0.0, False, float(np.inf)

def run_diagnostics():
    load_dotenv(os.path.join(workspace_root, ".env"))
    api_key = os.getenv("ALPACA_API_KEY_ID")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("[!] ERROR: ALPACA_API_KEY_ID or ALPACA_SECRET_KEY not found in .env")
        sys.exit(1)
        
    client = StockHistoricalDataClient(api_key, secret_key)
    
    non_stationary_regimes_count = 0
    results_summary = []
    
    print("="*60)
    print("PHASE 31: PRE-IMPLEMENTATION DIAGNOSTIC PASS")
    print("="*60)
    
    for r in REGIMES:
        name = r["name"]
        start_str = r["start_date"]
        end_str = r["end_date"]
        vol_type = r["volatility"]
        
        start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(seconds=1)
        padded_start = start - timedelta(days=35)
        
        print(f"\nFetching bars for {name} ({start_str} to {end_str})...")
        spy_bars = fetch_alpaca_bars("SPY", padded_start, end, client, "1h")
        qqq_bars = fetch_alpaca_bars("QQQ", padded_start, end, client, "1h")
        
        df_spy = pd.DataFrame(spy_bars)
        df_qqq = pd.DataFrame(qqq_bars)
        
        # Align by timestamp
        df_spy = df_spy.rename(columns={"close": "spy_close"}).set_index("timestamp")
        df_qqq = df_qqq.rename(columns={"close": "qqq_close"}).set_index("timestamp")
        
        merged = df_spy.join(df_qqq, how="inner", lsuffix="_spy", rsuffix="_qqq")
        
        # Calculate RSC
        merged["rsc"] = merged["qqq_close"] / merged["spy_close"]
        merged["rsc_mean_100"] = merged["rsc"].rolling(window=100).mean()
        merged["rsc_std_100"] = merged["rsc"].rolling(window=100).std()
        merged["z_rsc"] = (merged["rsc"] - merged["rsc_mean_100"]) / merged["rsc_std_100"]
        
        # Drop the warm-up padding
        active_df = merged.loc[start:]
        active_df = active_df.dropna(subset=["z_rsc"])
        
        # 1. Distribution of RSC
        rsc_mean = active_df["rsc"].mean()
        rsc_std = active_df["rsc"].std()
        rsc_skew = active_df["rsc"].skew()
        rsc_kurt = active_df["rsc"].kurt()
        
        # Max drawdown of RSC
        peak = active_df["rsc"].cummax()
        drawdown = (active_df["rsc"] - peak) / peak * 100
        max_dd_rsc = drawdown.min()
        
        # 2. Stationarity of Z_RSC
        beta, t_stat, is_stationary, half_life = adf_test_approx(active_df["z_rsc"])
        
        # 3. Variance of spread (represented by rolling 100h std of RSC)
        spread_var_mean = active_df["rsc_std_100"].mean()
        spread_var_max = active_df["rsc_std_100"].max()
        
        print(f"--- {name} Results ---")
        print(f"  RSC Close Distribution: Mean={rsc_mean:.4f}, Std={rsc_std:.4f}, Skew={rsc_skew:.2f}, Kurt={rsc_kurt:.2f}")
        print(f"  RSC Max Drawdown: {max_dd_rsc:.2f}%")
        print(f"  Z_RSC Dickey-Fuller Approx: Beta={beta:.6f}, t-stat={t_stat:.2f}")
        print(f"  Stationary? {is_stationary} (Half-life: {half_life:.2f} hours)")
        print(f"  Spread Volatility (100h std): Mean={spread_var_mean:.4f}, Max={spread_var_max:.4f}")
        
        if not is_stationary:
            non_stationary_regimes_count += 1
            
        results_summary.append({
            "id": int(r["id"]),
            "name": name,
            "volatility": vol_type,
            "rsc_mean": float(rsc_mean),
            "rsc_std": float(rsc_std),
            "max_dd_rsc": float(max_dd_rsc),
            "beta": float(beta),
            "t_stat": float(t_stat),
            "is_stationary": bool(is_stationary),
            "half_life": float(half_life),
            "spread_vol_mean": float(spread_var_mean),
            "spread_vol_max": float(spread_var_max)
        })
        
    print("\n" + "="*60)
    print("DIAGNOSTIC SUMMARY & STATIONARITY TEST")
    print("="*60)
    print(f"Non-stationary regimes: {non_stationary_regimes_count} / {len(REGIMES)}")
    
    if non_stationary_regimes_count > 1:
        print("[!] FLAG IN LOGS: The spread process is NON-STATIONARY in more than one regime!")
        print("This suggests structural instability during specific macroeconomic periods.")
    else:
        print("[*] PASS: The spread process is stationary in most regimes.")
        
    # Write summary to out/diagnostic_summary.json for validation engine consumption
    diag_file = os.path.join(workspace_root, "out", "diagnostic_summary.json")
    with open(diag_file, "w") as f:
        json.dump({
            "non_stationary_regimes_count": int(non_stationary_regimes_count),
            "results": results_summary,
            "flagged_unstable": bool(non_stationary_regimes_count > 1)
        }, f, indent=4)
        
    print(f"[!] Saved diagnostic summary to {diag_file}")

if __name__ == "__main__":
    run_diagnostics()
