"""fill_notice.py — plain-English headlines for fills (#1115).

Fill notifications used to read `basis:B10:o_x:open — 2 leg fill(s): BOT GLD
261120P00380000 @ 10.70, SLD ...`, leaving the operator to decode OCC
symbols and do the arithmetic. This module does the arithmetic instead:

    B10 opened a GLD bear put spread (bets GLD falls). Paid $235. Max gain
    $265 if GLD ≤ $375 on Nov 20; max loss $235 if GLD > $380. Breakeven
    $377.65.

Pure: no I/O, no database, no broker. Two callers feed it:

- the morning fill push (fill_check.py) — legs parsed from the broker's own
  execution symbols, net price from the per-leg execution prices;
- the evening digest (digest.py) — legs from the order's `combo_legs`, net
  price from the `fills` ledger (which keys legs by conId only, so it has
  no per-leg identity — but the payoff math only needs the NET).

Every amount comes from ACTUAL fill prices (never the limit) × the contract
multiplier × the filled quantity.

Fail-soft contract: every entry point returns None whenever it cannot state
the fill correctly — an unparseable symbol, a leg shape that does not match
the order's strategy, a partial combo fill, a missing leg. The caller then
sends the raw line alone. A None must never drop a notification, and a
wrong headline is worse than no headline, so every doubt returns None.
"""

import datetime
import itertools
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

DEFAULT_MULTIPLIER = 100.0
_EPS = 1e-6

OptionType = Literal["CALL", "PUT"]
Direction = Literal["LONG", "SHORT"]
Kind = Literal["OPEN", "CLOSE", "SHARE"]


class ExecutionRow(TypedDict):
    """One broker execution, as fill_check fetches it."""

    order_ref: str
    side: str  # BOT | SLD
    quantity: float
    price: float
    symbol: str  # IBKR localSymbol: padded OCC for options, ticker for shares
    multiplier: NotRequired[float]


@dataclass(frozen=True)
class OrderContext:
    """What the database knows about an order. All optional: a field the
    loader could not fill is None and the headline degrades (or falls back)."""

    strategy_type: str | None = None
    order_quantity: int | None = None  # combo contracts ordered
    leg_occs: tuple[str, ...] = ()  # order legs, ratio-expanded (BWB body twice)
    exit_trigger: str | None = None
    entry_premium: float | None = None  # per share, unsigned
    premium_direction: str | None = None  # CREDIT | DEBIT
    share_quantity: int | None = None  # shares ordered (share books)


@dataclass(frozen=True)
class Leg:
    """One leg of the POSITION (direction is the position's, not the order
    side: closing a LONG leg sells it). `ratio` is per combo."""

    option_type: OptionType
    direction: Direction
    strike: float
    expiration: datetime.date
    ratio: int = 1


STRATEGY_LABELS: dict[str, str] = {
    "BULL_CALL_SPREAD": "bull call spread",
    "BEAR_PUT_SPREAD": "bear put spread",
    "BULL_PUT_SPREAD": "bull put spread",
    "BEAR_CALL_SPREAD": "bear call spread",
    "IRON_CONDOR": "iron condor",
    "BROKEN_WING_BUTTERFLY": "broken-wing butterfly",
    "CALENDAR_SPREAD": "calendar spread",
    "LONG_STRADDLE": "long straddle",
    "LONG_STRANGLE": "long strangle",
    "LONG_PUT": "long put",
}

EXIT_REASONS: dict[str, str] = {
    "PROFIT_TARGET": "profit target",
    "LOSS_LIMIT": "stop",
    "TIME_RULE": "time exit",
    "REGIME_FLIP": "regime flip",
    "MANUAL": "manual flatten",
    "ASSIGNMENT_RISK": "assignment-risk exit",
}

_OCC_RE = re.compile(r"^([A-Z0-9.]+)(\d{6})([CP])(\d{8})$")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def money(amount: float) -> str:
    """$235, $1,250, $377.65 — no trailing .00. Unsigned."""
    v = round(abs(amount) + 1e-9, 2)
    text = f"{v:,.2f}"
    text = text.removesuffix(".00")
    return f"${text}"


def signed_money(amount: float) -> str:
    """+$120 / −$80 (a real minus sign, as the operator reads it)."""
    return ("+" if amount >= 0 else "−") + money(amount)


def article(word: str) -> str:
    """'an XSP', 'an IWM', 'a SPY', 'a GLD': tickers are read letter by
    letter, and the letters whose NAMES start with a vowel sound take 'an'.
    SPY is the exception everyone says as a word ("spy")."""
    if word.upper() in _SPOKEN_AS_WORDS:
        return "a"
    return "an" if word[:1].upper() in "AEFHILMNORSX" else "a"


