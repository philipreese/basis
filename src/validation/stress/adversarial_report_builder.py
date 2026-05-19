import os
import sys
import pandas as pd
import numpy as np

# Ensure project root is in path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.validation.stress.adversarial_simulator import run_adversarial_simulator
from src.validation.stress.concentration_evaluator import run_concentration_evaluator

def build_adversarial_report():
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    # 1. Run sweeps
    run_adversarial_simulator()
    run_concentration_evaluator()
    
    # 2. Load outputs
    adv_file = os.path.join(out_dir, "adversarial_sweeps.csv")
    conc_file = os.path.join(out_dir, "concentrated_sweeps.csv")
    
    if not os.path.exists(adv_file) or not os.path.exists(conc_file):
        print("[!] Missing sweep files. Unable to generate report.")
        return
        
    df_adv = pd.read_csv(adv_file)
    df_conc = pd.read_csv(conc_file)
    
    # 3. Aggregate results across all epochs for each strategy/profile combo
    # We want tables of: Strategy | Profile | Total Trades | Win Rate % | Net Profit ($) | Expectancy ($) | Profit Factor
    
    records = []
    
    # Process Baseline Strategy profiles
    for profile in df_adv["profile"].unique():
        sub = df_adv[df_adv["profile"] == profile]
        tc = sub["trade_count"].sum()
        net_prof = sub["net_profit"].sum()
        avg_wr = sub["win_rate"].mean() # Simple average for simplicity, or weighted if desired
        avg_exp = sub["expectancy"].mean()
        
        # Calculate aggregate profit factor
        # Since we don't have individual wins/losses here, we can proxy via profit factor average or keep it simple
        avg_pf = sub["profit_factor"].replace([np.inf, -np.inf], np.nan).mean()
        if np.isnan(avg_pf):
            avg_pf = 0.0
            
        records.append({
            "strategy": "Baseline Strategy",
            "profile": profile,
            "trade_count": tc,
            "win_rate": avg_wr,
            "net_profit": net_prof,
            "profit_factor": avg_pf,
            "expectancy": avg_exp
        })
        
    # Process Concentrated strategies profiles
    for strat in df_conc["strategy"].unique():
        sub_strat = df_conc[df_conc["strategy"] == strat]
        for profile in sub_strat["profile"].unique():
            sub = sub_strat[sub_strat["profile"] == profile]
            tc = sub["trade_count"].sum()
            net_prof = sub["net_profit"].sum()
            avg_wr = sub["win_rate"].mean()
            avg_exp = sub["expectancy"].mean()
            avg_pf = sub["profit_factor"].replace([np.inf, -np.inf], np.nan).mean()
            if np.isnan(avg_pf):
                avg_pf = 0.0
                
            records.append({
                "strategy": strat,
                "profile": profile,
                "trade_count": tc,
                "win_rate": avg_wr,
                "net_profit": net_prof,
                "profit_factor": avg_pf,
                "expectancy": avg_exp
            })
            
    df_agg = pd.DataFrame(records)
    
    # 4. Formulate Go/No-Go Decision logic
    # We analyze the "Concentrated (Shift=0, Gap>=4%, ORB=30m)" and "Concentrated (Shift=-5, Gap>=4%, ORB=30m)"
    # specifically under "Log-Normal Latency" and "Spread-Widening 2x".
    
    decision_notes = []
    go_status = "No-Go"
    
    # Get expectancy for Concentrated (Shift=0) under Log-Normal Latency
    c_shift0_lat = df_agg[(df_agg["strategy"] == "Concentrated (Shift=0, Gap>=4%, ORB=30m)") & (df_agg["profile"] == "Log-Normal Latency")]
    c_shift0_spread = df_agg[(df_agg["strategy"] == "Concentrated (Shift=0, Gap>=4%, ORB=30m)") & (df_agg["profile"] == "Spread-Widening 2x")]
    c_shift0_worst = df_agg[(df_agg["strategy"] == "Concentrated (Shift=0, Gap>=4%, ORB=30m)") & (df_agg["profile"] == "Worst-Case Fill")]
    
    # Get expectancy for Concentrated (Shift=-5) under Log-Normal Latency
    c_shift5_lat = df_agg[(df_agg["strategy"] == "Concentrated (Shift=-5, Gap>=4%, ORB=30m)") & (df_agg["profile"] == "Log-Normal Latency")]
    c_shift5_spread = df_agg[(df_agg["strategy"] == "Concentrated (Shift=-5, Gap>=4%, ORB=30m)") & (df_agg["profile"] == "Spread-Widening 2x")]
    
    shift0_lat_exp = c_shift0_lat["expectancy"].values[0] if not c_shift0_lat.empty else -1.0
    shift0_spread_exp = c_shift0_spread["expectancy"].values[0] if not c_shift0_spread.empty else -1.0
    shift0_worst_exp = c_shift0_worst["expectancy"].values[0] if not c_shift0_worst.empty else -1.0
    
    shift5_lat_exp = c_shift5_lat["expectancy"].values[0] if not c_shift5_lat.empty else -1.0
    shift5_spread_exp = c_shift5_spread["expectancy"].values[0] if not c_shift5_spread.empty else -1.0
    
    if shift0_lat_exp > 0 and shift0_spread_exp > 0:
        go_status = "Go (Highly Robust)"
        decision_notes.append("The Concentrated strategy (Shift=0) maintains positive expectancy under realistic latency and spread widening across all 6 epochs.")
        if shift0_worst_exp > 0:
            go_status = "Go (Triple-A Diamond Grade)"
            decision_notes.append("CRITICAL: The strategy even survives the absolute worst-case bar-boundary fills (Adversarial Mode) with positive expectancy!")
    elif shift5_lat_exp > 0 and shift5_spread_exp > 0:
        go_status = "Conditional Go (Early Entry Required)"
        decision_notes.append("The strategy only maintains positive expectancy if we target early momentum continuation (Shift=-5). Standard ORB breakout at Close (Shift=0) is not viable under realistic friction.")
        decision_notes.append("WARNING: Exploiting Shift=-5 requires specialized execution routing or pre-market execution capability.")
    else:
        go_status = "No-Go (Edge Collapsed)"
        decision_notes.append("All configurations collapsed into negative expectancy under realistic retail latency (Log-Normal) and spread widening. The remaining edge is a backtest-shaped artifact of clean-fill assumptions.")
        
    # Write report
    report_file = os.path.join(out_dir, "adversarial_validation_report_phase_a6.md")
    
    with open(report_file, "w") as f:
        f.write("# Phase A.6: Adversarial Execution Hardening & Edge Concentration Validation Report\n\n")
        f.write("This report evaluates the baseline strategy alongside concentrated edge variations under randomized execution latency, spread widening, and worst-case bar fills across 6 representative epochs (2020-2026).\n\n")
        
        f.write(f"## Final Validation Verdict: **{go_status}**\n\n")
        f.write("### Decision Diagnostic Rationale\n")
        for note in decision_notes:
            f.write(f"- {note}\n")
        f.write("\n---\n\n")
        
        f.write("## 1. Aggregate Strategy Performance Under Friction\n\n")
        f.write("This table summarizes performance aggregated across all 6 historical epochs under various execution stress profiles:\n\n")
        
        f.write("| Strategy | Stress Profile | Total Trades | Win Rate (%) | Net Profit ($) | Expectancy ($) | Profit Factor |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for _, row in df_agg.iterrows():
            f.write(f"| {row['strategy']} | {row['profile']} | {row['trade_count']} | {row['win_rate']:.1f}% | ${row['net_profit']:.2f} | ${row['expectancy']:.2f} | {row['profit_factor']:.2f} |\n")
            
        f.write("\n---\n\n")
        
        f.write("## 2. Epoch-Level Breakdown (Concentrated Shift=0)\n\n")
        f.write("This table tracks performance of the Concentrated Strategy (Shift=0, Gap>=4%, ORB=30m) across individual historical regimes:\n\n")
        f.write("| Regime Epoch | Stress Profile | Trades | Win Rate (%) | Net Profit ($) | Expectancy ($) |\n")
        f.write("|---|---|---|---|---|---|\n")
        sub_strat = df_conc[df_conc["strategy"] == "Concentrated (Shift=0, Gap>=4%, ORB=30m)"]
        for _, row in sub_strat.iterrows():
            f.write(f"| {row['epoch']} | {row['profile']} | {row['trade_count']} | {row['win_rate']:.1f}% | ${row['net_profit']:.2f} | ${row['expectancy']:.2f} |\n")
            
        f.write("\n---\n\n")
        
        f.write("## 3. Epoch-Level Breakdown (Concentrated Shift=-5)\n\n")
        f.write("This table tracks performance of the Concentrated Strategy with Early Entry (Shift=-5, Gap>=4%, ORB=30m) across individual historical regimes:\n\n")
        f.write("| Regime Epoch | Stress Profile | Trades | Win Rate (%) | Net Profit ($) | Expectancy ($) |\n")
        f.write("|---|---|---|---|---|---|\n")
        sub_strat = df_conc[df_conc["strategy"] == "Concentrated (Shift=-5, Gap>=4%, ORB=30m)"]
        for _, row in sub_strat.iterrows():
            f.write(f"| {row['epoch']} | {row['profile']} | {row['trade_count']} | {row['win_rate']:.1f}% | ${row['net_profit']:.2f} | ${row['expectancy']:.2f} |\n")
            
        f.write("\n---\n\n")
        f.write("## 4. Quantitative Conclusion & Takeaways\n\n")
        if go_status.startswith("Go"):
            f.write("> [!NOTE]\n")
            f.write("> **VERDICT: GO.** The strategy demonstrates a true structural edge under concentrated parameters. By restricting execution to high-displacement openings (Gaps >= 4.0%) and wider ORB windows (30m), the strategy preserves positive expectancy even under realistic slippage, spread widening, and retail latency delays. It is ready for the live validation phase with tight risk controls.\n")
        elif go_status.startswith("Conditional Go"):
            f.write("> [!WARNING]\n")
            f.write("> **VERDICT: CONDITIONAL GO.** The edge is highly dependent on timing. Standard execution at the close of the breakout minute bar (Shift=0) is destroyed by latency. However, early entry (Shift=-5) survives. This suggests the edge is a pure microstructure speed game. Live deployment should only be attempted using low-latency direct-market-access (DMA) execution systems.\n")
        else:
            f.write("> [!CAUTION]\n")
            f.write("> **VERDICT: NO-GO.** The edge collapsed under stress. The positive returns observed in basic backtesting are an illusion created by clean entry/exit fill assumptions. In reality, execution delays and bid-ask spread widening consume all net alpha, resulting in a system with negative expectancy across all historical regimes. Live deployment is NOT recommended.\n")
            
    print(f"[+] Saved master adversarial validation report to {report_file}")

if __name__ == "__main__":
    build_adversarial_report()
