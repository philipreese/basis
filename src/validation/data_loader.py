import os
import pandas as pd
from datetime import datetime, timezone, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

def add_orthogonal_indicators(bars: list[dict]) -> list[dict]:
    if not bars:
        return bars
    df = pd.DataFrame(bars)
    
    # 1. VWAP (20-bar anchor)
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    pv = tp * df['volume']
    df['vwap'] = pv.rolling(window=20).sum() / df['volume'].rolling(window=20).sum()
    df['vwap'] = df['vwap'].fillna(df['close'])
    
    # 2. OBV (Continuous Accumulation)
    close_diff = df['close'].diff()
    direction = pd.Series(0.0, index=df.index)
    direction[close_diff > 0] = 1.0
    direction[close_diff < 0] = -1.0
    df['obv'] = (direction * df['volume']).cumsum()
    df['obv'] = df['obv'].fillna(0.0)
    
    # 3. OBV SMA20
    df['obv_sma20'] = df['obv'].rolling(window=20).mean()
    df['obv_sma20'] = df['obv_sma20'].fillna(0.0)
    
    for idx, row in df.iterrows():
        bars[idx]['vwap'] = float(row['vwap'])
        bars[idx]['obv'] = float(row['obv'])
        bars[idx]['obv_sma20'] = float(row['obv_sma20'])
        
    return bars


def generate_mock_bars(symbol: str, length: int = 100, anomaly: str = None) -> list[dict]:
    """
    Mode A: Generate a highly predictable array of historical bars.
    Supports injecting anomalies such as 'flat_volume' and 'price_spike'.
    """
    bars = []
    base_time = datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc)
    
    if symbol == "SPY":
        # Low volatility (ATR baseline ~1.5%), prolonged trend counts, clean SMA crossovers.
        close = 100.0
        for i in range(length):
            timestamp = base_time + timedelta(minutes=15 * i)
            # Prolonged trends: Bull for i < 40, Congestion for i < 65, Bear for i < 100
            if i < 40:
                close = 100.0 + i * 0.2
            elif i < 65:
                close = 108.0
            else:
                close = 108.0 - (i - 65) * 0.25
                
            open_val = close - 0.1
            # ATR ~1.5% of price (~100), so high - low ~ 1.5
            high = max(open_val, close) + 0.7
            low = min(open_val, close) - 0.7
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
                "trade_count": int(trade_count),
                "data_provenance": {
                    "source": "mock_generator",
                    "fetch_timestamp": base_time.isoformat(),
                    "normalization_version": "v2_hard_norm"
                }
            })
            
    elif symbol == "QQQ":
        # High volatility (ATR baseline ~3.5%), frequent localized noise, sharp mean-reversion pullbacks, and extended blocks of 'Congestion'.
        close = 100.0
        for i in range(length):
            timestamp = base_time + timedelta(minutes=15 * i)
            
            # Oscillating and high frequency crossovers
            if i < 20:
                close = 100.0 + i * 0.4
            elif i < 50:
                # Congestion: close oscillates around 108
                close = 108.0 + (1.5 if i % 2 == 0 else -1.5)
            elif i < 70:
                # Sharp pullback
                close = 108.0 - (i - 50) * 0.8
            else:
                # Extended block of congestion
                close = 92.0 + (2.0 if i % 3 == 0 else -2.0)
                
            open_val = close - 0.2
            # ATR ~3.5% of price (~100), so high - low ~ 3.5
            high = max(open_val, close) + 1.6
            low = min(open_val, close) - 1.7
            volume = 20000.0 + i * 100
            trade_count = 200 + i
            
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
                "trade_count": int(trade_count),
                "data_provenance": {
                    "source": "mock_generator",
                    "fetch_timestamp": base_time.isoformat(),
                    "normalization_version": "v2_hard_norm"
                }
            })
    else:
        # Fallback to default
        close = 100.0
        for i in range(length):
            timestamp = base_time + timedelta(minutes=15 * i)
            close = 100.0 + i * 0.1
            open_val = close - 0.05
            high = max(open_val, close) + 0.2
            low = min(open_val, close) - 0.2
            volume = 10000.0 + i * 50
            trade_count = 100 + i
            bars.append({
                "timestamp": timestamp,
                "open": float(open_val),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
                "trade_count": int(trade_count),
                "data_provenance": {
                    "source": "mock_generator",
                    "fetch_timestamp": base_time.isoformat(),
                    "normalization_version": "v2_hard_norm"
                }
            })
            
    for idx, bar in enumerate(bars):
        window = [b["close"] for b in bars[max(0, idx - 129): idx + 1]]
        bar["sma_macro"] = float(sum(window) / len(window))
        
    return add_orthogonal_indicators(bars)

def fetch_alpaca_bars(symbol: str, start_time: datetime, end_time: datetime, client: StockHistoricalDataClient, timeframe_str: str = "15m") -> list[dict]:
    """
    Mode B: Fetch real historical bars and normalize into the standard dictionary schema.
    """
    if timeframe_str == "1h":
        tf = TimeFrame(1, TimeFrameUnit.Hour)
    else:
        tf = TimeFrame(15, TimeFrameUnit.Minute)
        
    request_params = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start_time,
        end=end_time
    )
    response = client.get_stock_bars(request_params)
    raw_bars = response.data.get(symbol, [])
    
    normalized_bars = []
    fetch_ts = datetime.now(timezone.utc).isoformat()
    closes = [float(bar.close) for bar in raw_bars]
    for idx, bar in enumerate(raw_bars):
        trade_count = getattr(bar, "trade_count", 0)
        if trade_count is None:
            trade_count = 0
            
        window = closes[max(0, idx - 129): idx + 1]
        sma_macro = sum(window) / len(window)
        
        normalized_bars.append({
            "timestamp": bar.timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "trade_count": int(trade_count),
            "sma_macro": float(sma_macro),
            "data_provenance": {
                "source": "alpaca_historical",
                "fetch_timestamp": fetch_ts,
                "normalization_version": "v2_hard_norm"
            }
        })
        
    return add_orthogonal_indicators(normalized_bars)
