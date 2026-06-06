# CI Speedup Feasibility Audit — 2026-06-06

## Current baseline

- **yadgar-ci image:** `docker.io/openfantasy/yadgar-ci:5.46.3` — 3.04 GB compressed / 5.96 GB uncompressed
- **Runner:** Forgejo runner:12 + dind (docker:dind), single instance, `capacity: 2` concurrent jobs; host has 24 vCPU / 62 GiB RAM, no resource limits on dind container (CpuQuota=0, Memory=0), so dind sees the full 24 cores / 65 GB
- **Effective pytest workers:** `pyproject.toml addopts` sets `-n 4 --dist loadgroup --maxprocesses=4 -m 'not integration'`; CI yaml adds `-n auto` (CLI arg appended after addopts → `-n auto` wins nominal count, but `--maxprocesses=4` from addopts caps the result). Actual workers: **4 per job**
- **Concurrent load:** `test` + `viz-tests` run in parallel = 4 + 4 = 8 xdist workers across 24 cores at peak. 16 cores idle during test phase
- **ci.yaml test job reference duration:** ~8 min (post anchor-480601 fixes: pip cache, `-n auto`, concurrency cancel-in-progress). Not independently re-verified — if warm-cache run is closer to 5 min, bake savings below are proportionally more impactful
- **Integration tests:** excluded from default run via `addopts -m 'not integration'`; viz-tests (`integration/viz/`) run separately in the viz-tests job, manually passed `-m integration`

---

## 1. Runner CPU + RAM

### Findings

The runner is not CPU-bound or RAM-bound at the current effective worker count of 4. Host has 24 cores / 62 GiB; dind is unconstrained. With two concurrent jobs (test + viz-tests), peak load is 8 workers on 24 cores — 33% utilization. RAM ceiling for 8 Python workers + one SurrealDB process is well under 8 GiB (sentence-transformers model ~400 MB resident, SurrealDB ~200 MB, each worker ~150 MB = ~5 GiB total). Massive headroom.

**-n 4 vs -n 8:** Bumping `--maxprocesses` from 4 to 8 would put 16 workers on 24 cores when both jobs run concurrently. That is fine from a CPU perspective. The risk is SurrealDB contention: the session-scoped fixture spawns one SurrealDB process shared across all 8 workers within each job. More workers → more concurrent queries → more lock contention under test. The `--reruns 2 --reruns-delay 2` guard exists precisely because some tests flake under worker-ordering races on the shared server. Raising to 8 workers will increase flake rate unless the SurrealDB fixture is tested at that concurrency level first.

**Second runner instance:** Would double throughput at the cost of a second `capacity: 2` slot, but `test` + `viz-tests` already run concurrently on the single runner; a second instance only helps if there are queued jobs waiting — not the current bottleneck.

### Recommendation

**Leave as-is for now.** The 4-worker cap is a SurrealDB contention guard, not a CPU limit. If `-n 8` is desired, test it locally with `pytest -n 8 --dist loadgroup` before changing `addopts`. If flake rate stays zero on three full runs: raise `--maxprocesses` to 8 in `pyproject.toml`. Do not raise further without evidence — 16 workers per job means 32 concurrent SurrealDB queries when both CI jobs are active. Risk: brittle.

---

## 2. Bake into yadgar-ci image

### What is currently installed per-run vs baked

| Item | Current behavior | Cache? | Per-run cost on cache miss |
|------|-----------------|--------|---------------------------|
| Python test+ml deps (torch, sentence-transformers, etc.) | Baked at image build via `pip install` in Dockerfile.ci | pip cache (actions/cache) | ~60-90s re-download if miss |
| HuggingFace `all-MiniLM-L6-v2` **model weights** | NOT baked — downloaded to `/root/.cache/huggingface` on first use | HF cache (actions/cache, key `hf-models-v2-all-MiniLM-L6-v2`) | ~30-60s on miss (~80 MB) |
| SurrealDB v3.0.5 binary | NOT baked — curl+SHA256 verify per run | surreal binary cache (actions/cache, key `surrealdb-v3.0.5`) | ~15-30s on miss (~50 MB) |
| Playwright Chromium + system libs (viz-tests only) | `apt-get install chromium chromium-driver + 8 libs` every run | None | ~60-90s always (no apt cache step) |
| npm + Node.js deps (viz-tests Layer 3) | `npm ci` every run in viz-tests/ | None | ~15-30s always |

