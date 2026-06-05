# PLAN — v5.46.1: Distribution infrastructure prep (tap repo + tokens + bump script + PyPI decision)

**Status:** drafted 2026-06-05. Renumber of original v5.46.1 (script-authoring) → v5.46.2. This slot now covers the prerequisite infrastructure that v5.46.2 cross-repo auto-PR depends on.

**Parent plan:** `docs/PLAN_V5_46_0_DISTRIBUTION.md` (shipped 2026-06-05 — distribution lanes exist; this plan stands them up).

**Effort estimate:** ~0.5-1 cal-day (mostly USER manual actions: account/repo/token setup + 1 small Python script).

**Origin:** v5.46.0 ship + post-ship verification 2026-06-05 found v5.46.1 (script-authoring) dependencies unmet — no `homebrew-yadgar` tap repo on codeberg, no `BREW_BUMP_TOKEN`/`NIX_BUMP_TOKEN` Forgejo secrets, no `scripts/bump_version.py`. User direction: renumber + slot a dedicated infra-prep release. Per strict version-order rule, v5.46.1 ships before v5.46.2 (auto-PR scripts) before v5.47.0 (update mechanism).

---

## Goal

Stand up the cross-repo distribution surface so v5.46.2 cross-repo auto-PR can fire on tag push. Three lanes:

1. **Codeberg `homebrew-yadgar` tap repo** — user-created, public, seeded with rendered `Formula/yadgar.rb` for v5.46.0.
2. **Forgejo secrets configured** — `BREW_BUMP_TOKEN` + `NIX_BUMP_TOKEN` as scoped PATs.
3. **`scripts/bump_version.py`** — minimal Python helper that renders `Formula/yadgar.rb` from the `.in` template + substitutes version. Reused by v5.46.2 `open_brew_pr.sh`.

Plus one decision point + optional fourth lane:

4. **PyPI publish decision (DP-A)** — is the `pipx install yadgar` path served from PyPI or from forgejo release artifacts? Affects whether you need a PyPI account + token + 2FA + reserved package name.

---

## Non-goals

- No auto-PR script implementation (that is v5.46.2).
- No release.yaml workflow stub flipping (also v5.46.2).
- No brew tap PR auto-merge or auto-tagging.
- No Test PyPI integration (separate decision; defer unless v5.46.x explicitly opts in).

---

## DP-A — PyPI yes/no

**Question:** publish wheel to pypi.org so `pipx install yadgar` works out-of-the-box, or rely on forgejo release artifacts (`pipx install https://codeberg.org/maxagahi/yadgar/releases/download/v5.46.0/yadgar-5.46.0-py3-none-any.whl`)?

**Tradeoffs:**

| Aspect | PyPI publish | Forgejo artifact only |
|---|---|---|
| `pipx install yadgar` UX | works | fails (must use URL) |
| Account/2FA required | YES (pypi.org + 2FA) | NO |
| Trusted publisher / OIDC | uncertain Forgejo OIDC support | n/a |
| Long-lived API token | needed if no OIDC | n/a |
| Package name squatting risk | YES (must reserve `yadgar` early) | NO |
| Brew formula install | `depends_on "python@3.13"` + `pip install yadgar` or wheel URL | wheel URL |
| Nix flake | uses local source; no PyPI dependency | same |

**Lean (without user input):** YES publish to PyPI. The `pipx install yadgar` ergonomic is half of v5.46.0's user-facing promise; without it, the Distribution release feels half-shipped. Cost: ~10 min PyPI signup + 2FA setup + token generation. Squatting risk is real — reserve `yadgar` soon if not already.

**Resolve before dispatch:** user confirms PyPI publish in scope OR opts for forgejo-only and accepts the URL-install UX trade.

---

## Architecture Conformance (P1)

`docs/architecture.md` sections:
- **Release lifecycle**: this plan stands up the cross-repo distribution surface referenced by v5.46.0's release workflow.
- **Secret management**: Forgejo secrets are the canonical store for cross-repo PATs (matches existing `BREW_BUMP_TOKEN`/`NIX_BUMP_TOKEN` placeholder comments at `.forgejo/workflows/release.yaml:162,179`).

No architecture changes proposed.

---

## Touched Invariants (P2)

