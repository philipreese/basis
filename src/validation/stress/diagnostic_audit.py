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

def run_diagnostic_audit():
    print("\n=======================================================")
    print("STARTING CAUSALITY AUDIT & DIAGNOSTIC EXPERIMENTS")
    print("=======================================================")
    
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    original_stdout = sys.stdout
    
    # -----------------------------------------------------------------
    # EXPERIMENT 1: Shift Sweep (Causality Audit)
    # -----------------------------------------------------------------
    print("\n[*] Experiment 1: Running Shift Sweep (Causality Audit)...")
    shift_values = [-5, 0, 1, 2, 3]
    shift_results = []
    
    for shift in shift_values:
        print(f"  - Testing Shift: {shift}...")
        for epoch_name, start_date, end_date in EPOCHS:
            sys.stdout = open(os.devnull, 'w')
            try:
                # Testing under standard baseline parameters with new causal VWAP/watchlist
                trade_logs = run_multi_day_backtest(
                    start_date, end_date,
                    use_llm=False,
                    initial_value=100000.0,
                    entry_timing_shift=shift,
                    orb_minutes=30,
                    min_gap=4.0,
                    log_to_csv=False,
                    adversarial_mode=False,
                    random_latency_spread=False,
                    spread_widening_coeff=1.0
                )
            except Exception as e:
                sys.stdout = original_stdout
                print(f"    Error on epoch {epoch_name}: {e}")
                continue
            finally:
                sys.stdout.close()
                sys.stdout = original_stdout
            
            for t in trade_logs:
                shift_results.append({
                    "shift": shift,
                    "epoch": epoch_name,
                    "ticker": t["ticker"],
                    "pnl": t["pnl"],
                    "regime": t.get("regime", "CHOPPY_ROTATIONAL"),
                    "status": t["status"]
                })
                
    df_shift = pd.DataFrame(shift_results)
    
    # Summarize Shift performance
    shift_summary = []
    if not df_shift.empty:
        for shift in shift_values:
            sub = df_shift[df_shift["shift"] == shift]
            tc = len(sub)
            if tc > 0:
                net_p = sub["pnl"].sum()
                wr = len(sub[sub["pnl"] > 0]) / tc * 100.0
                exp = sub["pnl"].mean()
            else:
                net_p, wr, exp = 0.0, 0.0, 0.0
            shift_summary.append({
                "shift": shift,
                "trades": tc,
                "win_rate": wr,
                "net_profit": net_p,
                "expectancy": exp
            })
    df_shift_sum = pd.DataFrame(shift_summary)
    print("  [+] Completed Shift Sweep.")
    print(df_shift_sum.to_string(index=False))
    
    # -----------------------------------------------------------------
    # EXPERIMENT 2: Delayed Execution Realism Test
    # -----------------------------------------------------------------
    print("\n[*] Experiment 2: Running Delayed Execution Realism Test...")
    # Simulate 1 to 3 bar delay, under adverse fills and 2x spread expansion
    delay_values = [1, 2, 3]
    delay_results = []
    
    for delay in delay_values:
        print(f"  - Testing Delay of {delay} bars (with Worst-Case Fill & 2x Spread)...")
        for epoch_name, start_date, end_date in EPOCHS:
            sys.stdout = open(os.devnull, 'w')
            try:
                trade_logs = run_multi_day_backtest(
                    start_date, end_date,
                    use_llm=False,
                    initial_value=100000.0,
                    entry_timing_shift=delay,
                    orb_minutes=30,
                    min_gap=4.0,
                    log_to_csv=False,
                    adversarial_mode=True,         # Worst-case fill
                    random_latency_spread=True,    # Log-normal latency & variable slippage
                    spread_widening_coeff=2.0      # 2x spread expansion
                )
            except Exception as e:
                sys.stdout = original_stdout
                print(f"    Error on epoch {epoch_name}: {e}")
                continue
            finally:
                sys.stdout.close()
                sys.stdout = original_stdout
            
            for t in trade_logs:
                delay_results.append({
                    "delay": delay,
                    "epoch": epoch_name,
                    "ticker": t["ticker"],
                    "pnl": t["pnl"],
                    "regime": t.get("regime", "CHOPPY_ROTATIONAL"),
                    "status": t["status"]
                })
                
    df_delay = pd.DataFrame(delay_results)
    
    delay_summary = []
    if not df_delay.empty:
        for delay in delay_values:
            sub = df_delay[df_delay["delay"] == delay]
            tc = len(sub)
            if tc > 0:
                net_p = sub["pnl"].sum()
                wr = len(sub[sub["pnl"] > 0]) / tc * 100.0
                exp = sub["pnl"].mean()
            else:
                net_p, wr, exp = 0.0, 0.0, 0.0
            delay_summary.append({
                "delay_bars": delay,
                "trades": tc,
                "win_rate": wr,
                "net_profit": net_p,
                "expectancy": exp
            })
    df_delay_sum = pd.DataFrame(delay_summary)
    print("  [+] Completed Delayed Realism Test.")
    print(df_delay_sum.to_string(index=False))
    
    # -----------------------------------------------------------------
    # EXPERIMENT 3: Regime Conditional Decomposition
    # -----------------------------------------------------------------
    print("\n[*] Experiment 3: Running Regime Conditional Decomposition (for Shift=0 under friction)...")
    # Run Shift=0 under Log-Normal latency + Spread-Widening 2x across all epochs
    regime_results = []
    
    for epoch_name, start_date, end_date in EPOCHS:
        sys.stdout = open(os.devnull, 'w')
        try:
            trade_logs = run_multi_day_backtest(
                start_date, end_date,
                use_llm=False,
                initial_value=100000.0,
                entry_timing_shift=0,          # Immediate execution
                orb_minutes=30,
                min_gap=4.0,
                log_to_csv=False,
                adversarial_mode=False,
                random_latency_spread=True,    # Realistic latency
                spread_widening_coeff=2.0      # 2x spread expansion
            )
        except Exception as e:
            sys.stdout = original_stdout
            print(f"    Error on epoch {epoch_name}: {e}")
            continue
        finally:
            sys.stdout.close()
            sys.stdout = original_stdout
            
        for t in trade_logs:
            regime_results.append({
                "epoch": epoch_name,
                "ticker": t["ticker"],
                "pnl": t["pnl"],
                "regime": t.get("regime", "CHOPPY_ROTATIONAL"),
                "status": t["status"]
            })
            
    df_regime = pd.DataFrame(regime_results)
    
    regime_summary = []
    if not df_regime.empty:
        for rg in df_regime["regime"].unique():
            sub = df_regime[df_regime["regime"] == rg]
            tc = len(sub)
            if tc > 0:
                net_p = sub["pnl"].sum()
                wr = len(sub[sub["pnl"] > 0]) / tc * 100.0
                exp = sub["pnl"].mean()
            else:
                net_p, wr, exp = 0.0, 0.0, 0.0
            regime_summary.append({
                "regime": rg,
                "trades": tc,
                "win_rate": wr,
                "net_profit": net_p,
                "expectancy": exp
            })
    df_regime_sum = pd.DataFrame(regime_summary)
    print("  [+] Completed Regime Decomposition.")
    print(df_regime_sum.to_string(index=False))
    
    # -----------------------------------------------------------------
    # COMPILE THE MASTER DIAGNOSTIC REPORT
    # -----------------------------------------------------------------
    report_file = os.path.join(out_dir, "diagnostic_causality_report.md")
    
    # Determine Go/No-Go based on whether there's ANY regime for Shift=0 with positive expectancy and a statistically valid sample size
    has_positive_regime = False
    positive_regimes = []
    for _, row in df_regime_sum.iterrows():
        if row["expectancy"] > 0 and row["trades"] >= 15:
            has_positive_regime = True
            positive_regimes.append(f"{row['regime']} (Expectancy: ${row['expectancy']:.2f}, N={row['trades']})")
            
    verdict = "SALVAGEABLE (Conditional Go)" if has_positive_regime else "STRUCTURAL REDESIGN REQUIRED (Hard No-Go)"
    
    with open(report_file, "w") as f:
        f.write("# Diagnostic Causality Audit & Execution Realism Report\n\n")
        f.write("This report details three critical diagnostic experiments to verify signal causality, evaluate access latency, and decompose regime-specific edge viability.\n\n")
        
        f.write(f"## Final System Verdict: **{verdict}**\n\n")
        
        if has_positive_regime:
            f.write(f"> [IMPORTANT]\n")
            f.write(f"> **Verdict Explanation:** The strategy shows positive expectancy at Shift=0 under friction in the following regimes: {', '.join(positive_regimes)}. The system is salvageable if execution is strictly disabled outside these regimes.\n\n")
        else:
            f.write(f"> [!CAUTION]\n")
            f.write(f"> **Verdict Explanation:** There is **no single regime** with a statistically significant sample size (N >= 15) where immediate execution (Shift=0) maintains positive expectancy after realistic latency and spread expansion. The entire system requires a fundamental structural redesign.\n\n")
            
        f.write("## 1. True Causality Audit (Shift Sweep)\n\n")
        f.write("We ran the entry timing shift sweep using strict timestamp-enforced signals (no daily close gap leakage, no look-ahead volume filters, and dynamically computed cumulative daily VWAP):\n\n")
        
        f.write("| Entry Shift | Total Trades | Win Rate (%) | Net Profit ($) | Expectancy ($) | Status |\n")
        f.write("|---|---|---|---|---|---|\n")
        for _, row in df_shift_sum.iterrows():
            status = "Causal" if row["shift"] >= 0 else "Non-Causal (Leakage)"
            f.write(f"| {row['shift']} | {row['trades']} | {row['win_rate']:.1f}% | ${row['net_profit']:.2f} | ${row['expectancy']:.2f} | {status} |\n")
            
        f.write("\n")
        # Diagnostic analysis of Shift=-5
        shift_neg5_row = df_shift_sum[df_shift_sum["shift"] == -5]
        shift_0_row = df_shift_sum[df_shift_sum["shift"] == 0]
        
        if not shift_neg5_row.empty and not shift_0_row.empty:
            neg5_exp = shift_neg5_row["expectancy"].values[0]
            zero_exp = shift_0_row["expectancy"].values[0]
            if neg5_exp > 0 and zero_exp <= 0:
                f.write("> [!WARNING]\n")
                f.write(f"> **Audit Analysis:** The Shift=-5 configuration shows an expectancy of **${neg5_exp:.2f}**, while the Shift=0 configuration shows **${zero_exp:.2f}**. This massive divergence confirms that the apparent 'early entry' advantage is a **statistical artifact of look-ahead execution leakage**, not a tradable alpha engine.\n\n")
            else:
                f.write("> [!NOTE]\n")
                f.write(f"> **Audit Analysis:** The Shift=-5 configuration has expectancy of ${neg5_exp:.2f} vs ${zero_exp:.2f} for Shift=0. This indicates the degree of alpha deterioration when moving from retroactive execution to immediate causal execution.\n\n")
                
        f.write("## 2. Delayed Execution Realism Test\n\n")
        f.write("This test simulates a 1-3 bar entry execution delay under worst-case bar fills (entry filled at High, exit filled at Low) and 2x spread expansion:\n\n")
        
        f.write("| Delay (Bars) | Total Trades | Win Rate (%) | Net Profit ($) | Expectancy ($) |\n")
        f.write("|---|---|---|---|---|\n")
        for _, row in df_delay_sum.iterrows():
            f.write(f"| {row['delay_bars']} | {row['trades']} | {row['win_rate']:.1f}% | ${row['net_profit']:.2f} | ${row['expectancy']:.2f} |\n")
            
        f.write("\n---\n\n")
        
        f.write("## 3. Regime Conditional Decomposition (Shift=0 Under Friction)\n\n")
        f.write("This table breaks down the performance of immediate execution (Shift=0) under realistic retail latency and 2x spread expansion across individual market regimes:\n\n")
        
        f.write("| Market Regime | Total Trades | Win Rate (%) | Net Profit ($) | Expectancy ($) | Status |\n")
        f.write("|---|---|---|---|---|---|\n")
        for _, row in df_regime_sum.iterrows():
            status = "Viable Edge" if row["expectancy"] > 0 else "Negative Expectancy"
            f.write(f"| {row['regime']} | {row['trades']} | {row['win_rate']:.1f}% | ${row['net_profit']:.2f} | ${row['expectancy']:.2f} | {status} |\n")
            
        f.write("\n")
        
    print(f"\n[+] Saved diagnostic causality report to {report_file}\n")

if __name__ == "__main__":
    run_diagnostic_audit()
