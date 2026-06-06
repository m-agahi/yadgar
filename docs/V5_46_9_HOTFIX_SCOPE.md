# v5.46.9 Hotfix Scope — bake yadgar-ci image + log fold

**Source log:** `.local-review/ci-test-202606020913.log`
**Log run date:** 2026-06-06T05:53–06:42 UTC
**Prior catalog:** `docs/CI_ISSUES_2026_06_06.md` (v5.46.6 — 65 failed)
**Bake audit:** `docs/CI_SPEEDUP_AUDIT_2026_06_06.md` (PROCEED verdict)
**Current master:** 2481c7d

---

## Log analysis: `.local-review/ci-test-202606020913.log`

### Run stats

| Metric | This log | v5.46.6 log | Delta |
|--------|----------|-------------|-------|
| Failed | 16 | 65 | −49 |
| Passed | 3749 | 3691 | +58 |
| Skipped | 41 | 37 | +4 |
| Rerun | 32 | 136 | −104 |
| Errors | 0 | 3 | −3 |
| Warnings | 10 | 10 | 0 |
| Runtime | 1:07:58 | 1:06:09 | +109s |

**Trigger:** push/merge to master (inferred from runtime pattern + Python 3.14.5 + gw0–gw3 workers).
**Outcome:** FAILED (16 failures). v5.46.7 fixes resolved ~49 failures from the v5.46.6 baseline.
**Effective prior issues resolved by v5.46.7:** P1 (YADGAR_CI_BRANCH daemon env fallback), N1 (unique index), N2 (viz env knob), N3 (skip mark), P2 (hook files), P7 (os.walk mock), P8-partial, P9 (session end capture).

---

### Per-failure analysis

#### F1 — v5.46.7 regression: `YADGAR_CI_BRANCH` fallback overshoot (5 failures)

**Files:**
- `test_branch_auto_capture.py::TestMemoriseBranchCapture::test_memorize_branch_none_when_detect_returns_none`
- `test_branch_auto_capture.py::TestAnchorBranchCapture::test_anchor_branch_none_when_non_git`
- `test_v5_42_3_drainer_branch_enforcement.py::TestMemorizeHardReject::test_memorize_missing_branch_hard_rejects`
- `test_v5_42_3_drainer_branch_enforcement.py::TestMemorizeHardReject::test_memorize_hard_reject_no_queue_entry`
- `test_v5_42_3_drainer_branch_enforcement.py::TestMcpBoundaryValidators::test_memorize_no_branch_returns_error_dict`

**Symptom:**
- branch_auto_capture: tests mock `detect_branch` to return `None`, expect `branch is None` — getting `'master'`.
- drainer_branch_enforcement: tests call `memorize` with no branch, expect `missing_branch` error — getting `{'stored': True, 'queued': True}`.

**Root cause:** v5.46.7's P1 fix added a daemon-side `YADGAR_CI_BRANCH=master` env-var fallback (set in CI workflow) so that branch-less memorize calls in CI succeed. The fallback is unconditional — it fires even in tests that explicitly mock `detect_branch` to return `None` to assert the None/reject path. The env var is set for the entire test process, so the daemon always falls back to `'master'` regardless of the mock.

**Category:** v5.46.7 regression — code bug in fallback implementation.

**Fix:** Gate the `YADGAR_CI_BRANCH` fallback behind a more specific condition. Options:
- (a) Rename to `YADGAR_CI_FALLBACK_BRANCH` and only apply when the calling context is not inside a test that explicitly passes `branch=None`. This requires test fixtures to unset the env var.
- (b) Test-side: fixtures in `test_branch_auto_capture` and `test_v5_42_3_drainer_branch_enforcement` must `monkeypatch.delenv('YADGAR_CI_BRANCH', raising=False)` to isolate the none/reject tests from the CI fallback.
- (c) Daemon-side: apply the fallback only when `branch` is not supplied at all (absent from request), not when `branch=None` is explicitly supplied — i.e., treat absent vs. None differently in the memorize call signature.

Option (b) is lowest-blast-radius: no change to daemon logic, just add `monkeypatch.delenv` in the two test files.

