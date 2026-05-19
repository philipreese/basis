import os
import json
import time
import datetime
import pytz
import requests
from dotenv import load_dotenv

from src.risk_reviewer import RiskReviewer

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY_ID")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

def get_config():
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "risk_params.json")
    with open(config_path, "r") as f:
        return json.load(f)

def log_telemetry(msg):
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "trading_journal.log")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {msg}\n"
    print(full_msg.strip())
    with open(log_path, "a") as f:
        f.write(full_msg)

class IntradayStream:
    def __init__(self, ticker, item_data, risk_reviewer):
        self.ticker = ticker
        self.item_data = item_data
        self.risk_reviewer = risk_reviewer
        self.state = "WAITING_ORB" # WAITING_ORB, ACTIVE, COMPLETED, REJECTED
        self.orb_high = None
        self.orb_low = None
        self.entry_price = None
        self.stop_loss = None
        self.be_triggered = False
        self.position_size = 0
        self.risk_dollar = 0
        
    def evaluate(self, current_time, current_price, vwap, atr, volume, relative_volume):
        if self.state in ["COMPLETED", "REJECTED"]:
            return
            
        market_open = current_time.replace(hour=9, minute=30, second=0, microsecond=0)
        orb_end = current_time.replace(hour=9, minute=45, second=0, microsecond=0)
        
        # Collect ORB
        if current_time <= orb_end:
            if self.orb_high is None or current_price > self.orb_high:
                self.orb_high = current_price
            if self.orb_low is None or current_price < self.orb_low:
                self.orb_low = current_price
            return
            
        # Entry Logic (ORB Breakout + VWAP Reclaim)
        if self.state == "WAITING_ORB":
            if self.orb_high is None or self.orb_low is None:
                self.state = "REJECTED"
                log_telemetry(f"[{self.ticker}] REJECTED: ORB boundaries not established.")
                return
                
            continuation_bias = self.item_data.get("continuation_bias", "uncertain")
            
            # Simple momentum entry rule: price > ORB high AND price > VWAP
            if continuation_bias == "expansion" and current_price > self.orb_high and current_price > vwap:
                if relative_volume > 1.2: # Volume confirmation
                    self.execute_entry(current_price, atr)
            
        elif self.state == "ACTIVE":
            # Hard trailing ATR stop logic
            if current_price < self.stop_loss:
                self.execute_exit(current_price, "StopLoss")
                return
                
            # Check Take Profit
            tp_mult = self.risk_reviewer.take_profit_ratio
            if tp_mult is not None:
                target_price = self.entry_price + (atr * self.risk_reviewer.stop_atr_multiplier * tp_mult)
                if current_price >= target_price:
                    self.execute_exit(current_price, "TakeProfit")
                    return
                    
            # Check Break-Even Trigger
            be_ratio = self.risk_reviewer.break_even_ratio
            if be_ratio is not None and not self.be_triggered:
                trigger_price = self.entry_price + (atr * self.risk_reviewer.stop_atr_multiplier * be_ratio)
                if current_price >= trigger_price:
                    self.stop_loss = self.entry_price
                    self.be_triggered = True
                    log_telemetry(f"[{self.ticker}] BREAK-EVEN SECURED (Stop moved to entry @ {self.entry_price:.2f})")
                    
            # Update trailing stop (only if new_stop is higher than current stop_loss)
            new_stop = current_price - (atr * self.risk_reviewer.stop_atr_multiplier)
            if new_stop > self.stop_loss:
                self.stop_loss = new_stop
                
            # End of day exit
            market_close = current_time.replace(hour=15, minute=50, second=0, microsecond=0)
            if current_time >= market_close:
                self.execute_exit(current_price, "EndOfDay")
 
    def execute_entry(self, price, atr):
        account_value = 1000.0 # Mock small account balance ($1000)
        # Sizing based on risk parameter
        self.risk_dollar = account_value * self.risk_reviewer.risk_budget_pct
        stop_distance = atr * self.risk_reviewer.stop_atr_multiplier
        
        if stop_distance == 0:
            return
            
        self.position_size = int(self.risk_dollar / stop_distance)
        if self.position_size <= 0:
            self.state = "REJECTED"
            log_telemetry(f"[{self.ticker}] REJECTED: Position size rounds to 0 due to wide ATR.")
            return
            
        self.entry_price = price
        self.stop_loss = price - stop_distance
        self.be_triggered = False
        self.state = "ACTIVE"
        
        log_telemetry(f"EXECUTION - ENTRY | Ticker: {self.ticker} | Size: {self.position_size} | Price: {self.entry_price} | Initial Stop: {self.stop_loss:.2f}")
 
    def execute_exit(self, price, reason):
        realized_pnl = (price - self.entry_price) * self.position_size
        r_multiple = realized_pnl / self.risk_dollar if self.risk_dollar > 0 else 0
        
        # Estimate slippage as fixed bps
        slippage_bps = 5.0
        slippage_drag = (price * self.position_size) * (slippage_bps / 10000.0)
        net_pnl = realized_pnl - slippage_drag
        
        self.state = "COMPLETED"
        log_telemetry(f"EXECUTION - EXIT | Ticker: {self.ticker} | Reason: {reason} | Exit Price: {price} | Net PnL: {net_pnl:.2f} | R-Multiple: {r_multiple:.2f}R | Slippage Drag: {slippage_drag:.2f}")

