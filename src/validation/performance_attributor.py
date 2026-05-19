import os
import sys
import pandas as pd
import numpy as np

project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

def calculate_sharpe(returns):
    if len(returns) < 2:
        return 0.0
    # Annualized Sharpe assuming 252 trading days and average trades per day
    std = returns.std()
    if std == 0:
        return 0.0
    return (returns.mean() / std) * np.sqrt(252)

def calculate_sortino(returns):
    if len(returns) < 2:
        return 0.0
    neg_returns = returns[returns < 0]
    if len(neg_returns) < 2:
        return 0.0
    neg_std = neg_returns.std()
    if neg_std == 0:
        return 0.0
    return (returns.mean() / neg_std) * np.sqrt(252)

def calculate_expectancy(df):
    """Expectancy = (Win Rate * Avg Win) - (Loss Rate * Avg Loss)"""
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    
    total = len(df)
    if total == 0:
        return 0.0
        
    win_rate = len(wins) / total
    loss_rate = len(losses) / total
    
    avg_win = wins["pnl"].mean() if not wins.empty else 0.0
    avg_loss = abs(losses["pnl"].mean()) if not losses.empty else 0.0
    
    return (win_rate * avg_win) - (loss_rate * avg_loss)

def generate_markdown_table(df_stats, title):
    md = f"### {title}\n\n"
    if df_stats.empty:
        return md + "*No data available for this segment.*\n\n"
    
    # Format floats nicely
    df_formatted = df_stats.copy()
    for col in df_formatted.columns:
        if df_formatted[col].dtype == 'float64':
            df_formatted[col] = df_formatted[col].map(lambda x: f"{x:.4f}" if abs(x) < 1 else f"{x:.2f}")
            
    # Headers
    headers = ["Segment"] + list(df_formatted.columns)
    md += "| " + " | ".join(headers) + " |\n"
    md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    
    # Rows
    for idx, row in df_formatted.iterrows():
        row_vals = [str(idx)] + [str(row[c]) for c in df_formatted.columns]
        md += "| " + " | ".join(row_vals) + " |\n"
        
    md += "\n"
    return md

