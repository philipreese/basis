import random

class RiskReviewer:
    def __init__(self):
        self.max_position_size = 25.0
        self.daily_drawdown_limit = -100.0
        self.current_drawdown = 0.0 # Mock state
        
    def evaluate_proposal(self, proposal: dict):
        """
        Intercepts proposal. Enforces $25 max. Validates drawdown.
        Generates a skeptical counter-argument and assigns severity.
        """
        action = proposal.get("suggested_action", "Hold")
        if action == "Hold":
            return {
                "review_status": "APPROVED",
                "objection_severity": "LOW",
                "counter_argument": "No action proposed, risk is zero.",
                "max_allowed_size": 0.0
            }
            
        # Mock Drawdown Check
        if self.current_drawdown <= self.daily_drawdown_limit:
            return {
                "review_status": "REJECTED",
                "objection_severity": "HIGH",
                "counter_argument": "Daily drawdown limit breached. No trading allowed.",
                "max_allowed_size": 0.0
            }
            
        # Generate skeptical counter-argument (Deterministic mock logic)
        metrics = proposal.get("metrics", {})
        sma_5 = metrics.get("sma_5", 0)
        sma_20 = metrics.get("sma_20", 0)
        gap = abs(sma_5 - sma_20)
        current_price = metrics.get("current_price", 1)
        
        gap_percentage = (gap / current_price) * 100
        
        # Severity assignment based on mock indicator lag/trend fatigue
        if gap_percentage < 0.1:
            severity = "HIGH"
            counter = "Indicator lag: SMA convergence is too tight, highly susceptible to whipsawing."
        elif gap_percentage > 2.0:
            severity = "MEDIUM"
            counter = "Trend fatigue: Asset has extended too far past the mean, mean reversion likely."
        else:
            # Randomize between LOW and MEDIUM for demonstration if in normal range
            # To keep it completely deterministic without state, we can use the timestamp hash
            ts_hash = sum(ord(c) for c in proposal.get("timestamp", ""))
            if ts_hash % 2 == 0:
                severity = "LOW"
                counter = "Volume appears stable, but macro headwinds could still invalidate the setup."
            else:
                severity = "MEDIUM"
                counter = "Low volume confirmation. The moving average crossover lacks institutional backing."
                
        return {
            "review_status": "REVIEWED",
            "objection_severity": severity,
            "counter_argument": counter,
            "max_allowed_size": self.max_position_size
        }
