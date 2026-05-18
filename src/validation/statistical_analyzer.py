import os
import json
import math

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

def analyze(journal_path: str):
    if not os.path.exists(journal_path):
        print(f"Journal file not found: {journal_path}")
        return
        
    rows = parse_replay_journal(journal_path)
    if not rows:
        print("No valid rows found to analyze.")
        return
        
    print("=== SYSTEM BEHAVIORAL CALIBRATION REPORT ===")
    print(f"Total Valid Horizons Analyzed: {len(rows)}\n")
    
    # 1. Confidence Stratification
    print("--- Confidence Stratification (Return +10) ---")
    confidence_groups = {}
    for row in rows:
        conf = row.get("base_confidence", 0.0)
        ret_10 = row["forward_returns"]["return_10"]
        confidence_groups.setdefault(conf, []).append(ret_10)
        
    conf_stats = []
    for conf in sorted(confidence_groups.keys()):
        vals = confidence_groups[conf]
        mean_ret = compute_mean(vals)
        std_ret = compute_std(vals, mean_ret)
        conf_stats.append({"conf": conf, "mean": mean_ret, "std": std_ret, "count": len(vals)})
        print(f"Confidence {conf:.2f}: Count={len(vals):<3} | Mean = {mean_ret:+.6f} | Std = {std_ret:.6f}")
        
    # Check for Confidence Inversion Anomalies
    for i in range(1, len(conf_stats)):
        if conf_stats[i]["mean"] < conf_stats[i-1]["mean"]:
            print(f"   [WARNING] Confidence Inversion Detected: Higher confidence ({conf_stats[i]['conf']:.2f}) produced lower returns than ({conf_stats[i-1]['conf']:.2f}).")
            
    print("\n--- Fatigue Validity Check ---")
    fatigue_ratios = []
    returns_10 = []
    for row in rows:
        telemetry = row.get("state_telemetry", {})
        fatigue = telemetry.get("fatigue_ratio")
        ret_10 = row["forward_returns"]["return_10"]
        if fatigue is not None:
            fatigue_ratios.append(fatigue)
            returns_10.append(ret_10)
            
    if fatigue_ratios:
        pearson_r = calculate_pearson(fatigue_ratios, returns_10)
        print(f"Pearson Correlation (Fatigue vs Return +10): {pearson_r:+.4f}")
        if pearson_r > 0:
            print("   [WARNING] Fatigue Exhaustion Inversion Detected: Correlation is positive! Exhausted trends are producing higher returns, breaking the fatigue heuristic logic.")
        else:
            print("   [VALID] Fatigue correlation is negative (higher fatigue leads to lower returns).")
    else:
        print("No fatigue data found.")
        
    print("\n--- Regime Performance Split ---")
    regime_groups = {}
    for row in rows:
        regime = row.get("market_regime", "Unknown")
        ret_3 = row["forward_returns"]["return_3"]
        ret_10 = row["forward_returns"]["return_10"]
        regime_groups.setdefault(regime, {"ret_3": [], "ret_10": []})
        regime_groups[regime]["ret_3"].append(ret_3)
        regime_groups[regime]["ret_10"].append(ret_10)
        
    for regime in sorted(regime_groups.keys()):
        ret_3_vals = regime_groups[regime]["ret_3"]
        ret_10_vals = regime_groups[regime]["ret_10"]
        mean_3 = compute_mean(ret_3_vals)
        mean_10 = compute_mean(ret_10_vals)
        print(f"Regime {regime:<10}: Count={len(ret_3_vals):<3} | Avg Return +3 = {mean_3:+.6f} | Avg Return +10 = {mean_10:+.6f}")

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    journal_path = os.path.join(project_root, "out", "replay_journal.jsonl")
    analyze(journal_path)
