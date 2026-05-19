import os
import json
import pandas as pd
import numpy as np

def run_minimal_reality_backtest(
    df: pd.DataFrame,
    signal_col: str,
    horizon_minutes: int = 60,
    train_split: float = 0.6,
    transaction_cost_bps: float = 1.5,
    slippage_bps: float = 1.0,
    min_universe_size: int = 2,
) -> dict:
    """
    Validation gate backtester.
    Runs a walk-forward testing script under basic friction without advanced optimization.
    """
    ret_col = f"future_{horizon_minutes}m_return"
    if ret_col not in df.columns:
        raise ValueError(f"Required return column '{ret_col}' not found in DataFrame.")
    if signal_col not in df.columns:
        raise ValueError(f"Required signal column '{signal_col}' not found in DataFrame.")

    # 1. Deterministic Split
    unique_dates = sorted(df["date"].unique())
    n_dates = len(unique_dates)
    if n_dates == 0:
        raise ValueError("DataFrame contains no dates.")

    split_idx = int(n_dates * train_split)
    train_dates = unique_dates[:split_idx]
    test_dates = unique_dates[split_idx:]

    # 2. Backtest engine runner
    def run_simulation_for_dates(dates_list, tc, slip):
        cost_factor_bps = (tc + slip) * 2
        cost_pct = cost_factor_bps / 100.0  # 1 bps = 0.01%
        
        daily_returns = []
        trade_counts = []
        universe_sizes = []
        skipped_days_flags = []

        # Pre-group by date for speed
        daily_groups = {d: g for d, g in df[df["date"].isin(dates_list)].groupby("date")}

        for d in dates_list:
            if d not in daily_groups:
                skipped_days_flags.append(True)
                daily_returns.append(0.0)
                trade_counts.append(0)
                universe_sizes.append(0)
                continue

            day_df = daily_groups[d]
            n_univ = len(day_df)
            universe_sizes.append(n_univ)

            if n_univ < min_universe_size:
                skipped_days_flags.append(True)
                daily_returns.append(0.0)
                trade_counts.append(0)
                continue

            # Sorting by signal descending
            sorted_df = day_df.sort_values(signal_col, ascending=False)

            if n_univ >= 10:
                q_size = n_univ // 5
                if q_size < 1:
                    q_size = 1
                long = sorted_df.head(q_size)
                short = sorted_df.tail(q_size)
            else:
                long = sorted_df.head(1)
                short = sorted_df.tail(1)

            n_long = len(long)
            n_short = len(short)

            # Equal weights
            w_long = 1.0 / n_long
            w_short = -1.0 / n_short

            # Net returns calculation: w_i * return - abs(w_i) * cost
            long_rets = long[ret_col].values
            short_rets = short[ret_col].values

            long_net = w_long * long_rets - w_long * cost_pct
            short_net = w_short * short_rets - abs(w_short) * cost_pct

            day_ret = np.sum(long_net) + np.sum(short_net)
            daily_returns.append(day_ret)
            trade_counts.append(n_long + n_short)
            skipped_days_flags.append(False)

        return np.array(daily_returns), np.array(trade_counts), np.array(universe_sizes), np.array(skipped_days_flags)

    # 3. Calculate Performance Metrics
    def calculate_period_metrics(daily_rets):
        n_days = len(daily_rets)
        if n_days == 0:
            return {"cagr": 0.0, "sharpe": 0.0, "max_drawdown": 0.0, "hit_rate": 0.0}

        # Equity curve compounding
        equity = np.zeros(n_days + 1)
        equity[0] = 1.0
        for i in range(n_days):
            equity[i+1] = equity[i] * (1.0 + daily_rets[i] / 100.0)

        # CAGR (assumes 252 trading days per year)
        final_equity = equity[-1]
        years = n_days / 252.0
        cagr = (final_equity) ** (1.0 / years) - 1.0 if years > 0 and final_equity > 0 else 0.0

        # Sharpe Ratio (annualized)
        mean_ret = np.mean(daily_rets)
        std_ret = np.std(daily_rets, ddof=1)
        sharpe = np.sqrt(252) * (mean_ret / std_ret) if std_ret > 0 else 0.0

        # Max Drawdown
        equity_series = pd.Series(equity)
        peaks = equity_series.cummax()
        drawdowns = (equity_series - peaks) / peaks
        max_dd = drawdowns.min()

        # Hit Rate
        hit_rate = (daily_rets > 0).mean()

        return {
            "cagr": float(cagr),
            "sharpe": float(sharpe),
            "max_drawdown": float(max_dd),
            "hit_rate": float(hit_rate)
        }

    # Run default cost simulation ONCE on the full dataset (OPTIMIZED)
    full_rets, full_trades, full_univ, full_skipped = run_simulation_for_dates(unique_dates, transaction_cost_bps, slippage_bps)

    # Slice the results into train and test periods (OPTIMIZED: O(1) slice instead of re-simulating)
    train_rets = full_rets[:split_idx]
    test_rets = full_rets[split_idx:]

    train_skipped_count = int(np.sum(full_skipped[:split_idx]))
    test_skipped_count = int(np.sum(full_skipped[split_idx:]))
    full_skipped_count = int(np.sum(full_skipped))

    # Compute period metrics
    train_metrics = calculate_period_metrics(train_rets)
    test_metrics = calculate_period_metrics(test_rets)
    full_metrics = calculate_period_metrics(full_rets)

    # Robustness / Friction Sensitivity (Run ONCE on full dataset)
    high_tc = transaction_cost_bps * 2.0
    high_slip = slippage_bps * 2.0
    full_rets_high, _, _, _ = run_simulation_for_dates(unique_dates, high_tc, high_slip)

    # Cumulative baseline vs high cost returns (final_equity - 1)
    baseline_equity = np.prod(1.0 + full_rets / 100.0)
    high_cost_equity = np.prod(1.0 + full_rets_high / 100.0)

    baseline_cum_ret = baseline_equity - 1.0
    high_cost_cum_ret = high_cost_equity - 1.0

    if baseline_cum_ret <= 0.0:
        friction_sensitivity = 1.0
    else:
        friction_sensitivity = (baseline_cum_ret - high_cost_cum_ret) / baseline_cum_ret

    # Diagnostics
    avg_trades = float(np.mean(full_trades))
    avg_univ = float(np.mean(full_univ))
    skipped_fraction = float(full_skipped_count / n_dates)

    diagnostics = {
        "avg_daily_trades": avg_trades,
        "avg_universe_size": avg_univ,
        "fraction_skipped_days": skipped_fraction,
        "friction_sensitivity": float(friction_sensitivity)
    }

    # Sharpe Retention Ratio
    train_sharpe = train_metrics["sharpe"]
    test_sharpe = test_metrics["sharpe"]
    retention_ratio = (test_sharpe / train_sharpe) if train_sharpe > 0 else 0.0

    # Failure Conditions Checks
    failure_reasons = []
    
    # A. Overfitting collapse: test_sharpe < 0.5 * train_sharpe or train_sharpe <= 0
    if train_sharpe <= 0.0:
        failure_reasons.append(f"Overfitting collapse: Negative/zero train Sharpe ({train_sharpe:.3f})")
    elif test_sharpe < 0.5 * train_sharpe:
        failure_reasons.append(f"Overfitting collapse: Test Sharpe ({test_sharpe:.3f}) is less than 50% of Train Sharpe ({train_sharpe:.3f})")

    # B. Illiquid structure dominance: avg_trades_per_day < 1.5
    if avg_trades < 1.5:
        failure_reasons.append(f"Illiquid structure dominance: Average daily trades ({avg_trades:.3f}) < 1.5")

    # C. Fragility to cost: friction_sensitivity > 0.5
    if friction_sensitivity > 0.5:
        failure_reasons.append(f"Fragility to cost: Friction sensitivity ({friction_sensitivity:.3f}) > 0.5")

    # D. Degenerate universe: fraction_skipped_days > 0.3
    if skipped_fraction > 0.3:
        failure_reasons.append(f"Degenerate universe: Fraction of skipped days ({skipped_fraction:.3f}) > 0.3")

    status = "FAIL" if failure_reasons else "PASS"

    return {
        "status": status,
        "failure_reasons": failure_reasons,
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "full_metrics": full_metrics,
        "diagnostics": {
            "avg_daily_trades": avg_trades,
            "avg_universe_size": avg_univ,
            "fraction_skipped_days": skipped_fraction,
            "friction_sensitivity": float(friction_sensitivity),
            "sharpe_retention_ratio": float(retention_ratio)
        }
    }
