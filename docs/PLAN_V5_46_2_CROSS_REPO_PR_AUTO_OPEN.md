# PLAN — v5.46.2: Cross-Repo PR Auto-Open (Nix) + PyPI publish

**Status:** skeleton drafted 2026-06-04. REMEDIATED 2026-06-04 per V5_46_AUDIT_2026_06_04.md (P8 implementer detail — Forgejo PR body, script skeletons, auth path, idempotency). Split from v5.46.0 per opus-reviewer. Plan-first per I27. RENUMBERED 2026-06-05 from v5.46.1 → v5.46.2 — original slot reassigned to infrastructure prep (see `docs/PLAN_V5_46_1_DISTRIBUTION_INFRA.md`). **BREW LANE RETIRED 2026-06-05 per PD-39** — scope reduced to nix-pr + PyPI publish only. See `docs/DECISIONS.md` PD-39.

**Parent plan:** `docs/PLAN_V5_46_0_DISTRIBUTION.md` (Step 7 job `open-nix-pr` — split out for token-rotation security surface).

**Effort estimate:** ~0.5-1 calendar day (reduced from original 1-2 days; brew scope retired).

**Split rationale:** v5.46.0 ships manual bump workflow (release workflow attaches assets + creates Codeberg release). Auto-open PR against `nix` repo requires cross-repo PAT (`NIX_BUMP_TOKEN`) — separate security surface worth a dedicated dispatch once v5.46.0 manual workflow is proven.

**Depends on:** v5.46.1 SHIPPED (NIX_BUMP_TOKEN configured in yadgar Forgejo secrets + `scripts/bump_version.py` operational + PyPI account/token live).

---

## Goal

Automate one PR-open action on every yadgar version tag:

1. **Nix repo PR:** clone `~/git/nix` (Codeberg) → bump `yadger_core_version` in `modules/home/yadgar.nix` → push to `bump-yadgar-v<version>` → open PR via Forgejo API.

Idempotency: if a bump PR for the exact version already exists (open or merged), skip without error.

---

## Non-goals

- No brew tap PR (retired per PD-39).
- No auto-merge of bump PRs (user reviews + merges manually).
- No signed commits in bump PRs.
- No pre-release tags trigger cross-repo PRs (conditional: `if: !contains(github.ref, '-alpha') && !contains(github.ref, '-beta')`).
- No GitHub/other-host support — Forgejo API only (Codeberg).

---

## Architecture Conformance (P1)

Cites `docs/architecture.md`:

- **Observability §**: no new Prometheus metrics from this plan (CI-only; not on the daemon hot path).
- **Security §** (`auth_middleware.py`): cross-repo PAT is a CI secret (`NIX_BUMP_TOKEN`) — stored in Forgejo repo secrets, NOT in `~/.yadgar/` or config.yaml. No yadgar-process-level secret handling required.
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

No new yadgar config knobs. CI workflow secret (`NIX_BUMP_TOKEN`) is a Forgejo repo secret — outside yadgar's I25 registry.

**Secret rotation policy** (required by split rationale security concern):
- `NIX_BUMP_TOKEN`: scoped to PR-create on `nix` repo only. Rotate on any personnel change or if token appears in logs.
- Token is a Forgejo personal access token (or bot-account token). Documented in `MIGRATION_NOTES.md`.

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
| `docs/PLAN_V5_46_0_DISTRIBUTION.md` | Parent. v5.46.0 ships `.forgejo/workflows/release.yaml` with `open-nix-pr` job as stub (if: false gated). v5.46.2 fills in that job. |
| `docs/PLAN_V5_46_1_DISTRIBUTION_INFRA.md` | Prerequisite. NIX_BUMP_TOKEN + bump_version.py + PyPI account must be live before dispatch. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | Downstream. `yadgar update --check` calls Codeberg releases API — format aligned with release automation output from v5.46.0/v5.46.2. No conflict. |
| `docs/DECISIONS.md` PD-39 | Brew lane retired. Scope reduced to nix-pr only. |

No migration number conflicts.

---

## Bug Class Precedent (P7)

**Precedent 1 — Idempotency (v5.46.0 risk §):** bump PRs opening on every tag push including retries. Idempotency check: before opening PR, call Forgejo `GET /repos/{owner}/{repo}/pulls?state=open&head={branch}` — if branch already has an open PR, skip. If merged PR exists, skip. Only open if no PR exists for this branch.

