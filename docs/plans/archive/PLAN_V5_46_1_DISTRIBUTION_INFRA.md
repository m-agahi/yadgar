# PLAN — v5.46.1: Distribution infrastructure prep (bump script + PyPI + pre-commit flake.nix sync)

**Status:** drafted 2026-06-05. Renumber of original v5.46.1 (script-authoring) → v5.46.2 (itself now RETIRED per PD-40). This slot covers: (1) `scripts/bump_version.py` helper, (2) PyPI publish, (3) pre-commit hook extension for flake.nix sync (already committed @53de97a). **BREW LANE RETIRED 2026-06-05 per PD-39** — PyPI is now mandatory. **NIX_BUMP_TOKEN + cross-repo nix PR RETIRED 2026-06-05 per PD-40** — replaced by pre-commit flake.nix sync. See `docs/DECISIONS.md` PD-39 and PD-40.

**Parent plan:** `docs/PLAN_V5_46_0_DISTRIBUTION.md` (shipped 2026-06-05 — distribution lanes exist; this plan stands them up).

**Effort estimate:** ~0.2-0.3 cal-day (mostly USER manual actions: PyPI token setup + 1 small Python script).

**Origin:** v5.46.0 ship + post-ship verification 2026-06-05 found v5.46.1 (script-authoring) dependencies unmet — no `scripts/bump_version.py`. Brew lane retired per PD-39. Nix cross-repo PR lane retired per PD-40 (pre-commit hook now auto-syncs flake.nix). User direction: renumber + slot a dedicated infra-prep release. Per strict version-order rule, v5.46.1 ships before v5.47.0 (update mechanism).

---

## Goal

Stand up the distribution surface for v5.46.1:

1. **`scripts/bump_version.py`** — minimal Python helper that substitutes version in pyproject.toml and related files.
2. **PyPI publish** — `pipx install yadgar` path served from PyPI. PyPI is mandatory as of PD-39 (brew lane retired; pipx-from-PyPI is the primary non-nix install path).
3. **Pre-commit hook extension** (`scripts/sync_version.py` + `scripts/check_versions.py` + `.pre-commit-config.yaml`) — already committed @53de97a — auto-updates `flake.nix` line 41 on `pyproject.toml` version bump + verifies consistency across all version-pinned files (pyproject.toml, server.json, docker-compose.yml, uv.lock, flake.nix) on every commit.

---

## Non-goals

- No auto-PR script implementation (v5.46.2 RETIRED per PD-40 — replaced by pre-commit hook).
- No release.yaml workflow stub flipping for nix (open-nix-pr stub DELETED per PD-40).
- No brew tap (retired per PD-39).
- No NIX_BUMP_TOKEN or cross-repo nix PR (retired per PD-40).
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
- **Secret management**: Forgejo secrets are the canonical store for CI PATs. `PYPI_API_TOKEN` follows this pattern. `NIX_BUMP_TOKEN` removed from scope per PD-40; `open-nix-pr` stub deleted from release.yaml.

No architecture changes proposed.

---

## Touched Invariants (P2)

| Invariant | Verb | Notes |
|---|---|---|
| I26 (secret-gate) | **preserves** | PATs live in Forgejo secret store; no cross-repo PAT needed (NIX_BUMP_TOKEN retired per PD-40). PYPI_API_TOKEN is the only new secret. |
| Workflow rule 2026-05-18 (build amd64 local; no dockerhub push) | **preserves** | This plan does not touch container build/push path. |
| Workflow rule "every doc on master" | **preserves** | Plan + bump_version.py both land on master. |

No new invariants.

---

## Config Knob Lifecycle (P3)

No new yaml/config knobs. The PAT secrets are environment-scoped at Forgejo workflow runtime.

Forgejo secret naming convention:
- `PYPI_API_TOKEN`: project-scoped to `yadgar` on pypi.org. Use OIDC trusted publisher if Forgejo OIDC supported; else API token. Rotate on any compromise or annually.

Note: `NIX_BUMP_TOKEN` removed from this plan per PD-40 (cross-repo nix PR lane retired 2026-06-05; pre-commit flake.nix sync replaces it).

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
| `docs/PLAN_V5_46_0_DISTRIBUTION.md` | Parent. v5.46.0 shipped distribution code surface; v5.46.1 stands up the PyPI + bump_version.py infra. |
| `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN.md` | **RETIRED per PD-40** (2026-06-05). v5.46.2 had no remaining deliverables after PD-39 brew drop + PD-40 nix-PR drop. Preserved as archaeological artifact. Version number not reused; next release after v5.46.1 is v5.47.0. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | Sibling. Update mechanism CHECK-ONLY; not affected by token infra. Sequenced after v5.46.1 per strict-order rule. |
| `docs/DECISIONS.md` PD-39 | Brew lane retired. This plan reflects reduced scope. |
| `docs/DECISIONS.md` PD-40 | Nix cross-repo PR lane retired. `open-nix-pr` stub DELETED from release.yaml. NIX_BUMP_TOKEN removed from this plan. |

No migration number conflicts (no schema work).

---

## Bug Class Precedent (P7)

**Precedent 1 — Token scope leak:** cross-repo PATs with write access beyond intended scope can allow a compromised CI job to push directly to external repos. General mitigation: scope any PAT to minimum required access. (NIX_BUMP_TOKEN removed from this plan per PD-40 — pre-commit hook replaces the cross-repo action entirely.)