| Invariant | Verb | Notes |
|---|---|---|
| I26 (secret-gate) | **preserves** | New PATs live in Forgejo secret store, never in repo files or logs. |
| Workflow rule 2026-05-18 (build amd64 local; no dockerhub push) | **preserves** | This plan does not touch container build/push path. |
| Workflow rule "every doc on master" | **preserves** | Plan + bump_version.py both land on master. |

No new invariants.

---

## Config Knob Lifecycle (P3)

No new yaml/config knobs. The two PAT secrets are environment-scoped at Forgejo workflow runtime (`${{ secrets.BREW_BUMP_TOKEN }}`).

Forgejo secret naming convention (matches stub comments in release.yaml):
- `BREW_BUMP_TOKEN`: scoped to PR-create on `homebrew-yadgar` only. Rotate on any compromise or annually.
- `NIX_BUMP_TOKEN`: scoped to PR-create on `nix` repo only. Same rotation policy.
- (optional) `PYPI_API_TOKEN`: project-scoped to `yadgar` on pypi.org. Use OIDC trusted publisher if Forgejo OIDC supported; else API token.

---

## Schema Constraint Lifecycle (P4)

N/A — no DB changes.

---

## MCP Contract Changes (P5)

N/A — no MCP tool changes.

---

## Cross-Plan Coordination (P6)

| Plan | Relationship |
|---|---|
| `docs/PLAN_V5_46_0_DISTRIBUTION.md` | Parent. v5.46.0 shipped distribution code surface; v5.46.1 stands up the cross-repo infra v5.46.2 will activate. |
| `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN.md` | Successor. v5.46.2 flips the `if: false` stubs in `.forgejo/workflows/release.yaml` (L162 + L179) once tap repo + tokens are in place. v5.46.2 depends on every deliverable in this plan. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | Sibling. Update mechanism CHECK-ONLY; not affected by tap/token infra. Sequenced after v5.46.2 per strict-order rule. |

No migration number conflicts (no schema work).

---

## Bug Class Precedent (P7)

**Precedent 1 — Token scope leak (v5.46.2 precedent recycled):** if `BREW_BUMP_TOKEN` has write access beyond PR-create (e.g., push to `main`), a compromised CI job could push directly to the tap. Token MUST be scoped to PR-create only; verify token permissions in Step 0.

**Precedent 2 — Secret rotation drift:** PATs expire (codeberg default 365d). If expired tokens trigger silent workflow failures, distribution stops without notice. Mitigation: set 90d-before-expiry reminder; monitor Forgejo run logs after every release.

**Precedent 3 — Tap repo desync:** if `homebrew-yadgar` is manually edited between auto-PRs, future bumps may merge-conflict. Convention: tap repo accepts ONLY PRs from yadgar release workflow + bot account; no manual commits to `main`.

**Verification probes (post-ship):**
1. Codeberg shows `maxagahi/homebrew-yadgar` public repo with `Formula/yadgar.rb` rendered for v5.46.0.
2. Forgejo `yadgar` repo Settings → Actions → Secrets lists `BREW_BUMP_TOKEN` + `NIX_BUMP_TOKEN` (values masked).
3. `scripts/bump_version.py --dry-run` renders Formula/yadgar.rb diff against tap repo's current `Formula/yadgar.rb` without error.
4. (if PyPI lane in scope) `pypi.org` shows reserved package `yadgar` owned by your account.
5. (if PyPI lane in scope) Forgejo secrets list includes `PYPI_API_TOKEN`.

---

## Rollback Path (P9)

- Tap repo: created public; can be deleted via codeberg UI if abandoned (rollback = delete repo).
- Forgejo secrets: removable via Settings → Actions → Secrets → Delete. No persistence beyond secret store.
- `scripts/bump_version.py`: revert commit on master if approach changes.
- PyPI package reservation: cannot be deleted cleanly once any release uploaded (PyPI policy). If reserved-but-empty, can delete project; if any release uploaded, project name is permanently held even after delete. Decision is one-way after first upload.

---

## Dependency Pinning (P10)

| Dep | Pin | Lockfile | Upgrade policy |
|---|---|---|---|
| `requests` (for bump_version.py Forgejo API calls) | `>=2.32,<3` | pyproject.toml `[project.optional-dependencies].dev` | Quarterly review; CVE monitor via dependabot equivalent |
| Codeberg PAT spec | API v1 | n/a | Per Forgejo upstream releases |

