# Track A — hook-recall caching (PARKED discussion)

**Status:** PARKED. The #81 freeze is already fixed by **track B** (bounded hook-recall pool, v5.92.0). Track A is a **speed** optimization only, not a fix. Revisit if the bounded-but-still-~1.5s hook recall proves too heavy in practice.

## Background
Agent-lifecycle hooks (`/hooks/prompt-recall`, `/hooks/subagent-start`, `/hooks/instructions-loaded`) inject auto-context by running `retriever.recall(profile="fast")` ≈ **1.5s** (FTS + vector-embed + KNN + fusion on ~4800 records; rerank/PPR/spreading already skipped). Track B made the *thread pool* bounded so a slow uncancellable recall can no longer starve the loop. Track A would make the recall itself cheap.

## The wrinkle (why a naive cache is wrong)
The hook recall is **query-specific** — `prompt-recall`'s query is the user's prompt. So a plain per-directory cache would serve the **previous prompt's** context for a *new* prompt → wrong injection. This is why the current code **throttles to empty** (safe: just no context) rather than caching a stale result. Any cache design must resolve this.

## Options (user's framing + analysis)

### 1. Background-refreshed, query-agnostic hot-context  ← recommended IF we want instant hooks
Don't cache the query-recall. Inject a **query-agnostic "hot context"** per directory (top-heat / most-relevant memories for the dir), **refreshed in the background** (a periodic task and/or on `memorize` for that dir). The hook reads it instantly (sub-ms), never pays the 1.5s, and it's always fresh.
- **Pro:** callers never pay the recall cost; auto-updating; no freeze risk on the hook path.
- **Con:** changes *what* the hook injects — hot-context instead of prompt-relevant recall. Acceptable for a lightweight context nudge; not if prompt-relevance is essential.
- This is the user's "shouldn't the cache auto-update" intuition — correct design.

### 2. TTL tweak (cheapest, zero correctness risk)
Raise the existing ~2-min `prompt-recall` throttle → the recall fires less often. No new code, no semantics change. But it still pays 1.5s when it does fire (just rarer).

### 3. Force-trigger / invalidate-on-write
Refresh the cached context when a `memorize` lands in that directory. Not standalone — it's *how* #1's background refresh gets triggered (event-driven instead of/with periodic).

## Recommendation
Track B fixed the actual problem (freeze). For the user's sequential use, 1–2 bounded background recalls are harmless. So:
- If "instant hooks" is wanted → **#1 (background hot-context)**, with **#3** as its refresh trigger.
- Otherwise → leave it, or do **#2** (raise the throttle) as a one-line nicety.

## Revisit trigger
The bounded hook recall (max 2 threads × ~1.5s) becomes a measured bottleneck under real load, OR hooks feel slow in normal use.
