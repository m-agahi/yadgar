# PLAN — v5.49.8: Untested Modules Coverage Wave 3

**Status:** drafted 2026-06-09. **READY for impl.** Continuation of v5.49.6 (Wave 1, 10 modules) + v5.49.7 (Wave 2, 10 modules). Wave 3 = next 10 untested.

**Branch:** `feat/v5.49.8-coverage-wave-3` off master.

**Effort estimate:** 5–7 hours. Pattern proven over waves 1+2.

---

## 1. Background

Cumulative coverage debt clearance:
- Wave 1 (v5.49.6): 10 modules covered. Project: 59 modules <10% → 49 remaining.
- Wave 2 (v5.49.7): 10 modules covered. Cumulative 20 modules, ~618 new tests.
- Wave 3 (v5.49.8): pick next 10. Project should be at ~39 modules <10% pre-wave (subject to Phase 0 re-audit).

---

## 2. Resolved decisions

Unchanged from v5.49.6 / v5.49.7 plans:

| DP | Decision |
|---|---|
| **A — tool** | `pytest-cov` already in test extras |
| **B — "untested" threshold** | <10% line coverage |
| **C — per-module target** | ≥60% line coverage OR documented floor |
| **D — per-release scope** | Top 10 untested by stmt count |
| **E — test file naming** | `test_<module_stem>.py` |
| **F — strategy** | Mock at module boundaries — unit tests not integration |
| **G — refactor** | PAUSED (v5.90 track) — do NOT refactor during test-writing |

---

## 3. Scope

### Phase 0 — Re-audit (agent runs)

Run `pytest --cov=yadgar` against current master. Generate sorted-by-stmts list of <10% covered modules. Compare against `docs/UNTESTED_MODULES_V5_49_6.md` (or whatever audit doc is canonical). Pick top 10. Document any surprises in commit message.

### Likely Wave 3 candidates (Phase 0 will confirm)

Based on wave 2 audit DEFERRED list:

- `yadgar/hooks/post-tool-capture.py`
- `yadgar/hooks/instructions-loaded.py`
- `yadgar/hooks/subagent_start.py`
- `yadgar/hooks/file-changed.py`
- `yadgar/hooks/session-start-context.py`
- `yadgar/cli/install_hooks.py`
- `yadgar/cli/setup.py`
- `yadgar/cli/install_subagents.py`
- `yadgar/cli/install_service.py`
- `yadgar/cli/configure_mcp.py`

If actual audit shows different stmt counts or modules above this list, agent swaps.

### Phases 1–10 — Per-module tests

Same approach as waves 1+2. Per module:
- Read public surface.
- `yadgar/tests/test_<stem>.py`.
- Happy path + 2–3 edge cases + 1–2 failure modes + side-effect mocks.
- Target ≥60%. Document untestable floor if unreachable in 30min.

### Phase 11 — Audit doc update

Update `docs/UNTESTED_MODULES_V5_49_6.md` (cumulative) with Wave 3 results section, OR create `docs/UNTESTED_MODULES_V5_49_8.md` (agent's call — match wave 2's pattern).

### Phase 12 — Version bump + CHANGELOG

5.49.7 → 5.49.8. CHANGELOG entry listing modules + cumulative wave totals.

---

## 4. Phases (agent dispatch)

0. **Re-audit** → COMMIT `chore(coverage): re-audit pre-wave-3`
1–10. **Per-module tests** → 10 COMMITS (or single bundle) `test(<module>): wave-3 coverage`
11. **Audit doc update** → COMMIT `docs(coverage): post-wave-3 inventory diff`
12. **Version bump + CHANGELOG** → COMMIT `chore: bump version 5.49.7 → 5.49.8 + CHANGELOG`

Phased preferred; single bundle acceptable.

---

## 5. Acceptance gates

- 10 modules from wave have ≥60% coverage OR documented untestable floor.
- ALL existing tests still green (v5.49.0–v5.49.7 + others).
- Pre-commit clean. NO `--no-verify`. NO `--no-gpg-sign`.
- ruff clean.
- `check-versions` passes.
- Cumulative project coverage delta documented.

---

## 6. Non-goals

Same as v5.49.6 / v5.49.7. No refactor. No integration tests. No 100% targets.

---

## 7. References

- `docs/PLAN_V5_49_6.md` (Wave 1)
- `docs/PLAN_V5_49_7.md` (Wave 2)
- `docs/UNTESTED_MODULES_V5_49_6.md` (rolling audit)
- `docs/PLAN_V5_90.md` (refactor track — paused)
