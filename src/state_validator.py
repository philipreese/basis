class StateValidationError(Exception):
    """Raised when a state transition violates validation rules."""
    pass

def validate_transition(prev_state: dict, new_state: dict) -> bool:
    """
    Validates state transitions for a single symbol to prevent narrative drift.
    
    Args:
        prev_state: Dictionary containing the previous validated state.
        new_state: Dictionary containing the proposed new state.
        
    Raises:
        StateValidationError: If any assertion fails.
        
    Returns:
        bool: True if validation passes.
    """
    prev_id = prev_state.get("last_unique_id")
    new_id = new_state.get("last_unique_id")
    
    # 1. Deduplication Check (Resolve Risk 3)
    if prev_id is not None and prev_id == new_id:
        raise StateValidationError(f"Duplicate bar detected. Unique ID {new_id} already processed.")
        
    prev_regime = prev_state.get("previous_market_regime")
    new_regime = new_state.get("previous_market_regime")
    
    prev_count = prev_state.get("bars_in_trend_count", 0)
    new_count = new_state.get("bars_in_trend_count", 0)
    
    # 2. Three-State Transition Matrix Validation (Resolve Risk 2)
    if prev_regime is not None:
        if prev_regime == new_regime:
            # If remaining in the same regime, count must increment linearly
            expected_count = prev_count + 1
            if new_count != expected_count:
                raise StateValidationError(
                    f"Invalid trend count increment. Expected {expected_count}, got {new_count} "
                    f"(Regime: {new_regime})."
                )
        else:
            # If changing regime, count must reset to 1
            if new_count != 1:
                raise StateValidationError(
                    f"Invalid trend count reset. Expected 1 on regime change from "
                    f"'{prev_regime}' to '{new_regime}', got {new_count}."
                )
                
    return True
