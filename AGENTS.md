# AI Agent Workspace Instructions & Architecture

Welcome, AI Coding Assistant. This workspace is configured using a state-of-the-art multi-agent framework designed to ensure maximum code quality, correctness, safety, and architectural integrity.

## Agent System Architecture

Every task in this repository should follow a strict hierarchy and validation pipeline:

1.  **Orchestrator Agent**: Manages the overarching workflow, decomposes tasks, creates/updates plans, and schedules subagents (e.g., using `invoke_subagent` if supported, or sequentially).
2.  **Architect Agent**: Formulates technical specifications, data models, schema definitions, and APIs prior to coding.
3.  **Developer Agent**: Translates architectural specifications into code.
4.  **Quality Auditor Agent**: Enforces code hygiene, executes unit/integration tests, and audits security vulnerabilities or secrets.
5.  **Independent Validation & Verification (IV&V) Agent**: Executes E2E, black-box checks and ensures that implementation matches original requirements without developer bias.

---

## Workspace Rules Index

You MUST read and strictly adhere to the guidelines configured inside the `.agents/rules/` directory:

1.  **[01_orchestration_and_routing.md](file:///C:/Users/pbree/source/repos/alpaca-agent-bot/.agents/rules/01_orchestration_and_routing.md)**: Standard protocols for task routing, state tracking, and planning.
2.  **[02_architectural_design.md](file:///C:/Users/pbree/source/repos/alpaca-agent-bot/.agents/rules/02_architectural_design.md)**: Clean Architecture, SOLID design, contract-first APIs, and DDD.
3.  **[03_test_driven_development.md](file:///C:/Users/pbree/source/repos/alpaca-agent-bot/.agents/rules/03_test_driven_development.md)**: Strict test-first workflow, test metrics, and test styling.
4.  **[04_code_hygiene.md](file:///C:/Users/pbree/source/repos/alpaca-agent-bot/.agents/rules/04_code_hygiene.md)**: Strict typing, lint rules, modular structure, and DRY principles.
5.  **[05_security_and_privacy.md](file:///C:/Users/pbree/source/repos/alpaca-agent-bot/.agents/rules/05_security_and_privacy.md)**: Secret handling, safe coding practices, OWASP mitigation, and dependency safety.
6.  **[06_independent_validation.md](file:///C:/Users/pbree/source/repos/alpaca-agent-bot/.agents/rules/06_independent_validation.md)**: Functional validation guidelines, black-box verification rules, and automated reporting.
7.  **[07_git_workflows.md](file:///C:/Users/pbree/source/repos/alpaca-agent-bot/.agents/rules/07_git_workflows.md)**: Conventional commits, branch naming, and merge standards.

---

## Action Plan Protocol

Before making changes to the source code:
1.  Verify the objective and constraints.
2.  Read the relevant rules and skills within `.agents/`.
3.  Formulate an implementation plan in planning mode, mapping files and verification steps.
4.  Once authorized, execute and verify using the local verification script:
    ```powershell
    ./scripts/verify-project.ps1
    ```
