# Car-L — Task #94 (enriched_content refresh) is already shipped

## Verdict

**No code change shipped.** The defect described in the Car-L brief —
"`memory_update` itself does not refresh `enriched_content` when
`content` changes" — is structurally fixed in the current code AND has
direct test coverage. Per ADR-0450, this car is a "lock in coverage"
car with nothing to lock in, so it ships as a single audit marker
rather than fabricating a code or test diff.

## What was investigated

- `yadgar/backend/admin_exec/memory.py:122` — `memory_update` calls
  `resync_enrichment_on_content_change(memory_id, _reembed_content, ...)`
  on a real content change (the `_reembed_content is not None` branch).
  The `_reembed_content` variable is only set when `content` is in
  `fields` AND differs from the stored content AND is truthy — so a
  same-value content patch or a metadata-only patch takes the cheap
  path and skips the resync, exactly matching the "re-embed only on
  real change" guard.

- `yadgar/_shared/storage/memory.py:122` — `resync_enrichment_on_content_change`
  runs `_derive_enrichment_resync` (the same producer `insert_memory`
  uses via `_get_enrichment_pipeline`). When the pipeline is reachable
  it writes the six fresh enrichment columns; when it is not, it
  NULLs them explicitly (stale-but-honest, per ADR-0428).

- `yadgar/_shared/storage/memory.py:93` — `_enrichment_null_clauses`
  is wired into `update_memory_fields` at line 1250, so ANY content
  patch (even one whose caller does not re-derive) nulls the six
  enrichment columns in the SAME UPDATE. This is the task-296 NULL-OUT
  floor Car-F covered (commit 31a8f597).

- `yadgar/tests/backend/test_task94_enriched_content_resync.py` —
  four tests cover the `memory_update` caller-side path end-to-end:
  content change re-enriches from NEW text, nulls stale enrichment
  when the pipeline is unreachable, nulls stale enrichment when raw
  re-embed fails, and tags-only patches leave enrichment untouched.

## What is NOT covered (intentional, per ADR-0428)

- `_phase_contradiction._handle_update` (yadgar/backend/write_exec/_memorize_phases/_phase_contradiction.py:63)
  calls `update_memory_fields(content=...)` and inherits the NULL-OUT
  floor but does not re-embed or re-derive enrichment. ADR-0428
  explicitly preserves this state — patching it would either re-derive
  enrichment while still not writing an embedding (manufacturing a NEW
  inconsistency) or duplicate the resync machinery that lives in
  `memory_update`. Filed, not fixed.

- `reembed_stale` (`yadgar/backend/sleep_compute/embed_compress.py:43`)
  replaces an enrichment-derived embedding with a raw-content vector on
  a model migration because `get_memories_needing_reembedding` filters
  only on model mismatch and nothing excludes enriched rows. Same
  nightly pass, same broken invariant, also filed in ADR-0428.

## Why this is a no-op car

ADR-0450 says: "Future train cars reading #94 in the ledger will see
'completed' and skip — preventing the duplicate-fix failure mode." The
defect Car-L was asked to fix is the same defect Car-F covered and the
same defect the task-94 resync tests cover end-to-end. A second car
fixing the same bug is the failure mode ADR-0450 explicitly rejects.

## Action items for main thread

- This branch (`car/L-memory-update-enrich-refresh-2026-08-26`) ships
  a single marker commit (this file + commit message).
- Push deferred to main thread per Car-L brief.
- Train tip will move to this commit; the next car off train tip
  reads the audit and skips any #94-themed work.

## Provenance

- Car-L brief, ledger "#363 / #94 part 2", 2026-08-26.
- ADR-0428 (car 296): update_memory_fields can carry a nulling floor
  but never enrichment re-derivation.
- ADR-0450: train cars lock in coverage of shipped fixes; they do not
  re-derive.
- Car-F (commit 31a8f597): task-296 NULL-OUT floor gets unit +
  integration coverage.
- Car 7 (PR #63, commit 1f026d25): the original resync plumbing in
  `memory_update` and `_derive_enrichment_resync`.
