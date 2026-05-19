import os
import json

class RiskReviewer:
    def __init__(self):
        # Load dynamic sizing parameters from config/risk_params.json
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "risk_params.json")
        with open(config_path, "r") as f:
            params = json.load(f)
        self.risk_budget_pct = params.get("risk_budget_pct", 0.02)  # 2% of equity risk per trade
        self.stop_atr_multiplier = params.get("stop_atr_multiplier", 3.5)  # ATR multiplier for stop distance
        self.break_even_ratio = params.get("break_even_ratio", 0.5)
        self.take_profit_ratio = params.get("take_profit_ratio", 1.2)
        self.max_position_pct = params.get("max_position_pct", 0.25)  # Hard cap per position
        self.confidence_threshold = params.get("confidence_threshold", 0.4)
        self.transaction_fee_bps = params.get("transaction_fee_bps", 1.5)
        self.transaction_fee_rate = self.transaction_fee_bps / 10000.0
        self.temporal_persistence_bars = params.get("temporal_persistence_bars", 3)
        self.break_even_atr_multiplier = params.get("break_even_atr_multiplier", 1.5)
        self.convexity_capture_atr_multiplier = params.get("convexity_capture_atr_multiplier", 5.0)
        self.max_notional_leverage_multiplier = params.get("max_notional_leverage_multiplier", 1.0)
        self.daily_drawdown_limit = -params.get("daily_drawdown_limit", 5.0)
        self.non_stationarity_threshold = params.get("non_stationarity_threshold", 0.0)
        self.non_stationarity_window_limit = params.get("non_stationarity_window_limit", 3)
        self.max_exposure_decay_per_bar = params.get("max_exposure_decay_per_bar", 0.15)
        self.critical_value = params.get("critical_value", 0.0)
        self.scale_factor = params.get("scale_factor", 2.0)
        self.current_drawdown = 0.0  # Mock state

    def _calculate_atr(self, highs, lows, prev_closes):
        true_ranges = []
        for h, l, pc in zip(highs, lows, prev_closes):
            tr = max(h - l, abs(h - pc), abs(l - pc))
            true_ranges.append(tr)
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def calculate_position_size(self, account_value: float, current_price: float, atr: float):
        """Phase 35 Momentum Sizing Engine
        Returns a dict with max allowable position size based on strict 2% account risk and ATR stop distance.
        """
        # 1. Absolute risk budget (USD)
        risk_budget = account_value * self.risk_budget_pct
        
        # 2. Risk per share (USD) based on ATR trailing stop distance
        stop_distance = self.stop_atr_multiplier * atr
        
        if stop_distance <= 0 or current_price <= 0:
            return {"position_size_usd": 0.0, "shares": 0}
            
        # 3. Maximum shares we can buy without risking more than the budget
        max_shares_risk = risk_budget / stop_distance
        
        # 4. Convert to dollar allocation using current price
        base_allocation_usd = max_shares_risk * current_price
        
        # 5. Hard cap - do not allocate more than max_position_pct of entire account
        hard_cap_usd = account_value * self.max_position_pct
        final_allocation_usd = min(base_allocation_usd, hard_cap_usd)
        
        final_shares = int(final_allocation_usd / current_price)
        
        return {
            "position_size_usd": final_allocation_usd,
            "shares": final_shares,
            "metadata": {
                "risk_budget": risk_budget,
                "stop_distance": stop_distance,
                "hard_cap_usd": hard_cap_usd
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
