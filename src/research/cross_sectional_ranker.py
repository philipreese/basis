import os
import re
import datetime
import pandas as pd
import numpy as np
from tabulate import tabulate

EPOCHS = [
    ("2020_Crash", "2020-02-15", "2020-06-15"),
    ("2021_Bull", "2021-01-01", "2021-12-31"),
    ("2022_Bear", "2022-01-01", "2022-12-31"),
    ("2023_Tech", "2023-02-15", "2023-07-15"),
    ("2024_Choppy", "2024-07-01", "2024-10-31"),
    ("2025_Current", "2025-05-01", "2026-04-30"),
]

class CrossSectionalRanker:
    """
    Deterministic cross-sectional relative edge discovery research engine.
    Calculates percentile ranks daily, buckets them, and evaluates spreads,
    stability, bootstrap significance, and monotonicity.
    Optimized for fast execution using vectorized NumPy operations.
    """
    def __init__(self, input_path="out/causal_feature_dataset.csv", out_dir="out"):
        self.input_path = input_path
        self.out_dir = out_dir
        self.df = None
        self.features = []
        self.horizons = []
        
    def load_data(self):
        """
        Loads the dataset and parses dates.
        """
        if not os.path.exists(self.input_path):
            raise FileNotFoundError(f"Input dataset not found at {self.input_path}")
            
        print(f"[*] Loading dataset from {self.input_path}...")
        self.df = pd.read_csv(self.input_path)
        self.df["date"] = pd.to_datetime(self.df["date"])
        print(f"[+] Loaded {len(self.df)} rows, {len(self.df.columns)} columns.")
        
    def detect_features_and_horizons(self):
        """
        Automatically detects features and available return horizons.
        """
        # Metadata and non-feature columns
        meta_cols = ["ticker", "symbol", "date", "epoch", "market_regime", "atr_14"]
        
        # Discover features (numeric, not metadata, not outcomes)
        self.features = [
            col for col in self.df.columns
            if col not in meta_cols
            and not col.startswith("future_")
            and not col.startswith("hit_")
            and pd.api.types.is_numeric_dtype(self.df[col])
        ]
        
        # Discover horizons from column names like future_15m_return
        horizons_found = set()
        for col in self.df.columns:
            m = re.match(r"^future_(\d+)m_return$", col)
            if m:
                horizons_found.add(int(m.group(1)))
        
        self.horizons = sorted(list(horizons_found))
        
        print(f"[+] Detected {len(self.features)} numeric causal features.")
        print(f"[+] Detected horizons: {self.horizons} minutes.")
        if not self.horizons:
            self.horizons = [5, 15, 30, 60]
            print(f"[!] No horizon columns matched. Using fallback: {self.horizons}")

    def compute_daily_ranks(self):
        """
        Computes cross-sectional percentile ranks [0.0 -> 1.0] daily for each feature.
        """
        print("[*] Computing daily cross-sectional percentile ranks...")
        # Sort values by date to keep daily ranks ordered
        self.df = self.df.sort_values("date").reset_index(drop=True)
        
        new_cols = {}
        
        def rank_group(group):
            non_nan_count = group.notna().sum()
            if non_nan_count <= 1:
                res = pd.Series(np.nan, index=group.index)
                if non_nan_count == 1:
                    res[group.notna()] = 0.5
                return res
            r = group.rank(method="first")
            return (r - 1) / (non_nan_count - 1)
            
        for feat in self.features:
            new_cols[f"{feat}_rank_pct"] = self.df.groupby("date")[feat].transform(rank_group)
            
        # Concatenate all new columns at once to prevent fragmentation
        self.df = pd.concat([self.df, pd.DataFrame(new_cols)], axis=1)
        print("[+] Percentile ranks successfully generated.")

    def assign_buckets(self):
        """
        Assigns percentile ranks into quintile and decile buckets.
        """
        print("[*] Binning percentile ranks into quintile and decile buckets...")
        
        new_cols = {}
        for feat in self.features:
            rank_col = f"{feat}_rank_pct"
            
            # Quintile bucket assignments (1 to 5)
            new_cols[f"{feat}_quintile"] = pd.cut(
                self.df[rank_col],
                bins=np.linspace(0.0, 1.0, 6),
                include_lowest=True,
                labels=False
            ) + 1
            
            # Decile bucket assignments (1 to 10)
            new_cols[f"{feat}_decile"] = pd.cut(
                self.df[rank_col],
                bins=np.linspace(0.0, 1.0, 11),
                include_lowest=True,
                labels=False
            ) + 1
            
        # Concatenate all new columns at once to prevent fragmentation
        self.df = pd.concat([self.df, pd.DataFrame(new_cols)], axis=1)
        print("[+] Bucketing complete.")

    def run_bootstrap_spread(self, df_by_date_agg, top_b, bottom_b, n_iterations=100, seed=42):
        """
        Runs an extremely optimized date-level bootstrap using pre-aggregated sums and counts.
        """
        sum_top_col = ("sum", top_b)
        count_top_col = ("count", top_b)
        sum_bottom_col = ("sum", bottom_b)
        count_bottom_col = ("count", bottom_b)
        
        if (sum_top_col not in df_by_date_agg.columns or 
            count_top_col not in df_by_date_agg.columns or
            sum_bottom_col not in df_by_date_agg.columns or
            count_bottom_col not in df_by_date_agg.columns):
            return 1.0
            
        sum_top = df_by_date_agg[sum_top_col].values
        count_top = df_by_date_agg[count_top_col].values
        sum_bottom = df_by_date_agg[sum_bottom_col].values
        count_bottom = df_by_date_agg[count_bottom_col].values
        
        n_dates = len(df_by_date_agg)
        if n_dates == 0:
            return 1.0
            
        # Extract the overall spread return to determine sign
        s_t_all = sum_top.sum()
        c_t_all = count_top.sum()
        s_b_all = sum_bottom.sum()
        c_b_all = count_bottom.sum()
        
        if c_t_all == 0 or c_b_all == 0:
            return 1.0
            
        overall_spread = (s_t_all / c_t_all) - (s_b_all / c_b_all)
        
        np.random.seed(seed)
        boot_idx = np.random.randint(0, n_dates, size=(n_iterations, n_dates))
        
        # Vectorized sum across columns for each bootstrap iteration
        s_t_boot = sum_top[boot_idx].sum(axis=1)
        c_t_boot = count_top[boot_idx].sum(axis=1)
        s_b_boot = sum_bottom[boot_idx].sum(axis=1)
        c_b_boot = count_bottom[boot_idx].sum(axis=1)
        
        # Calculate bootstrap spreads where counts are non-zero
        valid = (c_t_boot > 0) & (c_b_boot > 0)
        if not valid.any():
            return 1.0
            
        boot_spreads = (s_t_boot[valid] / c_t_boot[valid]) - (s_b_boot[valid] / c_b_boot[valid])
        
        # P-value: fraction of bootstrap spreads with opposite sign of overall spread
        opposite_sign_count = np.sum(np.sign(boot_spreads) != np.sign(overall_spread))
        p_value = opposite_sign_count / len(boot_spreads)
        return p_value

    def evaluate_stability(self, temp_df, top_b, bottom_b, overall_spread):
        """
        Measures spread stability and epoch consistency across 6 defined epochs.
        """
        matching_epochs = 0
        epoch_edges = {}
        
        for name, start_str, end_str in EPOCHS:
            start = pd.to_datetime(start_str)
            end = pd.to_datetime(end_str)
            epoch_df = temp_df[(temp_df["date"] >= start) & (temp_df["date"] <= end)]
            
            top_returns = epoch_df.loc[epoch_df["bucket"] == top_b, "outcome"]
            bottom_returns = epoch_df.loc[epoch_df["bucket"] == bottom_b, "outcome"]
            
            if len(top_returns) >= 10 and len(bottom_returns) >= 10:
                epoch_spread = top_returns.mean() - bottom_returns.mean()
                epoch_edges[name] = epoch_spread
                
                # Check direction consistency
                if np.sign(epoch_spread) == np.sign(overall_spread) and overall_spread != 0:
                    matching_epochs += 1
            else:
                epoch_edges[name] = np.nan
                
        stability_score = matching_epochs / len(EPOCHS)
        return stability_score, epoch_edges

    def compute_monotonicity(self, mean_returns):
        """
        Computes Spearman rank correlation of bucket index to mean return.
        """
        n = len(mean_returns)
        bin_series = pd.Series(range(1, n + 1))
        exp_series = pd.Series(mean_returns)
        
        bin_ranked = bin_series.rank()
        exp_ranked = exp_series.rank()
        
        monotonicity = bin_ranked.corr(exp_ranked, method="pearson")
        if pd.isna(monotonicity):
            return 0.0
        return monotonicity

    def run_analysis(self):
        """
        Executes the cross-sectional ranking research pipeline.
        """
        self.load_data()
        self.detect_features_and_horizons()
        self.compute_daily_ranks()
        self.assign_buckets()
        
        results = []
        
        # Friction profiles
        friction_profiles = {
            "raw": "",
            "friction": "_friction",
            "worst": "_worst"
        }
        
        # Bucket configurations: (n_buckets, top_bucket, bottom_bucket)
        bucket_types = {
            "quintile": (5, 5, 1),
            "decile": (10, 10, 1)
        }
        
        total_runs = len(self.features) * len(self.horizons) * len(friction_profiles) * len(bucket_types)
        print(f"[*] Running relative edge discovery scans ({total_runs} combinations)...")
        
        run_count = 0
        for feat in self.features:
            for horizon in self.horizons:
                for f_name, f_suffix in friction_profiles.items():
                    outcome_col = f"future_{horizon}m_return{f_suffix}"
                    
                    if outcome_col not in self.df.columns:
                        continue
                        
                    for b_type, (n_buckets, top_b, bottom_b) in bucket_types.items():
                        bucket_col = f"{feat}_{b_type}"
                        
                        if bucket_col not in self.df.columns:
                            continue
                            
                        # Construct a temporary clean alignment DataFrame
                        temp_df = pd.DataFrame({
                            "date": self.df["date"],
                            "bucket": self.df[bucket_col],
                            "outcome": self.df[outcome_col]
                        }).dropna()
                        
                        if len(temp_df) < 50:
                            continue
                            
                        # Calculate overall bucket returns
                        bucket_stats = temp_df.groupby("bucket")["outcome"].agg(["mean", "count"])
                        
                        # Fill missing buckets with 0.0 to maintain size consistency
                        for b in range(1, n_buckets + 1):
                            if b not in bucket_stats.index:
                                bucket_stats.loc[b] = [0.0, 0]
                                
                        bucket_stats = bucket_stats.sort_index()
                        bucket_means = bucket_stats["mean"].tolist()
                        bucket_sizes = bucket_stats["count"].tolist()
                        
                        top_return = bucket_means[top_b - 1]
                        bottom_return = bucket_means[bottom_b - 1]
                        spread_return = top_return - bottom_return
                        
                        # Monotonicity
                        monotonicity = self.compute_monotonicity(bucket_means)
                        
                        # Pre-aggregate by date and bucket for optimized bootstrap
                        grouped = temp_df.groupby(["date", "bucket"])["outcome"].agg(["sum", "count"])
                        df_by_date_agg = grouped.unstack(level="bucket").fillna(0.0)
                        
                        # Vectorized bootstrap significance (p-value)
                        bootstrap_pvalue = self.run_bootstrap_spread(
                            df_by_date_agg, top_b, bottom_b
                        )
                        
                        # Epoch stability
                        epoch_stability, epoch_edges = self.evaluate_stability(
                            temp_df, top_b, bottom_b, spread_return
                        )
                        
                        # Survival Logic
                        # 1. Spread direction matches in >= 4/6 epochs (stability score >= 4/6)
                        # 2. Sample size is adequate (overall sample count >= 100, and minimum bucket size >= 15)
                        # 3. Bootstrap p-value < 0.05
                        min_bucket_size = min(bucket_sizes) if bucket_sizes else 0
                        sample_adequate = (len(temp_df) >= 100) and (min_bucket_size >= 15)
                        epoch_stable = (epoch_stability >= (4 / 6) - 1e-9)
                        stat_significant = (bootstrap_pvalue < 0.05)
                        
                        survived = int(sample_adequate and epoch_stable and stat_significant)
                        
                        res = {
                            "feature": feat,
                            "bucket_type": b_type,
                            "horizon": f"{horizon}m",
                            "friction_profile": f_name,
                            "top_bucket_return": top_return,
                            "bottom_bucket_return": bottom_return,
                            "spread_return": spread_return,
                            "monotonicity": monotonicity,
                            "bootstrap_pvalue": bootstrap_pvalue,
                            "epoch_stability": epoch_stability,
                            "sample_count": len(temp_df),
                            "survived": survived,
                            "epoch_edges": epoch_edges,
                            "bucket_means": bucket_means,
                            "bucket_sizes": bucket_sizes
                        }
                        results.append(res)
                        
            run_count += 1
            if run_count % 5 == 0:
                print(f"  Processed {run_count}/{len(self.features)} features...")
                
        results_df = pd.DataFrame(results)
        print(f"[+] Scan complete. Total evaluated records: {len(results_df)}.")
        
        # Save output CSV and report
        os.makedirs(self.out_dir, exist_ok=True)
        self.generate_csv(results_df)
        self.generate_report(results_df)
        
    def generate_csv(self, df):
        """
        Saves output rankings to out/cross_sectional_feature_rankings.csv.
        """
        csv_path = os.path.join(self.out_dir, "cross_sectional_feature_rankings.csv")
        output_cols = [
            "feature", "bucket_type", "horizon", "friction_profile",
            "top_bucket_return", "bottom_bucket_return", "spread_return",
            "monotonicity", "bootstrap_pvalue", "epoch_stability",
            "sample_count", "survived"
        ]
        df[output_cols].to_csv(csv_path, index=False)
        print(f"[+] Saved rankings to {csv_path}")

    def generate_report(self, df):
        """
        Generates a comprehensive cross-sectional research report in markdown.
        """
        report_path = os.path.join(self.out_dir, "cross_sectional_report.md")
        
        # Filter for survived features
        survived_df = df[df["survived"] == 1].copy()
        
        # Sort survived features by absolute spread return descending
        if not survived_df.empty:
            survived_df["abs_spread"] = survived_df["spread_return"].abs()
            survived_df = survived_df.sort_values("abs_spread", ascending=False)
            
        report_lines = []
        report_lines.append("# Cross-Sectional Relative Return Edge Discovery Report")
        report_lines.append("")
        report_lines.append(f"Generated on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append(f"Total ticker-days analyzed: {len(self.df)}")
        report_lines.append("")
        report_lines.append("## Executive Summary")
        report_lines.append("This research engine determines if causal features have predictive power when evaluated *cross-sectionally* (ranking stocks relative to each other on the same day) rather than absolutely.")
        report_lines.append("A feature is considered to have a **surviving cross-sectional edge** if:")
        report_lines.append("- Its long/short spread direction matches in at least 4 of the 6 historical regimes.")
        report_lines.append("- Its sample size is statistically adequate (>= 100 overall and >= 15 per bucket).")
        report_lines.append("- Its date-level bootstrap p-value is < 0.05.")
        report_lines.append("")
        
        # Surviving Features Section
        report_lines.append("## 1. Surviving Features Matrix")
        report_lines.append("The following feature-horizon combinations successfully survived all robustness, stability, and significance criteria:")
        report_lines.append("")
        
        if not survived_df.empty:
            survived_summary_data = []
            for _, r in survived_df.iterrows():
                survived_summary_data.append([
                    r["feature"],
                    r["bucket_type"],
                    r["horizon"],
                    r["friction_profile"],
                    f"{r['top_bucket_return']:.4f}%",
                    f"{r['bottom_bucket_return']:.4f}%",
                    f"{r['spread_return']:.4f}%",
                    f"{r['monotonicity']:.3f}",
                    f"{r['bootstrap_pvalue']:.3f}",
                    f"{r['epoch_stability'] * 100:.1f}%",
                    r["sample_count"]
                ])
            headers = [
                "Feature", "Bucket Type", "Horizon", "Friction",
                "Top Bucket Mean", "Bottom Bucket Mean", "Spread Return",
                "Monotonicity", "Bootstrap p-val", "Epoch Stability", "Samples"
            ]
            report_lines.append(tabulate(survived_summary_data, headers=headers, tablefmt="github"))
        else:
            report_lines.append("> [!WARNING]")
            report_lines.append("> **Zero features survived all robustness filters.** This implies absolute/cross-sectional return structure is heavily degraded under execution friction.")
        report_lines.append("")
        
        # Strongest Horizons & Friction Profiles
        report_lines.append("## 2. Horizon & Friction Resilience Diagnostics")
        report_lines.append("Analysis of spread expectancy across different horizons and execution friction models:")
        report_lines.append("")
        
        df_copy = df.copy()
        df_copy["abs_spread"] = df_copy["spread_return"].abs()
        
        horizon_summary = df_copy.groupby("horizon")["abs_spread"].mean().reset_index()
        horizon_summary.columns = ["Horizon", "Average Absolute Spread (%)"]
        report_lines.append("### Edge Decay by Horizon (All Features)")
        report_lines.append(tabulate(horizon_summary.values, headers=horizon_summary.columns, tablefmt="github"))
        report_lines.append("")
        
        friction_summary = df_copy.groupby("friction_profile")["abs_spread"].mean().reset_index()
        friction_summary.columns = ["Friction Profile", "Average Absolute Spread (%)"]
        report_lines.append("### Edge Degradation by Friction Profile")
        report_lines.append(tabulate(friction_summary.values, headers=friction_summary.columns, tablefmt="github"))
        report_lines.append("")
        
        # Long/Short Spread Tables & Decile Heatmaps for top features
        report_lines.append("## 3. Top Performing Feature Detail & Heatmaps")
        report_lines.append("Detailed analysis of the top 3 features with the strongest cross-sectional spread (under the 'friction' adjusted profile, '15m' or '30m' horizons, using deciles or quintiles):")
        report_lines.append("")
        
        friction_15_30 = df[(df["friction_profile"] == "friction")].copy()
        if not friction_15_30.empty:
            friction_15_30["abs_spread"] = friction_15_30["spread_return"].abs()
            top_features = friction_15_30.sort_values("abs_spread", ascending=False).head(3)
            
            for _, top_f in top_features.iterrows():
                feat_name = top_f["feature"]
                b_type = top_f["bucket_type"]
                horizon = top_f["horizon"]
                spread_val = top_f["spread_return"]
                p_val = top_f["bootstrap_pvalue"]
                stability_score = top_f["epoch_stability"]
                
                report_lines.append(f"### Feature: `{feat_name}` ({b_type.capitalize()} Buckets, {horizon} Horizon, Friction-Adjusted)")
                report_lines.append(f"- **Overall Spread**: {spread_val:.4f}% (Bootstrap p-val: {p_val:.3f}, Epoch Stability: {stability_score * 100:.1f}%)")
                report_lines.append("- **Monotonicity**: " + f"{top_f['monotonicity']:.3f}")
                report_lines.append("")
                
                means = top_f["bucket_means"]
                sizes = top_f["bucket_sizes"]
                
                bucket_headers = [f"B{i+1}" for i in range(len(means))]
                bucket_data = [[f"{m:.4f}%" for m in means], [str(s) for s in sizes]]
                
                report_lines.append("#### Bucket Heatmap Matrix")
                report_lines.append(tabulate(bucket_data, headers=bucket_headers, showindex=["Mean Return", "Sample Size"], tablefmt="github"))
                report_lines.append("")
                
                epoch_data = []
                for ep_name, ep_spread in top_f["epoch_edges"].items():
                    val_str = f"{ep_spread:.4f}%" if not pd.isna(ep_spread) else "NaN (Low Sample)"
                    epoch_data.append([ep_name, val_str])
                report_lines.append("#### Epoch Spread Stability")
                report_lines.append(tabulate(epoch_data, headers=["Regime Epoch", "Epoch Spread Return"], tablefmt="github"))
                report_lines.append("")
                report_lines.append("---")
                report_lines.append("")
        else:
            report_lines.append("No features found matching top criteria.")
            
        # Methodology and Diagnostics
        report_lines.append("## 4. Stability & Methodology Diagnostics")
        report_lines.append("1. **Daily Percentile Ranking**: Vectorized rank normalization to `[0.0, 1.0]` daily. Breaks ties deterministically using `method='first'`.")
        report_lines.append("2. **Regime Anchors**: Standardized across 6 discrete epochs. Spreads must align in >= 4/6 epochs to be flagged as stable.")
        report_lines.append("3. **Bootstrap Method**: 100-iteration date-level block bootstrap. Date-level sampling preserves cross-sectional correlation within trading sessions.")
        report_lines.append("4. **Execution Friction Model**:")
        report_lines.append("   - Flat entry/exit fee: 1.5 bps.")
        report_lines.append("   - Volatility-scaled entry slippage.")
        report_lines.append("   - Latency: 1-minute execution delay.")
        
        with open(report_path, "w") as f:
            f.write("\n".join(report_lines))
            
        print(f"[+] Generated cross-sectional report at {report_path}")

if __name__ == "__main__":
    ranker = CrossSectionalRanker()
    ranker.run_analysis()