---

## Agent Dispatch Budget (P11)

N/A — most steps are USER manual; small `scripts/bump_version.py` authoring goes inline or one small agent dispatch.

---

## Effort Estimate

| Step | Days | Owner |
|---|---:|---|
| 0 — Pre-flight + DP-A resolution | 0.1 | user decides PyPI lane |
| 1 — Create homebrew-yadgar tap repo + seed `Formula/yadgar.rb` for v5.46.0 | 0.15 | user (1 codeberg repo + 1 push) |
| 2 — Generate + scope BREW_BUMP_TOKEN + NIX_BUMP_TOKEN | 0.1 | user (codeberg PAT UI + 1Password save) |
| 3 — Add tokens to Forgejo secrets on maxagahi/yadgar | 0.05 | user (Settings → Secrets) |
| 4 — (optional, if DP-A=YES) PyPI signup + 2FA + reserve `yadgar` + generate token + add to Forgejo secrets | 0.2 | user |
| 5 — Write `scripts/bump_version.py` skeleton + tests | 0.3 | agent (small dispatch) |
| 6 — CHANGELOG + MIGRATION_NOTES v5.46.1 + version bump 5.46.0 → 5.46.1 | 0.1 | agent |
| **Total** | **~0.5-1 cal-day** | mixed |

---

## Acceptance Criteria

- [ ] `maxagahi/homebrew-yadgar` public repo exists on codeberg with `Formula/yadgar.rb` rendered for v5.46.0 (downloads matching forgejo release asset).
- [ ] `BREW_BUMP_TOKEN` + `NIX_BUMP_TOKEN` configured in `maxagahi/yadgar` Forgejo secrets (verified by Settings → Secrets list).
- [ ] (DP-A decision recorded) PyPI publish scope decided + (if yes) `PYPI_API_TOKEN` added + `yadgar` package reserved.
- [ ] `scripts/bump_version.py --version 5.46.0 --dry-run` renders Formula/yadgar.rb diff without error.
- [ ] `scripts/bump_version.py` has unit tests covering version sub + dry-run mode + tap-repo-clone path.
- [ ] CHANGELOG.md v5.46.1 entry.
- [ ] MIGRATION_NOTES.md v5.46.1 section documenting:
  - Tap repo URL + how users add to brew: `brew tap maxagahi/yadgar https://codeberg.org/maxagahi/homebrew-yadgar`
  - DP-A outcome (PyPI yes/no)
  - Token rotation cadence (90d-before-expiry reminder)
- [ ] Post-ship verification probes 1-5 (per P7) all green.

---

## USER ACTION CHECKLIST (do these manually before agent dispatch)

Concrete step-by-step. Each item ~2-5 min. Total ~30-45 min.

### Codeberg (signup status — likely already have account at maxagahi)

**1. Create `homebrew-yadgar` tap repo:**
- Log into codeberg.org as `maxagahi`.
- Top-right `+` → New Repository.
- Owner: `maxagahi`. Name: `homebrew-yadgar` (literal — Homebrew convention).
- Visibility: PUBLIC (Homebrew taps must be reachable by brew CLI).
- Description: "Homebrew tap for openfantasy/yadgar — memory engine for Claude Code"
- Initialize with README: YES.
- Click Create.

**2. Generate `BREW_BUMP_TOKEN` (Codeberg PAT scoped):**
- Codeberg → User Settings → Applications → Access Tokens.
- Token name: `yadgar-brew-bump`.
- Scopes: `write:repository` ONLY (NOT `admin` / NOT `delete`).
- Repository scope: select `maxagahi/homebrew-yadgar` ONLY (NOT all repos).
- Expiry: 365d (codeberg default; rotate every 90d-before-expiry).
- Generate → copy token immediately (one-time display).
- SAVE in 1Password: vault `Private`, item `Codeberg/yadgar-brew-bump-PAT`.

**3. Generate `NIX_BUMP_TOKEN` (Codeberg PAT scoped):**
- Same as step 2 but:
- Token name: `yadgar-nix-bump`.
- Scope: `write:repository` ONLY.
- Repository scope: select `maxagahi/nix` ONLY.
- SAVE in 1Password: item `Codeberg/yadgar-nix-bump-PAT`.

