# PLAN — v5.46.1: Cross-Repo PR Auto-Open (Brew Tap + Nix Repo)

**Status:** skeleton drafted 2026-06-04. Split from v5.46.0 per opus-reviewer. Plan-first per I27.

**Parent plan:** `docs/PLAN_V5_46_0_DISTRIBUTION.md` (Step 7 jobs `open-brew-pr` + `open-nix-pr` — split out for token-rotation security surface).

**Effort estimate:** 1-2 calendar days.

**Split rationale:** v5.46.0 ships manual bump workflow (release workflow attaches assets + creates Codeberg release). Auto-open PRs against `homebrew-yadgar` + `nix` repo require cross-repo PATs (`BREW_BUMP_TOKEN`, `NIX_BUMP_TOKEN`) — separate security surface worth a dedicated dispatch once v5.46.0 manual workflow is proven.

**Depends on:** v5.46.0 shipped (Homebrew formula template + `homebrew-yadgar` tap repo created + `scripts/bump_version.py` operational).

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
| `docs/PLAN_V5_46_0_DISTRIBUTION.md` | Parent. v5.46.0 ships `.forgejo/workflows/release.yaml` with `open-brew-pr` + `open-nix-pr` jobs as stubs (commented out or `if: false` gated). v5.46.1 fills in those jobs. Coordinate: v5.46.0 must leave the stub + document token secrets in MIGRATION_NOTES. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | Downstream. `yadgar update --check` calls Codeberg releases API — format aligned with release automation output from v5.46.0/v5.46.1. No conflict. |

No migration number conflicts.

---

## Bug Class Precedent (P7)

**Precedent 1 — Idempotency (v5.46.0 risk §):** bump PRs opening on every tag push including retries. Idempotency check: before opening PR, call Forgejo `GET /repos/{owner}/{repo}/pulls?state=open&head={branch}` — if branch already has an open PR, skip. If merged PR exists, skip. Only open if no PR exists for this branch.

**Precedent 2 — Token scope leak:** if `BREW_BUMP_TOKEN` has write access beyond PR-create (e.g., push to `main`), a compromised CI job could push directly to the tap. Token MUST be scoped to PR-create only; verify token permissions in Step 0.

**Verification Probes (post-ship):**
1. Push `v5.46.1` tag → confirm PR opens on `homebrew-yadgar` at `bump-v5.46.1` → confirm PR title includes version.
2. Push same tag again (retry scenario) → confirm no duplicate PR opened (idempotency).
3. Push alpha tag `v5.46.1-alpha.1` → confirm NO PR opened (conditional gate).
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
- Test: `open_brew_pr(version="5.46.1", existing_prs=["bump-v5.46.1"])` → skipped.
- Test: pre-release version → skipped.

### Step 2 — Forgejo PR-open script
- `scripts/open_brew_pr.sh` + `scripts/open_nix_pr.sh` — shell scripts called by CI workflow.
- Idempotency check via Forgejo `GET /pulls`.
- Exit 0 on skip (idempotent), non-zero on actual failure.

### Step 3 — Wire into `.forgejo/workflows/release.yaml`
- Uncomment stubs + fill in with actual scripts.
- Secrets: `BREW_BUMP_TOKEN`, `NIX_BUMP_TOKEN`.
- Conditional: skip pre-release tags.

### Step 4 — MIGRATION_NOTES + CHANGELOG
- Document token setup steps (user-action; cannot be automated).
- Document fallback manual procedure.

---

## Acceptance Criteria

- [ ] Push `v5.46.1` tag → PR auto-opens on `homebrew-yadgar`.
- [ ] Push `v5.46.1` tag a second time → no duplicate PR.
- [ ] Push `v5.46.1-alpha.1` → no PR opened.
- [ ] `BREW_BUMP_TOKEN` revoked → CI job fails with non-zero exit + explicit error message.
- [ ] `pytest yadgar/tests/test_cross_repo_pr.py` green.
- [ ] CHANGELOG.md v5.46.1 entry.
- [ ] MIGRATION_NOTES.md v5.46.1 section: token setup + fallback procedure.
