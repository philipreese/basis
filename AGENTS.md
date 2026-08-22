# AI Agent Workspace Instructions & Architecture

Welcome, AI Coding Assistant. This workspace is configured with structured architectural guidelines and automated verification pipelines to ensure maximum code quality, security, and Git discipline. Domain vocabulary lives in [`CONTEXT.md`](CONTEXT.md); load-bearing decisions in [`spec/decisions.md`](spec/decisions.md).

---

## Workflow

For non-trivial tasks, follow this sequence:

1. **Plan**: Explore the codebase and write an implementation plan before coding.
2. **Architect**: Define API schemas, TypeScript types, or Pydantic models before implementing handlers or UI.
3. **Implement**: Write code per the plan, using subagents for large search or design tasks.
4. **Verify**: Run `./scripts/verify-project.ps1` and confirm tests pass before declaring done.

---

## 🚨 CRITICAL CONSTRAINTS & WORKSPACE RULES

You MUST strictly adhere to the following rules in every turn:

### 1. Git & Commit Standards
*   **Branch Constraints**: Never commit directly to `main` or `master`. Always create a separate branch prefixed with its purpose (e.g. `feat/`, `fix/`, `refactor/`, `docs/`, `test/`, `chore/` or `sprint`).
*   **Conventional Commit Formatting**: All commits must follow the conventional commits standard:
    *   **Format**: `<type>(<scope>): <Description>` (e.g. `feat(sprint2): Implement scanner`).
    *   **Tone**: Imperative mood (e.g. "Add feature" instead of "Added feature").
    *   **Capitalization**: The first letter of the description MUST be capitalized.
    *   **Punctuation**: Do not end the commit message with a period.
*   **PR titles are commits**: The repo squash-merges; the PR title becomes the commit on `main`, so PR titles MUST follow the same conventional-commit format — release-please reads them for versioning and the changelog. Don't mix `feat:` and `fix:` work in one PR.
*   **No AI attribution**: Commit messages and PR bodies contain no AI/assistant attribution, session links, or generated-by footers of any kind.
*   **Issue-Driven Workflow**: Work items are tracked as GitHub issues; the project board is the source of truth for what is open and done. Create an issue before starting work that doesn't have one (`gh issue create` — the board's Auto-add workflow puts it on the board), branch from it with `gh issue develop <n> --checkout`, and include `Closes #<n>` in the PR body so merging auto-closes the issue and the board's workflows move its card to Done. Full loop (incl. one-time board setup) in [`spec/standards.md`](spec/standards.md) → "Issue & PR workflow".

### 2. Architectural Integrity
*   **Separation of Concerns**: Isolate business logic from transport (HTTP/FastAPI), database, and third-party APIs.
*   **State-enumeration review**: When a change introduces a new state, lifecycle phase, or pending form (a new order status, a new drift kind, a resting-order window, a held verdict), list every existing predicate that enumerates states of that kind — gates, guards, sync arms, reconciliation aggregations — and re-verify each still covers the world. Predicates born correct get outgrown: the cross-book netting gate reasoned over OPEN positions from an era when open positions WERE the account's whole broker-visible exposure (#665). Name the invariant the predicate serves, then check coverage against today's states, not the states that existed when it was written. Made mechanical (#674): every order/position/book status vocabulary lives as a named set in [`backend/states.py`](backend/states.py); a query predicate spelling out a raw status literal instead of importing one fails [`backend/tests/test_state_vocabularies.py`](backend/tests/test_state_vocabularies.py), naming the offending file:line — adding a new state is one edit there, and the tripwire's failure on every predicate that still needs updating IS the review, not a reminder to do it by hand.
*   **Contract-First**: Define API schemas (Pydantic / OpenAPI) and TypeScript types *before* implementing handlers or UI code.

### 3. Test-Driven Development
*   **Branch Coverage**: Code changes must achieve a minimum of **80% branch coverage**.
*   **Mocking**: Mock external services and databases. Do not make network requests during unit tests.

### 4. Code Hygiene
*   **Strict Typing**: Zero untyped fallbacks. No use of `any` in TypeScript or un-annotated definitions in Python.
*   **Unified Tooling**: Always use **Pixi** for environment and dependency management. All linting/test runners must execute via `pixi run <command>`.

### 5. Security & Secrets
*   **No Hardcoded Credentials**: Never commit passwords, API keys, or database URLs. Store them in `.env`.
*   **Input Sanitization**: Validate all inputs to prevent injection and XSS.

### 6. Documentation Sync
*   **README**: Always update `README.md` to reflect new modules, configuration parameters, and APIs. The README must stay truthful — it describes what the system currently does, never what is planned.
*   **CHANGELOG is generated**: `CHANGELOG.md` is produced entirely by release-please from conventional commit messages. Never edit it by hand — the commit message IS the changelog entry, so write it accordingly.
*   **Updated Spec**: The `/spec` folder is a modular, concern-based specification indexed by [`spec/README.md`](spec/README.md). When behavior changes, update the **relevant concern file** (e.g. `domain-rules.md`, `api.md`, `data-models.md`) — not the frozen `spec/archive/` monolith — and keep its `Source of truth` pointer accurate.
*   **Independent Validation**: Run `./scripts/verify-project.ps1` before declaring any task done.

---

## Agent skills

Per-repo configuration for the engineering skills (triage, to-spec, to-tickets, implement, wayfinder, domain-modeling, …).

### Issue tracker

GitHub issues on `philipreese/basis` via the `gh` CLI; the project board is the source of truth. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, default names (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: glossary in `CONTEXT.md`, ADRs in `spec/decisions.md` (one decision log, no `docs/adr/`). See `docs/agents/domain.md`.
