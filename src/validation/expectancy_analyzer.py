import os
import json
import pandas as pd
import numpy as np
from datetime import datetime
import zoneinfo
import sys

# Add the project root to sys.path if not present
project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.validation.portfolio_simulator import execute_backtest

def calculate_time_bucket(dt):
    dec_hour = dt.hour + dt.minute / 60.0
    if 9.5 <= dec_hour < 11.5:
        return "morning"
    elif 11.5 <= dec_hour < 14.0:
        return "midday"
    elif 14.0 <= dec_hour <= 16.0:
        return "afternoon"
    return "other"

def get_signal_density(signal_history, current_idx, window):
    count = 0
    for idx in range(max(0, current_idx - window), current_idx):
        if signal_history[idx] in ["Buy", "Sell"]:
            count += 1
    return count

def process_journal(journal_path):
    events = []
    signal_history = []
    last_regime = None
    last_action = None
    consecutive_action_count = 0
    
    with open(journal_path, 'r') as f:
        for line in f:
            if not line.strip(): continue
            event = json.loads(line)
            ts = event["timestamp"]
            action = event["suggested_action"]
            regime = event["market_regime"]
            
            signal_history.append(action)
            current_idx = len(signal_history)
            
            density = get_signal_density(signal_history, current_idx, 10)
            
            if action in ["Buy", "Sell"]:
                if action == last_action and regime == last_regime:
                    consecutive_action_count += 1
                else:
                    consecutive_action_count = 1
                last_action = action
                last_regime = regime
            else:
                consecutive_action_count = 0
                
            event["signal_density_10"] = density
            event["consecutive_action_count"] = consecutive_action_count
            events.append(event)
            
    return { e["timestamp"] + "_" + e["symbol"]: e for e in events }

def safe_qcut(series, q):
    if len(series.unique()) < 2:
        return pd.Series(["Q_single"] * len(series), index=series.index)
    try:
        return pd.qcut(series, q=q, duplicates='drop').astype(str)
    except:
        return pd.Series(["Q_unknown"] * len(series), index=series.index)

def calculate_expectancy(group, is_raw=False):
    tc = len(group)
    if tc == 0: return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    
    if is_raw:
        wins = group[group['ret_atr'] > 0]
        losses = group[group['ret_atr'] <= 0]
    else:
        wins = group[group['pnl'] > 0]
        losses = group[group['pnl'] <= 0]
        
    win_rate = len(wins) / tc
    loss_rate = 1.0 - win_rate
    
    avg_win = wins['ret_atr'].mean() if len(wins) > 0 else 0.0
    avg_loss = abs(losses['ret_atr'].mean()) if len(losses) > 0 else 0.0
    
    exp = (win_rate * avg_win) - (loss_rate * avg_loss)
    
    returns = group['ret_atr']
    sharpe = returns.mean() / returns.std() if len(returns) > 1 and returns.std() > 0 else 0.0
    
    return tc, win_rate, avg_win, avg_loss, exp, sharpe

