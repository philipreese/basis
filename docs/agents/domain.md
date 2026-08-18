# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the domain glossary (single-context repo).
- **`spec/decisions.md`** — this repo's ADR home. All architectural decision records (ADR-0001 onward) live in this one decision log, NOT in `docs/adr/`. New ADRs are appended here in the file's existing format.
- The `spec/` folder is the modular specification, indexed by `spec/README.md` — concern files (`domain-rules.md`, `api.md`, `data-models.md`, …) hold binding behavior.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR in `spec/decisions.md`, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 — but worth reopening because…_

## Where new records go

- New glossary terms → `CONTEXT.md` (glossary only, no implementation detail).
- New ADRs → appended to `spec/decisions.md` (when a decision is hard to reverse, surprising without context, and a real trade-off).
- Behavior changes → the relevant `spec/*.md` concern file, never `spec/archive/`.
