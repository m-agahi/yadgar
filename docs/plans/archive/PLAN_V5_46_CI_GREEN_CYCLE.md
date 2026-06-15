# PLAN — v5.46.3 → v5.46.6 cycle: CI-green push

**Status:** drafted 2026-06-05 evening. Umbrella plan for the chained CI-failure remediation cycle. Replaces a hypothetical single PLAN_V5_46_3 per user direction (one umbrella + PD-42). Plan-first per I27.

**Parent context:** `docs/CI_ISSUES_2026_06_05.md` (@706e61d) — 27 issue classes (24 BLOCKING + 3 WARNING) catalogued from v5.46.2 release.yaml + ci.yaml runs (141 failed / 3548 passed / 25 errors / 332 rerun).

**Decision record:** `docs/DECISIONS.md` PD-42 records cycle strategy.

**Cycle goal:** 0 failed + 0 errors on Linux CI run. `darwin-skipif` tests stay skipped (per Q2 answer). All 4 sub-releases ship; ONLY the final release (v5.46.6) gets amd64 build + nix repo bump + tag + post-ship verification (per Q5 + Q7 answers).

**Effort estimate:** ~3 calendar days total across 4 releases.

---

## User-settled decisions (12 Qs answered 2026-06-05 evening)

| Q | Decision |
|---|---|
| 1 | Chained v5.46.3 → v5.46.4 → v5.46.5 → v5.46.6 (NOT single mega) |
| 2 | "100% clean" = 0 failed + 0 errors on Linux CI; darwin-skipif OK |
| 3 | Custom curated `yadgar-ci` image (NOT python:3.14-slim) |
| 4 | `docker.io/openfantasy/yadgar-ci` registry location |
| 5 | Tag ONLY the final release (v5.46.6); intermediate merges to master only |
| 6 | Impl agent decides per-test (fixture-fix vs preserve-negative-path) from code context |
| 7 | B2 fix mechanism: workflow env var `YADGAR_CI_BRANCH=master` |
| 8 | Final release deploy: amd64 build + nix bump + tag + `home-manager switch` + post-ship probes |
| 9 | If effort overruns: continue + report at completion |
| 10 | SBOM workflow fix folded into v5.46.3 |
| 11 | Light self-test coverage for each fix |
| 12 | PyPI upload failure mid-chain: STOP + report |

---

## Slot allocation (4 releases, 27 issues)

### v5.46.3 — CI infrastructure layer (env + image + workflow)

Foundational changes that unblock downstream test fixes. ~1 cal-day.

**Includes:**
- **NEW** `docker.io/openfantasy/yadgar-ci` image build (Dockerfile.ci at repo root)
  - Base: `python:3.14-slim`
  - System deps: `make`, `git`, `curl`, `ca-certificates`, podman client (for runtime detect tests if needed)
  - Pre-installed: `pytest`, `pytest-asyncio`, `anyio`, `cyclonedx-bom==7.3.0`, build tooling
  - Pinned: same Python version as pyproject `requires-python>=3.14`
  - Push: amd64 to `docker.io/openfantasy/yadgar-ci:5.46.3` (only this lane pushes to dockerhub — workflow needs to PULL; per Q4 answer + workflow-rule carve-out documented in PD-42)
- **B2** YADGAR_CI_BRANCH=master env var added to `.forgejo/workflows/{ci.yaml,release.yaml}`. Daemon picks via existing branch_hint API.
- **B6** `make` now in CI image (no skipif needed; 37 tests unblocked)
- **B7** `pytest-asyncio` + `anyio` in pyproject.toml test extra + `asyncio_mode = "auto"` in [tool.pytest.ini_options] (3 tests unblocked)
- **SBOM fix** release.yaml build-sbom job: install from local wheel (`pip install "dist/yadgar-${version}-py3-none-any.whl[sbom]"`) instead of PyPI roundtrip
- **Workflow image swap** all `image: python:3.14-slim` references → `image: docker.io/openfantasy/yadgar-ci:5.46.3`
- Light self-tests: `test_v5_46_3_ci_image_has_make.py`, `test_v5_46_3_ci_branch_env_var.py`, `test_v5_46_3_sbom_wheel_install.py`

