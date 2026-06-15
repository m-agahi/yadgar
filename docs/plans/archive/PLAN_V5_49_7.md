# PLAN — v5.49.7: Untested Modules Coverage Wave 2

**Status:** drafted 2026-06-09. **READY for impl.** Continuation of v5.49.6 (Wave 1 covered top 10; this release picks the next 10 from the DEFERRED list in `docs/UNTESTED_MODULES_V5_49_6.md`).

**Branch:** `feat/v5.49.7-coverage-wave-2` off master.

**Effort estimate:** 5–8 hours. Hook scripts + CLI dispatchers are easier than the Wave 1 hot-path modules.

---

## 1. Background

v5.49.6 Wave 1 covered 10 modules from the 59 at <10% line coverage. Audit doc `docs/UNTESTED_MODULES_V5_49_6.md` lists the remaining DEFERRED entries; Wave 2 picks the top 10 by stmt count from that list.

---

## 2. Resolved decisions

Unchanged from v5.49.6 plan (`docs/PLAN_V5_49_6.md`). Re-iterated:

| DP | Decision |
|---|---|
| **A — tool** | `pytest-cov` already in test extras |
| **B — "untested" threshold** | <10% line coverage |
| **C — per-module target** | ≥60% line coverage OR documented floor |
| **D — per-release scope** | Top 10 untested by stmt count |
| **E — test file naming** | `test_<module_stem>.py` |
| **F — strategy** | Mock at module boundaries — unit tests not integration |

---

## 3. Scope — Wave 2 module picks

From the DEFERRED entries in `docs/UNTESTED_MODULES_V5_49_6.md`, sorted by stmt count descending:

| Wave 2 phase | Module | Stmts | Pre-cov | Notes |
|---|---|---|---|---|
| **1** | `yadgar/cli/daemon.py` | 153 | 0% | CLI dispatcher — subprocess glue. Mock at `subprocess.run` boundary. |
| **2** | `yadgar/metacognition/cognitive_load.py` | 148 | 6% | Cognitive-load tracking; mostly pure functions. Easier. |
| **3** | `yadgar/models.py` | 142 | 0% | Dataclasses + enums. EASY — just instantiate + assert. |
| **4** | `yadgar/hooks/session-end-capture.py` | 142 | 0% | Hook script. Test the helper functions; mock filesystem. |
| **5** | `yadgar/cli/seed.py` | 142 | 0% | CLI dispatcher for `yadgar seed`. Mock storage layer. |
| **6** | `yadgar/hooks/subagent_stop.py` | 141 | 0% | Hook script. Same approach as session-end-capture. |
| **7** | `yadgar/hooks/prompt-recall.py` | 126 | 0% | Hook script. Same approach. |
| **8** | `yadgar/curation/prune_passes.py` | 109 | 6% | Prune logic — pure functions over storage queries. Mock storage. |
| **9** | `yadgar/hooks/post-tool-capture.py` | TBD | 0% | Hook script. (Phase 0 audit will confirm stmt count.) |
| **10** | `yadgar/hooks/instructions-loaded.py` | TBD | 0% | Hook script. |

If Phase 0 audit (re-run coverage on current master) reveals different stmt counts, swap based on actual numbers. Plan order is leverage × isolation: easy wins (models, cognitive_load) first to build momentum, then hooks + CLI dispatchers.

---

## 4. Phases (agent dispatch)

0. **Phase 0 — Re-audit.** Run `pytest --cov=yadgar` against current master (5.49.6). Confirm Wave 2 picks against actual coverage; swap if needed. Document delta in commit msg. → COMMIT `chore(coverage): re-audit pre-wave-2`
1–10. **Per-module test writing.** 10 modules, one phase per. → 10 COMMITS `test(<module>): wave-2 coverage`
11. **Re-audit + diff.** Update `docs/UNTESTED_MODULES_V5_49_6.md` with Wave 2 deltas (or create `docs/UNTESTED_MODULES_V5_49_7.md` if cleaner — agent decides). → COMMIT `docs(coverage): post-wave-2 inventory diff`
12. **Version bump + CHANGELOG.** 5.49.6 → 5.49.7. → COMMIT `chore: bump version 5.49.6 → 5.49.7 + CHANGELOG`

Phased preferred; single bundle acceptable.

---

## 5. Acceptance gates

- 10 modules from the wave have ≥60% line coverage OR documented untestable reason.
- ALL existing tests still green.
- Pre-commit clean. NO `--no-verify`. NO `--no-gpg-sign`.
- ruff clean.
- `check-versions` passes.
- Audit doc updated (or new doc created).
- CHANGELOG entry listing modules + project-wide coverage delta.

---

## 6. Non-goals

Same as v5.49.6: no 100% targets, no integration tests, no refactor (v5.90 paused), no test infra changes beyond coverage tooling.

---

## 7. References

- `docs/PLAN_V5_49_6.md` — v5.49.6 wave 1 plan
- `docs/UNTESTED_MODULES_V5_49_6.md` — initial audit + wave 1 results
- `docs/PLAN_V5_90.md` — refactor track (paused — do NOT refactor any module touched here)
