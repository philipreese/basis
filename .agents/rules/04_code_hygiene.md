# 🧼 Rule 04: Code Hygiene & Static Analysis

## 1. Goal
To maintain high readability, consistency, and simplicity in the codebase by enforcing strict type systems, linting rules, and complexity gates.

## 2. Type Safety & Linting
- **Strict Typing**: Enable and adhere to strict mode flags (e.g. `strict: true` in typescript, type hints in python with mypy). Never use `any` or untyped fallbacks unless absolutely unavoidable.
- **Linter Conformity**: Zero linter warnings or errors are tolerated in committed code. Run local linters prior to completing a task.
- **Code Style**: Follow standard language guidelines (e.g., PEP 8 for Python, Prettier/ESLint for JavaScript/TypeScript, standard rules for C#/.NET).

## 3. Complexity Gates
- **Cognitive Complexity**: Keep functions small. A single function should not exceed 25 lines or have a cyclomatic complexity greater than 10.
- **DRY (Don't Repeat Yourself)**: Refactor repeating code blocks into reusable utilities, helper classes, or hooks.
- **File Structure**: Keep file directories clean, organized, and properly named according to project idioms.

## 4. Documentation & Comments
- **Self-documenting Code**: Prefer clear variable and function names over verbose comments.
- **Docstrings**: Include descriptive docstrings/comments for exported interfaces, public classes, and complex algorithms.

## 5. Virtual Environment & Dependency Management (Pixi)
- **Unified Environment Setup**: For projects requiring Python, Node.js, or multi-language runtimes, **Pixi** (`pixi.toml`) is the preferred environment and dependency manager.
- **Dependency Registration**: Every new dependency must be explicitly declared in `pixi.toml` (e.g. using `pixi add` or editing `pixi.toml`). Direct installations via raw `pip`, `npm`, or global managers without updates to configuration or lock files are strictly prohibited.
- **Task Execution**: Always run linters, formatters, and test runners inside the Pixi environment using `pixi run <command>` or by activating the environment.
- **Environment Exclusions**: The local environment folder (`.pixi/`) must be excluded from Git tracking (`.gitignore`) and agent context indexes (`.antigravityignore`, `.claudeignore`).
