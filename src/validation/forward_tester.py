import os
import json
import requests
import datetime
import pytz
import csv
import random
import numpy as np
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY_ID")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "out", "watchlist_cache.json")
CACHE_FILE_QUANT = os.path.join(os.path.dirname(__file__), "..", "..", "out", "watchlist_cache_quant.json")

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

def load_quant_cache():
    if os.path.exists(CACHE_FILE_QUANT):
        try:
            with open(CACHE_FILE_QUANT, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_to_quant_cache(date_str, watchlist):
    cache = load_quant_cache()
    cache[date_str] = watchlist
    os.makedirs(os.path.dirname(CACHE_FILE_QUANT), exist_ok=True)
    with open(CACHE_FILE_QUANT, "w") as f:
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

def get_daily_liquidity_metrics(symbols, date_str, client):
    import datetime
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    
    # Strictly causal: query up to the day before date_str to avoid today's daily bar look-ahead
    end_dt = datetime.datetime.strptime(date_str, "%Y-%m-%d") - datetime.timedelta(days=1)
    # Pull trailing 30 days to guarantee at least 14 daily bars
    start_dt = end_dt - datetime.timedelta(days=30)
    
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start_dt,
        end=end_dt
    )
    
    metrics = {}
    try:
        res = client.get_stock_bars(req)
        for sym in symbols:
            bars = res.data.get(sym, [])
            if len(bars) >= 2:
                # Calculate metrics over the last min(14, len(bars))
                recent_bars = bars[-14:]
                spreads = []
                dollar_vols = []
                for b in recent_bars:
                    spr = (b.high - b.low) / b.close if b.close > 0 else 0.0
                    spreads.append(spr)
                    dollar_vols.append(b.volume * b.close)
                avg_spread = sum(spreads) / len(spreads) if spreads else 0.01
                avg_dollar_vol = sum(dollar_vols) / len(dollar_vols) if dollar_vols else 10000000.0
                metrics[sym] = {
                    "spread_proxy": avg_spread,
                    "dollar_volume": avg_dollar_vol
                }
            else:
                metrics[sym] = {
                    "spread_proxy": 0.01,
                    "dollar_volume": 10000000.0
                }
    except Exception as e:
        # Fallback values
        for sym in symbols:
            metrics[sym] = {
                "spread_proxy": 0.01,
                "dollar_volume": 10000000.0
            }
    return metrics

