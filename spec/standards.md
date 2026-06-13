# Project Standards & Conventions

> Part of the [modular specification](README.md). This is the human-readable reference for how work is done in this repo. The **enforced** source is [CLAUDE.md](../CLAUDE.md) (workspace rules) and [scripts/verify-project.ps1](../scripts/verify-project.ps1) (the gate). Where the two differ, CLAUDE.md wins.

## Git & commits
- Never commit directly to `main`/`master`. Branch with a purpose prefix: `feat/`, `fix/`, `refactor/`, `docs/`, `test/`, `chore/`, or `sprint`.
- Conventional Commits: `<type>(<scope>): <Description>` — imperative mood, capitalized first letter, no trailing period. e.g. `docs(spec): Split monolithic spec into concern-based files`.

## Issue & PR workflow
Work is tracked as **GitHub issues**; the [project board](https://github.com/users/philipreese/projects/1) is the source of truth for what's open and done. Requires the GitHub CLI (`winget install --id GitHub.cli`, then `gh auth login`).

**Board setup (one-time, already enabled on this project).** In the Project's **Settings → Workflows**:
- **Auto-add to project** (filter `is:issue`) — every new issue lands on the board automatically.
- **Item closed → Done** — closing an issue moves its card to Done.

Per work item:

```bash
# 0. No issue yet? Create one — Auto-add puts it on the board:
gh issue create --title "..." --body "..."
# 1. Branch from the issue:
gh issue develop <n> --checkout     # branch linked to issue #<n>, checked out
# 2. ...implement, commit (conventional commits)...
# 3. Open the PR, referencing the issue:
gh pr create --fill                 # include "Closes #<n>" in the PR body
```

- Branch names follow the purpose-prefix convention below (pass `--name` to `gh issue develop` to control it).
- Always include `Closes #<n>` in the **PR body** (not in commit messages) to auto-close the issue on merge, and the **Item closed → Done** workflow will move the card.
- Issues and PRs share one number sequence per repo, so PR numbers interleave with issue numbers (a "missing" issue number is usually a PR).

## Architecture
- **Separation of concerns:** isolate business logic from transport (FastAPI), persistence, and third-party APIs. The three engine layers live in dedicated modules ([observation.py](../backend/observation.py), [regime.py](../backend/regime.py), [opportunity.py](../backend/opportunity.py)).
- **Contract-first:** define Pydantic/OpenAPI schemas and TypeScript types *before* implementing handlers or UI. The frontend never hand-writes API types — it regenerates them (`pixi run sync-types`).

## Testing
- Minimum **80% branch coverage** on changed code.
- Mock external services and databases; no network calls in unit tests. Alpaca and the DB are always mocked.

## Code hygiene
- Strict typing, zero untyped fallbacks. No `any` in TypeScript; no un-annotated definitions in Python.
- Unified tooling via **Pixi** — every linter/test runner executes through `pixi run <command>`.

## Security & secrets
- No hardcoded credentials. API keys and URLs live in `.env` (`ALPACA_API_KEY_ID`, `ALPACA_SECRET_KEY`, `ALPACA_LIVE_MODE`).
- Validate and sanitize all inputs.

## Documentation sync
- Update [README.md](../README.md) and [CHANGELOG.md](../CHANGELOG.md) for every new module, config parameter, or API.
- Keep `CHANGELOG.md`'s `[Unreleased]` section current — add an entry as each change lands; move to a versioned section only at release.
- Keep the spec current: edit the relevant concern file under `spec/` (indexed by [README.md](README.md)) when behavior changes, and update the matching `## Source of truth` pointer.

## CI & Release

Automated via GitHub Actions — see [`spec/ci-release-setup.md`](ci-release-setup.md) for the full setup guide and troubleshooting reference.

- **CI** (`.github/workflows/ci.yml`): runs backend tests, syntax check, and frontend type/test checks on every PR and push to `main`
- **Release Please** (`.github/workflows/release-please.yml`): auto-creates versioned Release PRs from conventional commits; auto-merges when CI passes
- **Version source of truth**: `pixi.toml` `[workspace].version` — never edit by hand; release-please bumps it
- **CHANGELOG.md**: generated automatically; never edit the versioned sections by hand

## Verification
- Run [`./scripts/verify-project.ps1`](../scripts/verify-project.ps1) before declaring any task done (secrets scan, lint, tests).

## Common pixi tasks
| Task                             | Command               |
| -------------------------------- | --------------------- |
| Run backend + frontend           | `pixi run dev`        |
| Backend only                     | `pixi run server`     |
| Frontend only                    | `pixi run client`     |
| Regenerate TS types from OpenAPI | `pixi run sync-types` |
| All tests                        | `pixi run test`       |
| Syntax check both stacks         | `pixi run check`      |
