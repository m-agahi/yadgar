# PLAN — v5.31.1: Deferred patches bundle (hotfix)

**Status:** drafted 2026-06-01. Hotfix bundle. Surfaced from v5.29.0 + v5.31.0 deferred items.

**Why now:** both items deferred during their parent releases, log noted. Slotting before v5.33.0 keeps the deferred-list short + avoids debt accumulation.

**Effort estimate:** 0.5-1 calendar day.

**Branch:** `fix/v5.31.1-deferred-patches` off master.

---

## Items

### Item 1 — `get_full_graph` invalidated-edge filter bugs (v5.29 origin)

Two tests fail on master (pre-existed v5.29; surfaced during v5.29 verification):

```
yadgar/tests/test_bitemporal_edges.py::TestGetFullGraphFiltering::test_invalidated_causal_edge_excluded_by_default
yadgar/tests/test_bitemporal_edges.py::TestGetFullGraphIncludeInvalidated::test_include_invalidated_returns_all
```

Investigation needed:
- `yadgar/graph_api.py::get_full_graph()` — does it correctly propagate `include_invalidated=False` to `causal_dag_edge` reads?
- Suspected: v5.29 `as_of_filter` integration changed semantics of one branch but missed the other. Confirm via running tests first + reading the failure output.
- v5.29 introduced `get_all_causal_edges(as_of=)` — verify `include_invalidated=False` still filters when `as_of=None`.

Acceptance:
- Both tests pass.
- No regression in `test_bitemporal_extension.py` (22 tests) or other graph tests.
- Root cause documented in commit message (1-line).

### Item 2 — MCP `recall()` tool exposes pipeline kwargs (v5.31 origin)

`yadgar/server/tools/recall.py` still routes through monolithic `recall()`. MCP callers cannot use v5.31 plugin pipeline via `profile=` / `stage_overrides=` even though `Retriever.recall_via_pipeline()` exists.

Changes:
- Add optional `profile: str | None = None` parameter to MCP tool signature. Values: `fast` / `balanced` / `full` / `debug` (validate against `yadgar/retrieval/profiles.py`).
- Add optional `stage_overrides: dict[str, dict] | None = None` parameter. Schema: `{"stage_name": {"enabled": bool, ...}}` — passed through to `recall_via_pipeline()`.
- When `profile=None` (default): preserve current monolithic `recall()` path — zero behavior change for existing callers.
- When `profile` provided: route through `recall_via_pipeline(profile=, stage_overrides=)`.

Update MCP tool description string to document the new kwargs.

Acceptance:
- New tests in `test_retrieval_pipeline.py` (or new file) cover: `profile=None` calls legacy path, `profile="balanced"` produces bit-identical output to legacy, invalid profile raises validation error.
- Existing recall callers unaffected (regression test on at least 2 existing callers — `restore`, `auto-capture hook`).
- Pipeline metrics (4 prometheus from v5.31) emit when `profile` set.

---

## Test plan

- Item 1 first (red → green). Then Item 2.
- Run full `yadgar/tests/test_bitemporal*.py` + `test_retrieval_pipeline.py` + `test_recall*.py` after each item.
- Pre-existing failures on master (not from v5.29/v5.31): surface, don't silence.

## Acceptance criteria

1. 2 graph filter tests green.
2. MCP `recall()` accepts `profile=` and `stage_overrides=`.
3. Backward compat: zero-arg + content-only callers unchanged.
4. Pipeline metrics emit when `profile` set.
5. Version bumped 5.31.0 → 5.31.1 (pyproject + server.json + docker-compose + uv.lock).
6. CHANGELOG + MIGRATION_NOTES updated.
7. All pre-existing tests still pass (no regression).

## Non-goals

- No new pipeline stages.
- No new profile types.
- No refactor of monolithic `recall()` (defer to v5.33+).
- No changes to `consolidate_now` / write-path.

## Risks

- Item 1: may turn out test was always wrong (assertion mismatch). If so, fix the test, not the code. Document in commit.
- Item 2: pipeline signature drift — `recall_via_pipeline` kwargs may have changed since v5.31.0. Re-read before signature lift.

## Coordination notes

Single agent dispatch. Worktree-isolated. Sonnet. 0.5-1d.

Pre-existing failures NOT in scope:
- Other test failures on master should be surfaced + logged as v5.33.x or later patches, NOT fixed here.

After ship: roadmap wiki "Follow-ups logged" gets both items moved from open → closed with v5.31.1 SHA.