**v5.46.9 fix scope:** YES — blocking regression, must fix before green.

---

#### F2 — P3 persisted: `/hooks/session-context` returns 404 (4 failures)

**Files:**
- `test_session_context_endpoint.py::test_session_context_returns_text_field`
- `test_v579_smart_sessionstart.py::TestSourceCompact::test_compact_no_restore_hint_without_checkpoint`
- `test_v579_smart_sessionstart.py::TestSourceCompact::test_compact_no_restore_hint_with_checkpoint`
- `test_v579_smart_sessionstart.py::TestSourceMissing::test_missing_source_not_compact`

**Symptom:** `GET /hooks/session-context` returns 404. Server logs confirm route not registered.

**Category:** Persisted from v5.46.6 P3 — code bug (route not registered).

**Note:** v5.46.7 did not address P3. The route `GET /hooks/session-context` is still unregistered in `yadgar/server/http.py`.

**v5.46.9 fix scope:** YES — carry forward from v5.46.7 backlog.

---

#### F3 — P4 persisted: `_audit_anchors` sentinel not written (2 failures)

**Files:**
- `test_consolidate_anchor_pass.py::TestAnchorPassEnabled::test_sentinel_written_after_consolidate`
- `test_consolidate_anchor_pass.py::TestAnchorPassEnabled::test_sentinel_is_latest_wins`

**Symptom:** `_audit_anchors` sentinel never written after `consolidate_now()` even with `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`.

**Category:** Persisted P4 — the prior catalog's "fix P1 first" cascade theory did not hold. v5.46.7 fixed P1 (fallback), but these tests still fail. The sentinel write path has an independent bug.

**Note:** Unlike F1, these tests do not assert the None/reject path, so they would not benefit from the F1 fix alone. The sentinel write in `consolidate_now()` is independently broken.

**v5.46.9 fix scope:** YES — investigate `consolidate_now()` sentinel write path independently of F1.

---

#### F4 — P6 persisted: health endpoint empty body (1 failure)

**File:** `test_transport.py::TestSessionManagement::test_session_count_reflected_in_health`

**Symptom:** `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`. Server log shows `GET /health` returning 404 (not empty body — the route is 404, not a malformed 200).

**Category:** Persisted P6 — server log shows `/health` returning 404, which means the health route is also unregistered in this test's server instance (or the test is hitting the wrong port). v5.46.7's readiness-retry fix did not fully solve this.

**Note:** The JSONDecodeError is a symptom of `.json()` on a 404 response (empty body in httpx for 404s), not of a malformed JSON response. Root cause is route registration, not readiness timing.

**v5.46.9 fix scope:** YES — re-examine whether `/health` is registered in the test server fixture.

---

#### F5 — P8 mutated: `make help` fails — `column` binary missing (2 failures)

**Files:**
- `test_v5_45_makefile_targets.py::TestV5_45MakefileTargets::test_v5_45_make_help_target_dry_run`
- `test_v5_46_2_makefile_install_runtime.py::TestV5_46_2MakefileInstallRuntimeTarget::test_make_help_lists_install_runtime`

**Symptom:**
- `grep -E '^## ' Makefile | sed 's/## //' | column -t -s ':'` — `column` not found, producing empty output (found 0/5 targets).
- `bash: line 1: column: command not found` / `make: *** [Makefile:43: help] Error 127`

**Category:** P8 mutation — prior P8 was `pre-setup` runtime check. That was fixed in v5.46.7. Now `column` binary is missing from the CI image. The `make help` target uses `column -t -s ':'` (from `bsdmainutils` or `util-linux` package) which is not installed in `yadgar-ci`.

**Fix:** Add `column` (via `bsdmainutils` or `util-linux`) to `Dockerfile.ci`. One-line `apt-get install` addition.

**Bake-fixable:** YES — fold into the Dockerfile.ci bake commit alongside SurrealDB/HF model additions.

**v5.46.9 fix scope:** YES — bake item.

---

#### F6 — New: `test_subagent_stop_hook::test_endpoint_stores_findings_with_provenance` (1 failure)

