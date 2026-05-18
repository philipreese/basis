import os
import json
import time
import uuid
from datetime import datetime
from dotenv import load_dotenv

from .analysis_agent import AnalysisAgent
from .risk_reviewer import RiskReviewer

def main():
    # Ensure .env is loaded from the parent root directory regardless of where the script is executed
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
    load_dotenv(env_path)
    
    api_key = os.getenv("ALPACA_API_KEY_ID")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    
    analysis_agent = AnalysisAgent(api_key, secret_key)
    risk_reviewer = RiskReviewer()
    
    symbols = ["SPY", "QQQ"]
    last_bar_timestamps = {symbol: None for symbol in symbols}
    
    print(f"[{datetime.now()}] Starting Modular Two-Agent System (Hardened)...")
    
    iteration = 0
    while True:
        iteration += 1
        loop_id = str(uuid.uuid4())
        evaluation_timestamp = datetime.now().isoformat()
        
        try:
            print(f"[{datetime.now()}] Cycle {iteration}: Polling Alpaca for new bars...")
            proposals = analysis_agent.generate_proposals(symbols)
            
            new_proposals = []
            for proposal in proposals:
                symbol = proposal["symbol"]
                bar_ts = proposal["timestamp"]
                if last_bar_timestamps[symbol] != bar_ts:
                    new_proposals.append(proposal)
                    last_bar_timestamps[symbol] = bar_ts
                    
            if not new_proposals:
                print(f"[{datetime.now()}] No new bars. Sleeping for 10 seconds...")
                time.sleep(10)
                # For simulation: break if we've already done an iteration and there are no new bars.
                if iteration > 1:
                    print("Simulation limit reached (no new bars). Breaking loop.")
                    break
                continue
            
            for proposal in new_proposals:
                print(f"[{datetime.now()}] Risk Reviewer evaluating {proposal['symbol']}...")
                review = risk_reviewer.evaluate_proposal(proposal)
                
                consensus = analysis_agent.resolve_consensus(proposal, review)
                
                volatility_metric = f"ATR: {review.get('atr_14', 0):.2f}"
                
                journal_entry = {
                    "loop_id": loop_id,
                    "bar_timestamp": proposal["timestamp"],
                    "evaluation_timestamp": evaluation_timestamp,
                    "symbol": proposal["symbol"],
                    "metrics": proposal["metrics"],
                    "market_regime": proposal["market_regime"],
                    "bars_in_trend_count": proposal["bars_in_trend_count"],
                    "suggested_action": proposal["suggested_action"],
                    "confidence": consensus["final_confidence"],
                    "confidence_factors": proposal["confidence_factors"],
                    "volatility_metric": volatility_metric,
                    "validator_status": proposal.get("validator_status", "UNKNOWN"),
                    "review_status": review["review_status"],
                    "reviewer_severity": review["objection_severity"],
                    "reviewer_counter_argument": review["counter_argument"],
                    "final_action": consensus["final_action"],
                    "max_allowed_size": review["max_allowed_size"] * consensus["position_modifier"],
                    "token_usage_estimate": "0 tokens (Math Engine)"
                }
                
                out_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "out", "trading_journal.jsonl")
                with open(out_file, "a") as f:
                    f.write(json.dumps(journal_entry) + "\n")
                    
            print(f"[{datetime.now()}] Cycle {iteration} completed successfully.")
            
        except Exception as e:
            print(f"[{datetime.now()}] Error in evaluation loop: {e}")
            
        if iteration >= 2:
            print("Simulation limit reached. Breaking loop.")
            break

if __name__ == "__main__":
    main()
