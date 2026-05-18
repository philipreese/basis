import os
import json
import math
from datetime import datetime

def load_configs():
    """Loads asset archetypes and configurations."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    config_dir = os.path.join(project_root, "config")
    
    assets_path = os.path.join(config_dir, "assets.json")
    archetypes_path = os.path.join(config_dir, "archetypes.json")
    
    with open(assets_path, "r") as f:
        assets_config = json.load(f)
        
    with open(archetypes_path, "r") as f:
        archetypes_config = json.load(f)
        
    return assets_config, archetypes_config

def resolve_asset_parameters(symbol: str, assets_config: dict, archetypes_config: dict) -> dict:
    """Resolves configuration parameters for a given symbol."""
    archetype_key = assets_config.get(symbol, "BROAD_MARKET_MEAN_REVERSION")
    return archetypes_config.get(archetype_key, {
        "volatility_gate_limit": 0.035,
        "fatigue_multiplier": -0.1,
        "slippage_atr_coefficient": 0.1
    })

def execute_backtest(stops_mode: str):
    """Executes the simulation logic for:
       - 'none': Phase 20 (Unprotected baseline)
       - 'trailing': Phase 21 (Trailing stops + Break-even floor)
       - 'structural_raw': Phase 22 (Raw Macro Anchor stops + Break-even floor + Take Profit ceiling)
       - 'structural_buffered': Phase 23 (Buffered Macro Anchor stops + 3-Bar Gate + Break-even floor + Take Profit ceiling)
    """
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    journal_path = os.path.join(project_root, "out", "replay_journal.jsonl")
    
    if not os.path.exists(journal_path):
        raise FileNotFoundError(f"Replay journal not found at {journal_path}. Please run replay_engine.py first.")
        
    assets_config, archetypes_config = load_configs()
    
    # 1. Ingest journal rows and group by symbol
    raw_events_by_symbol = {}
    with open(journal_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            event = json.loads(line)
            symbol = event["symbol"]
            if symbol not in raw_events_by_symbol:
                raw_events_by_symbol[symbol] = []
            raw_events_by_symbol[symbol].append(event)
            
    # Sort events per symbol chronologically
    for symbol in raw_events_by_symbol:
        raw_events_by_symbol[symbol].sort(key=lambda x: x["timestamp"])
        
    symbol_histories = {}
    symbol_diagnostics = {}
    
    for symbol, events in raw_events_by_symbol.items():
        params = resolve_asset_parameters(symbol, assets_config, archetypes_config)
        slippage_coef = params.get("slippage_atr_coefficient", 0.1)
        
        # State variables for Strategy
        cash = 100000.0
        units = 0.0
        state = "FLAT"
        last_transition_idx = -2
        trades = []
        current_trade = None
        
        # Stateful stop loss variables
        entry_atr = 0.0
        high_water_mark = 0.0
        break_even_activated = False
        break_even_floor_value = 0.0
        pending_stop_exit = False
        pending_stop_reason = ""
        consecutive_breaches = 0
        
        # State variables for Buy & Hold
        bh_cash = 100000.0
        bh_units = 0.0
        bh_entered = False
        
        history = {}  # timestamp -> state info
        
        for idx, event in enumerate(events):
            timestamp = event["timestamp"]
            close = float(event["metrics"]["current_price"])
            open_price = float(event["metrics"]["open"])
            high = float(event["metrics"]["high"])
            atr = float(event["metrics"]["atr_14"])
            action = event["suggested_action"]
            confidence = float(event["base_confidence"])
            
            # --- Buy & Hold Execution ---
            if not bh_entered:
                slippage = atr * slippage_coef
                eff_price = close + slippage
                fee = bh_cash * 0.00015
                bh_units = (bh_cash - fee) / eff_price
                bh_cash = 0.0
                bh_entered = True
            bh_equity = bh_cash + bh_units * close
            
            # --- Active Strategy Execution ---
            # Check next-bar open liquidation if triggered on previous bar
            if pending_stop_exit:
                slippage = atr * slippage_coef
                eff_price = open_price - slippage
                gross_proceeds = units * eff_price
                fee = gross_proceeds * 0.00015
                cash = gross_proceeds - fee
                state = "FLAT"
                last_transition_idx = idx
                
                if current_trade:
                    current_trade["exit_idx"] = idx
                    current_trade["exit_timestamp"] = timestamp
                    current_trade["exit_price"] = open_price
                    current_trade["exit_effective_price"] = eff_price
                    current_trade["exit_fee"] = fee
                    current_trade["pnl"] = cash - current_trade["entry_cash"]
                    current_trade["holding_bars"] = idx - current_trade["entry_idx"]
                    current_trade["exit_reason"] = pending_stop_reason
                    trades.append(current_trade)
                    current_trade = None
                    
                units = 0.0
                pending_stop_exit = False
                break_even_activated = False
                entry_atr = 0.0
                high_water_mark = 0.0
                break_even_floor_value = 0.0
                pending_stop_reason = ""
                consecutive_breaches = 0
                
            bars_since_last_transition = idx - last_transition_idx
            
            # Evaluate stops if position is LONG
            if state == "LONG" and not pending_stop_exit:
                if stops_mode == "trailing":
                    # Update continuous variables
                    high_water_mark = max(high_water_mark, close)
                    
                    # Check Break-Even Floor activation
                    if not break_even_activated and close >= (current_trade["entry_price"] + 1.5 * entry_atr):
                        break_even_activated = True
                        break_even_floor_value = current_trade["entry_price"] * (1.0 + 0.00015)
                        
                    # Volatility Trailing Stop
                    trailing_stop = high_water_mark - (3.0 * atr)
                    
                    # Trigger Stop exit if close falls below stops
                    if close < trailing_stop or (break_even_activated and close < break_even_floor_value):
                        pending_stop_exit = True
                        pending_stop_reason = "StopLoss"
                        
                elif stops_mode == "structural_raw":
                    # 1. Convexity Capture Limit (Take Profit Ceiling) - Immediate Intra-bar Liquidation
                    tp_ceiling = current_trade["entry_price"] + (5.0 * entry_atr)
                    if high >= tp_ceiling:
                        # Immediate intra-bar liquidation at the exact ceiling price
                        eff_price = tp_ceiling
                        gross_proceeds = units * eff_price
                        fee = gross_proceeds * 0.00015
                        cash = gross_proceeds - fee
                        state = "FLAT"
                        last_transition_idx = idx
                        
                        if current_trade:
                            current_trade["exit_idx"] = idx
                            current_trade["exit_timestamp"] = timestamp
                            current_trade["exit_price"] = tp_ceiling
                            current_trade["exit_effective_price"] = eff_price
                            current_trade["exit_fee"] = fee
                            current_trade["pnl"] = cash - current_trade["entry_cash"]
                            current_trade["holding_bars"] = idx - current_trade["entry_idx"]
                            current_trade["exit_reason"] = "TakeProfit"
                            trades.append(current_trade)
                            current_trade = None
                            
                        units = 0.0
                        pending_stop_exit = False
                        break_even_activated = False
                        entry_atr = 0.0
                        high_water_mark = 0.0
                        break_even_floor_value = 0.0
                        pending_stop_reason = ""
                        
                        # Set current strategy equity and record to history
                        strategy_equity = cash
                        history[timestamp] = {
                            "close": close,
                            "strategy_cash": cash,
                            "strategy_units": units,
                            "strategy_state": state,
                            "strategy_equity": strategy_equity,
                            "bh_cash": bh_cash,
                            "bh_units": bh_units,
                            "bh_equity": bh_equity
                        }
                        continue
                        
                    # 2. Break-Even Floor (Retained)
                    if not break_even_activated and close >= (current_trade["entry_price"] + 1.5 * entry_atr):
                        break_even_activated = True
                        break_even_floor_value = current_trade["entry_price"] * (1.0 + 0.00015)
                        
                    # 3. Macro Anchor Stop (close < sma_macro)
                    sma_macro = float(event["metrics"]["sma_macro"])
                    if close < sma_macro or (break_even_activated and close < break_even_floor_value):
                        pending_stop_exit = True
                        pending_stop_reason = "AnchorStop"
                        
                elif stops_mode == "structural_buffered":
                    # 1. Convexity Capture Limit (Take Profit Ceiling) - Immediate Intra-bar Liquidation
                    tp_ceiling = current_trade["entry_price"] + (5.0 * entry_atr)
                    if high >= tp_ceiling:
                        # Immediate intra-bar liquidation at the exact ceiling price
                        eff_price = tp_ceiling
                        gross_proceeds = units * eff_price
                        fee = gross_proceeds * 0.00015
                        cash = gross_proceeds - fee
                        state = "FLAT"
                        last_transition_idx = idx
                        
                        if current_trade:
                            current_trade["exit_idx"] = idx
                            current_trade["exit_timestamp"] = timestamp
                            current_trade["exit_price"] = tp_ceiling
                            current_trade["exit_effective_price"] = eff_price
                            current_trade["exit_fee"] = fee
                            current_trade["pnl"] = cash - current_trade["entry_cash"]
                            current_trade["holding_bars"] = idx - current_trade["entry_idx"]
                            current_trade["exit_reason"] = "TakeProfit"
                            trades.append(current_trade)
                            current_trade = None
                            
                        units = 0.0
                        pending_stop_exit = False
                        break_even_activated = False
                        entry_atr = 0.0
                        high_water_mark = 0.0
                        break_even_floor_value = 0.0
                        pending_stop_reason = ""
                        consecutive_breaches = 0
                        
                        # Set current strategy equity and record to history
                        strategy_equity = cash
                        history[timestamp] = {
                            "close": close,
                            "strategy_cash": cash,
                            "strategy_units": units,
                            "strategy_state": state,
                            "strategy_equity": strategy_equity,
                            "bh_cash": bh_cash,
                            "bh_units": bh_units,
                            "bh_equity": bh_equity
                        }
                        continue
                        
                    # 2. Break-Even Floor (Retained)
                    if not break_even_activated and close >= (current_trade["entry_price"] + 1.5 * entry_atr):
                        break_even_activated = True
                        break_even_floor_value = current_trade["entry_price"] * (1.0 + 0.00015)
                        
                    # 3. Volatility-Buffered Stop Line and 3-Bar Temporal Persistence Gate
                    sma_macro = float(event["metrics"]["sma_macro"])
                    stop_line = sma_macro - (1.0 * atr)
                    
                    if close < stop_line:
                        consecutive_breaches += 1
                    else:
                        consecutive_breaches = 0
                        
                    # Trigger conditions
                    if consecutive_breaches >= 3:
                        pending_stop_exit = True
                        pending_stop_reason = "AnchorStop"
                    elif break_even_activated and close < break_even_floor_value:
                        pending_stop_exit = True
                        pending_stop_reason = "AnchorStop"
                        
            # Normal transition logic if not stopped out
            if not pending_stop_exit:
                # Transition FLAT -> LONG (Entry)
                if state == "FLAT" and action == "Buy" and confidence > 0.4:
                    if bars_since_last_transition >= 1:
                        slippage = atr * slippage_coef
                        eff_price = close + slippage
                        fee = cash * 0.00015
                        units = (cash - fee) / eff_price
                        cash_spent = cash
                        cash = 0.0
                        state = "LONG"
                        last_transition_idx = idx
                        
                        current_trade = {
                            "entry_idx": idx,
                            "entry_timestamp": timestamp,
                            "entry_price": close,
                            "entry_cash": cash_spent,
                            "entry_units": units,
                            "entry_effective_price": eff_price,
                            "entry_fee": fee
                        }
                        
                        entry_atr = atr
                        high_water_mark = close
                        break_even_activated = False
                        break_even_floor_value = 0.0
                        pending_stop_exit = False
                        pending_stop_reason = ""
                        consecutive_breaches = 0
                            
                # Transition LONG -> FLAT (Tactical Exit)
                elif state == "LONG" and action in ["Hold", "Sell"]:
                    if bars_since_last_transition >= 1:
                        slippage = atr * slippage_coef
                        eff_price = close - slippage
                        gross_proceeds = units * eff_price
                        fee = gross_proceeds * 0.00015
                        cash = gross_proceeds - fee
                        state = "FLAT"
                        last_transition_idx = idx
                        
                        if current_trade:
                            current_trade["exit_idx"] = idx
                            current_trade["exit_timestamp"] = timestamp
                            current_trade["exit_price"] = close
                            current_trade["exit_effective_price"] = eff_price
                            current_trade["exit_fee"] = fee
                            current_trade["pnl"] = cash - current_trade["entry_cash"]
                            current_trade["holding_bars"] = idx - current_trade["entry_idx"]
                            current_trade["exit_reason"] = "Tactical"
                            trades.append(current_trade)
                            current_trade = None
                            
                        units = 0.0
                        
            strategy_equity = cash + units * close
            
            history[timestamp] = {
                "close": close,
                "strategy_cash": cash,
                "strategy_units": units,
                "strategy_state": state,
                "strategy_equity": strategy_equity,
                "bh_cash": bh_cash,
                "bh_units": bh_units,
                "bh_equity": bh_equity
            }
            
        symbol_histories[symbol] = history
        
        # Calculate diagnostics
        trade_count = len(trades)
        avg_holding = sum(t["holding_bars"] for t in trades) / trade_count if trade_count > 0 else 0.0
        
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] <= 0)
        
        symbol_diagnostics[symbol] = {
            "trade_count": trade_count,
            "avg_holding": avg_holding,
            "wins": wins,
            "losses": losses,
            "trades": trades
        }
        
    # 3. Time-Align and Merge
    all_timestamps = sorted(list(set().union(*(h.keys() for h in symbol_histories.values()))))
    merged_history = []
    
    last_known_state = {symbol: None for symbol in symbol_histories}
    first_closes = {symbol: list(symbol_histories[symbol].values())[0]["close"] for symbol in symbol_histories}
    
    for ts in all_timestamps:
        ts_data = {"timestamp": ts}
        total_strategy_equity = 0.0
        total_bh_equity = 0.0
        
        for symbol in symbol_histories:
            history = symbol_histories[symbol]
            if ts in history:
                state = history[ts]
                last_known_state[symbol] = state
            else:
                if last_known_state[symbol] is None:
                    state = {
                        "close": first_closes[symbol],
                        "strategy_cash": 100000.0,
                        "strategy_units": 0.0,
                        "strategy_state": "FLAT",
                        "strategy_equity": 100000.0,
                        "bh_cash": 100000.0,
                        "bh_units": 0.0,
                        "bh_equity": 100000.0
                    }
                else:
                    state = last_known_state[symbol].copy()
                    
            total_strategy_equity += state["strategy_equity"]
            total_bh_equity += state["bh_equity"]
            
        ts_data["total_strategy_equity"] = total_strategy_equity
        ts_data["total_bh_equity"] = total_bh_equity
        merged_history.append(ts_data)
        
    # 4. Compounding Performance Metrics
    initial_strategy_equity = merged_history[0]["total_strategy_equity"]
    final_strategy_equity = merged_history[-1]["total_strategy_equity"]
    strategy_total_return = (final_strategy_equity - initial_strategy_equity) / initial_strategy_equity * 100
    
    initial_bh_equity = merged_history[0]["total_bh_equity"]
    final_bh_equity = merged_history[-1]["total_bh_equity"]
    bh_total_return = (final_bh_equity - initial_bh_equity) / initial_bh_equity * 100
    
    strategy_peak = 0.0
    bh_peak = 0.0
    strategy_mdd = 0.0
    bh_mdd = 0.0
    
    strategy_log_returns = []
    prev_strategy_equity = None
    
    for data in merged_history:
        strat_eq = data["total_strategy_equity"]
        bh_eq = data["total_bh_equity"]
        
        if strat_eq > strategy_peak:
            strategy_peak = strat_eq
        strategy_dd = (strategy_peak - strat_eq) / strategy_peak * 100 if strategy_peak > 0 else 0.0
        data["strategy_drawdown"] = strategy_dd
        if strategy_dd > strategy_mdd:
            strategy_mdd = strategy_dd
            
        if bh_eq > bh_peak:
            bh_peak = bh_eq
        bh_dd = (bh_peak - bh_eq) / bh_peak * 100 if bh_peak > 0 else 0.0
        data["bh_drawdown"] = bh_dd
        if bh_dd > bh_mdd:
            bh_mdd = bh_dd
            
        if prev_strategy_equity is not None:
            r = math.log(strat_eq / prev_strategy_equity)
            strategy_log_returns.append(r)
        prev_strategy_equity = strat_eq
        
    if len(strategy_log_returns) > 1:
        mean_r = sum(strategy_log_returns) / len(strategy_log_returns)
        var_r = sum((r - mean_r) ** 2 for r in strategy_log_returns) / (len(strategy_log_returns) - 1)
        std_r = math.sqrt(var_r)
        strategy_sharpe = (mean_r / std_r) * math.sqrt(1764) if std_r > 0 else 0.0
    else:
        strategy_sharpe = 0.0
        
    bh_log_returns = []
    prev_bh_equity = None
    for data in merged_history:
        bh_eq = data["total_bh_equity"]
        if prev_bh_equity is not None:
            r = math.log(bh_eq / prev_bh_equity)
            bh_log_returns.append(r)
        prev_bh_equity = bh_eq
        
    if len(bh_log_returns) > 1:
        mean_bh = sum(bh_log_returns) / len(bh_log_returns)
        var_bh = sum((r - mean_bh) ** 2 for r in bh_log_returns) / (len(bh_log_returns) - 1)
        std_bh = math.sqrt(var_bh)
        bh_sharpe = (mean_bh / std_bh) * math.sqrt(1764) if std_bh > 0 else 0.0
    else:
        bh_sharpe = 0.0
        
    return {
        "initial_equity": initial_strategy_equity,
        "final_equity": final_strategy_equity,
        "total_return": strategy_total_return,
        "mdd": strategy_mdd,
        "sharpe": strategy_sharpe,
        "bh_initial_equity": initial_bh_equity,
        "bh_final_equity": final_bh_equity,
        "bh_total_return": bh_total_return,
        "bh_mdd": bh_mdd,
        "bh_sharpe": bh_sharpe,
        "symbol_diagnostics": symbol_diagnostics,
        "merged_history": merged_history,
        "all_timestamps": all_timestamps
    }

def run_comparative_simulations():
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    
    print("[!] Executing Phase 20 (Unprotected) backtest baseline...")
    p20 = execute_backtest(stops_mode="none")
    
    print("[!] Executing Phase 21 (Protected with Stateful Trailing Stops) backtest...")
    p21 = execute_backtest(stops_mode="trailing")
    
    print("[!] Executing Phase 22 (Protected with Raw Structural Anchor Stops) backtest...")
    p22 = execute_backtest(stops_mode="structural_raw")
    
    print("[!] Executing Phase 23 (Protected with Buffered Structural Stops & 3-Bar Gate) backtest...")
    p23 = execute_backtest(stops_mode="structural_buffered")
    
    # 1. Print Phase 23 stdout Report
    console_report = []
    console_report.append("=== PHASE 23: BUFFERED STRUCTURAL PORTFOLIO REPORT ===")
    console_report.append(f"Simulation Period: {p23['all_timestamps'][0]} to {p23['all_timestamps'][-1]}")
    console_report.append(f"Total Historical Bars: {len(p23['all_timestamps'])}")
    console_report.append("-" * 125)
    console_report.append(f"{'Metric':<25} | {'Phase 20 Baseline':<18} | {'Phase 21 Protected':<18} | {'Phase 22 Structural':<18} | {'Phase 23 Buffered':<18} | {'Passive B&H':<12}")
    console_report.append("-" * 125)
    console_report.append(f"{'Initial Value (USD)':<25} | {p20['initial_equity']:<18,.2f} | {p21['initial_equity']:<18,.2f} | {p22['initial_equity']:<18,.2f} | {p23['initial_equity']:<18,.2f} | {p23['bh_initial_equity']:<12,.2f}")
    console_report.append(f"{'Final Value (USD)':<25} | {p20['final_equity']:<18,.2f} | {p21['final_equity']:<18,.2f} | {p22['final_equity']:<18,.2f} | {p23['final_equity']:<18,.2f} | {p23['bh_final_equity']:<12,.2f}")
    console_report.append(f"{'Total Return (%)':<25} | {p20['total_return']:<17,.2f}% | {p21['total_return']:<17,.2f}% | {p22['total_return']:<17,.2f}% | {p23['total_return']:<17,.2f}% | {p23['bh_total_return']:<11,.2f}%")
    console_report.append(f"{'Maximum Drawdown (MDD %)':<25} | {p20['mdd']:<17,.2f}% | {p21['mdd']:<17,.2f}% | {p22['mdd']:<17,.2f}% | {p23['mdd']:<17,.2f}% | {p23['bh_mdd']:<11,.2f}%")
    console_report.append(f"{'Realized Sharpe Ratio':<25} | {p20['sharpe']:<18,.4f} | {p21['sharpe']:<18,.4f} | {p22['sharpe']:<18,.4f} | {p23['sharpe']:<18,.4f} | {p23['bh_sharpe']:<12,.4f}")
    console_report.append("-" * 125)
    console_report.append("\n=== ASSET DIAGNOSTIC STATISTICS (PHASE 23 BUFFERED STRUCTURAL) ===")
    for symbol in p23["symbol_diagnostics"]:
        diag = p23["symbol_diagnostics"][symbol]
        console_report.append(f"Asset: {symbol}")
        console_report.append(f"  - Completed Round-trip Trades: {diag['trade_count']}")
        console_report.append(f"  - Average Holding Period (Bars): {diag['avg_holding']:.2f}")
        
        wl_ratio_str = "N/A"
        if diag["wins"] > 0 or diag["losses"] > 0:
            if diag["losses"] == 0:
                wl_ratio_str = f"100% Wins ({diag['wins']})"
            else:
                wl_ratio_str = f"{diag['wins'] / diag['losses']:.2f} (Wins: {diag['wins']}, Losses: {diag['losses']})"
        console_report.append(f"  - Win/Loss Ratio (realized PnL): {wl_ratio_str}")
        
        # Report Exit Reasons
        reasons = [t.get("exit_reason", "Tactical") for t in diag["trades"]]
        console_report.append(f"  - Exit Reasons -> Take-Profit Exits: {reasons.count('TakeProfit')}, Anchor Stop Exits: {reasons.count('AnchorStop')}, Tactical: {reasons.count('Tactical')}")
        console_report.append("")
        
    console_text = "\n".join(console_report)
    print(console_text)
    
    # 2. OVERWRITE out/portfolio_performance.md
    md_path = os.path.join(project_root, "out", "portfolio_performance.md")
    
    md_write = []
    md_write.append("# === PHASE 23: BUFFERED STRUCTURAL PORTFOLIO REPORT ===")
    md_write.append(f"\n*Compiled on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    md_write.append(f"\n### Simulation Overview")
    md_write.append(f"- **Total Bars**: {len(p23['all_timestamps'])}")
    md_write.append(f"- **Start Timestamp**: {p23['all_timestamps'][0]}")
    md_write.append(f"- **End Timestamp**: {p23['all_timestamps'][-1]}")
    
    md_write.append(f"\n### Multi-Phase Compounding Metrics Comparison")
    md_write.append("| Metric | Phase 20 Baseline | Phase 21 Protected | Phase 22 Structural (Raw) | Phase 23 Structural (Buffered) | Passive Buy-and-Hold |")
    md_write.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
    md_write.append(f"| **Initial Portfolio Value (USD)** | ${p20['initial_equity']:,.2f} | ${p21['initial_equity']:,.2f} | ${p22['initial_equity']:,.2f} | ${p23['initial_equity']:,.2f} | ${p23['bh_initial_equity']:,.2f} |")
    md_write.append(f"| **Final Portfolio Value (USD)** | ${p20['final_equity']:,.2f} | ${p21['final_equity']:,.2f} | ${p22['final_equity']:,.2f} | ${p23['final_equity']:,.2f} | ${p23['bh_final_equity']:,.2f} |")
    md_write.append(f"| **Total Compounded Return (%)** | {p20['total_return']:.2f}% | {p21['total_return']:.2f}% | {p22['total_return']:.2f}% | **{p23['total_return']:.2f}%** | {p23['bh_total_return']:.2f}% |")
    md_write.append(f"| **Maximum Drawdown (MDD %)** | {p20['mdd']:.2f}% | {p21['mdd']:.2f}% | {p22['mdd']:.2f}% | **{p23['mdd']:.2f}%** | {p23['bh_mdd']:.2f}% |")
    md_write.append(f"| **Realized Sharpe Ratio** | {p20['sharpe']:.4f} | {p21['sharpe']:.4f} | {p22['sharpe']:.4f} | **{p23['sharpe']:.4f}** | {p23['bh_sharpe']:.4f} |")
    
    md_write.append(f"\n### Per-Symbol Diagnostics (Phase 23 Structural Buffered)")
    md_write.append("| Symbol | Completed Trades | Avg Holding Period (Bars) | Wins | Losses | Win/Loss Ratio | Take-Profit Exits | Anchor Stop Exits | Tactical Exits |")
    md_write.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for symbol in p23["symbol_diagnostics"]:
        diag = p23["symbol_diagnostics"][symbol]
        wl_ratio = diag["wins"] / diag["losses"] if diag["losses"] > 0 else (diag["wins"] if diag["wins"] > 0 else 0.0)
        wl_str = f"{wl_ratio:.2f}" if diag["losses"] > 0 else ("100% Wins" if diag["wins"] > 0 else "0.00")
        reasons = [t.get("exit_reason", "Tactical") for t in diag["trades"]]
        md_write.append(f"| {symbol} | {diag['trade_count']} | {diag['avg_holding']:.2f} | {diag['wins']} | {diag['losses']} | {wl_str} | {reasons.count('TakeProfit')} | {reasons.count('AnchorStop')} | {reasons.count('Tactical')} |")
        
    md_write.append(f"\n### Drawdown and Equity Curve Log (Phase 23 Structural Buffered - First 15 & Last 15 Bars)")
    md_write.append("| Timestamp | Active Equity (USD) | Active Drawdown (%) | B&H Equity (USD) | B&H Drawdown (%) |")
    md_write.append("| :--- | :---: | :---: | :---: | :---: |")
    
    for data in p23["merged_history"][:15]:
        md_write.append(f"| {data['timestamp']} | ${data['total_strategy_equity']:,.2f} | {data['strategy_drawdown']:.2f}% | ${data['total_bh_equity']:,.2f} | {data['bh_drawdown']:.2f}% |")
        
    md_write.append("| ... | ... | ... | ... | ... |")
    
    for data in p23["merged_history"][-15:]:
        md_write.append(f"| {data['timestamp']} | ${data['total_strategy_equity']:,.2f} | {data['strategy_drawdown']:.2f}% | ${data['total_bh_equity']:,.2f} | {data['bh_drawdown']:.2f}% |")
        
    with open(md_path, "w") as f:
        f.write("\n".join(md_write))
        
    print(f"[!] Performance reports written cleanly and overwritten to {md_path}")

if __name__ == "__main__":
    run_comparative_simulations()
