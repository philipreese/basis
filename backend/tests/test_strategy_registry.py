"""The StrategyType vocabulary and the builder registry must stay in lockstep.

StrategyType (models.py) is declared once and shared by playbook and position
schemas; STRATEGY_BUILDERS (strategy_builders.py) must provide a builder for
every member. Adding a strategy to one without the other fails here, at
commit time — not at scan time in production (#170).
"""

from typing import get_args

from backend.models import StrategyType
from backend.strategy_builders import STRATEGY_BUILDERS


def test_registry_covers_the_full_strategy_vocabulary():
    assert set(STRATEGY_BUILDERS) == set(get_args(StrategyType))
