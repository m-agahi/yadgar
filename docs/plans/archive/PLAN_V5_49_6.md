# PLAN — v5.49.6: Untested Modules Audit + Coverage Wave 1

**Status:** drafted 2026-06-09. **READY for impl.**

**Branch:** `feat/v5.49.6-coverage-wave-1` off master.

**Effort estimate:** 6–10 hours total. Phase 0 audit ~30min. Phases 1–N test writing depends on module pick (per-module budget ~30min).

---

## 1. Background

Memory checkpoint flagged "89 untested modules" weeks ago. Survey 2026-06-09 of `yadgar/**/*.py` (excluding tests + `__init__.py`):

- 194 non-init modules total
- 313 test files total
- 160 modules with no test file by simple name-match (over-estimate — many modules ARE tested by tests with different names, e.g. `update/orchestrator.py` is exercised by `test_upgrade_orchestrator.py`)

The simple heuristic is unreliable. v5.49.6 phase 0 produces an accurate inventory via `coverage.py` (or pytest --cov) before any test writing.

---

## 2. Resolved decisions

| DP | Decision | Rationale |
|---|---|---|
| **A — coverage tool** | **`coverage.py` + `pytest-cov` plugin** | Industry standard. Already in pyproject dev extras (verify; if not, add). Run once per release to delta-track. |
| **B — coverage target for "untested"** | **<10% line coverage = "untested" for this release's purposes** | Modules with 0% are absolute targets; <10% means there might be a stub test that doesn't actually exercise the module. |
| **C — coverage target post-release** | **Each module touched gets to ≥60% line coverage** | Realistic for one writing pass. Not aiming for 100% (test bloat). Happy path + 2-3 edge cases + 1 failure mode = ~60%. |
| **D — scope per release** | **Top 10 untested modules by LOC** | Highest-impact debt clearance per release. v5.49.7+ continues until untested-count hits the floor. |
| **E — test file naming** | **`test_<module_name>.py`** for new tests | Aligns with the simple name-match heuristic going forward + future audits don't need coverage tooling. |
| **F — happy-path coverage strategy** | **Mock at module boundaries (storage, HTTP, embedding service) — unit tests, not integration** | Integration tests already exist for the cross-module flows. Per-module unit tests cover the module's own logic, not its callers' or callees'. |

---

## 3. Scope

### Phase 0 — Audit

Add `coverage` + `pytest-cov` to dev/test extras (if not present). Run:

```bash
OTEL_SDK_DISABLED=true pytest \
  --cov=yadgar \
  --cov-report=term-missing:skip-covered \
  --cov-report=json:/tmp/yadgar-coverage.json \
  -m "not integration" -p no:xdist \
  yadgar/tests/
```

Generate `docs/UNTESTED_MODULES_V5_49_6.md` listing every yadgar module with <10% coverage, sorted by LOC descending. Format:

```markdown
| Module | LOC | Cov % | Lines covered / total |
|---|---|---|---|
| yadgar/server/http.py | 1684 | 2% | 34 / 1620 |
| yadgar/config_yaml.py | 1341 | 5% | 65 / 1290 |
...
```

This file gets committed for future audits / per-release scope picks.

### Phase 1–10 — Test writing

Top 10 by LOC from the Phase 0 inventory. Each phase = one module + one test file. Per-module structure:

1. **Happy path** — call the primary public entry point with valid args; assert return shape + return value.
2. **Edge cases** — empty input, missing optional arg, boundary value (e.g. 0, max, off-by-one).
3. **Failure modes** — invalid type, missing required dep, downstream error propagation.
4. **Side effects** — if the module has side effects (DB writes, network calls), mock + assert call shape.

Targets ≥60% line coverage per module. Some modules may be hard to test in isolation (mostly imports / stub code) — document why coverage stays low and move on.

### Module pick guidance (Phase 0 will refine)

Likely candidates by LOC + believed-untested:

- `yadgar/server/http.py` (FastAPI app + endpoint glue — tests likely cover `/health` only)
- `yadgar/config_yaml.py` (config loader)
- `yadgar/log_config.py` (logging setup)
- `yadgar/embed_service.py` (embed RPC client)
- `yadgar/metrics.py` (Prometheus metric writers)
- `yadgar/storage/client.py` (SurrealDB HTTP client)
- `yadgar/server/lifecycle.py` (FastAPI lifespan)
- `yadgar/cli/stats.py` (stats CLI)
- `yadgar/storage/blocks.py` (block storage layer)
- `yadgar/export/duckdb_exporter.py` (analytics export)

Phase 0 audit will produce the actual top-10 (may differ from this list).

### Phase 11 — Update `docs/UNTESTED_MODULES_V5_49_6.md`

After all 10 modules covered, rebuild the inventory + commit the diff. Shows the progress + sets up v5.49.7 scope.

### Phase 12 — Version bump + CHANGELOG

`pyproject.toml:version` 5.49.5 → 5.49.6. CHANGELOG entry listing modules covered + delta.

---

## 4. Non-goals

- 100% coverage of any module.
- Integration tests for cross-module flows (already exist for the major paths).
- Refactor of modules during test-writing (refactor track stays paused at v5.90.x).
- Coverage of `_test_*` / `_dev_*` helpers.

---

## 5. Acceptance gates

- Phase 0 inventory committed.
- All 10 modules reach ≥60% line coverage OR the module has a documented "untestable" reason (e.g. pure import shim, top-level constants).
- ALL existing tests still green (v5.49.0–v5.49.5 + others).
- Pre-commit clean. NO `--no-verify`. NO `--no-gpg-sign`.
- ruff clean.
- `check-versions` passes.
- Total project coverage delta documented in CHANGELOG.

---

## 6. Phases (agent dispatch)

0. **Coverage tool + audit** — add cov deps if missing, run pytest --cov, generate `docs/UNTESTED_MODULES_V5_49_6.md`. → COMMIT `chore: add coverage tooling + initial untested-modules audit`
1–10. **Per-module test writing** — one phase per module from the top 10. → 10 COMMITS `test(<module>): cover happy path + edge cases + failure modes`
11. **Re-audit + diff** — update inventory after writing. → COMMIT `docs(coverage): post-wave-1 inventory diff`
12. **Version bump + CHANGELOG** — 5.49.5 → 5.49.6. → COMMIT `chore: bump version 5.49.5 → 5.49.6 + CHANGELOG`

Or single bundled commit if cleaner. Phased preferred for review.

---

## 7. Risks

- **Phase 0 over-budget.** If running coverage on the full suite takes longer than expected (the suite is parallel but coverage adds overhead), time-box at 10min wall-clock. If hit, run on a subset + extrapolate, document the gap.
- **Module hard to test in isolation.** Mitigation: document and move on. Coverage debt that genuinely can't be reduced is the floor.
- **Coverage measurement overhead masks real test results.** Mitigation: phase 0 runs cov separately; phases 1–10 run pytest plain (no cov flag in CI).
- **Mocking creates fragile tests.** Mitigation: mock at module boundaries (HTTP, DB client), not at module internals. Tests verify input/output contracts, not impl.

---

## 8. References

- `pyproject.toml [tool.pytest.ini_options]` — test config
- Top 30 untested by LOC (simple name-match heuristic, 2026-06-09 snapshot — see commit msg)
- `docs/PLAN_V5_90.md` — refactor track paused until user re-opens; this release does NOT refactor any modules even when test-writing reveals smell.