**Acceptance:** local pytest with simulated CI conditions (no /usr/bin/make on PATH guard, YADGAR_CI_BRANCH=master) shows B2/B6/B7 tests pass. SBOM workflow step parsed for correctness.

---

### v5.46.4 — Test fixture refactor layer (schema + data-shape)

The biggest test fix scope. ~1 cal-day.

**Includes:**
- **B1** `directory_context` NOT NULL fixture audit: agent reads each failing test, decides per-test:
  - Positive-path tests (fixture should supply directory_context) → update fixture to inject sentinel like `"/test/sandbox"` or `"global"`
  - Negative-path tests (`test_anchor_surfacing.py::test_empty_string_directory_context_treated_as_global`, etc.) → preserve the test's intent; may need to adjust expected error message
  - Suspected affected fixture locations: `yadgar/tests/conftest.py`, `yadgar/tests/test_export_duckdb.py` (`seeded_storage`), inline fixtures in `test_wiki_*`
- **B8** Vector dim 4→384: `seeded_storage` fixture in `test_export_duckdb.py` + any shared `wiki_page` factories use `[0.0] * 384` instead of dim=4 placeholder
- **B10** `test_harness_hardening.py` lines 468, 494: replace `/home/max/git/yadgar` with `Path(__file__).resolve().parents[2]`
- **B11** `test_migration_014_wiki_embedding_backfill.py`: drop `is_last` assertion; replace with `assert "014_..." in [m["name"] for m in _MIGRATIONS]`
- **B13** DLQ backoff fixtures: add `branch_hint` + `directory_context` to test payloads (B1+B2 must land first; v5.46.3 ships B2 so v5.46.4 can address)
- **B9** Token budget: identify which payload field caused growth (compare `project_brief(mode="signals")` output snapshot to budget threshold). Either trim payload OR bump budget constants to new baseline. Investigate first; both options documented in plan.
- Light self-tests for fixture-injection patterns

**Agent authority per Q6:** classify each B1-affected test per-test; report classifications at the end of the release report.

**Acceptance:** local pytest with B2 env honored (v5.46.3 conditions) shows B1+B8+B10+B11+B13+B9 tests pass.

---

### v5.46.5 — Missing functions, endpoints, hook files

Smaller boilerplate fixes. ~0.5 cal-day.

**Includes:**
- **B3** Add `hook_db_lockdown_check` to `yadgar/scripts/hook_runner.py`. Check git log first to see if it was renamed; if so, restore the canonical name. If new function: minimal implementation that reads the lockdown sentinel file + returns boolean.
- **B4** Create missing hook files: `yadgar/hooks/session-start-context.py` + `yadgar/hooks/stop-memory-checkpoint.py`. Cross-reference `PLAN_V5_65_FRESH_MEMORY_ACCESS.md` for intended hook contracts. Minimal stub if no design exists; full impl if v5.65 plan documents it.
- **B5** Register `GET /hooks/session-context` route in `yadgar/server/http.py` (or wherever routes declared). Returns project-brief-style session context.
- **B16** Register `GET /viz/config` route returning viz configuration defaults.
- **B12** Restore `sleep_cycle` key to `consolidate_now()` response — investigate why it was removed; if intentional, update test; if accidental, restore.
- **B22** `os.walk` mock target update in `test_embed_service_v530.py`: investigate current dbsize implementation; update mock to target the actual function called (likely `pathlib.Path.iterdir` or similar after refactor).
- Light self-tests for new endpoints + functions

**Acceptance:** local pytest shows B3, B4, B5, B16, B12, B22 tests pass.

---

### v5.46.6 — Behavior fixes, final cleanup, ship

Last release in the chain. Gets full deploy treatment per Q5 + Q8. ~0.5 cal-day + deploy.