### Per-item feasibility + size impact

**SurrealDB binary (~50 MB)**

- Feasibility: HIGH. Single binary, deterministic version pin (`v3.0.5`). Add to Dockerfile.ci as a `RUN curl+sha256+chmod` step, same as the workflow step. Eliminates the install step entirely; saves 15-30s on cache miss, ~5s on cache hit (stat check). Duplicate version logic: Dockerfile.ci must track SurrealDB version. When upgrading SurrealDB, bump both Dockerfile.ci and ci.yaml cache key — one extra step per upgrade. Low maintenance burden.
- Size increment: +50 MB compressed → image becomes ~3.09 GB compressed.

**HuggingFace `all-MiniLM-L6-v2` weights + tokenizer (~90 MB)**

- Feasibility: HIGH. Add a `RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"` layer to Dockerfile.ci after the pip install step. The sentence-transformers library is already in the image; this just forces a cache download at build time. Eliminates HF cache step entirely from ci.yaml. Saves 30-60s on cache miss. On cache hit the savings are minimal (~5s restore overhead), but it removes a fragile cache-keying concern (HF cache LRU eviction per anchor-480601 cause 5).
- Size increment: +90 MB → ~3.18 GB compressed.
- Risk: model version locked at image build. Upgrading model requires image rebuild. Acceptable — model upgrades are rare and intentional.

**Playwright Chromium + browser deps (~300-400 MB apt packages)**

- Feasibility: MODERATE. Baking into the main yadgar-ci image adds 300-400 MB and bloats every test job's image pull (test job doesn't need Chromium). Better approach: create a separate `yadgar-ci-viz:VERSION` image extending yadgar-ci with the Playwright/chromium layer. viz-tests job uses the viz variant; test job stays lean.
- If baked: saves 60-90s on viz-tests (apt-get install currently has no cache and runs every time). This is the largest per-run savings opportunity.
- Size impact on main image: zero (separate image). viz image: yadgar-ci (3.18 GB compressed post other bakes) + chromium/libs (~400 MB) = ~3.58 GB compressed.
- One-time complexity: need to build and push two images at CI infrastructure release. Dockerfile.ci-viz is a 5-line extension of Dockerfile.ci. Not brittle.

**npm + Node.js Layer 3 (viz-tests)**

- `npm ci` in viz-tests/ runs every time with no cache. Adding a `node_modules/` cache step via `actions/cache` on `viz-tests/package-lock.json` hash would save 15-30s. This is a ci.yaml change, not an image change. Trivial.
- Baking `node_modules` into the image is fragile (symlinked node_modules + container paths). Recommend cache step instead.

**xpra, pandoc, other one-shot installs**

- Scanned ci.yaml: no other one-shot apt installs in the `test` job. viz-tests installs exactly the chromium set listed above. No xpra, pandoc, or other large deps found.

### Estimated savings after all bakes

| Change | Time saved (warm) | Time saved (cold) |
|--------|-------------------|-------------------|
| Bake SurrealDB binary | 5s | 25s |
| Bake HF model weights | 5s | 50s |
| yadgar-ci-viz image (Playwright bake) | 75s | 75s |
| npm ci cache step | 20s | 20s |
| **Total** | **~105s / ~1.75 min** | **~170s / ~2.8 min** |

