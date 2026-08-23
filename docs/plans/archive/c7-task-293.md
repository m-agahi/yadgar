# C7 — task 293: `make longmemeval` reports 0.000 — absence of data read as a score

## State going in

Task 293 in the ledger: `make longmemeval broken since the identity train:
run_longmemeval.py never stamps project_id, so insert_memory raises
UnresolvedProjectError and it reports 0.000 — absence of data read as a score`.

The failure surface is invisible: the benchmark prints a clean summary table
with zeros, the operator pastes the 0.000 into a comparison doc, and the next
run also reports 0.000 — there is no exception, no traceback, no "0 of 500
questions ingested" line. The run is silent, fast, and wrong.

## What the bug actually is

`benchmarks/run_longmemeval.py:507-518` (now ~533 with the fix) builds the
insert-memory payload like this:

```python
memory_id = storage.insert_memory(
    {
        "content": content,
        "embedding": embedding,
        "tags": tags,
        "directory_context": BENCHMARK_DIRECTORY,   # <-- only legacy key
        "heat": 1.0,
        ...
    },
    ...
)
```

`storage.insert_memory` (yadgar/_shared/storage/memory.py:455) builds the row
dict and routes the project_id resolution through
`_resolve_project_id_for_write` (yadgar/_shared/storage/_project_id_writer.py).
That helper's contract after C5 (ADR-0227) is "the caller's value, or a raise
— there is no second branch". The directory-context fallback that used to
carry the resolution is gone. With no `project_id` on the payload, the
helper raises `UnresolvedProjectError`.

The benchmark's outer loop catches that exception inside `except Exception as
_qerr` (line 1126), records the question as an error, and moves on. After
500 iterations the aggregate is `overall: {count: 500, recall@5: 0, ...}`,
and the metric printout is indistinguishable from "the model genuinely
retrieved nothing". A user looking at the JSON output sees
`"errors": [{"UnresolvedProjectError": ...}]` buried under `per_query[]` —
not at the top of the file.

## Why the benchmark needs a stable, fixed identity

The haystack corpus is **benchmark-shaped, not project-scoped**: every
caller compares against the same fixed dataset (xiaowu0162/longmemeval-cleaned).
No real project owns the corpus, so the "what project does this belong to"
question has no project-shaped answer.

Three identity candidates, all rejected:

1. **Per-run UUID**: scatters the corpus across identity-named shards;
   breaks the `mid_to_session` reverse map (session_map is rebuilt per
   question, but cross-run comparisons need the same identity per
   question).
3. **Resolved from CWD via `mint_project_id(cwd)`**: the benchmark is
   host-side, but the bench dir is `/tmp/yadgar_bench_q_<rand>` —
   `mint_project_id` cannot resolve an unresolvable tree.
4. **Hardcoded identity `benchmark/longmemeval`**: matches the shape
   the `global` tag uses — owns nothing, available to every project,
   stable across runs. The right answer for "benchmark-shaped" data.

## What the fix is

1. **`BENCHMARK_PROJECT_ID = "benchmark/longmemeval"`** added next to
   `BENCHMARK_DIRECTORY` at the top of the file. Single source of truth.
2. **Include `"project_id": BENCHMARK_PROJECT_ID`** in the payload dict.
   `directory_context` stays — ADR-0233 keeps it so project_backfill can
   derive project_id FROM it on the restamp half of task 310. The fix adds
   project_id; it does not rename columns.

## Files touched

| File | Edit |
|---|---|
| `benchmarks/run_longmemeval.py` | Add `BENCHMARK_PROJECT_ID` constant; add `project_id` to ingest payload |
| `yadgar/tests/core/test_longmemeval_enrich_wiring.py` | Pin: payload carries project_id; identity is the constant; directory_context unchanged |

## Pins (test plan)

- `test_ingest_payload_carries_project_id` — `payload["project_id"]` is
  truthy.
- `test_ingest_payload_uses_benchmark_identity` — payload id is the
  `BENCHMARK_PROJECT_ID` constant.
- `test_ingest_payload_directory_context_unchanged` — `payload["directory_context"]`
  still equals `BENCHMARK_DIRECTORY` (no regression on the 310 half).

## Acceptance

- All 6 `test_longmemeval_enrich_wiring.py` tests pass.
- The benchmark now produces non-zero recall metrics when run against a real
  corpus (manually verified after deploy — the test suite is hermetic).
- Task 293 closed in harness + yadgar ledger.

## What this car is NOT

- Not a per-question identity decision. The benchmark corpus is global.
- Not a fix to `insert_memory`'s raise behavior. C5/ADR-0227 is the
  correct contract — the caller must supply the identity.
- Not a fix to the silent error swallowing in the benchmark loop.
  Out of scope; belongs in a separate "benchmark error surface" car
  (operator-facing visibility — surface the error count at the top of
  the JSON output, not buried in per_query[].errors).
- Not a fix to task 310 (restamp half). Out of scope.

## Follow-ups (out of scope for this car)

- The benchmark's `except Exception as _qerr` block silently records
  errors. A future car should aggregate the per-question error count
  to the top of the JSON output so an operator sees "450/500 errors:
  UnresolvedProjectError" at a glance, not buried 500 lines deep.
- `benchmark/longmemeval` is not in `_project_registry` either. The
  C5 contract is "writes must carry an identity" — the registry check
  (`assert_project_registered_for_create`) is the OTHER gate. The
  benchmark identity may need to be seeded there; flag for the next
  identity-train pass.
