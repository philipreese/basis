import pandas as pd
import numpy as np

class LeakageAuditor:
    """
    Auditor to detect look-ahead leakage, future timestamp contamination,
    suspicious win-rate discontinuities, and target leakage in the feature matrix.
    """
    def __init__(self, target_correlation_threshold=0.85, expectancy_threshold_pct=5.0):
        self.target_correlation_threshold = target_correlation_threshold
        self.expectancy_threshold_pct = expectancy_threshold_pct
        self.flagged_features = {}

    def audit_features(self, df: pd.DataFrame, feature_names: list, target_name: str) -> dict:
        """
        Runs the full leakage audit on a list of features against a target outcome.
        Returns a dict of flagged features with the reasons.
        """
        self.flagged_features = {}
        
        # Verify that the target column itself exists
        if target_name not in df.columns:
            raise ValueError(f"Target column '{target_name}' not found in DataFrame.")

        # Check for invalid feature names
        forbidden_keywords = ["pnl", "r_multiple", "mfe", "mae", "outcome", "future", "post"]
        for feat in feature_names:
            if feat not in df.columns:
                continue
                
            reasons = []
            
            # Check 1: Forbidden keyword in name (suggests post-entry data)
            if any(kw in feat.lower() for kw in forbidden_keywords):
                reasons.append(f"Forbidden keyword in feature name: '{feat}'")
                
            # Check 2: Absolute correlation with target
            feat_series = df[feat].dropna()
            target_series = df.loc[feat_series.index, target_name]
            
            # Combine and dropna
            combined = pd.concat([feat_series, target_series], axis=1).dropna()
            if len(combined) >= 30:
                corr = combined[feat].corr(combined[target_name])
                if abs(corr) >= self.target_correlation_threshold:
                    reasons.append(f"Suspiciously high target correlation: r = {corr:.4f}")
            
            # Check 3: Suspicious win-rate/expectancy discontinuities in bins
            # Bin the feature into 5 bins
            if len(combined) >= 30:
                try:
                    combined["bin"] = pd.qcut(combined[feat].rank(method="first"), 5, labels=False) + 1
                    # Compute mean and win rate (pct positive) per bin
                    grouped = combined.groupby("bin")
                    
                    for bin_id, group in grouped:
                        n_samples = len(group)
                        if n_samples >= 15:
                            mean_val = group[target_name].mean()
                            # Win rate: % of samples with return > 0
                            positive_count = (group[target_name] > 0).sum()
                            win_rate = positive_count / n_samples
                            
                            # Discontinuity check
                            if win_rate == 1.0 or win_rate == 0.0:
                                reasons.append(f"Bin {bin_id} win rate discontinuity: {win_rate*100:.1f}% win rate with {n_samples} samples.")
                                
                            # Suspiciously high average return
                            if abs(mean_val) >= self.expectancy_threshold_pct:
                                reasons.append(f"Bin {bin_id} has unrealistic expectancy: {mean_val:.2f}% (threshold: {self.expectancy_threshold_pct}%)")
                except Exception as e:
                    reasons.append(f"Error during binning audit: {e}")

            if reasons:
                self.flagged_features[feat] = reasons

        return self.flagged_features
        
    def generate_audit_report(self) -> str:
        """
        Generates a markdown text summary of the audit results.
        """
        if not self.flagged_features:
            return "### Leakage Auditor Report\n\n[+] **No leakage detected.** All features passed the audit."

        lines = [
            "### Leakage Auditor Report",
            "",
            "> [!WARNING]",
            f"The following {len(self.flagged_features)} features were FLAGGED for potential data leakage or target contamination and will be excluded from the feature rankings:",
            ""
        ]
        
        for feat, reasons in self.flagged_features.items():
            lines.append(f"- **{feat}**:")
            for r in reasons:
                lines.append(f"  * {r}")
                
        return "\n".join(lines)
