import os
import pandas as pd
import numpy as np

def run_regime_isolation():
    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "out"))
    trade_dataset_path = os.path.join(out_dir, "trade_dataset.csv")
    
    if not os.path.exists(trade_dataset_path):
        print(f"[!] Error: {trade_dataset_path} does not exist. Run forward_tester first.")
        return
        
    df = pd.read_csv(trade_dataset_path)
    if df.empty:
        print("[!] Trade dataset is empty.")
        return
        
    # Convert regime and is_transition
    df["is_transition"] = df["is_transition"].astype(bool)
    
    regimes = df["regime"].unique()
    
    records = []
    
    # 1. Regime level performance (stable vs transition)
    for r in regimes:
        df_r = df[df["regime"] == r]
        
        # Split into Stable and Transition
        for subset_name, mask in [("Stable", ~df_r["is_transition"]), ("Transition", df_r["is_transition"]), ("Full", pd.Series(True, index=df_r.index))]:
            df_sub = df_r[mask]
            tc = len(df_sub)
            if tc == 0:
                continue
                
            pnl_vals = df_sub["pnl"].values
            net_profit = np.sum(pnl_vals)
            expectancy = np.mean(pnl_vals)
            
            wins = pnl_vals[pnl_vals > 0]
            losses = abs(pnl_vals[pnl_vals <= 0])
            win_rate = len(wins) / tc * 100.0
            
            total_gains = np.sum(wins)
            total_losses = np.sum(losses)
            profit_factor = total_gains / total_losses if total_losses > 0 else (total_gains if total_gains > 0 else 1.0)
            
            records.append({
                "regime": r,
                "subset": subset_name,
                "trade_count": tc,
                "net_profit": net_profit,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "expectancy": expectancy
            })
            
    # Calculate transition zone fragility
    df_trans = df[df["is_transition"] == True]
    df_stable = df[df["is_transition"] == False]
    
    trans_expectancy = df_trans["pnl"].mean() if len(df_trans) > 0 else 0.0
    stable_expectancy = df_stable["pnl"].mean() if len(df_stable) > 0 else 0.0
    
    # Fragility score: ratio of expectancy loss in transition zones
    # Fragility = (Stable Expectancy - Transition Expectancy) / (Stable Expectancy if stable > 0 else 1.0)
    fragility_score = (stable_expectancy - trans_expectancy) / (stable_expectancy if stable_expectancy > 0 else 1.0)
    
    print(f"Regime Isolation: Stable Expectancy = ${stable_expectancy:.2f}, Transition Expectancy = ${trans_expectancy:.2f} -> Fragility Score = {fragility_score:.4f}")
    
    df_isolation = pd.DataFrame(records)
    df_isolation.to_csv(os.path.join(out_dir, "regime_isolation_stats.csv"), index=False)
    print(f"[+] Saved regime isolation stats to {os.path.join(out_dir, 'regime_isolation_stats.csv')}")
    
if __name__ == "__main__":
    run_regime_isolation()
