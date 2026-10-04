"""A book's behavior fingerprint — what its config_hash covers (#1049).

The evidence era (Live Gate Duration, breach window, the gate's current-hash
trade filter, as_raced_config_hash) restarts only when config_hash changes. A
hash over the book's config alone let playbook edits (#990, #1036) and engine
edits (#1040) keep old eras running, pooling trades decided by different rules.
So the hash covers everything that decides how the book trades:

- its config;
- the content of every playbook it can select — the executor's own
  whitelist-else-all rule (executor._book_playbooks), hashed in the same shape
  the playbook sync hashes (seeds.playbook_content), so "this book's playbooks
  changed" fires exactly when PLAYBOOK_SYNCED does;
- the revision of every engine it reads: its own variant, plus V0-V3 for a
  consensus book. Included even for ignore_regime books, deliberately: an
  unneeded era restart costs a few weeks, pooled evidence costs the gate;
- the regime → strategy table, for the same reason.

A share book (#1054, config key `etf_trend`) reads none of the last three; its
fingerprint is its config plus the ETF trend rules' own revision instead.

Not covered, on purpose: catalyst calendar dates (new CPI/FOMC dates are data,
not a rule change), market data and broker behavior.
"""

from backend.eligibility import REGIME_ALLOWED_STRATEGIES
from backend.engine_revisions import CONSENSUS_VARIANTS, ENGINE_REVISIONS, ETF_TREND_REVISION
from backend.seeds import _config_hash, playbook_content


def book_config_hash(config: dict, playbooks: list[dict]) -> str:
    """The config_hash a book with *config* races under, given the seed
    *playbooks* (passed in, never read from seeds here, so a caller racing a
    modified playbook set fingerprints what it actually races)."""
    if config.get("etf_trend") is not None:
        # #1054: a share book reads no playbook, no regime engine and no
        # regime table, so none of them can restart its era. What decides how
        # it trades is its config plus backend/etf_trend.py's rules, which
        # carry their own revision (pinned by a source-digest test, the
        # ENGINE_SOURCE_DIGEST pattern).
        return _config_hash({"config": config, "share_rules": ETF_TREND_REVISION})
    ids = config.get("playbook_ids")
    selected = sorted(
        (pb for pb in playbooks if not ids or pb["id"] in ids),
        key=lambda pb: (pb["id"], pb["version"]),
    )
    variants: set[str] = set()
    if config.get("engine_variant"):
        variants.add(config["engine_variant"])
    if config.get("require_consensus"):
        variants.update(CONSENSUS_VARIANTS)
    return _config_hash(
        {
            "config": config,
            "playbooks": {f"{pb['id']}@{pb['version']}": playbook_content(pb) for pb in selected},
            "engines": {v: ENGINE_REVISIONS[v] for v in sorted(variants)},
            "regime_table": {regime: sorted(allowed) for regime, allowed in REGIME_ALLOWED_STRATEGIES.items()},
        }
    )
