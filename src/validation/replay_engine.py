import os
import json
from datetime import datetime
from src.analysis_agent import AnalysisAgent
from src.validation.data_loader import generate_mock_bars
from src.validation.outcome_tracker import calculate_forward_returns

class NormalizedBar:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

def run_replay(symbol: str = "SPY", anomaly: str = None) -> list:
    """
    Executes a deterministic offline replay simulation over the normalized mock bars.
    Logs each proposal with calculated future returns to out/replay_journal.jsonl.
    """
    # 1. Load mock historical data array (Mode A)
    bars = generate_mock_bars(symbol, length=100, anomaly=anomaly)
    
    # 2. Instantiate AnalysisAgent with mock credentials
    agent = AnalysisAgent(api_key="mock", secret_key="mock")
    
    # 3. Establish and isolate replay state output path
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
    agent.state_file = os.path.join(out_dir, "state_replay.json")
    if os.path.exists(agent.state_file):
        try:
            os.remove(agent.state_file)
        except Exception as err:
            print(f"Failed to clear old replay state file: {err}")
    agent._init_state()
    
    replay_records = []
    
    # 4. Sequentially roll forward through the bars (requires 20 lookback bars)
    for i in range(19, len(bars)):
        window_dicts = bars[i - 19 : i + 1]
        
        # Convert dictionary schema to dot-notation object for AnalysisAgent
        window_objs = [NormalizedBar(**b) for b in window_dicts]
        
        # Run proposals with override override_bars parameter
        override_bars = {symbol: window_objs}
        proposals = agent.generate_proposals([symbol], override_bars=override_bars)
        
        if proposals:
            proposal = proposals[0]
            
            # Compute empirical forward returns (+1, +3, +10 bars)
            forward_returns = calculate_forward_returns(bars, i)
            
            # Formulate outcome record row
            replay_entry = {
                "timestamp": proposal["timestamp"],
                "symbol": proposal["symbol"],
                "suggested_action": proposal["suggested_action"],
                "market_regime": proposal["market_regime"],
                "bars_in_trend_count": proposal["bars_in_trend_count"],
                "base_confidence": proposal["base_confidence"],
                "validator_status": proposal["validator_status"],
                "state_telemetry": proposal["state_telemetry"],
                "forward_returns": forward_returns
            }
            
            # Log output cleanly to replay_journal.jsonl
            try:
                with open(replay_journal_path, "a") as f:
                    f.write(json.dumps(replay_entry) + "\n")
            except Exception as write_err:
                print(f"Failed to write replay journal entry: {write_err}")
                
            replay_records.append(replay_entry)
            
    print(f"[!] Replay completed. Logged {len(replay_records)} rows to out/replay_journal.jsonl.")
    return replay_records

if __name__ == "__main__":
    run_replay()
