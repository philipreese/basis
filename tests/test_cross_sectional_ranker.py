import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from src.research.cross_sectional_ranker import CrossSectionalRanker

class TestCrossSectionalRanker(unittest.TestCase):
    def setUp(self):
        # Generate dummy cross-sectional dataset
        # 10 dates, 5 tickers per date = 50 rows
        np.random.seed(42)
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]
        dates = pd.date_range("2022-01-01", periods=10)
        
        records = []
        for d in dates:
            for t in tickers:
                # feature_a has some structure, feature_b is random
                feat_a = np.random.normal(0, 1.0)
                feat_b = np.random.normal(0, 1.0)
                # target will be correlated with feature_a's ranking on that day
                records.append({
                    "date": d,
                    "ticker": t,
                    "feature_a": feat_a,
                    "feature_b": feat_b,
                    "future_15m_return": 1.5 * feat_a + np.random.normal(0, 0.2),
                    "future_15m_return_friction": 1.5 * feat_a + np.random.normal(0, 0.2) - 0.05,
                    "future_15m_return_worst": 1.5 * feat_a + np.random.normal(0, 0.2) - 0.10
                })
                
        self.df = pd.DataFrame(records)
        self.ranker = CrossSectionalRanker()
        self.ranker.df = self.df
        self.ranker.features = ["feature_a", "feature_b"]
        self.ranker.horizons = [15]

    def test_compute_daily_ranks(self):
        self.ranker.compute_daily_ranks()
        
        # Verify columns exist
        self.assertIn("feature_a_rank_pct", self.ranker.df.columns)
        self.assertIn("feature_b_rank_pct", self.ranker.df.columns)
        
        # Verify ranks are in [0.0, 1.0] range
        self.assertTrue((self.ranker.df["feature_a_rank_pct"] >= 0.0).all())
        self.assertTrue((self.ranker.df["feature_a_rank_pct"] <= 1.0).all())
        
        # For a single day, check if min rank is 0.0 and max rank is 1.0
        first_day = self.ranker.df[self.ranker.df["date"] == "2022-01-01"]
        self.assertEqual(first_day["feature_a_rank_pct"].min(), 0.0)
        self.assertEqual(first_day["feature_a_rank_pct"].max(), 1.0)
        # Check that ranks are distinct (since we have 5 tickers and method="first")
        self.assertEqual(len(first_day["feature_a_rank_pct"].unique()), 5)

    def test_assign_buckets(self):
        self.ranker.compute_daily_ranks()
        self.ranker.assign_buckets()
        
        # Verify columns exist
        self.assertIn("feature_a_quintile", self.ranker.df.columns)
        self.assertIn("feature_a_decile", self.ranker.df.columns)
        
        # Verify ranges
        self.assertTrue(self.ranker.df["feature_a_quintile"].isin([1, 2, 3, 4, 5]).all())
        self.assertTrue(self.ranker.df["feature_a_decile"].isin(list(range(1, 11))).all())
        
        # Verify bucket distributions
        # With N=5 per day, each day has ranks: 0.0, 0.25, 0.5, 0.75, 1.0
        # Under quintile binning [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], they fall into:
        # 0.0 -> Q1 (1)
        # 0.25 -> Q2 (2)
        # 0.5 -> Q3 (3)
        # 0.75 -> Q4 (4)
        # 1.0 -> Q5 (5)
        # Each bucket should have exactly 10 samples across 10 days
        counts = self.ranker.df["feature_a_quintile"].value_counts()
        for q in [1, 2, 3, 4, 5]:
            self.assertEqual(counts[q], 10)

    def test_compute_monotonicity(self):
        # Monotonically increasing
        arr_inc = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(self.ranker.compute_monotonicity(arr_inc), 1.0)
        
        # Monotonically decreasing
        arr_dec = [5.0, 4.0, 3.0, 2.0, 1.0]
        self.assertAlmostEqual(self.ranker.compute_monotonicity(arr_dec), -1.0)
        
        # Non-monotonic flat or mixed
        arr_flat = [1.0, 1.0, 1.0, 1.0, 1.0]
        self.assertEqual(self.ranker.compute_monotonicity(arr_flat), 0.0)

    def test_evaluate_stability(self):
        # Assign ranks and buckets
        self.ranker.compute_daily_ranks()
        self.ranker.assign_buckets()
        
        # Create temp_df aligned like in run_analysis
        temp_df = pd.DataFrame({
            "date": self.ranker.df["date"],
            "bucket": self.ranker.df["feature_a_quintile"],
            "outcome": self.ranker.df["future_15m_return"]
        })
        
        overall_spread = 1.0
        stability_score, epoch_edges = self.ranker.evaluate_stability(
            temp_df, 5, 1, overall_spread
        )
        
        self.assertAlmostEqual(stability_score, 1 / 6)
        self.assertIn("2022_Bear", epoch_edges)
        self.assertFalse(np.isnan(epoch_edges["2022_Bear"]))
        # For other epochs like 2020_Crash, they should be NaN
        self.assertTrue(np.isnan(epoch_edges["2020_Crash"]))

    def test_run_bootstrap_spread(self):
        self.ranker.compute_daily_ranks()
        self.ranker.assign_buckets()
        
        temp_df = pd.DataFrame({
            "date": self.ranker.df["date"],
            "bucket": self.ranker.df["feature_a_quintile"],
            "outcome": self.ranker.df["future_15m_return"]
        })
        grouped = temp_df.groupby(["date", "bucket"])["outcome"].agg(["sum", "count"])
        df_by_date_agg = grouped.unstack(level="bucket").fillna(0.0)
        
        p_val = self.ranker.run_bootstrap_spread(
            df_by_date_agg, 5, 1, n_iterations=20
        )
        
        # Since feature_a is highly correlated with outcomes, p-value of the spread should be 0.0 (or very low)
        self.assertLess(p_val, 0.2)

if __name__ == "__main__":
    unittest.main()
