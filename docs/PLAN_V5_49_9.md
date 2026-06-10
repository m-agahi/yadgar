# PLAN — v5.49.9: Untested Modules Coverage Wave 4

**Status:** drafted 2026-06-10. **READY for impl.** Continuation of v5.49.6 (Wave 1, 10 modules) + v5.49.7 (Wave 2, 10 modules) + v5.49.8 (Wave 3, 11 modules). Wave 4 = next 10 untested.

**Branch:** `feat/v5.49.9-coverage-wave-4` off master.

**Effort estimate:** 4–6 hours. Pattern proven over waves 1+2+3. Parallel worktree dispatch reduces wall-clock time.

---

## 1. Background

Cumulative coverage debt clearance:
- Wave 1 (v5.49.6): 10 modules covered, 315 tests.
- Wave 2 (v5.49.7): 10 modules covered, 302 tests.
- Wave 3 (v5.49.8): 11 modules covered, 134 tests.
- **Wave 4 (v5.49.9):** pick next 10 by stmt count from remaining DEFERRED list.

Rolling audit: `docs/UNTESTED_MODULES_V5_49_6.md`. After waves 1–3, ~28 DEFERRED Phase-2 modules remain with <10% coverage.

---

## 2. Resolved decisions

Unchanged from v5.49.6 / v5.49.7 / v5.49.8 plans:

| DP | Decision |
|---|---|
| **A — tool** | `pytest-cov` already in test extras |
| **B — "untested" threshold** | <10% line coverage |
| **C — per-module target** | ≥80% line coverage OR documented floor |
| **D — per-release scope** | Top 10 untested by stmt count |
| **E — test file naming** | `test_<module_stem>.py` |
| **F — strategy** | Mock at module boundaries — unit tests not integration |
| **G — refactor** | PAUSED (v5.90 track) — do NOT refactor during test-writing |

Note on target: waves 1–3 used ≥60% with "floor" exception. Wave 4 raises aspirational bar to ≥80% per module where pure functions permit; floor documentation pattern unchanged.

---

## 3. Scope

### Wave 4 module list (top 10 DEFERRED by stmt count, post-wave-3 state)

| # | Module | Stmts | Cov % | Priority |
|---|---|---|---|---|
| 1 | `yadgar/seed/_scan.py` | 102 | 0.0% | High — largest remaining DEFERRED |
| 2 | `yadgar/metacognition/coverage.py` | 86 | 4.7% | High |
| 3 | `yadgar/curation/strengthen.py` | 81 | 8.6% | High |
| 4 | `yadgar/observability/timing.py` | 72 | 0.0% | High |
| 5 | `yadgar/__main__.py` | 71 | 0.0% | High |
| 6 | `yadgar/metacognition/gap_detection.py` | 64 | 4.7% | Medium |
| 7 | `yadgar/retrieval/_reranking_heuristic.py` | 61 | 4.9% | Medium |
| 8 | `yadgar/update/install_methods.py` | 52 | 0.0% | Medium |
| 9 | `yadgar/retrieval/_reranking_mmr.py` | 50 | 6.0% | Medium |
| 10 | `yadgar/install_subagents_lib.py` | 46 | 0.0% | Medium |

**Total stmts targeted:** 685. Sorted by LOC × (1 - cov%) descending.

### Phase 0 — Re-audit (agent runs)

Run `pytest --cov=yadgar` against current master HEAD. Confirm above stmt counts + coverage figures. Swap any module that has moved above 10% (wave-3 side-effects). Document swaps in commit message.

### Phases 1–10 — Per-module tests

Same approach as waves 1–3. Per module:
- Read public surface + imports.
- Write `yadgar/tests/test_<stem>.py`.
- Happy path + 2–3 edge cases + 1–2 failure modes + mocked side-effects.
- Target ≥80%. Document untestable floor if unreachable.
- Test file name conventions for multi-word stems: `test_seed_scan.py`, `test_metacognition_coverage.py`, `test_curation_strengthen.py`, `test_observability_timing.py`, `test_main_module.py`, `test_metacognition_gap_detection.py`, `test_reranking_heuristic.py`, `test_update_install_methods.py`, `test_reranking_mmr.py`, `test_install_subagents_lib.py`.

### Phase 11 — Audit doc update

Update `docs/UNTESTED_MODULES_V5_49_6.md` (cumulative) with Wave 4 results section. Mark covered modules as `TARGETED (Wave 4)`. Record post-cov %, delta, and new test count per module.

### Phase 12 — Version bump + CHANGELOG

5.49.8 → 5.49.9. CHANGELOG entry listing all 10 modules + cumulative wave totals (waves 1+2+3+4).

