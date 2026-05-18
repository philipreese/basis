import os
from datetime import datetime, timezone, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

def generate_mock_bars(symbol: str, length: int = 100, anomaly: str = None) -> list[dict]:
    """
    Mode A: Generate a highly predictable array of historical bars.
    Supports injecting anomalies such as 'flat_volume' and 'price_spike'.
    """
    bars = []
    base_time = datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc)
    
    for i in range(length):
        timestamp = base_time + timedelta(minutes=15 * i)
        
        # Piecewise price trends to exercise crossovers and congestion regime
        if i < 30:
            close = 100.0 + i * 0.1  # Bull regime
        elif i < 60:
            close = 103.0            # Congestion regime
        else:
            close = 103.0 - (i - 60) * 0.15  # Bear regime
            
        open_val = close - 0.05
        high = max(open_val, close) + 0.2
        low = min(open_val, close) - 0.2
        volume = 10000.0 + i * 50
        trade_count = 100 + i
        
        # Anomaly Injection Logic
        if anomaly == "flat_volume":
            volume = 1.0
        elif anomaly == "price_spike" and i == 50:
            close *= 10.0
            open_val *= 10.0
            high *= 10.0
            low *= 10.0
            
        bars.append({
            "timestamp": timestamp,
            "open": float(open_val),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
            "trade_count": int(trade_count)
        })
        
    return bars

def fetch_alpaca_bars(symbol: str, start_time: datetime, end_time: datetime, client: StockHistoricalDataClient) -> list[dict]:
    """
    Mode B: Fetch real historical 15-minute bars and normalize into the standard dictionary schema.
    """
    request_params = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start_time,
        end=end_time
    )
    response = client.get_stock_bars(request_params)
    raw_bars = response.data.get(symbol, [])
    
    normalized_bars = []
    for bar in raw_bars:
        normalized_bars.append({
            "timestamp": bar.timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "trade_count": int(bar.trade_count)
        })
        
    return normalized_bars
