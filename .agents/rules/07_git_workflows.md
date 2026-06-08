# 🌿 Rule 07: Git Workflow & Commit Standards

## 1. Goal
To maintain a clean, readable, and structured version control history that enables automated changelogs and easy code reviews.

## 2. Branch Naming Conventions
Use descriptive, lowercase branch names prefixing the purpose:
- **`feat/`**: New feature development (e.g., `feat/auth-google`)
- **`fix/`**: Bug and error fixes (e.g., `fix/login-crash`)
- **`refactor/`**: Code restructuring without changing behavior (e.g., `refactor/api-client`)
- **`docs/`**: Documentation updates only (e.g., `docs/api-readme`)
- **`test/`**: Adding or correcting tests only (e.g., `test/auth-coverage`)
- **`chore/`**: General tasks or build configs (e.g., `chore/dependency-update`)

## 3. Commit Message Standards
Enforce **Conventional Commits** formatting for all commits:
Format: `<type>(<optional-scope>): <description>`

### Guidelines:
- **Type**: Must be one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
- **Subject Length**: Limit the first line to 50 characters.
- **Capitalization**: Capitalize the first letter of the description.
- **Punctuation**: Do not end the description with a period.
- **Imperative Tone**: Write the commit message in the imperative mood (e.g., "Add feature" instead of "Added feature" or "Adds feature").

### Examples:
- `feat(auth): Add Google OAuth2 sign-in integration`
- `fix(db): Resolve memory leak in query builder pool`
- `docs(readme): Add installation guide for Windows`

## 4. PR & Merge Strategy
- **Quality Gates**: All pre-commit checks and validation pipelines (`verify-project.ps1`) must pass.
- **Squash & Merge**: Prefer squashing commits when merging feature branches to keep the `main` branch history linear and clean.

## 5. Agent-Specific Git Constraints
- **Branch Creation**: Coding agents MUST create a separate branch (prefixed with the conventions above) for any task involving code modifications. Committing directly to the `main` or `master` branch is strictly prohibited.
- **Branch Off Point**: Always pull the latest changes from the remote tracking branch and branch off the current stable `main` or integration branch.
- **Commit Frequency**: Commit in small, logical, atomic increments (e.g., after writing tests, implementing a module, or refactoring). Avoid giant single-commit pull requests.
- **Pushing & Merging**:
  - Agents may push feature branches to the remote repository for sharing, CI checks, or backup after local verification passes.
  - Agents MUST NOT perform merges into `main` or other protected branches without explicit user authorization or a pull request review.