Against an ~8 min baseline: **saving ~1-2 min** (12-22%). Worthwhile but not transformative. Against a 5 min baseline (if warm-cache runs are already faster): saving ~1-2 min is proportionally more meaningful (~20-35%).

### Pull cost vs runtime savings

Larger image = longer pull on cache miss. yadgar-ci current pull: ~3 GB compressed. Post-bake: ~3.2 GB for core, ~3.6 GB for viz variant. Pull time delta on a warm dind cache (layer caching in dind-data volume): near zero for incremental adds. On a cold pull (e.g. first run after node restart): +15-20s for core, +30-40s for viz variant. Net still positive.

### Feasibility verdict

PROCEED. Baking SurrealDB + HF model weights into yadgar-ci is low-risk and low-maintenance. Splitting out a yadgar-ci-viz image for Playwright is moderate effort with the largest savings. npm cache step is trivial. All four changes are independent and can ship incrementally.

---

## 3. Drop unused tests

### Audit findings

**Duplicate test names across files**

Three names appear in multiple files: `test_helper_is_importable`, `test_live_codebase_all_pass`, `test_regen_fixture_if_requested`. These are **intentional per-file boilerplate**: `test_helper_is_importable` appears in `test_enrichment_dedupe.py` and `test_causal_traverse_helper.py` as isolated importability checks; `test_live_codebase_all_pass` appears in `test_check_trace_spans.py` and `test_check_metric_writers.py` as structural linting tests; `test_regen_fixture_if_requested` appears in characterization test files. Different files, different classes/modules, different test logic. pytest never confuses them (different nodeids). Not cruft.

**Skip-marked tests**

Active skips as of this audit:

