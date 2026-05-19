import os
import sys
import pandas as pd
import numpy as np

# Ensure project root is in path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.validation.forward_tester import run_multi_day_backtest

# Define representative epochs
EPOCHS = [
    ("2020_Crash", "2020-03-01", "2020-05-30"),
    ("2021_Bull", "2021-01-01", "2021-03-31"),
    ("2022_Bear", "2022-01-01", "2022-03-31"),
    ("2023_Tech", "2023-01-01", "2023-03-31"),
    ("2024_Choppy", "2024-01-01", "2024-03-31"),
    ("2025_Current", "2025-01-01", "2025-03-31"),
]

def run_adversarial_simulator():
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    profiles = [
        ("Baseline", False, False, 1.0),
        ("Log-Normal Latency", False, True, 1.0),
        ("Spread-Widening 2x", False, False, 2.0),
        ("Worst-Case Fill", True, False, 1.0),
    ]
    
    records = []
    
    # Suppress standard print outputs during sweeps
    original_stdout = sys.stdout
    
    print("\n=======================================================")
    print("RUNNING ADVERSARIAL SENSITIVITY SIMULATOR...")
    print("=======================================================")
    
    try:
        for profile_name, adv_mode, rand_lat, spread_coeff in profiles:
            print(f"[*] Running Profile: {profile_name}...")
            
            for epoch_name, start_date, end_date in EPOCHS:
                # Redirect stdout to a null device to keep terminal clean
                sys.stdout = open(os.devnull, 'w')
                
                try:
                    # Run backtest with log_to_csv=False to avoid corrupting baseline trade logs
                    trade_logs = run_multi_day_backtest(
                        start_date, end_date,
                        use_llm=False,
                        initial_value=100000.0,
                        log_to_csv=False,
                        adversarial_mode=adv_mode,
                        random_latency_spread=rand_lat,
                        spread_widening_coeff=spread_coeff
                    )
                except Exception as e:
                    sys.stdout = original_stdout
                    print(f"Error running epoch {epoch_name}: {e}")
                    continue
                finally:
                    sys.stdout.close()
                    sys.stdout = original_stdout
                
                tc = len(trade_logs)
                if tc > 0:
                    pnls = [t["pnl"] for t in trade_logs]
                    net_profit = sum(pnls)
                    win_rate = len([p for p in pnls if p > 0]) / tc * 100.0
                    expectancy = np.mean(pnls)
                    
                    wins = [p for p in pnls if p > 0]
                    losses = [abs(p) for p in pnls if p <= 0]
                    profit_factor = sum(wins) / sum(losses) if sum(losses) > 0 else float('inf')
                else:
                    net_profit = 0.0
                    win_rate = 0.0
                    expectancy = 0.0
                    profit_factor = 0.0
                
                records.append({
                    "profile": profile_name,
                    "epoch": epoch_name,
                    "trade_count": tc,
                    "win_rate": win_rate,
                    "net_profit": net_profit,
                    "profit_factor": profit_factor,
                    "expectancy": expectancy
                })
                
                print(f"    - {epoch_name}: Trades={tc}, WinRate={win_rate:.1f}%, Profit={net_profit:.2f}, Expectancy={expectancy:.2f}")
                
    finally:
        sys.stdout = original_stdout
        
    df = pd.DataFrame(records)
    out_file = os.path.join(out_dir, "adversarial_sweeps.csv")
    df.to_csv(out_file, index=False)
    print(f"\n[+] Saved adversarial sweeps to {out_file}\n")

if __name__ == "__main__":
    run_adversarial_simulator()
