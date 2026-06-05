# PLAN — v5.46.2: Cross-Repo PR Auto-Open (Brew Tap + Nix Repo)

**Status:** skeleton drafted 2026-06-04. REMEDIATED 2026-06-04 per V5_46_AUDIT_2026_06_04.md (P8 implementer detail — Forgejo PR body, script skeletons, auth path, idempotency). Split from v5.46.0 per opus-reviewer. Plan-first per I27. RENUMBERED 2026-06-05 from v5.46.1 → v5.46.2 — original slot reassigned to infrastructure prep (see `docs/PLAN_V5_46_1_DISTRIBUTION_INFRA.md`) because tap repo + Forgejo secrets + `scripts/bump_version.py` were unmet dependencies for the script-authoring scope.

**Parent plan:** `docs/PLAN_V5_46_0_DISTRIBUTION.md` (Step 7 jobs `open-brew-pr` + `open-nix-pr` — split out for token-rotation security surface).

**Effort estimate:** 1-2 calendar days.

**Split rationale:** v5.46.0 ships manual bump workflow (release workflow attaches assets + creates Codeberg release). Auto-open PRs against `homebrew-yadgar` + `nix` repo require cross-repo PATs (`BREW_BUMP_TOKEN`, `NIX_BUMP_TOKEN`) — separate security surface worth a dedicated dispatch once v5.46.0 manual workflow is proven.

**Depends on:** v5.46.1 SHIPPED (homebrew-yadgar tap repo exists + BREW_BUMP_TOKEN/NIX_BUMP_TOKEN configured in yadgar Forgejo secrets + `scripts/bump_version.py` operational + optional PyPI account/token if pipx-PyPI lane is in scope).

---

## Goal

Automate two PR-open actions on every yadgar version tag:

1. **Brew tap PR:** clone `homebrew-yadgar` → render `Formula/yadgar.rb` from template → push to `bump-v<version>` branch → open PR via Forgejo API.
2. **Nix repo PR:** clone `~/git/nix` (Codeberg) → bump `yadger_core_version` in `modules/home/yadgar.nix` → push to `bump-yadgar-v<version>` → open PR.

Idempotency: if a bump PR for the exact version already exists (open or merged), skip without error.

---

## Non-goals

- No auto-merge of bump PRs (user reviews + merges manually).
- No signed commits in bump PRs.
- No pre-release tags trigger cross-repo PRs (conditional: `if: !contains(github.ref, '-alpha') && !contains(github.ref, '-beta')`).
- No GitHub/other-host support — Forgejo API only (Codeberg).

---

## Architecture Conformance (P1)

Cites `docs/architecture.md`:

- **Observability §**: no new Prometheus metrics from this plan (CI-only; not on the daemon hot path).
- **Security §** (`auth_middleware.py`): cross-repo PATs are CI secrets (`BREW_BUMP_TOKEN`, `NIX_BUMP_TOKEN`) — stored in Forgejo repo secrets, NOT in `~/.yadgar/` or config.yaml. No yadgar-process-level secret handling required.
- **Module Responsibilities §** (`hooks/`, `cli/`): all new code lives in `.forgejo/workflows/release.yaml` — CI workflow, not yadgar Python source.

## Proposed Architecture Updates

None. This is CI infrastructure only.

---

## Touched Invariants (P2)

| Invariant | Verb | Notes |
|---|---|---|
| I9 (hot path latency) | **preserves** | CI-only. No runtime impact. |
| I25 (three-way-sync registry) | **preserves** | No new config knobs. |
| I27 (plan-first) | **preserves** | This doc. |

---

## Config Knob Lifecycle (P3)

No new yadgar config knobs. CI workflow secrets (`BREW_BUMP_TOKEN`, `NIX_BUMP_TOKEN`) are Forgejo repo secrets — outside yadgar's I25 registry.

**Secret rotation policy** (required by split rationale security concern):
- `BREW_BUMP_TOKEN`: scoped to PR-create on `homebrew-yadgar` only. Rotate on any personnel change or if token appears in logs.
- `NIX_BUMP_TOKEN`: scoped to PR-create on `nix` repo only. Same rotation trigger.
- Both tokens are Forgejo personal access tokens (or bot-account tokens). Documented in `MIGRATION_NOTES.md`.

---

## Schema Constraint Lifecycle (P4)

