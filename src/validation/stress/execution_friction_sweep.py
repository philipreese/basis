import os
import pandas as pd
import numpy as np

def run_friction_sweep():
    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "out"))
    trade_dataset_path = os.path.join(out_dir, "trade_dataset.csv")
    
    if not os.path.exists(trade_dataset_path):
        print(f"[!] Error: {trade_dataset_path} does not exist. Run forward_tester first.")
        return
        
    df = pd.read_csv(trade_dataset_path)
    if df.empty:
        print("[!] Trade dataset is empty.")
        return

    # Results table
    records = []
    
    # We will sweep:
    # 1. Slippage Multipliers
    # 2. Latency Delays (ms)
    # 3. Partial Fills
    
    slippage_mults = [1.0, 1.5, 2.0, 3.0]
    delays = [0, 100, 250, 500]
    fills = [1.0, 0.75, 0.50]
    
    # Baseline Metrics
    base_trades = df["pnl"].values
    base_profit_factor = calculate_profit_factor(base_trades)
    base_expectancy = np.mean(base_trades)
    
    records.append({
        "scenario_type": "Baseline",
        "parameter_value": "1.0 / 0ms / 100%",
        "trade_count": len(df),
        "net_profit": np.sum(base_trades),
        "profit_factor": base_profit_factor,
        "expectancy": base_expectancy
    })
    
    # 1. Slippage Multiplier Sweep (Delay = 0ms, Fill = 100%)
    for sm in slippage_mults[1:]:
        pnl_adj = []
        for _, row in df.iterrows():
            # new_pnl = net_pnl - (S - 1) * slippage_estimate
            net_pnl = row["pnl"]
            slip_est = row["slippage_estimate"]
            pnl_adj.append(net_pnl - (sm - 1.0) * slip_est)
            
        pnl_adj = np.array(pnl_adj)
        records.append({
            "scenario_type": "Slippage Multiplier",
            "parameter_value": f"{sm}x",
            "trade_count": len(df),
            "net_profit": np.sum(pnl_adj),
            "profit_factor": calculate_profit_factor(pnl_adj),
            "expectancy": np.mean(pnl_adj)
        })
        
    # 2. Latency Delay Sweep (Slippage = 1.0x, Fill = 100%)
    # penalty = (Delay / 100.0) * slippage_estimate * (spread_proxy / 0.01)
    for d in delays[1:]:
        pnl_adj = []
        for _, row in df.iterrows():
            net_pnl = row["pnl"]
            slip_est = row["slippage_estimate"]
            spread = row.get("spread_proxy", 0.01)
            penalty = (d / 100.0) * slip_est * (spread / 0.01)
            pnl_adj.append(net_pnl - penalty)
            
        pnl_adj = np.array(pnl_adj)
        records.append({
            "scenario_type": "Latency Delay",
            "parameter_value": f"{d}ms",
            "trade_count": len(df),
            "net_profit": np.sum(pnl_adj),
            "profit_factor": calculate_profit_factor(pnl_adj),
            "expectancy": np.mean(pnl_adj)
        })
        
    # 3. Partial Fill Sweep (Slippage = 1.0x, Delay = 0ms)
    for f in fills[1:]:
        pnl_adj = []
        for _, row in df.iterrows():
            pnl_adj.append(row["pnl"] * f)
            
        pnl_adj = np.array(pnl_adj)
        records.append({
            "scenario_type": "Partial Fill",
            "parameter_value": f"{int(f * 100)}%",
            "trade_count": len(df),
            "net_profit": np.sum(pnl_adj),
            "profit_factor": calculate_profit_factor(pnl_adj),
            "expectancy": np.mean(pnl_adj)
        })
        
    # 4. Severe Compound Friction Stress (Worst Case: 3.0x slippage, 500ms delay, 50% fill)
    pnl_worst = []
    for _, row in df.iterrows():
        net_pnl = row["pnl"]
        slip_est = row["slippage_estimate"]
        spread = row.get("spread_proxy", 0.01)
        # Apply 3x slippage and 500ms delay to position value, then multiply by 50% fill
        base_gross_pnl = net_pnl + slip_est
        worst_slip = 3.0 * slip_est
        worst_delay_penalty = (500.0 / 100.0) * slip_est * (spread / 0.01)
        pnl_worst.append((base_gross_pnl - worst_slip - worst_delay_penalty) * 0.50)
        
    pnl_worst = np.array(pnl_worst)
    records.append({
        "scenario_type": "Compound Severe",
        "parameter_value": "3x / 500ms / 50%",
        "trade_count": len(df),
        "net_profit": np.sum(pnl_worst),
        "profit_factor": calculate_profit_factor(pnl_worst),
        "expectancy": np.mean(pnl_worst)
    })
    
    df_sweep = pd.DataFrame(records)
    df_sweep.to_csv(os.path.join(out_dir, "friction_sweep.csv"), index=False)
    print(f"[+] Saved execution friction stress sweep to {os.path.join(out_dir, 'friction_sweep.csv')}")

def calculate_profit_factor(pnls):
    gains = pnls[pnls > 0].sum()
    losses = abs(pnls[pnls <= 0].sum())
    return gains / losses if losses > 0 else (gains if gains > 0 else 1.0)

if __name__ == "__main__":
    run_friction_sweep()