def analyze():
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    out_dir = os.path.join(project_root, "out")
    
    regimes = [1, 2, 3, 4, 5]
    all_raw_signals = []
    all_trades = []
    
    for r in regimes:
        journal_name = f"replay_journal_regime_{r}.jsonl"
        journal_path = os.path.join(out_dir, journal_name)
        if not os.path.exists(journal_path):
            continue
            
        print(f"Processing {journal_name}...")
        events_by_key = process_journal(journal_path)
        
        sim_results = execute_backtest("structural_buffered", journal_path)
        trades_dict = {}
        for sym, diag in sim_results["symbol_diagnostics"].items():
            for t in diag["trades"]:
                trades_dict[t["entry_timestamp"] + "_" + sym] = t

        for key, event in events_by_key.items():
            dt = datetime.fromisoformat(event["timestamp"])
            time_bucket = calculate_time_bucket(dt.astimezone(zoneinfo.ZoneInfo("America/New_York")))
            
            price = event["metrics"].get("current_price", 1.0)
            vwap = event["metrics"].get("vwap", price)
            vwap_dist = abs(price - vwap) / vwap * 100
            obv = event["metrics"].get("obv", 0.0)
            obv_sma = event["metrics"].get("obv_sma20", 0.0)
            obv_slope = obv - obv_sma
            atr = event["metrics"].get("atr_14", 0.001)
            atr_pct = atr / price if price > 0 else 0.001
            
            action = event["suggested_action"]
            
            # Counterfactual signal detection for Temporal Gate Analysis
            if action == "Hold" and time_bucket == "morning":
                is_macro_bull = event["confidence_factors"].get("is_macro_bull", 0) == 1
                if price > vwap and obv > obv_sma and is_macro_bull:
                    action = "Buy"
                elif price < vwap and obv < obv_sma and not is_macro_bull:
                    action = "Sell"
                    
            if action in ["Buy", "Sell"]:
                # Raw signal
                fwd = event.get("forward_returns", {}) or {}
                ret_3 = fwd.get("return_3")
                if ret_3 is None:
                    ret_3 = 0.0
                if action == "Sell":
                    ret_3 = -ret_3
                ret_atr_raw = ret_3 / atr_pct if atr_pct > 0 else 0.0
                
                base_dict = {
                    "regime": r,
                    "action": action,
                    "macro_regime": event["market_regime"],
                    "time_bucket": time_bucket,
                    "vwap_dist": vwap_dist,
                    "obv_slope": obv_slope,
                    "fatigue_ratio": event["state_telemetry"].get("fatigue_ratio", 0.0),
                    "density": event["signal_density_10"],
                    "consecutive": event["consecutive_action_count"]
                }
                
                raw_dict = base_dict.copy()
                raw_dict["ret_3"] = ret_3
                raw_dict["ret_atr"] = ret_atr_raw
                all_raw_signals.append(raw_dict)
                
                # Executed trade
                if key in trades_dict:
                    t = trades_dict[key]
                    pnl_pct = t["pnl"] / t["entry_cash"] if t["entry_cash"] > 0 else 0
                    ret_atr_exec = pnl_pct / atr_pct if atr_pct > 0 else 0
                    
                    exec_dict = base_dict.copy()
                    exec_dict["pnl"] = t["pnl"]
                    exec_dict["ret_atr"] = ret_atr_exec
                    all_trades.append(exec_dict)
                    
    df_raw = pd.DataFrame(all_raw_signals)
    df_exec = pd.DataFrame(all_trades)
    
    if len(df_raw) == 0:
        print("No raw signals found!")
        return
        
    df_raw['vwap_dist_bin'] = safe_qcut(df_raw['vwap_dist'], 10)
    df_raw['obv_slope_bin'] = safe_qcut(df_raw['obv_slope'], 10)
    
    if len(df_exec) > 0:
        df_exec['vwap_dist_bin'] = safe_qcut(df_exec['vwap_dist'], 10)
        df_exec['obv_slope_bin'] = safe_qcut(df_exec['obv_slope'], 10)
        
    # Generate Reports
    md_lines = ["# Phase 30: Conditional Expectancy Cartography", ""]
    md_lines.append(f"*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    
    total_raw = len(df_raw)
    total_exec = len(df_exec)
    
    def format_group(group_name, group_df, is_raw=False):
        tc, wr, aw, al, exp, sharpe = calculate_expectancy(group_df, is_raw)
        pct_total = (tc / (total_raw if is_raw else total_exec)) * 100
        pnl_contrib = 0.0 if is_raw else group_df['pnl'].sum()
        pnl_str = "N/A" if is_raw else f"${pnl_contrib:,.2f}"
        return f"| {group_name} | {tc} ({pct_total:.1f}%) | {wr*100:.1f}% | {aw:.2f} ATR | {al:.2f} ATR | **{exp:.2f} ATR** | {sharpe:.2f} | {pnl_str} |"
    
    md_lines.append("\n## VWAP Distance Deciles")
    md_lines.append("| Decile | Trades (% Total) | Win Rate | Avg Win (ATR) | Avg Loss (ATR) | Expectancy (ATR) | Sharpe | PnL Contrib |")
    md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    md_lines.append("**Raw Signals**")
    for b in sorted(df_raw['vwap_dist_bin'].unique()):
        md_lines.append(format_group(b, df_raw[df_raw['vwap_dist_bin'] == b], is_raw=True))
    if len(df_exec) > 0:
        md_lines.append("\n**Executed Trades**")
        md_lines.append("| Decile | Trades (% Total) | Win Rate | Avg Win (ATR) | Avg Loss (ATR) | Expectancy (ATR) | Sharpe | PnL Contrib |")
        md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for b in sorted(df_exec['vwap_dist_bin'].unique()):
            md_lines.append(format_group(b, df_exec[df_exec['vwap_dist_bin'] == b], is_raw=False))
            
    md_lines.append("\n## OBV Slope Deciles")
    md_lines.append("| Decile | Trades (% Total) | Win Rate | Avg Win (ATR) | Avg Loss (ATR) | Expectancy (ATR) | Sharpe | PnL Contrib |")
    md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    md_lines.append("**Raw Signals**")
    for b in sorted(df_raw['obv_slope_bin'].unique()):
        md_lines.append(format_group(b, df_raw[df_raw['obv_slope_bin'] == b], is_raw=True))
    if len(df_exec) > 0:
        md_lines.append("\n**Executed Trades**")
        md_lines.append("| Decile | Trades (% Total) | Win Rate | Avg Win (ATR) | Avg Loss (ATR) | Expectancy (ATR) | Sharpe | PnL Contrib |")
        md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for b in sorted(df_exec['obv_slope_bin'].unique()):
            md_lines.append(format_group(b, df_exec[df_exec['obv_slope_bin'] == b], is_raw=False))
            
    md_lines.append("\n## Signal Persistence (Consecutive Entries in Same Direction)")
    md_lines.append("| Consecutive Count | Trades (% Total) | Win Rate | Avg Win (ATR) | Avg Loss (ATR) | Expectancy (ATR) | Sharpe | PnL Contrib |")
    md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for c in sorted(df_exec['consecutive'].unique()) if len(df_exec) > 0 else []:
        if c > 0:
            md_lines.append(format_group(str(c), df_exec[df_exec['consecutive'] == c], is_raw=False))

    md_lines.append("\n## Cross-Dimensional Heatmap (Executed Trades, min 10 samples)")
    md_lines.append("| Condition | Trades (% Total) | Win Rate | Avg Win (ATR) | Avg Loss (ATR) | Expectancy (ATR) | Sharpe | PnL Contrib |")
    md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    
    intersections = []
    if len(df_exec) > 0:
        for macro in df_exec['macro_regime'].unique():
            for tb in df_exec['time_bucket'].unique():
                for v_bin in df_exec['vwap_dist_bin'].unique():
                    subset = df_exec[(df_exec['macro_regime'] == macro) & (df_exec['time_bucket'] == tb) & (df_exec['vwap_dist_bin'] == v_bin)]
                    if len(subset) >= 10:
                        tc, wr, aw, al, exp, sharpe = calculate_expectancy(subset, is_raw=False)
                        intersections.append({
                            "name": f"{macro} + {tb} + VWAP {v_bin}",
                            "tc": tc, "wr": wr, "aw": aw, "al": al, "exp": exp, "sharpe": sharpe,
                            "pnl": subset['pnl'].sum(), "pct_total": tc / total_exec * 100
                        })
                        
    intersections.sort(key=lambda x: x["exp"], reverse=True)
    md_lines.append("**Top 5 Conditions**")
    for x in intersections[:5]:
        md_lines.append(f"| {x['name']} | {x['tc']} ({x['pct_total']:.1f}%) | {x['wr']*100:.1f}% | {x['aw']:.2f} ATR | {x['al']:.2f} ATR | **{x['exp']:.2f} ATR** | {x['sharpe']:.2f} | ${x['pnl']:,.2f} |")
    
    md_lines.append("\n**Bottom 5 Conditions**")
    md_lines.append("| Condition | Trades (% Total) | Win Rate | Avg Win (ATR) | Avg Loss (ATR) | Expectancy (ATR) | Sharpe | PnL Contrib |")
    md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for x in intersections[-5:]:
        md_lines.append(f"| {x['name']} | {x['tc']} ({x['pct_total']:.1f}%) | {x['wr']*100:.1f}% | {x['aw']:.2f} ATR | {x['al']:.2f} ATR | **{x['exp']:.2f} ATR** | {x['sharpe']:.2f} | ${x['pnl']:,.2f} |")

    with open(os.path.join(out_dir, "expectancy_report.md"), "w") as f:
        f.write("\n".join(md_lines))
        
    print("[!] Wrote expectancy_report.md")
    
    # Counterfactual Temporal Gate Analysis (Temporal Regime Heatmap)
    tr_lines = ["# Counterfactual Temporal Gate Analysis", ""]
    tr_lines.append(f"*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    tr_lines.append("\n## By Regime: Before 11:30 vs After 11:30 (Raw Signal Expectancy in ATR units)")
    tr_lines.append("| Regime | Time | Trades | Win Rate | Expectancy (ATR) | Sharpe |")
    tr_lines.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
    
    for r in regimes:
        reg_raw = df_raw[df_raw['regime'] == r]
        before = reg_raw[reg_raw['time_bucket'] == 'morning']
        after = reg_raw[reg_raw['time_bucket'] != 'morning']
        
        tc_b, wr_b, aw_b, al_b, exp_b, sh_b = calculate_expectancy(before, is_raw=True)
        tc_a, wr_a, aw_a, al_a, exp_a, sh_a = calculate_expectancy(after, is_raw=True)
        
        tr_lines.append(f"| Regime {r} | Before 11:30 | {tc_b} | {wr_b*100:.1f}% | **{exp_b:.2f}** | {sh_b:.2f} |")
        tr_lines.append(f"| Regime {r} | After 11:30  | {tc_a} | {wr_a*100:.1f}% | **{exp_a:.2f}** | {sh_a:.2f} |")
        
    with open(os.path.join(out_dir, "temporal_regime_heatmap.md"), "w") as f:
        f.write("\n".join(tr_lines))
        
    print("[!] Wrote temporal_regime_heatmap.md")

if __name__ == "__main__":
    analyze()
