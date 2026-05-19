import os
import datetime
import pandas as pd
import numpy as np
from tabulate import tabulate
from src.research.feature_survival_engine import EPOCHS

def cap_weights(weights: np.ndarray, max_cap: float) -> np.ndarray:
    """
    Enforce max weight cap on a 1D numpy array of weights (which sum to 1.0).
    Redistributes the excess weight proportionally.
    """
    w = np.copy(weights)
    n = len(w)
    if n == 0 or max_cap >= 1.0:
        return w
    
    # If the uniform weight is already >= max_cap, we must equal-weight
    if 1.0 / n >= max_cap:
        return np.ones(n) / n
        
    for _ in range(100):
        exceeded = w > max_cap
        if not np.any(exceeded):
            break
        
        excess = np.sum(w[exceeded] - max_cap)
        w[exceeded] = max_cap
        
        allowed = w < max_cap
        if not np.any(allowed):
            break
        
        allowed_sum = np.sum(w[allowed])
        if allowed_sum > 0:
            w[allowed] += excess * (w[allowed] / allowed_sum)
        else:
            w[allowed] += excess / np.sum(allowed)
            
    # Normalize to prevent floating point sum issues
    w_sum = np.sum(w)
    if w_sum > 0:
        w = w / w_sum
    return w

class CrossSectionalPortfolioConstructor:
    """
    Cross-Sectional Portfolio Constructor research engine.
    Simulates portfolio equity curves and trade logs based on daily cross-sectional feature rankings.
    """
    def __init__(self, dataset_path: str = "out/causal_feature_dataset.csv", out_dir: str = "out"):
        self.dataset_path = dataset_path
        self.out_dir = out_dir
        self.df = None

    def load_data(self) -> pd.DataFrame:
        """Loads and prepares the dataset from CSV."""
        if not os.path.exists(self.dataset_path):
            raise FileNotFoundError(f"Dataset not found at {self.dataset_path}")
        self.df = pd.read_csv(self.dataset_path)
        self.df["date"] = pd.to_datetime(self.df["date"])
        
        # Ensure epoch column is assigned dynamically if missing from CSV disk cache
        if "epoch" not in self.df.columns:
            self.df["epoch"] = "Out_of_Epoch"
            for name, start_str, end_str in EPOCHS:
                start = pd.to_datetime(start_str)
                end = pd.to_datetime(end_str)
                self.df.loc[(self.df["date"] >= start) & (self.df["date"] <= end), "epoch"] = name
                
        # Sort chronologically by date and ticker
        self.df = self.df.sort_values(["date", "ticker"]).reset_index(drop=True)

        # Pre-compute estimated price and clamped ATR% globally to avoid loops
        denom = 5.0 * self.df["liquidity_proxy"]
        self.df["est_price"] = np.where(denom > 0, self.df["dollar_volume"] / denom, 100.0)
        self.df["atr_pct"] = self.df["atr_14"] / self.df["est_price"]
        
        # Daily 20th percentile floor
        daily_floor = self.df.groupby("date")["atr_pct"].quantile(0.2)
        self.df["floor"] = self.df["date"].map(daily_floor).fillna(1e-4)
        self.df["atr_pct_clamped"] = np.maximum(self.df["atr_pct"].fillna(self.df["floor"]), self.df["floor"])
        
        # Pre-group daily dataframes into a dictionary for fast lookup
        self.daily_groups = {d: group.copy() for d, group in self.df.groupby("date")}
        
        return self.df

    def run_simulation(
        self,
        feature: str = "gap_pct",
        horizon: int = 60,
        bucket_type: str = "quintile",
        long_only: bool = False,
        vol_scaled: bool = False,
        initial_capital: float = 1000000.0,
        max_weight_cap: float = 0.125,
        min_tickers_for_decile: int = 10
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        Simulates the cross-sectional portfolio construction and returns:
          1. Daily portfolio equity curve & stats dataframe.
          2. Trade log dataframe.
        """
        if self.df is None:
            self.load_data()

        dates = sorted(self.daily_groups.keys())
        equity_records = []
        trade_records = []

        eq_raw = initial_capital
        eq_friction = initial_capital
        eq_worst = initial_capital

        raw_col = f"future_{horizon}m_return"
        friction_col = f"future_{horizon}m_return_friction"
        worst_col = f"future_{horizon}m_return_worst"

        for d in dates:
            day_df = self.daily_groups[d]
            n_tickers = len(day_df)
            
            # Skip days with fewer than 2 tickers (impossible to do cross-sectional ranking)
            if n_tickers < 2:
                regime = day_df["market_regime"].iloc[0] if n_tickers > 0 else "UNKNOWN"
                equity_records.append({
                    "date": d,
                    "equity_raw": eq_raw,
                    "equity_friction": eq_friction,
                    "equity_worst": eq_worst,
                    "daily_return_raw": 0.0,
                    "daily_return_friction": 0.0,
                    "daily_return_worst": 0.0,
                    "r_long_raw": 0.0,
                    "r_long_friction": 0.0,
                    "r_long_worst": 0.0,
                    "r_short_raw": 0.0,
                    "r_short_friction": 0.0,
                    "r_short_worst": 0.0,
                    "long_exposure": 0.0,
                    "short_exposure": 0.0,
                    "gross_exposure": 0.0,
                    "net_exposure": 0.0,
                    "turnover": 0.0,
                    "friction_drag": 0.0,
                    "market_regime": regime
                })
                continue

            # Handle decile bucket occupancy check
            actual_bucket_type = bucket_type
            if bucket_type == "decile" and n_tickers < min_tickers_for_decile:
                # Automatic fallback to quintiles for statistical robustness
                actual_bucket_type = "quintile"

            group = day_df.copy()
            ranks = group[feature].rank(method="first")
            rank_pct = (ranks - 1) / (n_tickers - 1)
            group["rank_pct"] = rank_pct

            n_bins = 5 if actual_bucket_type == "quintile" else 10
            group["bucket"] = pd.cut(
                rank_pct,
                bins=np.linspace(0.0, 1.0, n_bins + 1),
                include_lowest=True,
                labels=False
            )

            # Split into Long and Short legs
            long_mask = group["bucket"] == (n_bins - 1)
            short_mask = group["bucket"] == 0

            long_tickers = group[long_mask]
            short_tickers = group[short_mask]

            n_long = len(long_tickers)
            n_short = len(short_tickers)

            weights = {}

            # Calculate weights for Long leg
            if n_long > 0:
                if not vol_scaled:
                    long_w = np.ones(n_long) / n_long
                else:
                    inv_vol = 1.0 / long_tickers["atr_pct_clamped"]
                    inv_vol_sum = inv_vol.sum()
                    if inv_vol_sum > 0:
                        long_w = inv_vol.values / inv_vol_sum
                    else:
                        long_w = np.ones(n_long) / n_long
                    long_w = cap_weights(long_w, max_weight_cap)

                for idx, ticker in enumerate(long_tickers["ticker"]):
                    weights[ticker] = long_w[idx]

            # Calculate weights for Short leg
            if not long_only and n_short > 0:
                if not vol_scaled:
                    short_w = np.ones(n_short) / n_short
                else:
                    inv_vol = 1.0 / short_tickers["atr_pct_clamped"]
                    inv_vol_sum = inv_vol.sum()
                    if inv_vol_sum > 0:
                        short_w = inv_vol.values / inv_vol_sum
                    else:
                        short_w = np.ones(n_short) / n_short
                    short_w = cap_weights(short_w, max_weight_cap)

                for idx, ticker in enumerate(short_tickers["ticker"]):
                    weights[ticker] = -short_w[idx]

            # Exposure tracking
            long_w_sum = sum(w for w in weights.values() if w > 0.0)
            short_w_sum = sum(w for w in weights.values() if w < 0.0)
            gross_exposure = long_w_sum + abs(short_w_sum)
            net_exposure = long_w_sum + short_w_sum
            turnover = gross_exposure # Daily one-way traded volume equivalent to enter/exit from cash

            # Map daily weights back to group
            group["weight"] = group["ticker"].map(weights).fillna(0.0)

            # Compute daily portfolio returns
            r_raw = np.sum(group["weight"] * group[raw_col])
            r_friction = np.sum(group["weight"] * group[friction_col])
            r_worst = np.sum(group["weight"] * group[worst_col])

            # Leg-level returns attribution
            long_weight_series = group["weight"].clip(lower=0.0)
            r_long_raw = np.sum(long_weight_series * group[raw_col])
            r_long_friction = np.sum(long_weight_series * group[friction_col])
            r_long_worst = np.sum(long_weight_series * group[worst_col])

            short_weight_series = group["weight"].clip(upper=0.0)
            r_short_raw = np.sum(short_weight_series * group[raw_col])
            r_short_friction = np.sum(short_weight_series * group[friction_col])
            r_short_worst = np.sum(short_weight_series * group[worst_col])

            # Update compounded equity curve
            eq_raw *= (1.0 + r_raw / 100.0)
            eq_friction *= (1.0 + r_friction / 100.0)
            eq_worst *= (1.0 + r_worst / 100.0)

            regime = group["market_regime"].iloc[0]

            equity_records.append({
                "date": d,
                "equity_raw": eq_raw,
                "equity_friction": eq_friction,
                "equity_worst": eq_worst,
                "daily_return_raw": r_raw,
                "daily_return_friction": r_friction,
                "daily_return_worst": r_worst,
                "r_long_raw": r_long_raw,
                "r_long_friction": r_long_friction,
                "r_long_worst": r_long_worst,
                "r_short_raw": r_short_raw,
                "r_short_friction": r_short_friction,
                "r_short_worst": r_short_worst,
                "long_exposure": long_w_sum,
                "short_exposure": short_w_sum,
                "gross_exposure": gross_exposure,
                "net_exposure": net_exposure,
                "turnover": turnover,
                "friction_drag": r_raw - r_friction,
                "market_regime": regime
            })

            # Save non-zero weight trades in log
            active_group = group[group["weight"] != 0.0]
            for _, row in active_group.iterrows():
                trade_records.append({
                    "date": d,
                    "ticker": row["ticker"],
                    "side": "LONG" if row["weight"] > 0 else "SHORT",
                    "weight": row["weight"],
                    "raw_return": row[raw_col],
                    "friction_return": row[friction_col],
                    "worst_return": row[worst_col],
                    "market_regime": row["market_regime"],
                    "epoch": row["epoch"]
                })

        equity_df = pd.DataFrame(equity_records)
        trades_df = pd.DataFrame(trade_records)

        # Pre-assign epoch to equity_df
        equity_df["epoch"] = "Out_of_Epoch"
        for name, start_str, end_str in EPOCHS:
            start = pd.to_datetime(start_str)
            end = pd.to_datetime(end_str)
            equity_df.loc[(equity_df["date"] >= start) & (equity_df["date"] <= end), "epoch"] = name

        return equity_df, trades_df

    def calculate_metrics(self, equity_df: pd.DataFrame, trades_df: pd.DataFrame, initial_capital: float = 1000000.0) -> dict:
        """Calculates performance and attribution metrics for the portfolio."""
        metrics = {}
        
        if equity_df.empty:
            return metrics

        # Parse date range for CAGR
        first_date = pd.to_datetime(equity_df["date"].iloc[0])
        last_date = pd.to_datetime(equity_df["date"].iloc[-1])
        years = (last_date - first_date).days / 365.25
        if years <= 0:
            years = 1.0

        profiles = ["raw", "friction", "worst"]
        for prof in profiles:
            eq_col = f"equity_{prof}"
            ret_col = f"daily_return_{prof}"
            
            final_eq = equity_df[eq_col].iloc[-1]
            cagr = (final_eq / initial_capital) ** (1.0 / years) - 1.0 if final_eq > 0 else -1.0

            daily_ret_fraction = equity_df[ret_col] / 100.0
            
            # Sharpe Ratio
            mean_ret = daily_ret_fraction.mean()
            std_ret = daily_ret_fraction.std(ddof=1)
            sharpe = np.sqrt(252) * (mean_ret / std_ret) if std_ret > 0 else 0.0

            # Sortino Ratio
            neg_ret = np.where(daily_ret_fraction < 0, daily_ret_fraction, 0.0)
            downside_std = np.std(neg_ret, ddof=1)
            sortino = np.sqrt(252) * (mean_ret / downside_std) if downside_std > 0 else 0.0

            # Max Drawdown
            equity_curve = equity_df[eq_col]
            cum_max = equity_curve.cummax()
            drawdowns = (equity_curve - cum_max) / cum_max
            max_dd = drawdowns.min()

            # Hit Rate
            hit_rate = (equity_df[ret_col] > 0).mean()

            # Profit Factor
            g_profit = equity_df.loc[equity_df[ret_col] > 0, ret_col].sum()
            g_loss = abs(equity_df.loc[equity_df[ret_col] < 0, ret_col].sum())
            profit_factor = g_profit / g_loss if g_loss > 0 else (99.9 if g_profit > 0 else 1.0)

            # Exposure Adjusted Expectancy
            avg_gross_exposure = equity_df["gross_exposure"].mean()
            expectancy = equity_df[ret_col].mean() / avg_gross_exposure if avg_gross_exposure > 0 else 0.0

            metrics[prof] = {
                "cagr": cagr,
                "sharpe": sharpe,
                "sortino": sortino,
                "max_drawdown": max_dd,
                "hit_rate": hit_rate,
                "profit_factor": profit_factor,
                "expectancy": expectancy,
                "final_equity": final_eq
            }

        # General portfolio metrics
        metrics["avg_turnover"] = equity_df["turnover"].mean()
        metrics["avg_long_exposure"] = equity_df["long_exposure"].mean()
        metrics["avg_short_exposure"] = equity_df["short_exposure"].mean()
        metrics["avg_gross_exposure"] = equity_df["gross_exposure"].mean()
        metrics["avg_net_exposure"] = equity_df["net_exposure"].mean()

        # 1. Long vs Short Attribution (using friction profile)
        for side in ["long", "short"]:
            ret_col = f"r_{side}_friction"
            daily_ret_fraction = equity_df[ret_col] / 100.0
            
            # Compounded equity for leg
            leg_eq = initial_capital
            leg_curve = []
            for r_val in equity_df[ret_col]:
                leg_eq *= (1.0 + r_val / 100.0)
                leg_curve.append(leg_eq)
            
            leg_curve = np.array(leg_curve)
            leg_cagr = (leg_eq / initial_capital) ** (1.0 / years) - 1.0 if leg_eq > 0 else -1.0
            
            leg_mean_ret = daily_ret_fraction.mean()
            leg_std_ret = daily_ret_fraction.std(ddof=1)
            leg_sharpe = np.sqrt(252) * (leg_mean_ret / leg_std_ret) if leg_std_ret > 0 else 0.0
            
            leg_neg_ret = np.where(daily_ret_fraction < 0, daily_ret_fraction, 0.0)
            leg_downside_std = np.std(leg_neg_ret, ddof=1)
            leg_sortino = np.sqrt(252) * (leg_mean_ret / leg_downside_std) if leg_downside_std > 0 else 0.0
            
            leg_cum_max = np.maximum.accumulate(leg_curve)
            leg_drawdowns = (leg_curve - leg_cum_max) / leg_cum_max
            leg_max_dd = leg_drawdowns.min() if len(leg_drawdowns) > 0 else 0.0
            
            metrics[f"{side}_leg"] = {
                "cagr": leg_cagr,
                "sharpe": leg_sharpe,
                "sortino": leg_sortino,
                "max_drawdown": leg_max_dd,
                "avg_return": equity_df[ret_col].mean()
            }

        # 2. Regime Attribution (using friction profile)
        regimes = sorted(equity_df["market_regime"].unique())
        regime_attr = {}
        for reg in regimes:
            reg_df = equity_df[equity_df["market_regime"] == reg]
            n_days = len(reg_df)
            if n_days > 0:
                ret_fraction = reg_df["daily_return_friction"] / 100.0
                mean_ret = ret_fraction.mean()
                std_ret = ret_fraction.std(ddof=1)
                sharpe = np.sqrt(252) * (mean_ret / std_ret) if std_ret > 0 else 0.0
                hit_rate = (reg_df["daily_return_friction"] > 0).mean()
                regime_attr[reg] = {
                    "days": n_days,
                    "avg_return": reg_df["daily_return_friction"].mean(),
                    "sharpe": sharpe,
                    "hit_rate": hit_rate
                }
        metrics["regime_attribution"] = regime_attr

        # 3. Epoch Attribution (using friction profile)
        epochs = sorted(equity_df["epoch"].unique())
        epoch_attr = {}
        for ep in epochs:
            ep_df = equity_df[equity_df["epoch"] == ep]
            n_days = len(ep_df)
            if n_days > 0:
                # Compounded return within the epoch
                ep_capital = initial_capital
                ep_curve = []
                for r_val in ep_df["daily_return_friction"]:
                    ep_capital *= (1.0 + r_val / 100.0)
                    ep_curve.append(ep_capital)
                ep_curve = np.array(ep_curve)
                ep_return = (ep_capital - initial_capital) / initial_capital
                
                ret_fraction = ep_df["daily_return_friction"] / 100.0
                mean_ret = ret_fraction.mean()
                std_ret = ret_fraction.std(ddof=1)
                sharpe = np.sqrt(252) * (mean_ret / std_ret) if std_ret > 0 else 0.0
                
                ep_cum_max = np.maximum.accumulate(ep_curve)
                ep_drawdowns = (ep_curve - ep_cum_max) / ep_cum_max
                ep_max_dd = ep_drawdowns.min() if len(ep_drawdowns) > 0 else 0.0
                
                hit_rate = (ep_df["daily_return_friction"] > 0).mean()
                epoch_attr[ep] = {
                    "days": n_days,
                    "epoch_return": ep_return,
                    "sharpe": sharpe,
                    "max_drawdown": ep_max_dd,
                    "hit_rate": hit_rate
                }
        metrics["epoch_attribution"] = epoch_attr

        return metrics

    def run_grid_scan(self) -> list[dict]:
        """Runs a param sweep across horizons, features, weights, and L/S modes."""
        print("[*] Running comparative grid scan (24 parameter combinations)...")
        features = ["gap_pct", "overnight_spy_relative_strength"]
        horizons = [15, 30, 60]
        modes = [False, True]  # False = Long/Short, True = Long-Only
        weights = [False, True]  # False = Equal, True = Vol-Scaled
        
        scan_results = []
        
        for feat in features:
            for hor in horizons:
                for lo in modes:
                    for vs in weights:
                        try:
                            eq_df, tr_df = self.run_simulation(
                                feature=feat,
                                horizon=hor,
                                bucket_type="quintile",
                                long_only=lo,
                                vol_scaled=vs
                            )
                            mets = self.calculate_metrics(eq_df, tr_df)
                            if "friction" in mets:
                                scan_results.append({
                                    "feature": feat,
                                    "horizon": hor,
                                    "long_only": lo,
                                    "vol_scaled": vs,
                                    "cagr": mets["friction"]["cagr"],
                                    "sharpe": mets["friction"]["sharpe"],
                                    "max_dd": mets["friction"]["max_drawdown"],
                                    "turnover": mets["avg_turnover"]
                                })
                        except Exception as e:
                            print(f"[!] Error in grid run: {e}")
                            
        return scan_results

    def generate_report(self, default_metrics: dict, grid_results: list[dict]):
        """Generates out/portfolio_summary_report.md containing all metrics and attributions."""
        os.makedirs(self.out_dir, exist_ok=True)
        report_path = os.path.join(self.out_dir, "portfolio_summary_report.md")

        # Table 1: Core Friction Profile Metrics Comparison
        profile_data = []
        for prof in ["raw", "friction", "worst"]:
            m = default_metrics[prof]
            profile_data.append([
                prof.upper(),
                f"${m['final_equity']:,.2f}",
                f"{m['cagr'] * 100:.2f}%",
                f"{m['sharpe']:.3f}",
                f"{m['sortino']:.3f}",
                f"{m['max_drawdown'] * 100:.2f}%",
                f"{m['hit_rate'] * 100:.1f}%",
                f"{m['profit_factor']:.2f}",
                f"{m['expectancy']:.4f}%"
            ])
        profile_headers = ["Profile", "Ending Equity", "CAGR", "Sharpe", "Sortino", "Max DD", "Hit Rate", "Profit Factor", "Expectancy"]
        profile_table = tabulate(profile_data, headers=profile_headers, tablefmt="github")

        # Table 2: Long vs Short Attribution
        leg_data = []
        for leg in ["long_leg", "short_leg"]:
            m = default_metrics[leg]
            leg_data.append([
                leg.replace("_", " ").upper(),
                f"{m['avg_return']:.4f}%",
                f"{m['cagr'] * 100:.2f}%",
                f"{m['sharpe']:.3f}",
                f"{m['sortino']:.3f}",
                f"{m['max_drawdown'] * 100:.2f}%"
            ])
        leg_headers = ["Leg", "Avg Daily Return", "CAGR", "Sharpe", "Sortino", "Max DD"]
        leg_table = tabulate(leg_data, headers=leg_headers, tablefmt="github")

        # Table 3: Regime Attribution
        reg_data = []
        for reg, m in default_metrics["regime_attribution"].items():
            reg_data.append([
                reg,
                m["days"],
                f"{m["avg_return"]:.4f}%",
                f"{m["sharpe"]:.3f}",
                f"{m["hit_rate"] * 100:.1f}%"
            ])
        reg_headers = ["Market Regime", "Days", "Avg Daily Return", "Sharpe", "Hit Rate"]
        reg_table = tabulate(reg_data, headers=reg_headers, tablefmt="github")

        # Table 4: Epoch Attribution
        ep_data = []
        for ep, m in default_metrics["epoch_attribution"].items():
            ep_data.append([
                ep,
                m["days"],
                f"{m["epoch_return"] * 100:.2f}%",
                f"{m["sharpe"]:.3f}",
                f"{m["max_drawdown"] * 100:.2f}%",
                f"{m["hit_rate"] * 100:.1f}%"
            ])
        ep_headers = ["Epoch Name", "Days", "Epoch Return", "Sharpe", "Max DD", "Hit Rate"]
        ep_table = tabulate(ep_data, headers=ep_headers, tablefmt="github")

        # Table 5: Parameter Grid Scan
        grid_data = []
        for r in grid_results:
            grid_data.append([
                r["feature"],
                f"{r['horizon']}m",
                "Long-Only" if r["long_only"] else "Long/Short",
                "Vol-Scaled" if r["vol_scaled"] else "Equal-Weight",
                f"{r['cagr'] * 100:.2f}%",
                f"{r['sharpe']:.3f}",
                f"{r['max_dd'] * 100:.2f}%",
                f"{r['turnover']:.2f}"
            ])
        grid_headers = ["Feature", "Horizon", "Mode", "Weighting", "CAGR (Friction)", "Sharpe (Friction)", "Max DD", "Turnover"]
        grid_table = tabulate(grid_data, headers=grid_headers, tablefmt="github")

        # Calculate exact drag
        drag_cagr = default_metrics["raw"]["cagr"] - default_metrics["friction"]["cagr"]
        drag_sharpe = default_metrics["raw"]["sharpe"] - default_metrics["friction"]["sharpe"]

        report_content = f"""# Cross-Sectional Portfolio Constructor Simulation Report

Generated on: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## Executive Summary
This report analyzes the performance of a deterministic **Cross-Sectional Portfolio Constructor** that transforms daily relative ranking signals (computed strictly at **09:35 ET**) into simulated long/short portfolios.
The default baseline configuration evaluated is:
- **Feature**: `gap_pct`
- **Holding Horizon**: 60 minutes (exit at **10:35 ET**)
- **Bucketing**: Quintile
- **Leverage/Mode**: Long/Short (Market-Neutral: Long weights = +1.0, Short weights = -1.0)
- **Weighting**: Volatility-Scaled (ATR% denominator, 20th percentile floor, 12.5% max single-name weight cap)
- **Compounding**: Compounded daily returns ($1,000,000 baseline)

---

## 1. Friction & Execution Profile Performance
Comparative performance across **Raw**, **Friction-Adjusted** (1m execution delay, 1.5 bps fee each way, and volatility-scaled slippage), and **Worst-Case** outcomes:

{profile_table}

### Friction Drag Analysis
- **CAGR Drag**: **{drag_cagr * 100:.2f}%** (Raw CAGR of {default_metrics['raw']['cagr'] * 100:.2f}% reduced to {default_metrics['friction']['cagr'] * 100:.2f}% under realistic friction).
- **Sharpe Drag**: **{drag_sharpe:.3f}** (Raw Sharpe of {default_metrics['raw']['sharpe']:.3f} reduced to {default_metrics['friction']['sharpe']:.3f}).
- **Average Daily Turnover**: **{default_metrics['avg_turnover']:.2f}** (represents total gross weight traded daily).

---

## 2. Leg-Level Attribution (Friction-Adjusted)
Decomposition of return contribution between the **Long Leg** and **Short Leg**:

{leg_table}

*Note: For the Long/Short portfolio, the short leg return is additive to the long leg. A positive short leg CAGR indicates that shorted assets depreciated relative to entry on average, contributing positively to market-neutral returns.*

---

## 3. Market Regime & Historical Epoch Attribution
Attribution of the friction-adjusted portfolio returns across various market regimes and historical epochs:

### Performance by Daily Market Regime
{reg_table}

### Performance by Historical Epoch
{ep_table}

---

## 4. Parameter Sensitivity Grid Scan
Friction-adjusted portfolio performance across all 24 parameters:

{grid_table}

---

## 5. Diagnostic Conclusions
1. **Friction Resilience**: This report confirms if the relative return edge survives realistic execution drag. 
2. **Vol Scaling & Concentration Risk**: Volatility scaling using ATR% and single-name weight capping ensures risk is distributed evenly across assets, preventing low-priced or low-volatility names from dominating the portfolio.
3. **Decile Occupancy Check**: Automatically falls back to quintiles on days when ticker density is below the statistical threshold (10 tickers), preventing empty-bucket noise.
"""
        with open(report_path, "w") as f:
            f.write(report_content)
            
        print(f"[+] Generated final portfolio report at '{report_path}'.")

    def run_analysis(self) -> dict:
        """Runs the default analysis and comparative sweeps, then saves all report assets."""
        print("[*] Starting Cross-Sectional Portfolio Constructor simulation...")
        
        # 1. Run default configuration simulation
        equity_df, trades_df = self.run_simulation(
            feature="gap_pct",
            horizon=60,
            bucket_type="quintile",
            long_only=False,
            vol_scaled=True,
            initial_capital=1000000.0,
            max_weight_cap=0.125
        )
        
        # Save baseline output curves
        equity_df.to_csv(os.path.join(self.out_dir, "portfolio_equity_curve.csv"), index=False)
        trades_df.to_csv(os.path.join(self.out_dir, "portfolio_trade_log.csv"), index=False)
        print(f"[+] Saved baseline curves to '{self.out_dir}/portfolio_equity_curve.csv'")
        print(f"[+] Saved baseline trade logs to '{self.out_dir}/portfolio_trade_log.csv'")
        
        # 2. Calculate metrics
        default_metrics = self.calculate_metrics(equity_df, trades_df)
        
        # 3. Run grid parameter sweep
        grid_results = self.run_grid_scan()
        
        # 4. Write summary report
        self.generate_report(default_metrics, grid_results)
        
        print("[+] Cross-Sectional Portfolio Constructor simulation run complete.")
        return default_metrics

if __name__ == "__main__":
    constructor = CrossSectionalPortfolioConstructor()
    constructor.run_analysis()
