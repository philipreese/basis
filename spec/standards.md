# Project Standards & Conventions

> Part of the [modular specification](README.md). The canonical ruleset (git discipline, architecture, testing, hygiene, security, doc-sync) lives in [AGENTS.md](../AGENTS.md) — read by every AI agent — and is enforced by [scripts/verify-project.ps1](../scripts/verify-project.ps1). This file covers only what AGENTS.md doesn't: the issue/PR loop, CI & release mechanics, and the pixi task table. Where the two differ, AGENTS.md wins.

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

## CI & Release

Automated via GitHub Actions — see [`spec/ci-release-setup.md`](ci-release-setup.md) for the full setup guide and troubleshooting reference.

- **CI** (`.github/workflows/ci.yml`): runs backend tests, syntax check, and frontend type/test checks on every PR and push to `main`
- **Release Please** (`.github/workflows/release-please.yml`): auto-creates versioned Release PRs from conventional commits and merges them automatically
- **Version source of truth**: `pixi.toml` `[workspace].version` — never edit by hand; release-please bumps it (and mirrors it into `pyproject.toml`)
- **CHANGELOG.md**: generated entirely by release-please from conventional commits — never edit by hand; the commit message is the changelog entry

## Common pixi tasks
| Task                             | Command               |
| -------------------------------- | --------------------- |
| Run backend + frontend           | `pixi run dev`        |
| Backend only                     | `pixi run server`     |
| Frontend only                    | `pixi run client`     |
| Regenerate TS types from OpenAPI | `pixi run sync-types` |
| All tests                        | `pixi run test`       |
| Syntax check both stacks         | `pixi run check`      |
