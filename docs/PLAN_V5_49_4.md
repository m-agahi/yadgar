# PLAN — v5.49.4: Followup Cleanup

**Status:** drafted 2026-06-09. **READY for impl.** No new features — pure debt reduction across 4 surfaces flagged during v5.49.0→.3 cycle.

**Branch:** `feat/v5.49.4-followups` off master.

**Effort estimate:** 3–4 hours total across 4 phases. Phases A+B+D mechanical/short; C investigation-heavy.

---

## 1. Background

Three v5.49.x rapid-iteration hotfixes (v5.49.1, .2, .3) closed shipped daemon CLI bugs surfaced by Rocky VM fresh-install dogfood. The release thread left four loose ends that don't fit a hotfix release but accumulated as visible debt:

1. **Stale README roadmap** — lists v5.26-v5.35 items as future when shipped 3+ weeks ago.
2. **Stale `docs/RELEASE_5_49_0.md`** — runbook written for v5.49.0 PyPI publish, not committed, superseded by the v5.49.1/.2/.3 release cycle. Either delete or generalise.
3. **Container-side sd_notify gap** — Phase 6 of v5.49 wired sd_notify in the HOST CLI (`yadgar/daemon.py:294`). The container's FastAPI server lifespan does NOT emit `READY=1` / `STOPPING=1`. Current `Type=notify` only works because podman `--sdnotify=healthy` surrogate-emits READY=1 once the container HEALTHCHECK passes. STOPPING=1 from the daemon process never reaches systemd.
4. **28 pre-existing test failures** — surfaced during v5.49.0 full-suite verification. Cluster: `test_memory_behavior.py` (11), `test_frontier_integration.py` (7), `test_consolidate_anchor_pass.py` (2), `test_idle_eviction_flip.py` (2), `test_backup.py` (2), plus singletons in `test_integration.py`, `test_action_log_poison_pill.py`, `test_exception_telemetry.py`, `test_mcp_trace_middleware.py`. Confirmed pre-existing on `master` BEFORE v5.49.0 (bisected against `test_mcp_trace_middleware`). Likely env-related (fixture state, stale SurrealDB instances, race conditions). Need root-cause bisection per cluster.

---

## 2. Resolved decisions

| DP | Decision | Rationale |
|---|---|---|
| **A — README roadmap shape** | **Trim shipped items + add an "Already shipped" footnote pointing at CHANGELOG.md** | Keep roadmap forward-looking; don't duplicate CHANGELOG content. Cheap to maintain. |
| **B — Stale RELEASE_5_49_0 doc** | **Generalise to `docs/RELEASE.md`** | Concrete release runbook for any version (twine upload + nix bump + container build + VM verify). Replaces ad-hoc `RELEASE_5_X_Y.md` files. |
| **C — Container-side sd_notify** | **Wire `sd_notify.ready()` + `sd_notify.stopping()` into FastAPI lifespan** | Belt-and-suspenders alongside `--sdnotify=healthy`. STOPPING=1 from server gives systemd accurate shutdown signal even if podman doesn't propagate. Backwards-compatible: silent no-op when `NOTIFY_SOCKET` unset. |
| **D — Test failure bisection scope** | **Per-cluster: identify root cause, fix or document as known-flake-by-env** | Not all 28 are real regressions. Some are fixture-isolation issues that only fail under serial run (`-p no:xdist`). Document each cluster's verdict. |
| **E — Version slot** | **5.49.4** | Continuation of v5.49.x hotfix string; no new MCP surfaces. |

---

## 3. Scope by phase

### Phase A — README roadmap refresh + RELEASE.md (mechanical, ~30min)

#### A1. README.md `## Roadmap` section

Read current README.md roadmap section. Current state lists v5.26-v5.35 items as future. Action: remove items already in CHANGELOG.md; keep only v5.50+ and v6/v7 entries. Add a one-line link: "v5.0.0-v5.49.x shipped — see [CHANGELOG.md](CHANGELOG.md) for full release history."

#### A2. `docs/RELEASE_5_49_0.md` → `docs/RELEASE.md`

Delete `docs/RELEASE_5_49_0.md`. Create `docs/RELEASE.md` with generic version placeholders. Sections:
- Prerequisites (PyPI token, Docker Hub creds skipped, nix repo write access)
- Bump version (`pyproject.toml`, `server.json` — `check-versions` pre-commit hook validates)
- Build container amd64 locally (`podman build --arch amd64 -t docker.io/openfantasy/yadgar:<ver> -f Dockerfile .`)
- Build PyPI artifacts (`python -m build --sdist --wheel --outdir /tmp/yadgar-dist`)
- twine check + upload
- git tag + push
- Bump nix `yadger_core_version` + push
- Verify: `yadgar update --check` reports new version
- Optional Rocky VM smoke (`podman save` + scp + load + `yadgar setup` + `daemon start`)

### Phase B — Container-side sd_notify lifespan wire-up (~1hr)

#### B1. Extend `yadgar/server/lifecycle.py` startup yield

Read `yadgar/server/lifecycle.py` around the FastAPI lifespan. Find the position after `init_engines()` completes + before `yield` (the "everything ready" point). Add `yadgar.sd_notify.ready()`. Same module already imports paths/settings — add `from yadgar import sd_notify`.

