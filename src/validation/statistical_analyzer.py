import os
import json
import math
import random

def parse_replay_journal(journal_path: str):
    """Parses replay_journal.jsonl into memory, filtering out rows with null forward_returns."""
    valid_rows = []
    with open(journal_path, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            fwd_returns = row.get("forward_returns", {})
            if (fwd_returns.get("return_1") is not None and 
                fwd_returns.get("return_3") is not None and 
                fwd_returns.get("return_10") is not None):
                valid_rows.append(row)
    return valid_rows

def compute_mean(values: list) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)

def compute_std(values: list, mean: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)

def calculate_pearson(x: list, y: list) -> float:
    n = len(x)
    if n == 0 or len(y) != n:
        return 0.0
    mean_x = compute_mean(x)
    mean_y = compute_mean(y)
    numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    denominator = math.sqrt(sum((x[i] - mean_x) ** 2 for i in range(n)) * sum((y[i] - mean_y) ** 2 for i in range(n)))
    if denominator == 0:
        return 0.0
    return numerator / denominator

def get_directional_return(action, ret_10):
    if action == "Buy":
        return ret_10
    elif action == "Sell":
        return -ret_10
    return 0.0

def evaluate_metrics(rows):
    if not rows:
        return {
            "random_baseline": 0.0,
            "pure_trend_baseline": 0.0,
            "index_baseline": 0.0,
            
            "pearson_a": 0.0,
            "pearson_b": 0.0,
            "pearson_c": 0.0,
            
            "return_a": 0.0,
            "return_b": 0.0,
            "return_c": 0.0,
        }

    # Baselines
    random.seed(42)  # mathematical determinism
    random_sim_returns = []
    for _ in range(1000):
        sim_sum = 0
        for row in rows:
            ret_10 = row["forward_returns"]["return_10"]
            action = random.choice([1, -1])
            sim_sum += action * ret_10
        random_sim_returns.append(sim_sum / len(rows))
    random_baseline = compute_mean(random_sim_returns)

    pure_trend_returns = []
    index_returns = []
    
    for row in rows:
        ret_10 = row["forward_returns"]["return_10"]
        index_returns.append(ret_10)
        
        metrics = row.get("metrics", {})
        sma_5 = metrics.get("sma_5", 0)
        sma_20 = metrics.get("sma_20", 0)
        
        if sma_5 > sma_20:
            pure_trend_returns.append(ret_10)
        else:
            pure_trend_returns.append(-ret_10)
            
    pure_trend_baseline = compute_mean(pure_trend_returns)
    index_baseline = compute_mean(index_returns)
    
    # Configurations
    confs_a = []
    rets_a = []
    
    confs_b = []
    rets_b = []
    
    confs_c = []
    rets_c = []
    
    for row in rows:
        ret_10 = row["forward_returns"]["return_10"]
        metrics = row.get("metrics", {})
        sma_5 = metrics.get("sma_5", 0.0)
        sma_20 = metrics.get("sma_20", 0.0)
        
        # Configuration A (Raw Signal)
        action_a = "Buy" if sma_5 > sma_20 else "Sell"
        conf_a = 1.0
        ret_a = get_directional_return(action_a, ret_10)
        confs_a.append(conf_a)
        rets_a.append(ret_a)
        
        # Configuration B (Temporal System - No Macro Veto)
        regime = row.get("market_regime", "Congestion")
        action_b = "Buy" if regime == "Bull" else ("Sell" if regime == "Bear" else "Hold")
        ret_b = get_directional_return(action_b, ret_10)
        trend_alignment_b = 0.5 if action_b != "Hold" else 0.0
        maturity_mod = row.get("confidence_factors", {}).get("trend_maturity_modifier", 0.0)
        conf_b = max(0.0, trend_alignment_b + maturity_mod)
        confs_b.append(conf_b)
        rets_b.append(ret_b)
        
        # Configuration C (Complete Topology)
        action_c = row.get("suggested_action", "Hold")
        ret_c = get_directional_return(action_c, ret_10)
        conf_c = row.get("base_confidence", 0.0)
        confs_c.append(conf_c)
        rets_c.append(ret_c)

    def weighted_return(confs, rets):
        sum_c = sum(confs)
        if sum_c == 0:
            return 0.0
        return sum(c * r for c, r in zip(confs, rets)) / sum_c

    return {
        "random_baseline": random_baseline,
        "pure_trend_baseline": pure_trend_baseline,
        "index_baseline": index_baseline,
        
        "pearson_a": calculate_pearson(confs_a, rets_a),
        "pearson_b": calculate_pearson(confs_b, rets_b),
        "pearson_c": calculate_pearson(confs_c, rets_c),
        
        "return_a": weighted_return(confs_a, rets_a),
        "return_b": weighted_return(confs_b, rets_b),
        "return_c": weighted_return(confs_c, rets_c)
    }

