"""Tests for benchmark and risk-adjusted analytics (backend/performance.py, #9).

The invariant under test: metrics appear only when the sample supports them —
never a percentage without N, never a fabricated figure. The small-N path is
pinned as carefully as the math.
"""

import pytest

from backend.performance import (
    MIN_TRADES_FOR_RISK_METRICS,
    TradeRecord,
    compute_risk_metrics,
    spy_benchmark_cagr,
)


def _trade(entry: str, exit_: str, pnl: float, risk: float = 200.0) -> TradeRecord:
    return TradeRecord(entry_date=entry, exit_date=exit_, realized_pnl=pnl, capital_at_risk=risk)


def _n_trades(n: int, pnl_pattern: list[float]) -> list[TradeRecord]:
    """n trades spread over ~a year with a repeating P&L pattern."""
    trades = []
    for i in range(n):
        month = i % 12 + 1
        trades.append(_trade(f"2026-{month:02d}-01", f"2026-{month:02d}-20", pnl_pattern[i % len(pnl_pattern)]))
    return trades


class TestSpyBenchmark:
    def test_annualizes_a_full_year(self):
        closes = [("2025-08-18", 500.0), ("2026-08-18", 550.0)]
        assert spy_benchmark_cagr(closes) == pytest.approx(0.1, abs=0.001)

    def test_short_window_returns_none(self):
        closes = [("2026-06-01", 500.0), ("2026-08-18", 550.0)]  # ~78 days — annualizing would mislead
        assert spy_benchmark_cagr(closes) is None

    def test_empty_and_single_row_return_none(self):
        assert spy_benchmark_cagr([]) is None
        assert spy_benchmark_cagr([("2026-08-18", 500.0)]) is None

    def test_unsorted_input_is_handled(self):
        closes = [("2026-08-18", 550.0), ("2025-08-18", 500.0)]
        assert spy_benchmark_cagr(closes) == pytest.approx(0.1, abs=0.001)


class TestRiskMetrics:
    def test_small_sample_gates_cagr_and_sharpe_but_not_drawdown(self):
        trades = _n_trades(MIN_TRADES_FOR_RISK_METRICS - 1, [50.0, -40.0])
        metrics = compute_risk_metrics(trades)
        assert metrics.cagr is None
        assert metrics.sharpe is None
        assert metrics.max_drawdown is not None  # a dollar figure is honest at any N

    def test_sufficient_sample_produces_figures(self):
        trades = _n_trades(12, [50.0, -40.0, 60.0])
        metrics = compute_risk_metrics(trades)
        assert metrics.cagr is not None and metrics.cagr > 0  # net-positive pattern
        assert metrics.sharpe is not None
        assert metrics.max_drawdown is not None

    def test_zero_dispersion_yields_no_sharpe(self):
        # Ten identical wins: std = 0 — degenerate sample, not infinite skill.
        trades = _n_trades(10, [50.0])
        assert compute_risk_metrics(trades).sharpe is None
        assert compute_risk_metrics(trades).cagr is not None

    def test_max_drawdown_is_peak_to_trough_in_exit_order(self):
        trades = [
            _trade("2026-01-01", "2026-01-10", 100.0),
            _trade("2026-02-01", "2026-02-10", -80.0),
            _trade("2026-03-01", "2026-03-10", -50.0),
            _trade("2026-04-01", "2026-04-10", 200.0),
        ]
        assert compute_risk_metrics(trades).max_drawdown == 130.0  # peak +100 → trough -30

    def test_short_span_gates_annualized_figures(self):
        # 12 trades all inside one week — annualizing would fabricate a number.
        trades = [_trade("2026-08-10", f"2026-08-{11 + i % 5:02d}", 50.0 if i % 2 else -40.0) for i in range(12)]
        metrics = compute_risk_metrics(trades)
        assert metrics.cagr is None
        assert metrics.sharpe is None

    def test_no_trades(self):
        metrics = compute_risk_metrics([])
        assert metrics.cagr is None and metrics.sharpe is None and metrics.max_drawdown is None

    def test_cagr_math_on_known_input(self):
        # Two trades bracketing exactly one year, +10% on risk each, rest neutral fillers
        trades = [_trade("2026-01-01", "2026-06-01", 20.0)] * 5 + [_trade("2026-06-01", "2027-01-01", 20.0)] * 5
        metrics = compute_risk_metrics(trades)
        # multiplier = 1.1^10 over exactly 365 days → CAGR = 1.1^10 − 1
        assert metrics.cagr == pytest.approx(1.1**10 - 1, abs=0.001)
