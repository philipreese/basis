import os
import json
from datetime import datetime, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .state_validator import StateValidationError, validate_transition

class AnalysisAgent:
    def __init__(self, api_key: str, secret_key: str):
        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'out')
        os.makedirs(self.out_dir, exist_ok=True)
        self.state_file = os.path.join(self.out_dir, 'state.json')
        self._init_state()

    def _init_state(self):
        if not os.path.exists(self.state_file):
            initial_state = {
                "SPY": {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None},
                "QQQ": {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None}
            }
            with open(self.state_file, 'w') as f:
                json.dump(initial_state, f)

    def _load_state(self):
        with open(self.state_file, 'r') as f:
            return json.load(f)

    def _save_state(self, state):
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=4)

    def _calculate_atr(self, highs, lows, prev_closes):
        true_ranges = []
        for h, l, pc in zip(highs, lows, prev_closes):
            tr = max(h - l, abs(h - pc), abs(l - pc))
            true_ranges.append(tr)
            
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def _rebuild_ledger(self, symbol, symbol_bars):
        """Sequential fallback to rebuild ledger from scratch based on historical bars."""
        state = {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None}
        for i in range(20, len(symbol_bars) + 1):
            window = symbol_bars[i-20:i]
            closes = [b.close for b in window]
            sma_5 = sum(closes[-5:]) / 5
            sma_20 = sum(closes[-20:]) / 20
            price = closes[-1]
            
            if abs(sma_5 - sma_20) < (0.0005 * price):
                regime = "Congestion"
            elif sma_5 > sma_20:
                regime = "Bull"
            else:
                regime = "Bear"
                
            current_bar = window[-1]
            uid = f"{symbol}_{current_bar.timestamp.isoformat()}_{current_bar.trade_count}"
            if state["last_unique_id"] != uid:
                if state["previous_market_regime"] == regime:
                    state["bars_in_trend_count"] += 1
                else:
                    state["bars_in_trend_count"] = 1
                    state["previous_market_regime"] = regime
                state["last_unique_id"] = uid
        return state
        
    def generate_proposals(self, symbols: list):
        end_time = datetime.now()
        start_time = end_time - timedelta(days=5)
        
        request_params = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
            end=end_time
        )
        
        bars = self.client.get_stock_bars(request_params)
        proposals = []
        state = self._load_state()
        
        for symbol in symbols:
            symbol_bars = bars.data.get(symbol, [])
            if len(symbol_bars) < 20:
                print(f"Analysis Agent: Not enough data for {symbol}")
                continue
                
            closes = [bar.close for bar in symbol_bars]
            volumes = [bar.volume for bar in symbol_bars]
            
            sma_5 = sum(closes[-5:]) / 5
            sma_20 = sum(closes[-20:]) / 20
            vol_sma_20 = sum(volumes[-20:]) / 20
            
            current_price = closes[-1]
            current_vol = volumes[-1]
            
            hist_high = [bar.high for bar in symbol_bars[-14:]]
            hist_low = [bar.low for bar in symbol_bars[-14:]]
            hist_close = [bar.close for bar in symbol_bars[-15:-1]]
            
            atr_14 = self._calculate_atr(hist_high, hist_low, hist_close)
            
            if abs(sma_5 - sma_20) < (0.0005 * current_price):
                action = "Hold"
                current_regime = "Congestion"
            elif sma_5 > sma_20:
                action = "Buy"
                current_regime = "Bull"
            else:
                action = "Sell"
                current_regime = "Bear"
                
            symbol_state = state.get(symbol, {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None})
            current_bar = symbol_bars[-1]
            current_bar_ts = current_bar.timestamp.isoformat()
            trade_count = current_bar.trade_count
            unique_id = f"{symbol}_{current_bar_ts}_{trade_count}"
            
            prev_regime = symbol_state.get("previous_market_regime")
            if prev_regime is None:
                prev_regime = "None"
            
            validator_status = "PASSED"
            if symbol_state.get("last_unique_id") != unique_id:
                # New bar detected
                proposed_state = symbol_state.copy()
                if proposed_state.get("previous_market_regime") == current_regime:
                    proposed_state["bars_in_trend_count"] = proposed_state.get("bars_in_trend_count", 0) + 1
                else:
                    proposed_state["bars_in_trend_count"] = 1
                    proposed_state["previous_market_regime"] = current_regime
                    
                proposed_state["last_unique_id"] = unique_id
                
                try:
                    validate_transition(symbol_state, proposed_state)
                    symbol_state = proposed_state
                except StateValidationError as e:
                    print(f"[{datetime.now()}] State Validation Error for {symbol}: {e}")
                    # Standalone self-healing audit log
                    corrupted_regime = symbol_state.get("previous_market_regime", "None") or "None"
                    corrupted_count = symbol_state.get("bars_in_trend_count", 0)
                    
                    healed_state = self._rebuild_ledger(symbol, symbol_bars)
                    healed_regime = healed_state.get("previous_market_regime", "None") or "None"
                    healed_count = healed_state.get("bars_in_trend_count", 0)
                    
                    audit_entry = {
                        "event_type": "STATE_RECONSTRUCTION",
                        "timestamp": datetime.now().isoformat(),
                        "symbol": symbol,
                        "failed_assertion": str(e),
                        "corrupted_state_snapshot": {
                            "REGIME": corrupted_regime,
                            "COUNT": corrupted_count
                        },
                        "healed_state_snapshot": {
                            "REGIME": healed_regime,
                            "COUNT": healed_count
                        }
                    }
                    
                    journal_file = os.path.join(self.out_dir, "trading_journal.jsonl")
                    try:
                        with open(journal_file, "a") as f:
                            f.write(json.dumps(audit_entry) + "\n")
                    except Exception as log_err:
                        print(f"Failed to write to journal: {log_err}")
                        
                    symbol_state = healed_state
                    validator_status = "REBUILT"
                    
                state[symbol] = symbol_state
            else:
                validator_status = "PASSED (Duplicate)"
                
            trend_count = symbol_state["bars_in_trend_count"]
                
            # Confidence Engine
            trend_alignment = 0.5 if action != "Hold" else 0.0
            volume_confirmation = 0.2 if current_vol > vol_sma_20 else -0.1
            
            price_diff = abs(current_price - sma_20)
            if atr_14 > 0:
                penalty_steps = int(price_diff / (0.5 * atr_14))
                atr_penalty = max(-0.4, -0.1 * penalty_steps)
            else:
                atr_penalty = 0.0
                
            # Volatility-Based Fatigue Calibration
            safe_atr = max(atr_14, 0.1)
            dynamic_fatigue_threshold = max(3, int(20 / safe_atr))
            
            # Trend Maturity Modifier
            if trend_count in (1, 2):
                trend_maturity_modifier = 0.1
            elif trend_count > dynamic_fatigue_threshold:
                penalty_steps = trend_count - dynamic_fatigue_threshold
                trend_maturity_modifier = max(-0.3, -0.1 * penalty_steps)
            else:
                trend_maturity_modifier = 0.0
                
            confidence_factors = {
                "trend_alignment": round(trend_alignment, 2),
                "volume_confirmation": round(volume_confirmation, 2),
                "atr_penalty": round(atr_penalty, 2),
                "trend_maturity_modifier": round(trend_maturity_modifier, 2)
            }
            
            base_confidence = max(0.0, sum(confidence_factors.values()))
            
            # Transition Trajectory Logging
            dynamic_fatigue_limit = int(20 / safe_atr)
            fatigue_ratio = round(trend_count / dynamic_fatigue_limit, 4)
            
            state_telemetry = {
                "transition_trajectory": f"{prev_regime} -> {current_regime}",
                "dynamic_fatigue_limit": dynamic_fatigue_limit,
                "fatigue_ratio": fatigue_ratio
            }
                
            proposal = {
                "timestamp": current_bar_ts,
                "symbol": symbol,
                "metrics": {
                    "current_price": current_price,
                    "current_volume": current_vol,
                    "sma_5": sma_5,
                    "sma_20": sma_20,
                    "vol_sma_20": vol_sma_20
                },
                "historical_data": {
                    "high": hist_high,
                    "low": hist_low,
                    "prev_close": hist_close
                },
                "market_regime": current_regime,
                "bars_in_trend_count": trend_count,
                "suggested_action": action,
                "base_confidence": round(base_confidence, 2),
                "confidence_factors": confidence_factors,
                "validator_status": validator_status,
                "state_telemetry": state_telemetry
            }
            proposals.append(proposal)
            
        self._save_state(state)
        return proposals
        
    def resolve_consensus(self, proposal: dict, review: dict):
        severity = review.get("objection_severity", "LOW")
        base_confidence = proposal["base_confidence"]
        
        if severity == "LOW":
            final_confidence = base_confidence
            position_modifier = 1.0
        elif severity == "MEDIUM":
            final_confidence = base_confidence * 0.7
            position_modifier = 0.5
        elif severity == "HIGH":
            final_confidence = base_confidence * 0.3
            position_modifier = 0.0
            if proposal["suggested_action"] != "Hold":
                proposal["suggested_action"] = "Hold (Vetoed)"
        else:
            final_confidence = base_confidence
            position_modifier = 1.0
            
        return {
            "final_action": proposal["suggested_action"],
            "final_confidence": round(final_confidence, 2),
            "position_modifier": position_modifier
        }