def run_attribution():
    out_dir = os.path.join(project_root, "out")
    csv_path = os.path.join(out_dir, "trade_dataset.csv")
    
    if not os.path.exists(csv_path):
        print(f"[!] Error: {csv_path} does not exist. Cannot run attribution.")
        return
        
    df = pd.read_csv(csv_path)
    if df.empty:
        print("[!] Trade dataset is empty. Cannot run attribution.")
        return

    # Standardize columns
    if "r_multiple" in df.columns and "realized_r_multiple" not in df.columns:
        df["realized_r_multiple"] = df["r_multiple"]
        
    # Ensure datetime parsing
    df["datetime"] = pd.to_datetime(df["date"])
    
    # 1. Overall Metrics
    total_trades = len(df)
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100.0 if total_trades > 0 else 0.0
    
    net_profit = df["pnl"].sum()
    total_gains = wins["pnl"].sum()
    total_losses = abs(losses["pnl"].sum())
    profit_factor = total_gains / total_losses if total_losses > 0 else (total_gains if total_gains > 0 else 1.0)
    
    expectancy = calculate_expectancy(df)
    
    # Simple daily returns for Sharpe/Sortino
    daily_pnl = df.groupby("date")["pnl"].sum()
    sharpe = calculate_sharpe(daily_pnl)
    sortino = calculate_sortino(daily_pnl)
    
    # Max drawdown
    df_sorted = df.sort_values("datetime")
    cum_pnl = df_sorted["pnl"].cumsum()
    peak = cum_pnl.cummax()
    drawdown = cum_pnl - peak
    max_dd = drawdown.min()

    # 2. Segment by SPY Regime
    regime_groups = df.groupby("regime")
    regime_records = {}
    for regime, group in regime_groups:
        regime_records[regime] = {
            "Trades": len(group),
            "Win Rate (%)": len(group[group["pnl"] > 0]) / len(group) * 100.0,
            "Net Profit ($)": group["pnl"].sum(),
            "Profit Factor": group[group["pnl"] > 0]["pnl"].sum() / abs(group[group["pnl"] <= 0]["pnl"].sum()) if len(group[group["pnl"] <= 0]) > 0 else 1.0,
            "Avg R-Multiple": group["realized_r_multiple"].mean(),
            "Avg MFE (%)": group["mfe"].mean(),
            "Avg MAE (%)": group["mae"].mean()
        }
    df_regime = pd.DataFrame(regime_records).T

    # 3. Segment by Transition vs Stable Zone
    zone_groups = df.groupby("is_transition")
    zone_records = {}
    for zone_val, group in zone_groups:
        label = "Transition Zone" if zone_val else "Stable Zone"
        zone_records[label] = {
            "Trades": len(group),
            "Win Rate (%)": len(group[group["pnl"] > 0]) / len(group) * 100.0,
            "Net Profit ($)": group["pnl"].sum(),
            "Profit Factor": group[group["pnl"] > 0]["pnl"].sum() / abs(group[group["pnl"] <= 0]["pnl"].sum()) if len(group[group["pnl"] <= 0]) > 0 else 1.0,
            "Avg R-Multiple": group["realized_r_multiple"].mean(),
            "Avg MFE (%)": group["mfe"].mean(),
            "Avg MAE (%)": group["mae"].mean()
        }
    df_zone = pd.DataFrame(zone_records).T

    # 4. Segment by Catalyst Type
    cat_groups = df.groupby("catalyst_type")
    cat_records = {}
    for cat, group in cat_groups:
        cat_records[cat] = {
            "Trades": len(group),
            "Win Rate (%)": len(group[group["pnl"] > 0]) / len(group) * 100.0,
            "Net Profit ($)": group["pnl"].sum(),
            "Profit Factor": group[group["pnl"] > 0]["pnl"].sum() / abs(group[group["pnl"] <= 0]["pnl"].sum()) if len(group[group["pnl"] <= 0]) > 0 else 1.0,
            "Avg R-Multiple": group["realized_r_multiple"].mean(),
            "Avg MFE (%)": group["mfe"].mean(),
            "Avg MAE (%)": group["mae"].mean()
        }
    df_cat = pd.DataFrame(cat_records).T

    # 5. Segment by Hold Duration (Bins: Short <30, Mid 30-120, Long >120 mins)
    df["hold_category"] = pd.cut(df["hold_duration"], bins=[-1, 30, 120, 9999], labels=["Short (<30m)", "Medium (30-120m)", "Long (>120m)"])
    hold_groups = df.groupby("hold_category")
    hold_records = {}
    for hold_cat, group in hold_groups:
        hold_records[hold_cat] = {
            "Trades": len(group),
            "Win Rate (%)": len(group[group["pnl"] > 0]) / len(group) * 100.0 if len(group) > 0 else 0.0,
            "Net Profit ($)": group["pnl"].sum() if len(group) > 0 else 0.0,
            "Profit Factor": group[group["pnl"] > 0]["pnl"].sum() / abs(group[group["pnl"] <= 0]["pnl"].sum()) if len(group[group["pnl"] <= 0]) > 0 else 1.0,
            "Avg R-Multiple": group["realized_r_multiple"].mean() if len(group) > 0 else 0.0,
            "Avg MFE (%)": group["mfe"].mean() if len(group) > 0 else 0.0,
            "Avg MAE (%)": group["mae"].mean() if len(group) > 0 else 0.0
        }
    df_hold = pd.DataFrame(hold_records).T

    # 6. Slippage & Execution Realism Analysis
    avg_slippage_dollar = df["slippage_estimate"].mean()
    avg_latency_ms = df["execution_latency_estimate"].mean()
    avg_spread_proxy = df["spread_proxy"].mean()
    
    # Prepare Validation Report Markdown
    report_md = f"""# Regime Validation Report

This report evaluates the **Catalyst-Driven Momentum Strategy** across multiple historical market environments (2020-2026).

## Executive Performance Summary

- **Total Trades Executed**: {total_trades}
- **Overall Win Rate**: {win_rate:.2f}%
- **Cumulative Net Profit**: ${net_profit:.2f}
- **Profit Factor**: {profit_factor:.2f}
- **Mathematical Expectancy (per trade)**: ${expectancy:.2f}
- **Annualized Sharpe Ratio**: {sharpe:.4f}
- **Annualized Sortino Ratio**: {sortino:.4f}
- **Max Strategy Drawdown**: ${max_dd:.2f}

---

## Performance by Market Regime

Market regimes are classified daily using SPY volatility, trend direction, rate-of-change, and drawdown.

{generate_markdown_table(df_regime, "Regime Attribution Matrix")}

---

## Transition Zone Analysis

Isolates performance during the first 10% of any new market regime block to evaluate edge decay during regime boundaries.

{generate_markdown_table(df_zone, "Regime Transition Attribution Matrix")}

---

## Slippage & Execution Realism Telemetry

Real-world execution friction, scaled by historical spreads and volume spikes.

- **Average Rolling Spread Proxy**: {avg_spread_proxy * 100:.4f}% of closing price
- **Average Deducted Slippage (per trade)**: ${avg_slippage_dollar:.4f}
- **Average Execution Latency**: {avg_latency_ms:.2f} ms
"""

    report_path = os.path.join(out_dir, "regime_validation_report.md")
    with open(report_path, "w") as f:
        f.write(report_md)
    print(f"[+] Saved Regime Validation Report to {report_path}")

    # Prepare Performance Attribution Report
    attr_md = f"""# Performance Attribution Report

This report evaluates return decomposition, trade telemetry, and catalyst efficiency.

## Catalyst Efficiency Breakdown

{generate_markdown_table(df_cat, "Catalyst Segment Performance")}

---

## Trade Duration & Drawdown Sensitivity

{generate_markdown_table(df_hold, "Performance by Hold Duration Bins")}

---

## Macro Robustness Summary & Key Takeaways

1. **Regime Proximity Defense**: How well the volatility-buffered stop line protected capital in bears.
2. **Transition Edge Decay**: The performance difference between stable regimes and transition zones.
3. **Execution Realistic Penalties**: A realistic baseline including volatility-scaled slippage and execution latency.
"""

    attr_path = os.path.join(out_dir, "performance_attribution_report.md")
    with open(attr_path, "w") as f:
        f.write(attr_md)
    print(f"[+] Saved Performance Attribution Report to {attr_path}")

if __name__ == "__main__":
    run_attribution()
