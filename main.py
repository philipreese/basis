import os
import json
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

def main():
    # Load environment variables
    load_dotenv()
    
    api_key = os.getenv("ALPACA_API_KEY_ID")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    
    # Initialize Historical Data Client (SAFE: No trading endpoints)
    client = StockHistoricalDataClient(api_key, secret_key)
    
    symbols = ["SPY", "QQQ"]
    
    print(f"[{datetime.now()}] Starting Alpaca data collection loop...")
    
    # Loop to simulate the data collection baseline
    for i in range(3): # Run 3 loops for demonstration
        try:
            # We want to make sure we have enough historical data for a 20-period 15-minute SMA.
            # 20 periods * 15 minutes = 300 minutes (5 hours) of trading time.
            # Requesting the last 5 days to ensure we bypass weekends/holidays.
            end_time = datetime.now()
            start_time = end_time - timedelta(days=5)
            
            request_params = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                start=start_time,
                end=end_time
            )
            
            bars = client.get_stock_bars(request_params)
            
            for symbol in symbols:
                symbol_bars = bars.data.get(symbol, [])
                if len(symbol_bars) < 20:
                    print(f"Not enough data for {symbol}")
                    continue
                
                # Extract close prices
                closes = [bar.close for bar in symbol_bars]
                
                # Hardcoded condition: 5-period SMA vs 20-period SMA
                sma_5 = sum(closes[-5:]) / 5
                sma_20 = sum(closes[-20:]) / 20
                
                current_price = closes[-1]
                
                if sma_5 > sma_20:
                    decision = "Buy"
                elif sma_5 < sma_20:
                    decision = "Sell"
                else:
                    decision = "Hold"
                    
                journal_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "symbol": symbol,
                    "metrics": {
                        "current_price": current_price,
                        "sma_5": sma_5,
                        "sma_20": sma_20
                    },
                    "decision": decision,
                    "model_suggested_decision": decision,
                    "hard_coded_decision": decision
                }
                
                with open("trading_journal.jsonl", "a") as f:
                    f.write(json.dumps(journal_entry) + "\n")
            
            print(f"[{datetime.now()}] Evaluation loop {i+1} completed.")
            
        except Exception as e:
            print(f"Error in data collection loop: {e}")
            
        # Sleep before next iteration to avoid rate limits and simulate periodic checks
        if i < 2:
            time.sleep(5)

if __name__ == "__main__":
    main()