def analyze_transition_zones(rows):
    """
    Groups chronologically sorted rows by symbol, finds crossing points of 130-period sma_macro,
    isolates Regime Transition Zone windows of +/- 15 bars, and calculates return profiles.
    """
    by_symbol = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = []
        by_symbol[sym].append(r)
        
    results = {}
    for sym in ["SPY", "QQQ"]:
        sym_rows = by_symbol.get(sym, [])
        sym_rows.sort(key=lambda x: x["timestamp"])
        
        crossings = []
        n = len(sym_rows)
        for i in range(1, n):
            prev_price = sym_rows[i-1]["metrics"]["current_price"]
            prev_macro = sym_rows[i-1]["metrics"]["sma_macro"]
            curr_price = sym_rows[i]["metrics"]["current_price"]
            curr_macro = sym_rows[i]["metrics"]["sma_macro"]
            
            prev_diff = prev_price - prev_macro
            curr_diff = curr_price - curr_macro
            
            if prev_diff * curr_diff < 0:
                crossings.append(i)
                
        # Isolate indices
        zone_indices = set()
        for idx in crossings:
            start = max(0, idx - 15)
            end = min(n - 1, idx + 15)
            for j in range(start, end + 1):
                zone_indices.add(j)
                
        zone_rows = [sym_rows[j] for j in sorted(zone_indices)]
        
        # Calculate returns inside transition zone
        ret1_list = []
        ret3_list = []
        ret10_list = []
        for r in zone_rows:
            action = r["suggested_action"]
            fwd = r["forward_returns"]
            
            ret1 = fwd.get("return_1")
            ret3 = fwd.get("return_3")
            ret10 = fwd.get("return_10")
            
            if ret1 is not None:
                ret1_list.append(get_directional_return(action, ret1))
            if ret3 is not None:
                ret3_list.append(get_directional_return(action, ret3))
            if ret10 is not None:
                ret10_list.append(get_directional_return(action, ret10))
                
        results[sym] = {
            "crossings_count": len(crossings),
            "zone_bars_count": len(zone_rows),
            "mean_ret1": compute_mean(ret1_list),
            "mean_ret3": compute_mean(ret3_list),
            "mean_ret10": compute_mean(ret10_list),
        }
    return results

def analyze(journal_path: str):
    if not os.path.exists(journal_path):
        print(f"Journal file not found: {journal_path}")
        return
        
    rows = parse_replay_journal(journal_path)
    if not rows:
        print("No valid rows found to analyze.")
        return
        
    spy_rows = [r for r in rows if r["symbol"] == "SPY"]
    qqq_rows = [r for r in rows if r["symbol"] == "QQQ"]
    
    metrics_agg = evaluate_metrics(rows)
    metrics_spy = evaluate_metrics(spy_rows)
    metrics_qqq = evaluate_metrics(qqq_rows)
    
    zone_results = analyze_transition_zones(rows)
    
    report = []
    report.append("=== PHASE 19: ASYMMETRIC HYSTERESIS CALIBRATION REPORT ===")
    report.append("")
    report.append("### SECTION 1: REGIME BOUNDARY STRESS TESTING (LAG TAX ISOLATION)")
    report.append("| Symbol | Total Crossings | Zone Size (Bars) | Mean Return +1 | Mean Return +3 | Mean Return +10 |")
    report.append("| --- | --- | --- | --- | --- | --- |")
    for sym in ["SPY", "QQQ"]:
        res = zone_results.get(sym, {"crossings_count": 0, "zone_bars_count": 0, "mean_ret1": 0.0, "mean_ret3": 0.0, "mean_ret10": 0.0})
        report.append(f"| {sym} | {res['crossings_count']} | {res['zone_bars_count']} | {res['mean_ret1']:+.6f} | {res['mean_ret3']:+.6f} | {res['mean_ret10']:+.6f} |")
    report.append("")
    report.append("### SECTION 2: MARGINAL ATTRIBUTION MATRIX")
    report.append("| Metric / Strategy Variant | Aggregated (All) | SPY Only | QQQ Only |")
    report.append("| --- | --- | --- | --- |")
    report.append("| **Baselines (Mean Return +10)** | | | |")
    report.append(f"| Random Coin-Flip | {metrics_agg['random_baseline']:+.6f} | {metrics_spy['random_baseline']:+.6f} | {metrics_qqq['random_baseline']:+.6f} |")
    report.append(f"| Pure Trend (SMA5 > SMA20) | {metrics_agg['pure_trend_baseline']:+.6f} | {metrics_spy['pure_trend_baseline']:+.6f} | {metrics_qqq['pure_trend_baseline']:+.6f} |")
    report.append(f"| Passive Index (Buy & Hold) | {metrics_agg['index_baseline']:+.6f} | {metrics_spy['index_baseline']:+.6f} | {metrics_qqq['index_baseline']:+.6f} |")
    report.append("| **Pearson Correlation (Conf vs Ret+10)** | | | |")
    report.append(f"| Configuration A (Raw Signal) | {metrics_agg['pearson_a']:+.4f} | {metrics_spy['pearson_a']:+.4f} | {metrics_qqq['pearson_a']:+.4f} |")
    report.append(f"| Configuration B (Temporal System) | {metrics_agg['pearson_b']:+.4f} | {metrics_spy['pearson_b']:+.4f} | {metrics_qqq['pearson_b']:+.4f} |")
    report.append(f"| Configuration C (Complete Topology) | {metrics_agg['pearson_c']:+.4f} | {metrics_spy['pearson_c']:+.4f} | {metrics_qqq['pearson_c']:+.4f} |")
    report.append("| **Return Profile (Conf-Weighted Mean Strat Ret+10)** | | | |")
    report.append(f"| Configuration A (Raw Signal) | {metrics_agg['return_a']:+.6f} | {metrics_spy['return_a']:+.6f} | {metrics_qqq['return_a']:+.6f} |")
    report.append(f"| Configuration B (Temporal System) | {metrics_agg['return_b']:+.6f} | {metrics_spy['return_b']:+.6f} | {metrics_qqq['return_b']:+.6f} |")
    report.append(f"| Configuration C (Complete Topology) | {metrics_agg['return_c']:+.6f} | {metrics_spy['return_c']:+.6f} | {metrics_qqq['return_c']:+.6f} |")
    
    output_str = "\n".join(report)
    print(output_str)
    
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    report_path = os.path.join(project_root, "out", "calibration_report.md")
    
    with open(report_path, "w") as f:
        f.write(output_str + "\n")

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    journal_path = os.path.join(project_root, "out", "replay_journal.jsonl")
    analyze(journal_path)
