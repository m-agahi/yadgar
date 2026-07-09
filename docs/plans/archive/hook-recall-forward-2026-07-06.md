> ARCHIVED 2026-07-09 — SHIPPED as #166 (31169f4f): hook sites forward via tools.recall._forward_to_backend; ADR-0054 records the §5.4 reversal.

# Plan: Wire the prompt-recall HOOKS through the forward-to-backend recall path

**Status:** BUILD — implement to this spec (decided, TDD).
**Date:** 2026-07-06
**Branch:** `feat/wire-recall-caches` (stacked on `e0959a98`, the metrics-visibility fix; backend already 5.23.0).
**Scope:** Route the three in-core prompt-recall HOOK sites in `yadgar/server/http.py` through the SAME
forward-to-backend mechanism the MCP `recall` tool uses (`recall.py::_forward_to_backend`), instead of the
in-core `retriever.recall(profile="fast")` monolithic path. Preserve profile=fast, max_results, min_heat, the
per-caller directory filter, the tight `HOOK_RECALL_TIMEOUT_S` timeout, and graceful degradation
(return `[]` / `{"text": ""}` on backend timeout/error — never block the prompt path).
**Related:** `recall-forward-only-2026-07-05.md` (§5.4 — the disposition this plan **reverses**, see §0);
`backend-caching-train-2026-07-06.md` / `caching-train-build-2026-07-05.md` (the cache train that is the new
trigger); memory 531869 (orphan map of in-core `Retriever.recall(profile=fast)` callers).

---

## 0. §5.4 REVERSAL — read this first (the explicit conflict)

`recall-forward-only-2026-07-05.md` §5.4 dispositioned the prompt-recall hook as **"the ONE accepted
core-resident exception"** to forward-only, and the live code at `http.py:907-914` encodes that decision in a
comment: *"Forwarding would add a real TCP hop to YADGAR_EMBED_URL which fights the 2.0s latency budget …
keep these three hook sites calling `_st._retriever.recall(profile="fast")` directly. Revisit if backend TCP
p99 is measured < 500ms on production box."*

**This plan is that revisit.** It is not a violation of §5.4 — §5.4 explicitly made the choice
*measurement-gated* (the §7 hook-latency benchmark) and left the door open ("Revisit if …"). Two facts changed
since 2026-07-05:

1. **New trigger — the cache train (this branch).** `e0959a98` wired + made visible the backend
   `memory_doc` / `engram_slot` / `graph` data caches on the forward-only backend `/recall` pipeline.
   Forwarding the hook recalls lets those backend caches serve hook recalls too — a benefit that did not
   exist when §5.4 deferred.
2. **The latency argument was always weaker than it read.** Core has NO ML models loaded (thin, ~203MB,
   uses `RemoteMLClient` for embed). So the in-core hook recall **already** round-trips to the backend for
   the embed, then queries the DB several times from core. Measured: hook recall ~700–1000 ms; a localhost
   round-trip is ~9 ms (~1 % of hook latency). Forwarding **consolidates** the several core→backend/DB hops
   into ONE backend call (embed + DB + build_results co-located) and should *lower* total latency, not raise
   it.

**Honesty caveat (do not overclaim the 9 ms):** 9 ms is *uncontended* localhost. The real safety is NOT the
9 ms — it is the **timeout + graceful degradation** (below). We ship the forward because the direction is
sound (one hop replaces several) and the timeout makes the worst case safe, NOT because 9 ms is a correctness
proof.

**Action for the impl:** update the stale comment at `http.py:907-914` to describe the new (forward) decision
and cite this plan + the cache-train trigger. Leaving the old comment is a landmine.

---

## 1. The forward mechanism to REUSE (do not duplicate)

`yadgar/server/tools/recall.py::_forward_to_backend` (`recall.py:44`) — the exact mechanism the MCP `recall`
tool uses (`recall.py:260`). It:

- reads `YADGAR_EMBED_URL` (same base URL `RemoteMLClient` uses for `/rerank`), appends `/recall`,
- `POST`s a JSON payload `{query, directory, current_branch, default_branch, max_results, min_heat, type,
  tags, mode, profile}` with the `YADGAR_MCP_AUTH_TOKEN` bearer header,
- raises `RuntimeError` if `YADGAR_EMBED_URL` is unset, or `httpx.HTTPError` on backend failure,
- returns `data["results"]` (a `list[dict]`).

The backend side (`embed_service.py::recall_route`, `:1023`) runs `_fanout_recall` with the passed
`directory` / `current_branch` / `default_branch` / `profile`, applies the backend DB-side side-effects, and
returns `RecallResponse(results=[...])`. Each result dict carries `directory_context` (the fanout providers
apply `is_directory_eligible` server-side and the rows retain their `directory_context`).

