import os
import json
import requests
import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY_ID")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "out", "watchlist_cache.json")

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_to_cache(date_str, watchlist):
    cache = load_cache()
    cache[date_str] = watchlist
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=4)

def calculate_rolling_atr(bars, idx, period=14):
    if idx < 1:
        return 0.1
    trs = []
    start_idx = max(0, idx - period + 1)
    for i in range(start_idx, idx + 1):
        b = bars[i]
        h = b.get("h", b.get("c"))
        l = b.get("l", b.get("c"))
        if i > 0:
            pc = bars[i-1].get("c")
            tr = max(h - l, abs(h - pc), abs(l - pc))
        else:
            tr = h - l
        trs.append(tr)
    return max(sum(trs) / len(trs), 0.1)

def get_historical_news(symbols, start_date_str, end_date_str):
    if not symbols:
        return {}
    
    from alpaca.data.historical import NewsClient
    from alpaca.data.requests import NewsRequest
    
    start_time = datetime.datetime.strptime(start_date_str, "%Y-%m-%d")
    end_time = datetime.datetime.strptime(end_date_str, "%Y-%m-%d") + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    
    news_client = NewsClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = NewsRequest(
        symbols=symbols,
        start=start_time,
        end=end_time,
        limit=50
    )
    
    news_dict = {sym: [] for sym in symbols}
    try:
        res = news_client.get_news(req)
        articles = res.data.get("news", [])
        for article in articles:
            for sym in article.get("symbols", []):
                if sym in news_dict:
                    news_dict[sym].append(article.get("headline", "") + ". " + article.get("summary", ""))
    except Exception as e:
        print(f"[!] Error fetching news: {e}")
    return news_dict

def get_historical_bars(symbol, date_str):
    start_time = datetime.datetime.strptime(f"{date_str}T09:30:00", "%Y-%m-%dT%H:%M:%S")
    end_time = datetime.datetime.strptime(f"{date_str}T16:00:00", "%Y-%m-%dT%H:%M:%S")
    
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start_time,
        end=end_time
    )
    try:
        res = client.get_stock_bars(req)
        bars = res.data.get(symbol, [])
        return [
            {
                "t": b.timestamp.isoformat(),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
                "vw": b.vwap
            }
            for b in bars
        ]
    except Exception as e:
        print(f"[!] Error fetching bars for {symbol} on {date_str}: {e}")
        return []

def get_spy_performance(start_date_str, end_date_str):
    """Calculate SPY benchmark buy-and-hold returns over the period"""
    start_time = datetime.datetime.strptime(f"{start_date_str}T00:00:00", "%Y-%m-%dT%H:%M:%S")
    end_time = datetime.datetime.strptime(f"{end_date_str}T23:59:59", "%Y-%m-%dT%H:%M:%S")
    
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Day,
        start=start_time,
        end=end_time
    )
    try:
        res = client.get_stock_bars(req)
        bars = res.data.get("SPY", [])
        if len(bars) >= 2:
            start_price = bars[0].close
            end_price = bars[-1].close
            spy_return = ((end_price - start_price) / start_price) * 100.0
            return spy_return, start_price, end_price
    except Exception as e:
        print(f"[!] Error fetching SPY performance: {e}")
    return 0.0, 1.0, 1.0