_SPOKEN_AS_WORDS = frozenset({"SPY"})


def _short_date(d: datetime.date) -> str:
    return f"{d:%b} {d.day}"  # %-d is not portable to Windows


def _count_phrase(qty: int, underlying: str, label: str) -> str:
    """'a GLD bear put spread' / '2 GLD bear put spreads'."""
    if qty == 1:
        return f"{article(underlying)} {underlying} {label}"
    return f"{qty} {underlying} {label}s"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_occ(symbol: str) -> tuple[str, datetime.date, OptionType, float] | None:
    """(root, expiration, type, strike) from an OCC symbol, padded or not."""
    m = _OCC_RE.match(re.sub(r"\s+", "", symbol))
    if m is None:
        return None
    root, ymd, cp, strike = m.groups()
    try:
        exp = datetime.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    return root, exp, ("CALL" if cp == "C" else "PUT"), int(strike) / 1000.0


def parse_order_ref(ref: str) -> tuple[str, Kind] | None:
    """(book_id, kind). `basis:{book}:{id}:open` is an OPEN; `...:open:tp`
    (the resting GTC profit-taker) and `...:close` are CLOSEs; `...:share`
    is a share-book order. Anything else is not ours to describe."""
    parts = ref.split(":")
    if len(parts) < 4 or parts[0] != "basis":
        return None
    book, action, rest = parts[1], parts[3], parts[4:]
    if action == "open" and not rest:
        return book, "OPEN"
    if (action == "open" and rest == ["tp"]) or (action == "close" and not rest):
        return book, "CLOSE"
    if action == "share" and not rest:
        return book, "SHARE"
    return None


def legs_from_order(raw_legs: Sequence[Mapping[str, object]]) -> list[Leg] | None:
    """Position legs from an order's `combo_legs["legs"]` (ratio-expanded:
    a BWB body appears twice) collapsed back to one Leg per contract with
    its ratio. None on any malformed leg."""
    counts: Counter[tuple[OptionType, Direction, float, datetime.date]] = Counter()
    for raw in raw_legs:
        opt, direction, strike, exp = (
            raw.get("option_type"),
            raw.get("direction"),
            raw.get("strike"),
            raw.get("expiration"),
        )
        if opt not in ("CALL", "PUT") or direction not in ("LONG", "SHORT"):
            return None
        if not isinstance(strike, int | float) or not isinstance(exp, str):
            return None
        try:
            expiration = datetime.date.fromisoformat(exp)
        except ValueError:
            return None
        counts[(opt, direction, float(strike), expiration)] += 1  # type: ignore[index]
    if not counts:
        return None
    return sort_legs([Leg(o, d, k, e, n) for (o, d, k, e), n in counts.items()])


def sort_legs(legs: Sequence[Leg]) -> list[Leg]:
    return sorted(legs, key=lambda leg: (leg.expiration, leg.option_type, leg.strike))


def _per_symbol(executions: Sequence[ExecutionRow]) -> dict[str, tuple[str, float]] | None:
    """normalized symbol -> (side, total quantity). None when a symbol was
    both bought and sold, or a row is malformed."""
    out: dict[str, tuple[str, float]] = {}
    for e in executions:
        if e["side"] not in ("BOT", "SLD") or e["quantity"] <= 0:
            return None
        sym = re.sub(r"\s+", "", e["symbol"])
        side, q = out.get(sym, (e["side"], 0.0))
        if side != e["side"]:
            return None
        out[sym] = (side, q + e["quantity"])
    return out


def _combo_quantity(executions: Sequence[ExecutionRow]) -> int | None:
    per = _per_symbol(executions)
    if not per:
        return None
    qtys = [q for _side, q in per.values()]
    if any(abs(q - round(q)) > _EPS for q in qtys):
        return None
    return math.gcd(*(round(q) for q in qtys))