def get_rolling_atr_live(symbol, client):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    
    # Fetch last 30 minutes of 1-minute bars to ensure we have at least 14 bars
    now = datetime.datetime.now(pytz.utc)
    start_time = now - datetime.timedelta(minutes=30)
    
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start_time,
        end=now
    )
    try:
        res = client.get_stock_bars(req)
        bars = res.data.get(symbol, [])
        if len(bars) < 2:
            return 0.1
        
        # Calculate rolling ATR on the retrieved bars
        trs = []
        for i in range(1, len(bars)):
            h = bars[i].high
            l = bars[i].low
            pc = bars[i-1].close
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
            
        if not trs:
            return 0.1
            
        last_trs = trs[-14:]
        return max(sum(last_trs) / len(last_trs), 0.1)
    except Exception as e:
        print(f"[!] Error calculating live rolling ATR for {symbol}: {e}")
        return 0.1

def get_latest_market_data(symbols):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = StockSnapshotRequest(symbol_or_symbols=symbols)
    try:
        res = client.get_stock_snapshot(req)
        mapped_data = {}
        for sym in symbols:
            snap = res.get(sym)
            if not snap:
                continue
            
            p_bar = snap.previous_daily_bar
            d_bar = snap.daily_bar
            l_trade = snap.latest_trade
            
            mapped_data[sym] = {
                "prev_daily_bar": {
                    "v": p_bar.volume if p_bar else 0,
                    "h": p_bar.high if p_bar else 0.0,
                    "l": p_bar.low if p_bar else 0.0,
                    "c": p_bar.close if p_bar else 0.0
                } if p_bar else {},
                "daily_bar": {
                    "vw": d_bar.vwap if d_bar else (l_trade.price if l_trade else 0.0),
                    "v": d_bar.volume if d_bar else 0
                } if d_bar else {},
                "latest_trade": {
                    "p": l_trade.price if l_trade else 0.0
                } if l_trade else {}
            }
        return mapped_data
    except Exception as e:
        print(f"[!] Error fetching latest market data: {e}")
        return {}


def run_intraday_engine():
    log_telemetry("Intraday Engine Initializing...")
    config = get_config()
    risk_reviewer = RiskReviewer() # Auto-loads risk_params.json internally
    
    out_dir = os.path.join(os.path.dirname(__file__), "..", "..", "out")
    watchlist_path = os.path.join(out_dir, "watchlist.json")
    
    if not os.path.exists(watchlist_path):
        log_telemetry("NO TRADE: watchlist.json does not exist. Standing down for the session.")
        return
        
    with open(watchlist_path, "r") as f:
        watchlist = json.load(f)
        
    if not watchlist:
        log_telemetry("NO TRADE: Watchlist is empty. No valid setups detected. Standing down for the session.")
        return
        
    max_streams = config.get("max_concurrent_positions", 3)
    
    # Priority sorting (highest catalyst strength first)
    priority_map = {"high": 3, "medium": 2, "low": 1}
    watchlist = sorted(watchlist, key=lambda x: priority_map.get(x.get("catalyst_strength", "low"), 0), reverse=True)
    
    selected_targets = watchlist[:max_streams]
    symbols = [w["ticker"] for w in selected_targets]
    
    log_telemetry(f"Targets Selected for Concurrent Streams: {symbols}")
    
    streams = {sym: IntradayStream(sym, w, risk_reviewer) for sym, w in zip(symbols, selected_targets)}
    
    tz = pytz.timezone('US/Eastern')
    
    # Wait until 9:30 AM EST
    while True:
        now = datetime.datetime.now(tz)
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        
        if now < market_open:
            time_to_wait = (market_open - now).total_seconds()
            log_telemetry(f"Waiting {time_to_wait:.0f} seconds until RTH Open (09:30 EST)...")
            time.sleep(min(60, time_to_wait)) # Sleep in chunks
        else:
            break
            
    log_telemetry("09:30 EST Reached. Starting Concurrent Intraday Execution Streams.")
    
    from alpaca.data.historical import StockHistoricalDataClient
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    
    # Intraday Polling Loop
    while True:
        now = datetime.datetime.now(tz)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        
        if now >= market_close:
            log_telemetry("Market Closed. Shutting down Intraday Engine.")
            break
            
        # Check if all streams are completed or rejected
        active_streams = [s for s in streams.values() if s.state in ["WAITING_ORB", "ACTIVE"]]
        if not active_streams:
            log_telemetry("All intraday streams are COMPLETED or REJECTED. Shutting down cleanly.")
            break
            
        try:
            market_data = get_latest_market_data(symbols)
            for sym, stream in streams.items():
                if stream.state in ["COMPLETED", "REJECTED"]:
                    continue
                    
                snap = market_data.get(sym)
                if not snap:
                    continue
                    
                latest_trade = snap.get("latest_trade", {})
                current_price = latest_trade.get("p", 0.0)
                
                # Retrieve indicators
                prev_bar = snap.get("prev_daily_bar", {})
                vwap = snap.get("daily_bar", {}).get("vw", current_price)
                volume = snap.get("daily_bar", {}).get("v", 0)
                avg_volume = prev_bar.get("v", 1)
                relative_volume = volume / avg_volume if avg_volume > 0 else 1.0
                
                # Call live rolling 14-period ATR
                atr = get_rolling_atr_live(sym, client)
                
                if current_price > 0:
                    stream.evaluate(now, current_price, vwap, atr, volume, relative_volume)
                    
        except Exception as e:
            log_telemetry(f"ERROR in Intraday Polling Loop: {e}")
            
        time.sleep(60) # 1-minute polling cycle

if __name__ == "__main__":
    run_intraday_engine()
