# PLAN — v5.41.3: MCP-handler perf test + correct I9 attribution

**Status:** drafted 2026-06-02. Pure-docs + tiny test addition. Tag: hotfix.

**Origin:** v5.41.1 (commit `dcf0c78`) introduced `test_update_under_5ms_p50` measuring `st.update_wiki_page(...)` directly — the storage layer = queue-worker call path, NOT the MCP request path. CHANGELOG + MIGRATION_NOTES + test docstring incorrectly attribute the ~89ms baseline to an I9 violation. I9 governs the MCP handler (file enqueue), not the queue worker.

**Why now:** I9 attribution drift = wrong signal for future contributors. They'll either (a) chase a non-violation forever, (b) become numb to I9 warnings, (c) assume I9 doesn't apply to wiki. All bad. Fix doc + add the correct perf test.

**Effort estimate:** 0.25 day.

**Branch:** `fix/v5.41.3-mcp-perf-i9-attribution` off master.

---

## Layer model (explicit)

| Layer | Location | Latency budget |
|---|---|---|
| MCP handler | `yadgar/server/tools/wiki.py::wiki_add` | **I9 ≤5ms p50** — file enqueue only |
| File queue write | `Path.write_text(json.dumps(payload))` | sub-ms expected |
| Queue worker | `yadgar/queue/drainer.py` (or similar) | not I9; heavy work allowed per I2/I4 |
| Storage layer | `yadgar/storage/wiki.py::update_wiki_page` | not I9; ~89ms on embedded SurrealKV (pre-existing) |

## Fix scope

### 1. MCP-handler perf test (NEW)

`yadgar/tests/test_wiki_mcp_handler_perf.py`:

- Time the `wiki_add(...)` MCP tool function directly (not storage).
- 100 calls; assert p50 ≤5ms (true I9 budget).
- Bypass the queue worker — measure the handler's own time-to-return.
- Document explicitly: "this is the I9 budget; storage-layer latency is a separate concern measured by `test_wiki_versioning_atomicity::TestUpdatePerfUnder5msP50`."

**Mock strategy (opus reviewer note):** use a REAL tmpdir for the queue file write. Only mock the drainer-side processing. Otherwise the test measures dictionary serialization, not the real handler — the `Path.write_text` cost IS part of I9-budgeted MCP handler path. Add explicit assertion: drainer is NOT called on the request thread (test that handler returns before drainer processes the job).

### 2. Re-attribute test docstring

`yadgar/tests/test_wiki_versioning_atomicity.py::TestUpdatePerfUnder5msP50`:
- Rename: `TestUpdatePerfUnder5msP50` → `TestStorageUpdatePerfRegressionGuard`
- Update docstring: remove "I9 budget" / "pre-existing I9 violation" framing. Replace with: "Regression guard at storage layer (queue-worker path). I9 does NOT apply here — it governs MCP handlers only. See `test_wiki_mcp_handler_perf.py` for the I9 test."

### 3. Re-attribute CHANGELOG + MIGRATION_NOTES

`CHANGELOG.md` v5.41.1 entry: remove "pre-existing I9 violation in embedded mode" line. Replace: "Storage-layer latency ~89ms on embedded SurrealKV — separate concern from I9, which governs MCP handlers and is verified separately."

`MIGRATION_NOTES.md` v5.41.1 section: same correction. Also clarify: MCP `wiki_add` still meets I9 ≤5ms p50 (file enqueue only).

## Acceptance criteria

1. New `yadgar/tests/test_wiki_mcp_handler_perf.py` exists + green. Measures MCP handler directly. p50 ≤5ms verified.
2. Existing `test_update_under_5ms_p50` renamed + docstring corrected.
3. CHANGELOG v5.41.1 entry corrected (no I9-violation framing).
4. MIGRATION_NOTES v5.41.1 entry corrected.
5. Version bumped 5.41.2 → 5.41.3 (assuming v5.41.2 ships first).
6. All existing tests still pass.
7. CHANGELOG entry for v5.41.3 documents the attribution fix.

## Non-goals

- No storage-layer perf optimization (separate v5.99-class spike if anyone cares).
- No new MCP tools.
- No queue-worker refactor.
- No changes to the transactional fix itself — it's correct as shipped.

## Risks

- MCP handler perf test fails. Reveals real I9 violation. Mitigation: investigate → either (a) the test is wrong (e.g., importing heavy module at call-time triggers slow path), (b) actual handler does heavier work than expected and needs refactor. v5.41.3 ships the test green OR opens a real bug for follow-up.
- Test infrastructure for MCP handler perf: how to bypass queue but still exercise `wiki_add`. Use `unittest.mock` to patch the enqueue function with a no-op + measure handler return time. Storage layer not called in this test.

## Phases (3 commits)

1. **MCP-handler perf test** — new file, p50 ≤5ms assertion. → COMMIT `test(wiki): MCP-handler perf test for true I9 budget`
2. **Re-attribute existing test + docstring** — rename + comment fix. → COMMIT `docs(wiki): re-attribute storage perf test (not I9, queue-worker layer)`
3. **CHANGELOG + MIGRATION_NOTES corrections + version bump** → COMMIT `chore: bump version 5.41.2 → 5.41.3 + correct I9 attribution`

## References

- I9 invariant — `docs/ARCHITECTURE_INVARIANTS.md` (write-path code ≤5ms p50)
- I2 invariant — drainer is single catch-up lane (heavy work allowed there)
- v5.41.1 commit `dcf0c78` — origin of mis-attribution
- v5.41.2 plan — `wait` flag, ships before this

## Coordination

Ship AFTER v5.41.2 (wait flag). Pure docs + 1 test addition — single agent dispatch, NO isolation, sonnet, ~0.25 day.

After ship: roadmap wiki updated.
