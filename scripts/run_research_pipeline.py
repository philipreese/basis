import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import json
import datetime
import pandas as pd
import numpy as np
import math
from tabulate import tabulate

from src.research.causal_feature_lab import CausalFeatureLab
from src.research.feature_survival_engine import FeatureSurvivalEngine
from src.research.leakage_auditor import LeakageAuditor

def classify_spy_regimes(df_spy: pd.DataFrame) -> pd.DataFrame:
    """
    Applies the regime classification rules from regime_classifier.py on SPY daily data.
    """
    df = df_spy.sort_index().copy()
    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    df["rolling_vol"] = df["log_return"].rolling(window=20).std() * math.sqrt(252) * 100.0
    df["sma_50"] = df["close"].rolling(window=50).mean()
    df["sma_200"] = df["close"].rolling(window=200).mean()
    df["rolling_return_20"] = ((df["close"] - df["close"].shift(20)) / df["close"].shift(20)) * 100.0
    df["max_252"] = df["close"].rolling(window=252).max()
    df["drawdown_252"] = ((df["close"] - df["max_252"]) / df["max_252"]) * 100.0

    regimes = []
    for idx, row in df.iterrows():
        if pd.isna(row["rolling_vol"]) or pd.isna(row["sma_200"]) or pd.isna(row["drawdown_252"]):
            regimes.append("CHOPPY_ROTATIONAL")
            continue
            
        vol = row["rolling_vol"]
        close = row["close"]
        sma_50 = row["sma_50"]
        sma_200 = row["sma_200"]
        ret_20 = row["rolling_return_20"]
        
        if vol > 22.0:
            regimes.append("HIGH_VOLATILITY")
        elif close < sma_200 and ret_20 < -3.0:
            regimes.append("TRENDING_BEAR")
        elif close > sma_50 and sma_50 > sma_200:
            if ret_20 > 4.0:
                regimes.append("MOMENTUM_EXPANSION")
            else:
                regimes.append("TRENDING_BULL")
        else:
            regimes.append("CHOPPY_ROTATIONAL")
            
    df["regime"] = regimes
    df["regime_change"] = df["regime"] != df["regime"].shift(1)
    df["segment_id"] = df["regime_change"].cumsum()
    
    is_transition_list = []
    # Group by segments to tag transition zones (first 10% of a continuous segment)
    for seg_id, group in df.groupby("segment_id"):
        n = len(group)
        transition_cutoff = max(1, int(math.ceil(0.10 * n)))
        for i in range(n):
            if i < transition_cutoff:
                is_transition_list.append(True)
            else:
                is_transition_list.append(False)
                
    df["is_transition"] = is_transition_list
    return df