No schema changes.

---

## MCP Contract Changes (P5)

No MCP changes. CI workflow only.

---

## Cross-Plan Coordination (P6)

| Plan | Relationship |
|---|---|
| `docs/PLAN_V5_46_0_DISTRIBUTION.md` | Parent. v5.46.0 ships `.forgejo/workflows/release.yaml` with `open-brew-pr` + `open-nix-pr` jobs as stubs (commented out or `if: false` gated). v5.46.2 fills in those jobs. Coordinate: v5.46.0 must leave the stub + document token secrets in MIGRATION_NOTES. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | Downstream. `yadgar update --check` calls Codeberg releases API — format aligned with release automation output from v5.46.0/v5.46.2. No conflict. |

No migration number conflicts.

---

## Bug Class Precedent (P7)

**Precedent 1 — Idempotency (v5.46.0 risk §):** bump PRs opening on every tag push including retries. Idempotency check: before opening PR, call Forgejo `GET /repos/{owner}/{repo}/pulls?state=open&head={branch}` — if branch already has an open PR, skip. If merged PR exists, skip. Only open if no PR exists for this branch.

**Precedent 2 — Token scope leak:** if `BREW_BUMP_TOKEN` has write access beyond PR-create (e.g., push to `main`), a compromised CI job could push directly to the tap. Token MUST be scoped to PR-create only; verify token permissions in Step 0.

**Verification Probes (post-ship):**
1. Push `v5.46.2` tag → confirm PR opens on `homebrew-yadgar` at `bump-v5.46.2` → confirm PR title includes version.
2. Push same tag again (retry scenario) → confirm no duplicate PR opened (idempotency).
3. Push alpha tag `v5.46.2-alpha.1` → confirm NO PR opened (conditional gate).
4. Revoke `BREW_BUMP_TOKEN` → push tag → confirm job fails with explicit error (not silent).

---

## Rollback Path (P9)

No rollback needed. If PR-open fails: release assets are already uploaded (v5.46.0 base workflow succeeds). User manually opens bump PR. Document fallback command in MIGRATION_NOTES:
```bash
# Manual brew bump fallback
git clone https://codeberg.org/maxagahi/homebrew-yadgar
cd homebrew-yadgar
# render Formula/yadgar.rb from yadgar repo template
gh pr create --repo maxagahi/homebrew-yadgar ...
```

---

## Dependency Pinning (P10)

CI workflow uses `curl` (system-provided) + Forgejo REST API — no new PyPI/npm deps. If a Forgejo Actions action is used (e.g., `forgejo-actions/create-pull-request`), pin to a SHA not a floating tag. Resolve in Step 0.

---

## Agent Dispatch Budget (P11)

N/A — no benchmark agent dispatch. Standard implementer dispatch; 1-2 calendar days.

---

## Plan Steps (skeleton)

### Step 0 — Pre-flight
- Verify Forgejo API endpoint for PR create: `POST /api/v1/repos/{owner}/{repo}/pulls`.
- Confirm token-scoping mechanism in Codeberg UI (PAT vs bot account).
- Confirm v5.46.0 stub jobs are present in `.forgejo/workflows/release.yaml`.

### Step 1 — TDD scaffolding
- `yadgar/tests/test_cross_repo_pr.py`: test idempotency logic (mock Forgejo API).
- Test: `open_brew_pr(version="5.46.1", existing_prs=[])` → API called once.
- Test: `open_brew_pr(version="5.46.1", existing_prs=["bump-v5.46.2"])` → skipped.
- Test: pre-release version → skipped.

### Step 2 — Forgejo PR-open script

