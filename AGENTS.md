# AI Agent Workspace Instructions & Architecture

Welcome, AI Coding Assistant. This workspace is configured with structured architectural guidelines and automated verification pipelines to ensure maximum code quality, security, and Git discipline.

---

## 🏛️ Agent System Architecture

Every task in this repository should follow a strict hierarchy and validation pipeline:

1.  **Orchestrator Agent**: Manages the overarching workflow, decomposes tasks, creates/updates plans, and schedules subagents (e.g., using `invoke_subagent`).
2.  **Architect Agent**: Formulates technical specifications, data models, schema definitions, and APIs prior to coding.
3.  **Developer Agent**: Translates architectural specifications into code.
4.  **Quality Auditor Agent**: Enforces code hygiene, executes unit/integration tests, and audits security vulnerabilities.
5.  **Independent Validation & Verification (IV&V) Agent**: Executes E2E, black-box checks and ensures that implementation matches original requirements without developer bias.

---

## 🚨 CRITICAL CONSTRAINTS & WORKSPACE RULES

You MUST strictly adhere to the following rules in every turn:

### 1. Git & Commit Standards (Rule 07)
*   **Branch Constraints**: Never commit directly to `main` or `master`. Always create a separate branch prefixed with its purpose (e.g. `feat/`, `fix/`, `refactor/`, `docs/`, `test/`, `chore/` or `sprint`).
*   **Conventional Commit Formatting**: All commits must follow the conventional commits standard:
    *   **Format**: `<type>(<scope>): <Description>` (e.g. `feat(sprint2): Implement scanner`).
    *   **Tone**: Imperative mood (e.g. "Add feature" instead of "Added feature").
    *   **Capitalization**: The first letter of the description MUST be capitalized.
    *   **Punctuation**: Do not end the commit message with a period.

### 2. Architectural Integrity (Rule 02)
*   **Separation of Concerns**: Isolate business logic from transport (HTTP/FastAPI), database, and third-party APIs.
*   **Contract-First**: Define API schemas (Pydantic / OpenAPI) and TypeScript types *before* implementing handlers or UI code.

### 3. Test-Driven Development (Rule 03)
*   **Branch Coverage**: Code changes must achieve a minimum of **80% branch coverage**.
*   **Mocking**: Mock external services and databases. Do not make network requests during unit tests.

### 4. Code Hygiene (Rule 04)
*   **Strict Typing**: Zero untyped fallbacks. No use of `any` in TypeScript or un-annotated definitions in Python.
*   **unified Tooling**: Always use **Pixi** for environment and dependency manager. All linting/test runners must execute via `pixi run <command>`.

### 5. Security & Secrets (Rule 05)
*   **No Hardcoded Credentials**: Never commit passwords, API keys, or database URLs. Store them in `.env`.
*   **Input Sanitization**: Validate all inputs to prevent injection and XSS.

### 6. Documentation Sync (Rule 06 & Doc Sync Skill)
*   **Synchronized Docs**: Always update `README.md` and `CHANGELOG.md` to reflect new modules, configuration parameters, and APIs.
*   **Independent Validation**: Run `./scripts/verify-project.ps1` before declaring any task done.
