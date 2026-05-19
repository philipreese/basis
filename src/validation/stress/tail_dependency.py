import os
import pandas as pd
import numpy as np

def run_tail_dependency():
    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "out"))
    trade_dataset_path = os.path.join(out_dir, "trade_dataset.csv")
    
    if not os.path.exists(trade_dataset_path):
        print(f"[!] Error: {trade_dataset_path} does not exist. Run forward_tester first.")
        return
        
    df = pd.read_csv(trade_dataset_path)
    if df.empty:
        print("[!] Trade dataset is empty.")
        return
        
    pnls = df["pnl"].values
    n_trades = len(pnls)
    
    # Sort pnls to perform tail truncation
    sorted_pnls = np.sort(pnls)
    
    # 5% index
    k_5pct = int(np.ceil(0.05 * n_trades))
    
    # Baseline
    base_net = np.sum(pnls)
    base_pf = calculate_profit_factor(pnls)
    base_expectancy = np.mean(pnls)
    
    # 1. Truncate Top 5% (Right-tail stress)
    right_truncated = sorted_pnls[:-k_5pct]
    rt_net = np.sum(right_truncated)
    rt_pf = calculate_profit_factor(right_truncated)
    rt_expectancy = np.mean(right_truncated)
    
    # 2. Truncate Worst 5% (Left-tail stress)
    left_truncated = sorted_pnls[k_5pct:]
    lt_net = np.sum(left_truncated)
    lt_pf = calculate_profit_factor(left_truncated)
    lt_expectancy = np.mean(left_truncated)
    
    # 3. Truncate both Top & Worst 5%
    both_truncated = sorted_pnls[k_5pct:-k_5pct]
    bt_net = np.sum(both_truncated)
    bt_pf = calculate_profit_factor(both_truncated)
    bt_expectancy = np.mean(both_truncated)
    
    # 4. Cap wins to 2x ATR and losses to 1.5x ATR equivalent
    # Since r_multiple maps roughly to ATR-based multiples:
    # Cap R-multiple: max R = 2.0, min R = -1.5
    # Let's map this using r_multiple and pnl per R.
    # If r_multiple is not zero, 1R = pnl / r_multiple
    capped_pnls = []
    for _, row in df.iterrows():
        pnl = row["pnl"]
        r = row["r_multiple"]
        if abs(r) > 0.01:
            pnl_per_r = pnl / r
            r_capped = max(-1.5, min(2.0, r))
            capped_pnls.append(r_capped * pnl_per_r)
        else:
            capped_pnls.append(pnl)
            
    capped_pnls = np.array(capped_pnls)
    cap_net = np.sum(capped_pnls)
    cap_pf = calculate_profit_factor(capped_pnls)
    cap_expectancy = np.mean(capped_pnls)
    
    # Skew dependency metric: percentage of profits contributed by the top 5%
    top_5pct_sum = np.sum(sorted_pnls[-k_5pct:])
    skew_dependency = (top_5pct_sum / base_net) if base_net > 0 else 0.0
    
    # Convexity score: RT expectancy degradation vs LT expectancy improvement
    # A positive convexity score means left-tail truncation improves expectancy more than right-tail truncation degrades it (or similar)
    # We can measure it as: rt_expectancy / base_expectancy (if < 1 is bad, showing dependence on fat right tail)
    convexity_score = rt_expectancy / base_expectancy if base_expectancy != 0 else 0.0
    
    records = [
        {
            "scenario": "Baseline",
            "net_profit": base_net,
            "profit_factor": base_pf,
            "expectancy": base_expectancy,
            "metric_value": 0.0
        },
        {
            "scenario": "Truncate Top 5%",
            "net_profit": rt_net,
            "profit_factor": rt_pf,
            "expectancy": rt_expectancy,
            "metric_value": skew_dependency
        },
        {
            "scenario": "Truncate Worst 5%",
            "net_profit": lt_net,
            "profit_factor": lt_pf,
            "expectancy": lt_expectancy,
            "metric_value": 0.0
        },
        {
            "scenario": "Truncate Both 5%",
            "net_profit": bt_net,
            "profit_factor": bt_pf,
            "expectancy": bt_expectancy,
            "metric_value": 0.0
        },
        {
            "scenario": "Capped R [-1.5R, +2.0R]",
            "net_profit": cap_net,
            "profit_factor": cap_pf,
            "expectancy": cap_expectancy,
            "metric_value": convexity_score
        }
    ]
    
    df_tail = pd.DataFrame(records)
    df_tail.to_csv(os.path.join(out_dir, "tail_dependency_stats.csv"), index=False)
    print(f"[+] Saved tail dependency stats to {os.path.join(out_dir, 'tail_dependency_stats.csv')}")

def calculate_profit_factor(pnls):
    gains = pnls[pnls > 0].sum()
    losses = abs(pnls[pnls <= 0].sum())
    return gains / losses if losses > 0 else (gains if gains > 0 else 1.0)

if __name__ == "__main__":
    run_tail_dependency()