#### B2. Extend shutdown after `drain_in_flight_requests()` snapshot

`yadgar/server/lifecycle.py:382-396` (`shutdown()`) — Phase 6 already calls `sd_notify.stopping()` at line 400 (per Phase 6 audit). VERIFY this still fires. If yes: no change needed. If gone (regression): re-add.

#### B3. Test container-side notify path

Add `yadgar/tests/test_container_sd_notify.py`:

1. `test_lifespan_startup_emits_ready` — mock `sd_notify.ready` via monkeypatch; drive lifespan startup yield; assert `ready()` was called exactly once.
2. `test_lifespan_shutdown_emits_stopping` — same for `stopping()` on shutdown.
3. `test_lifespan_no_socket_silent_noop` — unset `NOTIFY_SOCKET`; lifespan startup completes without exception.

### Phase C — 28 pre-existing test failure bisection (~2hr investigation)

NOT a single-commit fix. Investigation + per-cluster verdict. Output: `docs/PRE_EXISTING_TEST_FAILURES_V5_49_4.md` documenting:

- Cluster name
- Failed tests
- Root cause (env / fixture / race / real bug)
- Verdict (fix in this release / document as known / quarantine / refactor needed)
- If fix: commit reference. If quarantine: pytest marker (`@pytest.mark.xfail(reason=...)`).

Priority order (start with highest-signal cluster):

1. `test_memory_behavior.py` (11) — content integrity + heat decay. Likely fixture-isolation: SurrealDB state from earlier tests bleeds in.
2. `test_frontier_integration.py` (7) — CRDT recall / write gate. Integration-flavour even though not `@pytest.mark.integration`. May need re-marker.
3. `test_consolidate_anchor_pass.py` (2) — sentinel write paths. Possibly XDG path regression.
4. `test_idle_eviction_flip.py` (2) — span emission. OpenTelemetry mock interference.
5. `test_backup.py` (2) — TestPruneSnapshots. Potential name-collision with v5.49 `upgrade-snapshots/`.
6. Singletons (5) — judge case-by-case.

**Approach per cluster:**
- Run cluster in isolation (`pytest yadgar/tests/test_X.py --tb=short`).
- If passes in isolation → fixture-bleed. Add `@pytest.fixture(autouse=True)` reset hook or refactor.
- If fails in isolation → real defect. Fix or mark.

**Quarantine criteria:** if fix > 30min AND cluster is not a v5.49 regression, mark `xfail` with TODO link to v5.50+ refactor ticket. Do not block the followup release.

### Phase D — Version bump + CHANGELOG (mechanical, ~15min)

Bump `pyproject.toml:version` 5.49.3 → 5.49.4. Backend stays 5.4.0. `server.json:version` same. Append v5.49.4 entry to `CHANGELOG.md` listing phases A/B/C/D.

---

## 4. Non-goals

- v5.50 viz overhaul, v6 LLM curator, v7 synthesis (separate release cycles).
- 89 untested modules coverage (next sequential release).
- Grandfathered complexity refactor (next sequential release).
- Adding new MCP tools / CLI subcommands.

---

## 5. Test plan

- Phase A: no new tests (docs only).
- Phase B: 3 new tests (`test_container_sd_notify.py`).
- Phase C: per-cluster — either real fix tests OR quarantine markers. Sum unknown until investigation.
- Phase D: pre-commit `check-versions` validates sync.

**Acceptance:**
- All 3 new B tests green.
- Test suite count delta documented (added X xfails, fixed Y, total green count was Z before/after).
- Pre-commit clean. NO `--no-verify`.

---

## 6. Phases (agent dispatch)

A. **README roadmap refresh + generic RELEASE.md.** `docs(release): trim shipped roadmap entries + add generic RELEASE runbook`
B. **Container-side sd_notify lifespan wire-up + 3 tests.** `feat(sd-notify): emit READY=1/STOPPING=1 from FastAPI lifespan`
C. **Pre-existing test failure bisection.** Per-cluster verdict file + targeted fixes + xfail markers where investigation cost > 30min. `fix(tests): bisect + quarantine pre-existing failures (v5.49.4 followup)`
D. **Version bump + CHANGELOG.** `chore: bump version 5.49.3 → 5.49.4 + CHANGELOG`

---

## 7. Risks

- **Phase C overruns budget** — quarantine policy bounds. If a cluster bleeds past 30min investigation, xfail + ticket and move on.
- **Phase B sd_notify wire-up regresses uvicorn lifespan** — covered by existing `test_graceful_shutdown.py` + new tests.
- **README + RELEASE.md drift from reality again next release** — mitigate by adding `check-readme-roadmap-freshness` consideration to v5.50+ scope (out of v5.49.4 scope).

---

## 8. References

- `docs/PLAN_V5_49_0.md` (v5.49.0 bundled plan + Phase 6 sd_notify wiring)
- `yadgar/sd_notify.py` (Phase 5 helper)
- `yadgar/server/lifecycle.py:382-400` (current shutdown + STOPPING=1 emit)
- `yadgar/daemon.py:294` (host CLI READY=1 emit)
- CHANGELOG.md v5.49.0-3 entries (release history)
- `.complexity-baseline.json` (NOT touched in this release; v5.49.5 candidate)
