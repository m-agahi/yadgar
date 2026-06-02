# PLAN — v5.41.2: `wiki_add` `wait` flag for read-your-writes consistency

**Status:** drafted 2026-06-02. Hotfix patch (minor UX).

**Origin:** v5.41.0 smoke-test (2026-06-02) showed expected stale read: `wiki_history(slug)` called immediately after `wiki_add` returns the pre-write version list because writes go through the async file queue. Existing doc string warns about this, but it forces every test / interactive caller to insert a `sleep(N)` — fragile.

**Why now:** small surface change. Compose with v5.41.0 versioning + v5.41.1 transactional fix. Closes the smoke-test "had to sleep 3s" friction permanently.

**Effort estimate:** 0.25-0.5 day.

**Branch:** `fix/v5.41.2-wiki-add-wait` off master (after v5.41.1 ships).

---

## Problem

```
wiki_add(...) → {"queued": true}    # returns immediately
wiki_history(slug) → 1 version       # WRONG — write not yet committed
sleep(3)
wiki_history(slug) → 2 versions      # correct
```

Doc says this is expected. It's a footgun for tests, integration scripts, and human MCP smoke checks. Race conditions also possible in normal agent workflows: agent writes then immediately queries.

## Fix

Add opt-in `wait: bool = False` parameter to:
- `wiki_add(...)` — when `True`, blocks until queue commits this specific write before returning
- `wiki_update(...)` — same
- `wiki_restore(...)` — already returns sync per agent's v5.41.0 impl; verify, add `wait` for symmetry if not
- `wiki_append_section(...)` — same; verify if already sync

Default `wait=False` preserves current async-fast behavior. Callers that need read-your-writes pass `wait=True`.

## Implementation sketch

Queue submission already returns a job ID (or can be made to). After enqueueing:

```python
if wait:
    await queue.wait_for_job(job_id, timeout=WIKI_WRITE_WAIT_TIMEOUT_SECONDS)
return {"stored": True, "queued": True if not wait else False, ...}
```

Reuse existing queue-completion semaphore / future pattern. Don't busy-poll.

## Config knob (I25)

`WIKI_WRITE_WAIT_TIMEOUT_SECONDS: float = 5.0` — max time `wait=True` blocks. Timeout returns error rather than silently dropping consistency.

## Tests

`yadgar/tests/test_wiki_add_wait.py`:

1. `test_wait_false_default_returns_immediately` — current async behavior unchanged.
2. `test_wait_true_blocks_until_committed` — wiki_add(wait=True); immediate wiki_history shows new version with no sleep.
3. `test_wait_true_on_wiki_update_sees_new_version` — same for update.
4. `test_wait_timeout_returns_error` — patch queue to never drain; assert timeout error within budget.
5. `test_wait_default_still_async_for_perf` — assert wait=False call returns in <50ms (no accidental sync).
6. `test_wait_param_composes_with_force_and_replace_slug` — v5.39 gate params still work.

## Acceptance criteria

1. `wiki_add` / `wiki_update` accept `wait: bool = False`.
2. `wiki_restore` + `wiki_append_section` accept same param (or already sync — document).
3. `wait=True` blocks until commit; `wait=False` unchanged.
4. New `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` knob registered three-way (I25).
5. All 6 new tests green; all 38 v5.41.0 + 5 v5.41.1 tests still green.
6. Version bumped 5.41.1 → 5.41.2.
7. CHANGELOG + MIGRATION_NOTES updated.
8. wiki_history docstring updated: "Use `wait=True` on the preceding write to avoid stale reads."

## Non-goals

- Don't change default behavior — async stays default for throughput.
- Don't change queue internals (no new queue tech, just expose existing job-tracking).
- Don't add wait to read tools (`wiki_history`, `wiki_read_version`, etc.) — they're DB reads, not queue-affected.
- Don't add `wait` to `memorize` or other write tools out of scope.

## Risks

- Queue API may not currently expose per-job completion handles. Mitigation: if not, smallest extension to add it; if too invasive, fall back to short-poll loop with backoff (still better than caller-side `sleep(3)`).
- Timeout window too tight under load. Mitigation: 5s default; user-tunable. Log timeouts.
- `wait=True` becomes the default in agent prompts ("be safe") → throughput regression. Mitigation: clear doc + naming.

## Phases (3 commits)

1. Queue completion handle (if missing) + tests. → COMMIT `feat(queue): expose per-job completion future`
2. `wait` param on wiki_add / wiki_update / wiki_restore / wiki_append_section + tests. → COMMIT `feat(wiki): add wait flag for read-your-writes consistency`
3. Version bump + docs + I25 knob. → COMMIT `chore: bump version 5.41.1 → 5.41.2 + docs + WIKI_WRITE_WAIT_TIMEOUT_SECONDS knob`

## References

- v5.41.0 smoke test 2026-06-02 — verification revealed the stale-read footgun
- `yadgar/queue/*` (or wherever file queue lives) — needs per-job tracking
- `wiki_history` MCP tool docstring — currently documents the issue as known limitation
- I25 invariant for config registration

## Coordination notes

Ship AFTER v5.41.1 transactional fix (currently in flight). v5.41.2 composes on top — no conflict expected.

Single agent dispatch. Sonnet. NO-isolation (v5.41.0 + v5.41.1 hit worktree-isolation stale-base bug; main-worktree dispatch worked).

After ship: roadmap wiki updated with v5.41.2 in Recently Shipped + Pipeline.
