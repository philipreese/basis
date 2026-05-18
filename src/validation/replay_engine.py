import os
import json
import argparse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from src.analysis_agent import AnalysisAgent
from src.validation.data_loader import generate_mock_bars, fetch_alpaca_bars
from src.validation.outcome_tracker import calculate_forward_returns
from alpaca.data.historical import StockHistoricalDataClient

class NormalizedBar:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

def run_replay(symbols: list = None, anomaly: str = None, mode: str = "mock", lookback_days: int = 30, timeframe_str: str = "15m") -> list:
    """
    Executes a deterministic offline replay simulation over either normalized mock bars or historical data.
    Logs each proposal with calculated future returns to out/replay_journal.jsonl.
    """
    if symbols is None:
        symbols = ["SPY", "QQQ"]
        
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    out_dir = os.path.join(project_root, "out")
    os.makedirs(out_dir, exist_ok=True)
    
    replay_journal_path = os.path.join(out_dir, "replay_journal.jsonl")
    if os.path.exists(replay_journal_path):
        try:
            os.remove(replay_journal_path)
        except Exception as err:
            print(f"Failed to clear old replay journal: {err}")
            
    # Force state isolation to ensure 100% mathematical determinism
    agent = AnalysisAgent(api_key="mock", secret_key="mock")
    agent.state_file = os.path.join(out_dir, "state_replay.json")
    if os.path.exists(agent.state_file):
        try:
            os.remove(agent.state_file)
        except Exception as err:
            print(f"Failed to clear old replay state file: {err}")
    agent._init_state()
    
    all_replay_records = []
    
    alpaca_client = None
    if mode == "historical":
        load_dotenv()
        api_key = os.getenv("ALPACA_API_KEY_ID")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise ValueError("Historical mode requires ALPACA_API_KEY_ID and ALPACA_SECRET_KEY in .env")
        alpaca_client = StockHistoricalDataClient(api_key, secret_key)
        
    for symbol in symbols:
        true_start_time = None
        
        if mode == "historical":
            end_time = datetime.now(timezone.utc)
            true_start_time = end_time - timedelta(days=lookback_days)
            # Warm-up Padding Fetch: pull an extra 35 days for 1h to guarantee at least 130 leading bars
            padding_days = 35 if timeframe_str == "1h" else 10
            padded_start = true_start_time - timedelta(days=padding_days)
            bars = fetch_alpaca_bars(symbol, padded_start, end_time, alpaca_client, timeframe_str)
            print(f"[{symbol}] Fetched {len(bars)} historical bars (including padding).")
        else:
            bars = generate_mock_bars(symbol, length=200, anomaly=anomaly)
            
        # Sequentially roll forward through the bars (requires 130 lookback bars for sma_macro)
        for i in range(129, len(bars)):
            window_dicts = bars[i - 129 : i + 1]
            
            # Convert dictionary schema to dot-notation object for AnalysisAgent
            window_objs = [NormalizedBar(**b) for b in window_dicts]
            
            # Run proposals with override override_bars parameter
            override_bars = {symbol: window_objs}
            proposals = agent.generate_proposals([symbol], override_bars=override_bars)
            
            if proposals:
                proposal = proposals[0]
                
                # Suppress log output until we hit the true start of the testing window
                current_timestamp = window_objs[-1].timestamp
                if mode == "historical" and true_start_time and current_timestamp < true_start_time:
                    continue
                
                # Compute empirical forward returns (+1, +3, +10 bars)
                forward_returns = calculate_forward_returns(bars, i)
                
                # Formulate outcome record row
                metrics_copy = proposal["metrics"].copy()
                metrics_copy["sma_macro"] = bars[i].get("sma_macro", bars[i]["close"])
                metrics_copy["open"] = float(bars[i]["open"])
                metrics_copy["high"] = float(bars[i]["high"])
                metrics_copy["low"] = float(bars[i]["low"])
                replay_entry = {
                    "timestamp": proposal["timestamp"],
                    "symbol": proposal["symbol"],
                    "data_provenance": proposal.get("data_provenance", {}),
                    "suggested_action": proposal["suggested_action"],
                    "market_regime": proposal["market_regime"],
                    "bars_in_trend_count": proposal["bars_in_trend_count"],
                    "base_confidence": proposal["base_confidence"],
                    "validator_status": proposal["validator_status"],
                    "state_telemetry": proposal["state_telemetry"],
                    "metrics": metrics_copy,
                    "confidence_factors": proposal["confidence_factors"],
                    "forward_returns": forward_returns
                }
                
                # Log output cleanly to replay_journal.jsonl
                try:
                    with open(replay_journal_path, "a") as f:
                        f.write(json.dumps(replay_entry) + "\n")
                except Exception as write_err:
                    print(f"Failed to write replay journal entry: {write_err}")
                    
                all_replay_records.append(replay_entry)
                
    print(f"[!] Replay completed. Logged {len(all_replay_records)} rows to out/replay_journal.jsonl.")
    return all_replay_records

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic Offline Replay Engine")
    parser.add_argument("--mode", type=str, default="mock", choices=["mock", "historical"], help="Execution mode (mock or historical)")
    parser.add_argument("--symbols", type=str, default="SPY,QQQ", help="Comma-separated list of symbols to replay")
    parser.add_argument("--lookback_days", type=int, default=30, help="Number of days for historical lookback")
    parser.add_argument("--timeframe", type=str, default="15m", choices=["15m", "1h"], help="Resolution of historical bars (15m or 1h)")
    args = parser.parse_args()
    
    symbol_list = [s.strip() for s in args.symbols.split(",")]
    run_replay(symbols=symbol_list, mode=args.mode, lookback_days=args.lookback_days, timeframe_str=args.timeframe)
