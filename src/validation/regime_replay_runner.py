import os
import sys
import json
import argparse
import datetime
import pandas as pd
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.validation.regime_classifier import classify_regime_for_period
from src.validation.forward_tester import run_multi_day_backtest
from alpaca.data.historical import StockHistoricalDataClient

# Define the historical epochs to test
EPOCHS = {
    "2020_Crash": {
        "start": "2020-03-01",
        "end": "2020-06-30",
        "representative_start": "2020-03-01",
        "representative_end": "2020-05-30",
        "description": "2020 Crash & Recovery"
    },
    "2021_Bull": {
        "start": "2021-01-01",
        "end": "2021-12-31",
        "representative_start": "2021-01-01",
        "representative_end": "2021-03-31",
        "description": "2021 Momentum Expansion"
    },
    "2022_Bear": {
        "start": "2022-01-01",
        "end": "2022-12-31",
        "representative_start": "2022-01-01",
        "representative_end": "2022-03-31",
        "description": "2022 Bear Market"
    },
    "2023_Tech": {
        "start": "2023-01-01",
        "end": "2023-12-31",
        "representative_start": "2023-01-01",
        "representative_end": "2023-03-31",
        "description": "2023 AI / Tech Leadership"
    },
    "2024_Choppy": {
        "start": "2024-01-01",
        "end": "2024-12-31",
        "representative_start": "2024-01-01",
        "representative_end": "2024-03-31",
        "description": "2024 Rotational / Choppy Market"
    },
    "2025_Current": {
        "start": "2025-01-01",
        "end": "2026-04-01",
        "representative_start": "2025-01-01",
        "representative_end": "2025-03-31",
        "description": "2025+ Current Market Structure"
    }
}

def main():
    parser = argparse.ArgumentParser(description="Large-Scale Historical Replay Runner")
    parser.add_argument("--full", action="store_true", help="Run full epoch windows instead of 90-day representative windows")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY_ID")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("[!] Error: ALPACA_API_KEY_ID and ALPACA_SECRET_KEY must be set in your .env")
        sys.exit(1)

    client = StockHistoricalDataClient(api_key, secret_key)
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)

    # 1. Clear previous trade dataset to prevent compounding duplicate runs
    csv_path = os.path.join(out_dir, "trade_dataset.csv")
    if os.path.exists(csv_path):
        try:
            os.remove(csv_path)
            print("[*] Cleared old trade dataset.")
        except Exception as e:
            print(f"[!] Error clearing old trade dataset: {e}")

    # Initialize master list for all trades across epochs
    all_trades = []
    
    # 2. Iterate through each epoch, classify regimes, and run backtest
    for epoch_name, config in EPOCHS.items():
        start = config["representative_start"] if not args.full else config["start"]
        end = config["representative_end"] if not args.full else config["end"]
        desc = config["description"]

        print(f"\n=======================================================")
        print(f"PROCESSING EPOCH: {epoch_name} ({desc})")
        print(f"Period: {start} to {end}")
        print(f"=======================================================")

        # Classify regimes
        print("[*] Running regime classification for epoch...")
        regime_df = classify_regime_for_period(start, end, client)
        if regime_df.empty:
            print(f"[!] Warning: Regime classification returned no data for {start} to {end}. Skipping.")
            continue
            
        # Map date strings to regime labels and transition zones
        regime_map = {}
        for _, row in regime_df.iterrows():
            d_str = row["date"].strftime("%Y-%m-%d")
            regime_map[d_str] = {
                "regime": row["regime"],
                "is_transition": row["is_transition"]
            }

        # Execute backtest
        print("[*] Running simulated historical replay...")
        try:
            epoch_trades = run_multi_day_backtest(start, end, use_llm=False, initial_value=100000.0)
        except Exception as e:
            print(f"[!] Replay failed for epoch {epoch_name}: {e}")
            continue

        # Tag trades with regime and transition zone
        for t in epoch_trades:
            t_date = t["date"]
            r_info = regime_map.get(t_date, {"regime": "CHOPPY_ROTATIONAL", "is_transition": False})
            t["regime"] = r_info["regime"]
            t["is_transition"] = r_info["is_transition"]
            t["epoch"] = epoch_name
            all_trades.append(t)

        print(f"[+] Completed epoch {epoch_name}. Logged {len(epoch_trades)} trades.")

    if not all_trades:
        print("[!] No trades executed across all epochs. Exiting.")
        sys.exit(0)

    # 3. Save combined tagged trades to trade_dataset_tagged.csv
    df_all = pd.DataFrame(all_trades)
    tagged_csv_path = os.path.join(out_dir, "trade_dataset.csv")
    df_all.to_csv(tagged_csv_path, index=False)
    print(f"\n[+] Saved {len(df_all)} total trades to {tagged_csv_path}")

    # 4. Generate rolling 90-day overlapping windows analysis
    print("\n[*] Generating rolling 90-day overlapping windows analysis...")
    generate_rolling_windows_analysis(df_all, out_dir)

    # 5. Call Performance Attributor to compile final attribution report
    print("\n[*] Invoking performance attributor to generate reports...")
    from src.validation.performance_attributor import run_attribution
    run_attribution()
    print("[+] All validation tasks completed successfully.")

def generate_rolling_windows_analysis(df, out_dir):
    """
    Groups trades into overlapping 90-day windows (stepping by 30 days) and computes metrics.
    """
    df["datetime"] = pd.to_datetime(df["date"])
    min_date = df["datetime"].min()
    max_date = df["datetime"].max()
    
    window_duration = datetime.timedelta(days=90)
    step_duration = datetime.timedelta(days=30)
    
    current_start = min_date
    window_records = []
    
    while current_start + window_duration <= max_date + step_duration:
        current_end = current_start + window_duration
        # Filter trades in the current 90-day window
        df_win = df[(df["datetime"] >= current_start) & (df["datetime"] < current_end)]
        
        win_start_str = current_start.strftime("%Y-%m-%d")
        win_end_str = current_end.strftime("%Y-%m-%d")
        
        tc = len(df_win)
        if tc > 0:
            wins = df_win[df_win["pnl"] > 0]
            losses = df_win[df_win["pnl"] <= 0]
            win_rate = len(wins) / tc * 100.0
            
            total_gains = wins["pnl"].sum()
            total_losses = abs(losses["pnl"].sum())
            profit_factor = total_gains / total_losses if total_losses > 0 else (total_gains if total_gains > 0 else 1.0)
            
            avg_r = df_win["realized_r_multiple"].mean() if "realized_r_multiple" in df_win.columns else df_win["r_multiple"].mean()
            net_profit = df_win["pnl"].sum()
            
            # Cumulative drawdown in this window
            df_win = df_win.sort_values("datetime")
            cum_pnl = df_win["pnl"].cumsum()
            peak = cum_pnl.cummax()
            dd = cum_pnl - peak
            max_dd = dd.min() if len(dd) > 0 else 0.0
            
            window_records.append({
                "window_start": win_start_str,
                "window_end": win_end_str,
                "trade_count": tc,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "avg_r": avg_r,
                "net_profit": net_profit,
                "max_drawdown": max_dd
            })
            
        current_start += step_duration
        
    df_windows = pd.DataFrame(window_records)
    windows_csv_path = os.path.join(out_dir, "rolling_windows_performance.csv")
    df_windows.to_csv(windows_csv_path, index=False)
    print(f"[+] Saved rolling 90-day windows metrics to {windows_csv_path}")

if __name__ == "__main__":
    main()
