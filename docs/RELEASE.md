# Release Checklist

## When using the long-lived feature-branch workflow (see `claude-workflow.md`)

The final integration PR (`feat/vX.Y → master`) ships the version bump in the same PR. There is **no separate chore-bump PR**. Both v5.0 (#64) and v5.1 (#66) followed this pattern.

The `no-release` label exists only for direct-to-master PRs that touch `yadgar/**` but legitimately do not ship a release (e.g. doc-only changes, test-only fixes that don't warrant a version bump).

## Checklist

1. Before opening the master PR, on the integration branch (`feat/vX.Y`), bump:
   - `pyproject.toml` version
   - `docker-compose.yml` `CORE_VERSION` default
   - `server.json` (auto-synced by pre-commit `sync-version` hook)
   - `uv.lock` (auto-synced by pre-commit `sync-uv-lock` hook)
   - Decide `backend_version`: bump iff `embed_service.py` / `Dockerfile.backend` changed in this release
2. Push. CI `.forgejo/workflows/release-check.yaml` should pass — it only fails when `pyproject.toml` version still matches the latest tag while `yadgar/**` files changed.
3. Open one PR: `feat/vX.Y → master`. Include the bump in the same PR.
4. Merge.
5. `git tag v{X.Y.0} && git push origin v{X.Y.0}`
6. Update `~/git/nix/modules/home/yadgar.nix` `yadger_core_version` (+ `yadger_backend_version` if it moved).
7. `home-manager switch` (or whichever activation command applies).
8. Append release entry to the `yadgar-roadmap-future-improvements` wiki.
9. Verify deployed: `recall("yadgar version")` returns new version.

## Forgot the bump? (post-PR-open recovery)

If CI fails the release-readiness check after the PR is already open, do NOT split into a separate chore PR. Push the bump commits on the same `feat/vX.Y` branch — the PR picks them up. Pattern from v5.1 (PR #66):

```bash
git checkout feat/vX.Y
# edit pyproject.toml + docker-compose.yml
git add pyproject.toml docker-compose.yml
git commit -m "chore: bump core version A.B.C → X.Y.0"
# pre-commit auto-syncs server.json + uv.lock; re-stage + commit if it does
git add server.json uv.lock
git commit --amend --no-edit
git push origin feat/vX.Y
```
