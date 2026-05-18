import os
import json
import time
import uuid
from datetime import datetime
from dotenv import load_dotenv

from .analysis_agent import AnalysisAgent
from .risk_reviewer import RiskReviewer

def main():
    load_dotenv()
    
    api_key = os.getenv("ALPACA_API_KEY_ID")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    
    analysis_agent = AnalysisAgent(api_key, secret_key)
    risk_reviewer = RiskReviewer()
    
    symbols = ["SPY", "QQQ"]
    
    print(f"[{datetime.now()}] Starting Modular Two-Agent System...")
    
    # We will run 1 initial loop for simulation, then sleep for 15 minutes.
    iteration = 0
    while True:
        iteration += 1
        loop_id = str(uuid.uuid4())
        evaluation_timestamp = datetime.now().isoformat()
        
        try:
            print(f"[{datetime.now()}] Cycle {iteration}: Analysis Agent generating proposals...")
            proposals = analysis_agent.generate_proposals(symbols)
            
            for proposal in proposals:
                # 1. Analysis Agent proposes
                # 2. Risk Reviewer intercepts and evaluates
                print(f"[{datetime.now()}] Risk Reviewer evaluating {proposal['symbol']}...")
                review = risk_reviewer.evaluate_proposal(proposal)
                
                # 3. Consensus resolution
                consensus = analysis_agent.resolve_consensus(proposal, review)
                
                # Mock metrics for regime and volatility
                market_regime = "Bull" if proposal["metrics"]["sma_5"] > proposal["metrics"]["sma_20"] else "Bear"
                volatility_metric = "Low"
                
                # 4. Construct Journal Entry
                journal_entry = {
                    "loop_id": loop_id,
                    "bar_timestamp": proposal["timestamp"],  # Timestamp of the analyzed data capture
                    "evaluation_timestamp": evaluation_timestamp,
                    "symbol": proposal["symbol"],
                    "metrics": proposal["metrics"],
                    "market_regime": market_regime,
                    "suggested_action": proposal["suggested_action"],
                    "confidence": consensus["final_confidence"],
                    "volatility_metric": volatility_metric,
                    "review_status": review["review_status"],
                    "reviewer_severity": review["objection_severity"],
                    "reviewer_counter_argument": review["counter_argument"],
                    "final_action": consensus["final_action"],
                    "max_allowed_size": review["max_allowed_size"] * consensus["position_modifier"],
                    "token_usage_estimate": "145 tokens (mock)"
                }
                
                with open("trading_journal.jsonl", "a") as f:
                    f.write(json.dumps(journal_entry) + "\n")
                    
            print(f"[{datetime.now()}] Cycle {iteration} completed successfully.")
            
        except Exception as e:
            print(f"[{datetime.now()}] Error in evaluation loop: {e}")
            
        # For simulation: we only run a few iterations. 
        # In actual deployment, sleep for 15 minutes: time.sleep(15 * 60)
        # We will sleep for 15 minutes but break after 1 loop for demonstration 
        # since we don't want the agent to hang indefinitely.
        if iteration >= 1:
            print("Simulation limit reached. Breaking loop.")
            break
            
        # If continuing:
        # print("Sleeping for 15 minutes...")
        # time.sleep(900)

if __name__ == "__main__":
    main()