`scripts/install/open_brew_pr.sh` skeleton:
```bash
#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:?Usage: open_brew_pr.sh <version>}"
BRANCH="bump-v${VERSION}"
REPO="maxagahi/homebrew-yadgar"
API="https://codeberg.org/api/v1"
TOKEN="${BREW_BUMP_TOKEN:?BREW_BUMP_TOKEN not set}"

# Idempotency: check for existing open PR on this branch
EXISTING=$(curl -sf -H "Authorization: token ${TOKEN}" \
  "${API}/repos/${REPO}/pulls?state=open&head=${BRANCH}&limit=1" | \
  python3 -c "import sys,json; prs=json.load(sys.stdin); print(len(prs))")
if [ "${EXISTING}" -gt 0 ]; then
  echo "PR already open for ${BRANCH} — skipping (idempotent)."
  exit 0
fi

# Also check merged: if merged PR exists for branch, skip too
MERGED=$(curl -sf -H "Authorization: token ${TOKEN}" \
  "${API}/repos/${REPO}/pulls?state=closed&head=${BRANCH}&limit=1" | \
  python3 -c "import sys,json; prs=json.load(sys.stdin); print(len([p for p in prs if p.get('merged')]))")
if [ "${MERGED}" -gt 0 ]; then
  echo "PR already merged for ${BRANCH} — skipping."
  exit 0
fi

# Edge: branch pushed but PR-open failed on prior retry — re-open PR
PR_BODY=$(cat <<EOF
## Bump yadgar to v${VERSION}

Automated PR opened by yadgar release workflow.

- Version: ${VERSION}
- Source: https://codeberg.org/maxagahi/yadgar/releases/tag/v${VERSION}
- Formula: update \`url\`, \`sha256\`, \`version\` fields.

Merge after verifying \`brew audit --strict yadgar\` passes in CI.
EOF
)

curl -sf -X POST \
  -H "Authorization: token ${TOKEN}" \
  -H "Content-Type: application/json" \
  "${API}/repos/${REPO}/pulls" \
  -d "{\"title\":\"Bump yadgar to v${VERSION}\",\"body\":$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "${PR_BODY}"),\"head\":\"${BRANCH}\",\"base\":\"main\"}"

echo "PR opened on ${REPO} for ${BRANCH}."
```

`scripts/install/open_nix_pr.sh` skeleton (same structure; `REPO="maxagahi/nix"`, branch `"bump-yadgar-v${VERSION}"`, `TOKEN="${NIX_BUMP_TOKEN}"`, body references `yadger_core_version` in `modules/home/yadgar.nix`).

**Auth token path:** `op://Private/Codeberg/Security/PAT` (1Password anchor 15). Tokens stored as Forgejo repo secrets `BREW_BUMP_TOKEN` + `NIX_BUMP_TOKEN`. Scoped to PR-create only on respective repos — verify in Codeberg PAT settings before dispatch (Step 0).

**Idempotency retry case (branch pushed, no PR):** if branch exists on remote but no PR is open and no PR is merged, the script proceeds to open a new PR. This handles the case where a prior retry pushed the branch but the PR-open curl call failed (network error). The script will re-attempt the PR create — idempotent at the branch level via `head=` filter.

**Error handling:**
- Codeberg API rate limit: curl exits non-zero on HTTP 429; `set -e` aborts script; CI job fails explicitly (non-zero exit).
- Auth failure: curl exits non-zero on HTTP 401/403; same abort.
- Branch conflict (branch already at same commit): `git push` exits non-zero; handle by checking if branch tip matches expected commit before push.

### Step 3 — Wire into `.forgejo/workflows/release.yaml`
- Locate stubs (`if: false`-gated jobs `open-brew-pr` + `open-nix-pr` added by v5.46.0).
- Replace `if: false` with `if: "!contains(github.ref, '-alpha') && !contains(github.ref, '-beta')"`.
- Fill in job steps: `git clone` tap repo, render formula template, `git push`, call `scripts/install/open_brew_pr.sh $VERSION`.
- Secrets: confirm `BREW_BUMP_TOKEN` + `NIX_BUMP_TOKEN` are set in Codeberg repo settings before activating.
- Conditional: skip pre-release tags (already in `if:` condition above).

### Step 4 — MIGRATION_NOTES + CHANGELOG
- Document token setup steps (user-action; cannot be automated).
- Document fallback manual procedure.

---

## Acceptance Criteria

- [ ] Push `v5.46.2` tag → PR auto-opens on `homebrew-yadgar`.
- [ ] Push `v5.46.2` tag a second time → no duplicate PR.
- [ ] Push `v5.46.2-alpha.1` → no PR opened.
- [ ] `BREW_BUMP_TOKEN` revoked → CI job fails with non-zero exit + explicit error message.
- [ ] `pytest yadgar/tests/test_cross_repo_pr.py` green.
- [ ] CHANGELOG.md v5.46.2 entry.
- [ ] MIGRATION_NOTES.md v5.46.2 section: token setup + fallback procedure.
