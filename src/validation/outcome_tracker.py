def calculate_forward_returns(bars: list[dict], trigger_idx: int) -> dict:
    """
    Computes forward returns at exactly +1, +3, and +10 bar intervals from a trigger bar index.
    If there are not enough future bars available, return None for that interval.
    """
    forward_returns = {}
    close_at_trigger = bars[trigger_idx]["close"]
    
    for k in [1, 3, 10]:
        target_idx = trigger_idx + k
        if target_idx < len(bars):
            close_future = bars[target_idx]["close"]
            fwd_return = (close_future - close_at_trigger) / close_at_trigger
            forward_returns[f"return_{k}"] = round(fwd_return, 6)
        else:
            forward_returns[f"return_{k}"] = None
            
    return forward_returns