def legs_from_executions(
    executions: Sequence[ExecutionRow], kind: Kind, ctx: OrderContext
) -> tuple[str, list[Leg], int] | None:
    """(underlying, position legs, combo quantity) from the executions, or
    None when the fill is partial or unreadable. On a CLOSE the broker side
    is the REVERSE of the position's direction (SLD closes a LONG)."""
    per = _per_symbol(executions)
    combo_qty = _combo_quantity(executions)
    if per is None or combo_qty is None:
        return None
    int_qty = {sym: round(q) for sym, (_side, q) in per.items()}

    if ctx.leg_occs and ctx.order_quantity:
        # Exact check against what was ORDERED: a leg short of ratio ×
        # quantity, or a missing / extra leg, is a partial fill.
        expected = Counter(re.sub(r"\s+", "", o) for o in ctx.leg_occs)
        if set(expected) != set(int_qty):
            return None
        if any(int_qty[s] != n * ctx.order_quantity for s, n in expected.items()):
            return None
        combo_qty = ctx.order_quantity
    elif ctx.order_quantity and combo_qty < ctx.order_quantity:
        return None  # fewer contracts than ordered: partial

    legs: list[Leg] = []
    roots: set[str] = set()
    for sym, (side, _q) in per.items():
        parsed = parse_occ(sym)
        if parsed is None:
            return None
        root, exp, opt_type, strike = parsed
        roots.add(root)
        bought = side == "BOT"
        long_leg = bought if kind == "OPEN" else not bought
        legs.append(Leg(opt_type, "LONG" if long_leg else "SHORT", strike, exp, int_qty[sym] // combo_qty))
    if len(roots) != 1:
        return None
    return roots.pop(), sort_legs(legs), combo_qty


# ---------------------------------------------------------------------------
# Strategy shape
# ---------------------------------------------------------------------------


def classify(legs: Sequence[Leg]) -> str | None:
    """The strategy_type these (sorted) position legs form, or None."""
    if len({leg.expiration for leg in legs}) > 1:
        if (
            len(legs) == 2
            and all(leg.ratio == 1 for leg in legs)
            and legs[0].option_type == legs[1].option_type
            and legs[0].strike == legs[1].strike
            and legs[0].direction == "SHORT"
            and legs[1].direction == "LONG"
        ):
            return "CALENDAR_SPREAD"  # short the front month, long the back
        return None
    if len(legs) == 1:
        leg = legs[0]
        if leg.option_type == "PUT" and leg.direction == "LONG" and leg.ratio == 1:
            return "LONG_PUT"
        return None
    if len(legs) == 2 and all(leg.ratio == 1 for leg in legs):
        a, b = legs  # same expiry, so sorted by type then strike
        if a.option_type != b.option_type:
            put, call = (a, b) if a.option_type == "PUT" else (b, a)
            if put.direction == call.direction == "LONG" and put.strike <= call.strike:
                return "LONG_STRADDLE" if put.strike == call.strike else "LONG_STRANGLE"
            return None
        if a.direction == b.direction or a.strike == b.strike:
            return None
        low_long = a.direction == "LONG"
        if a.option_type == "CALL":
            return "BULL_CALL_SPREAD" if low_long else "BEAR_CALL_SPREAD"
        return "BULL_PUT_SPREAD" if low_long else "BEAR_PUT_SPREAD"
    if len(legs) == 3:
        low, mid, high = legs
        if (
            len({leg.option_type for leg in legs}) == 1
            and low.direction == high.direction == "LONG"
            and mid.direction == "SHORT"
            and low.ratio == high.ratio == 1
            and mid.ratio == 2
        ):
            return "BROKEN_WING_BUTTERFLY"
        return None
    if len(legs) == 4 and all(leg.ratio == 1 for leg in legs):
        puts = [leg for leg in legs if leg.option_type == "PUT"]
        calls = [leg for leg in legs if leg.option_type == "CALL"]
        if len(puts) == 2 and len(calls) == 2:
            lp, sp = puts  # sorted by strike
            sc, lc = calls
            if (lp.direction, sp.direction, sc.direction, lc.direction) == (
                "LONG",
                "SHORT",
                "SHORT",
                "LONG",
            ) and sp.strike < sc.strike:
                return "IRON_CONDOR"
    return None


# ---------------------------------------------------------------------------
# Expiration payoff (single-expiry structures)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Payoff:
    """Per-combo payoff at expiry, net of the opening premium, per share,
    sampled at 0 and every strike (it is linear in between)."""

    points: tuple[float, ...]
    values: tuple[float, ...]
    right_slope: float  # d(payoff)/dS above the highest strike


def payoff(legs: Sequence[Leg], net_debit: float) -> Payoff:
    def at(s: float) -> float:
        total = -net_debit
        for leg in legs:
            intrinsic = max(0.0, s - leg.strike) if leg.option_type == "CALL" else max(0.0, leg.strike - s)
            total += (1 if leg.direction == "LONG" else -1) * leg.ratio * intrinsic
        return total

    points = tuple(sorted({0.0, *(leg.strike for leg in legs)}))
    slope = sum((1 if leg.direction == "LONG" else -1) * leg.ratio for leg in legs if leg.option_type == "CALL")
    return Payoff(points, tuple(at(p) for p in points), float(slope))


def _regions(p: Payoff, target: float) -> list[tuple[float, float | None]]:
    """Maximal intervals [lo, hi] (hi None = unbounded) where payoff == target."""
    hits = [abs(v - target) < _EPS for v in p.values]
    regions: list[tuple[float, float | None]] = []
    start: float | None = None
    for i, x in enumerate(p.points):
        if not hits[i]:
            continue
        if start is None:
            start = x
        if i == len(p.points) - 1:
            regions.append((start, None if abs(p.right_slope) < _EPS else x))
        elif not hits[i + 1]:
            regions.append((start, x))
            start = None
    return regions


def _breakevens(p: Payoff) -> list[float]:
    roots: list[float] = []
    pairs = list(zip(p.points, p.values, strict=True))
    for (x0, v0), (x1, v1) in itertools.pairwise(pairs):
        if v0 * v1 < 0:
            roots.append(x0 + (x1 - x0) * (-v0) / (v1 - v0))
        elif abs(v1) < _EPS <= abs(v0):
            roots.append(x1)
    last_x, last_v = pairs[-1]
    if abs(p.right_slope) > _EPS and last_v * p.right_slope < 0:
        roots.append(last_x - last_v / p.right_slope)
    return sorted({round(r, 2) for r in roots})


def _condition(u: str, regions: Sequence[tuple[float, float | None]], strict: bool) -> str | None:
    """'GLD ≤ $375', 'XSP < $560 or XSP > $600', 'GLD is at $380' … None
    when the payoff hits the target everywhere (nothing worth stating)."""
    parts: list[str] = []
    for lo, hi in regions:
        if lo == 0 and hi is None:
            return None
        if hi is None:
            parts.append(f"{u} {'>' if strict else '≥'} {money(lo)}")
        elif lo == hi == 0:
            parts.append(f"{u} goes to $0")
        elif lo == 0:
            parts.append(f"{u} {'<' if strict else '≤'} {money(hi)}")
        elif lo == hi:
            parts.append(f"{u} is at {money(lo)}")
        else:
            parts.append(f"{u} is between {money(lo)} and {money(hi)}")
    return " or ".join(parts) if parts else None


def _bet(strategy: str, u: str, legs: Sequence[Leg]) -> str:
    shorts = sorted(leg.strike for leg in legs if leg.direction == "SHORT")
    if strategy.startswith("BULL_"):
        return f"bets {u} rises"
    if strategy.startswith("BEAR_") or strategy == "LONG_PUT":
        return f"bets {u} falls"
    if strategy == "IRON_CONDOR":
        return f"bets {u} stays between {money(shorts[0])} and {money(shorts[-1])}"
    if strategy in ("BROKEN_WING_BUTTERFLY", "CALENDAR_SPREAD"):
        return f"bets {u} settles near {money(shorts[0])}"
    return f"bets {u} makes a big move either way"  # straddle / strangle


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------


def describe_open(
    book: str, underlying: str, strategy: str, legs: Sequence[Leg], net_debit: float, qty: int, mult: float
) -> str:
    """The OPEN headline. *net_debit* is per combo per share: + paid, − collected."""
    scale = mult * qty
    cash = f"{'Paid' if net_debit >= 0 else 'Collected'} {money(net_debit * scale)}."
    what = _count_phrase(qty, underlying, STRATEGY_LABELS[strategy])
    head = f"{book} opened {what} ({_bet(strategy, underlying, legs)})."
    if strategy == "CALENDAR_SPREAD":
        return (
            f"{head} {cash} Max loss {money(net_debit * scale)} (the debit); no fixed max gain, "
            f"it depends on volatility at the {_short_date(legs[0].expiration)} expiry."
        )

    p = payoff(legs, net_debit)
    best, worst = max(p.values), min(p.values)
    if p.right_slope > _EPS:
        gain_amount, gain_cond = "unlimited", None
    else:
        gain_amount, gain_cond = money(best * scale), _condition(underlying, _regions(p, best), strict=False)
    loss_cond = _condition(underlying, _regions(p, worst), strict=True)

    # The expiry date rides on the first clause that names a condition.
    date = f" on {_short_date(legs[0].expiration)}"
    gain = f"Max gain {gain_amount}" + (f" if {gain_cond}{date}" if gain_cond else "")
    if gain_cond:
        date = ""
    loss = f"max loss {money(worst * scale)}" + (f" if {loss_cond}{date}" if loss_cond else "")
    bes = _breakevens(p)
    be = ""
    if len(bes) == 1:
        be = f" Breakeven {money(bes[0])}."
    elif bes:
        be = f" Breakevens {' and '.join(money(b) for b in bes)}."
    return f"{head} {cash} {gain}; {loss}.{be}"


def describe_close(
    book: str,
    underlying: str,
    strategy: str,
    close_paid: float,
    qty: int,
    mult: float,
    exit_trigger: str | None = None,
    entry_premium: float | None = None,
    premium_direction: str | None = None,
) -> str:
    """The CLOSE headline. *close_paid* is per combo per share: + the close
    paid, − it collected. Realized P&L only when the entry is known."""
    scale = mult * qty
    label = STRATEGY_LABELS[strategy]
    what = f"its {underlying} {label}" if qty == 1 else f"its {qty} {underlying} {label}s"
    cash = f"{'Paid' if close_paid > 0 else 'Collected'} {money(close_paid * scale)} to close"

    head = f"{book} closed {what}"
    entry = ""
    if entry_premium is not None and premium_direction in ("CREDIT", "DEBIT"):
        signed_entry = entry_premium if premium_direction == "DEBIT" else -entry_premium
        head += f" for {signed_money(-(signed_entry + close_paid) * scale)}"
        entry = f"; entry {'paid' if signed_entry >= 0 else 'collected'} {money(signed_entry * scale)}"
    reason = EXIT_REASONS.get(exit_trigger or "")
    if reason:
        head += f" ({reason})"
    return f"{head}. {cash}{entry}."


def describe_share(book: str, symbol: str, side: str, quantity: float, price: float, ordered: int | None = None) -> str:
    """'B36 bought 12 SCHB @ $25.10 ($301.20).'"""
    verb = "bought" if side in ("BOT", "BUY") else "sold"
    text = f"{book} {verb} {quantity:g} {symbol} @ {money(price)} ({money(quantity * price)})."
    if ordered is not None and quantity < ordered:
        text += f" Partial: {quantity:g} of {ordered} shares filled so far."
    return text


def describe_option_order(
    book: str,
    kind: Kind,
    underlying: str,
    legs: Sequence[Leg],
    net: float,
    qty: int,
    mult: float = DEFAULT_MULTIPLIER,
    ctx: OrderContext | None = None,
) -> str | None:
    """Shape-check the legs against the order's strategy, then describe.
    *net* is the signed per-share net of THIS order (BOT positive)."""
    ctx = ctx or OrderContext()
    shape = classify(sort_legs(legs))
    if shape is None or (ctx.strategy_type is not None and ctx.strategy_type != shape):
        return None  # the legs don't form the strategy the order says: never guess
    if kind == "OPEN":
        return describe_open(book, underlying, shape, sort_legs(legs), net, qty, mult)
    if kind == "CLOSE":
        return describe_close(
            book, underlying, shape, net, qty, mult, ctx.exit_trigger, ctx.entry_premium, ctx.premium_direction
        )
    return None


def describe_fill(order_ref: str, executions: Sequence[ExecutionRow], ctx: OrderContext | None = None) -> str | None:
    """The headline for one order's broker executions (the morning push),
    or None when it cannot be stated correctly — the caller then sends the
    raw leg line alone."""
    ctx = ctx or OrderContext()
    parsed = parse_order_ref(order_ref)
    if parsed is None or not executions:
        return None
    book, kind = parsed
    if kind == "SHARE":
        per = _per_symbol(executions)
        if per is None or len(per) != 1:
            return None
        ((symbol, (side, qty)),) = per.items()
        price = sum(e["price"] * e["quantity"] for e in executions) / qty
        return describe_share(book, symbol, side, qty, price, ctx.share_quantity)

    mults = {float(e.get("multiplier") or DEFAULT_MULTIPLIER) for e in executions}
    built = legs_from_executions(executions, kind, ctx)
    if len(mults) != 1 or built is None:
        return None
    underlying, legs, qty = built
    # Signed net per combo per share: BOT positive, SLD negative, each leg
    # weighted by its own filled quantity — so a ratio leg counts twice. On
    # an OPEN it is the debit paid; on a CLOSE what the close paid.
    net = sum((e["price"] if e["side"] == "BOT" else -e["price"]) * e["quantity"] for e in executions) / qty
    return describe_option_order(book, kind, underlying, legs, net, qty, mults.pop(), ctx)
