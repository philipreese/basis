import os
import json
import datetime
import pandas as pd
import numpy as np
from dotenv import load_dotenv

# Ensure environment is loaded
load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

class CausalFeatureLab:
    """
    Causal Feature Discovery Lab.
    Generates strictly causal features and future outcome labels.
    """
    def __init__(self, out_dir="out"):
        self.out_dir = out_dir
        self.daily_cache_dir = os.path.join(out_dir, "daily_bars_cache")
        os.makedirs(self.daily_cache_dir, exist_ok=True)
        self.client = None

    def _get_alpaca_client(self):
        if self.client is None:
            if not API_KEY or not SECRET_KEY:
                # No keys, we'll try to run fully offline
                return None
            from alpaca.data.historical import StockHistoricalDataClient
            self.client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
        return self.client

    def get_daily_bars(self, ticker, start_date_str, end_date_str) -> pd.DataFrame:
        """
        Loads daily bars for a ticker from cache, or fetches from Alpaca if offline is not enforced
        or data is missing.
        """
        cache_file = os.path.join(self.daily_cache_dir, f"{ticker}_daily.csv")
        
        # Load from cache if exists
        df_cached = None
        if os.path.exists(cache_file):
            try:
                df_cached = pd.read_csv(cache_file, parse_dates=["date"])
                df_cached.set_index("date", inplace=True)
            except Exception as e:
                print(f"[!] Error loading daily cache for {ticker}: {e}")

        # Determine if we need to fetch new data (prefer local cache to remain offline)
        start_dt = pd.to_datetime(start_date_str)
        end_dt = pd.to_datetime(end_date_str)
        
        needs_fetch = False
        if df_cached is None or df_cached.empty:
            needs_fetch = True

        if needs_fetch:
            client = self._get_alpaca_client()
            if client is not None:
                print(f"[*] Fetching daily bars for {ticker} from Alpaca...")
                from alpaca.data.requests import StockBarsRequest
                from alpaca.data.timeframe import TimeFrame
                # Fetch with some warm-up padding (e.g. 350 calendar days before start_date)
                pad_start = start_dt - datetime.timedelta(days=350)
                req = StockBarsRequest(
                    symbol_or_symbols=ticker,
                    timeframe=TimeFrame.Day,
                    start=pad_start,
                    end=end_dt
                )
                try:
                    res = client.get_stock_bars(req)
                    bars = res.data.get(ticker, [])
                    if bars:
                        new_data = []
                        for b in bars:
                            new_data.append({
                                "date": b.timestamp.date(),
                                "open": float(b.open),
                                "high": float(b.high),
                                "low": float(b.low),
                                "close": float(b.close),
                                "volume": float(b.volume)
                            })
                        df_new = pd.DataFrame(new_data)
                        df_new["date"] = pd.to_datetime(df_new["date"])
                        df_new.set_index("date", inplace=True)
                        
                        # Merge with cache if we had some cached data
                        if df_cached is not None and not df_cached.empty:
                            df_merged = df_new.combine_first(df_cached)
                        else:
                            df_merged = df_new
                            
                        # Save back to cache
                        df_merged.to_csv(cache_file)
                        df_cached = df_merged
                        print(f"[+] Cached daily bars for {ticker} ({len(df_cached)} total rows).")
                except Exception as e:
                    print(f"[!] Error fetching daily bars for {ticker}: {e}")
            else:
                if df_cached is None or df_cached.empty:
                    raise ValueError(f"No cached daily bars for {ticker} and Alpaca client is unavailable.")
                else:
                    print(f"[!] Offline mode: Using cached daily bars for {ticker} up to {df_cached.index.max().strftime('%Y-%m-%d')}")

        if df_cached is None or df_cached.empty:
            return pd.DataFrame()
            
        # Filter to requested period
        df_filtered = df_cached.loc[start_dt:end_dt].copy()
        return df_filtered

    def compute_features(self, ticker, date_str, minute_bars, daily_df, spy_daily_df, quant_watchlist_item) -> dict:
        """
        Computes all causal features at checkpoint T (09:35 US/Eastern).
        Returns a dict of feature names and values.
        """
        if len(minute_bars) < 5:
            # Not enough regular session data
            return {}

        # 1. Parse bars in the opening session (first 5 minutes: 09:30 to 09:34)
        opening_bars = minute_bars[:5]
        
        # 2. Get daily references for stock
        date_obj = pd.to_datetime(date_str)
        daily_before = daily_df.loc[daily_df.index < date_obj]
        if daily_before.empty:
            return {}
            
        yesterday_row = daily_before.iloc[-1]
        close_yesterday = yesterday_row["close"]
        high_yesterday = yesterday_row["high"]
        low_yesterday = yesterday_row["low"]
        open_yesterday = yesterday_row["open"]
        range_yesterday = high_yesterday - low_yesterday

        # Today's daily bar (to compare outcomes or check values if needed, but features must be causal)
        today_open = opening_bars[0]["o"]

        # Calculate daily ATR (14-period) up to yesterday
        highs = daily_before["high"]
        lows = daily_before["low"]
        closes_prev = daily_before["close"].shift(1)
        tr = pd.concat([highs - lows, (highs - closes_prev).abs(), (closes_prev - lows).abs()], axis=1).max(axis=1)
        rolling_atr = tr.rolling(window=14).mean()
        if rolling_atr.empty or pd.isna(rolling_atr.iloc[-1]):
            atr_14 = range_yesterday if range_yesterday > 0 else 1.0
        else:
            atr_14 = rolling_atr.iloc[-1]

        # 3. PREMARKET FEATURES
        gap_pct = quant_watchlist_item.get("gap_pct", ((today_open - close_yesterday) / close_yesterday * 100.0) if close_yesterday > 0 else 0.0)
        
        # Overnight Realized Volatility: rolling 14-day std of daily returns
        daily_returns = daily_before["close"].pct_change().dropna()
        overnight_vol = daily_returns.rolling(window=14).std().iloc[-1] * 100.0 if len(daily_returns) >= 14 else 1.0
        if pd.isna(overnight_vol):
            overnight_vol = 1.0
            
        premarket_rvol = quant_watchlist_item.get("relative_volume", 1.0)
        
        # Premarket range compression proxy: gap divided by ATR
        premarket_range_comp = abs(gap_pct) / (atr_14 / close_yesterday * 100.0) if close_yesterday > 0 and atr_14 > 0 else 1.0
        
        # Overnight SPY relative strength
        spy_before = spy_daily_df.loc[spy_daily_df.index <= date_obj]
        if len(spy_before) >= 2:
            spy_yesterday = spy_before.iloc[-2]
            spy_today = spy_before.iloc[-1]
            spy_gap = ((spy_today["open"] - spy_yesterday["close"]) / spy_yesterday["close"] * 100.0) if spy_yesterday["close"] > 0 else 0.0
            overnight_spy_rel_strength = gap_pct - spy_gap
        else:
            overnight_spy_rel_strength = 0.0

        # 4. OPENING SESSION FEATURES (first 5 bars: 09:30 to 09:34)
        c_0930 = opening_bars[0]["c"]
        o_0930 = opening_bars[0]["o"]
        first_1m_return = ((c_0930 - o_0930) / o_0930 * 100.0) if o_0930 > 0 else 0.0
        
        high_5m = max(b["h"] for b in opening_bars)
        low_5m = min(b["l"] for b in opening_bars)
        range_5m = high_5m - low_5m
        first_5m_range_expansion = range_5m / atr_14 if atr_14 > 0 else 0.0
        
        opening_range_comp = range_5m / range_yesterday if range_yesterday > 0 else 1.0
        
        # VWAP distance
        vols_5m = [b["v"] for b in opening_bars]
        sum_vol = sum(vols_5m)
        if sum_vol > 0:
            vwap_5m = sum(b["v"] * b.get("vw", b["c"]) for b in opening_bars) / sum_vol
        else:
            vwap_5m = opening_bars[-1]["c"]
        c_0934 = opening_bars[-1]["c"]
        vwap_dist = ((c_0934 - vwap_5m) / vwap_5m * 100.0) if vwap_5m > 0 else 0.0
        
        # Rolling intraday volatility: std of 1m returns
        intraday_returns = []
        for i in range(1, len(opening_bars)):
            prev_c = opening_bars[i-1]["c"]
            curr_c = opening_bars[i]["c"]
            intraday_returns.append((curr_c - prev_c) / prev_c)
        rolling_intraday_vol = np.std(intraday_returns) * 100.0 if intraday_returns else 0.0
        
        # Spread proxy
        spread_proxy = np.mean([(b["h"] - b["l"]) / b["c"] for b in opening_bars]) if opening_bars else 0.01
        
        # Liquidity proxy
        liq_proxy = sum_vol / 5.0
        
        # Dollar volume
        dollar_vol = sum(b["v"] * b["c"] for b in opening_bars)
        
        # Relative volume acceleration: vol of 5th bar / average of first 4
        vols_first_4 = vols_5m[:4]
        avg_vol_4 = sum(vols_first_4) / 4.0 if vols_first_4 else 1.0
        vol_accel = vols_5m[-1] / avg_vol_4 if avg_vol_4 > 0 else 1.0

        # 5. STRUCTURAL FEATURES
        # ATR expansion ratio: atr_14 / 20-day average daily ATR
        rolling_atr_20 = tr.rolling(window=20).mean()
        atr_20_val = rolling_atr_20.iloc[-1] if not rolling_atr_20.empty and not pd.isna(rolling_atr_20.iloc[-1]) else atr_14
        atr_expansion = atr_14 / atr_20_val if atr_20_val > 0 else 1.0
        
        prior_trend_eff = (close_yesterday - open_yesterday) / range_yesterday if range_yesterday > 0 else 0.0
        prior_range_ext = range_yesterday / atr_14 if atr_14 > 0 else 1.0
        
        dist_prev_high = ((today_open - high_yesterday) / high_yesterday * 100.0) if high_yesterday > 0 else 0.0
        dist_prev_low = ((today_open - low_yesterday) / low_yesterday * 100.0) if low_yesterday > 0 else 0.0
        
        # Rolling momentum persistence: rolling average return / standard dev (last 5 days)
        last_5_returns = daily_returns.iloc[-5:] if len(daily_returns) >= 5 else daily_returns
        if not last_5_returns.empty:
            mean_ret = last_5_returns.mean()
            std_ret = last_5_returns.std()
            mom_persistence = mean_ret / std_ret if std_ret > 0 else 0.0
        else:
            mom_persistence = 0.0
            
        # Realized Volatility Percentile: 20-day volatility percentile in the last 252 days
        hist_vol_20 = daily_returns.rolling(window=20).std() * np.sqrt(252) * 100.0
        hist_vol_20 = hist_vol_20.dropna()
        if len(hist_vol_20) >= 20:
            current_vol_20 = hist_vol_20.iloc[-1]
            last_year_vols = hist_vol_20.iloc[-252:]
            vol_percentile = (last_year_vols < current_vol_20).mean() * 100.0
        else:
            vol_percentile = 50.0

        # 6. REGIME FEATURES
        regime_row = spy_daily_df.loc[spy_daily_df.index <= date_obj]
        if not regime_row.empty:
            market_regime = regime_row.iloc[-1].get("regime", "CHOPPY_ROTATIONAL")
            is_transition = int(regime_row.iloc[-1].get("is_transition", False))
            
            # Regime duration
            regimes_history = spy_daily_df.loc[spy_daily_df.index <= date_obj, "regime"]
            duration = 0
            if not regimes_history.empty:
                current_reg = regimes_history.iloc[-1]
                for r in regimes_history.iloc[::-1]:
                    if r == current_reg:
                        duration += 1
                    else:
                        break
            regime_duration = duration
        else:
            market_regime = "CHOPPY_ROTATIONAL"
            is_transition = 0
            regime_duration = 1

        features = {
            "ticker": ticker,
            "date": date_str,
            "gap_pct": gap_pct,
            "overnight_volatility": overnight_vol,
            "premarket_relative_volume": premarket_rvol,
            "premarket_range_compression": premarket_range_comp,
            "overnight_spy_relative_strength": overnight_spy_rel_strength,
            "first_1m_return": first_1m_return,
            "first_5m_range_expansion": first_5m_range_expansion,
            "opening_range_compression_ratio": opening_range_comp,
            "vwap_distance": vwap_dist,
            "rolling_intraday_volatility": rolling_intraday_vol,
            "spread_proxy": spread_proxy,
            "liquidity_proxy": liq_proxy,
            "dollar_volume": dollar_vol,
            "relative_volume_acceleration": vol_accel,
            "atr_expansion_ratio": atr_expansion,
            "prior_day_trend_efficiency": prior_trend_eff,
            "prior_day_range_extension": prior_range_ext,
            "distance_from_prev_high": dist_prev_high,
            "distance_from_prev_low": dist_prev_low,
            "rolling_momentum_persistence": mom_persistence,
            "realized_volatility_percentile": vol_percentile,
            "market_regime": market_regime,
            "is_transition": is_transition,
            "rolling_regime_duration": regime_duration,
            "atr_14": atr_14
        }
        return features

    def compute_outcomes(self, minute_bars, features, apply_friction=False, delayed_entry_offset=0, worst_case_fills=False) -> dict:
        """
        Computes outcomes strictly looking forward from checkpoint T (09:35 US/Eastern).
        Evaluates at index 5 + delayed_entry_offset.
        """
        if len(minute_bars) < 6:
            return {}

        c_T = minute_bars[4]["c"] # 09:34 close
        atr_14 = features.get("atr_14", 1.0)
        spread = features.get("spread_proxy", 0.01)

        # Base entry configuration
        entry_idx = 5 + delayed_entry_offset
        if entry_idx >= len(minute_bars):
            return {}
            
        entry_bar = minute_bars[entry_idx]
        
        # Entry price selection
        if worst_case_fills:
            entry_price = entry_bar["h"] # worst fill for buy is highest price
        else:
            entry_price = entry_bar["c"]

        # Friction drag details
        fee_bps = 1.5
        slippage_bps = 0.0
        if apply_friction:
            # Volatility-scaled slippage
            slippage_bps = max(2.0, min(50.0, 5.0 * (spread / 0.01)))
            # Adjust entry price higher by drag for long entry
            drag_factor = 1.0 + (fee_bps + slippage_bps) / 10000.0
            entry_price = entry_price * drag_factor

        outcomes = {}
        horizons = [5, 15, 30, 60]
        
        # Future returns
        for h in horizons:
            exit_idx = entry_idx + h
            if exit_idx < len(minute_bars):
                exit_bar = minute_bars[exit_idx]
                exit_price = exit_bar["c"]
                if worst_case_fills:
                    exit_price = exit_bar["l"] # worst fill for sell is lowest price
                if apply_friction:
                    # Adjust exit price lower by drag
                    drag_factor = 1.0 - (fee_bps + slippage_bps) / 10000.0
                    exit_price = exit_price * drag_factor
                ret = ((exit_price - entry_price) / entry_price) * 100.0
            else:
                ret = np.nan
            outcomes[f"future_{h}m_return"] = ret

        # Future excursions (next 60 minutes)
        excursion_bars = minute_bars[entry_idx : entry_idx + 60]
        if excursion_bars:
            max_h = max(b["h"] for b in excursion_bars)
            min_l = min(b["l"] for b in excursion_bars)
            outcomes["future_max_excursion"] = ((max_h - entry_price) / entry_price) * 100.0
            outcomes["future_min_excursion"] = ((min_l - entry_price) / entry_price) * 100.0
        else:
            outcomes["future_max_excursion"] = np.nan
            outcomes["future_min_excursion"] = np.nan

        # Target hit logic (1R target vs 1R stop)
        # 1R target is 1.5x ATR, stop is 1.5x ATR
        r_dist = 1.5 * atr_14
        target_price = entry_price + r_dist
        stop_price = entry_price - r_dist
        
        hit_target = False
        hit_stop = False
        
        remaining_bars = minute_bars[entry_idx:]
        for b in remaining_bars:
            if b["h"] >= target_price and not hit_stop:
                hit_target = True
                break
            if b["l"] <= stop_price and not hit_target:
                hit_stop = True
                break
                
        outcomes["hit_1r_before_minus_1r"] = 1.0 if hit_target else 0.0

        # Hit +X Sigma logic
        # Sigma = rolling intraday volatility (percent)
        sigma = features.get("rolling_intraday_volatility", 0.0)
        # target of +2.0 Sigma
        sigma_dist = 2.0 * entry_price * (sigma / 100.0)
        target_sigma = entry_price + sigma_dist
        
        hit_sigma = False
        for b in remaining_bars:
            if b["h"] >= target_sigma:
                hit_sigma = True
                break
        outcomes["hit_plus_x_sigma"] = 1.0 if hit_sigma else 0.0

        return outcomes