1. `test_anchor_surfacing.py::TestAnchorSurfacing::test_empty_string_directory_context_treated_as_global` — skipped per `v5.46.4` deferral + N3 (v5.46.7) guard. The inline docstring says "v5.46.6: skip removed" but the skip is still present — **stale comment, not stale test**. The N3 guard in `test_v5_46_7_n3_skip_marker_applied.py` actively asserts the skip must exist. This is a tautology: test skipped because guard requires it to be skipped. The underlying feature (`''` normalised to `'global'`) is implemented per v5.46.6. The skip + guard pair should be resolved together: unskip the test, delete the guard, verify it passes. ~30 LOC cleanup, zero run-time impact (skipped tests don't run).

2. `test_consolidation.py::test_merge_duplicates_under_5s_at_500_memories_with_embeddings` — `skipif PYTEST_XDIST_WORKER`. Intentional: timing-sensitive perf guard that is unreliable under parallel CPU contention. Not cruft; condition is correct.

3. `test_scan_script.py::TestScanScriptLiveDB` — `skipif not YADGAR_TEST_LIVE_SCAN`. Intentional: live-DB test guarded behind env var. Not cruft.

4. Various `skipif` guards in v5.46.0 workflow stubs (PyYAML not installed), v5.46.0 nix flake eval (nix not in PATH), v5.46.0 yadgar-setup equivalence (setup script not yet created), v5.45.1 macOS launchd (non-Darwin skip). All are correct platform/capability guards.

**macOS v5.45.1 tests (PD-38 / deferred, NOT retired)**

`test_v5_45_1_{detect_macos,launchd_install,launchd_render,makefile_route,podman_machine_socket,uninstall_macos}.py` — 6 files. PD-38 (macOS launchd) is **deferred to when a macOS host is available**, not retired. Tests run on Linux: the `darwin`-specific runtime tests skip via `skipif sys.platform != 'darwin'`; the cross-platform render/script tests pass on Linux. These should stay — they protect the launchd render logic and macOS install paths.

**Retired features (PD-39 brew, PD-40 nix-pr, PD-44 v7 real-time synthesis)**

- PD-39 (Homebrew): formula + workflow stubs deleted. Tests referencing brew: `test_v5_46_0_forgejo_release_workflow_stubs.py` (tests release.yaml stubs — these were updated when brew stub was removed per PD-39), `test_postmortem_boost.py`, `test_retrieval_branch_polish.py`, `test_v5_45_1_launchd_render.py` (contain word "brew" in comments only). No dedicated brew test file exists. Nothing to drop.
- PD-40 (nix-pr cross-repo auto-open): the open-nix-pr step was deleted from release.yaml. `test_v5_46_1_publish_pypi_job.py` tests the current release workflow. No orphaned nix-pr tests found.
- PD-44 (v7 real-time synthesis retired): no `test_v7*` or `test_real_time_synthesis*` files exist. v7 was a roadmap item, never implemented, so no tests to delete.

**Tests against unused MCP tools or removed code paths**

No tests found referencing tools removed from the MCP surface. The `test_v5_46_0_forgejo_release_workflow_stubs.py` stubs match the current ci.yaml/release.yaml structure. No orphaned test-vs-implementation gaps found in this audit.

### Verdict on drops

**Minimal actionable cruft.** Only real cleanup opportunity:

| Item | LOC | Action |
|------|-----|--------|
| Stale comment in `test_anchor_surfacing.py` (`v5.46.6: skip removed`) | ~5 LOC | Fix comment or unskip + delete N3 guard |
| `test_v5_46_7_n3_skip_marker_applied.py` tautology guard | ~30 LOC | Delete once anchor_surfacing test is unskipped |

Total: ~35 LOC. Zero run-time savings (both are already skipped or guard-only). Not worth a dedicated cycle. Bundle into next substantive test-touching PR.

---

## 4. 2-stage test split feasibility

### The structural blocker: session-scoped autouse SurrealDB fixture

`yadgar/tests/conftest.py:133`:

```python
@pytest.fixture(scope="session", autouse=True)
def surreal_server(tmp_path_factory):
```

This fixture is `autouse=True` at session scope. It fires for **every pytest session** regardless of whether any specific test uses storage. The 86 test files identified as "pure unit" (no direct storage/embedding imports) still pay SurrealDB startup cost (~3-5s) because autouse fires before test collection for that session.

Splitting into Stage A "fast" (no DB) and Stage B "slow" (DB-backed) requires either:

1. **Making `surreal_server` not-autouse** and explicitly requesting it only from DB-using fixtures. Blast radius: every test fixture chain that implicitly depends on SurrealDB being started (either directly or via module-level globals) needs auditing. This is a multi-day refactor of 282 test files. High brittleness risk during transition.

2. **Separate pytest invocation with `--noconftest`** for fast tests, pointing at a parallel conftest that has no DB fixture. Requires maintaining two conftest trees. The `_isolate_yaml_config` and `_isolate_file_queue` autouse fixtures in the main conftest both use `monkeypatch` which the `conftest_v5_45.py` already had to work around via `--noconftest`. This pattern scales poorly.

3. **Collection-time marker filtering**: `pytest -m fast` + `pytest -m slow` in two sequential steps in the same job. Works only if tests are accurately marked. See below.

### Existing markers

Current registered markers in `pyproject.toml`:

```
xdist_group: group tests that share server module-level globals into one worker
integration: requires docker/podman + spins up live containers (slow, opt-in)
```

No `slow` or `fast` marker exists. The `integration` marker is already used to gate docker-dependent tests out of the default run.

### Split ratio estimate

Of 282 test files and ~3,811 test functions:

- **Pure unit candidates** (no storage/surreal/embedding/httpx imports): 86 files, estimated 700-900 test functions (~20-25%)
- **DB-backed tests**: ~120 files using SurrealDB directly or via fixtures
- **Embedding-dependent tests**: embedded in DB-backed category; model loads once per session
- **Integration / Playwright**: already gated to viz-tests job

Categorization is **not clean**. Many "pure" tests import from yadgar modules that themselves import storage classes (not at module level, but lazy-imported inside functions). Whether a test triggers SurrealDB startup depends on the autouse fixture, not on the test's own imports. A test file that imports nothing from storage can still exercise code paths that call `StorageEngine.__init__` if the autouse fixture runs first and leaves a global side effect.

### Fragility assessment

Adding `@pytest.mark.fast` / `@pytest.mark.slow` to 3,811 functions across 282 files and maintaining that categorization as the codebase evolves is high-maintenance. Any test that changes its DB usage (acquires a storage fixture, calls an endpoint that internally uses DB) must also have its marker updated — and that's an invisible coupling that doesn't get CI enforcement unless a meta-test counts and validates marker presence. The v5.46.7 tautology pattern (skip guard that exists only to ensure the skip exists) is a precedent for how maintenance friction accumulates.

The conftest autouse issue means Stage A isn't actually DB-free unless the fixture is refactored. A "fast" stage that still pays SurrealDB startup cost is not a 1-2 min stage.

### Would same-workflow work at all?

Yes, structurally: Forgejo Actions supports multiple steps in sequence; a `test` job could run `pytest -m fast` (Step A) then `pytest -m slow` (Step B). But:

- Both steps in the same job run sequentially, not in parallel. Total time = A + B, not max(A, B). No PR gate advantage unless A is fast enough to give early signal before B runs.
- Running them in two parallel jobs (fast-job + slow-job) requires the `surreal_server` fixture to work across separate sessions, which it does — but the per-job startup overhead (SurrealDB spawn + pip install) negates the fast-job time savings.

### Recommendation

**DEFER.** The `surreal_server` autouse fixture is a structural blocker. Doing this right requires a conftest refactor with significant blast radius. Without that refactor, a "fast" stage isn't actually fast (still pays DB startup). With the refactor, the split ratio is ~25% fast / 75% slow — marginal early-signal benefit for substantial engineering investment. User's stated preference: "if not leave as is dont want a brittle setup." This is brittle. Leave as-is.

If this is revisited in v5.50+: the path is (1) make `surreal_server` fixture opt-in (not autouse), (2) add a `fast` marker via an automated script that checks for storage fixture usage, (3) validate with a meta-test that every test file has a marker. That's a ~1-week effort done correctly.

---

## Verdict

| Area | Verdict | Confidence |
|------|---------|------------|
| Runner CPU + RAM | LEAVE AS-IS — 4 workers per job, 16 cores idle; SurrealDB contention (not CPU) limits safe worker count | High |
| Bake into yadgar-ci image | PROCEED — SurrealDB binary + HF weights in core image; yadgar-ci-viz for Playwright; npm cache step. ~1-2 min savings | High |
| Drop unused tests | DEFER / BUNDLE — ~35 LOC stale comment + tautology guard; no run-time savings; not worth standalone cycle | High |
| 2-stage test split | DEFER — `surreal_server` autouse is structural blocker; split without conftest refactor is not actually fast; brittle | High |

**Overall: PROCEED partial — bake improvements only.**

---

## If PROCEED — slot recommendation

**v5.46.x hotfix (next available slot, e.g. v5.46.9).**

Rationale: the bake changes are self-contained CI infrastructure changes (Dockerfile.ci additions + optional new Dockerfile.ci-viz + one cache step in ci.yaml). They are the same PD-42 carve-out class as v5.46.3. No impact on yadgar application code, no test changes, no version bumps needed beyond Dockerfile.ci label update. All three bake items (SurrealDB binary, HF model weights, Playwright image split) can ship in a single commit.

The npm cache step is a ci.yaml change only; can be bundled in the same commit or deferred to the following slot.

**NOT v5.47+ unless no hotfix slot is available.** The Playwright apt-get install runs every viz-tests CI run. That is the biggest per-run waste (60-90s with no cache). It is currently paid every PR.

---

*Audit conducted 2026-06-06. Read-only investigation — no code changes.*
