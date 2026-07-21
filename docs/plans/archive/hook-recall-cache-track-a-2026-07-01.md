# Track A — hook-recall caching (PARKED discussion)

**Status:** DROPPED / defer indefinitely — pool=2 stable (ADR-0025/0077), naive per-dir cache design flawed (ADR-0023)

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

---

## UPDATE 2026-07-01 — UN-PARKED (revisit trigger fired: residual lag confirmed live)

**Evidence (v5.94.0 deploy).** On the first agent spawn after the freeze fix went live, the #80 obs gauge caught `yadgar_event_loop_lag_max_seconds = 16.95` with a ~33s health-probe gap coincident with the two `subagent-start` hooks (both timed out cleanly at 2s). The daemon **survived** (no SIGKILL — track B's primary win holds) but the loop still stalled ~17s.

**Mechanism (refined — box-saturation, not the pool alone).** The core is `--cpus 1`. Under concurrent box load (a worktree profiler agent hammering the box), the OS starves the core's single CPU: the recall thread runs slow (→ 2s wait_for timeout) AND the event-loop probe is delayed (→ 17s lag). Root cause = **box-saturation of the 1-CPU core**, not purely the leaked-thread GIL. Bounding the pool reduces the *number of threads competing for that 1 CPU* but cannot remove the recall's CPU demand.

**Shipped mitigation (v5.95, this PR).** `_HOOK_RECALL_POOL_WORKERS` 2 → 1 — halves the loop-vs-thread CPU competition on the 1-CPU core, strictly freeze-safer (fewer leaked threads), hooks serialize under a burst but each keeps its own 2s wait_for. Mitigation, not elimination.

**Track A is now the elimination.** Removing the hot-path recall means the hook does ~no work during a box-saturation spike → the lag vanishes regardless of CPU contention.

### Buildable design (option 1, recommended) — background-refreshed hot-context
- **Read path (hook, hot):** `/hooks/{prompt-recall,subagent-start,instructions-loaded}` serve a per-directory **precomputed hot-context** blob from an in-memory cache (sub-ms; no `recall()`, no thread, no pool). Fail-open to empty on miss.
- **Write path (background, cold):** a refresh populates the cache off the hot path — (a) on a periodic tick (e.g. per consolidation cycle), and (b) invalidate-on-`memorize` for that directory (#3). The refresh itself may call the real retriever, but OFF the hook path, so its cost never touches loop latency.
- **Content:** query-AGNOSTIC top-heat memories for the directory (like `project_brief` hot_memories) — NOT the prompt-relevant recall. This is the load-bearing semantic decision (below).
- **Freeze-safety:** structurally immune (the freeze vector is a hot-path semantic `recall()`; this design has none). Consistent with ADR-0022.

### THE decision to confirm before building Track A code
The hook currently injects **prompt-relevant** recall (query = the user's prompt). Track A changes it to **query-agnostic hot-context** (top-heat memories for the dir, prompt-independent). Trade: instant + freeze-proof, but the injected context is generic "what's hot here" rather than "relevant to this prompt." Acceptable for a lightweight session-context nudge? If prompt-relevance is essential, the fallback is option 2 (raise the throttle) — keeps prompt-relevance, stays slow-but-rare.

### Status
- pool=1 mitigation: BUILT (v5.95, fix/hook-lag-residual).
- Track A hot-context: DESIGN READY, build gated on the semantic decision above.
- Root (box-saturation of the 1-CPU core): ties to CI/profiler capping (#83/#84) and is out of scope for "no resource increase."
