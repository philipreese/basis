"""clock_guard.py — poison the wall clock for the duration of a replay (#796 PR-3).

Every decision-path production function takes an injectable ``today`` and
falls back to ``backend.dates.market_today()`` when it is omitted (the
``today or market_today()`` trapdoor pattern, #540). During a replay that
fallback is a silent look-ahead: the wall clock is 2026, the replay day is
2015, and a defaulted call quietly evaluates 2026 catalyst windows against
2015 positions. Instead of trusting every call site to remember ``today=``,
this guard makes the failure LOUD: while active, ANY call to
``market_today`` raises :class:`ReplayClockError`. Same tripwire philosophy
as #674's state-vocabulary test — the failure IS the review.

The subtlety: consumers do ``from backend.dates import market_today``,
which binds the FUNCTION OBJECT into their own module namespace at import
time — patching ``backend.dates.market_today`` alone does nothing for them.
The guard therefore enumerates every loaded ``backend.*`` module and
patches each attribute that holds the real function BY IDENTITY, restoring
all of them (plus any binding created by an import that happened while the
guard was active) on exit.
"""

from __future__ import annotations

import datetime
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType

import backend.dates

_REAL_MARKET_TODAY = backend.dates.market_today


class ReplayClockError(RuntimeError):
    """A decision function consulted the wall clock during a replay."""


def _poisoned_market_today() -> datetime.date:
    raise ReplayClockError(
        "market_today() was called during a replay — some decision function "
        "was invoked without an explicit today=. The replay driver must pass "
        "today= at every call site (#796)."
    )


def _backend_modules() -> list[ModuleType]:
    # This module is excluded: patching our own _REAL_MARKET_TODAY sentinel
    # would corrupt the identity reference every other patch and the restore
    # depend on (found the hard way — the guard silently un-poisoned itself).
    return [
        mod
        for name, mod in list(sys.modules.items())
        if mod is not None and (name == "backend" or name.startswith("backend.")) and name != __name__
    ]


@contextmanager
def poisoned_clock() -> Iterator[None]:
    """While active, every ``market_today`` binding in ``backend.*`` raises.

    On exit, every binding is restored — including bindings created by
    modules first imported while the guard was active (those imported the
    poisoned callable, so restoration scans for it by identity too).
    """
    patched: list[tuple[ModuleType, str]] = []
    for mod in _backend_modules():
        for attr, value in list(vars(mod).items()):
            if value is _REAL_MARKET_TODAY:
                setattr(mod, attr, _poisoned_market_today)
                patched.append((mod, attr))
    try:
        yield
    finally:
        for mod, attr in patched:
            setattr(mod, attr, _REAL_MARKET_TODAY)
        # A module imported DURING the guard bound the poisoned callable;
        # sweep for those so nothing stays poisoned after the replay ends.
        for mod in _backend_modules():
            for attr, value in list(vars(mod).items()):
                if value is _poisoned_market_today:
                    setattr(mod, attr, _REAL_MARKET_TODAY)
