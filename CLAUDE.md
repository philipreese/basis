# AI Agent Workspace Instructions & Architecture

Welcome, AI Coding Assistant. This workspace is configured with structured architectural guidelines and automated verification pipelines to ensure maximum code quality, security, and Git discipline.

---

## Workflow

For non-trivial tasks, follow this sequence:

1. **Plan**: Enter plan mode to explore the codebase and write an implementation plan before coding.
2. **Architect**: Define API schemas, TypeScript types, or Pydantic models before implementing handlers or UI.
3. **Implement**: Write code per the plan, using subagents (Explore, Plan) for large search or design tasks.
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
*   **Issue-Driven Workflow**: Work items are tracked as GitHub issues; the project board is the source of truth for what is open and done. Create an issue before starting work that doesn't have one (`gh issue create` — the board's Auto-add workflow puts it on the board), branch from it with `gh issue develop <n> --checkout`, and include `Closes #<n>` in the PR body so merging auto-closes the issue and the board's workflows move its card to Done. Full loop (incl. one-time board setup) in [`spec/standards.md`](spec/standards.md) → "Issue & PR workflow".

### 2. Architectural Integrity
*   **Separation of Concerns**: Isolate business logic from transport (HTTP/FastAPI), database, and third-party APIs.
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
*   **Synchronized Docs**: Always update `README.md` and `CHANGELOG.md` to reflect new modules, configuration parameters, and APIs.
*   **Unreleased Section**: Keep the `[Unreleased]` section of `CHANGELOG.md` current — add an entry for every change as it lands. Move entries to a versioned section only at release time.
*   **Updated Spec**: The `/spec` folder is a modular, concern-based specification indexed by [`spec/README.md`](spec/README.md). When behavior changes, update the **relevant concern file** (e.g. `domain-rules.md`, `api.md`, `data-models.md`) — not the frozen `spec/archive/` monolith — and keep its `Source of truth` pointer accurate.
*   **Independent Validation**: Run `./scripts/verify-project.ps1` before declaring any task done.
