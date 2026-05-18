import os
from datetime import datetime, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

class AnalysisAgent:
    def __init__(self, api_key: str, secret_key: str):
        self.client = StockHistoricalDataClient(api_key, secret_key)

    def _calculate_atr(self, highs, lows, prev_closes):
        true_ranges = []
        for h, l, pc in zip(highs, lows, prev_closes):
            tr = max(h - l, abs(h - pc), abs(l - pc))
            true_ranges.append(tr)
            
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
        
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
            
            if sma_5 > sma_20:
                action = "Buy"
            elif sma_5 < sma_20:
                action = "Sell"
            else:
                action = "Hold"
                
            # Confidence Engine
            trend_alignment = 0.5 if action != "Hold" else 0.0
            volume_confirmation = 0.2 if current_vol > vol_sma_20 else -0.1
            
            price_diff = abs(current_price - sma_20)
            if atr_14 > 0:
                penalty_steps = int(price_diff / (0.5 * atr_14))
                atr_penalty = max(-0.4, -0.1 * penalty_steps)
            else:
                atr_penalty = 0.0
                
            confidence_factors = {
                "trend_alignment": round(trend_alignment, 2),
                "volume_confirmation": round(volume_confirmation, 2),
                "atr_penalty": round(atr_penalty, 2)
            }
            
            base_confidence = max(0.0, sum(confidence_factors.values()))
                
            proposal = {
                "timestamp": symbol_bars[-1].timestamp.isoformat(),
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
                "suggested_action": action,
                "base_confidence": round(base_confidence, 2),
                "confidence_factors": confidence_factors
            }
            proposals.append(proposal)
            
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