**File:** `test_subagent_stop_hook.py::TestSubagentStopEndpoint::test_endpoint_stores_findings_with_provenance`

**Symptom:** `assert 0 == 2` — endpoint stores 0 findings, test expects 2.

**Category:** New failure — not in v5.46.6 catalog. Post-v5.46.7 regression or newly added test against unimplemented behavior.

**Note:** `test_subagent_stop_hook` was in P1's cascade list (prior catalog) — prior failures were `missing_branch` rejections. After P1 fix, the branch is now resolved, but the endpoint logic itself fails to store findings (stored=0 instead of 2). This is likely a separate bug in the subagent stop endpoint's storage path.

**v5.46.9 fix scope:** YES — investigate `test_subagent_stop_hook.py:211` and the stop endpoint's findings storage path.

---

#### F7 — New: `/api/viz/config` returns 404 (1 failure)

**File:** `test_viz_config_endpoint.py::test_viz_config_defaults_search_and_colors`

**Symptom:** `GET /api/viz/config` returns 404. Prior catalog listed B16 (`/viz/config` returns 404) as resolved in v5.46.3–v5.46.6. This is either a path change (`/viz/config` → `/api/viz/config`) or a regression.

**Category:** New failure or prior partial fix — route registration drift.

**v5.46.9 fix scope:** YES — verify `/api/viz/config` route registration in `yadgar/server/http.py`.

---

### Failure summary table

| ID | Failure | Category | Count | v5.46.9 action |
|----|---------|----------|------:|----------------|
| F1 | `YADGAR_CI_BRANCH` fallback overshoot | v5.46.7 regression | 5 | Fix: monkeypatch.delenv in test fixtures |
| F2 | `/hooks/session-context` 404 | P3 persisted — code bug | 4 | Fix: register route in http.py |
| F3 | `_audit_anchors` sentinel not written | P4 persisted — code bug | 2 | Fix: investigate consolidate_now() |
| F4 | `/health` 404 → JSONDecodeError | P6 persisted — code bug | 1 | Fix: verify health route in test fixture |
| F5 | `make help` — `column` missing | P8 mutation — env/build | 2 | Fix: bake `column` into Dockerfile.ci |
| F6 | Stop endpoint stores 0 findings | New — code bug | 1 | Fix: subagent stop endpoint storage |
| F7 | `/api/viz/config` 404 | New — route regression | 1 | Fix: register /api/viz/config |
| **Total** | | | **16** | |

---

## Bake-into-image deliverables (from CI_SPEEDUP_AUDIT_2026_06_06.md)

Audit verdict: **PROCEED** on all four items. All are independent and can ship in one commit or incrementally.

### 1. SurrealDB binary bake (core image)

- **What:** `curl + sha256 + chmod` step in `Dockerfile.ci`, same as the current workflow step.
- **Version to bake:** `v3.0.5` (current pin — match `ci.yaml` cache key `surrealdb-v3.0.5`).
- **Binary path:** `/usr/local/bin/surreal`
- **Size delta:** +50 MB compressed → image ~3.09 GB compressed (from 3.04 GB).
- **Saves:** 5s warm (cache stat skip), 25s cold (curl+verify eliminated).
- **Maintenance:** bump both `Dockerfile.ci` and `ci.yaml` cache key on SurrealDB upgrades.

### 2. HuggingFace `all-MiniLM-L6-v2` weights bake (core image)

- **What:** `RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"` layer after pip install in `Dockerfile.ci`.
- **Cache path baked into image:** `/root/.cache/huggingface/` (sentence-transformers default).
- **Size delta:** +90 MB compressed → image ~3.18 GB compressed.
- **Saves:** 5s warm (HF cache step eliminated), 50s cold.
- **Maintenance:** model upgrades require image rebuild. Rare and intentional.

### 3. Playwright Chromium — `yadgar-ci-viz` split image