---

## 4. Parallel worktree split

Wave 4 uses 3 parallel worktrees to reduce wall-clock time. Groups are disjoint — no shared file edits.

### Group A — `feat/v5.49.9-wave-4-group-a`

**Modules:**
1. `yadgar/seed/_scan.py` → `yadgar/tests/test_seed_scan.py`
2. `yadgar/metacognition/coverage.py` → `yadgar/tests/test_metacognition_coverage.py`
3. `yadgar/metacognition/gap_detection.py` → `yadgar/tests/test_metacognition_gap_detection.py`
4. `yadgar/curation/strengthen.py` → `yadgar/tests/test_curation_strengthen.py`

**Rationale:** seed + metacognition + curation share no import graph overlaps. All are pure/near-pure function modules.

**Agent:** `Agent(subagent_type="general-purpose", model="sonnet")`

### Group B — `feat/v5.49.9-wave-4-group-b`

**Modules:**
1. `yadgar/observability/timing.py` → `yadgar/tests/test_observability_timing.py`
2. `yadgar/__main__.py` → `yadgar/tests/test_main_module.py`
3. `yadgar/update/install_methods.py` → `yadgar/tests/test_update_install_methods.py`
4. `yadgar/install_subagents_lib.py` → `yadgar/tests/test_install_subagents_lib.py`

**Rationale:** observability + entry point + update + install-libs. No overlap with Group A or C modules.

**Agent:** `Agent(subagent_type="general-purpose", model="sonnet")`

### Group C — `feat/v5.49.9-wave-4-group-c`

**Modules:**
1. `yadgar/retrieval/_reranking_heuristic.py` → `yadgar/tests/test_reranking_heuristic.py`
2. `yadgar/retrieval/_reranking_mmr.py` → `yadgar/tests/test_reranking_mmr.py`

**Rationale:** retrieval submodules are tightly coupled to each other (share `_reranking_*` namespace and potentially fixtures) — kept together in one group. Only 2 modules, lighter group, agent completes faster.

**Agent:** `Agent(subagent_type="general-purpose", model="sonnet")`

### Isolation contract

- Each group agent operates in its own git worktree (`isolation: "worktree"`).
- No agent touches `conftest.py`, `pyproject.toml`, or any source file under `yadgar/` (tests only).
- Each group's branch merges to master after its own CI passes — no waiting for other groups.

---

## 5. Test conventions

Matches existing wave style:

- **Framework:** `pytest` (existing infra, `uv run pytest`)
- **Fixtures:** import from `conftest.py` (existing fixtures: `mock_storage_engine`, `mock_embedding_engine`, `tmp_path`, etc.). No new conftest entries.
- **Mock strategy:** `unittest.mock.patch` / `MagicMock` at module import boundary. Patch `yadgar.<dep>` not internal private names.
- **Coverage run:** `OTEL_SDK_DISABLED=true uv run pytest --cov=yadgar --cov-report=term-missing -m "not integration" -p no:xdist --override-ini="addopts="` for each group's test file(s).
- **Target per module:** ≥80% line coverage OR documented untestable floor with rationale.
- **Floor documentation:** inline comment block `# UNTESTABLE FLOOR: ...` at top of test file when floor applies.

---

## 6. Acceptance gates

- 10 modules from wave have ≥80% coverage OR documented untestable floor.
- ALL existing tests still green (v5.49.0–v5.49.8 + others).
- Pre-commit clean. NO `--no-verify`. NO `--no-gpg-sign`.
- ruff clean on new test files.
- `check-versions` passes.
- Cumulative project coverage delta documented in audit doc.
- Each group branch merged cleanly (no conflicts on test-only files).

---

## 7. Ship sequence

1. Group A, B, C agents run in parallel worktrees.
2. Each group: tests pass → commit → push → merge branch to master.
3. After all 3 groups merged: single version bump commit on master (5.49.8 → 5.49.9) + CHANGELOG update.
4. Tag `v5.49.9`. Release notes reference all 3 groups' module ranges (A: seed/metacognition/curation, B: observability/update/install, C: retrieval reranking).

---

## 8. Non-goals

Same as v5.49.6 / v5.49.7 / v5.49.8. No refactor. No integration tests. No 100% targets as hard requirement. No edits to `yadgar/` source modules.

---

## 9. References

- `docs/PLAN_V5_49_6.md` (Wave 1)
- `docs/PLAN_V5_49_7.md` (Wave 2)
- `docs/PLAN_V5_49_8.md` (Wave 3)
- `docs/UNTESTED_MODULES_V5_49_6.md` (rolling audit)
- `docs/PLAN_V5_90.md` (refactor track — paused)