**Includes:**
- **B14** Circuit breaker probe-failure state: fix `yadgar/ml_client.py` `_CircuitBreaker.probe()` — ensure state flips back to OPEN on failed probe in backoff window. Likely a state-update bug in the probe handler that emits the log but doesn't update internal state flag.
- **B15** NLI default-OFF test alignment: per `PLAN_V5_57_58_D2_D3_AB_RESCOPE.md`, NLI is intentionally default-OFF until benchmark. Update `test_write_time_contradiction.py::test_default_on_fires_detector` to either: (a) rename + restructure to test explicit-ON case via env var setup, OR (b) split into two tests (default-OFF skip + explicit-ON enabled). Recommend (a) — simpler.
- **B17** Health endpoint empty body: investigate `/health` handler for exception swallowing OR startup race. After B2 lands (v5.46.3), B17 may auto-resolve since the startup failure path may be the cause. If still failing, add readiness wait in test fixture OR fix handler exception path.
- **B18, B19, B20, B21** verify auto-resolved by B1+B2 landings. Run pytest to confirm. Any residual failures get targeted fix.
- **W1-W3 WARNINGS** evaluate folding: W1 (httpx2 migration), W2 (asynccontextmanager lifespan), W3 (websockets.legacy upstream). W1 + W2 cheap to fix; W3 upstream issue, defer. Fold W1 + W2 if time allows.
- Final consolidation pass: full pytest run (no skips beyond darwin), document any residual failures + apply fixes
- Final amd64 image build: `podman build --arch amd64 -t docker.io/openfantasy/yadgar:5.46.6 -f Dockerfile .`
- Nix repo bump: `/home/max/git/nix` `modules/home/yadgar.nix` line 12 → `"5.46.6"`
- Tag push: `git tag v5.46.6 && git push origin v5.46.6` — triggers CI publish-pypi → PyPI gets v5.46.6
- USER applies via `home-manager switch`
- Post-ship probes per protocol (live daemon version + health + tests + verification table)

**Acceptance:** 0 failed + 0 errors on full pytest run. Live daemon v5.46.6. PyPI shows 5.46.6. All probes green.

---

## Cross-cycle conventions

- **Branch naming:** `feat/v5.46.X-<short-slug>` per release. Worktree-isolated dispatches per orchestrator rule.
- **Commit pattern:** TDD-disciplined (RED tests committed before impl). Conventional commit prefixes (`feat`, `fix`, `test`, `chore`).
- **Inter-release merge:** each release merges to master + pushes code only. NO tags, NO image builds, NO nix bumps until v5.46.6.
- **Version bump:** each release bumps pyproject.toml; pre-commit hook (v5.46.x) auto-syncs flake.nix + server.json + docker-compose.yml + uv.lock.
- **CI verification mid-cycle:** agent runs local pytest with simulated CI env (`YADGAR_CI_BRANCH=master`, isolated PATH without make if testing skipif logic) between releases. Remote CI confirmation happens only after final tag push.
- **Failure recovery:** per Q12, if PyPI upload fails mid-final-release, STOP + report. Other failure classes: STOP + report + offer 3 options + recommendation.

---

## Architecture Conformance (P1)

- **Test infrastructure** invariants: pytest discovery + fixture composition + parallelism (`-n 4 --dist loadgroup`) — preserves existing patterns.
- **CI image contract**: new `Dockerfile.ci` documented in `docs/architecture.md` § Release Lifecycle (proposed update separate from this plan per P1 rule — user-approved commit before dispatch).
- **Secret management**: PYPI_API_TOKEN unchanged; no new secrets added.
- **Workflow rule 2026-05-18**: amd64-only build constraint preserved. `yadgar-ci` image push to dockerhub is the documented exception (workflow rule carve-out: CI consumer images need registry presence). PD-42 records the exception.

---

## Touched Invariants (P2)

| Invariant | Verb | Notes |
|---|---|---|
| I26 (secret-gate) | preserves | No new secrets; only existing PYPI_API_TOKEN |
| Workflow rule "amd64 local only + no dockerhub push" | **EXCEPTION** | CI image (`yadgar-ci`) pushes to dockerhub by necessity — CI runner needs to pull it. Documented in PD-42. NOT a general policy change; specific to CI consumer images. |
| Workflow rule "every doc on master" | preserves | Plan + CI catalog + PD-42 + per-release fix docs all land on master via merge |
| Pre-commit flake.nix sync (PD-40) | preserves | Each release bump uses the existing hook chain |

---

## Config Knob Lifecycle (P3)

- `YADGAR_CI_BRANCH=master` — NEW workflow env var. Not a yaml/config knob, not registered in `_KNOB_FIELDS` (env-only). Documented in `MIGRATION_NOTES.md` v5.46.3 section + `.forgejo/workflows/*.yaml` inline comments. No yaml sync needed.

---

## Schema Constraint Lifecycle (P4)