- **What:** New `Dockerfile.ci-viz` extending `yadgar-ci` with Playwright Chromium + 8 apt libraries.
- **Tag pattern:** `yadgar-ci-viz:VERSION` (e.g., `yadgar-ci-viz:5.46.9`).
- **viz-tests job:** switch `image:` reference from `yadgar-ci` to `yadgar-ci-viz`.
- **core image:** unchanged (Chromium stays out of the test-job image pull).
- **Size delta:** viz image ~3.58 GB compressed (+400 MB over core). Core unchanged.
- **Saves:** 75s (apt-get install currently runs every viz-tests run with no cache).
- **apt packages to bake:** `chromium chromium-driver` + the 8 libs currently installed inline.

### 4. npm cache step (`ci.yaml` change)

- **What:** `actions/cache` step on `viz-tests/package-lock.json` hash before `npm ci`.
- **Cache key pattern:** `npm-viz-{{ hashFiles('viz-tests/package-lock.json') }}`
- **Not baked into image** — baking `node_modules` into an image is fragile (path bindings).
- **Saves:** 20s warm, 20s cold (npm ci always re-runs without cache).

### 5. `column` binary bake (core image) — NEW, log-surfaced

- **What:** Add `bsdmainutils` (or `util-linux`) to `Dockerfile.ci` apt-get install layer.
- **Package:** `bsdmainutils` on Debian/Ubuntu provides `column`. On Ubuntu 22.04+ `util-linux` has it. Verify with `apt-cache show bsdmainutils` in the image.
- **Why:** `make help` target uses `column -t -s ':'` to format output. Missing in current image causes `make: *** [Makefile:43: help] Error 127` (P8 mutation F5).
- **Size delta:** <1 MB.
- **Saves:** unblocks F5 (2 test failures), no direct time savings but required for test correctness.
- **Bake alongside:** SurrealDB + HF model in the same Dockerfile.ci commit.

### Estimated savings summary

| Change | Time saved (warm) | Time saved (cold) |
|--------|-------------------|--------------------|
| Bake SurrealDB binary | 5s | 25s |
| Bake HF model weights | 5s | 50s |
| yadgar-ci-viz image (Playwright) | 75s | 75s |
| npm ci cache step | 20s | 20s |
| Bake `column` binary | ~0s | ~0s (correctness fix) |
| **Total** | **~105s / ~1.75 min** | **~170s / ~2.8 min** |

---

## Log-driven additions (new fixes beyond bake plan)

### L1 — v5.46.7 regression: branch fallback overshoot (F1, 5 tests)

**Root cause:** `YADGAR_CI_BRANCH=master` set for the entire test process; tests that mock `detect_branch` to return `None` and assert the None/reject path now get `'master'` from the env fallback instead.

**Fix approach (low blast radius):** In `test_branch_auto_capture.py` and `test_v5_42_3_drainer_branch_enforcement.py`, add `monkeypatch.delenv('YADGAR_CI_BRANCH', raising=False)` in each affected test's setup. This isolates those specific none/reject assertions from the CI fallback without touching daemon logic.

**Alternative (daemon-side, more robust):** Treat `branch=None` (explicitly passed) vs absent `branch` (not passed) differently. Apply fallback only on absent branch; never override an explicit `None`.

### L2 — `column` missing from CI image (F5, 2 tests)

Folded into bake deliverables (item 5 above). Dockerfile.ci one-liner.

### L3 — `/api/viz/config` route missing (F7, 1 test)

**Root cause:** B16 was marked resolved in v5.46.6 catalog but the route path may have changed from `/viz/config` to `/api/viz/config`. Verify in `yadgar/server/http.py`.

### L4 — Stop endpoint stores 0 findings (F6, 1 test)

**Root cause:** After P1/F1 unblocks branch resolution, the stop endpoint's findings storage is broken independently. Investigate `yadgar/hooks/subagent_stop.py` (or equivalent) — the endpoint receives the provenance payload but stores 0 items.

---

## Out-of-scope (defer)

