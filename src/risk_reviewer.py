import os
import json

class RiskReviewer:
    def __init__(self):
        # Load dynamic sizing parameters from config/risk_params.json
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "risk_params.json")
        with open(config_path, "r") as f:
            params = json.load(f)
        self.risk_budget_pct = params.get("risk_budget_pct", 0.01)  # 1% of equity risk per trade
        self.stop_atr_multiplier = params.get("stop_atr_multiplier", 2.0)  # ATR multiplier for stop distance
        self.max_position_pct = params.get("max_position_pct", 0.25)  # Hard cap per position
        self.confidence_threshold = params.get("confidence_threshold", 0.4)
        self.transaction_fee_bps = params.get("transaction_fee_bps", 1.5)
        self.transaction_fee_rate = self.transaction_fee_bps / 10000.0
        self.temporal_persistence_bars = params.get("temporal_persistence_bars", 3)
        self.break_even_atr_multiplier = params.get("break_even_atr_multiplier", 1.5)
        self.convexity_capture_atr_multiplier = params.get("convexity_capture_atr_multiplier", 5.0)
        self.max_notional_leverage_multiplier = params.get("max_notional_leverage_multiplier", 1.0)
        self.daily_drawdown_limit = -100.0
        self.current_drawdown = 0.0  # Mock state

    def _calculate_atr(self, highs, lows, prev_closes):
        true_ranges = []
        for h, l, pc in zip(highs, lows, prev_closes):
            tr = max(h - l, abs(h - pc), abs(l - pc))
            true_ranges.append(tr)
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def calculate_position_size(self, equity: float, price: float, atr: float, confidence: float):
        """Hybrid sizing engine.
        Returns a dict with:
            position_size_usd – final allocation in USD (capped)
            shares – number of shares to buy
            metadata – intermediate values for audit
        """
        # Edge case: confidence below threshold -> no allocation
        if confidence <= self.confidence_threshold:
            return {
                "position_size_usd": 0.0,
                "shares": 0,
                "metadata": {
                    "reason": "confidence_below_threshold",
                    "confidence": confidence
                }
            }
        # 1. Risk budget (USD) – fraction of equity to risk per trade
        risk_budget = equity * self.risk_budget_pct
        # 2. Risk per share (USD) based on stop distance
        risk_per_share = self.stop_atr_multiplier * atr
        if risk_per_share <= 0:
            # Defensive fallback – allocate a minimal amount
            base_shares = 0
        else:
            base_shares = risk_budget / risk_per_share
        # Convert to dollar allocation using current price
        base_allocation_usd = base_shares * price
        # 3. Confidence gradient scaling (linear from threshold to 1.0)
        conf_min, conf_max = self.confidence_threshold, 1.0
        confidence_multiplier = (confidence - conf_min) / (conf_max - conf_min)
        scaled_allocation_usd = base_allocation_usd * confidence_multiplier
        # 4. Hard cap – max_position_pct of equity
        hard_cap_usd = equity * self.max_position_pct
        final_allocation_usd = min(scaled_allocation_usd, hard_cap_usd)
        # Final shares (may be fractional for simulation purposes)
        final_shares = final_allocation_usd / price if price > 0 else 0
        return {
            "position_size_usd": final_allocation_usd,
            "shares": final_shares,
            "metadata": {
                "risk_budget": risk_budget,
                "risk_per_share": risk_per_share,
                "base_shares": base_shares,
                "base_allocation_usd": base_allocation_usd,
                "confidence_multiplier": confidence_multiplier,
                "hard_cap_usd": hard_cap_usd,
                "confidence": confidence
            }
        }

    def evaluate_proposal(self, proposal: dict):
        action = proposal.get("suggested_action", "Hold")
        # Immediate hold case – no risk
        if action == "Hold":
            return {
                "review_status": "APPROVED",
                "objection_severity": "LOW",
                "counter_argument": "No action proposed, risk is zero.",
                "max_allowed_size": 0.0,
                "atr_14": 0.0
            }
        # Daily drawdown guard
        if self.current_drawdown <= self.daily_drawdown_limit:
            return {
                "review_status": "REJECTED",
                "objection_severity": "HIGH",
                "counter_argument": "Daily drawdown limit breached. No trading allowed.",
                "max_allowed_size": 0.0,
                "atr_14": 0.0
            }
        # Extract metrics for ATR calculation
        metrics = proposal.get("metrics", {})
        hist_data = proposal.get("historical_data", {})
        highs = hist_data.get("high", [])
        lows = hist_data.get("low", [])
        prev_closes = hist_data.get("prev_close", [])
        atr_14 = self._calculate_atr(highs, lows, prev_closes)
        # Basic risk flags
        current_price = metrics.get("current_price", 1)
        current_vol = metrics.get("current_volume", 0)
        sma_20 = metrics.get("sma_20", 0)
        vol_sma_20 = metrics.get("vol_sma_20", 0)
        price_diff = abs(current_price - sma_20)
        if price_diff > (2 * atr_14) and atr_14 > 0:
            severity = "HIGH"
            counter = "Price is overextended beyond 2 ATRs. High whipsaw risk."
        elif current_vol < vol_sma_20:
            severity = "MEDIUM"
            counter = f"Volume of {current_vol} is below the 20-bar average of {vol_sma_20:.2f}. Breakout lacks institutional backing."
        else:
            severity = "LOW"
            counter = "Volume and volatility within acceptable bounds."
        # Dynamic sizing – placeholder equity and price will be overridden by caller (simulator)
        size_info = self.calculate_position_size(
            equity=0.0,  # placeholder – simulator supplies real equity
            price=metrics.get("current_price", 0.0),
            atr=atr_14,
            confidence=proposal.get("base_confidence", 0.0)
        )
        return {
            "review_status": "REVIEWED",
            "objection_severity": severity,
            "counter_argument": counter,
            "max_allowed_size": size_info["position_size_usd"],
            "sizing_metadata": size_info["metadata"],
            "atr_14": atr_14
        }
