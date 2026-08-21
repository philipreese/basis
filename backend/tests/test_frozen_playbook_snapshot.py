"""Tripwire: every newer field on EntryFilters/ExecutionSpecs/ExitRules/
PlaybookDefinitionSchema must have a default (#548).

Positions freeze their playbook under playbook_snapshot at entry time
(#260) — a legacy position minted years ago carries only the ORIGINAL
field set, forever. PositionModel.to_schema() re-validates that dict as
PlaybookDefinitionSchema on every read (console, close, resolution, the
Live Gate). Adding a REQUIRED field (no default) to any of the nested
schemas would make every existing frozen snapshot fail that validation —
bricking Layer A wholesale the moment it touches an old position.

PROSE THAT NOBODY READS IS USELESS — this is the check that enforces the
rule: it fails the instant a newer field ships without a default, instead
of relying on a reviewer remembering to check.
"""

from backend.models import PlaybookDefinitionSchema, PositionModel

# Only the fields that exist with NO default today. Anything added to
# EntryFilters/ExecutionSpecs/ExitRules/PlaybookDefinitionSchema since must
# have shipped with a default — omitted here on purpose, exactly like a
# frozen snapshot minted before that field existed.
_MINIMAL_LEGACY_SNAPSHOT = {
    "id": "legacy_playbook",
    "version": "0.1",
    "name": "Legacy Playbook",
    "underlying_ticker": "SPY",
    "strategy_type": "BULL_PUT_SPREAD",
    "entry_filters": {
        "min_ivr": 20.0,
        "max_ivr": 80.0,
        "vix_range": [12.0, 30.0],
        "required_trend": "ANY",
        "block_catalyst_14dte": True,
        "require_catalyst_14dte": False,
    },
    "execution_specs": {
        "target_dte": 30,
        "short_leg_delta": 0.2,
        "long_leg_delta": 0.05,
        "spread_width_dollars": 5.0,
        "straddle_atm": False,
    },
    "exit_rules": {
        "profit_take_pct": 50.0,
        "stop_loss_pct": 200.0,
        "mandatory_exit_dte": 21,
    },
}


def test_minimal_legacy_snapshot_validates_as_a_playbook_definition():
    # Direct pin on the schema itself — the earliest failure point if a
    # required field slips in.
    PlaybookDefinitionSchema(**_MINIMAL_LEGACY_SNAPSHOT)


def test_position_to_schema_survives_a_minimal_legacy_frozen_snapshot():
    # The actual failure mode this tripwire exists for: to_schema() is
    # called on every position read, not just at entry time.
    pos = PositionModel(
        id="pos_legacy",
        underlying="SPY",
        strategy_type="BULL_PUT_SPREAD",
        legs=[],
        entry_date="2020-01-01",
        expiration_date="2020-02-01",
        entry_premium=1.0,
        premium_direction="CREDIT",
        current_value_per_share=0.5,
        contracts=1,
        max_profit=1.0,
        max_loss=4.0,
        notes="",
        rolls=0,
        status="CLOSED",
        journal={
            "core_thesis_rationale": "t",
            "structural_invalidation": "t",
            "expected_underlying_move_pct": 1.0,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 3,
        },
        playbook_snapshot=_MINIMAL_LEGACY_SNAPSHOT,
        book_id="B00",
    )
    schema = pos.to_schema()  # must not raise
    assert schema.playbook_snapshot is not None
    assert schema.playbook_snapshot.id == "legacy_playbook"
