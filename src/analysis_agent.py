import os
import json
import math
from datetime import datetime, timedelta
import zoneinfo
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .state_validator import StateValidationError, validate_transition

class AnalysisAgent:
    def __init__(self, api_key: str, secret_key: str):
        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'out')
        self.config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
        os.makedirs(self.out_dir, exist_ok=True)
        self.state_file = os.path.join(self.out_dir, 'state.json')
        self._init_state()
        self._load_config()

    def _load_config(self):
        assets_path = os.path.join(self.config_dir, 'assets.json')
        archetypes_path = os.path.join(self.config_dir, 'archetypes.json')
        
        with open(assets_path, 'r') as f:
            self.assets = json.load(f)
            
        with open(archetypes_path, 'r') as f:
            self.archetypes = json.load(f)
            
    def resolve_asset_parameters(self, symbol: str) -> dict:
        archetype_key = self.assets.get(symbol, "BROAD_MARKET_MEAN_REVERSION")
        return self.archetypes.get(archetype_key, {
            "volatility_gate_limit": 0.035,
            "fatigue_multiplier": -0.1
        })

    def _init_state(self):
        if not os.path.exists(self.state_file):
            initial_state = {
                "SPY": {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None, "alpha_t": 0.5, "boundary_pressure_t": 0.5},
                "QQQ": {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None, "alpha_t": 0.5, "boundary_pressure_t": 0.5}
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
        state = {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None, "alpha_t": 0.5, "boundary_pressure_t": 0.5}
        for i in range(20, len(symbol_bars) + 1):
            current_bar = symbol_bars[i-1]
            # Retrieve Volume-Energy attributes
            curr_vwap = getattr(current_bar, "vwap", current_bar.close)
            curr_obv = getattr(current_bar, "obv", 0.0)
            curr_obv_sma20 = getattr(current_bar, "obv_sma20", 0.0)
            
            # Buy Conditions
            cond1_buy = current_bar.close > curr_vwap
            cond2_buy = curr_obv > curr_obv_sma20
            buy_conditions_met = sum([cond1_buy, cond2_buy])
            
            # Sell Conditions
            cond1_sell = current_bar.close < curr_vwap
            cond2_sell = curr_obv < curr_obv_sma20
            sell_conditions_met = sum([cond1_sell, cond2_sell])
            
            sma_macro = getattr(current_bar, "sma_macro", current_bar.close)
            is_macro_bull = current_bar.close > sma_macro
            
            regime = "Congestion"
            if is_macro_bull:
                if buy_conditions_met == 2:
                    regime = "Bull"
            else:
                if sell_conditions_met == 2:
                    regime = "Bear"
                    
            # Apply Volatility Gate
            if i >= 15:
                hist_high = [b.high for b in symbol_bars[i-14:i]]
                hist_low = [b.low for b in symbol_bars[i-14:i]]
                hist_close = [b.close for b in symbol_bars[i-15:i-1]]
                atr_14 = self._calculate_atr(hist_high, hist_low, hist_close)
                params = self.resolve_asset_parameters(symbol)
                vol_gate_limit = params["volatility_gate_limit"]
                if atr_14 > (vol_gate_limit * current_bar.close):
                    regime = "Congestion"
                    
            # Apply Temporal Gate
            local_dt = current_bar.timestamp.astimezone(zoneinfo.ZoneInfo('America/New_York'))
            decimal_hour = local_dt.hour + local_dt.minute / 60.0
            if decimal_hour < 11.5:
                regime = "Congestion"
            
            uid = f"{symbol}_{current_bar.timestamp.isoformat()}_{current_bar.trade_count}"
            if state["last_unique_id"] != uid:
                if state["previous_market_regime"] == regime:
                    state["bars_in_trend_count"] += 1
                else:
                    state["bars_in_trend_count"] = 1
                    state["previous_market_regime"] = regime
                state["last_unique_id"] = uid
        return state
        
    def generate_proposals(self, symbols: list, override_bars: dict = None):
        if override_bars is not None:
            class MockBarsResponse:
                def __init__(self, data):
                    self.data = data
            bars = MockBarsResponse(override_bars)
        else:
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
            if len(symbol_bars) < 130:
                print(f"Analysis Agent: Not enough data for {symbol}")
                continue
                
            closes = [bar.close for bar in symbol_bars]
            volumes = [bar.volume for bar in symbol_bars]
            
            # Base variables
            current_price = closes[-1]
            current_vol = volumes[-1]
            vol_sma_20 = sum(volumes[-20:]) / 20
            sma_5 = sum(closes[-5:]) / 5
            sma_20 = sum(closes[-20:]) / 20
            
            hist_high = [bar.high for bar in symbol_bars[-14:]]
            hist_low = [bar.low for bar in symbol_bars[-14:]]
            hist_close = [bar.close for bar in symbol_bars[-15:-1]]
            
            atr_14 = self._calculate_atr(hist_high, hist_low, hist_close)
            
            params = self.resolve_asset_parameters(symbol)
            vol_gate_limit = params["volatility_gate_limit"]
            
            # 1. Macro Proximity Metric
            current_bar = symbol_bars[-1]
            sma_macro = getattr(current_bar, "sma_macro", current_price)
            safe_atr = max(atr_14, 0.0001)
            proximity_distance = abs(current_price - sma_macro) / safe_atr
            
            # 2. Smooth Transition Function
            try:
                alpha_current = 1.0 / (1.0 + math.exp(3.0 * (proximity_distance - 1.5)))
            except OverflowError:
                alpha_current = 0.0 if (proximity_distance - 1.5) > 0 else 1.0
                
            alpha_current = max(0.05, min(0.95, alpha_current))
            
            # 3. Smooth Temporal Inertia & Hysteresis
            symbol_state = state.get(symbol, {"previous_market_regime": None, "bars_in_trend_count": 0, "last_unique_id": None, "alpha_t": 0.5, "boundary_pressure_t": 0.5})
            prev_alpha_t = symbol_state.get("alpha_t", 0.5)
            prev_boundary_pressure = symbol_state.get("boundary_pressure_t", alpha_current)
            
            current_bar_ts = current_bar.timestamp.isoformat()
            trade_count = current_bar.trade_count
            unique_id = f"{symbol}_{current_bar_ts}_{trade_count}"
            
            is_new_bar = (symbol_state.get("last_unique_id") != unique_id)
            if is_new_bar:
                boundary_pressure_t = 0.85 * prev_boundary_pressure + 0.15 * alpha_current
                alpha_t = 0.9 * prev_alpha_t + 0.1 * alpha_current
            else:
                boundary_pressure_t = prev_boundary_pressure
                alpha_t = prev_alpha_t
                
            boundary_pressure_t = max(0.05, min(0.95, boundary_pressure_t))
            alpha_t = max(0.05, min(0.95, alpha_t))
            
            prev_regime = symbol_state.get("previous_market_regime")
            if prev_regime is None:
                prev_regime = "None"
                
            is_entry_suppressed = False
            
            # 4. Institutional Synergy Matrix
            curr_vwap = getattr(current_bar, "vwap", current_price)
            curr_obv = getattr(current_bar, "obv", 0.0)
            curr_obv_sma20 = getattr(current_bar, "obv_sma20", 0.0)
            
            # Buy Conditions
            cond1_buy = current_price > curr_vwap
            cond2_buy = curr_obv > curr_obv_sma20
            buy_conditions_met = sum([cond1_buy, cond2_buy])
            
            # Sell Conditions
            cond1_sell = current_price < curr_vwap
            cond2_sell = curr_obv < curr_obv_sma20
            sell_conditions_met = sum([cond1_sell, cond2_sell])
            
            is_macro_bull = current_price > sma_macro
            
            action = "Hold"
            current_regime = "Congestion"
            conditions_met_count = 0
            
            if is_macro_bull:
                if buy_conditions_met == 2:
                    action = "Buy"
                    current_regime = "Bull"
                    conditions_met_count = buy_conditions_met
            else:
                if sell_conditions_met == 2:
                    action = "Sell"
                    current_regime = "Bear"
                    conditions_met_count = sell_conditions_met
                    
            # Apply Volatility Gate
            if atr_14 > (vol_gate_limit * current_price):
                action = "Hold"
                current_regime = "Congestion"
                conditions_met_count = 0
                
            # Apply Temporal Gate
            local_dt = current_bar.timestamp.astimezone(zoneinfo.ZoneInfo('America/New_York'))
            decimal_hour = local_dt.hour + local_dt.minute / 60.0
            if decimal_hour < 11.5:
                action = "Hold"
                current_regime = "Congestion"
                conditions_met_count = 0
            
            validator_status = "PASSED"
            if is_new_bar:
                # New bar detected
                proposed_state = symbol_state.copy()
                if proposed_state.get("previous_market_regime") == current_regime:
                    proposed_state["bars_in_trend_count"] = proposed_state.get("bars_in_trend_count", 0) + 1
                else:
                    proposed_state["bars_in_trend_count"] = 1
                    proposed_state["previous_market_regime"] = current_regime
                    
                proposed_state["last_unique_id"] = unique_id
                proposed_state["alpha_t"] = alpha_t
                proposed_state["boundary_pressure_t"] = boundary_pressure_t
                
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
                    symbol_state["alpha_t"] = alpha_t
                    symbol_state["boundary_pressure_t"] = boundary_pressure_t
                    validator_status = "REBUILT"
                    
                state[symbol] = symbol_state
            else:
                validator_status = "PASSED (Duplicate)"
                
            trend_count = symbol_state["bars_in_trend_count"]
                
            # Confidence Engine
            if action == "Hold":
                base_confidence = 0.0
                trend_maturity_modifier = 0.0
                trend_alignment = 0.0
            else:
                base_confidence = min(1.0, 0.4 + (abs(current_price - curr_vwap) / curr_vwap) * 100.0)
                
                # Trend Maturity Modifier for logging / analytics
                trend_alignment = 0.5
                safe_atr = max(atr_14, 0.1)
                dynamic_fatigue_threshold = max(3, int(20 / safe_atr))
                
                if trend_count in (1, 2):
                    trend_maturity_modifier = 0.1
                elif trend_count > dynamic_fatigue_threshold:
                    penalty_steps = trend_count - dynamic_fatigue_threshold
                    trend_maturity_modifier = max(-0.3, params["fatigue_multiplier"] * penalty_steps)
                else:
                    trend_maturity_modifier = 0.0
                
            confidence_factors = {
                "trend_alignment": round(trend_alignment, 2),
                "trend_maturity_modifier": round(trend_maturity_modifier, 2),
                "conditions_met_count": conditions_met_count,
                "is_macro_bull": int(is_macro_bull)
            }
            
            # Transition Trajectory Logging
            dynamic_fatigue_limit = int(20 / safe_atr)
            fatigue_ratio = round(trend_count / dynamic_fatigue_limit, 4)
            
            state_telemetry = {
                "transition_trajectory": f"{prev_regime} -> {current_regime}",
                "dynamic_fatigue_limit": dynamic_fatigue_limit,
                "fatigue_ratio": fatigue_ratio,
                "alpha_t": round(alpha_t, 4),
                "boundary_pressure_t": round(boundary_pressure_t, 4)
            }
                
            proposal = {
                "timestamp": current_bar_ts,
                "symbol": symbol,
                "data_provenance": getattr(current_bar, "data_provenance", {}),
                "metrics": {
                    "current_price": current_price,
                    "current_volume": current_vol,
                    "sma_5": sma_5,
                    "sma_20": sma_20,
                    "vol_sma_20": vol_sma_20,
                    "atr_14": atr_14
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
