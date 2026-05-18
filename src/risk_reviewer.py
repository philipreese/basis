class RiskReviewer:
    def __init__(self):
        self.max_position_size = 25.0
        self.daily_drawdown_limit = -100.0
        self.current_drawdown = 0.0 # Mock state
        
    def _calculate_atr(self, highs, lows, prev_closes):
        true_ranges = []
        for h, l, pc in zip(highs, lows, prev_closes):
            tr = max(h - l, abs(h - pc), abs(l - pc))
            true_ranges.append(tr)
            
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def evaluate_proposal(self, proposal: dict):
        action = proposal.get("suggested_action", "Hold")
        if action == "Hold":
            return {
                "review_status": "APPROVED",
                "objection_severity": "LOW",
                "counter_argument": "No action proposed, risk is zero.",
                "max_allowed_size": 0.0,
                "atr_14": 0.0
            }
            
        if self.current_drawdown <= self.daily_drawdown_limit:
            return {
                "review_status": "REJECTED",
                "objection_severity": "HIGH",
                "counter_argument": "Daily drawdown limit breached. No trading allowed.",
                "max_allowed_size": 0.0,
                "atr_14": 0.0
            }
            
        metrics = proposal.get("metrics", {})
        hist_data = proposal.get("historical_data", {})
        
        current_price = metrics.get("current_price", 1)
        current_vol = metrics.get("current_volume", 0)
        sma_20 = metrics.get("sma_20", 0)
        vol_sma_20 = metrics.get("vol_sma_20", 0)
        
        highs = hist_data.get("high", [])
        lows = hist_data.get("low", [])
        prev_closes = hist_data.get("prev_close", [])
        
        atr_14 = self._calculate_atr(highs, lows, prev_closes)
        
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
            
        return {
            "review_status": "REVIEWED",
            "objection_severity": severity,
            "counter_argument": counter,
            "max_allowed_size": self.max_position_size,
            "atr_14": atr_14
        }
