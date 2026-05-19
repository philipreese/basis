import unittest
import pandas as pd
import numpy as np
import os
import shutil
import json
from src.research.edge_falsifier import CrossSectionalEdgeFalsifier

class TestEdgeFalsifier(unittest.TestCase):
    def setUp(self):
        # Create a mock environment
        self.test_dir = "out_test_falsifier"
        os.makedirs(self.test_dir, exist_ok=True)
        self.dataset_path = os.path.join(self.test_dir, "test_dataset.csv")
        self.bars_cache_dir = os.path.join(self.test_dir, "bars_cache")
        os.makedirs(self.bars_cache_dir, exist_ok=True)

        # Create mock causal feature dataset
        # 4 dates, 3 tickers per day
        tickers = ["AAPL", "MSFT", "GOOGL"]
        dates = ["2022-01-03", "2022-01-04", "2022-01-05", "2022-01-06"]
        
        records = []
        for d in dates:
            for idx, ticker in enumerate(tickers):
                # Ensure gap_pct and overnight_spy are highly correlated
                gap_val = float(idx + 1)
                spy_rel = gap_val - 0.5
                vwap_dist = float(3 - idx)
                
                records.append({
                    "ticker": ticker,
                    "date": d,
                    "gap_pct": gap_val,
                    "overnight_spy_relative_strength": spy_rel,
                    "vwap_distance": vwap_dist,
                    "atr_14": 1.5,
                    "dollar_volume": 10000.0,
                    "liquidity_proxy": 20.0,
                    "market_regime": "TRENDING_BULL",
                    "epoch": "2022_Bear",
                    "future_60m_return": 1.0 if idx == 2 else (-1.0 if idx == 0 else 0.0),
                    "future_60m_return_friction": 0.8 if idx == 2 else (-1.2 if idx == 0 else 0.0),
                    "future_60m_return_worst": 0.5 if idx == 2 else (-1.5 if idx == 0 else 0.0),
                })
                
                # Mock minute bars for path dependency (70 minutes of data)
                bars = []
                for m in range(80):
                    bars.append({
                        "o": 100.0,
                        "h": 100.2,
                        "l": 99.8,
                        "c": 100.0 + (m * 0.01 if idx == 2 else (-m * 0.01 if idx == 0 else 0.0))
                    })
                with open(os.path.join(self.bars_cache_dir, f"{ticker}_{d}.json"), "w") as f:
                    json.dump(bars, f)

        self.mock_df = pd.DataFrame(records)
        self.mock_df.to_csv(self.dataset_path, index=False)

        self.falsifier = CrossSectionalEdgeFalsifier(
            dataset_path=self.dataset_path,
            bars_cache_dir=self.bars_cache_dir,
            out_dir=self.test_dir
        )

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_load_and_prepare(self):
        self.falsifier.load_data()
        self.assertIsNotNone(self.falsifier.df)
        self.assertEqual(len(self.falsifier.df), 12)
        self.assertIn("date", self.falsifier.df.columns)
        self.assertIn("atr_pct_clamped", self.falsifier.df.columns)

    def test_layer1_universe_scaling(self):
        self.falsifier.load_data()
        # Scale to N=6 (original is 3 per day)
        results = self.falsifier.run_universe_scaling(target_n_sizes=[6])
        self.assertIn(6, results)
        row = results[6]
        self.assertIn("cagr", row)
        self.assertIn("sharpe", row)
        self.assertIn("max_dd", row)
        self.assertIn("spread", row)

    def test_layer2_rank_stability_collapse(self):
        self.falsifier.load_data()
        z_score, p_val, null_sharpes = self.falsifier.run_permutation_test(n_permutations=10)
        self.assertEqual(len(null_sharpes), 10)
        self.assertTrue(isinstance(z_score, float))
        self.assertTrue(isinstance(p_val, float))

    def test_layer3_pca_redundancy(self):
        self.falsifier.load_data()
        corr_matrix, explained_variance, loadings = self.falsifier.run_pca_redundancy()
        self.assertEqual(corr_matrix.shape, (3, 3))
        self.assertEqual(len(explained_variance), 3)
        self.assertAlmostEqual(np.sum(explained_variance), 1.0)
        self.assertEqual(loadings.shape, (3, 3))

    def test_layer4_bucket_diagnostics(self):
        self.falsifier.load_data()
        diagnostics = self.falsifier.run_bucket_diagnostics()
        self.assertIn("gap_pct", diagnostics)
        df_diag = diagnostics["gap_pct"]
        self.assertEqual(len(df_diag), 5) # 5 quintile buckets
        self.assertIn("avg_occupancy", df_diag.columns)
        self.assertIn("pct_empty", df_diag.columns)
        self.assertIn("pct_single", df_diag.columns)

    def test_layer5_path_dependency(self):
        self.falsifier.load_data()
        stats, path_results = self.falsifier.run_path_dependency_test(n_paths=5)
        self.assertEqual(len(path_results), 5)
        self.assertIn("mean_sharpe", stats)
        self.assertIn("std_sharpe", stats)
        self.assertIn("worst_case_sharpe_p5", stats)

    def test_generate_reports(self):
        # Verify that generating report writes all 6 files
        self.falsifier.run_falsification_suite(
            target_n_sizes=[6],
            n_permutations=5,
            n_paths=3
        )
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "b3_falsification_report.md")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "universe_scaling_results.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "rank_permutation_null_distribution.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "feature_redundancy_analysis.md")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "bucket_collapse_diagnostics.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.test_dir, "execution_noise_sensitivity_report.md")))

if __name__ == "__main__":
    unittest.main()