def generate_watchlist_for_day(date_str, use_llm=False):
    """Wrapper that leverages cache or falls back to LLM to get day's watchlist"""
    if not use_llm:
        # Quant-only mode: bypass Gemini API and treat top 3 gap gainers as watchlist directly
        pass
    else:
        cache = load_cache()
        if date_str in cache:
            print(f"[*] Cache hit for {date_str}. Watchlist retrieved from local storage.")
            return cache[date_str]
        
    print(f"[*] Fetching candidates for {date_str}...")
    sample_universe = ["AAPL", "MSFT", "NVDA", "AMD", "META", "TSLA", "AMZN", "GOOGL", "NFLX", "COIN", "MARA", "PLTR", "BABA", "JD", "PDD"]
    
    candidates = []
    
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    
    start_time = datetime.datetime.strptime(f"{date_str}T00:00:00", "%Y-%m-%dT%H:%M:%S")
    end_time = datetime.datetime.strptime(f"{date_str}T23:59:59", "%Y-%m-%dT%H:%M:%S")
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = StockBarsRequest(
        symbol_or_symbols=sample_universe,
        timeframe=TimeFrame.Day,
        start=start_time,
        end=end_time
    )
    try:
        res = client.get_stock_bars(req)
        for sym in sample_universe:
            bars = res.data.get(sym, [])
            if len(bars) >= 1:
                bar = bars[0]
                close = bar.close
                vol = bar.volume
                open_p = bar.open
                gap_pct = ((open_p - close) / close) * 100.0 if close > 0 else 0.0
                
                # Symmetrical gap gainer filters
                if vol >= 1000000 and close >= 5.0 and abs(gap_pct) > 0.5:
                    candidates.append({
                        "ticker": sym,
                        "price": close,
                        "volume": vol,
                        "gap_percent": gap_pct,
                        "relevance_score": abs(gap_pct) * (vol / 1000000)
                    })
    except Exception as e:
        print(f"[!] Error fetching candidates: {e}")
        
    candidates = sorted(candidates, key=lambda x: x["relevance_score"], reverse=True)[:15]
    if not candidates:
        save_to_cache(date_str, [])
        return []
        
    # If using LLM mode, run Gemini extraction
    if use_llm:
        symbols = [c["ticker"] for c in candidates]
        news_map = get_historical_news(symbols, date_str, date_str)
        
        for c in candidates:
            c["events_text"] = " | ".join(news_map.get(c["ticker"], []))
            if not c["events_text"]:
                c["events_text"] = "Abnormal volume/gap detected without explicit news headline."
                
        out_dir = os.path.join(os.path.dirname(__file__), "..", "..", "out")
        os.makedirs(out_dir, exist_ok=True)
        candidates_path = os.path.join(out_dir, "premarket_candidates.json")
        with open(candidates_path, "w") as f:
            json.dump(candidates, f, indent=4)
            
        from src.analysis.watchlist_generator import generate_watchlist
        generate_watchlist()
        
        watchlist_path = os.path.join(out_dir, "watchlist.json")
        if os.path.exists(watchlist_path):
            with open(watchlist_path, "r") as f:
                watchlist = json.load(f)
            save_to_cache(date_str, watchlist)
            return watchlist
        return []
    else:
        # Quant-only baseline: convert top 3 candidates directly to watchlists
        watchlist = []
        for c in candidates[:3]:
            watchlist.append({
                "ticker": c["ticker"],
                "event_type": "Quant Gap Mover",
                "sentiment_shift": "positive" if c["gap_percent"] > 0 else "negative",
                "volatility_expectation": "high",
                "continuation_bias": "expansion",
                "catalyst_strength": "high",
                "crowding_risk": "medium",
                "risk_flags": []
            })
        return watchlist

