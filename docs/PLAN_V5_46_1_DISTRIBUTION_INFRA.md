# PLAN — v5.46.1: Distribution infrastructure prep (tokens + bump script + PyPI)

**Status:** drafted 2026-06-05. Renumber of original v5.46.1 (script-authoring) → v5.46.2. This slot now covers the prerequisite infrastructure that v5.46.2 cross-repo auto-PR depends on. **BREW LANE RETIRED 2026-06-05 per PD-39** — PyPI is now mandatory (no decision required). See `docs/DECISIONS.md` PD-39.

**Parent plan:** `docs/PLAN_V5_46_0_DISTRIBUTION.md` (shipped 2026-06-05 — distribution lanes exist; this plan stands them up).

**Effort estimate:** ~0.25-0.5 cal-day (mostly USER manual actions: token setup + 1 small Python script).

**Origin:** v5.46.0 ship + post-ship verification 2026-06-05 found v5.46.1 (script-authoring) dependencies unmet — no `NIX_BUMP_TOKEN` Forgejo secret, no `scripts/bump_version.py`. Brew lane retired per PD-39. User direction: renumber + slot a dedicated infra-prep release. Per strict version-order rule, v5.46.1 ships before v5.46.2 (auto-PR scripts) before v5.47.0 (update mechanism).

---

## Goal

Stand up the cross-repo distribution surface so v5.46.2 cross-repo nix auto-PR can fire on tag push:

1. **Forgejo secret configured** — `NIX_BUMP_TOKEN` as scoped PAT.
2. **`scripts/bump_version.py`** — minimal Python helper that substitutes version in nix bump. Reused by v5.46.2 `open_nix_pr.sh`.
3. **PyPI publish** — `pipx install yadgar` path served from PyPI. PyPI is mandatory as of PD-39 (brew lane retired; pipx-from-PyPI is the primary non-nix install path).

---

## Non-goals

- No auto-PR script implementation (that is v5.46.2).
- No release.yaml workflow stub flipping (also v5.46.2).
- No brew tap (retired per PD-39).
- No Test PyPI integration (separate decision; defer unless v5.46.x explicitly opts in).

---

## DP-A — PyPI publish decision (RESOLVED)

**Resolution:** YES — PyPI publish is mandatory. Brew lane retired per PD-39 2026-06-05; PyPI is now the primary install path for non-nix users (`pipx install yadgar`). No further decision required.

**Artifacts required:**
- pypi.org account + 2FA + reserved `yadgar` package name
- `PYPI_API_TOKEN` Forgejo secret (project-scoped to `yadgar`)

See USER ACTION CHECKLIST below.

---

## Architecture Conformance (P1)

`docs/architecture.md` sections:
- **Release lifecycle**: this plan stands up the cross-repo distribution surface referenced by v5.46.0's release workflow.
- **Secret management**: Forgejo secrets are the canonical store for cross-repo PATs (matches existing `NIX_BUMP_TOKEN` placeholder comment at `.forgejo/workflows/release.yaml`).

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

No new yaml/config knobs. The PAT secrets are environment-scoped at Forgejo workflow runtime.

Forgejo secret naming convention (matches stub comment in release.yaml):
- `NIX_BUMP_TOKEN`: scoped to PR-create on `nix` repo only. Rotate on any compromise or annually.
- `PYPI_API_TOKEN`: project-scoped to `yadgar` on pypi.org. Use OIDC trusted publisher if Forgejo OIDC supported; else API token.

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
| `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN.md` | Successor. v5.46.2 flips the `if: false` stub in `.forgejo/workflows/release.yaml` (nix-pr only) once tokens are in place. v5.46.2 depends on every deliverable in this plan. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | Sibling. Update mechanism CHECK-ONLY; not affected by token infra. Sequenced after v5.46.2 per strict-order rule. |
| `docs/DECISIONS.md` PD-39 | Brew lane retired. This plan reflects reduced scope. |

No migration number conflicts (no schema work).

---

## Bug Class Precedent (P7)

**Precedent 1 — Token scope leak:** if `NIX_BUMP_TOKEN` has write access beyond PR-create (e.g., push to `main`), a compromised CI job could push directly to the nix repo. Token MUST be scoped to PR-create only; verify token permissions in Step 0.

**Precedent 2 — Secret rotation drift:** PATs expire (codeberg default 365d). If expired tokens trigger silent workflow failures, distribution stops without notice. Mitigation: set 90d-before-expiry reminder; monitor Forgejo run logs after every release.

**Verification probes (post-ship):**
1. Forgejo `yadgar` repo Settings → Actions → Secrets lists `NIX_BUMP_TOKEN` (value masked).
2. `scripts/bump_version.py --dry-run` runs without error.
3. `pypi.org` shows reserved package `yadgar` owned by your account.
4. Forgejo secrets list includes `PYPI_API_TOKEN`.

---

## Rollback Path (P9)

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
|---:|---:|---|
| 0 — Pre-flight | 0.05 | agent |
| 1 — Generate + scope NIX_BUMP_TOKEN | 0.05 | user (codeberg PAT UI + 1Password save) |
| 2 — Add NIX_BUMP_TOKEN to Forgejo secrets on maxagahi/yadgar | 0.05 | user (Settings → Secrets) |
| 3 — PyPI signup + 2FA + reserve `yadgar` + generate token + add to Forgejo secrets | 0.15 | user |
| 4 — Write `scripts/bump_version.py` skeleton + tests | 0.3 | agent (small dispatch) |
| 5 — CHANGELOG + MIGRATION_NOTES v5.46.1 + version bump 5.46.0 → 5.46.1 | 0.1 | agent |
| **Total** | **~0.25-0.5 cal-day** | mixed |

