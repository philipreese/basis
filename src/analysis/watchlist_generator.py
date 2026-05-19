import os
import json
from typing import List, Literal
from pydantic import BaseModel, Field
from google import genai
from dotenv import load_dotenv

load_dotenv()

class WatchlistItem(BaseModel):
    ticker: str
    event_type: str
    sentiment_shift: Literal["positive", "negative", "neutral"]
    volatility_expectation: Literal["high", "medium", "low"]
    continuation_bias: Literal["expansion", "fade", "uncertain"]
    catalyst_strength: Literal["high", "medium", "low"]
    crowding_risk: Literal["high", "medium", "low"]
    risk_flags: List[str]

class WatchlistResponse(BaseModel):
    watchlist: List[WatchlistItem]

def generate_watchlist():
    out_dir = os.path.join(os.path.dirname(__file__), "..", "..", "out")
    candidates_path = os.path.join(out_dir, "premarket_candidates.json")
    watchlist_path = os.path.join(out_dir, "watchlist.json")
    
    # 1. Load candidates
    if not os.path.exists(candidates_path):
        print("[!] No premarket_candidates.json found. Standing down.")
        with open(watchlist_path, "w") as f:
            json.dump([], f)
        return
        
    with open(candidates_path, "r") as f:
        try:
            candidates = json.load(f)
        except json.JSONDecodeError:
            candidates = []
            
    if not candidates:
        print("[!] premarket_candidates.json is empty. Standing down.")
        with open(watchlist_path, "w") as f:
            json.dump([], f)
        return

    print(f"[*] Loaded {len(candidates)} candidates for LLM Context Compression.")
    
    # Format the prompt context
    context_lines = []
    for c in candidates:
        sym = c.get("ticker", "UNKNOWN")
        gap = c.get("gap_percent", 0.0)
        vol = c.get("volume", 0.0)
        evs = c.get("events_text", "")
        context_lines.append(f"Ticker: {sym} | Gap: {gap:.2f}% | Volume: {vol} | Events: {evs}")
        
    context_str = "\n".join(context_lines)
    
    system_instruction = (
        "You are a strict Structured Context Extractor for a momentum trading system. "
        "Your only job is to analyze the provided pre-market candidate tickers and their associated events, "
        "and return a rigidly structured JSON response categorizing the catalysts. "
        "DO NOT generate natural language prose, trade recommendations, price targets, execution instructions, "
        "or discretionary commentary. Extract exactly what is requested."
    )
    
    prompt = f"Analyze the following candidates and return the structured watchlist:\n\n{context_str}"
    
    # Initialize the GenAI client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[!] GEMINI_API_KEY not found in environment. Generating empty watchlist.")
        with open(watchlist_path, "w") as f:
            json.dump([], f)
        return
        
    client = genai.Client(api_key=api_key)
    
    print("[*] Calling Gemini to compress context and generate watchlist...")
    
    # Fallback model list to handle quota limits
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    max_retries = 2
    response_data = None
    success = False
    
    for model_name in models_to_try:
        if success:
            break
            
        print(f"[*] Attempting to generate watchlist using model: {model_name}")
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_schema=WatchlistResponse,
                        temperature=0.0 # Deterministic extraction
                    )
                )
                
                parsed_json = json.loads(response.text)
                response_data = parsed_json.get("watchlist", [])
                success = True
                print(f"[*] Watchlist generated successfully with {model_name}")
                break
            except Exception as e:
                print(f"[!] Model {model_name} attempt {attempt + 1} failed: {e}")
                
    if not success:
        print("[!] All models and retries failed. Generating empty watchlist.")
        response_data = []
                
    # Save the output
    with open(watchlist_path, "w") as f:
        json.dump(response_data, f, indent=4)
        
    print(f"[*] Successfully wrote {len(response_data)} items to watchlist.json")

if __name__ == "__main__":
    generate_watchlist()