def run_multi_day_backtest(start_date_str, end_date_str, use_llm=False):
    print(f"\n=======================================================")
    print(f"STARTING COMPREHENSIVE MULTI-DAY SIMULATION")
    print(f"Period: {start_date_str} to {end_date_str} | Mode: {'LLM Catalyst' if use_llm else 'Quant-Only Baseline'}")
    print(f"=======================================================\n")
    
    start_dt = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()
    
    from src.risk_reviewer import RiskReviewer
    risk_reviewer = RiskReviewer()
    
    total_days = 0
    no_trade_days = 0
    trade_logs = []
    
    account_value = 1000.0  # Small cap balance starting at $1000
    initial_value = account_value
    
    curr = start_dt
    while curr <= end_dt:
        # Skip weekends
        if curr.weekday() >= 5:
            curr += datetime.timedelta(days=1)
            continue
            
        date_str = curr.strftime("%Y-%m-%d")
        total_days += 1
        print(f"\n[*] Day {total_days}: Processing {date_str}...")
        
        try:
            watchlist = generate_watchlist_for_day(date_str, use_llm=use_llm)
        except Exception as e:
            print(f"[!] Error generating watchlist for {date_str}: {e}")
            watchlist = []
            
        if not watchlist:
            no_trade_days += 1
            print(f"[-] NO TRADE DAY recorded for {date_str}.")
            curr += datetime.timedelta(days=1)
            continue
            
        # Simulate intraday stream execution for watchlist items
        max_streams = 3
        priority_map = {"high": 3, "medium": 2, "low": 1}
        watchlist = sorted(watchlist, key=lambda x: priority_map.get(x.get("catalyst_strength", "low"), 0), reverse=True)
        targets = watchlist[:max_streams]
        
        for item in targets:
            sym = item["ticker"]
            bars = get_historical_bars(sym, date_str)
            if not bars:
                continue
                
            orb_high = None
            orb_low = None
            state = "WAITING_ORB"
            entry_price = None
            stop_loss = None
            be_triggered = False
            position_size = 0
            risk_dollar = account_value * risk_reviewer.risk_budget_pct # 2% per trade
            
            for idx, bar in enumerate(bars):
                ts_str = bar.get("t")
                dt = datetime.datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=pytz.utc).astimezone(pytz.timezone("US/Eastern"))
                price = bar.get("c", 0.0)
                vwap = bar.get("vw", price)
                
                # ORB Period (9:30 to 9:45 EST)
                if dt.hour == 9 and dt.minute <= 45:
                    if orb_high is None or price > orb_high:
                        orb_high = price
                    if orb_low is None or price < orb_low:
                        orb_low = price
                    continue
                    
                atr = calculate_rolling_atr(bars, idx)
                
                # Entry Evaluation
                if state == "WAITING_ORB" and orb_high is not None:
                    continuation = item.get("continuation_bias", "uncertain")
                    if continuation == "expansion" and price > orb_high and price > vwap:
                        stop_distance = atr * risk_reviewer.stop_atr_multiplier
                        position_size = int(risk_dollar / stop_distance) if stop_distance > 0 else 0
                        if position_size > 0:
                            entry_price = price
                            stop_loss = price - stop_distance
                            state = "ACTIVE"
                            be_triggered = False
                            print(f"  [+] {dt.strftime('%H:%M')} - ENTRY: {sym} @ {price:.2f} | Stop: {stop_loss:.2f}")
                            
                # Exit Evaluation
                elif state == "ACTIVE":
                    if price < stop_loss:
                        realized_pnl = (price - entry_price) * position_size
                        r_multiple = realized_pnl / risk_dollar
                        
                        slippage_drag = (price * position_size) * 0.0005 # 5 bps slippage
                        net_pnl = realized_pnl - slippage_drag
                        
                        account_value += net_pnl
                        state = "COMPLETED"
                        trade_logs.append({
                            "date": date_str, "ticker": sym, "type": "StopLoss", 
                            "pnl": net_pnl, "r_multiple": r_multiple, "status": "Loss" if net_pnl < 0 else "Win"
                        })
                        print(f"  [-] {dt.strftime('%H:%M')} - STOP-OUT: {sym} @ {price:.2f} | Net PnL: {net_pnl:.2f} ({r_multiple:.2f}R)")
                        break
                        
                    # Check Take Profit
                    tp_mult = risk_reviewer.take_profit_ratio
                    if tp_mult is not None:
                        target_price = entry_price + (atr * risk_reviewer.stop_atr_multiplier * tp_mult)
                        if price >= target_price:
                            realized_pnl = (price - entry_price) * position_size
                            r_multiple = realized_pnl / risk_dollar
                            
                            slippage_drag = (price * position_size) * 0.0005
                            net_pnl = realized_pnl - slippage_drag
                            
                            account_value += net_pnl
                            state = "COMPLETED"
                            trade_logs.append({
                                "date": date_str, "ticker": sym, "type": "TakeProfit",
                                "pnl": net_pnl, "r_multiple": r_multiple, "status": "Loss" if net_pnl < 0 else "Win"
                            })
                            print(f"  [-] {dt.strftime('%H:%M')} - TAKE-PROFIT: {sym} @ {price:.2f} | Net PnL: {net_pnl:.2f} ({r_multiple:.2f}R)")
                            break
                            
                    # Check Break-Even Trigger
                    be_ratio = risk_reviewer.break_even_ratio
                    if be_ratio is not None and not be_triggered:
                        trigger_price = entry_price + (atr * risk_reviewer.stop_atr_multiplier * be_ratio)
                        if price >= trigger_price:
                            stop_loss = entry_price
                            be_triggered = True
                            print(f"  [*] {dt.strftime('%H:%M')} - BREAK-EVEN SECURED: {sym} @ {price:.2f} (Stop moved to entry)")
                            
                    # Trailing Stop
                    new_stop = price - (atr * risk_reviewer.stop_atr_multiplier)
                    if new_stop > stop_loss:
                        stop_loss = new_stop
                        
                    if dt.hour == 15 and dt.minute >= 50:
                        realized_pnl = (price - entry_price) * position_size
                        r_multiple = realized_pnl / risk_dollar
                        
                        slippage_drag = (price * position_size) * 0.0005
                        net_pnl = realized_pnl - slippage_drag
                        
                        account_value += net_pnl
                        state = "COMPLETED"
                        trade_logs.append({
                            "date": date_str, "ticker": sym, "type": "EOD", 
                            "pnl": net_pnl, "r_multiple": r_multiple, "status": "Loss" if net_pnl < 0 else "Win"
                        })
                        print(f"  [-] {dt.strftime('%H:%M')} - EOD EXIT: {sym} @ {price:.2f} | Net PnL: {net_pnl:.2f} ({r_multiple:.2f}R)")
                        break
                        
        curr += datetime.timedelta(days=1)
        
    # Benchmark Evaluation
    spy_return, spy_start, spy_end = get_spy_performance(start_date_str, end_date_str)
    strategy_return = ((account_value - initial_value) / initial_value) * 100.0
    
    # Compute aggregations
    completed_trades = len(trade_logs)
    wins = [t for t in trade_logs if t["status"] == "Win"]
    losses = [t for t in trade_logs if t["status"] == "Loss"]
    win_rate = (len(wins) / completed_trades) * 100.0 if completed_trades > 0 else 0.0
    
    total_gains = sum(t["pnl"] for t in wins)
    total_losses = abs(sum(t["pnl"] for t in losses))
    profit_factor = total_gains / total_losses if total_losses > 0 else (total_gains if total_gains > 0 else 1.0)
    
    avg_r = sum(t["r_multiple"] for t in trade_logs) / completed_trades if completed_trades > 0 else 0.0
    no_trade_pct = (no_trade_days / total_days) * 100.0 if total_days > 0 else 0.0
    
    # Print clean report
    print(f"\n=======================================================")
    print(f"BACKTEST PERFORMANCE SUMMARY")
    print(f"=======================================================")
    print(f"Simulation Period:      {start_date_str} to {end_date_str}")
    print(f"Total Trading Days:     {total_days}")
    print(f"NO TRADE Days:          {no_trade_days} ({no_trade_pct:.1f}%)")
    print(f"Completed Trades:       {completed_trades}")
    print(f"Win Rate:               {win_rate:.1f}% ({len(wins)} W / {len(losses)} L)")
    print(f"Profit Factor:          {profit_factor:.2f}")
    print(f"Average R-multiple:     {avg_r:.2f}R")
    print(f"-------------------------------------------------------")
    print(f"Initial Account Value:  ${initial_value:.2f}")
    print(f"Final Account Value:    ${account_value:.2f}")
    print(f"Strategy Total Return:  {strategy_return:.2f}%")
    print(f"SPY Buy-and-Hold Return: {spy_return:.2f}%")
    print(f"=======================================================\n")
    
if __name__ == "__main__":
    # Simulate a 1-month period (e.g. 2026-03-01 to 2026-04-01) for verification
    # By default, use_llm=False is active to prevent API quota usage. 
    # Set use_llm=True to test the full Catalyst sentiment extractor.
    run_multi_day_backtest("2026-03-01", "2026-04-01", use_llm=False)
