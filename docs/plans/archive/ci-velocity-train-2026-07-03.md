> ARCHIVED 2026-07-14 — #84 test-speed SHIPPED v5.104 (~2x CI shards, ADR-0036); #83 backend-bump/version gate MERGED (v5.60.0 — scripts/check_backend_bump.py --ci in ci-pr.yaml + ci-release.yaml). #79 load-test contract remains as the standalone docs/plans/perf-loadtest-contract-2026-06-30.md (being audited to implementation-ready). All velocity legs resolved → archived.

# CI / Velocity Train — unified, audited plan (2026-07-03)

**Status:** test-speed leg (#84) SHIPPED v5.104 (~2x CI shards, PR #156, ADR-0036). Remaining: #83 backend-bump CI gate, #79 load-test contract. **Author:** agent (bot).
**Scope:** ONE plan to greatly speed up dev cadence across three tracked items:
#84 test-suite speedup (the velocity blocker), #83 version-gate + conditional
image builds, #79 load-test contract. Integrates two existing feeder plans
(`docs/plans/archive/test-suite-speedup-2026-07-01.md`,
`docs/plans/perf-loadtest-contract-2026-06-30.md`) — this doc **sequences +
adds #83**, it does not re-litigate their decided contents.

> **Provenance discipline.** Numbers tagged `[measured]` (from ADR-0027's
> quiet-box profile), `[CI-config]`, `[code-read]`, or `[estimate]`. Estimates
> are not measurements. Self-versioning repo: each phase carries a version bump
> (`__version__` / `backend_version`) — the "version impact" column is real.

---

## 0. The headline — what actually costs time (audited, not guessed)

Every PR this session ate ~20 min of pre-push `make e2e`; CI is ~1.5 h. The
instinct is "parallelize e2e (`-n auto`)." **That instinct is wrong and is the
same hypothesis ADR-0027 already debunked.**

- **`-n` tuning is FLAT** on the surreal-heavy suite: `437±3s` across
  `-n4/8/12/auto` `[measured, ADR-0027]`. Adding workers does nothing because
  wall-clock is pinned by `N_tests × per-test-init`, not cores
  (`wall ÷ Σdurations = 1.62×` → overhead-bound, not compute-bound).
- **The real floor is a per-test `init_engines()` SurrealDB schema re-init**
  (~5–10s/test: connect + `_init_schema` — 25 tables + indexes + analyzers +
  migrations, `yadgar/storage/migrations.py:1171`), invoked from a
  **function-scoped `@pytest.fixture(autouse=True)`** in the test files
  `[code-read]`.
- **e2e is hit by the SAME floor.** e2e uses a function-scoped autouse
  `_engines`/`e2e_engines` fixture (`yadgar/tests/e2e/conftest.py:147`) that
  calls `init_engines()` **per test** — it is NOT the subprocess-daemon pattern
  (that pattern lives only in `test_offload_e2e.py` and the future #79 load
  harness). So `make e2e` = 116 tests × ~5s init ≈ **~580s of fixture overhead
  alone** `[estimate, grounded]`, serial (`-n0`).

**Therefore the single highest-ROI change attacks BOTH bottlenecks at once:**
module-scope the `init_engines()` fixture. It collapses per-test init to
once-per-file across the unit suite (1.5 h CI) **and** the e2e suite (~20 min
pre-push) simultaneously. This is P0 of the #84 feeder plan, promoted here to
the **train's recommended P1** on the strength of the 1.62×-overhead proof.

> e2e `-n auto` is deliberately NOT the headline. It is unmeasured for e2e, and
> the reason unit `-n` is flat (per-worker surreal saturation + the init floor)
> almost certainly reasserts the moment e2e runs >1 worker. Frame it as a
> *post-module-scope* experiment (§P1-b), pilot-gated — never the lead.

---

## 1. Audited current state (file:line)

### 1.1 CI — `.forgejo/workflows/ci-pr.yaml`
| Job | Runner / container | What it runs | Prov |
|---|---|---|---|
| `test` (5-way matrix `group:[1..5]`) | `openfantasy/yadgar-ci:5.73.0` | `pytest yadgar/tests/ -q --tb=short -n auto --dist loadgroup --reruns 2 --reruns-delay 2 -m 'not integration and not e2e' --splits 5 --group N` (:85-91) | `[CI-config]` |
| `invariant-checks` | `yadgar-ci:5.73.0` | 5 lints: `check_metric_writers.py` (I23), `check_trace_spans.py` (I24), `test_config_three_way_sync.py` (I25), `check_dead_capability.py` (I29), `check_capability_coverage.py` (I32) (:112-125) | `[CI-config]` |
| `test-gate` | — | aggregator; `if: always()`; asserts all 5 chunks green (:130-141) | `[CI-config]` |
| `viz-tests` | `yadgar-ci-viz:5.46.9` | graph API contract + `integration/viz/` + `npx vitest run` (:143-207) | `[CI-config]` |
| `verify-version-bump` | `python:3.14-slim` | fails IF `v$PYPROJECT_VER == latest_tag` AND any `yadgar/` file changed (:208-227) | `[CI-config]` |

- **Trigger:** PR→master; `paths-ignore: [benchmarks/**, docs/**, .forgejo/**]`
  (:10-13). This doc is docs-only → CI skipped for THIS PR.
- **All 5 chunks + other jobs run on the same host class** → the CI-vs-local
  collision source (ADR-0027 P2).
- **e2e is NOT in CI** (`-m 'not e2e'`) — CI's embedded surreal can't run them
  reliably. e2e lives ONLY in the pre-push hook.

### 1.2 Makefile targets
- `test-ci` (:281-284): `-m "not integration and not e2e" -p no:randomly -n auto`,
  flock-locked (local).
- `e2e` (:291-294): `OTEL_SDK_DISABLED=true ... pytest yadgar/tests/e2e/ -m e2e
  -p no:randomly -n0 --reruns 2 --reruns-delay 2` → **116 tests, single-process**.
- `TEST_LOCK` flock (:269-270): serializes LOCAL `make test*`/`make e2e`;
  **CI bypasses it** (invokes `--splits` directly) → cross-run collision.

### 1.3 Fixtures — `yadgar/tests/conftest.py`
- `surreal_server` (session-scoped, :278-312): ONE SurrealDB HTTP server per
  **session** on a fixed port, shared across all xdist workers.
- `_engines` (function-scoped autouse): **74 copy-pasted per-file definitions**
  (NOT one shared conftest fixture), each `server.init_engines(...)` then
  `server.shutdown()` in teardown. + 3 non-autouse variants.
- `_isolate_surrealdb` (session, :450-488): patches `_init_schema` to set header
  `surreal-db: t{md5(db_path)[:12]}` → per-test namespace isolation on the shared
  server.
- `_wipe_surrealdb_data` (function autouse, :672-754): HTTP-DELETEs the ~16
  `_WIPE_TABLES`; **wipes only namespaces CREATED during the test** (pre-test
  snapshot guard, :698) + always "main". **Wipes DATA, not schema/singletons.**
  → **Consequence for P1 (§P1 risk):** under module-scope the file's namespace
  is created by test 1, so for tests 2..N it is "pre-existing" and the snapshot
  guard **skips** it → data leaks between tests in the file. The wipe was
  *designed* to protect module/session namespaces; module-scoping the engine
  walks into that guard. P1 must therefore ALSO adapt the wipe (see §P1).
- OTLP drain (ADR-0027 §5): `setup_tracing()` wiring prod `otlp_endpoint` before
  config isolation → ~3–9s/test DNS-retry drain on affected teardowns. `make e2e`
  already sets `OTEL_SDK_DISABLED=true`; the unit path does not universally.

### 1.4 Version-gate machinery (the #83 surface)
- Constants in **4 pins**: `pyproject.toml:7` (`version`), `server.json:10-11`
  (`version` + `backend_version`), `yadgar/__init__.py:21` (`BACKEND_VERSION`),
  plus `flake.nix` / `docker-compose.yml` env defaults.
- `scripts/check_versions.py`: fails if the 4 pins disagree within a role
  (core vs backend, independently tracked since v4.7.0). Consistency, not "bump
  required."
- **`scripts/check_backend_bump.py` ALREADY EXISTS** as a **pre-commit hook**:
  if any backend build input is staged (`Dockerfile.backend`,
  `entrypoint-backend.sh`, any `backend/` dir incl. `yadgar/backend/`), then
  `server.json` must be staged **with a bumped `backend_version`** (:39-96).
- **THE #144 GAP:** `check_backend_bump.py` runs **locally only** — it is NOT a
  CI job. `verify-version-bump` in CI checks *only* `pyproject.toml` vs latest
  tag, never `backend_version`. A dev who skips/bypasses the local hook (or whose
  hook didn't fire) lands a `yadgar/backend/` change with a stale
  `backend_version` → the image is not rebuilt on release → the code ships dead.
  PR #144 (`ml_client.py` ONNX path) hit exactly this ("GAP 1 — the #83 trap").
- **Conditional image build ALREADY EXISTS** in `ci-release.yaml:77-149`
  (post-merge): per-component `core_changed`/`backend_changed` via git-diff globs
  + `server.json` version-field diff, plus a Docker-Hub registry-existence check
  (rebuild-on-absent, closing the v5.56/5.57 "phantom image gap"). So #83's build
  half is largely DONE at release time; the missing guard is the **per-PR** bump
  gate.

### 1.5 #79 load-test — already designed
The feeder plan (`perf-loadtest-contract-2026-06-30.md`) is decided:
Python-in-repo (not k6/Allure), pinned quiesced-copytree prod snapshot, the
`_Daemon`/`_call_tool` subprocess-over-HTTP driver
(`test_offload_e2e.py:77-175`), N×5 median runs, record-only-first then gate,
`make perf` + `.forgejo/workflows/perf.yaml`. **The "parallel-dispatch harness
that beats MCP serialization" the task asks for IS this design already**:
Phase B fires N `threading.Thread`s doing **direct HTTP** to the daemon
(`test_offload_e2e.py:208-234`) — precisely because batched MCP tool calls
serialize. Reference it; do not reinvent it.

---

## 2. The phased train (P1 = highest ROI)

| P | Scope | Expected saving | Risk | Effort | Version impact |
|---|---|---|---|---|---|
| **P1** | Module-scope `init_engines()` (unit **and** e2e) | **Largest single lever** — collapses per-test ~5–10s init to once-per-file across both suites; e2e ~580s→~75s fixture floor `[estimate]` | **Medium** — wipe snapshot-guard (:698) skips the shared namespace → cross-test data leak unless wipe adapted; `shutdown()`-mid-file/singleton tests break; `tmp_path`→`tmp_path_factory` ScopeMismatch | Med-high (74 sites, per-file body rewrite — NOT a scope-flip) — **pilot 5 slowest first** | patch bump per batch |
| **P1-a** | OTLP drain guard (session-wide `OTEL_SDK_DISABLED` before any tracing) | ~3–9s/test on affected teardowns; may rival P1 if broad | Low | Low | patch |
| **P1-b** | e2e `-n auto` (POST-P1, pilot) | unmeasured; possibly several-fold IF surreal port doesn't saturate | Med — shared session port; `-n0` was precautionary safety-net | Low (flag flip + measure) | none |
| **P2** | #83 CI version-gate: promote `check_backend_bump.py` into a CI job | Prevents the #144 class permanently (no wall-clock, correctness) | Low | Low | minor |
| **P2-b** | #83 per-PR conditional-build preview / assert release-gate sees the bump | avoids dead-code ships | Low | Low | minor |
| **P3** | CI-vs-local mutual exclusion + tighten pre-push e2e gate | reliability (kills the 1847-err rerun class) + fewer full-e2e triggers | Low-med | Med (infra) | none |
| **P4** | #79 load-test harness (record-only → gate) | latency-regression guard on concurrent recall | Med (noise) | ~3–4 days | `make perf`, new workflow |

### P1 — Module-scope `init_engines()` (RECOMMENDED — best speedup:risk)
- **What:** convert the function-scoped autouse `_engines` fixture to
  **module-scoped** so `init_engines()` runs once per test *file*, not per test.
  Rely on the existing autouse function-scoped `_wipe_surrealdb_data` for
  per-test DATA isolation (schema/namespace survive the wipe by design).
- **NOT a pure `scope=` flip — it is a per-file body rewrite.** Two coupled
  changes are forced by the audit, not optional:
  1. **ScopeMismatch:** the `_engines` fixtures request `tmp_path` (function-
     scoped). A module-scoped fixture **cannot** request `tmp_path` — pytest
     raises `ScopeMismatch`. Each must be rewired to **`tmp_path_factory`**
     (session-scoped) to derive ONE `db_path` per module.
  2. **Wipe adaptation (the isolation crux):** one `db_path`/file ⇒ one namespace
     `t{md5(db_path)}`/file. Because `_wipe_surrealdb_data`'s snapshot guard
     (conftest:698) skips namespaces NOT created during the current test, the
     shared module namespace is skipped for tests 2..N → **data leaks between
     tests in the file.** P1 MUST make the module namespace wipeable — either
     add the module namespace to the per-test wipe set, or keep a function-scoped
     inner wipe step that explicitly clears the module namespace's data tables
     each test. Verify conftest:698 semantics during the pilot.
- **Two implementation shapes** (pick per-file during pilot):
  1. **Centralize + module-scope** — lift one shared module-scoped `_engines`
     (`tmp_path_factory`-based) into root `conftest.py`, delete the 74 per-file
     copies. Cleanest, but the per-file `db_path`/name differences + wipe
     adaptation must be reconciled centrally.
  2. **In-place rewrite** — change each file's fixture to `scope="module"` +
     `tmp_path_factory`, move `server.shutdown()` to module teardown, and ensure
     the per-test wipe covers the module namespace. Lower blast radius per file;
     keeps 74 sites.
- **Expected speedup:** attacks the dominant floor directly. A 20-test file:
  ~20×7s=140s → ~7s + 20×(wipe only). Across the 74 affected files this is "a
  large fraction of the 1.62× overhead" `[estimate, grounded]`. **Measure
  per-file** post-conversion.
- **Files:** the 74 `_engines`-bearing test files + `yadgar/tests/conftest.py` +
  `yadgar/tests/e2e/conftest.py`. Pilot the 5 slowest (durations from the
  ADR-0027 profile) first.
- **Risk (the real one — data leak via the wipe guard):** module-scoped engines
  share one namespace per file, and `_wipe_surrealdb_data`'s snapshot guard
  (conftest:698) SKIPS that pre-existing namespace for tests 2..N → **residue
  leaks between tests unless the wipe is adapted** (above). Beyond that: it
  clears data but NOT schema/singletons, so tests that call `server.shutdown()`
  mid-file, assume a pristine engine, or mutate a singleton will break. **Some
  files are function-scoped for a reason** — audit each, do not blanket-convert.
  Full-suite green is the gate before rollout.

### P1-a — OTLP drain guard
- **What:** guarantee the test session never wires the prod `otlp_endpoint`.
  Set `OTEL_SDK_DISABLED=true` (or a no-op exporter) at **session start, process-
  wide**, before any `setup_tracing()` — `make e2e` already does this; extend to
  the unit path (`test-ci` env / a top-of-conftest guard).
  Gotcha (wiki: *OTEL_SDK_DISABLED module-scope setdefault poisons xdist
  workers*): OTEL reads it at `TracerProvider` construction, process-wide → set
  once at session start, never module scope.
- **Why before/with P1:** its measured size (3–9s/test) tells you how much P1
  still must carry; it may be the cheaper headline. **Size it independently.**

### P1-b — e2e `-n auto` (only AFTER P1)
- Flip `make e2e` `-n0`→`-n auto` on a branch, run the full e2e set, compare
  wall-clock AND green-rate. Session `surreal_server` is a single shared port
  (namespace-isolated) — the open question is whether >1 worker saturates it
  (the same saturation that flattened unit `-n`). **If flat or flaky → revert,
  keep `-n0`.** Do NOT ship this as the P1 headline; it is a measured follow-up.

### P2 — #83 version-gate in CI (close the #144 gap)
- **What:** add a CI job (extend `verify-version-bump`, or a new
  `verify-backend-bump` step) that runs the **same logic as
  `scripts/check_backend_bump.py`** against the PR diff — but comparing
  `origin/master...HEAD` instead of `--cached`:
  ```yaml
  # ci-pr.yaml — new step in verify-version-bump (python:3.14-slim)
  - name: backend_version bump required on yadgar/backend changes
    run: |
      CHANGED=$(git diff --name-only origin/master...HEAD)
      if echo "$CHANGED" | grep -qE '(^|/)backend/|(^|/)Dockerfile\.backend$|(^|/)entrypoint-backend\.sh$'; then
        BV_HEAD=$(git show origin/master:server.json | python -c 'import json,sys;print(json.load(sys.stdin)["backend_version"])')
        BV_PR=$(python -c 'import json;print(json.load(open("server.json"))["backend_version"])')
        if [ "$BV_HEAD" = "$BV_PR" ]; then
          echo "::error::yadgar/backend changed but server.json backend_version ($BV_PR) not bumped. Bump backend_version (+ __init__.py/flake.nix/docker-compose) or add 'no-backend-release' label."
          exit 1
        fi
      fi
  ```
  Reuse `check_backend_bump.py`'s `BACKEND_BUILD_INPUTS`/`BACKEND_BUILD_DIRS`
  constants (import the module in-CI to avoid drift, rather than re-hardcoding).
- **Also:** run `scripts/check_versions.py` as a CI step so the 4 pins can't
  drift in a PR (currently pre-commit only, same skip-risk as #144).
- **Why this is the fix, not new machinery:** the enforcement logic already
  exists and is correct — it's just never run in CI. This makes the local hook's
  guarantee un-bypassable.
- **Files:** `.forgejo/workflows/ci-pr.yaml` (+ import
  `scripts/check_backend_bump.py`). No source change.

### P2-b — conditional-build assurance
- `ci-release.yaml` already builds core/backend images conditionally
  (glob + `server.json` version-field diff + registry-existence). P2 guarantees
  the bump exists so the release-time `backend_changed` detection actually fires.
  Optional: emit the computed `core=/backend=` matrix as a PR comment (dry-run)
  so the author sees "this PR will/won't rebuild the backend image" pre-merge.
- **Files:** `.forgejo/workflows/ci-release.yaml` (read-only reference; optional
  PR-preview step). No behavior change required for the core #83 fix.

### P3 — CI-vs-local exclusion + pre-push e2e gate (see §3 for the e2e decision)
- CI-vs-local: (i) CI acquires the same flock; or (ii) run the 5 chunks 2-at-a-
  time under the core/RAM budget; or (iii) dedicated runner. Kills the cross-run
  cascade (ADR-0027 P2). Reliability win, not wall-clock on a quiet box.
- Pre-push e2e gate tightening: covered in §3.

### P4 — #79 load-test harness
- Build per `perf-loadtest-contract-2026-06-30.md` unchanged: record-only first
  (earn a baseline + noise band), then Phase-2 gates. Snapshot = quiesced
  copytree pin (`YADGAR_PERF_SNAPSHOT_DIR`), driver = `_Daemon`/`_call_tool`
  direct-HTTP threads (the parallel-dispatch harness), report mirrors
  `run_longmemeval.py`, `make perf` + `.forgejo/workflows/perf.yaml`
  (`workflow_dispatch`, non-gating).
- **Task's "MCP recall serializes → 6-concurrent unmeasurable" is exactly why
  the design goes direct-HTTP** (Phase B), not batched MCP tool calls. Already
  handled.

---

## 3. Pre-push e2e — keep, move, or parallelize? (addressed head-on)

**Fact:** the `e2e-behavior-contract` pre-push hook runs `make e2e` (~20 min,
`-n0`) on every push whose diff matches `\.py$|Makefile|pyproject\.toml|
conftest\.py|docs/BEHAVIOR_CONTRACT\.md` `[code-read]`. Docs-only/config-only
pushes already skip it. e2e is NOT in CI, so **the pre-push hook is the ONLY
place the behavior contract is enforced** — removing it removes the safety net,
and No-Hook-Bypass forbids `--no-verify` around it.

**Decision: KEEP it as pre-push, but make it cheap — do not move it to CI-only.**
Rationale: CI can't run e2e (embedded surreal), so "move to CI" = "delete the
net." The cadence cost is real but is a symptom of the `-n0`×per-test-init floor,
not of the gate's existence. Fix the cost, keep the guarantee:

1. **P1 module-scope** cuts the e2e floor ~580s→~75s `[estimate]` — the biggest
   single lever, with zero loss of coverage.
2. **P1-b `-n auto`** (post-P1, if it holds) cuts it further.
3. **Tighten the `files:` filter** to `docs/BEHAVIOR_CONTRACT.md` +
   `yadgar/tests/e2e/**` + genuinely touched subsystem source, so a narrow change
   doesn't trigger the full contract. Missed contract-surface changes still hit
   CI's other gates → acceptable (ADR-0027 P3).
4. **Affected-only e2e (stretch):** map subsystem→e2e-file and run only the
   affected e2e files pre-push; run the FULL e2e nightly + on release. Preserves
   the net for the changed surface, drops the tax for narrow changes. Higher
   effort; do after P1 proves the floor is gone.

**Net:** the guarantee is unchanged (contract still enforced pre-push); the
~20 min becomes ~3–5 min via P1(+P1-b) and fires less often via the tighter
filter. No `--no-verify`, no discipline erosion.

---

## 4. SELF-AUDIT — adversarial (why each fix might NOT help / might break)

ADR-0027 already debunked two hypotheses (model-load; `-n`-contention). Do not
repeat them. Fresh skepticism per lever:

- **P1 module-scope — the load-bearing risk is test isolation, not speed, and
  the wipe guard makes it WORSE than "just share a DB."** Module-scope creates
  one namespace per file; `_wipe_surrealdb_data`'s snapshot guard (conftest:698)
  is *designed* to NOT wipe pre-existing (module/session) namespaces → for tests
  2..N the shared namespace's data survives the wipe → **silent cross-test leak
  by construction, not by accident.** This falsifies the naive "the existing wipe
  handles per-test data" assumption; P1 is only safe once the wipe is adapted to
  clear the module namespace (§P1). On top of that the wipe never touches schema,
  server singletons, or in-memory engine state — a `shutdown()`-mid-file or
  singleton-mutating test leaks regardless. The `1.62×` proof caps the win large,
  but the *realized* win = (fraction of the 74 files that tolerate module-scope
  AFTER the wipe fix). If only half tolerate it, half the win evaporates. Also:
  the fixtures use function-scoped `tmp_path` → `ScopeMismatch` forces a
  `tmp_path_factory` rewrite, so this is NOT the "mechanical scope-flip" it looks
  like. **Mitigation: pilot 5, adapt the wipe, full-suite green gate, per-file
  audit, batch rollout with per-batch re-measure. Never blanket-convert.**
- **P1-a OTLP — may be first-test-per-worker only.** If the drain is paid once
  per worker (not per test), the guard saves ~29s total, not ~3–9s×N. Size it
  before crediting it. Could also mask a real tracing bug in prod-config paths.
- **P1-b e2e `-n auto` — most likely to disappoint.** The exact saturation that
  flattened unit `-n` (single shared surreal port + init floor) applies to e2e
  too. If P1 hasn't removed the floor first, `-n auto` scales flat here as well.
  And e2e was pinned `-n0` as the *safety-net* suite — parallelism can surface
  order/port flakes that erode trust in the one gate that must be trustworthy.
  **Only attempt post-P1; revert on any flake.** This is why it is NOT P1.
- **P2 version-gate — could produce false-positive CI failures.** `git diff
  origin/master...HEAD` on a stale branch base can misclassify; the `backend/`
  regex must match the SAME set as `check_backend_bump.py` (import it, don't
  re-hardcode — drift here recreates #144 in reverse). Needs a `no-backend-
  release` escape label for legit no-image backend edits (docstring-only). Low
  risk, but a noisy gate trains devs to bypass.
- **P3 CI exclusion — the quiet-box caveat.** The cascade did NOT reproduce on a
  quiet box; it's a CROSS-RUN collision. If the real CI runner is already
  dedicated (not shared with local), P3's premise is moot — **re-measure the
  rerun tax from actual CI logs before building it** (ADR-0027 open question).
- **P4 load-test — noise-dominated for small deltas.** Memory 518987: multi-agent
  pytest contention produced 14–47 false regressions. Record-only-first + median-
  of-N + 10–15% floor is the mitigation; a gate before the noise band is known is
  worse than no gate. Good at big regressions, blind to small ones — accept that.

**Best speedup:risk ratio → P1 (module-scope `init_engines()`).** It is the ONLY
lever grounded in the measured 1.62×-overhead proof, it attacks BOTH the 1.5 h
CI and the ~20 min e2e with one mechanical change, and its risk (isolation) is
containable by pilot+measure+gate. Everything else is smaller, riskier, or
correctness-only. **P1 is the recommended first build.**

---

## 5. Recommended build order

1. **P1-a OTLP guard** (cheapest; sizes P1's residual) → re-measure the standard
   257-test slice.
2. **P1 module-scope pilot** (5 slowest files) → per-file measure → full-suite
   green → batch rollout. Re-measure unit slice AND `make e2e` after each batch.
3. **P1-b e2e `-n auto`** experiment — only if P1 landed and the e2e floor is
   gone. Ship only if measurably faster AND green.
4. **P2 / P2-b #83 CI version-gate** — independent track, can land in parallel;
   closes the #144 class permanently.
5. **P3** CI-vs-local exclusion + pre-push filter tighten — after re-measuring
   the CI rerun tax from real logs.
6. **P4** #79 load-test — record-only first, then gate.

---

## 6. Concrete file lists

**P1 / P1-a / P1-b (test-speed):**
- `yadgar/tests/conftest.py` (session fixtures, wipe, isolation, OTLP guard)
- `yadgar/tests/e2e/conftest.py` (`e2e_engines` :147)
- 74 test files with per-file `_engines` autouse (module-scope conversion)
- `Makefile` (`e2e` target `-n0`→`-n auto` only for P1-b; `test-ci` OTLP env for P1-a)
- `.pre-commit-config.yaml` (`e2e-behavior-contract` `files:` filter tighten, §3)

**P2 / P2-b (#83 version-gate + builds):**
- `.forgejo/workflows/ci-pr.yaml` (`verify-version-bump` → add backend-bump +
  `check_versions.py` steps)
- `scripts/check_backend_bump.py` (import its constants in CI — no edit needed)
- `scripts/check_versions.py` (run in CI — no edit)
- `.forgejo/workflows/ci-release.yaml` (reference; optional PR-preview of the
  `core=/backend=` matrix)

**P3 (reliability):**
- `.forgejo/workflows/ci-pr.yaml` (chunk concurrency / flock / runner)
- `Makefile` (`TEST_LOCK` extension to CI, if chosen)

**P4 (#79 load-test):** per `perf-loadtest-contract-2026-06-30.md` §"Key file
references" — new `benchmarks/run_perf.py`, `make perf`,
`.forgejo/workflows/perf.yaml`, `benchmarks/reports/perf_baseline.json`, pinned
`YADGAR_PERF_SNAPSHOT_DIR`.

---

## 7. Open questions (carry into build)
- **P1 blast radius:** how many of the 74 files tolerate module-scope? Pilot
  answers it. (ADR-0027 open Q.)
- **OTLP size:** per-test everywhere, or first-test-per-worker? Determines whether
  P1-a rivals or trails P1.
- **Post-P1 `-n` curve:** once the init floor is gone, does `-n` become a lever
  again (unit AND e2e)? Re-sweep.
- **CI rerun tax, re-measured:** pull real RERUN/ConnectError counts from recent
  forgejo run artifacts to size P3 (vs the v5.58 anchor).
- **Is the CI runner already dedicated?** If so, P3's collision premise weakens —
  verify before building.

---

## References
- `docs/plans/archive/test-suite-speedup-2026-07-01.md` (#84 feeder — P0–P4; SHIPPED v5.104, archived)
- `docs/plans/perf-loadtest-contract-2026-06-30.md` (#79 feeder — full design)
- ADR-0027 (test-speed: `-n` + model-load debunked; schema-init floor)
- ADR-0031 (recall-perf N+1 — context for why load-test guards concurrency)
- ADR-0032 (CI must not live-download ML models — HF_HUB_OFFLINE)
