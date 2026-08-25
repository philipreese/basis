"""Backtest data plumbing (#796 PR-1).

Offline stores for the historical replay corpus. Bound by ADR-0015
(spec/decisions.md): this package imports nothing from backend.console,
backend.evidence, or backend.database, takes explicit filesystem paths,
and never touches the production DB.
"""