def get_historical_bars(symbol, date_str):
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "out", "bars_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{symbol}_{date_str}.json")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error reading bar cache for {symbol} on {date_str}: {e}")
            
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
        bars_data = [
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
        
        # Save to cache
        try:
            with open(cache_file, "w") as f:
                json.dump(bars_data, f)
        except Exception as cache_err:
            print(f"[!] Error writing bar cache for {symbol} on {date_str}: {cache_err}")
            
        return bars_data
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
        quant_cache = load_quant_cache()
        if date_str in quant_cache:
            return quant_cache[date_str]
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
    
    # Query from 7 days ago to today to ensure we get both today's open and yesterday's close/volume causally
    start_time_prev = datetime.datetime.strptime(f"{date_str}T00:00:00", "%Y-%m-%dT%H:%M:%S") - datetime.timedelta(days=7)
    end_time_today = datetime.datetime.strptime(f"{date_str}T23:59:59", "%Y-%m-%dT%H:%M:%S")
    
    client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    req = StockBarsRequest(
        symbol_or_symbols=sample_universe,
        timeframe=TimeFrame.Day,
        start=start_time_prev,
        end=end_time_today
    )
    try:
        res = client.get_stock_bars(req)
        for sym in sample_universe:
            bars = res.data.get(sym, [])
            # Filter bars up to today
            bars_clean = [b for b in bars if b.timestamp.strftime("%Y-%m-%d") <= date_str]
            if len(bars_clean) >= 2:
                today_bar = bars_clean[-1]
                prev_bar = bars_clean[-2]
                
                # Check if today_bar actually corresponds to date_str
                if today_bar.timestamp.strftime("%Y-%m-%d") == date_str:
                    close = prev_bar.close     # Previous day's close (causal)
                    vol = prev_bar.volume       # Previous day's volume (causal)
                    open_p = today_bar.open     # Today's open price (causal at 9:30 AM)
                    gap_pct = ((open_p - close) / close) * 100.0 if close > 0 else 0.0
                    
                    # Symmetrical gap filters
                    if vol >= 1000000 and close >= 5.0 and abs(gap_pct) > 0.5:
                        candidates.append({
                            "ticker": sym,
                            "price": close, # Use previous close as base price reference
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
        
    # Get daily liquidity metrics for candidates
    candidate_symbols = [c["ticker"] for c in candidates]
    liq_metrics = get_daily_liquidity_metrics(candidate_symbols, date_str, client)
    for c in candidates:
        m = liq_metrics.get(c["ticker"], {"spread_proxy": 0.01, "dollar_volume": 10000000.0})
        c["spread_proxy"] = m["spread_proxy"]
        c["dollar_volume"] = m["dollar_volume"]
        
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
            # Decorate watchlist items with metrics from candidates
            cand_map = {c["ticker"]: c for c in candidates}
            for item in watchlist:
                c = cand_map.get(item["ticker"], {})
                item["gap_pct"] = c.get("gap_percent", 0.0)
                item["relative_volume"] = c.get("relevance_score", 0.0) / abs(c.get("gap_percent", 1.0)) if c.get("gap_percent", 0.0) != 0 else 1.0
                item["spread_proxy"] = c.get("spread_proxy", 0.01)
                item["dollar_volume"] = c.get("dollar_volume", 10000000.0)
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
                "risk_flags": [],
                "gap_pct": c["gap_percent"],
                "relative_volume": c["relevance_score"] / abs(c["gap_percent"]) if c["gap_percent"] != 0 else 1.0,
                "spread_proxy": c.get("spread_proxy", 0.01),
                "dollar_volume": c.get("dollar_volume", 10000000.0)
            })
        save_to_quant_cache(date_str, watchlist)
        return watchlist

def log_trade_to_files(trade):
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    
    # 1. Log to trading_journal.log
    logs_dir = os.path.join(project_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    journal_path = os.path.join(logs_dir, "trading_journal.log")
    
    timestamp = datetime.datetime.now().isoformat()
    with open(journal_path, "a") as f:
        f.write(f"[{timestamp}] TRADE COMPLETE | Date: {trade['date']} | Ticker: {trade['ticker']} | Exit: {trade['stop_out_reason']} | PnL: {trade['pnl']:.2f} | R: {trade['r_multiple']:.2f}R\n")
        
    # 2. Log to trade_dataset.csv
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "trade_dataset.csv")
    
    file_exists = os.path.exists(csv_path)
    
    headers = [
        "date", "ticker", "type", "pnl", "r_multiple", "status",
        "catalyst_type", "gap_pct", "relative_volume", "entry_trigger_type",
        "hold_duration", "mfe", "mae", "slippage_estimate", "execution_latency_estimate",
        "stop_out_reason", "take_profit_trigger_status", "dollar_volume", "spread_proxy",
        "regime", "is_transition", "epoch"
    ]
    
    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(headers)
        writer.writerow([
            trade["date"],
            trade["ticker"],
            trade["type"],
            trade["pnl"],
            trade["r_multiple"],
            trade["status"],
            trade["catalyst_type"],
            trade["gap_pct"],
            trade["relative_volume"],
            trade["entry_trigger_type"],
            trade["hold_duration"],
            trade["mfe"],
            trade["mae"],
            trade["slippage_estimate"],
            trade["execution_latency_estimate"],
            trade["stop_out_reason"],
            trade["take_profit_trigger_status"],
            trade["dollar_volume"],
            trade["spread_proxy"],
            trade.get("regime", "CHOPPY_ROTATIONAL"),
            trade.get("is_transition", False),
            trade.get("epoch", "Phase_35")
        ])

def run_multi_day_backtest(start_date_str, end_date_str, use_llm=False, initial_value=1000.0, entry_timing_shift=0, orb_minutes=15, max_streams=3, min_rvol=None, min_gap=None, log_to_csv=True, adversarial_mode=False, random_latency_spread=False, spread_widening_coeff=1.0):
    print(f"\n=======================================================")
    print(f"STARTING COMPREHENSIVE MULTI-DAY SIMULATION")
    print(f"Period: {start_date_str} to {end_date_str} | Mode: {'LLM Catalyst' if use_llm else 'Quant-Only Baseline'}")
    print(f"=======================================================\n")
    
    start_dt = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()
    
    from src.validation.regime_classifier import classify_regime_for_period
    from alpaca.data.historical import StockHistoricalDataClient
    client_regime = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    try:
        regime_df = classify_regime_for_period(start_date_str, end_date_str, client_regime)
        regime_map = {}
        if not regime_df.empty:
            for _, row in regime_df.iterrows():
                date_key = row["date"].strftime("%Y-%m-%d")
                regime_map[date_key] = {
                    "regime": row["regime"],
                    "is_transition": row["is_transition"]
                }
    except Exception as e:
        print(f"[!] Error pre-classifying regimes: {e}")
        regime_map = {}
        
    from src.risk_reviewer import RiskReviewer
    risk_reviewer = RiskReviewer()
    
    account_value = initial_value
    trade_logs = []
    total_days = 0
    no_trade_days = 0
    
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
            
        # Apply min_rvol and min_gap filters if specified
        if min_rvol is not None:
            watchlist = [x for x in watchlist if x.get("relative_volume", 0) >= min_rvol]
        if min_gap is not None:
            watchlist = [x for x in watchlist if abs(x.get("gap_pct", 0)) >= min_gap]
            
        # Simulate intraday stream execution for watchlist items
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
            pending_entry_idx = -1
            entry_price = None
            stop_loss = None
            be_triggered = False
            position_size = 0
            risk_dollar = account_value * risk_reviewer.risk_budget_pct
            
            # Liquidity metrics
            spread_proxy = item.get("spread_proxy", 0.01) * spread_widening_coeff
            dollar_volume = item.get("dollar_volume", 10000000.0)
            slippage_bps = max(2.0, min(50.0, 5.0 * (spread_proxy / 0.01)))
            
            entry_idx = 0
            max_high_during_trade = 0.0
            min_low_during_trade = 999999.0
            
            # Cumulative tracking variables for causal daily VWAP
            cumulative_volume = 0.0
            cumulative_price_vol = 0.0
            
            for idx, bar in enumerate(bars):
                ts_str = bar.get("t")
                dt = datetime.datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=pytz.utc).astimezone(pytz.timezone("US/Eastern"))
                price = bar.get("c", 0.0)
                
                # Compute true cumulative daily VWAP causally
                vol = bar.get("v", 0.0)
                bar_vwap = bar.get("vw", price)
                cumulative_volume += vol
                cumulative_price_vol += bar_vwap * vol
                vwap = (cumulative_price_vol / cumulative_volume) if cumulative_volume > 0 else price
                
                # ORB Period evaluation
                elapsed_minutes = (dt.hour * 60 + dt.minute) - 570
                if 0 <= elapsed_minutes < orb_minutes:
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
                            target_idx = idx + entry_timing_shift
                            if target_idx <= idx:
                                # Anticipatory or immediate entry
                                target_idx = max(0, target_idx)
                                entry_bar = bars[target_idx]
                                entry_price = entry_bar.get("h", price) if adversarial_mode else entry_bar.get("c", price)
                                stop_loss = entry_price - stop_distance
                                state = "ACTIVE"
                                be_triggered = False
                                entry_idx = target_idx
                                max_high_during_trade = entry_bar.get("h", entry_price)
                                min_low_during_trade = entry_bar.get("l", entry_price)
                                print(f"  [+] {dt.strftime('%H:%M')} (Shifted Entry) - ENTRY: {sym} @ {entry_price:.2f} | Stop: {stop_loss:.2f}")
                            else:
                                # Delayed entry
                                state = "PENDING_ENTRY"
                                pending_entry_idx = target_idx
                                
                elif state == "PENDING_ENTRY" and idx >= pending_entry_idx:
                    # Execute pending entry
                    stop_distance = atr * risk_reviewer.stop_atr_multiplier
                    position_size = int(risk_dollar / stop_distance) if stop_distance > 0 else 0
                    if position_size > 0:
                        entry_price = bar.get("h", price) if adversarial_mode else price
                        stop_loss = entry_price - stop_distance
                        state = "ACTIVE"
                        be_triggered = False
                        entry_idx = idx
                        max_high_during_trade = bar.get("h", price)
                        min_low_during_trade = bar.get("l", price)
                        print(f"  [+] {dt.strftime('%H:%M')} (Delayed Entry) - ENTRY: {sym} @ {price:.2f} | Stop: {stop_loss:.2f}")
                    else:
                        state = "WAITING_ORB"
                            
                            
                # Exit Evaluation
                elif state == "ACTIVE":
                    h_val = bar.get("h", price)
                    l_val = bar.get("l", price)
                    if h_val > max_high_during_trade:
                        max_high_during_trade = h_val
                    if l_val < min_low_during_trade:
                        min_low_during_trade = l_val
                        
                    if price < stop_loss:
                        exit_price = l_val if adversarial_mode else price
                        realized_pnl = (exit_price - entry_price) * position_size
                        r_multiple = realized_pnl / risk_dollar
                        
                        latency = float(np.random.lognormal(mean=np.log(150), sigma=0.5)) if random_latency_spread else float(random.randint(50, 250))
                        if random_latency_spread:
                            vol_factor = (bar.get("h", price) - bar.get("l", price)) / (price * 0.002) if price > 0 else 1.0
                            vol_factor = max(0.5, min(5.0, vol_factor))
                            latency_factor = max(1.0, latency / 150.0)
                            adjusted_slippage_bps = slippage_bps * vol_factor * latency_factor
                        else:
                            adjusted_slippage_bps = slippage_bps
                            
                        slippage_drag = (exit_price * position_size) * (adjusted_slippage_bps / 10000.0)
                        net_pnl = realized_pnl - slippage_drag
                        
                        account_value += net_pnl
                        state = "COMPLETED"
                        
                        hold_dur = idx - entry_idx
                        mfe_pct = ((max_high_during_trade - entry_price) / entry_price) * 100.0
                        mae_pct = ((entry_price - min_low_during_trade) / entry_price) * 100.0
                        latency = float(latency)
                        
                        r_info = regime_map.get(date_str, {"regime": "CHOPPY_ROTATIONAL", "is_transition": False})
                        trade_entry = {
                            "date": date_str,
                            "ticker": sym,
                            "type": "StopLoss",
                            "pnl": net_pnl,
                            "r_multiple": r_multiple,
                            "status": "Loss" if net_pnl < 0 else "Win",
                            "catalyst_type": item.get("event_type", "Quant Gap Mover"),
                            "gap_pct": item.get("gap_pct", 0.0),
                            "relative_volume": item.get("relative_volume", 1.0),
                            "entry_trigger_type": "ORB Breakout",
                            "hold_duration": hold_dur,
                            "mfe": mfe_pct,
                            "mae": mae_pct,
                            "slippage_estimate": slippage_drag,
                            "execution_latency_estimate": latency,
                            "stop_out_reason": "StopLoss",
                            "take_profit_trigger_status": "Not Triggered",
                            "dollar_volume": dollar_volume,
                            "spread_proxy": spread_proxy,
                            "regime": r_info["regime"],
                            "is_transition": r_info["is_transition"],
                            "epoch": "Phase_35"
                        }
                        trade_logs.append(trade_entry)
                        if log_to_csv:
                            log_trade_to_files(trade_entry)
                        
                        print(f"  [-] {dt.strftime('%H:%M')} - STOP-OUT: {sym} @ {exit_price:.2f} | Net PnL: {net_pnl:.2f} ({r_multiple:.2f}R)")
                        break
                        
                    # Check Take Profit
                    tp_mult = risk_reviewer.take_profit_ratio
                    if tp_mult is not None:
                        target_price = entry_price + (atr * risk_reviewer.stop_atr_multiplier * tp_mult)
                        if price >= target_price:
                            exit_price = l_val if adversarial_mode else price
                            realized_pnl = (exit_price - entry_price) * position_size
                            r_multiple = realized_pnl / risk_dollar
                            
                            latency = float(np.random.lognormal(mean=np.log(150), sigma=0.5)) if random_latency_spread else float(random.randint(50, 250))
                            if random_latency_spread:
                                vol_factor = (bar.get("h", price) - bar.get("l", price)) / (price * 0.002) if price > 0 else 1.0
                                vol_factor = max(0.5, min(5.0, vol_factor))
                                latency_factor = max(1.0, latency / 150.0)
                                adjusted_slippage_bps = slippage_bps * vol_factor * latency_factor
                            else:
                                adjusted_slippage_bps = slippage_bps
                                
                            slippage_drag = (exit_price * position_size) * (adjusted_slippage_bps / 10000.0)
                            net_pnl = realized_pnl - slippage_drag
                            
                            account_value += net_pnl
                            state = "COMPLETED"
                            
                            hold_dur = idx - entry_idx
                            mfe_pct = ((max_high_during_trade - entry_price) / entry_price) * 100.0
                            mae_pct = ((entry_price - min_low_during_trade) / entry_price) * 100.0
                            latency = float(latency)
                            
                            r_info = regime_map.get(date_str, {"regime": "CHOPPY_ROTATIONAL", "is_transition": False})
                            trade_entry = {
                                "date": date_str,
                                "ticker": sym,
                                "type": "TakeProfit",
                                "pnl": net_pnl,
                                "r_multiple": r_multiple,
                                "status": "Loss" if net_pnl < 0 else "Win",
                                "catalyst_type": item.get("event_type", "Quant Gap Mover"),
                                "gap_pct": item.get("gap_pct", 0.0),
                                "relative_volume": item.get("relative_volume", 1.0),
                                "entry_trigger_type": "ORB Breakout",
                                "hold_duration": hold_dur,
                                "mfe": mfe_pct,
                                "mae": mae_pct,
                                "slippage_estimate": slippage_drag,
                                "execution_latency_estimate": latency,
                                "stop_out_reason": "TakeProfit",
                                "take_profit_trigger_status": "Triggered",
                                "dollar_volume": dollar_volume,
                                "spread_proxy": spread_proxy,
                                "regime": r_info["regime"],
                                "is_transition": r_info["is_transition"],
                                "epoch": "Phase_35"
                            }
                            trade_logs.append(trade_entry)
                            if log_to_csv:
                                log_trade_to_files(trade_entry)
                            
                            print(f"  [-] {dt.strftime('%H:%M')} - TAKE-PROFIT: {sym} @ {exit_price:.2f} | Net PnL: {net_pnl:.2f} ({r_multiple:.2f}R)")
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
                        exit_price = l_val if adversarial_mode else price
                        realized_pnl = (exit_price - entry_price) * position_size
                        r_multiple = realized_pnl / risk_dollar
                        
                        latency = float(np.random.lognormal(mean=np.log(150), sigma=0.5)) if random_latency_spread else float(random.randint(50, 250))
                        if random_latency_spread:
                            vol_factor = (bar.get("h", price) - bar.get("l", price)) / (price * 0.002) if price > 0 else 1.0
                            vol_factor = max(0.5, min(5.0, vol_factor))
                            latency_factor = max(1.0, latency / 150.0)
                            adjusted_slippage_bps = slippage_bps * vol_factor * latency_factor
                        else:
                            adjusted_slippage_bps = slippage_bps
                            
                        slippage_drag = (exit_price * position_size) * (adjusted_slippage_bps / 10000.0)
                        net_pnl = realized_pnl - slippage_drag
                        
                        account_value += net_pnl
                        state = "COMPLETED"
                        
                        hold_dur = idx - entry_idx
                        mfe_pct = ((max_high_during_trade - entry_price) / entry_price) * 100.0
                        mae_pct = ((entry_price - min_low_during_trade) / entry_price) * 100.0
                        latency = float(latency)
                        
                        r_info = regime_map.get(date_str, {"regime": "CHOPPY_ROTATIONAL", "is_transition": False})
                        trade_entry = {
                            "date": date_str,
                            "ticker": sym,
                            "type": "EOD",
                            "pnl": net_pnl,
                            "r_multiple": r_multiple,
                            "status": "Loss" if net_pnl < 0 else "Win",
                            "catalyst_type": item.get("event_type", "Quant Gap Mover"),
                            "gap_pct": item.get("gap_pct", 0.0),
                            "relative_volume": item.get("relative_volume", 1.0),
                            "entry_trigger_type": "ORB Breakout",
                            "hold_duration": hold_dur,
                            "mfe": mfe_pct,
                            "mae": mae_pct,
                            "slippage_estimate": slippage_drag,
                            "execution_latency_estimate": latency,
                            "stop_out_reason": "EndOfDay",
                            "take_profit_trigger_status": "Not Triggered",
                            "dollar_volume": dollar_volume,
                            "spread_proxy": spread_proxy,
                            "regime": r_info["regime"],
                            "is_transition": r_info["is_transition"],
                            "epoch": "Phase_35"
                        }
                        trade_logs.append(trade_entry)
                        if log_to_csv:
                            log_trade_to_files(trade_entry)
                        
                        print(f"  [-] {dt.strftime('%H:%M')} - EOD EXIT: {sym} @ {exit_price:.2f} | Net PnL: {net_pnl:.2f} ({r_multiple:.2f}R)")
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
    
    return trade_logs

if __name__ == "__main__":
    # Simulate a 1-month period (e.g. 2026-03-01 to 2026-04-01) for verification
    # By default, use_llm=False is active to prevent API quota usage. 
    # Set use_llm=True to test the full Catalyst sentiment extractor.
    run_multi_day_backtest("2026-03-01", "2026-04-01", use_llm=False)