**Reuse `_forward_to_backend`** — one change to it (see §2, trap 1): parametrize the httpx timeout so hook
callers can pass a short one. MCP recall keeps its 120 s.

---

## 2. Design — how the hook forward works, and the three traps

### The wiring point

Keep `_recall_with_timeout` (`http.py:78`) as the guard boundary: it already provides the two properties we
must not lose — (a) the **bounded 1-worker hook-recall pool** (#81 freeze fix), and (b) the
`asyncio.wait_for(timeout=HOOK_RECALL_TIMEOUT_S)` returning `None` on timeout. We only swap the **callable it
runs** from `retriever.recall` to a forward function.

Introduce `_forward_hook_recall(query, *, max_results, min_heat, directory, profile) -> list[dict]` in
`http.py` (thin, hook-specific). It:

1. resolves `current_branch` / `default_branch` from the caller `directory` via `_detect_branch` /
   `_get_default_branch` (mirroring `recall.py:207-233`); on any failure → `None`/`None` (backend tolerates),
2. calls `_forward_to_backend(query=…, max_results=…, min_heat=…, directory=…, current_branch=…,
   default_branch=…, type_filter="all", tags=None, mode=None, profile=profile, timeout_s=<short>)`,
3. returns the `list[dict]`.

The three hook handlers keep calling `_recall_with_timeout(...)` but now pass the forward callable + its
kwargs. `_recall_with_timeout` runs it on the bounded pool under wait_for. On timeout → `None` (existing
handling: `{"text": ""}`). The handlers' existing `except Exception` blocks catch `RuntimeError` /
`httpx.HTTPError` and return `{"text": ""}` — **graceful degradation preserved**.

### Trap 1 — httpx timeout must be SHORT on the hook path (else #81 starvation returns)

`_forward_to_backend` hardcodes `httpx.post(timeout=120.0)`. Dropped into the 1-worker bounded pool, a hung
backend would keep the httpx thread alive up to 120 s while `wait_for(2.0s)` already returned `None` — the
exact uncancellable-thread pile-up the pool cap exists to bound (#81). **Fix:** add a `timeout_s: float`
parameter to `_forward_to_backend` (default 120.0 — MCP recall unchanged); the hook forward passes
`timeout_s = HOOK_RECALL_TIMEOUT_S` (2.0). So even if `wait_for` fires first, the httpx thread dies ~at the
same budget, not 120 s later. (A fully-cancellable async-httpx path would kill the leak outright but is a
larger change than "reuse it" — short timeout is the minimal-safe move; note as a follow-up.)

### Trap 2 — directory-filter quality-neutrality (the subtle one)

Today: hook calls **unscoped** `retriever.recall(...)` (no directory arg) → `_filter_prompt_recall_results`
post-filters via `is_directory_eligible(r["directory_context"], caller_dir)` (`http.py:823-844`).

Forwarded: `_forward_to_backend` **requires** `directory`; the backend fanout scopes **server-side** with the
SAME predicate `is_directory_eligible` (via `Scope`, `_recall_pipeline.py:338`), and result rows retain
`directory_context`. So:

- **Keep `_filter_prompt_recall_results` on the forwarded results** (prompt-recall handler, `http.py:937`).
  Because the backend already scoped by the same predicate, the post-filter is **idempotent** — it drops
  nothing more (every returned row is already directory-eligible). This preserves the exact contract:
  results the hook emits are directory-eligible for the caller dir. The other two hooks
  (instructions-loaded, subagent-start) do NOT post-filter today and keep not post-filtering (unchanged).
- **Verified against backend:** `embed_service.py:1019` (landscape) and the fanout providers
  (`_recall_pipeline.py:300-301`) both apply `is_directory_eligible`; rows keep `directory_context`.
  So the post-filter reads a present field and matches. No silent drop/mis-keep.

**Behavior change to NAME (not hide):** the forwarded path runs the **memory+wiki fanout** pipeline, whereas
the in-core hook ran the **monolithic `Retriever.recall(profile="fast")`**. These are different pipelines.
"Quality-neutral" here means **the forwarded hook returns exactly what the backend `/recall` pipeline returns
for the same args** — i.e. the hook now shares the MCP-recall pipeline, which is the intended consolidation.
It does NOT mean byte-identical to the old monolithic hook output. This is the same honest shift §3.1/§5.4 of
the forward-only plan already flagged for `profile=fast` callers (memory-only → memory+wiki). LongMemEval is
the quality backstop for the pipeline itself; the hook simply now uses that same, gated pipeline.

### Trap 3 — the stale comment

Flip `http.py:907-914` to describe the forward decision + cite this plan and the cache-train trigger.

### Trap 4 (BLOCKER — narrows scope) — the backend REQUIRES a directory; two hooks lack one

`RecallRequest.directory: str` on the backend is a **required, non-null field** (`extra="forbid"`,
`embed_service.py:970`). `_forward_to_backend` sends whatever `directory` it is given. Consequence per hook:

- **prompt-recall (`http.py:919`) — FORWARD.** The deployed hook script
  (`yadgar/hooks/prompt-recall.py:207,217`) ALWAYS passes `?directory=` (`directory = data.get("cwd","") or
  os.getcwd()`, urlencoded into the request). So prompt-recall always has a real directory to scope with.
  Backend scopes server-side (same `is_directory_eligible`), rows keep `directory_context`, post-filter is
  idempotent → **quality-neutral.** This is the one hook this plan forwards.
- **instructions-loaded (`http.py:1525`) — KEEP IN-CORE (documented exception).** It has NO directory
  (only `file_path` / `load_reason`; its shadow-observe passes `directory=None`). Today it runs **whole-DB**
  (unscoped, no post-filter). Forwarding would send an empty/None directory → backend scopes to nothing →
  **always-empty results = straight regression.** This is precisely "a hook path that needs core-local data
  the backend lacks." Leave it calling `retriever.recall(profile="fast")` in-core; document as the accepted
  core-resident exception.
- **subagent-start (`http.py:1654`) — KEEP IN-CORE (out of task scope).** The task named only sites
  `~103/911`. subagent-start *has* a `cwd`, but today runs **whole-DB unscoped** (no post-filter); forwarding
  would change it to directory-scoped — **not quality-neutral** (a behavior change, arguably more correct but
  not neutral). Out of scope for this plan; a defensible future candidate (forward with `cwd`, accept the
  scoping change) but NOT shipped here to keep the change minimal and neutral.

**Net scope:** forward **prompt-recall only**. instructions-loaded + subagent-start stay in-core. This matches
the task's literal site list (`~103/911`, not 1525/1654) and avoids the always-empty regression.

### What we DO NOT change

- The bounded 1-worker pool + wait_for guard (`_recall_with_timeout`) — kept verbatim.
- `HOOK_RECALL_TIMEOUT_S` (2.0) — kept; now ALSO caps the httpx timeout.
- The `retriever` singleton / plumbing — other non-recall call sites still need `_st._retriever`; only the
  hook's recall EXECUTION moves to the backend. The handlers keep the `retriever is None → {"text": ""}`
  short-circuit (if there is no retriever the daemon is not ready; degrade).
- The shadow-observe instrumentation blocks — unchanged.
- Reinjection on write (`_phase_post_write.py:190`, `REINJECT_ON_WRITE=False` default) — LOWER priority,
  out of scope unless trivially clean. Not touched in this plan.
- The viz graph endpoint (`http.py:2189`, admin-gated, whole-DB by design, BC-VZ2) — NOT a prompt-path hook;
  leave in-core (it needs whole-DB, un-directory-scoped, and is not latency-budgeted). Out of scope.

---

## 3. CONCURRENCY CAVEAT (document, do NOT fix now)

Forwarded hook recalls share the backend recall semaphore with MCP recalls. Under frequent-hook load
(prompt-recall + subagent-start bursts) this creates contention on the backend recall path that the in-core
hook path did not have. **Ship the forward first.** Follow-up if contention shows in prod
(`yadgar_event_loop_lag_max`, backend recall p99): a fast-profile fast-lane — a separate / higher-limit
semaphore for `profile="fast"` recalls so hooks do not queue behind heavy MCP recalls. Not built here.

---

## 4. Sites changed

| Site | Handler | Disposition |
|---|---|---|
| `http.py:919` | `hook_prompt_recall` | **FORWARD** — `max_results=5, min_heat=0.0, profile="fast"`, directory filter kept |
| `http.py:1525` | `hook_instructions_loaded` | KEEP IN-CORE (no directory → forwarding = always-empty regression; documented exception) |
| `http.py:1654` | `hook_subagent_start` | KEEP IN-CORE (out of task scope; forwarding would change whole-DB→scoped, not neutral) |

- `_forward_hook_recall(...)` added to `http.py` (resolves branch, calls `_forward_to_backend` with short timeout).
- The prompt-recall handler passes `_forward_hook_recall` as the callable into `_recall_with_timeout` (bounded
  pool + wait_for guard reused verbatim).
- `_forward_to_backend` (`recall.py:44`) gains a `timeout_s: float = 120.0` param; the hook passes
  `HOOK_RECALL_TIMEOUT_S`.
- The other two hooks are untouched (still call `retriever.recall`).

---

## 5. TDD test plan (failing first → green)

New file `yadgar/tests/test_hook_recall_forward.py`. Targeted only (timeout-wrapped run), NOT full suite.
Plus a deliberate update to `test_hook_latency_budget.py::TestHookCoreResidentDecision` (see below).

1. **prompt-recall forwards to backend with the right args.** Mock `_forward_hook_recall` (or
   `_forward_to_backend`); fire `hook_prompt_recall`; assert the forward is called with `profile="fast"`,
   `max_results=5`, `min_heat=0.0`, `type_filter="all"`, and the caller `directory`.
2. **`_forward_hook_recall` passes the SHORT timeout to `_forward_to_backend`.** Assert `timeout_s ==
   HOOK_RECALL_TIMEOUT_S` (trap 1 regression guard), NOT 120.
3. **Directory filter still applied to forwarded results.** Feed forwarded rows with mixed
   `directory_context`; assert `_filter_prompt_recall_results` keeps only eligible ones (idempotent on
   already-scoped rows).
4. **Graceful degradation — backend raises.** Mock the forward to raise `RuntimeError` / `httpx.HTTPError`;
   assert `hook_prompt_recall` returns `{"text": ""}` and does NOT raise.
5. **Graceful degradation — backend times out.** Slow forward > `HOOK_RECALL_TIMEOUT_S`; assert
   `_recall_with_timeout` returns `None` → handler returns `{"text": ""}`.
6. **Quality-neutral.** `hook_prompt_recall` emits the mocked backend rows unchanged (modulo the directory
   filter), proving the hook adds no divergent processing on top of the backend pipeline.
7. **MCP recall timeout default preserved.** `_forward_to_backend` default `timeout_s == 120.0` (MCP recall
   path unchanged).
8. **instructions-loaded + subagent-start stay in-core.** Assert those two handlers still drive
   `retriever.recall` via `_recall_with_timeout` (unchanged) — guards the scope decision.

**Deliberate #52-authorized test flip:** `test_hook_latency_budget.py::TestHookCoreResidentDecision` encodes
the OLD §5.4 core-resident decision for prompt-recall. Its own docstring authorizes the pivot ("If this test
breaks because a hook no longer calls retriever.recall... replace this test with a forward-path assertion").
Rename the prompt-recall case to a forward-path assertion (assert it forwards, not that it calls
`retriever.recall`); KEEP the instructions-loaded + subagent-start `profile=="fast"` core-resident assertions
(those stay in-core). This is an authorized substitution, not #52 weakening — cite it in the commit.

Verification gates: targeted green; `check_versions.py` / I33 invariant exit 0; three-way config sync
(`test_config_three_way_sync.py`) only if a config knob is touched (none planned); #52 guard. NOT full suite.
Guards run WITHOUT `cd` into the worktree (absolute paths).

---

## 6. Version bumps

`http.py` is CORE → bump **core 5.112.0 → 5.113.0**: `pyproject.toml:7`, `server.json` (`version` +
`packages[0].version`), then run `scripts/sync_version.py` to propagate flake.nix + docker-compose.yml, and
confirm `scripts/check_versions.py` (canonical floor) exits 0. Backend stays **5.23.0** (already bumped by the
metrics fix `e0959a98` on this branch; `yadgar/__init__.py::BACKEND_VERSION` + `server.json:backend_version`).

---

## 7. Live verification plan (after build + deploy)

1. Fire a prompt-hook recall (submit a prompt in a project dir with a running daemon).
2. Confirm it hits the **backend** `/recall` path: `yadgar_recall_*` / `backend.recall` observe series bump
   on `:8001/metrics`; core no longer runs the in-core monolithic pipeline for hooks (core recall pipeline
   observe stays flat for hook traffic).
3. Confirm the backend **data caches move on hook traffic**: `yadgar_cache_{hit,miss}_total{cache=memory_doc|
   engram_slot|graph}` on `:8001` increment on hook recalls (they already do for MCP recalls post-`e0959a98`).
4. Confirm degradation: with the backend stopped, a prompt hook returns empty context (`{"text": ""}`) within
   the 2.0 s budget and does NOT block the prompt or raise.

**Flags for the PR reviewer:**
- Only **prompt-recall** forwards; instructions-loaded + subagent-start stay in-core (Trap 4). If we later
  want those forwarded, instructions-loaded needs a directory source first (else always-empty), and
  subagent-start's whole-DB→scoped change must be accepted.
- The forwarded prompt-recall hook now uses the **memory+wiki fanout**, not the monolithic
  `Retriever.recall`. If any consumer depended on monolithic-only ranking/boosts (C4 branch boost /
  postmortem boost, which the fanout does NOT apply per forward-only §5.2), hook results re-rank. Intended
  consolidation (hook shares the MCP pipeline), LongMemEval-gated at the pipeline level — name it in the PR.