N/A — no schema changes. The `directory_context` NOT NULL constraint (migration 018) is being conformed to in tests, not modified. Test fixtures align with already-shipped schema.

---

## MCP Contract Changes (P5)

N/A — no MCP tool changes. The `branch_hint` and `directory` params already exist (v5.42.3+ contracts); B1 and B2 fixes use them.

---

## Cross-Plan Coordination (P6)

| Plan | Relationship |
|---|---|
| `docs/PLAN_V5_46_2_RUNTIME_DETECTION_HOTFIX.md` | Parent (just shipped). v5.46.3+ extends the runtime detection + Makefile sync work into broader CI infrastructure. |
| `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN_RETIRED.md` | Archaeology (retired per PD-40). N/A. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | BLOCKED until this cycle completes per strict-version-order rule. |
| `docs/PLAN_V5_57_58_D2_D3_AB_RESCOPE.md` | B15 references this plan's NLI default-OFF reframing. |
| `docs/PLAN_V5_65_FRESH_MEMORY_ACCESS.md` | B4 references hook contracts from this plan; if no contracts documented, v5.46.5 ships stubs. |

No migration number conflicts (no schema work).

---

## Bug Class Precedent (P7)

**Precedent 1 — Speculative-infrastructure shipped without runtime verification.** B3, B4, B5, B16 all reference functions/files/endpoints written for tests but not implemented. Mitigation: this cycle catches these; future plans must include "CI verification probe" sections (P7 invariant).

**Precedent 2 — Schema migrations applied without test fixture sync.** B1 (directory_context NOT NULL) was migrated but fixtures weren't updated. Same class as v5.42.5 → v5.42.6 hotfix train. Mitigation: every NOT NULL migration MUST be accompanied by a fixture-audit checklist before merging. Codify as I29 candidate.

**Precedent 3 — CI runner environment != dev environment** for git context detection (B2). Anonymous workdir paths break `_detect_branch(os.getcwd())`. Mitigation: workflow env var pattern (YADGAR_CI_BRANCH); document as a CI-runner-pitfall in `docs/architecture.md` § Branch Detection.

**Verification probes (post-final-release):**
1. Push v5.46.6 to codeberg → ci.yaml runs → 0 failed + 0 errors
2. release.yaml on tag push → all jobs succeed (build-wheel + build-sbom + attach-to-release + publish-pypi)
3. `curl -s https://pypi.org/pypi/yadgar/json | jq .releases | keys` includes "5.46.6"
4. Live daemon at 5.46.6 + health + db + embed all green
5. Roadmap + PyPI + nix repo + image registry all consistent on 5.46.6

---

## Rollback Path (P9)

- Per release: `git revert <merge-sha>` reverts the failed release; previous release stays live.
- Pre-commit hook auto-syncs flake.nix on revert too (version field re-bumped via revert of bump commit).
- Image registry: previous version still tagged. Nix repo: previous version pin still on disk; revert nix commit if needed.
- PyPI: ONLY the final release publishes. Mid-cycle PyPI is unchanged (still at 5.46.2 until v5.46.6 ships).
- yadgar-ci image: v5.46.3 ships a new image; rollback = pull previous image tag (`docker.io/openfantasy/yadgar-ci:5.46.2` — would need to be built first; v5.46.3 is the first version of this image, so rollback = revert to python:3.14-slim in workflow files).

---

## Dependency Pinning (P10)

| Dep | Version | Location | Pinned in this cycle |
|---|---|---|---|
| `pytest-asyncio` | latest stable (resolve at Step 0) | pyproject.toml `[project.optional-dependencies].test` | YES (B7) |
| `anyio` | latest stable | same | YES (B7) |
| `cyclonedx-bom` | 7.3.0 (already pinned in v5.46.0) | pyproject.toml `[project.optional-dependencies].sbom` | unchanged |
| `httpx2` | latest stable (if W1 folded) | pyproject.toml test extras | OPTIONAL (W1 — defer recommended) |
| Python | 3.14 | yadgar-ci Dockerfile | YES (matches pyproject) |
| Base image | `python:3.14-slim` | Dockerfile.ci FROM line | YES |

---

## Agent Dispatch Budget (P11)

