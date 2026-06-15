# PLAN — v5.49.5: Grandfathered Refactor Batch 1 — `memorize`

**Status:** drafted 2026-06-09. **READY for impl.** First batch in the v5.90 grandfathered-cleanup track (see `docs/PLAN_V5_90.md`).

**Branch:** `feat/v5.49.5-memorize-refactor` off master.

**Effort estimate:** 6–8 hours total (function is 608 LOC with cyclo=114; needs careful split + golden-output tests).

---

## 1. Background

`yadgar/server/tools/memorize.py::memorize` (line 76 — explicitly annotated `# noqa: C901, PLR0913 — pre-existing complexity + v5.10.x reason kwarg`). Highest cyclo in the entire codebase:

| Entry | cyclo | loc | nesting | params |
|---|---|---|---|---|
| `memorize@76` (current) | **114** | 608 | 6 | 10 |
| `memorize@36` (stale baseline orphan) | 84 | 493 | 6 | 5 |

The `@36` entry is a stale orphan from a previous code position — baseline was updated without GC'ing the old record. Both eliminated by this refactor.

Plus 7 stale `remember@<line>` baseline orphans (all with cyclo=1, loc=11 — trivially small but cluttering the baseline). Sweep included.

## 2. Resolved decisions

| DP | Decision | Rationale |
|---|---|---|
| **A — refactor strategy** | **Split `memorize()` into 6 phase-functions** (validation → branch resolution → embedding → contradiction detection → store → post-write hooks), called sequentially from a slim orchestrator | Mirrors the actual write-path stages (already implicit in current code). Each phase becomes independently testable. Slim orchestrator targets cyclo ≤ 10. |
| **B — public API preservation** | **`memorize()` signature unchanged** | This is an MCP tool — public surface frozen. Internal refactor only. |
| **C — behavior preservation** | **Golden-output snapshot tests on the v5.49.4 behavior** | Capture (a) accept-path output dict, (b) reject-path output dict for each reject reason, (c) queue side-effects, (d) DLQ side-effects. Pre-refactor: snapshot. Post-refactor: must match exactly. |
| **D — baseline GC** | **Add `--gc` flag to `scripts/check_complexity.py`** | Removes baseline entries whose `<symbol>@<line>` does NOT match a current symbol. Required to clear stale `memorize@36` + 7 `remember@<line>` orphans without manual JSON edits. |
| **E — phase boundary helpers stay public-package-private** | **Phase functions live in `yadgar/server/tools/_memorize_phases/` package** | Underscore prefix = pkg-internal. NOT exposed via `tools/__init__.py`. Future MCP tools that need similar phases can import from this package. |

## 3. Scope

### Refactor target

`yadgar/server/tools/memorize.py::memorize` (current 608 LOC, cyclo=114) → orchestrator + 6 phase helpers in a new internal sub-package.

### New file layout

```
yadgar/server/tools/
├── memorize.py                          # orchestrator + public memorize() + remember()
└── _memorize_phases/                    # internal phase helpers (one per stage)
    ├── __init__.py                      # re-exports phases for orchestrator
    ├── _phase_validate.py               # arg validation, branch_hint resolution, secret gate
    ├── _phase_resolve_branch.py         # branch resolution from cwd / branch_hint / fallback
    ├── _phase_embed.py                  # embedding request + retry / timeout
    ├── _phase_contradiction.py          # contradiction detector w/ heuristic
    ├── _phase_store.py                  # storage write + ID return
    └── _phase_post_write.py             # heat boost, link insertion, telemetry
```

Each phase function:
- Takes a `MemorizeContext` dataclass (carrying state between phases) + any per-phase typed inputs.
- Returns `MemorizeContext` (mutated) OR raises a typed `MemorizePhaseError` subclass.
- Target cyclo ≤ 15.
- Target LOC ≤ 80.
- Target nesting ≤ 4.

### `MemorizeContext` shape

Sketch:

```python
@dataclass
class MemorizeContext:
    # Inputs (frozen after _phase_validate)
    content: str
    context: str
    tags: list[str]
    is_protected: bool
    tier: str | None
    valid_until: str | None
    ttl_days: int | None
    provenance_agent: str | None
    reason: str
    branch_hint: str | None
    # Derived (set during phases)
    resolved_branch: str | None = None
    embedding: list[float] | None = None
    contradictions: list[int] = field(default_factory=list)
    stored_id: int | None = None
    # Side effects (collected for post-write phase)
    queued_ops: list[QueuedOp] = field(default_factory=list)
    rejection_reason: str | None = None
```

### Orchestrator

`memorize()` reduced to:

```python
async def memorize(...):
    ctx = MemorizeContext(...)
    try:
        ctx = _phase_validate(ctx)
        ctx = _phase_resolve_branch(ctx)
        ctx = await _phase_embed(ctx)
        ctx = _phase_contradiction(ctx)
        ctx = await _phase_store(ctx)
        ctx = _phase_post_write(ctx)
    except MemorizePhaseError as exc:
        return _format_rejection(ctx, exc)
    return _format_success(ctx)
```

Target cyclo ≤ 10. The two `_format_*` helpers live in `memorize.py` (keep result shape close to public surface).

## 4. Non-goals