def run_pipeline():
    print("=" * 60)
    print("        CAUSAL FEATURE RESEARCH PIPELINE STARTING")
    print("=" * 60)
    
    lab = CausalFeatureLab()
    
    # 1. Load watchlists
    watchlist_path = "out/watchlist_cache_quant.json"
    watchlist_quant = {}
    if os.path.exists(watchlist_path):
        with open(watchlist_path, "r") as f:
            watchlist_quant = json.load(f)
        print(f"[+] Loaded watchlist cache with {len(watchlist_quant)} dates.")
    else:
        print("[!] No watchlist cache found. Will generate features from bar cache filenames.")

    # 2. Scan bars cache
    cache_dir = "out/bars_cache"
    if not os.path.exists(cache_dir):
        print(f"[!] Bars cache directory '{cache_dir}' does not exist. Cannot proceed.")
        return
        
    bar_files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
    print(f"[+] Found {len(bar_files)} cached minute bar files.")
    
    # Parse available dates and tickers
    ticker_date_pairs = []
    all_tickers = set()
    all_dates = set()
    for file in bar_files:
        parts = file.replace(".json", "").split("_")
        if len(parts) >= 2:
            ticker = parts[0]
            date_str = parts[1]
            ticker_date_pairs.append((ticker, date_str, file))
            all_tickers.add(ticker)
            all_dates.add(date_str)
            
    # Sort pairs by date to keep historical alignment
    ticker_date_pairs.sort(key=lambda x: x[1])
    
    if not ticker_date_pairs:
        print("[!] No valid ticker-date pairs found in bars cache.")
        return
        
    start_date = min(all_dates)
    end_date = max(all_dates)
    print(f"[+] Processing period from {start_date} to {end_date} for tickers: {sorted(list(all_tickers))}")

    # 3. Load daily data for regime classification
    try:
        spy_daily_raw = lab.get_daily_bars("SPY", start_date, end_date)
        if spy_daily_raw.empty:
            print("[!] Failed to load SPY daily bars.")
            return
        spy_daily_df = classify_spy_regimes(spy_daily_raw)
        print(f"[+] Loaded and classified SPY regimes: {len(spy_daily_df)} daily bars.")
    except Exception as e:
        print(f"[!] Error building SPY regime data: {e}")
        return

    # Load daily data for all other tickers
    daily_dfs = {}
    for ticker in all_tickers:
        try:
            df = lab.get_daily_bars(ticker, start_date, end_date)
            if not df.empty:
                daily_dfs[ticker] = df
        except Exception as e:
            print(f"[!] Failed to load daily bars for {ticker}: {e}")

    # 4. Load or Generate Causal Feature Dataset
    dataset_path = "out/causal_feature_dataset.csv"
    if os.path.exists(dataset_path):
        print(f"[+] Found existing feature dataset at '{dataset_path}'. Loading directly...")
        df_full = pd.read_csv(dataset_path)
        print(f"[+] Feature matrix loaded: {len(df_full)} rows, {len(df_full.columns)} columns.")
    else:
        records = []
        print("[*] Generating causal features and outcome labels from raw bars cache...")
        
        for idx, (ticker, date_str, filename) in enumerate(ticker_date_pairs):
            if ticker not in daily_dfs:
                continue
                
            file_path = os.path.join(cache_dir, filename)
            try:
                with open(file_path, "r") as f:
                    bars = json.load(f)
            except Exception as e:
                continue
                
            # Get watchlist item if available
            watchlist_items = watchlist_quant.get(date_str, [])
            watchlist_item = {}
            for item in watchlist_items:
                if item.get("ticker") == ticker:
                    watchlist_item = item
                    break
                    
            # Generate baseline features
            features = lab.compute_features(
                ticker=ticker,
                date_str=date_str,
                minute_bars=bars,
                daily_df=daily_dfs[ticker],
                spy_daily_df=spy_daily_df,
                quant_watchlist_item=watchlist_item
            )
            
            if not features:
                continue
                
            # Generate baseline outcome (no friction, entry at T=09:35 close)
            outcomes_raw = lab.compute_outcomes(
                minute_bars=bars,
                features=features,
                apply_friction=False,
                delayed_entry_offset=0,
                worst_case_fills=False
            )
            
            # Generate friction-adjusted outcome
            outcomes_friction = lab.compute_outcomes(
                minute_bars=bars,
                features=features,
                apply_friction=True,
                delayed_entry_offset=1, # 1m entry latency
                worst_case_fills=False
            )
            
            # Generate worst-case outcome
            outcomes_worst = lab.compute_outcomes(
                minute_bars=bars,
                features=features,
                apply_friction=True,
                delayed_entry_offset=1,
                worst_case_fills=True
            )

            if not outcomes_raw or not outcomes_friction or not outcomes_worst:
                continue
                
            # Add labels and merge
            row = dict(features)
            
            # We'll suffix outcomes to keep them separate
            for k, v in outcomes_raw.items():
                row[k] = v
            for k, v in outcomes_friction.items():
                row[f"{k}_friction"] = v
            for k, v in outcomes_worst.items():
                row[f"{k}_worst"] = v

            records.append(row)
            
            if (idx + 1) % 500 == 0 or (idx + 1) == len(ticker_date_pairs):
                print(f"  Processed {idx + 1}/{len(ticker_date_pairs)} files...")

        df_full = pd.DataFrame(records)
        print(f"[+] Feature matrix constructed: {len(df_full)} rows, {len(df_full.columns)} columns.")
        
        # Save the full causal feature dataset
        os.makedirs("out", exist_ok=True)
        df_full.to_csv(dataset_path, index=False)
        print(f"[+] Saved full causal feature dataset to '{dataset_path}'.")

    # Pre-assign epochs in a vectorized way (extremely fast)
    if "epoch" not in df_full.columns:
        print("[*] Pre-assigning epochs to dataset...")
        df_full["date"] = pd.to_datetime(df_full["date"])
        df_full["epoch"] = "Out_of_Epoch"
        from src.research.feature_survival_engine import EPOCHS
        for name, start_str, end_str in EPOCHS:
            start = pd.to_datetime(start_str)
            end = pd.to_datetime(end_str)
            df_full.loc[(df_full["date"] >= start) & (df_full["date"] <= end), "epoch"] = name
        print("[+] Vectorized epoch assignment complete.")

    # Define the features to analyze (all numeric keys in features dict except meta keys)
    meta_keys = ["ticker", "date", "market_regime", "atr_14", "epoch"]
    feature_cols = [c for c in df_full.columns if c not in meta_keys and not c.startswith("future_") and not c.startswith("hit_")]
    
    # 5. Run Leakage Audit
    print("[*] Running Leakage Auditor...")
    auditor = LeakageAuditor()
    # We will test against future_15m_return
    auditor.audit_features(df_full, feature_cols, "future_15m_return")
    audit_report = auditor.generate_audit_report()
    
    # Filter out flagged features from further analysis
    flagged_features = auditor.flagged_features
    clean_features = [f for f in feature_cols if f not in flagged_features]
    print(f"[+] Leakage audit completed. Clean features: {len(clean_features)}/{len(feature_cols)}.")

    # 6. Run Feature Survival Analysis
    print("[*] Evaluating feature survival on 'future_15m_return'...")
    engine = FeatureSurvivalEngine(n_bins=5)
    
    results = []
    for feat in clean_features:
        res = engine.analyze_feature(df_full, feat, "future_15m_return")
        results.append(res)
        
    df_results = pd.DataFrame(results)
    
    # Rank by absolute Spearman correlation (monotonicity) for PASSED features
    df_passed = df_results[df_results["status"] == "PASSED"].copy()
    if not df_passed.empty:
        df_passed["abs_monotonicity"] = df_passed["monotonicity"].abs()
        df_passed = df_passed.sort_values("abs_monotonicity", ascending=False)
    
    # 7. Evaluate friction-adjusted outcomes survival
    print("[*] Evaluating feature survival on 'future_15m_return_friction'...")
    results_friction = []
    for feat in clean_features:
        res = engine.analyze_feature(df_full, feat, "future_15m_return_friction")
        results_friction.append(res)
    df_results_friction = pd.DataFrame(results_friction)
    
    # Save the output CSVs
    # Rankings
    rankings_cols = ["feature", "n_samples", "status", "monotonicity", "overall_edge", "stability_score", "bootstrap_p_value"]
    df_results[rankings_cols].to_csv("out/feature_rankings.csv", index=False)
    
    # Monotonicity
    df_results[["feature", "monotonicity"]].to_csv("out/feature_monotonicity.csv", index=False)
    
    # Stability
    # Build a nice matrix of feature vs epoch edges
    stability_records = []
    for res in results:
        rec = {"feature": res["feature"], "status": res["status"], "overall_edge": res["overall_edge"]}
        for ep, val in res["epoch_edges"].items():
            rec[f"edge_{ep}"] = val
        stability_records.append(rec)
    df_stability = pd.DataFrame(stability_records)
    df_stability.to_csv("out/epoch_stability_matrix.csv", index=False)

    # Friction-adjusted comparison
    friction_cols = ["feature", "status", "overall_edge_raw", "overall_edge_friction"]
    friction_compare = []
    for r_raw, r_fric in zip(results, results_friction):
        friction_compare.append({
            "feature": r_raw["feature"],
            "status": r_raw["status"],
            "overall_edge_raw": r_raw["overall_edge"],
            "overall_edge_friction": r_fric["overall_edge"]
        })
    df_friction = pd.DataFrame(friction_compare)
    df_friction.to_csv("out/friction_adjusted_edge_scan.csv", index=False)

    print("[+] All output files written to 'out/' directory.")

    # 8. Generate final report markdown
    report_path = "out/feature_survival_report.md"
    
    passed_summary_data = []
    failed_summary_data = []
    
    for r in results:
        feat_name = r["feature"]
        status = r["status"]
        if status == "PASSED":
            passed_summary_data.append([
                feat_name,
                r["n_samples"],
                f"{r['monotonicity']:.3f}",
                f"{r['overall_edge']:.4f}%",
                f"{r['stability_score'] * 100:.1f}%",
                f"{r['bootstrap_p_value']:.3f}"
            ])
        else:
            failed_summary_data.append([
                feat_name,
                status,
                f"{r['monotonicity']:.3f}",
                f"{r['overall_edge']:.4f}%",
                f"{r['stability_score'] * 100:.1f}%"
            ])
            
    passed_headers = ["Feature", "Samples", "Monotonicity (Spearman)", "Overall Edge", "Epoch Stability", "Bootstrap p-val"]
    failed_headers = ["Feature", "Failure Reason", "Monotonicity (Spearman)", "Overall Edge", "Epoch Stability"]

    passed_table = tabulate(passed_summary_data, headers=passed_headers, tablefmt="github") if passed_summary_data else "No features passed survival criteria."
    failed_table = tabulate(failed_summary_data, headers=failed_headers, tablefmt="github") if failed_summary_data else "No features failed survival criteria."

    # Compare raw vs friction edge in report
    friction_summary_data = []
    for item in friction_compare:
        friction_summary_data.append([
            item["feature"],
            f"{item['overall_edge_raw']:.4f}%",
            f"{item['overall_edge_friction']:.4f}%",
            f"{item['overall_edge_friction'] - item['overall_edge_raw']:.4f}%"
        ])
    friction_headers = ["Feature", "Raw Edge (no friction)", "Friction-Adjusted Edge", "Friction Drag"]
    friction_table = tabulate(friction_summary_data, headers=friction_headers, tablefmt="github")

    report_content = f"""# Causal Feature Survival & Validation Report

Generated on: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Total ticker-days analyzed: {len(df_full)}

## Executive Summary
This report analyzes whether any of the engineered causal features show a stable and statistically significant predictive edge for the 15-minute future return (`future_15m_return`), surviving realistic retail friction (fees, slippage, and latency).

{audit_report}

---

## Survival Status of Clean Features

### Passed Features
The following features met all survival criteria (monotonicity $\\ge 0.5$, epoch stability $\\ge 66\\%$, and bootstrap $p$-value $< 0.05$):

{passed_table}

### Failed Features
The following features did not meet one or more survival criteria:

{failed_table}

---

## Impact of Execution Friction on Edge Survival
This table compares the predictive edge (Top Bin Expectancy - Bottom Bin Expectancy) under raw vs. friction-adjusted settings (delayed execution and fee/slippage subtraction):

{friction_table}

---

## Diagnostics & Methodology
1. **Checkpoint T**: Features are computed strictly at **09:35:00 Eastern**, 5 minutes after the open.
2. **Outcome**: The primary target variable is `future_15m_return` (return from 09:35:00 to 09:50:00).
3. **Friction Parameters**:
   - Flat entry/exit fee: 1.5 basis points.
   - Slippage: Volatility-scaled entry slippage.
   - Latency: 1-minute execution delay (entry evaluated at 09:36:00 close).
"""
    with open(report_path, "w") as f:
        f.write(report_content)
        
    print(f"[+] Generated final report at '{report_path}'.")
    
    # 9. Run Cross-Sectional Ranking Engine
    print("[*] Running Cross-Sectional relative return edge discovery...")
    from src.research.cross_sectional_ranker import CrossSectionalRanker
    try:
        ranker = CrossSectionalRanker(input_path=dataset_path)
        ranker.run_analysis()
        print("[+] Cross-sectional relative return edge discovery completed.")
    except Exception as e:
        print(f"[!] Error running Cross-Sectional Ranking Engine: {e}")
        
    print("=" * 60)
    print("        CAUSAL FEATURE RESEARCH PIPELINE COMPLETED")
    print("=" * 60)

if __name__ == "__main__":
    run_pipeline()
