import os
import sys
import json
import pandas as pd
import numpy as np

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Import stress modules to run them sequentially
from src.validation.stress.execution_friction_sweep import run_friction_sweep
from src.validation.stress.entry_timing_shift import run_timing_sweep
from src.validation.stress.candidate_selection_sweep import run_selection_sweep
from src.validation.stress.regime_isolation import run_regime_isolation
from src.validation.stress.tail_dependency import run_tail_dependency

def build_reports():
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    print("\n=======================================================")
    print("RUNNING ALL STRUCTURAL SENSITIVITY SWEEPS...")
    print("=======================================================")
    
    print("\n[*] 1/5: Running Execution Friction Sweep...")
    run_friction_sweep()
    
    print("\n[*] 2/5: Running Entry Timing Shift Sweep...")
    run_timing_sweep()
    
    print("\n[*] 3/5: Running Candidate Selection Sweep...")
    run_selection_sweep()
    
    print("\n[*] 4/5: Running Regime Isolation Analysis...")
    run_regime_isolation()
    
    print("\n[*] 5/5: Running Tail & Skew Dependency Stress Test...")
    run_tail_dependency()
    
    print("\n=======================================================")
    print("AGGREGATING RESULTS & GENERATING MASTER REPORT...")
    print("=======================================================")
    
    # Load all CSVs
    df_friction = pd.read_csv(os.path.join(out_dir, "friction_sweep.csv"))
    df_timing = pd.read_csv(os.path.join(out_dir, "timing_shift_sweep.csv"))
    df_select = pd.read_csv(os.path.join(out_dir, "selection_sweep.csv"))
    df_regime = pd.read_csv(os.path.join(out_dir, "regime_isolation_stats.csv"))
    df_tail = pd.read_csv(os.path.join(out_dir, "tail_dependency_stats.csv"))
    
    # Calculate Strategy Survivability Score
    # Formula:
    # 1. Base Score = 100
    # 2. Friction Decay: 30 * (1 - Compound Severe Net Profit / Baseline Net Profit)
    # 3. Timing Fragility: 30 * (1 - Net Profit of shift +1 / Baseline Net Profit)
    # 4. Skew Dependency: 20 * (Skew Dependency Ratio of Top 5% Truncation)
    # 5. Transition Zone Fragility: 20 * (Stable vs Transition Expectancy delta ratio)
    
    base_friction = df_friction[df_friction["scenario_type"] == "Baseline"]["net_profit"].values[0]
    worst_friction = df_friction[df_friction["scenario_type"] == "Compound Severe"]["net_profit"].values[0]
    friction_decay = max(0.0, min(30.0, 30.0 * (1.0 - (worst_friction / base_friction))))
    
    timing_baseline = df_timing[(df_timing["orb_minutes"] == 15) & (df_timing["entry_timing_shift"] == 0)]["net_profit"].values[0]
    timing_delayed_1 = df_timing[(df_timing["orb_minutes"] == 15) & (df_timing["entry_timing_shift"] == 1)]["net_profit"].values[0]
    timing_decay = max(0.0, min(30.0, 30.0 * (1.0 - (timing_delayed_1 / timing_baseline))))
    
    skew_dependency_ratio = df_tail[df_tail["scenario"] == "Truncate Top 5%"]["metric_value"].values[0]
    skew_decay = max(0.0, min(20.0, 20.0 * skew_dependency_ratio))
    
    # Transition zone expectancy delta
    df_trans_stats = df_regime[df_regime["regime"] == "CHOPPY_ROTATIONAL"] # fallback
    stable_exp = df_regime[df_regime["subset"] == "Stable"]["expectancy"].mean()
    trans_exp = df_regime[df_regime["subset"] == "Transition"]["expectancy"].mean()
    trans_fragility = (stable_exp - trans_exp) / (stable_exp if stable_exp > 0 else 1.0)
    trans_decay = max(0.0, min(20.0, 20.0 * trans_fragility))
    
    survivability_score = 100.0 - (friction_decay + timing_decay + skew_decay + trans_decay)
    survivability_score = max(0.0, min(100.0, survivability_score))
    
    survivability_metrics = {
        "strategy_survivability_score": round(survivability_score, 2),
        "friction_degradation_factor": round(friction_decay, 2),
        "timing_fragility_factor": round(timing_decay, 2),
        "skew_dependency_factor": round(skew_decay, 2),
        "regime_transition_fragility_factor": round(trans_decay, 2)
    }
    
    with open(os.path.join(out_dir, "strategy_survivability_score.json"), "w") as f:
        json.dump(survivability_metrics, f, indent=4)
    print(f"[+] Saved survivability score to strategy_survivability_score.json")
    
    # Save Fragility Matrix
    # We combine key variables to output out/fragility_matrix.csv
    fragility_rows = [
        {"Stress Parameter": "Execution Slippage (3x)", "Degradation %": round((1.0 - (df_friction[df_friction["parameter_value"] == "3.0x"]["net_profit"].values[0] / base_friction)) * 100, 2)},
        {"Stress Parameter": "Execution Latency (500ms)", "Degradation %": round((1.0 - (df_friction[df_friction["parameter_value"] == "500ms"]["net_profit"].values[0] / base_friction)) * 100, 2)},
        {"Stress Parameter": "Partial Fill (50%)", "Degradation %": round((1.0 - (df_friction[df_friction["parameter_value"] == "50%"]["net_profit"].values[0] / base_friction)) * 100, 2)},
        {"Stress Parameter": "Entry Delay (+1 bar)", "Degradation %": round((1.0 - (timing_delayed_1 / timing_baseline)) * 100, 2)},
        {"Stress Parameter": "Right-Tail Truncation (Top 5%)", "Degradation %": round(skew_dependency_ratio * 100, 2)},
        {"Stress Parameter": "Transition Zone Shift", "Degradation %": round(trans_fragility * 100, 2)}
    ]
    df_fragility = pd.DataFrame(fragility_rows)
    df_fragility.to_csv(os.path.join(out_dir, "fragility_matrix.csv"), index=False)
    print(f"[+] Saved fragility matrix to fragility_matrix.csv")
    
    # Write Markdown Report
    report_md = f"""# Phase A.5: Structural Sensitivity & Execution Robustness Mapping Report

This report evaluates the stability, fragility, and expectancy structure of the **Catalyst-Driven Momentum Framework** under controlled perturbations, using the offline 1-minute historical bar cache.

## Strategy Survivability Score: `{survivability_metrics['strategy_survivability_score']}/100`

### Deductions Breakdown
* **Execution Friction Degradation:** -{survivability_metrics['friction_degradation_factor']}
* **Entry Timing Fragility:** -{survivability_metrics['timing_fragility_factor']}
* **Skew / Outlier Dependency:** -{survivability_metrics['skew_dependency_factor']}
* **Regime Transition Zone Fragility:** -{survivability_metrics['regime_transition_fragility_factor']}

---

## 1. Execution Friction Stress Sweep

This sweep evaluates how the strategy performs under increasing execution costs (slippage, latency, and partial fills).

| Scenario | Parameter | Trade Count | Net Profit ($) | Profit Factor | Expectancy ($) |
|---|---|---|---|---|---|
"""
    for _, row in df_friction.iterrows():
        report_md += f"| {row['scenario_type']} | {row['parameter_value']} | {row['trade_count']} | {row['net_profit']:.2f} | {row['profit_factor']:.2f} | {row['expectancy']:.2f} |\n"
        
    report_md += """
---

## 2. Entry Timing Shift Sweep

We analyze the sensitivity of the entry breakout signals to timing offsets (±1, ±2, ±5 bars) and varying ORB windows.

| ORB Duration (min) | Timing Shift (bars) | Trade Count | Win Rate (%) | Net Profit ($) | Profit Factor |
|---|---|---|---|---|---|
"""
    for _, row in df_timing.iterrows():
        report_md += f"| {row['orb_minutes']}m | {row['entry_timing_shift']} | {row['trade_count']} | {row['win_rate']:.1f}% | {row['net_profit']:.2f} | {row['profit_factor']:.2f} |\n"
        
    report_md += """
---

## 3. Candidate Selection Pressure Sweep

This sweep maps the concentration of strategy edge across top candidates, relative volume (RVOL), and Gap % thresholds.

| Sweep Type | Parameter Value | Trade Count | Win Rate (%) | Net Profit ($) |
|---|---|---|---|---|
"""
    for _, row in df_select.iterrows():
        report_md += f"| {row['sweep_type']} | {row['param_value']} | {row['trade_count']} | {row['win_rate']:.1f}% | {row['net_profit']:.2f} |\n"
        
    report_md += """
---

## 4. Regime Isolation & Boundary Analysis

We segment the performance of the strategy across SPY regimes and highlight the contrast between stable and transition zones.

| Regime | Zone Type | Trade Count | Net Profit ($) | Win Rate (%) | Profit Factor | Expectancy ($) |
|---|---|---|---|---|---|---|
"""
    for _, row in df_regime.iterrows():
        report_md += f"| {row['regime']} | {row['subset']} | {row['trade_count']} | {row['net_profit']:.2f} | {row['win_rate']:.1f}% | {row['profit_factor']:.2f} | {row['expectancy']:.2f} |\n"
        
    report_md += """
---

## 5. Tail & Skew Dependency Analysis

We measure strategy dependence on extreme outliers by truncating top/worst 5% trades and capping R-multiples.

| Scenario | Net Profit ($) | Profit Factor | Expectancy ($) | Metric Value |
|---|---|---|---|---|
"""
    for _, row in df_tail.iterrows():
        report_md += f"| {row['scenario']} | {row['net_profit']:.2f} | {row['profit_factor']:.2f} | {row['expectancy']:.2f} | {row['metric_value']:.4f} |\n"
        
    report_md += f"""
---

## 6. Quantitative Fragility Matrix

The fragility matrix summarizes the degradation of the strategy's net profit under specific stress parameters.

| Stress Parameter | Net Profit Degradation % |
|---|---|
"""
    for _, row in df_fragility.iterrows():
        report_md += f"| {row['Stress Parameter']} | {row['Degradation %']}% |\n"
        
    report_md += "\n"
    
    report_path = os.path.join(out_dir, "stress_report_phase_a5.md")
    with open(report_path, "w") as f:
        f.write(report_md)
    print(f"[+] Saved master stress report to {report_path}")

if __name__ == "__main__":
    build_reports()
