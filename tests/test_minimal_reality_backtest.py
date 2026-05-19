import unittest
import pandas as pd
import numpy as np
from src.research.minimal_reality_backtest import run_minimal_reality_backtest

class TestMinimalRealityBacktest(unittest.TestCase):
    def setUp(self):
        # Setup mock DataFrame with 10 dates, 12 tickers each (so N >= 10 quintiles are tested)
        self.tickers = [f"TICKER_{i}" for i in range(12)]
        self.dates = [f"2026-05-{i:02d}" for i in range(1, 11)]

    def test_run_minimal_reality_backtest_pass(self):
        # Create a dataframe where the signal is highly predictive
        records = []
        for d_idx, d in enumerate(self.dates):
            # Introduce variance across days by varying the signal coefficient
            spread_coef = 1.0 + 0.3 * (d_idx % 3 - 1.0)
            for i, ticker in enumerate(self.tickers):
                # High signal gets high future return, low signal gets low future return
                signal = float(i)
                # Future return is linear with signal (predictive)
                # Mean centered return to make it clean
                future_return = (signal - 5.5) * 2.0 * spread_coef
                
                records.append({
                    "date": d,
                    "ticker": ticker,
                    "gap_pct": signal,
                    "future_60m_return": future_return
                })
        df = pd.DataFrame(records)

        result = run_minimal_reality_backtest(
            df=df,
            signal_col="gap_pct",
            horizon_minutes=60,
            train_split=0.6,
            transaction_cost_bps=0.5, # low costs
            slippage_bps=0.5,
            min_universe_size=2
        )

        self.assertIn("status", result)
        self.assertIn("train_metrics", result)
        self.assertIn("test_metrics", result)
        self.assertIn("full_metrics", result)
        self.assertIn("diagnostics", result)
        
        # Diagnostics values checks
        self.assertEqual(result["diagnostics"]["avg_daily_trades"], 4.0) # top quintile of 12 = 12 // 5 = 2. Long 2, Short 2. Total 4.
        self.assertEqual(result["diagnostics"]["fraction_skipped_days"], 0.0)
        self.assertEqual(result["status"], "PASS")

    def test_run_minimal_reality_backtest_fail_overfitting(self):
        # Train dates (first 6 dates): highly predictive
        # Test dates (last 4 dates): random/zero returns (overfitting collapse)
        records = []
        for d_idx, d in enumerate(self.dates):
            is_train = d_idx < 6
            spread_coef = 1.0 + 0.3 * (d_idx % 3 - 1.0)
            for i, ticker in enumerate(self.tickers):
                signal = float(i)
                if is_train:
                    future_return = (signal - 5.5) * 5.0 * spread_coef
                else:
                    # random returns near zero to tank the Sharpe in test
                    future_return = 0.01 if (i + d_idx) % 2 == 0 else -0.01

                records.append({
                    "date": d,
                    "ticker": ticker,
                    "gap_pct": signal,
                    "future_60m_return": future_return
                })
        df = pd.DataFrame(records)

        result = run_minimal_reality_backtest(
            df=df,
            signal_col="gap_pct",
            horizon_minutes=60,
            train_split=0.6,
            transaction_cost_bps=0.5,
            slippage_bps=0.5
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("Overfitting collapse" in reason for reason in result["failure_reasons"]))

    def test_run_minimal_reality_backtest_fail_skipped_days(self):
        # Create a dataframe where half of the days have only 1 ticker (which is < min_universe_size)
        records = []
        for idx, d in enumerate(self.dates):
            # If idx is odd, only append 1 ticker
            # If idx is even, append all tickers
            is_skipped = idx % 2 == 1
            tickers_to_use = self.tickers[:1] if is_skipped else self.tickers
            for i, ticker in enumerate(tickers_to_use):
                signal = float(i)
                records.append({
                    "date": d,
                    "ticker": ticker,
                    "gap_pct": signal,
                    "future_60m_return": 1.0
                })
        df = pd.DataFrame(records)

        result = run_minimal_reality_backtest(
            df=df,
            signal_col="gap_pct",
            horizon_minutes=60,
            train_split=0.6,
            transaction_cost_bps=0.5,
            slippage_bps=0.5,
            min_universe_size=2
        )

        # 5 out of 10 days are skipped (fraction = 0.5 > 0.3)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("Degenerate universe" in reason for reason in result["failure_reasons"]))
        self.assertEqual(result["diagnostics"]["fraction_skipped_days"], 0.5)

if __name__ == "__main__":
    unittest.main()
