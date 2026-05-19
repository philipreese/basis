import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from src.research.causal_feature_lab import CausalFeatureLab
from src.research.feature_survival_engine import FeatureSurvivalEngine
from src.research.leakage_auditor import LeakageAuditor

class TestCausalFeatureLab(unittest.TestCase):
    def setUp(self):
        # Generate dummy 1-minute bars (e.g. 100 bars from 09:30 onwards)
        self.dummy_bars = []
        base_time = datetime(2025, 1, 15, 9, 30)
        np.random.seed(42)
        price = 100.0
        for i in range(100):
            t = (base_time + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            o = price
            c = price + np.random.normal(0, 0.5)
            h = max(o, c) + np.random.uniform(0, 0.2)
            l = min(o, c) - np.random.uniform(0, 0.2)
            v = float(np.random.randint(1000, 5000))
            vw = (o + h + l + c) / 4.0
            self.dummy_bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw})
            price = c

        # Generate dummy daily bars for the stock
        dates = pd.date_range("2024-01-01", "2025-01-15")
        daily_records = []
        price = 90.0
        for dt in dates:
            daily_records.append({
                "date": dt,
                "open": price,
                "high": price + 2.0,
                "low": price - 2.0,
                "close": price + np.random.normal(0, 1.0),
                "volume": 1000000.0
            })
            price = daily_records[-1]["close"]
        self.daily_df = pd.DataFrame(daily_records).set_index("date")

        # Generate dummy daily SPY regimes
        spy_records = []
        for dt in dates:
            spy_records.append({
                "date": dt,
                "open": 400.0,
                "high": 402.0,
                "low": 398.0,
                "close": 401.0,
                "volume": 50000000.0,
                "regime": "TRENDING_BULL",
                "is_transition": False
            })
        self.spy_daily_df = pd.DataFrame(spy_records).set_index("date")

        self.lab = CausalFeatureLab()

    def test_strict_causality(self):
        # 1. Verify that compute_features does not touch bars after index 4 (post-09:35:00)
        # We pass modified minute bars where all bars post-09:35 are modified to NaN, and verify features still compute.
        modified_bars = list(self.dummy_bars)
        for i in range(5, len(modified_bars)):
            modified_bars[i] = {k: np.nan for k in modified_bars[i]}
            
        quant_item = {"gap_pct": 1.5, "relative_volume": 2.0}
        features = self.lab.compute_features("AAPL", "2025-01-15", modified_bars, self.daily_df, self.spy_daily_df, quant_item)
        
        self.assertIsNotNone(features)
        self.assertEqual(features["ticker"], "AAPL")
        self.assertEqual(features["date"], "2025-01-15")
        self.assertEqual(features["gap_pct"], 1.5)
        self.assertEqual(features["premarket_relative_volume"], 2.0)
        self.assertFalse(np.isnan(features["first_1m_return"]))
        self.assertFalse(np.isnan(features["first_5m_range_expansion"]))

    def test_outcome_generation_with_friction(self):
        quant_item = {"gap_pct": 1.5, "relative_volume": 2.0}
        features = self.lab.compute_features("AAPL", "2025-01-15", self.dummy_bars, self.daily_df, self.spy_daily_df, quant_item)
        
        # Test raw outcomes (no friction)
        outcomes_raw = self.lab.compute_outcomes(self.dummy_bars, features, apply_friction=False)
        self.assertIn("future_15m_return", outcomes_raw)
        self.assertIn("hit_1r_before_minus_1r", outcomes_raw)
        
        # Test friction outcomes (fees and slippage subtracted, 1m latency)
        outcomes_friction = self.lab.compute_outcomes(self.dummy_bars, features, apply_friction=True, delayed_entry_offset=1)
        self.assertIn("future_15m_return", outcomes_friction)
        
        # Friction returns should be lower than raw returns due to execution drag and delay
        # Let's check that outcomes_friction is computed properly and is different
        self.assertNotEqual(outcomes_raw["future_15m_return"], outcomes_friction["future_15m_return"])


class TestFeatureSurvivalEngine(unittest.TestCase):
    def setUp(self):
        # Generate dummy dataset of features and outcomes
        np.random.seed(42)
        n = 100
        # feature_a has positive monotonic relationship with future_15m_return
        feature_a = np.linspace(-2.0, 2.0, n) + np.random.normal(0, 0.2, n)
        # feature_b has no relationship
        feature_b = np.random.normal(0, 1.0, n)
        
        # Outcome has strong correlation with feature_a
        future_return = 2.0 * feature_a + np.random.normal(0, 0.5, n)
        
        dates = [datetime(2022, 1, 1) + timedelta(days=i) for i in range(n)]
        dates_str = [d.strftime("%Y-%m-%d") for d in dates]
        
        self.df = pd.DataFrame({
            "date": dates_str,
            "feature_a": feature_a,
            "feature_b": feature_b,
            "future_15m_return": future_return
        })
        
        self.engine = FeatureSurvivalEngine(n_bins=5)

    def test_binning_and_monotonicity(self):
        res_a = self.engine.analyze_feature(self.df, "feature_a", "future_15m_return")
        res_b = self.engine.analyze_feature(self.df, "feature_b", "future_15m_return")
        
        # feature_a should pass or have high monotonicity
        self.assertGreater(res_a["monotonicity"], 0.8)
        self.assertGreater(res_a["overall_edge"], 0.0)
        
        # feature_b should have low monotonicity
        self.assertLess(abs(res_b["monotonicity"]), 0.5)


class TestLeakageAuditor(unittest.TestCase):
    def setUp(self):
        # Create dataset with normal features and leaked features
        n = 100
        np.random.seed(42)
        feature_ok = np.random.normal(0, 1.0, n)
        # Direct target leak: exactly equal or very high correlation
        target = np.random.normal(0, 2.0, n)
        feature_leak = target + np.random.normal(0, 0.01, n)
        # Forbidden word feature
        feature_pnl = np.random.normal(0, 1.0, n)
        
        dates = [datetime(2022, 1, 1) + timedelta(days=i) for i in range(n)]
        dates_str = [d.strftime("%Y-%m-%d") for d in dates]

        self.df = pd.DataFrame({
            "date": dates_str,
            "feature_ok": feature_ok,
            "feature_leak": feature_leak,
            "feature_pnl": feature_pnl,
            "future_15m_return": target
        })
        self.auditor = LeakageAuditor()

    def test_leakage_detection(self):
        feature_cols = ["feature_ok", "feature_leak", "feature_pnl"]
        flagged = self.auditor.audit_features(self.df, feature_cols, "future_15m_return")
        
        # feature_leak should be flagged for correlation
        self.assertIn("feature_leak", flagged)
        self.assertTrue(any("high target correlation" in r for r in flagged["feature_leak"]))
        
        # feature_pnl should be flagged for name
        self.assertIn("feature_pnl", flagged)
        self.assertTrue(any("Forbidden keyword" in r for r in flagged["feature_pnl"]))
        
        # feature_ok should NOT be flagged
        self.assertNotIn("feature_ok", flagged)

if __name__ == "__main__":
    unittest.main()
