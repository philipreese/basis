import os
import json
import math
from datetime import datetime
from src.risk_reviewer import RiskReviewer

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

def execute_backtest(stops_mode: str, journal_path: str = None):
    """
    Executes the spread/pairs simulation logic for:
       - 'none': Phase 20 (Unprotected baseline, profits target at z_rsc=0)
       - 'trailing': Phase 21 (Stop loss at 1.0x ATR and Break-even floor, immediate exit)
       - 'structural_raw': Phase 22 (Same as Phase 21, immediate exit)
       - 'structural_buffered': Phase 23 / 32 / 33 (Stop loss, Break-even, plus 8-Bar Persistence Gate & Z-Score Velocity Exits)
    """
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    if journal_path is None:
        journal_path = os.path.join(project_root, "out", "replay_journal.jsonl")
    
    if not os.path.exists(journal_path):
        raise FileNotFoundError(f"Replay journal not found at {journal_path}. Please run replay_engine.py first.")
        
    assets_config, archetypes_config = load_configs()
    risk_reviewer = RiskReviewer()
    temporal_persistence_bars = risk_reviewer.temporal_persistence_bars
    max_notional_leverage = risk_reviewer.max_notional_leverage_multiplier
    
    # 1. Parse and align QQQ and SPY events chronologically
    events_by_ts = {}
    with open(journal_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            event = json.loads(line)
            ts = event["timestamp"]
            symbol = event["symbol"]
            if ts not in events_by_ts:
                events_by_ts[ts] = {}
            events_by_ts[ts][symbol] = event
            
    # Sort timestamps chronologically
    sorted_timestamps = sorted(list(events_by_ts.keys()))
    
    aligned_events = []
    for ts in sorted_timestamps:
        entry = events_by_ts[ts]
        if "SPY" in entry and "QQQ" in entry:
            aligned_events.append((ts, entry["SPY"], entry["QQQ"]))
            
    if not aligned_events:
        raise ValueError(f"No synchronized SPY and QQQ events found in {journal_path}")
        
    # State variables for Strategy
    cash = 100000.0
    state = "FLAT" # FLAT, LONG_SPREAD, SHORT_SPREAD
    last_transition_idx = -2
    trades = []
    current_trade = None
    
    # Stateful stop loss variables
    capital = 100000.0
    n_q = 0.0
    n_s = 0.0
    rsc_entry = 0.0
    rsc_std_entry = 0.001
    entry_fee = 0.0
    
    break_even_activated = False
    break_even_floor_value = 0.0
    pending_stop_exit = False
    pending_stop_reason = ""
    consecutive_breaches = 0
    
    # Stateful break and cooldown variables
    velocity_breach_count = 0
    forbidden_entry_direction = 0 # 1 for LONG_SPREAD, -1 for SHORT_SPREAD
    forbidden_entry_cooldown = 0
    
    # Stateful Buy & Hold (Long QQQ, Long SPY)
    bh_cash = 200000.0
    bh_units_q = 0.0
    bh_units_s = 0.0
    bh_entered = False
    
    symbol_histories = {"QQQ_SPY_SPREAD": {}}
    
    # Initialize trading journal log path
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log_dir = os.path.join(project_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "trading_journal.log")
    
    prev_signal_direction = 0
    signal_consecutive_bars = 0
    
    for idx, (timestamp, spy_event, qqq_event) in enumerate(aligned_events):
        # Extract bar prices
        close_q = float(qqq_event["metrics"]["current_price"])
        open_q = float(qqq_event["metrics"]["open"])
        high_q = float(qqq_event["metrics"]["high"])
        low_q = float(qqq_event["metrics"]["low"])
        atr_q = float(qqq_event["metrics"]["atr_14"])
        
        close_s = float(spy_event["metrics"]["current_price"])
        open_s = float(spy_event["metrics"]["open"])
        high_s = float(spy_event["metrics"]["high"])
        low_s = float(spy_event["metrics"]["low"])
        atr_s = float(spy_event["metrics"]["atr_14"])
        
        # RSC metrics
        z_rsc = float(qqq_event["metrics"].get("z_rsc", 0.0))
        rsc = float(qqq_event["metrics"].get("rsc", 1.0))
        rsc_std_100 = float(qqq_event["metrics"].get("rsc_std_100", 0.001))
        
        # Continuous Parameter Estimation variables
        kappa = float(qqq_event["metrics"].get("kappa", 0.01))
        estimated_half_life = float(qqq_event["metrics"].get("estimated_half_life", 70.0))
        exposure_factor = float(qqq_event["metrics"].get("exposure_factor", 1.0))
        gate_bars = int(qqq_event["metrics"].get("gate_bars", 4))
        adf_t_stat = float(qqq_event["metrics"].get("adf_t_stat", 0.0))
        z_adf_t_stat = float(qqq_event["metrics"].get("z_adf_t_stat", 0.0))
        
        # Suggested actions
        action_q = qqq_event["suggested_action"]
        action_s = spy_event["suggested_action"]
        confidence = float(qqq_event["base_confidence"])
        
        # --- Buy & Hold execution (synchronous) ---
        if not bh_entered:
            # Entry fee (1.5 bps on entry)
            fee_q = 100000.0 * risk_reviewer.transaction_fee_rate
            fee_s = 100000.0 * risk_reviewer.transaction_fee_rate
            
            # Entry slippage
            p_q_bh = close_q + atr_q * 0.15
            p_s_bh = close_s + atr_s * 0.10
            
            bh_units_q = (100000.0 - fee_q) / p_q_bh
            bh_units_s = (100000.0 - fee_s) / p_s_bh
            bh_cash = 0.0
            bh_entered = True
            
        bh_equity = bh_cash + bh_units_q * close_q + bh_units_s * close_s
        
        # Slippage drag parameters
        slippage_q = atr_q * 0.15
        slippage_s = atr_s * 0.10
        
        # --- Next-bar open liquidation ---
        if pending_stop_exit:
            # Exit at open prices
            leg_nominal = capital * 0.5 * max_notional_leverage
            if state == "LONG_SPREAD":
                proceeds_q = n_q * (open_q - slippage_q)
                proceeds_s = leg_nominal - n_s * (open_s + slippage_s)
            else: # SHORT_SPREAD
                proceeds_q = leg_nominal - n_q * (open_q + slippage_q)
                proceeds_s = n_s * (open_s - slippage_s)
                
            exit_fee = (n_q * open_q + n_s * open_s) * risk_reviewer.transaction_fee_rate
            net_proceeds = proceeds_q + proceeds_s - exit_fee
            cash = cash + (net_proceeds - leg_nominal) # Update cash with realized PnL
            
            if current_trade:
                current_trade["exit_idx"] = idx
                current_trade["exit_timestamp"] = timestamp
                current_trade["exit_price"] = close_q # Use close for stats
                current_trade["exit_effective_price"] = open_q
                current_trade["exit_fee"] = exit_fee
                current_trade["pnl"] = net_proceeds - leg_nominal
                current_trade["holding_bars"] = idx - current_trade["entry_idx"]
                current_trade["exit_reason"] = pending_stop_reason
                
                # Compute Expected Capture Ratio
                realized_pnl = net_proceeds - leg_nominal
                theoretical_amplitude = current_trade["leg_notional_entry"] * (abs(current_trade["z_rsc_entry"]) * current_trade["rsc_std_entry"] / current_trade["rsc_entry"])
                current_trade["theoretical_amplitude"] = theoretical_amplitude
                current_trade["expected_capture_ratio"] = realized_pnl / theoretical_amplitude if theoretical_amplitude > 0 else 0.0
                
                trades.append(current_trade)
                current_trade = None
                
                # log exit execution drag
                log_exit_msg = (
                    f"[{timestamp}] EXECUTION - EXIT (Next-Bar Open) reason: {pending_stop_reason} | "
                    f"Realized PnL: {realized_pnl:.2f} | Exit Fee: {exit_fee:.2f}"
                )
                with open(log_path, "a") as log_f:
                    log_f.write(log_exit_msg + "\n")
                
            # If structural break exit occurred, set cooldown and direction blacklist
            if pending_stop_reason == "StructuralBreak":
                forbidden_entry_direction = 1 if state == "LONG_SPREAD" else -1
                forbidden_entry_cooldown = 8
                
            state = "FLAT"
            last_transition_idx = idx
            n_q = 0.0
            n_s = 0.0
            pending_stop_exit = False
            break_even_activated = False
            pending_stop_reason = ""
            consecutive_breaches = 0
            velocity_breach_count = 0
            
        bars_since_last_transition = idx - last_transition_idx
        
        # Compute current unrealized PnL and active equity
        leg_nominal = capital * 0.5 * max_notional_leverage
        if state == "LONG_SPREAD":
            unrealized_pnl = (n_q * close_q - leg_nominal) + (leg_nominal - n_s * close_s)
            equity = cash + unrealized_pnl
        elif state == "SHORT_SPREAD":
            unrealized_pnl = (leg_nominal - n_q * close_q) + (n_s * close_s - leg_nominal)
            equity = cash + unrealized_pnl
        else:
            unrealized_pnl = 0.0
            equity = cash
            
        # Log parameter telemetry on every bar block
        log_msg = (
            f"[{timestamp}] Ticker: QQQ/SPY | State: {state} | Kappa: {kappa:.6f} | "
            f"Half-Life: {estimated_half_life:.2f} | Exposure Factor: {exposure_factor:.4f} | "
            f"Gate Bars: {gate_bars} | Cash: {cash:.2f} | Equity: {equity:.2f}"
        )
        with open(log_path, "a") as log_f:
            log_f.write(log_msg + "\n")
            
        # --- Evaluate Stop Losses ---
        if state != "FLAT" and stops_mode != "none":
            # 0. Programmatic Stationarity Risk Interception
            if exposure_factor == 0.0:
                pending_stop_exit = True
                pending_stop_reason = "RiskInterception"
                
            # 1. Break-Even Floor
            if not pending_stop_exit:
                favorable_threshold = 1.5 * leg_nominal * (rsc_std_entry / rsc_entry)
                if unrealized_pnl >= favorable_threshold:
                    break_even_activated = True
                    break_even_floor_value = entry_fee
                    
                if break_even_activated and unrealized_pnl < break_even_floor_value:
                    pending_stop_exit = True
                    pending_stop_reason = "BreakEven"
                
            # 2. Adaptive Z-Score Velocity Structural Break Detection (Phase 33)
            if not pending_stop_exit:
                z_vel = float(qqq_event["metrics"].get("z_velocity", 0.0))
                is_structural_break = False
                if state == "LONG_SPREAD":
                    if z_vel < -1.0:
                        velocity_breach_count += 1
                    else:
                        velocity_breach_count = 0
                    if velocity_breach_count >= 2 or z_vel < -1.5:
                        is_structural_break = True
                elif state == "SHORT_SPREAD":
                    if z_vel > 1.0:
                        velocity_breach_count += 1
                    else:
                        velocity_breach_count = 0
                    if velocity_breach_count >= 2 or z_vel > 1.5:
                        is_structural_break = True
                        
                if is_structural_break:
                    pending_stop_exit = True
                    pending_stop_reason = "StructuralBreak"
                
            # 3. Volatility Stop Boundary (1.0x RSC Std below entry)
            if not pending_stop_exit:
                stop_loss_boundary = -1.0 * leg_nominal * (rsc_std_entry / rsc_entry)
                if unrealized_pnl < stop_loss_boundary:
                    consecutive_breaches += 1
                    if stops_mode == "structural_buffered":
                        if consecutive_breaches >= temporal_persistence_bars:
                            pending_stop_exit = True
                            pending_stop_reason = "AnchorStop"
                    else: # trailing or structural_raw (no temporal gate)
                        pending_stop_exit = True
                        pending_stop_reason = "AnchorStop"
                else:
                    consecutive_breaches = 0
                
        # --- Profit Target / Tactical Exit ---
        if state != "FLAT" and not pending_stop_exit:
            # Tactical Exit if Z-score mean reverts back to 0 or opposite crossover occurs
            tactical_exit_triggered = False
            if state == "LONG_SPREAD":
                if z_rsc >= 0.0 or (action_q == "Sell" and action_s == "Buy"):
                    tactical_exit_triggered = True
            elif state == "SHORT_SPREAD":
                if z_rsc <= 0.0 or (action_q == "Buy" and action_s == "Sell"):
                    tactical_exit_triggered = True
                    
            if tactical_exit_triggered and bars_since_last_transition >= 1:
                # Exit immediately at close prices
                leg_nominal = capital * 0.5 * max_notional_leverage
                if state == "LONG_SPREAD":
                    proceeds_q = n_q * (close_q - slippage_q)
                    proceeds_s = leg_nominal - n_s * (close_s + slippage_s)
                else:
                    proceeds_q = leg_nominal - n_q * (close_q + slippage_q)
                    proceeds_s = n_s * (close_s - slippage_s)
                    
                exit_fee = (n_q * close_q + n_s * close_s) * risk_reviewer.transaction_fee_rate
                net_proceeds = proceeds_q + proceeds_s - exit_fee
                cash = cash + (net_proceeds - leg_nominal)
                
                if current_trade:
                    current_trade["exit_idx"] = idx
                    current_trade["exit_timestamp"] = timestamp
                    current_trade["exit_price"] = close_q
                    current_trade["exit_effective_price"] = close_q
                    current_trade["exit_fee"] = exit_fee
                    current_trade["pnl"] = net_proceeds - leg_nominal
                    current_trade["holding_bars"] = idx - current_trade["entry_idx"]
                    current_trade["exit_reason"] = "TakeProfit"
                    
                    # Compute Expected Capture Ratio
                    realized_pnl = net_proceeds - leg_nominal
                    theoretical_amplitude = current_trade["leg_notional_entry"] * (abs(current_trade["z_rsc_entry"]) * current_trade["rsc_std_entry"] / current_trade["rsc_entry"])
                    current_trade["theoretical_amplitude"] = theoretical_amplitude
                    current_trade["expected_capture_ratio"] = realized_pnl / theoretical_amplitude if theoretical_amplitude > 0 else 0.0
                    
                    trades.append(current_trade)
                    current_trade = None
                    
                # log exit execution drag
                log_exit_msg = (
                    f"[{timestamp}] EXECUTION - EXIT (Tactical Close) reason: TakeProfit | "
                    f"Realized PnL: {realized_pnl:.2f} | Exit Fee: {exit_fee:.2f}"
                )
                with open(log_path, "a") as log_f:
                    log_f.write(log_exit_msg + "\n")
                    
                state = "FLAT"
                last_transition_idx = idx
                n_q = 0.0
                n_s = 0.0
                break_even_activated = False
                consecutive_breaches = 0
                velocity_breach_count = 0
                equity = cash
                
        # --- Evaluate Entries ---
        # Decr cooldown if active
        if forbidden_entry_cooldown > 0:
            forbidden_entry_cooldown -= 1
            if forbidden_entry_cooldown == 0:
                forbidden_entry_direction = 0
                
        # Determine signal direction
        signal_direction = 0
        if action_q == "Buy" and action_s == "Sell":
            signal_direction = 1 # Long Spread
        elif action_q == "Sell" and action_s == "Buy":
            signal_direction = -1 # Short Spread
            
        # Update entry persistence gate consecutive bars
        if signal_direction == prev_signal_direction and signal_direction != 0:
            signal_consecutive_bars += 1
        else:
            signal_consecutive_bars = 1 if signal_direction != 0 else 0
        prev_signal_direction = signal_direction
        
        if state == "FLAT" and not pending_stop_exit:
            if signal_direction != 0 and confidence >= 0.40 and signal_consecutive_bars >= gate_bars and exposure_factor > 0.0:
                if signal_direction == forbidden_entry_direction:
                    # Blocked by structural break cooldown
                    pass
                else:
                    capital = cash # Compounding
                    leg_notional = capital * 0.5 * max_notional_leverage * exposure_factor
                    entry_fee = leg_notional * risk_reviewer.transaction_fee_rate * 2
                    
                    # Friction sensitivity check
                    estimated_fee = leg_notional * risk_reviewer.transaction_fee_rate * 4
                    estimated_slippage = 2.0 * leg_notional * ((slippage_q / close_q) + (slippage_s / close_s))
                    estimated_friction = estimated_fee + estimated_slippage
                    
                    expected_rsc_change = abs(z_rsc) * rsc_std_100
                    net_expected_edge = leg_notional * (expected_rsc_change / rsc)
                    
                    if net_expected_edge >= estimated_friction:
                        # Enter spread
                        state = "LONG_SPREAD" if signal_direction == 1 else "SHORT_SPREAD"
                        last_transition_idx = idx
                        
                        rsc_entry = rsc
                        rsc_std_entry = rsc_std_100
                        
                        if signal_direction == 1:
                            p_q_entry = close_q + slippage_q
                            p_s_entry = close_s - slippage_s
                        else:
                            p_q_entry = close_q - slippage_q
                            p_s_entry = close_s + slippage_s
                            
                        n_q = leg_notional / p_q_entry
                        n_s = leg_notional / p_s_entry
                        
                        cash = cash - entry_fee
                        
                        current_trade = {
                            "entry_idx": idx,
                            "entry_timestamp": timestamp,
                            "entry_price": close_q,
                            "entry_effective_price": p_q_entry,
                            "entry_fee": entry_fee,
                            "entry_cash": leg_notional * 2,
                            "exit_idx": None,
                            "exit_timestamp": None,
                            "exit_price": None,
                            "exit_effective_price": None,
                            "exit_fee": 0.0,
                            "pnl": 0.0,
                            "holding_bars": 0,
                            "exit_reason": "None",
                            "friction_delta": estimated_friction,
                            "leg_notional_entry": leg_notional,
                            "z_rsc_entry": z_rsc,
                            "rsc_std_entry": rsc_std_100,
                            "rsc_entry": rsc
                        }
                        
                        # Deduct entry fee immediately from equity check
                        equity = cash + unrealized_pnl
                        
                        # log entry execution drag
                        log_entry_msg = (
                            f"[{timestamp}] EXECUTION - ENTRY spread: {state} | "
                            f"Leg Notional: {leg_notional:.2f} | Entry Fee BPS: {risk_reviewer.transaction_fee_bps} | "
                            f"Entry Fee: {entry_fee:.2f} | Slippage QQQ: {slippage_q:.4f} | Slippage SPY: {slippage_s:.4f}"
                        )
                        with open(log_path, "a") as log_f:
                            log_f.write(log_entry_msg + "\n")
                    else:
                        # Friction reject
                        pass
                    
        # Log state history
        symbol_histories["QQQ_SPY_SPREAD"][timestamp] = {
            "close": rsc,
            "strategy_cash": cash,
            "strategy_units": n_q,
            "strategy_state": state,
            "strategy_equity": equity,
            "bh_cash": bh_cash,
            "bh_units": bh_units_q,
            "bh_equity": bh_equity
        }
        
    # Calculate diagnostics
    trade_count = len(trades)
    avg_holding = sum(t["holding_bars"] for t in trades) / trade_count if trade_count > 0 else 0.0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    
    symbol_diagnostics = {
        "QQQ_SPY_SPREAD": {
            "trade_count": trade_count,
            "avg_holding": avg_holding,
            "wins": wins,
            "losses": losses,
            "trades": trades
        }
    }
    
    # 3. Compile merged histories and compound curves
    merged_history = []
    strategy_peak = 0.0
    bh_peak = 0.0
    strategy_mdd = 0.0
    bh_mdd = 0.0
    
    strategy_log_returns = []
    bh_log_returns = []
    prev_strat_equity = None
    prev_bh_equity = None
    
    for ts in sorted_timestamps:
        if ts in symbol_histories["QQQ_SPY_SPREAD"]:
            hist = symbol_histories["QQQ_SPY_SPREAD"][ts]
            strat_eq = hist["strategy_equity"]
            bh_eq = hist["bh_equity"]
            
            # Drawdowns
            if strat_eq > strategy_peak:
                strategy_peak = strat_eq
            strat_dd = (strategy_peak - strat_eq) / strategy_peak * 100 if strategy_peak > 0 else 0.0
            
            if bh_eq > bh_peak:
                bh_peak = bh_eq
            bh_dd = (bh_peak - bh_eq) / bh_peak * 100 if bh_peak > 0 else 0.0
            
            if strat_dd > strategy_mdd:
                strategy_mdd = strat_dd
            if bh_dd > bh_mdd:
                bh_mdd = bh_dd
                
            # Log returns for Sharpe
            if prev_strat_equity is not None:
                strategy_log_returns.append(math.log(strat_eq / prev_strat_equity))
            prev_strat_equity = strat_eq
            
            if prev_bh_equity is not None:
                bh_log_returns.append(math.log(bh_eq / prev_bh_equity))
            prev_bh_equity = bh_eq
            
            merged_history.append({
                "timestamp": ts,
                "total_strategy_equity": strat_eq,
                "strategy_drawdown": strat_dd,
                "total_bh_equity": bh_eq,
                "bh_drawdown": bh_dd
            })
            
    # Realized Sharpe Ratio
    if len(strategy_log_returns) > 1:
        mean_r = sum(strategy_log_returns) / len(strategy_log_returns)
        var_r = sum((r - mean_r) ** 2 for r in strategy_log_returns) / (len(strategy_log_returns) - 1)
        std_r = math.sqrt(var_r)
        strategy_sharpe = (mean_r / std_r) * math.sqrt(1764) if std_r > 0 else 0.0
    else:
        strategy_sharpe = 0.0
        
    if len(bh_log_returns) > 1:
        mean_bh = sum(bh_log_returns) / len(bh_log_returns)
        var_bh = sum((r - mean_bh) ** 2 for r in bh_log_returns) / (len(bh_log_returns) - 1)
        std_bh = math.sqrt(var_bh)
        bh_sharpe = (mean_bh / std_bh) * math.sqrt(1764) if std_bh > 0 else 0.0
    else:
        bh_sharpe = 0.0
        
    initial_strat_equity = merged_history[0]["total_strategy_equity"]
    final_strat_equity = merged_history[-1]["total_strategy_equity"]
    strategy_total_return = (final_strat_equity - initial_strat_equity) / initial_strat_equity * 100
    
    initial_bh_equity = merged_history[0]["total_bh_equity"]
    final_bh_equity = merged_history[-1]["total_bh_equity"]
    bh_total_return = (final_bh_equity - initial_bh_equity) / initial_bh_equity * 100
    
    return {
        "initial_equity": initial_strat_equity,
        "final_equity": final_strat_equity,
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
        "all_timestamps": sorted_timestamps
    }

def run_comparative_simulations(journal_path: str = None):
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    
    print("[!] Executing Phase 20 (Unprotected) backtest baseline...")
    p20 = execute_backtest(stops_mode="none", journal_path=journal_path)
    
    print("[!] Executing Phase 21 (Protected with Stateful Trailing Stops) backtest...")
    p21 = execute_backtest(stops_mode="trailing", journal_path=journal_path)
    
    print("[!] Executing Phase 22 (Protected with Raw Structural Anchor Stops) backtest...")
    p22 = execute_backtest(stops_mode="structural_raw", journal_path=journal_path)
    
    print("[!] Executing Phase 23 (Protected with Buffered Structural Stops & 8-Bar Gate) backtest...")
    p23 = execute_backtest(stops_mode="structural_buffered", journal_path=journal_path)
    
    # 1. Print Phase 23 stdout Report
    console_report = []
    console_report.append("=== PHASE 33: ADAPTIVE STRUCTURAL BREAK REPORT ===")
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
    console_report.append("\n=== ASSET DIAGNOSTIC STATISTICS (PHASE 33 BUFFERED STRUCTURAL) ===")
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
        
        # Report Expected Capture Ratio
        capture_ratios = [t.get("expected_capture_ratio", 0.0) for t in diag["trades"]]
        avg_capture = sum(capture_ratios) / len(capture_ratios) if capture_ratios else 0.0
        console_report.append(f"  - Average Expected Capture Ratio: {avg_capture:.4f}")
        
        # Report Exit Reasons
        reasons = [t.get("exit_reason", "Tactical") for t in diag["trades"]]
        console_report.append(f"  - Exit Reasons -> Take-Profit Exits: {reasons.count('TakeProfit')}, Anchor Stop Exits: {reasons.count('AnchorStop')}, Break-Even: {reasons.count('BreakEven')}, StructuralBreak: {reasons.count('StructuralBreak')}, RiskInterception: {reasons.count('RiskInterception')}, Tactical: {reasons.count('Tactical')}")
        console_report.append("")
        
    console_text = "\n".join(console_report)
    print(console_text)
    
    # 2. OVERWRITE out/portfolio_performance.md
    md_path = os.path.join(project_root, "out", "portfolio_performance.md")
    
    md_write = []
    md_write.append("# === PHASE 33: ADAPTIVE STRUCTURAL BREAK REPORT ===")
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
    
    md_write.append(f"\n### Per-Symbol Diagnostics (Phase 33 Structural Buffered)")
    md_write.append("| Symbol | Completed Trades | Avg Holding Period (Bars) | Wins | Losses | Win/Loss Ratio | Avg Expected Capture Ratio | Take-Profit Exits | Anchor Stop Exits | Break-Even Exits | Structural Break Exits | Risk Intercept Exits |")
    md_write.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for symbol in p23["symbol_diagnostics"]:
        diag = p23["symbol_diagnostics"][symbol]
        wl_ratio = diag["wins"] / diag["losses"] if diag["losses"] > 0 else (diag["wins"] if diag["wins"] > 0 else 0.0)
        wl_str = f"{wl_ratio:.2f}" if diag["losses"] > 0 else ("100% Wins" if diag["wins"] > 0 else "0.00")
        
        capture_ratios = [t.get("expected_capture_ratio", 0.0) for t in diag["trades"]]
        avg_capture = sum(capture_ratios) / len(capture_ratios) if capture_ratios else 0.0
        
        reasons = [t.get("exit_reason", "Tactical") for t in diag["trades"]]
        md_write.append(f"| {symbol} | {diag['trade_count']} | {diag['avg_holding']:.2f} | {diag['wins']} | {diag['losses']} | {wl_str} | {avg_capture:.4f} | {reasons.count('TakeProfit')} | {reasons.count('AnchorStop')} | {reasons.count('BreakEven')} | {reasons.count('StructuralBreak')} | {reasons.count('RiskInterception')} |")
        
    md_write.append(f"\n### Drawdown and Equity Curve Log (Phase 33 Structural Buffered - First 15 & Last 15 Bars)")
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
