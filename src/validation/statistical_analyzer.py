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
            "pearson_base": 0.0,
            "pearson_no_gate": 0.0,
            "pearson_no_mat": 0.0,
            "return_base": 0.0,
            "return_no_gate": 0.0,
            "return_no_mat": 0.0,
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
    
    # Feature Ablations
    strat_returns = []
    conf_base = []
    conf_no_mat = []
    
    # Ablation 1: Disable Volatility Gate (reconstructed action and confidence)
    strat_returns_no_gate = []
    conf_no_gate = []
    
    for row in rows:
        ret_10 = row["forward_returns"]["return_10"]
        action = row["suggested_action"]
        strat_ret = get_directional_return(action, ret_10)
        strat_returns.append(strat_ret)
        
        factors = row.get("confidence_factors", {})
        trend = factors.get("trend_alignment", 0)
        mat = factors.get("trend_maturity_modifier", 0)
        
        conf_base.append(max(0.0, trend + mat))
        conf_no_mat.append(max(0.0, trend + 0.0))
        
        # Reconstruction for Ablation 1: Disable Volatility Gate
        metrics = row.get("metrics", {})
        sma_5 = metrics.get("sma_5", 0.0)
        sma_20 = metrics.get("sma_20", 0.0)
        current_price = metrics.get("current_price", 1.0)
        
        if abs(sma_5 - sma_20) < (0.0005 * current_price):
            ungated_action = "Hold"
        elif sma_5 > sma_20:
            ungated_action = "Buy"
        else:
            ungated_action = "Sell"
            
        ungated_strat_ret = get_directional_return(ungated_action, ret_10)
        strat_returns_no_gate.append(ungated_strat_ret)
        
        trend_align = 0.5 if ungated_action != "Hold" else 0.0
        conf_no_gate.append(max(0.0, trend_align + mat))

    def weighted_return(confs, rets):
        sum_c = sum(confs)
        if sum_c == 0:
            return 0.0
        return sum(c * r for c, r in zip(confs, rets)) / sum_c

    return {
        "random_baseline": random_baseline,
        "pure_trend_baseline": pure_trend_baseline,
        "index_baseline": index_baseline,
        
        "pearson_base": calculate_pearson(conf_base, strat_returns),
        "pearson_no_gate": calculate_pearson(conf_no_gate, strat_returns_no_gate),
        "pearson_no_mat": calculate_pearson(conf_no_mat, strat_returns),
        
        "return_base": weighted_return(conf_base, strat_returns),
        "return_no_gate": weighted_return(conf_no_gate, strat_returns_no_gate),
        "return_no_mat": weighted_return(conf_no_mat, strat_returns)
    }

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
    
    report = []
    report.append("=== PHASE 16: MACRO-ANCHORED CALIBRATION REPORT ===")
    report.append("")
    report.append("| Metric / Strategy Variant | Aggregated (All) | SPY Only | QQQ Only |")
    report.append("| --- | --- | --- | --- |")
    report.append("| **Baselines (Mean Return +10)** | | | |")
    report.append(f"| Random Coin-Flip | {metrics_agg['random_baseline']:+.6f} | {metrics_spy['random_baseline']:+.6f} | {metrics_qqq['random_baseline']:+.6f} |")
    report.append(f"| Pure Trend (SMA5 > SMA20) | {metrics_agg['pure_trend_baseline']:+.6f} | {metrics_spy['pure_trend_baseline']:+.6f} | {metrics_qqq['pure_trend_baseline']:+.6f} |")
    report.append(f"| Passive Index (Buy & Hold) | {metrics_agg['index_baseline']:+.6f} | {metrics_spy['index_baseline']:+.6f} | {metrics_qqq['index_baseline']:+.6f} |")
    report.append("| **Pearson Correlation (Conf vs Ret+10)** | | | |")
    report.append(f"| Base System | {metrics_agg['pearson_base']:+.4f} | {metrics_spy['pearson_base']:+.4f} | {metrics_qqq['pearson_base']:+.4f} |")
    report.append(f"| Ablation 1: Disable Volatility Gate | {metrics_agg['pearson_no_gate']:+.4f} | {metrics_spy['pearson_no_gate']:+.4f} | {metrics_qqq['pearson_no_gate']:+.4f} |")
    report.append(f"| Ablation 2: No Trend Maturity | {metrics_agg['pearson_no_mat']:+.4f} | {metrics_spy['pearson_no_mat']:+.4f} | {metrics_qqq['pearson_no_mat']:+.4f} |")
    report.append("| **Return Profile (Conf-Weighted Mean Strat Ret+10)** | | | |")
    report.append(f"| Base System | {metrics_agg['return_base']:+.6f} | {metrics_spy['return_base']:+.6f} | {metrics_qqq['return_base']:+.6f} |")
    report.append(f"| Ablation 1: Disable Volatility Gate | {metrics_agg['return_no_gate']:+.6f} | {metrics_spy['return_no_gate']:+.6f} | {metrics_qqq['return_no_gate']:+.6f} |")
    report.append(f"| Ablation 2: No Trend Maturity | {metrics_agg['return_no_mat']:+.6f} | {metrics_spy['return_no_mat']:+.6f} | {metrics_qqq['return_no_mat']:+.6f} |")
    
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