---

## Acceptance Criteria

- [ ] `NIX_BUMP_TOKEN` configured in `maxagahi/yadgar` Forgejo secrets (verified by Settings → Secrets list).
- [ ] `PYPI_API_TOKEN` added + `yadgar` package reserved on pypi.org.
- [ ] `scripts/bump_version.py --version 5.46.0 --dry-run` runs without error.
- [ ] `scripts/bump_version.py` has unit tests covering version sub + dry-run mode.
- [ ] CHANGELOG.md v5.46.1 entry.
- [ ] MIGRATION_NOTES.md v5.46.1 section documenting:
  - DP-A outcome (PyPI mandatory per PD-39)
  - Token rotation cadence (90d-before-expiry reminder)
- [ ] Post-ship verification probes 1-4 (per P7) all green.

---

## USER ACTION CHECKLIST (do these manually before agent dispatch)

Concrete step-by-step. Each item ~2-5 min. Total ~15-20 min.

### Codeberg (signup status — likely already have account at maxagahi)

**1. Generate `NIX_BUMP_TOKEN` (Codeberg PAT scoped):**
- Codeberg → User Settings → Applications → Access Tokens.
- Token name: `yadgar-nix-bump`.
- Scopes: `write:repository` ONLY (NOT `admin` / NOT `delete`).
- Repository scope: select `maxagahi/nix` ONLY.
- Expiry: 365d (codeberg default; rotate every 90d-before-expiry).
- Generate → copy token immediately (one-time display).
- SAVE in 1Password: vault `Private`, item `Codeberg/yadgar-nix-bump-PAT`.

**2. Add NIX_BUMP_TOKEN to `maxagahi/yadgar` Forgejo secrets:**
- codeberg.org/maxagahi/yadgar → Settings → Actions → Secrets.
- New Secret: name `NIX_BUMP_TOKEN`, value = step 1 token.
- Save. Value is masked from logs by default.

### PyPI (mandatory — PD-39)

**3. Sign up for PyPI:**
- pypi.org → Register (top-right).
- Username: pick (e.g., `maxagahi-yadgar` or `openfantasy`).
- Email: your address.
- 2FA mandatory (TOTP via Aegis/Authy or hardware key) — set up immediately after first login.

**4. Reserve `yadgar` package name:**
- After verifying email + 2FA, build wheel locally + upload via `twine upload dist/yadgar-5.46.0-py3-none-any.whl` to claim the name.
- Recommend: just publish v5.46.0 wheel directly to PyPI on first push (already built locally).

**5. Generate `PYPI_API_TOKEN` (project-scoped):**
- pypi.org → Account Settings → API tokens → Add API token.
- Token name: `yadgar-codeberg-release`.
- Scope: `Project: yadgar` (DO NOT pick "Entire account" — least-privilege).
- Generate → copy token (one-time display, starts with `pypi-`).
- SAVE in 1Password at `op://Private/PyPI/yadgar-api-token` (canonical reference for the project-scoped token; retrievable in scripts via `op read "op://Private/PyPI/yadgar-api-token"`). NOTE: original account-scoped bootstrap token (op://Private/PyPI/api-token) was used to reserve the `yadgar` name via twine upload of v5.46.0 wheel on 2026-06-05; should be revoked at pypi.org once project-scoped token is in Forgejo secrets and verified.
- **Note OIDC alternative:** if codeberg/Forgejo gain OIDC trusted publisher support (currently uncertain — verify before dispatch), use that instead of long-lived API token. Track via https://pypi.org/help/#trusted-publishers.

**6. Add `PYPI_API_TOKEN` to Forgejo secrets:**
- codeberg.org/maxagahi/yadgar → Settings → Actions → Secrets.
- New Secret: name `PYPI_API_TOKEN`, value = step 5 token.

### Verify

**7. Sanity check:**
- Forgejo `yadgar` repo Settings → Secrets shows `NIX_BUMP_TOKEN` + `PYPI_API_TOKEN` (values masked).

---

## What happens NEXT (after user checklist done)

Agent dispatch implements Step 4 + 5:
- `scripts/bump_version.py` (~80 LOC + tests)
- CHANGELOG.md / MIGRATION_NOTES.md updates
- Version bump 5.46.0 → 5.46.1
- Commit chain + merge to master + push to codeberg
- Post-ship verification per protocol

Then v5.46.2 can dispatch — flips the `if: false` nix-pr stub + writes `open_nix_pr.sh` + tests.

---

## Defer rationale

Functional state is correct — distribution code shipped in v5.46.0 + manual install paths (forgejo release artifact, pipx-from-PyPI, nix flake) all work. v5.46.1 is the cross-repo infra layer. Sequential ship per user strict-order rule.

---

## Open questions for USER

1. **NIX_BUMP_TOKEN expiry policy:** 90d-before-expiry reminder cadence acceptable, or do you want monthly check?
2. **PyPI Trusted Publisher (OIDC):** before generating long-lived API token, want me to verify whether Forgejo (codeberg) supports OIDC trust to PyPI? Tracker: https://docs.codeberg.org / pypi.org/help/#trusted-publishers.