**Precedent 2 — Token scope leak:** if `NIX_BUMP_TOKEN` has write access beyond PR-create (e.g., push to `main`), a compromised CI job could push directly to the nix repo. Token MUST be scoped to PR-create only; verify token permissions in Step 0.

**Verification Probes (post-ship):**
1. Push `v5.46.2` tag → confirm PR opens on `nix` repo at `bump-yadgar-v5.46.2` → confirm PR title includes version.
2. Push same tag again (retry scenario) → confirm no duplicate PR opened (idempotency).
3. Push alpha tag `v5.46.2-alpha.1` → confirm NO PR opened (conditional gate).
4. Revoke `NIX_BUMP_TOKEN` → push tag → confirm job fails with explicit error (not silent).

---

## Rollback Path (P9)

No rollback needed. If PR-open fails: release assets are already uploaded (v5.46.0 base workflow succeeds). User manually opens nix bump PR. Document fallback command in MIGRATION_NOTES:
```bash
# Manual nix bump fallback
git clone https://codeberg.org/maxagahi/nix
cd nix
# edit modules/home/yadgar.nix: bump yadger_core_version
gh pr create --repo maxagahi/nix ...
```

---

## Dependency Pinning (P10)

CI workflow uses `curl` (system-provided) + Forgejo REST API — no new PyPI/npm deps. If a Forgejo Actions action is used (e.g., `forgejo-actions/create-pull-request`), pin to a SHA not a floating tag. Resolve in Step 0.

---

## Agent Dispatch Budget (P11)

N/A — no benchmark agent dispatch. Standard implementer dispatch; ~0.5-1 calendar day.

---

## Plan Steps (skeleton)

### Step 0 — Pre-flight
- Verify Forgejo API endpoint for PR create: `POST /api/v1/repos/{owner}/{repo}/pulls`.
- Confirm token-scoping mechanism in Codeberg UI (PAT vs bot account).
- Confirm v5.46.0 stub job is present in `.forgejo/workflows/release.yaml`.

### Step 1 — TDD scaffolding
- `yadgar/tests/test_cross_repo_pr.py`: test idempotency logic (mock Forgejo API).
- Test: `open_nix_pr(version="5.46.1", existing_prs=[])` → API called once.
- Test: `open_nix_pr(version="5.46.1", existing_prs=["bump-yadgar-v5.46.2"])` → skipped.
- Test: pre-release version → skipped.

### Step 2 — Forgejo PR-open script

`scripts/install/open_nix_pr.sh` skeleton (same pattern as former brew script; `REPO="maxagahi/nix"`, branch `"bump-yadgar-v${VERSION}"`, `TOKEN="${NIX_BUMP_TOKEN}"`, body references `yadger_core_version` in `modules/home/yadgar.nix`).

**Auth token path:** `op://Private/Codeberg/Security/PAT` (1Password anchor 15). Token stored as Forgejo repo secret `NIX_BUMP_TOKEN`. Scoped to PR-create only on nix repo — verify in Codeberg PAT settings before dispatch (Step 0).

### Step 3 — Wire into `.forgejo/workflows/release.yaml`
- Locate `open-nix-pr` stub (`if: false`-gated job added by v5.46.0).
- Replace `if: false` with `if: "!contains(github.ref, '-alpha') && !contains(github.ref, '-beta')"`.
- Fill in job steps: `git clone` nix repo, bump `yadger_core_version`, `git push`, call `scripts/install/open_nix_pr.sh $VERSION`.
- Confirm `NIX_BUMP_TOKEN` is set in Codeberg repo settings before activating.

### Step 4 — MIGRATION_NOTES + CHANGELOG
- Document token setup steps (user-action; cannot be automated).
- Document fallback manual procedure.

---

## Acceptance Criteria

- [ ] Push `v5.46.2` tag → PR auto-opens on `nix` repo.
- [ ] Push `v5.46.2` tag a second time → no duplicate PR.
- [ ] Push `v5.46.2-alpha.1` → no PR opened.
- [ ] `NIX_BUMP_TOKEN` revoked → CI job fails with non-zero exit + explicit error message.
- [ ] `pytest yadgar/tests/test_cross_repo_pr.py` green.
- [ ] CHANGELOG.md v5.46.2 entry.
- [ ] MIGRATION_NOTES.md v5.46.2 section: token setup + fallback procedure.