| Release | Wall-clock budget | Tokens (rough) | Phasing |
|---|---|---|---|
| v5.46.3 | ~60-90 min | ~100k | Single sonnet agent, worktree-isolated |
| v5.46.4 | ~60-90 min | ~150k (largest test fixture work) | Single sonnet agent, worktree-isolated |
| v5.46.5 | ~30-45 min | ~80k | Single sonnet agent, worktree-isolated |
| v5.46.6 | ~30-45 min impl + ~5 min build + ~5 min nix + ~2 min ship | ~80k | Single sonnet agent, worktree-isolated for code; main thread handles deploy |
| **Cycle total** | ~3-4 hours agent runtime + ~10 min deploy | ~410k tokens | Sequential — each agent's report informs the next |

---

## Acceptance Criteria (cycle-level)

- [ ] `docker.io/openfantasy/yadgar-ci:5.46.3` image exists locally + pushed to dockerhub (per Q4 exception)
- [ ] `.forgejo/workflows/{ci.yaml,release.yaml}` use `yadgar-ci` image + set `YADGAR_CI_BRANCH=master`
- [ ] SBOM workflow installs from local wheel (no PyPI roundtrip)
- [ ] All 24 BLOCKING issues from `docs/CI_ISSUES_2026_06_05.md` resolved with code+test
- [ ] W1 + W2 evaluated for fold (defer documented if skipped)
- [ ] Light self-test coverage for each fix class
- [ ] Final v5.46.6 ci.yaml + release.yaml runs: 0 failed + 0 errors
- [ ] v5.46.6 amd64 image built locally
- [ ] Nix repo bumped to 5.46.6 + pushed
- [ ] PyPI shows 5.46.6 (CI publish-pypi succeeded on tag push)
- [ ] Live daemon at 5.46.6 + all post-ship probes green
- [ ] Roadmap file-canonical mirror updated to v5.46.6
- [ ] PD-42 in DECISIONS.md records cycle strategy
- [ ] No `Co-Authored-By` trailers; no `--no-verify`; no hook bypass anywhere in chain

---

## Agent dispatch spec (for impl agents)

Each release dispatch follows this template:

```
Subagent: general-purpose
Model: sonnet
Isolation: worktree (fresh worktree off master)
Background: true

Spec: /home/max/git/yadgar/docs/PLAN_V5_46_CI_GREEN_CYCLE.md (this plan)
Slot: v5.46.X — see Slot allocation section above for issue list

Branch: feat/v5.46.X-<slug>
Authority: agent decides per-test fixture vs negative-path per Q6.
TDD: RED → GREEN per fix. Light self-tests per Q11.
Reporting: commit SHA range, fixed issues, deferred issues, tests passing, any deviations.

HARD RULES (per CLAUDE.md):
- NO terraform/tofu/tfp
- NO state-mutating commands beyond git
- NO image builds except yadgar-ci in v5.46.3 + yadgar in v5.46.6
- NO git push from agent (main thread merges + pushes)
- NO Co-Authored-By trailers
- NO --no-verify / --no-gpg-sign
- Pre-commit hooks must pass
- Caveman-terse responses; code/commits write normal

Main thread handles between dispatches:
- Verify report
- Merge feat → master
- Push master (no tag)
- Cleanup worktree + branches
- Dispatch next slot agent
```

Main thread workflow:

```
1. Plan committed @THIS_SHA + PD-42 added + PUSH master
2. Dispatch v5.46.3 agent → wait → verify → merge → push (NO TAG)
3. Dispatch v5.46.4 agent → wait → verify → merge → push (NO TAG)
4. Dispatch v5.46.5 agent → wait → verify → merge → push (NO TAG)
5. Dispatch v5.46.6 agent → wait → verify → merge → push + TAG v5.46.6 → PUSH TAG
6. amd64 build via build-agent (or inline if simpler)
7. Nix repo bump via build-agent
8. Inform user: "ready to apply" + wait for "applied" confirmation
9. User runs home-manager switch
10. Post-ship verification per protocol
11. Update roadmap file-canonical mirror
12. Push wiki update via wiki_update tool
```

---

## Defer rationale

This cycle finishes before v5.47.0 dispatch per strict-version-order rule + user directive "before we move to 5.47." The deploy gating ("build and push when you get to the last one") ensures user sees a single coherent v5.46.6 deploy rather than 4 incremental ones.