- No new MCP tools, no new public surface.
- No write-path behavior changes.
- No performance optimisation.
- No baseline-update tooling beyond `--gc` flag.

## 5. Test plan

### Pre-refactor — snapshot harness

Add `yadgar/tests/test_v5_49_5_memorize_snapshots.py`:

1. `test_memorize_accept_path_returns_expected_dict` — call `memorize()` with valid args; capture result dict; pickle to `yadgar/tests/snapshots/memorize_accept_v5_49_4.json`. Compare via golden-file pattern.
2. `test_memorize_reject_duplicate_path_returns_expected_dict` — same for similarity-gate rejection.
3. `test_memorize_reject_missing_branch_returns_expected_dict` — same for branch-hint rejection.
4. `test_memorize_reject_secret_leak_returns_expected_dict` — same for secret-gate rejection.
5. `test_memorize_writes_to_queue` — assert queue side-effect (queued op with right schema).
6. `test_memorize_writes_to_dlq_on_failure` — assert DLQ side-effect for permanent failure.

### Post-refactor — phase-level tests

Add `yadgar/tests/test_v5_49_5_memorize_phases.py`:

7. `test_phase_validate_rejects_empty_content`
8. `test_phase_validate_rejects_missing_branch`
9. `test_phase_validate_calls_secret_gate`
10. `test_phase_resolve_branch_uses_branch_hint_when_cwd_fails`
11. `test_phase_resolve_branch_prefers_cwd_when_both_available`
12. `test_phase_embed_returns_vector`
13. `test_phase_embed_retries_on_timeout`
14. `test_phase_contradiction_flags_known_pairs`
15. `test_phase_store_returns_id`
16. `test_phase_post_write_writes_link_when_contradictions_present`

### Baseline GC

17. `test_check_complexity_gc_removes_orphan_entries` — populate baseline with a fake `<symbol>@<line>` that doesn't exist in code; run `--gc`; assert entry removed.
18. `test_check_complexity_gc_preserves_current_entries` — assert real entries kept.

### Integration sanity

- All v5.49.0–v5.49.4 tests still green.
- Snapshot tests (1–6) pass before AND after refactor.
- Phase tests (7–16) pass post-refactor.
- Baseline `memorize.py` entries reduce from 11 to 8 (orchestrator + 6 phase functions + 1 file-LOC) — actual count depends on _phase shape but MUST not increase.

## 6. Phases (agent dispatch)

1. **Snapshot harness** — write tests 1–6 with golden files. Confirm GREEN against current code. → COMMIT `test(memorize): golden snapshots for v5.49.5 refactor`
2. **Baseline GC tool** — add `--gc` to `scripts/check_complexity.py`. Tests 17–18. → COMMIT `feat(check-complexity): --gc flag removes stale orphan entries`
3. **Extract `MemorizeContext` + phase scaffolding** — new package, dataclass, empty phase functions raising NotImplementedError. → COMMIT `refactor(memorize): extract phase scaffolding for v5.49.5 split`
4. **Migrate phases one at a time** — validate → resolve_branch → embed → contradiction → store → post_write. After each, re-run snapshot tests. → 6 COMMITS `refactor(memorize): migrate <phase> phase`
5. **Slim orchestrator** — once all 6 phases active, orchestrator becomes the 10-line dispatch. → COMMIT `refactor(memorize): slim orchestrator + remove old monolithic memorize`
6. **Run `--gc`** — clean stale orphans. Update baseline. → COMMIT `chore: gc complexity baseline (stale memorize/remember orphans)`
7. **Version bump + CHANGELOG** — 5.49.4 → 5.49.5. → COMMIT `chore: bump version 5.49.4 → 5.49.5 + CHANGELOG`

Or single commit if agent prefers. Phased is cleaner for review.

## 7. Acceptance gates

- `memorize.py` orchestrator cyclo ≤ 10.
- All 6 phase functions cyclo ≤ 15.
- All phase LOC ≤ 80, nesting ≤ 4.
- Snapshot tests (1–6) byte-for-byte match before/after.
- Phase tests (7–16) green.
- Baseline GC tests (17–18) green.
- v5.49.x existing tests still green.
- Pre-commit clean. NO `--no-verify`.

## 8. Risks

- **Subtle write-path behavior changes.** Mitigation: 6 golden-output snapshots + DLQ/queue side-effect assertions. Any divergence = revert.
- **Phase boundary mis-design.** Mitigation: scaffolding step (phase 3) sets boundaries BEFORE moving real code. Easier to revise boundaries while phases are still NotImplementedError.
- **Performance regression.** Mitigation: `memorize` is a tool handler — not on a hot inner loop. Adding 6 function-call frames is negligible. If measured regression > 5%, inline the smallest phase.
- **Baseline `--gc` flag silently removes real entries.** Mitigation: tests 17–18. Plus the flag is opt-in; pre-commit hook doesn't auto-`--gc`.

## 9. References

- `docs/PLAN_V5_90.md` — umbrella tracking plan
- `yadgar/server/tools/memorize.py` — refactor target
- `scripts/check_complexity.py` — enforcement script (gets `--gc` flag)
- `.complexity-baseline.json` — pre-refactor baseline (4 stale orphans to be GC'd)