| Item | Reason |
|------|--------|
| 2-stage test split (fast/slow) | `surreal_server` autouse fixture is structural blocker; brittle without conftest refactor. DEFER to v5.50+. |
| Bump `--maxprocesses` from 4 to 8 | SurrealDB contention risk; test locally first across 3 full runs. Not a v5.46.x item. |
| Stale skip + N3 tautology guard cleanup (~35 LOC) | Zero run-time impact; bundle into next substantive test-touching PR. |
| PD-43 (Ollama backend) | Separate roadmap item; no test failures linked. |
| Second runner instance | Not bottleneck; current load 33% CPU utilization. |

---

## v5.46.9 commit chain (proposed)

**Prerequisites:** branch off master, pre-commit hooks must pass, no `--no-verify`.

### Commit 1 — bake core image + `column` fix

```
feat(ci): bake SurrealDB v3.0.5 + HF all-MiniLM-L6-v2 + column into yadgar-ci image
```

Files: `Dockerfile.ci`

Changes:
1. Add `RUN curl .../surreal ... && sha256sum --check ... && chmod +x ...` (SurrealDB binary, same as ci.yaml step).
2. Add `RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"` (HF model weights).
3. Add `bsdmainutils` (or equivalent) to the `apt-get install` layer for `column`.
4. Update image label/version to `5.46.9`.

### Commit 2 — yadgar-ci-viz split image

```
feat(ci): add yadgar-ci-viz image — Playwright Chromium baked for viz-tests
```

Files: `Dockerfile.ci-viz` (new), `.forgejo/workflows/ci.yaml`

Changes:
1. Create `Dockerfile.ci-viz` extending `yadgar-ci:5.46.9` with Playwright chromium + 8 apt libs.
2. Update viz-tests job `image:` reference to `yadgar-ci-viz:5.46.9`.

### Commit 3 — npm cache step

```
feat(ci): add npm ci cache step for viz-tests/package-lock.json
```

Files: `.forgejo/workflows/ci.yaml`

Changes:
1. Add `actions/cache` step before `npm ci` in viz-tests job.

### Commit 4 — code fixes (F1, F2, F3, F4, F6, F7)

```
fix(v5.46.9): F1 branch fallback overshoot + F2/F4/F7 route registration + F3 sentinel + F6 stop endpoint
```

Files: test files for F1, `yadgar/server/http.py` for F2/F4/F7, `consolidate_now()` for F3, stop endpoint for F6.

Changes:
1. **F1:** Add `monkeypatch.delenv('YADGAR_CI_BRANCH', raising=False)` in `test_branch_auto_capture.py` and `test_v5_42_3_drainer_branch_enforcement.py` affected tests.
2. **F2:** Register `GET /hooks/session-context` route in `yadgar/server/http.py`.
3. **F3:** Investigate and fix `consolidate_now()` sentinel write path — `_audit_anchors` must be written when `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`.
4. **F4:** Verify `/health` route registration in the `test_transport` test server fixture.
5. **F6:** Fix subagent stop endpoint to correctly store findings with provenance.
6. **F7:** Register `GET /api/viz/config` route (verify path vs prior `/viz/config`).

### Commit 5 — version bump + changelog

```
chore: bump version 5.46.8 -> 5.46.9 + CHANGELOG + MIGRATION_NOTES
```

---

## V5_41_5_PROFILING_REPORT.md — hold, not committed

The file `docs/V5_41_5_PROFILING_REPORT.md` carries unstaged changes. The task description characterized these as "pre-commit hook auto-format (trailing 2-space markdown BR syntax)." The diff shows substantive numeric changes beyond formatting:

- `E2E handler` p50: `27.092 ms` → `0.059 ms` (3 orders of magnitude)
- Verdict: `FAIL — 5.4x over budget` → `PASS`
- Similarity gate: `27.735 ms` → `31.009 ms`

Pre-commit hooks add trailing whitespace; they do not rewrite performance measurement tables. These are measurement re-runs that flipped the conclusion. Committing alongside the audit doc without acknowledging the verdict flip would be misleading. **Hold for user decision:** stage and commit explicitly with a separate commit message that acknowledges the data update, or discard if the measurements are incorrect.

---

*Scope document produced 2026-06-06. Read-only investigation — no code changes.*
