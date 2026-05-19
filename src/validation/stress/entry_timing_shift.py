import os
import sys
import pandas as pd
import numpy as np

# Ensure project root is in path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.validation.forward_tester import run_multi_day_backtest

def run_timing_sweep():
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    # We will test on the 2025 Current representative window: 2025-01-01 to 2025-03-31
    start_date = "2025-01-01"
    end_date = "2025-03-31"
    
    shifts = [-5, -2, -1, 0, 1, 2, 5]
    orb_durations = [10, 15, 30]
    
    records = []
    
    # Suppress standard print outputs during sweep to avoid clutter
    original_stdout = sys.stdout
    
    try:
        for orb in orb_durations:
            for shift in shifts:
                # Redirect stdout to a null device to keep terminal clean
                sys.stdout = open(os.devnull, 'w')
                
                try:
                    trades = run_multi_day_backtest(
                        start_date_str=start_date,
                        end_date_str=end_date,
                        use_llm=False,
                        initial_value=100000.0,
                        entry_timing_shift=shift,
                        orb_minutes=orb,
                        log_to_csv=False
                    )
                except Exception as e:
                    trades = []
                    
                sys.stdout.close()
                sys.stdout = original_stdout
                
                tc = len(trades)
                net_profit = sum(t["pnl"] for t in trades)
                wins = [t for t in trades if t["pnl"] > 0]
                losses = [t for t in trades if t["pnl"] <= 0]
                win_rate = (len(wins) / tc * 100.0) if tc > 0 else 0.0
                
                total_gains = sum(t["pnl"] for t in wins)
                total_losses = abs(sum(t["pnl"] for t in losses))
                profit_factor = total_gains / total_losses if total_losses > 0 else (total_gains if total_gains > 0 else 1.0)
                
                print(f"Timing Shift Sweep: ORB={orb}m, Shift={shift} bars -> Trades={tc}, WinRate={win_rate:.1f}%, Profit={net_profit:.2f}, PF={profit_factor:.2f}")
                
                records.append({
                    "orb_minutes": orb,
                    "entry_timing_shift": shift,
                    "trade_count": tc,
                    "win_rate": win_rate,
                    "net_profit": net_profit,
                    "profit_factor": profit_factor
                })
    finally:
        sys.stdout = original_stdout
        
    df_timing = pd.DataFrame(records)
    df_timing.to_csv(os.path.join(out_dir, "timing_shift_sweep.csv"), index=False)
    print(f"[+] Saved entry timing sweep to {os.path.join(out_dir, 'timing_shift_sweep.csv')}")

if __name__ == "__main__":
    run_timing_sweep()