**Precedent 2 — Secret rotation drift:** PATs expire (codeberg default 365d). If expired tokens trigger silent workflow failures, distribution stops without notice. Mitigation: set 90d-before-expiry reminder; monitor Forgejo run logs after every release.

**Verification probes (post-ship):**
1. `scripts/bump_version.py --dry-run` runs without error.
2. `pypi.org` shows reserved package `yadgar` owned by your account.
3. Forgejo secrets list includes `PYPI_API_TOKEN`.

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

---

## Agent Dispatch Budget (P11)

N/A — most steps are USER manual; small `scripts/bump_version.py` authoring goes inline or one small agent dispatch.

---

## Effort Estimate

| Step | Days | Owner |
|---:|---:|---|
| 0 — Pre-flight | 0.05 | agent |
| 1 — PyPI signup + 2FA + reserve `yadgar` + generate token + add to Forgejo secrets | 0.15 | user |
| 2 — Write `scripts/bump_version.py` skeleton + tests | 0.3 | agent (small dispatch) |
| 3 — CHANGELOG + MIGRATION_NOTES v5.46.1 + version bump 5.46.0 → 5.46.1 | 0.1 | agent |
| **Total** | **~0.2-0.3 cal-day** | mixed |

---

## Acceptance Criteria

- [ ] `PYPI_API_TOKEN` added + `yadgar` package reserved on pypi.org.
- [ ] `scripts/bump_version.py --version 5.46.0 --dry-run` runs without error.
- [ ] `scripts/bump_version.py` has unit tests covering version sub + dry-run mode.
- [ ] Pre-commit hooks pass on a clean pyproject.toml-only bump commit (sync_version + check_versions verify flake.nix consistency).
- [ ] CHANGELOG.md v5.46.1 entry.
- [ ] MIGRATION_NOTES.md v5.46.1 section documenting:
  - DP-A outcome (PyPI mandatory per PD-39)
  - PD-40: nix cross-repo PR retired; pre-commit hook handles flake.nix sync
  - Token rotation cadence (90d-before-expiry reminder)
- [ ] Post-ship verification probes 1-3 (per P7) all green.

---

## USER ACTION CHECKLIST (do these manually before agent dispatch)

Concrete step-by-step. Each item ~2-5 min. Total ~10-15 min.

### PyPI (mandatory — PD-39)

**1. Sign up for PyPI:**
- pypi.org → Register (top-right).
- Username: pick (e.g., `maxagahi-yadgar` or `openfantasy`).
- Email: your address.
- 2FA mandatory (TOTP via Aegis/Authy or hardware key) — set up immediately after first login.

**2. Reserve `yadgar` package name:**
- After verifying email + 2FA, build wheel locally + upload via `twine upload dist/yadgar-5.46.0-py3-none-any.whl` to claim the name.
- Recommend: just publish v5.46.0 wheel directly to PyPI on first push (already built locally).

**3. Generate `PYPI_API_TOKEN` (project-scoped):**
- pypi.org → Account Settings → API tokens → Add API token.
- Token name: `yadgar-codeberg-release`.
- Scope: `Project: yadgar` (DO NOT pick "Entire account" — least-privilege).
- Generate → copy token (one-time display, starts with `pypi-`).
- SAVE in 1Password at `op://Private/PyPI/yadgar-api-token` (canonical reference for the project-scoped token; retrievable in scripts via `op read "op://Private/PyPI/yadgar-api-token"`). NOTE: original account-scoped bootstrap token (op://Private/PyPI/api-token) was used to reserve the `yadgar` name via twine upload of v5.46.0 wheel on 2026-06-05; should be revoked at pypi.org once project-scoped token is in Forgejo secrets and verified.
- **Note OIDC alternative:** if codeberg/Forgejo gain OIDC trusted publisher support (currently uncertain — verify before dispatch), use that instead of long-lived API token. Track via https://pypi.org/help/#trusted-publishers.

**4. Add `PYPI_API_TOKEN` to Forgejo secrets:**
- codeberg.org/maxagahi/yadgar → Settings → Actions → Secrets.
- New Secret: name `PYPI_API_TOKEN`, value = step 3 token.

### Verify

**5. Sanity check:**
- Forgejo `yadgar` repo Settings → Secrets shows `PYPI_API_TOKEN` (value masked).

---

## What happens NEXT (after user checklist done)

Agent dispatch implements Step 2 + 3:
- `scripts/bump_version.py` (~80 LOC + tests)
- CHANGELOG.md / MIGRATION_NOTES.md updates
- Version bump 5.46.0 → 5.46.1
- Commit chain + merge to master + push to codeberg
- Post-ship verification per protocol

v5.47.0 follows (update mechanism). v5.46.2 slot is RETIRED per PD-40 — no dispatch needed.

---

## Defer rationale

Functional state is correct — distribution code shipped in v5.46.0 + manual install paths (forgejo release artifact, pipx-from-PyPI, nix flake) all work. v5.46.1 is the cross-repo infra layer. Sequential ship per user strict-order rule.

---

## Open questions for USER

1. **PyPI Trusted Publisher (OIDC):** before generating long-lived API token, want me to verify whether Forgejo (codeberg) supports OIDC trust to PyPI? Tracker: https://docs.codeberg.org / pypi.org/help/#trusted-publishers.