**4. Add BOTH tokens to `maxagahi/yadgar` Forgejo secrets:**
- codeberg.org/maxagahi/yadgar → Settings → Actions → Secrets.
- New Secret: name `BREW_BUMP_TOKEN`, value = step 2 token.
- New Secret: name `NIX_BUMP_TOKEN`, value = step 3 token.
- Save both. Values are masked from logs by default.

### PyPI (DP-A — only if YES)

**5. Sign up for PyPI:**
- pypi.org → Register (top-right).
- Username: pick (e.g., `maxagahi-yadgar` or `openfantasy`).
- Email: your address.
- 2FA mandatory (TOTP via Aegis/Authy or hardware key) — set up immediately after first login.

**6. Reserve `yadgar` package name:**
- After verifying email + 2FA, go to https://pypi.org/account/projects/ → no projects yet.
- Easiest: build wheel locally + upload via `twine upload dist/yadgar-5.46.0-py3-none-any.whl` to claim the name.
- OR: register the project name via Setuptools metadata upload (more involved; UI doesn't have a "reserve only" path).
- Recommend: just publish v5.46.0 wheel directly to PyPI on first push (already built locally — `dist/yadgar-5.46.0-py3-none-any.whl`).

**7. Generate `PYPI_API_TOKEN` (project-scoped):**
- pypi.org → Account Settings → API tokens → Add API token.
- Token name: `yadgar-codeberg-release`.
- Scope: `Project: yadgar` (DO NOT pick "Entire account" — least-privilege).
- Generate → copy token (one-time display, starts with `pypi-`).
- SAVE in 1Password: vault `Private`, item `PyPI/yadgar-codeberg-release-token`.
- **Note OIDC alternative:** if codeberg/Forgejo gain OIDC trusted publisher support (currently uncertain — verify before dispatch), use that instead of long-lived API token. Track via https://pypi.org/help/#trusted-publishers.

**8. Add `PYPI_API_TOKEN` to Forgejo secrets:**
- codeberg.org/maxagahi/yadgar → Settings → Actions → Secrets.
- New Secret: name `PYPI_API_TOKEN`, value = step 7 token.

### Test PyPI (optional, recommended)

**9. (optional) Test PyPI account for dry-run uploads:**
- test.pypi.org → Register (separate account from main pypi.org).
- Same 2FA setup.
- Same project-scoped token generation.
- Forgejo secret name: `TEST_PYPI_API_TOKEN`.
- Use case: every release workflow uploads to test.pypi.org first; if passes, then production pypi.org.

### Verify

**10. Sanity check:**
- `curl -s https://codeberg.org/maxagahi/homebrew-yadgar` returns 200 (repo exists).
- `gh secret list --repo maxagahi/yadgar` (if `gh` configured for codeberg) OR codeberg UI Settings → Secrets shows 2-3 secrets (BREW/NIX + optional PYPI).

---

## What happens NEXT (after user checklist done)

Agent dispatch implements Step 5 + 6:
- `scripts/bump_version.py` (~80 LOC + tests)
- CHANGELOG.md / MIGRATION_NOTES.md updates
- Version bump 5.46.0 → 5.46.1
- Commit chain + merge to master + push to codeberg
- User builds amd64 image (per workflow rule) + bumps nix repo (manual or agent-assisted) + applies via home-manager switch
- Post-ship verification per protocol

Then v5.46.2 can dispatch — flips the `if: false` stubs + writes `open_brew_pr.sh` + `open_nix_pr.sh` + tests.

---

## Defer rationale

Functional state is correct — distribution code shipped in v5.46.0 + manual install paths (forgejo release artifact, brew tap once created, nix flake) all work. v5.46.1 is the cross-repo infra layer. Sequential ship per user strict-order rule.

---

## Open questions for USER

1. **DP-A:** PyPI publish lane — YES or NO for v5.46.x cycle?
2. **Tap repo seeding:** ship initial `Formula/yadgar.rb` rendered for v5.46.0 (already-shipped version) or wait for v5.46.1 release and seed at that tag?
3. **Token expiry policy:** 90d-before-expiry reminder cadence acceptable, or do you want monthly check?
4. **PyPI Trusted Publisher (OIDC):** before generating long-lived API token, want me to verify whether Forgejo (codeberg) supports OIDC trust to PyPI? Tracker: https://docs.codeberg.org / pypi.org/help/#trusted-publishers.
