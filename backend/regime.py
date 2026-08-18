import datetime
import re
from typing import Literal

# Bounded type for the active regimes
RegimeType = Literal["CALM_BULL", "HIGH_VOL_NEUTRAL", "TRENDING_BEAR", "EVENT_CATALYST"]

# Tie-breaker hierarchy: EVENT_CATALYST > TRENDING_BEAR > HIGH_VOL_NEUTRAL > CALM_BULL
REGIME_HIERARCHY: list[RegimeType] = [
    "EVENT_CATALYST",
    "TRENDING_BEAR",
    "HIGH_VOL_NEUTRAL",
    "CALM_BULL",
]


def classify_spy_sma(spy_price: float, spy_sma20: float) -> str:
    """
    Classifies SPY price relative to 20-day SMA.
    - ABOVE_STRONG: spy_price > 1.01 * spy_sma20
    - ABOVE_FLAT: 1.001 * spy_sma20 < spy_price <= 1.01 * spy_sma20
    - AT: 0.999 * spy_sma20 <= spy_price <= 1.001 * spy_sma20
    - BELOW_FLAT: 0.99 * spy_sma20 <= spy_price < 0.999 * spy_sma20
    - BELOW_FALLING: spy_price < 0.99 * spy_sma20
    """
    if spy_sma20 <= 0:
        return "ABOVE_STRONG"  # Default fallback if SMA is not provided or zero

    ratio = spy_price / spy_sma20
    if ratio > 1.01:
        return "ABOVE_STRONG"
    elif ratio > 1.001:
        return "ABOVE_FLAT"
    elif ratio >= 0.999:
        return "AT"
    elif ratio >= 0.99:
        return "BELOW_FLAT"
    else:
        return "BELOW_FALLING"


def classify_vix(vix_close: float) -> str:
    """
    Classifies VIX closing level.
    - VIX_LOW: <15
    - VIX_NORMAL: 15-20 (15 <= VIX < 20)
    - VIX_ELEVATED: 20-30 (20 <= VIX <= 30)
    - VIX_HIGH: >30
    """
    if vix_close < 15:
        return "VIX_LOW"
    elif vix_close < 20:
        return "VIX_NORMAL"
    elif vix_close <= 30:
        return "VIX_ELEVATED"
    else:
        return "VIX_HIGH"


def classify_ivr(ivr: float) -> str:
    """
    Classifies IVR (Implied Volatility Rank).
    - IVR_LOW: <30
    - IVR_MODERATE: 30-50 (30 <= IVR < 50)
    - IVR_ELEVATED: 50-70 (50 <= IVR <= 70)
    - IVR_HIGH: >70
    """
    if ivr < 30:
        return "IVR_LOW"
    elif ivr < 50:
        return "IVR_MODERATE"
    elif ivr <= 70:
        return "IVR_ELEVATED"
    else:
        return "IVR_HIGH"


def parse_catalyst(cat_str: str, today: datetime.date) -> tuple[str, bool]:
    """
    Parses a catalyst string and determines its type and if it falls within 14 days.
    Input formats can be:
    - "YYYY-MM-DD" -> defaults to MINOR
    - "FOMC:YYYY-MM-DD" -> MAJOR
    - "FOMC meeting on YYYY-MM-DD" -> MAJOR
    - "EARNINGS:YYYY-MM-DD" -> MINOR
    - Any string containing "fomc" or "major" will be treated as MAJOR.

    Returns (catalyst_type, is_active) where:
    - catalyst_type is 'MAJOR' or 'MINOR'
    - is_active is True if 0 <= days_diff <= 14
    """
    cat_str_lower = cat_str.lower()

    # Try to extract date
    date_str = ""
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", cat_str)
    if date_match:
        date_str = date_match.group(0)
    else:
        return "MINOR", False

    try:
        cat_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        return "MINOR", False

    days_diff = (cat_date - today).days
    is_active = 0 <= days_diff <= 14

    # CPI is MAJOR (#131): a scheduled macro print that moves index vol the
    # same way an FOMC decision does — selling premium into it is the exact
    # failure the catalyst gate exists to stop.
    if "fomc" in cat_str_lower or "major" in cat_str_lower or "cpi" in cat_str_lower:
        return "MAJOR", is_active
    elif "earnings" in cat_str_lower or "minor" in cat_str_lower:
        return "MINOR", is_active
    else:
        # Check if the string before ':' has a key, e.g. "FOMC:2026-06-18"
        if ":" in cat_str:
            prefix = cat_str.split(":")[0].lower().strip()
            if prefix in ("fomc", "major", "cpi"):
                return "MAJOR", is_active
        return "MINOR", is_active


def classify_catalysts(catalyst_dates: list[str], today: datetime.date) -> str:
    """
    Classifies catalyst calendar status.
    - CATALYST_MAJOR: Any major catalyst active within 14 days.
    - CATALYST_MINOR: Any minor catalyst active within 14 days (and no major).
    - CATALYST_NONE: No active catalysts.
    """
    has_major = False
    has_minor = False

    for cat in catalyst_dates:
        cat_type, is_active = parse_catalyst(cat, today)
        if is_active:
            if cat_type == "MAJOR":
                has_major = True
            elif cat_type == "MINOR":
                has_minor = True

    if has_major:
        return "CATALYST_MAJOR"
    elif has_minor:
        return "CATALYST_MINOR"
    else:
        return "CATALYST_NONE"


