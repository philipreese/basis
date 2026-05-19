import os
import time
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY_ID")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

def get_config():
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "risk_params.json")
    with open(config_path, "r") as f:
        return json.load(f)

def fetch_movers():
    from alpaca.data.historical import ScreenerClient
    from alpaca.data.requests import MarketMoversRequest
    
    client = ScreenerClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = MarketMoversRequest(top=50)
    try:
        res = client.get_market_movers(req)
        movers = {}
        for m in (res.gainers or []) + (res.losers or []):
            movers[m.symbol] = {
                "symbol": m.symbol,
                "percent_change": m.percent_change,
                "change": m.change,
                "price": m.price
            }
        return list(movers.values())
    except Exception as e:
        print(f"[!] Error fetching movers: {e}")
        return []

def filter_and_rank_candidates(movers):
    if not movers:
        return []
        
    symbols = [m["symbol"] for m in movers]
    
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = StockSnapshotRequest(symbol_or_symbols=symbols)
    
    try:
        res = client.get_stock_snapshot(req)
        filtered = []
        for m in movers:
            sym = m["symbol"]
            snap = res.get(sym)
            if not snap:
                continue
                
            prev_bar = snap.previous_daily_bar
            latest_trade = snap.latest_trade
            
            volume = prev_bar.volume if prev_bar else 0
            price = latest_trade.price if latest_trade else 0.0
            if price == 0.0 and prev_bar:
                price = prev_bar.close
                
            # Hard-coded constraints: ADV > 1M (approximated by prev daily volume), Price > $5.00
            if volume >= 1000000 and price >= 5.0:
                change_pct = m.get("percent_change", 0.0)
                filtered.append({
                    "ticker": sym,
                    "price": price,
                    "volume": volume,
                    "gap_percent": change_pct,
                    "relevance_score": abs(change_pct) * (volume / 1000000)
                })
                
        # Sort by relevance
        filtered = sorted(filtered, key=lambda x: x["relevance_score"], reverse=True)
        return filtered[:15]
    except Exception as e:
        print(f"[!] Error during filter and rank: {e}")
        return []

def fetch_catalyst_news(symbols):
    if not symbols:
        return {}
        
    from alpaca.data.historical import NewsClient
    from alpaca.data.requests import NewsRequest
    
    news_client = NewsClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = NewsRequest(symbols=symbols, limit=50)
    
    news_dict = {sym: [] for sym in symbols}
    try:
        res = news_client.get_news(req)
        articles = res.data.get("news", [])
        for article in articles:
            for sym in article.get("symbols", []):
                if sym in news_dict:
                    news_dict[sym].append(article.get("headline", "") + ". " + article.get("summary", ""))
    except Exception as e:
        print(f"[!] Error fetching catalyst news: {e}")
    return news_dict

def run_collection_interval():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Executing Pre-Market Catalyst Collection...")
    movers = fetch_movers()
    candidates = filter_and_rank_candidates(movers)
    
    if not candidates:
        print("[!] No valid candidates found in this interval.")
        return
        
    symbols = [c["ticker"] for c in candidates]
    news = fetch_catalyst_news(symbols)
    
    # Attach news
    for c in candidates:
        c["events_text"] = " | ".join(news.get(c["ticker"], []))
        if not c["events_text"]:
            c["events_text"] = "Abnormal pre-market volume/gap detected without explicit news headline."
            
    # Incremental Append
    out_dir = os.path.join(os.path.dirname(__file__), "..", "..", "out")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "premarket_candidates.json")
    
    existing = []
    if os.path.exists(out_file):
        try:
            with open(out_file, "r") as f:
                existing = json.load(f)
        except:
            pass
            
    # Append without duplicates (keep the most recent gap/price data if duplicate)
    existing_map = {e["ticker"]: e for e in existing}
    for c in candidates:
        existing_map[c["ticker"]] = c
        
    final_list = list(existing_map.values())
    
    with open(out_file, "w") as f:
        json.dump(final_list, f, indent=4)
        
    print(f"[*] Successfully appended {len(candidates)} candidates. Total pool size: {len(final_list)}")

def main():
    config = get_config()
    intervals = config.get("premarket_scan_intervals", ["07:30", "08:30", "09:15"])
    
    # Sort intervals to process them chronologically
    intervals = sorted(intervals)
    
    print(f"[*] Starting Catalyst Collector active loop. Scheduled intervals: {intervals}")
    
    while intervals:
        now = datetime.now()
        current_time_str = now.strftime("%H:%M")
        
        target_interval = intervals[0]
        
        if current_time_str >= target_interval:
            # We've reached or passed the target interval
            run_collection_interval()
            intervals.pop(0) # Remove the processed interval
        else:
            # Sleep until the next minute check
            time.sleep(30)
            
    print("[*] All pre-market scan intervals completed. Collector standing down.")

if __name__ == "__main__":
    main()
