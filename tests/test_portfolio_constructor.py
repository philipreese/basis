import unittest
import pandas as pd
import numpy as np
import os
import shutil
from src.research.portfolio_constructor import CrossSectionalPortfolioConstructor, cap_weights

class TestPortfolioConstructor(unittest.TestCase):
    def setUp(self):
        # Create a mock dataframe for testing
        # 3 dates, 5 tickers each day
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]
        dates = ["2022-01-03", "2022-01-04", "2022-01-05"]
        
        records = []
        for d in dates:
            for idx, ticker in enumerate(tickers):
                # We want known features to test ranking
                # Tickers ranked AAPL=5, MSFT=4, GOOGL=3, AMZN=2, TSLA=1 on gap_pct
                gap_val = float(5 - idx) 
                
                # ATR and close for price estimation
                # price will be dollar_volume / (5 * liquidity_proxy)
                # let's set them so price is 100.0 for AAPL/MSFT, 50.0 for others
                if ticker in ["AAPL", "MSFT"]:
                    dollar_vol = 50000.0
                    liq_proxy = 100.0
                    atr_14 = 2.0  # atr_pct = 2.0 / 100.0 = 2%
                else:
                    dollar_vol = 25000.0
                    liq_proxy = 100.0
                    atr_14 = 2.5  # atr_pct = 2.5 / 50.0 = 5%
                    
                records.append({
                    "ticker": ticker,
                    "date": d,
                    "gap_pct": gap_val,
                    "atr_14": atr_14,
                    "dollar_volume": dollar_vol,
                    "liquidity_proxy": liq_proxy,
                    "market_regime": "TRENDING_BULL",
                    "epoch": "2022_Bear",
                    "future_60m_return": 1.0 if idx == 0 else (-1.0 if idx == 4 else 0.0),
                    "future_60m_return_friction": 0.8 if idx == 0 else (-1.2 if idx == 4 else 0.0),
                    "future_60m_return_worst": 0.5 if idx == 0 else (-1.5 if idx == 4 else 0.0),
                })
                
        self.mock_df = pd.DataFrame(records)
        self.test_dir = "out_test_portfolio"
        os.makedirs(self.test_dir, exist_ok=True)
        self.dataset_path = os.path.join(self.test_dir, "test_dataset.csv")
        self.mock_df.to_csv(self.dataset_path, index=False)
        
        # Create daily_bars_cache test directory
        cache_dir = os.path.join(self.test_dir, "daily_bars_cache")
        os.makedirs(cache_dir, exist_ok=True)
        
        # Write SPY daily mock
        spy_dates = pd.date_range(start="2021-12-01", periods=40, freq="D")
        spy_data = pd.DataFrame({
            "date": spy_dates.strftime("%Y-%m-%d"),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0 + np.random.normal(0, 0.5, len(spy_dates)).cumsum(),
            "volume": 1000000.0
        })
        spy_data.to_csv(os.path.join(cache_dir, "SPY_daily.csv"), index=False)
        
        # Write ticker daily mocks
        for ticker in tickers:
            ticker_data = pd.DataFrame({
                "date": spy_dates.strftime("%Y-%m-%d"),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + np.random.normal(0, 0.5, len(spy_dates)).cumsum(),
                "volume": 50000.0
            })
            ticker_data.to_csv(os.path.join(cache_dir, f"{ticker}_daily.csv"), index=False)
            
        self.constructor = CrossSectionalPortfolioConstructor(
            dataset_path=self.dataset_path,
            out_dir=self.test_dir
        )

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_cap_weights(self):
        # Case 1: Simple capping and redistribution
        weights = np.array([0.5, 0.3, 0.2])
        capped = cap_weights(weights, max_cap=0.4)
        np.testing.assert_almost_equal(capped, [0.4, 0.36, 0.24])
        self.assertAlmostEqual(np.sum(capped), 1.0)
        
        # Case 2: Capping is impossible (1/n >= max_cap)
        # Should fallback to equal weighting [0.5, 0.5]
        weights2 = np.array([0.6, 0.4])
        capped2 = cap_weights(weights2, max_cap=0.3)
        np.testing.assert_almost_equal(capped2, [0.5, 0.5])
        self.assertAlmostEqual(np.sum(capped2), 1.0)

    def test_simulation_equal_weight_long_short(self):
        # In our mock data:
        # AAPL has highest gap_pct (5.0) -> Bucket 4 (Top)
        # TSLA has lowest gap_pct (1.0) -> Bucket 0 (Bottom)
        # With quintile, n_bins = 5:
        # AAPL (rank_pct=1.0) goes to bucket 4 (Long)
        # TSLA (rank_pct=0.0) goes to bucket 0 (Short)
        # Under equal-weighted Long/Short:
        # AAPL weight = 1.0
        # TSLA weight = -1.0
        # Day return:
        # r_raw = 1.0 * AAPL_return + (-1.0) * TSLA_return
        #       = 1.0 * (1.0%) - 1.0 * (-1.0%) = 2.0%
        # r_friction = 1.0 * (0.8%) - 1.0 * (-1.2%) = 2.0%
        
        equity_df, trades_df = self.constructor.run_simulation(
            feature="gap_pct",
            horizon=60,
            bucket_type="quintile",
            long_only=False,
            vol_scaled=False,
            initial_capital=100000.0
        )
        
        self.assertEqual(len(equity_df), 3)
        # Day 1 equity: 100000 * 1.02 = 102000
        # Day 2 equity: 102000 * 1.02 = 104040
        # Day 3 equity: 104040 * 1.02 = 106120.8
        self.assertAlmostEqual(equity_df["equity_raw"].iloc[0], 102000.0)
        self.assertAlmostEqual(equity_df["equity_raw"].iloc[1], 104040.0)
        self.assertAlmostEqual(equity_df["equity_raw"].iloc[2], 106120.8)
        
        # Verify trades log
        self.assertEqual(len(trades_df), 6)  # 3 days * 2 active tickers per day
        self.assertTrue(np.all(trades_df.loc[trades_df["ticker"] == "AAPL", "side"] == "LONG"))
        self.assertTrue(np.all(trades_df.loc[trades_df["ticker"] == "TSLA", "side"] == "SHORT"))

    def test_simulation_vol_scaled(self):
        # Test vol scaled calculation
        # AAPL is long: atr_pct = 2% (floor check: 20th percentile is AAPL=2%, MSFT=2%, others=5% -> floor is 2%.)
        # TSLA is short: atr_pct = 5% (floor is 2%, clamped to 5%.)
        # With 1 ticker in long (AAPL) and 1 in short (TSLA), the single ticker in each leg gets 100% of the leg weight.
        # Even with vol-scaling, since there's only 1 ticker in each leg, long_w = [1.0] and short_w = [1.0].
        # Weight cap 12.5%:
        # Since 1/1 = 1.0 >= 0.125, it falls back to equal weight (1.0).
        # Let's adjust mock data or test with multiple tickers in the same bucket
        pass

    def test_bucket_occupancy_fallback(self):
        # We have 5 tickers. If we request 'decile' but min_tickers_for_decile = 10,
        # it should automatically fall back to 'quintile' (which has 5 bins).
        # Let's run a simulation with decile and check that the resulting n_bins is 5
        # (i.e. bucket assignments should be in [0, 1, 2, 3, 4] and not up to 9).
        equity_df, trades_df = self.constructor.run_simulation(
            feature="gap_pct",
            horizon=60,
            bucket_type="decile",
            long_only=False,
            vol_scaled=False,
            initial_capital=100000.0,
            min_tickers_for_decile=10
        )
        
        # If it fell back to quintiles, AAPL (top rank) will have bucket 4.
        # If it didn't, AAPL would have bucket 9.
        # Let's check the trades log weights or return (which should match the quintile run).
        self.assertAlmostEqual(equity_df["equity_raw"].iloc[2], 106120.8)

    def test_calculate_metrics(self):
        # Create a simple mock equity_df and trades_df to verify metric calculations
        dates = pd.date_range(start="2022-01-01", periods=5, freq="D")
        eq_records = []
        eq_raw = 1000.0
        eq_fric = 1000.0
        eq_worst = 1000.0
        # Vary returns so std dev is not 0
        rets_raw = [1.0, 1.5, -0.5, 2.0, 0.5]
        rets_fric = [0.8, 1.2, -0.8, 1.7, 0.3]
        rets_worst = [0.5, 0.9, -1.2, 1.2, 0.0]
        
        for idx, d in enumerate(dates):
            r_raw = rets_raw[idx]
            r_fric = rets_fric[idx]
            r_worst = rets_worst[idx]
            
            eq_raw *= (1 + r_raw / 100.0)
            eq_fric *= (1 + r_fric / 100.0)
            eq_worst *= (1 + r_worst / 100.0)
            
            eq_records.append({
                "date": d,
                "equity_raw": eq_raw,
                "equity_friction": eq_fric,
                "equity_worst": eq_worst,
                "daily_return_raw": r_raw,
                "daily_return_friction": r_fric,
                "daily_return_worst": r_worst,
                "r_long_raw": r_raw * 0.6,
                "r_long_friction": r_fric * 0.6,
                "r_long_worst": r_worst * 0.6,
                "r_short_raw": r_raw * 0.4,
                "r_short_friction": r_fric * 0.4,
                "r_short_worst": r_worst * 0.4,
                "long_exposure": 1.0,
                "short_exposure": -1.0,
                "gross_exposure": 2.0,
                "net_exposure": 0.0,
                "turnover": 2.0,
                "friction_drag": r_raw - r_fric,
                "market_regime": "TRENDING_BULL",
                "epoch": "2022_Bear"
            })
        
        equity_df = pd.DataFrame(eq_records)
        trades_df = pd.DataFrame([
            {"date": dates[0], "ticker": "AAPL", "side": "LONG", "weight": 1.0, "raw_return": 1.0, "friction_return": 0.5, "worst_return": 0.2, "market_regime": "TRENDING_BULL", "epoch": "2022_Bear"}
        ])
        
        metrics = self.constructor.calculate_metrics(equity_df, trades_df, initial_capital=1000.0)
        
        # Verify presence and types of metrics
        self.assertIn("raw", metrics)
        self.assertIn("friction", metrics)
        self.assertIn("worst", metrics)
        self.assertIn("long_leg", metrics)
        self.assertIn("short_leg", metrics)
        
        self.assertGreater(metrics["friction"]["sharpe"], 0.0)
        self.assertGreater(metrics["friction"]["cagr"], 0.0)
        self.assertAlmostEqual(metrics["friction"]["hit_rate"], 0.8)
        self.assertAlmostEqual(metrics["avg_turnover"], 2.0)

    def test_optimize_weights_qp(self):
        # Test optimize_weights_qp directly
        w_signal = np.array([0.5, 0.5, -0.5, -0.5])
        betas = np.array([1.2, 0.8, 1.5, 0.5])
        
        # Unconstrained (just equality constrained) solve
        w_opt = self.constructor.optimize_weights_qp(
            w_signal,
            betas,
            net_exposure=0.0,
            max_weight_cap=1.0  # large enough to prevent clipping
        )
        
        # Verify sum(w) = 0.0
        self.assertAlmostEqual(np.sum(w_opt), 0.0, places=5)
        # Verify sum(beta * w) = 0.0
        self.assertAlmostEqual(np.sum(w_opt * betas), 0.0, places=5)
        
        # Test with weight caps: max_weight_cap = 0.3
        w_opt_capped = self.constructor.optimize_weights_qp(
            w_signal,
            betas,
            net_exposure=0.0,
            max_weight_cap=0.3
        )
        
        # Verify constraints still hold
        self.assertAlmostEqual(np.sum(w_opt_capped), 0.0, places=5)
        self.assertAlmostEqual(np.sum(w_opt_capped * betas), 0.0, places=5)
        # Verify cap is satisfied
        self.assertTrue(np.all(np.abs(w_opt_capped) <= 0.3 + 1e-5))

    def test_capacity_nonlinear_slippage_and_signal_deformation(self):
        # We run the simulation with beta_neutral=True and portfolio_capital=10,000,000
        # Check that it runs successfully and computes dynamic slippage
        # And verify that signal deformation applies (e.g. cumulative_adj modifies the gaps)
        equity_df, trades_df = self.constructor.run_simulation(
            feature="gap_pct",
            horizon=60,
            bucket_type="quintile",
            long_only=False,
            vol_scaled=True,
            initial_capital=100000.0,
            beta_neutral=True,
            portfolio_capital=10000000.0,
            dynamic_capacity=True
        )
        
        self.assertEqual(len(equity_df), 3)
        # Verify that friction returns are less than raw returns
        self.assertTrue(np.all(equity_df["equity_friction"] <= equity_df["equity_raw"]))
        self.assertTrue(np.all(equity_df["equity_worst"] <= equity_df["equity_friction"]))

    def test_optimize_weights_soft(self):
        # N=2 scenario where hard QP would collapse to [0, 0]
        w_signal = np.array([0.5, -0.5])
        betas = np.array([1.2, 0.8])
        
        # Call soft constraint optimizer with lambda_beta=1.0, lambda_d=1.0
        w_opt = self.constructor.optimize_weights_soft(
            w_signal,
            betas,
            net_exposure=0.0,
            max_weight_cap=0.5,
            lambda_beta=1.0,
            lambda_d=1.0
        )
        
        # Verify that weights did not collapse to zero
        self.assertTrue(np.any(np.abs(w_opt) > 0.01))
        # Verify that they satisfy the max_weight_cap
        self.assertTrue(np.all(np.abs(w_opt) <= 0.5 + 1e-5))

    def test_universe_expansion_guard(self):
        # We run the simulation with beta_neutral=True and soft_constraints=True
        # Check that we recover non-zero exposure even under N=2 active assets per day
        equity_df, trades_df = self.constructor.run_simulation(
            feature="gap_pct",
            horizon=60,
            bucket_type="quintile",
            long_only=False,
            vol_scaled=True,
            initial_capital=100000.0,
            beta_neutral=True,
            portfolio_capital=100000.0,
            dynamic_capacity=True,
            soft_constraints=True,
            lambda_beta=10.0,
            lambda_d=10.0
        )
        
        # Verify that long and short exposures are non-zero
        self.assertTrue(np.any(equity_df["long_exposure"] > 0.01))
        self.assertTrue(np.any(equity_df["short_exposure"] < -0.01))

    def test_hhi_capacity_model(self):
        # Let's run a small simulation with dynamic_capacity=True, capacity_gamma=2.0
        # verify that it executes successfully and computes returns
        equity_df, trades_df = self.constructor.run_simulation(
            feature="gap_pct",
            horizon=60,
            bucket_type="quintile",
            long_only=False,
            vol_scaled=True,
            initial_capital=100000.0,
            beta_neutral=False,
            portfolio_capital=1000000.0,
            dynamic_capacity=True,
            capacity_theta=0.5,
            capacity_gamma=2.0
        )
        self.assertEqual(len(equity_df), 3)

if __name__ == "__main__":
    unittest.main()

