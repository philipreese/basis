# CI & Release Automation Setup Guide

> Covers everything needed to replicate or re-create the automated CI + release-please pipeline in this repo.
> Adapted from the-record — order matters, read all the way through before starting.

---

## What this gives you

- **CI** runs on every PR and every push to `main`: backend tests, backend syntax check, frontend type check (svelte-check), frontend tests (vitest)
- **Release Please** auto-creates a versioned Release PR on every push to `main`
- **GitHub Release + tag** created automatically when the Release PR is merged

---

## Phase 1 — Create the PAT

Do this first. Everything else depends on it.

### Why a PAT is required

GitHub Actions workflows do not trigger from pushes made by `GITHUB_TOKEN`. If release-please uses `GITHUB_TOKEN` to:
- Create the Release PR → CI won't trigger on it (PR sits forever)
- Enable auto-merge → the merge is attributed to the Actions bot → release-please workflow won't fire after merge (tag/GitHub release never created)

A fine-grained PAT bypasses both constraints because pushes and PR events from a PAT user trigger workflows normally.

### Steps

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. Set **Repository access** to this repo only
3. Grant these **Repository permissions**:
   - Contents: **Read and write**
   - Pull requests: **Read and write**
4. Generate and copy the token immediately (you can't view it again)
5. Go to the repo → **Settings → Secrets and variables → Actions → New repository secret**
6. Name: `RELEASE_PLEASE_TOKEN`, value: the token you copied

> If you already have a PAT with these permissions from another repo, add this repo to its access list and reuse the same secret name.

---

## Phase 2 — Repo settings

All of these must be done before the first Release PR appears. Release Please fires within seconds of merging the setup PR, so configure these **before** you merge.

### 2a. Allow Actions to create PRs

**Settings → Actions → General → Workflow permissions**
Enable: ✅ **Allow GitHub Actions to create and approve pull requests**

Without this, release-please can't open the Release PR at all.

### 2b. Set rebase-only merges (recommended)

**Settings → General → Pull Requests → Merge strategies**
- ✅ Allow rebase merging
- ☐ Allow squash merging *(disable)*
- ☐ Allow merge commits *(disable)*

**Why rebase-only:** squash merging collapses all commits in a PR into one, losing the individual conventional commit types. A PR with both `feat:` and `fix:` commits would only count as one type. Rebase preserves all commits, so release-please can accurately compute whether the next version is a minor or patch bump.

> **Note:** Branch protection rules and auto-merge require GitHub Pro for private repos. This repo uses `gh pr merge --rebase` (immediate, no status-check gate) instead, which works on all plans. Release PRs only touch `CHANGELOG.md` and the version file, so merging immediately is safe.

---

## Phase 3 — The files (already committed)

These files are already in the repo. This section documents what they do and why.

### `.github/workflows/ci.yml`

Runs on every PR and every push to `main`. Jobs:

| Step | Command |
|---|---|
| Run backend tests | `pixi run test-backend` |
| Check backend syntax | `pixi run check-backend` |
| Install frontend deps | `npm ci` |
| Check frontend types | `npm run check` (svelte-check) |
| Run frontend tests | `npm test` (vitest) |

Each check is a **separate `run:` step** — GitHub annotates failures per step, making it obvious which one broke.

**pixi platform note:** `pixi.toml` must include `linux-64` in the `platforms` list. The default `win-64` only setup causes pixi to fail on `ubuntu-latest`. This is already set.

### `.github/workflows/release-please.yml`

Fires on every push to `main`. Uses the PAT (`RELEASE_PLEASE_TOKEN`) for both the release-please action and the merge step.

Critical details:
- `token` on the release-please action: uses the PAT so the PR it creates triggers CI
- `GH_TOKEN` on the merge step: also uses the PAT so the merge is attributed to the PAT user, triggering the next release-please run (which creates the tag and GitHub release)
- `gh pr merge --rebase` (not `--auto`): merges immediately without waiting for status checks — safe because Release PRs only touch `CHANGELOG.md` and the version in `pixi.toml`, and `--auto` requires branch protection rules (not available on free private repos)
- `--repo "${{ github.repository }}"`: required because there is no `actions/checkout` step in this workflow
- `fromJSON(steps.release.outputs.pr).number`: `steps.release.outputs.pr` is a JSON object, not a number; must parse it

### `release-please-config.json`

- `packages` block with `"."` key is **required** — flat config without it causes no Release PR
- `release-type: simple` — changelog-only, no language-specific version file magic beyond `extra-files`
- `extra-files` with `type: toml` and `jsonpath: $.workspace.version` targets `version` under `[workspace]` in `pixi.toml`

### `.release-please-manifest.json`

Seeded at `0.6.0` — tells release-please where history begins. It only considers commits after the corresponding `v0.6.0` git tag.

---

## Phase 4 — Bootstrap the version tag

Release-please anchors its commit search to the last git tag matching the version in the manifest. If no matching tag exists, it walks every commit since the beginning of the repo.

```bash
# Tag the commit that represents the state at v0.6.0 (last commit on main before the setup PR)
git tag v0.6.0 <commit-sha>
git push origin v0.6.0

# Create a matching GitHub release
gh release create v0.6.0 --title "v0.6.0" --notes "Baseline release"
```

The `<commit-sha>` should be the last commit on `main` before the setup branch was created.

---

## Phase 5 — Open and merge the setup PR

1. Push the branch with all files from Phase 3
2. Open the PR: `gh pr create --title "ci: Set up CI and release-please automation" ...`
3. CI will run on the PR — this is expected and fine
4. Get CI green, then merge
5. **Watch immediately**: within ~30 seconds release-please fires and opens the first Release PR

---

## Phase 6 — Verify end-to-end

After merging, confirm each stage:

| Stage | What to check |
|---|---|
| Release PR created | A PR titled `chore(main): release X.Y.Z` appears |
| CI triggered on Release PR | GitHub shows a pending `CI / test` check on the Release PR |
| CI passes | The check goes green |
| Auto-merge fires | PR merges automatically within seconds of CI passing |
| Tag + GitHub release created | `gh release list` shows the new version |

**Troubleshooting:**

If CI doesn't trigger on the Release PR:
- Is the PAT being used? (`token: ${{ secrets.RELEASE_PLEASE_TOKEN }}`)
- Does the PAT have Contents + Pull requests write permissions?

If auto-merge doesn't fire:
- Is auto-merge enabled in repo settings? (Phase 2b)
- Did the `gh pr merge` step succeed? (check Actions logs)

If the tag/release isn't created after auto-merge:
- Is `GH_TOKEN` in the auto-merge step set to the PAT (not `GITHUB_TOKEN`)?
- Check: does the push to `main` show a release-please workflow run in Actions?

---

## Version bump rules

| Commit type | Version bump |
|---|---|
| `feat` | minor (0.x.0) |
| `fix`, `perf` | patch (0.0.x) |
| `BREAKING CHANGE` footer or `!` suffix | major (x.0.0) |
| `docs`, `chore`, `refactor`, `ci`, `test` | no bump (appear in CHANGELOG only) |

---

## Day-to-day workflow

1. Write conventional commits: `feat(scope): ...`, `fix(scope): ...`, etc.
2. Open a PR — CI runs automatically
3. Merge — release-please updates (or creates) the open Release PR
4. When ready to ship, the Release PR auto-merges once CI passes
5. GitHub release and tag appear automatically

You never manually update `CHANGELOG.md` or version files.
