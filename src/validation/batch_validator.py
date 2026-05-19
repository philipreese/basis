import os
import json
from datetime import datetime
from src.validation.replay_engine import run_replay
from src.validation.portfolio_simulator import execute_backtest

# Define the 5 out-of-sample historical validation brackets
REGIMES = [
    {
        "id": 1,
        "name": "REGIME_1_2020 (Corona Crash & Recovery)",
        "start_date": "2020-02-15",
        "end_date": "2020-06-15"
    },
    {
        "id": 2,
        "name": "REGIME_2_2022 (Sustained Bear Market)",
        "start_date": "2022-01-01",
        "end_date": "2022-12-31"
    },
    {
        "id": 3,
        "name": "REGIME_3_2023 (Bull / Tech Recovery)",
        "start_date": "2023-02-15",
        "end_date": "2023-07-15"
    },
    {
        "id": 4,
        "name": "REGIME_4_2018 (Q4 Market Correction)",
        "start_date": "2018-10-01",
        "end_date": "2018-12-31"
    },
    {
        "id": 5,
        "name": "REGIME_5_2024 (Late Summer Chop / Pre-Election)",
        "start_date": "2024-07-01",
        "end_date": "2024-10-31"
    }
]

def run_batch_validation():
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    report_lines = []
    report_lines.append("=== PHASE 31: CROSS-SECTIONAL SPREAD ARBITRAGE REPORT ===")
    report_lines.append(f"\n*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    
    # 1. Embed Diagnostic Stationarity Summary
    diag_summary_path = os.path.join(out_dir, "diagnostic_summary.json")
    if os.path.exists(diag_summary_path):
        report_lines.append("\n### Pre-Implementation Spread Stationarity Pass")
        report_lines.append("| Regime | Volatility | RSC Mean | RSC Std | Max DD (%) | ADF t-stat | Stationary? | Half-Life (h) |")
        report_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        with open(diag_summary_path, "r") as f:
            diag_data = json.load(f)
        for r_res in diag_data["results"]:
            report_lines.append(
                f"| {r_res['name']} | {r_res['volatility'].upper()} | {r_res['rsc_mean']:.4f} | {r_res['rsc_std']:.4f} | {r_res['max_dd_rsc']:.2f}% | {r_res['t_stat']:.2f} | **{r_res['is_stationary']}** | {r_res['half_life']:.2f} |"
            )
            
    report_lines.append("\n### Cross-Validation Analytics Matrix (QQQ/SPY Spread Arbitrage)")
    report_lines.append("| Regime | Strategy Phase | Total Return (%) | Max Drawdown (%) | Completed Trades | Realized Sharpe Ratio |")
    report_lines.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
    
    print("=== STARTING PHASE 31 MULTI-REGIME OUT-OF-SAMPLE BATCH VALIDATION ===")
    
    for regime in REGIMES:
        reg_id = regime["id"]
        reg_name = regime["name"]
        start_dt = regime["start_date"]
        end_dt = regime["end_date"]
        journal_name = f"replay_journal_regime_{reg_id}.jsonl"
        journal_path = os.path.join(out_dir, journal_name)
        
        print(f"\n--- Running Replay for {reg_name} ({start_dt} to {end_dt}) ---")
        run_replay(
            symbols=["SPY", "QQQ"],
            mode="historical",
            timeframe_str="1h",
            start_date=start_dt,
            end_date=end_dt,
            journal_name=journal_name
        )
        
        print(f"--- Running Compounding Simulations for {reg_name} ---")
        p20 = execute_backtest(stops_mode="none", journal_path=journal_path)
        p21 = execute_backtest(stops_mode="trailing", journal_path=journal_path)
        p22 = execute_backtest(stops_mode="structural_raw", journal_path=journal_path)
        p23 = execute_backtest(stops_mode="structural_buffered", journal_path=journal_path)
        
        phases = [
            ("Phase 20 Baseline (Unprotected)", p20),
            ("Phase 21 Protected (Trailing)", p21),
            ("Phase 22 Structural (Raw)", p22),
            ("Phase 23 Structural (Buffered)", p23)
        ]
        
        first_row = True
        for phase_name, p_res in phases:
            tot_trades = sum(diag["trade_count"] for diag in p_res["symbol_diagnostics"].values())
            regime_col = f"**{reg_name}**" if first_row else ""
            
            report_lines.append(
                f"| {regime_col} | {phase_name} | {p_res['total_return']:.2f}% | {p_res['mdd']:.2f}% | {tot_trades} | {p_res['sharpe']:.4f} |"
            )
            first_row = False
            
    # Output to terminal
    report_text = "\n".join(report_lines)
    print("\n" + report_text)
    
    # Save to out/cross_validation_report_phase31.md
    report_path = os.path.join(out_dir, "cross_validation_report_phase31.md")
    with open(report_path, "w") as f:
        f.write(report_text)
        
    print(f"\n[!] Finalized out-of-sample batch validation report written cleanly to {report_path}")

if __name__ == "__main__":
    run_batch_validation()
