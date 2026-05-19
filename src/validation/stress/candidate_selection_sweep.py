import os
import sys
import pandas as pd

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.validation.forward_tester import run_multi_day_backtest

def run_selection_sweep():
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    start_date = "2025-01-01"
    end_date = "2025-03-31"
    
    ns = [1, 3, 5, 10, 9999]
    rvols = [None, 10.0, 30.0, 50.0]
    gaps = [None, 2.0, 4.0, 6.0]
    
    records = []
    original_stdout = sys.stdout
    
    try:
        # 1. Sweep Top-N Candidate restrictions
        for n in ns:
            sys.stdout = open(os.devnull, 'w')
            try:
                trades = run_multi_day_backtest(
                    start_date_str=start_date,
                    end_date_str=end_date,
                    use_llm=False,
                    initial_value=100000.0,
                    max_streams=n,
                    log_to_csv=False
                )
            except Exception as e:
                trades = []
            sys.stdout.close()
            sys.stdout = original_stdout
            
            tc = len(trades)
            net_profit = sum(t["pnl"] for t in trades)
            wins = [t for t in trades if t["pnl"] > 0]
            win_rate = (len(wins) / tc * 100.0) if tc > 0 else 0.0
            
            print(f"Selection Sweep: Top-N={n if n < 9999 else 'Unlimited'} -> Trades={tc}, WinRate={win_rate:.1f}%, Profit={net_profit:.2f}")
            records.append({
                "sweep_type": "Top-N",
                "param_value": str(n) if n < 9999 else "Unlimited",
                "trade_count": tc,
                "win_rate": win_rate,
                "net_profit": net_profit
            })
            
        # 2. Sweep RVOL thresholds
        for rvol in rvols:
            sys.stdout = open(os.devnull, 'w')
            try:
                trades = run_multi_day_backtest(
                    start_date_str=start_date,
                    end_date_str=end_date,
                    use_llm=False,
                    initial_value=100000.0,
                    min_rvol=rvol,
                    log_to_csv=False
                )
            except Exception as e:
                trades = []
            sys.stdout.close()
            sys.stdout = original_stdout
            
            tc = len(trades)
            net_profit = sum(t["pnl"] for t in trades)
            wins = [t for t in trades if t["pnl"] > 0]
            win_rate = (len(wins) / tc * 100.0) if tc > 0 else 0.0
            
            print(f"Selection Sweep: Min RVOL={rvol} -> Trades={tc}, WinRate={win_rate:.1f}%, Profit={net_profit:.2f}")
            records.append({
                "sweep_type": "Min-RVOL",
                "param_value": str(rvol) if rvol is not None else "None",
                "trade_count": tc,
                "win_rate": win_rate,
                "net_profit": net_profit
            })
            
        # 3. Sweep Gap % thresholds
        for gap in gaps:
            sys.stdout = open(os.devnull, 'w')
            try:
                trades = run_multi_day_backtest(
                    start_date_str=start_date,
                    end_date_str=end_date,
                    use_llm=False,
                    initial_value=100000.0,
                    min_gap=gap,
                    log_to_csv=False
                )
            except Exception as e:
                trades = []
            sys.stdout.close()
            sys.stdout = original_stdout
            
            tc = len(trades)
            net_profit = sum(t["pnl"] for t in trades)
            wins = [t for t in trades if t["pnl"] > 0]
            win_rate = (len(wins) / tc * 100.0) if tc > 0 else 0.0
            
            print(f"Selection Sweep: Min Gap={gap}% -> Trades={tc}, WinRate={win_rate:.1f}%, Profit={net_profit:.2f}")
            records.append({
                "sweep_type": "Min-Gap",
                "param_value": f"{gap}%" if gap is not None else "None",
                "trade_count": tc,
                "win_rate": win_rate,
                "net_profit": net_profit
            })
            
    finally:
        sys.stdout = original_stdout
        
    df_select = pd.DataFrame(records)
    df_select.to_csv(os.path.join(out_dir, "selection_sweep.csv"), index=False)
    print(f"[+] Saved candidate selection sweep to {os.path.join(out_dir, 'selection_sweep.csv')}")

if __name__ == "__main__":
    run_selection_sweep()