def classify_daily_return(daily_return: float) -> str:
    """
    Classifies daily return level (represented as decimal, e.g. 0.015 = 1.5%).
    - DAY_UP_1PLUS: return >= 1.0% (0.01)
    - DAY_FLAT: -1.0% < return < 1.0% (-0.01 < r < 0.01)
    - DAY_DOWN_1PLUS: -2.0% < return <= -1.0% (-0.02 < r <= -0.01)
    - DAY_DOWN_2PLUS: return <= -2.0% (r <= -0.02)
    """
    if daily_return >= 0.01:
        return "DAY_UP_1PLUS"
    elif daily_return > -0.01:
        return "DAY_FLAT"
    elif daily_return > -0.02:
        return "DAY_DOWN_1PLUS"
    else:
        return "DAY_DOWN_2PLUS"


def compute_regime(
    spy_price: float,
    spy_sma20: float,
    vix_close: float,
    underlying_ivrs: dict[str, float],
    spy_daily_return: float,
    catalyst_dates: list[str],
    today: datetime.date | None = None,
) -> tuple[RegimeType, dict[str, float]]:
    """
    Calculates scores for all four regimes using the scoring matrix from Section 4.2.
    Returns (winning_regime, scores_dict).
    """
    if today is None:
        today = datetime.date.today()

    # Initialize scores to 0
    scores: dict[RegimeType, float] = {
        "CALM_BULL": 0.0,
        "HIGH_VOL_NEUTRAL": 0.0,
        "TRENDING_BEAR": 0.0,
        "EVENT_CATALYST": 0.0,
    }

    # 1. SPY closing price relative to SMA20
    sma_label = classify_spy_sma(spy_price, spy_sma20)
    if sma_label == "ABOVE_STRONG":
        scores["CALM_BULL"] += 2
        scores["TRENDING_BEAR"] -= 2
    elif sma_label == "ABOVE_FLAT":
        scores["CALM_BULL"] += 1
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["TRENDING_BEAR"] -= 1
    elif sma_label == "AT":
        scores["HIGH_VOL_NEUTRAL"] += 1
    elif sma_label == "BELOW_FLAT":
        scores["TRENDING_BEAR"] += 1
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["CALM_BULL"] -= 1
    elif sma_label == "BELOW_FALLING":
        scores["TRENDING_BEAR"] += 2
        scores["CALM_BULL"] -= 2

    # 2. VIX closing level
    vix_label = classify_vix(vix_close)
    if vix_label == "VIX_LOW":
        scores["CALM_BULL"] += 2
        scores["HIGH_VOL_NEUTRAL"] -= 1
        scores["TRENDING_BEAR"] -= 1
    elif vix_label == "VIX_NORMAL":
        scores["CALM_BULL"] += 1
    elif vix_label == "VIX_ELEVATED":
        scores["HIGH_VOL_NEUTRAL"] += 2
        scores["TRENDING_BEAR"] += 1
        scores["CALM_BULL"] -= 1
    elif vix_label == "VIX_HIGH":
        scores["TRENDING_BEAR"] += 2
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["CALM_BULL"] -= 2

    # 3. IVR for each underlying in active playbooks
    # If the dict is empty, default to SPY IVR (using a neutral/average value of 25.0 if not provided)
    ivrs_to_score = underlying_ivrs.copy()
    if not ivrs_to_score:
        ivrs_to_score = {"SPY": 25.0}

    for ivr in ivrs_to_score.values():
        ivr_label = classify_ivr(ivr)
        if ivr_label == "IVR_LOW":
            scores["CALM_BULL"] += 1
            scores["HIGH_VOL_NEUTRAL"] -= 2
        elif ivr_label == "IVR_MODERATE":
            scores["CALM_BULL"] += 1
        elif ivr_label == "IVR_ELEVATED":
            scores["HIGH_VOL_NEUTRAL"] += 2
            scores["EVENT_CATALYST"] += 1
        elif ivr_label == "IVR_HIGH":
            scores["HIGH_VOL_NEUTRAL"] += 1
            scores["TRENDING_BEAR"] += 1
            scores["EVENT_CATALYST"] += 1
            scores["CALM_BULL"] -= 1

    # 4. Catalyst Calendar
    cat_label = classify_catalysts(catalyst_dates, today)
    if cat_label == "CATALYST_MAJOR":
        scores["EVENT_CATALYST"] += 3
        scores["CALM_BULL"] -= 1
    elif cat_label == "CATALYST_MINOR":
        scores["EVENT_CATALYST"] += 1
    elif cat_label == "CATALYST_NONE":
        scores["CALM_BULL"] += 1
        scores["EVENT_CATALYST"] -= 2

    # 5. daily return
    return_label = classify_daily_return(spy_daily_return)
    if return_label == "DAY_UP_1PLUS":
        scores["CALM_BULL"] += 1
        scores["TRENDING_BEAR"] -= 1
    elif return_label == "DAY_FLAT":
        scores["CALM_BULL"] += 1
        scores["HIGH_VOL_NEUTRAL"] += 1
    elif return_label == "DAY_DOWN_1PLUS":
        scores["TRENDING_BEAR"] += 1
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["CALM_BULL"] -= 1
    elif return_label == "DAY_DOWN_2PLUS":
        scores["TRENDING_BEAR"] += 2
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["CALM_BULL"] -= 2

    # Determine winning regime with risk-priority tie breaker
    max_score = max(scores.values())
    best_regimes = [r for r, s in scores.items() if s == max_score]
    if len(best_regimes) == 1:
        winning_regime = best_regimes[0]
    else:
        # Find the one that appears earliest in REGIME_HIERARCHY (highest priority)
        winning_regime = "CALM_BULL"
        for r in REGIME_HIERARCHY:
            if r in best_regimes:
                winning_regime = r
                break

    return winning_regime, scores
