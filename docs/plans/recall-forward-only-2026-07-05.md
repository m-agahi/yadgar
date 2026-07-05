# Plan: Forward-only recall — core becomes a pure forwarder, one unified backend pipeline

**Status:** DRAFT — planning only. No code in this doc. Supersedes the dual-path/measure-first framing of `recall-pipeline-to-backend-2026-07-04.md` for the *decision* (forward is now the committed default), while inheriting its audit facts.
**Date:** 2026-07-05
**Scope:** Make the backend `POST /recall` endpoint serve **all** recall variants (fanout, landscape, profile/fast, type filters), rip every recall-compute branch out of core, remove the `RECALL_BACKEND_ENABLED` flag, kill the standalone CLI recall/replay, and deprecate/drop the stdio transport so HTTP always has a backend to forward to.
**Related:** `recall-3-train-overhaul-2026-07-04.md` (Train 1 = the backend move, SHIPPED behind the flag), `recall-pipeline-to-backend-2026-07-04.md` (§ Statefulness+Fanout audit, GO-WITH-CAVEATS), `/tmp/recall_core_paths_audit.md` (full core-path inventory), memory id 530802 (consensus-as-non-default-mode decision, 2026-06-18, decided WITH the user).

---

## 1. Context and decisions (confirmed with the user)

Train 1 already landed the machinery: core `_forward_to_backend()` (`recall.py:99`), the dual-path gate `RECALL_BACKEND_ENABLED` (default **False**, `config.py:360`, checked `recall.py:387`), the backend endpoint `POST /recall` (`embed_service.py:943`), and the side-effect split (`_apply_recall_db_side_effects` backend half + `_apply_recall_session_side_effects` core half, `_recall_pipeline.py:360/402`). #44 (backend bootstrap 500 — offload guard on `init_engines(local_engines=True)`) is fixed on `feat/recall-backend-forward-only`.

The user has made the following **committed decisions** (this plan encodes them; it does not relitigate them):

1. **Transport = streamable-HTTP.** Deployed transport is streamable-HTTP (`entrypoint.sh:41` → `exec yadgar --transport streamable-http`; Docker core+backend). stdio (`__main__.py:8`, default in `main(transport="stdio")` `lifecycle.py:804`) is a Docker-unavailable fallback the user does **not** want ("not the purpose of yadgar"). **DECISION: drop/deprecate stdio.** Consequence owned explicitly (§6): with stdio gone, HTTP always has a backend → forward-only is clean, no "no-backend" branch to preserve.
2. **Remove `RECALL_BACKEND_ENABLED` entirely.** Forward is the DEFAULT and ONLY path. No toggle, no deploy selector.
3. **Delete the in-core fanout fallback** (B4) — the local `_fanout_recall` invocation in `recall()`.
4. **Kill the standalone CLI recall/replay** compute island (`cli/_shared.py:25` `init_replay_lightweight` → `Retriever`).
5. **THE CRUX:** make **landscape** and **profile** *parameters* of the unified backend pipeline instead of separate core dispatch branches (§3).
6. **No runtime fallback.** TESTS carry all safety — no silent in-core fallback on backend error; a backend failure is a **loud error**, not a degraded local recall.

**The reframe (observed-state-wins).** "Move recall to backend" is done. The forward-only ask is about **removing the OTHER branches** (landscape, profile, legacy, bare-FTS fallback, the local-fanout fallback) and the flag, then widening `/recall` so the backend serves every variant. What stays core-resident is irreducible and small: the MCP surface + arg validation, and the **session side-effects** (SR map, `_last_recalled_ids`, action buffer, replay counter) which run on the *returned* results and need core-process singletons. Core becomes **thin**, not **empty** — and that is honest and fine.

---

## 2. The dispatch tree today (what forward-only deletes)

MCP `recall()` (`recall.py:177`) dispatches by three orthogonal gates (`mode`, `profile`, `UNIFIED_RECALL_ENABLED`+`RECALL_BACKEND_ENABLED`):

| # | Branch | Guard | Routes to | Forward-only fate |
|---|---|---|---|---|
| B1 | Landscape | `mode=="landscape"` (recall.py:288) | `_landscape_recall` → `AstrocytePool.consensus_retrieve` (astrocyte_pool.py:267) | **Parameterize** → backend serves `mode=landscape` via a distinct backend function (§3.2). Core branch deleted. |
| B2 | Profile pipeline | `profile is not None` (recall.py:428) | `recall_via_pipeline` → v5.31 plugin pipeline | **Parameterize** → `rerank_level` param on the unified fanout (§3.1). Core branch deleted. |
| B3 | Fanout → backend | `UNIFIED` + `profile is None` + `RECALL_BACKEND_ENABLED` (recall.py:387) | `_forward_to_backend` → `POST /recall` | **Becomes the only path.** Flag removed; forward unconditional. |
| B4 | Fanout → in-core | same, flag OFF or forward failed | `_fanout_recall` in core (recall.py:410) | **DELETED.** No local fallback. |
| B5 | Legacy monolithic | `UNIFIED_RECALL_ENABLED=False` | `Retriever.recall` (core.py:484) | **DELETED** from core dispatch. Code stays in the package (backend runs it via the fanout memory arm). |
| B6 | Bare FTS+vector | `retriever is None` (recall.py:453) | inline FTS+vector (recall.py:456-478) | **DELETED.** "retriever is None" becomes "backend unreachable → error", not local FTS. |

Plus three **non-MCP in-core callers of `Retriever.recall`** Train 1 never touched (the orphans, §5.4):
- Prompt-recall hook — `http.py:912-919` calls `retriever.recall(query, max_results=5, min_heat=0.0, profile="fast")` via `_recall_with_timeout` (http.py:78-106), timeout `HOOK_RECALL_TIMEOUT_S=2.0` (config.py:377). Also at `http.py:1524` (instructions-loaded hook) and `http.py:1653` (subagent-start hook). **Corrected premise:** the hook *does* pass `profile="fast"` — to the **monolithic** `Retriever.recall(profile=)` (core.py:491), NOT `recall_via_pipeline`.
- Memorize reinjection — `_phase_post_write.py:181-189` calls `_st._retriever.recall(content[:300], ...)` (no profile → monolithic default), gated `REINJECT_ON_WRITE=False` default (config.py:718).
- CLI drain/restore replay — `cli/_shared.py:25` builds a `Retriever` inside `init_replay_lightweight`, used by `CheckpointRestore`. No `.recall()` in CLI directly; no `yadgar recall`/`replay` subcommand exists.

---

## 3. The Parameterization Design (the crux)

**Goal:** one unified backend `/recall` pipeline whose *parameters* (`type`, `rerank_level`, `mode`, `max_results`, `min_heat`, `tags`, session context) select every variant → core has zero recall branches, just forwards params. Assessed honestly below: **profile → YES (as `rerank_level`), landscape → YES but as an endpoint-dispatched distinct backend function, NOT a fanout post-stage.**

### 3.1 profile → `rerank_level` parameter — VIABLE

**Verdict: viable.** `profile` is fundamentally "which rerank stages run." From `profiles.py`:

| profile | signals | cross_encoder | nli | multi_passage |
|---|---|---|---|---|
| fast | [vector, fts] | ✗ | ✗ | ✗ |
| balanced (default) | [vector, fts, ppr, spreading] | ✓ | ✗ | ✓ |
| full | [vector, fts, ppr, spreading] | ✓ | ✓ | ✓ |
| debug | same as full + `_debug` | ✓ | ✓ | ✓ + emit stage_stats |

This is a natural **pipeline parameter**. The design: replace the separate B2 profile branch with a `rerank_level` (equivalently, keep the name `profile`) field on `RecallRequest`, threaded into `_fanout_recall` → the memory arm.

**The load-bearing decision — pick ONE memory pipeline.** There are two, and this is the wart the advisor flagged:
- `Retriever.recall(profile: str|None)` (core.py:484/491) — the **monolithic WRRF** path. This is what the **fanout memory arm** calls internally (`providers/memory.py:62` → `Retriever.recall`) AND what the **hook** calls with `profile="fast"`.
- `Retriever.recall_via_pipeline(profile=)` (core.py:372) — the **v5.31 plugin pipeline**. This is what MCP `recall(profile=)` (B2) routes to today.

**Decision: the fanout keeps the monolithic `Retriever.recall` and threads `profile` into it.** Rationale: (a) the fanout memory arm already calls `Retriever.recall`, and that method already accepts `profile: str|None` (core.py:491) — the hook exercises this exact path today, so it is battle-tested; (b) folding profile into the fanout via `recall_via_pipeline` would require re-plumbing the plugin pipeline through the cross-type fusion, a larger change; (c) `recall_via_pipeline` is documented as "functionally identical to recall() with profile=balanced" (core.py:384) — it is a diagnostic/timing wrapper, not a distinct capability. **B2's `recall_via_pipeline` route is therefore retired from the recall dispatch** (the plugin pipeline stays in the package for `recall_compare`/debug tooling, but is no longer a production recall path). Confirm no other production caller depends on `recall_via_pipeline` before removing the B2 branch (grep `recall_via_pipeline` — expect only tests + `recall_compare`).

**Does `fast` need to be memory-only, or can it be memory+wiki-minus-rerankers? — the hook-budget question.** `fast` exists for the **prompt-recall hook's 2.0s latency budget** (`HOOK_RECALL_TIMEOUT_S`). Routing `fast` through the full fanout adds (i) the wiki arm and (ii) the **cross-type CE fusion** (`providers/fusion.py`, CE over the pooled candidates). "fanout minus rerankers" is only cheap if `rerank_level=fast` gates **both** the memory-arm CE/NLI/MP **and** the cross-type fusion CE. So the `rerank_level` param must control **every** CE/NLI/MP stage:

| Stage | File | Gated by `rerank_level`? |
|---|---|---|
| memory-arm CE (`_rerank_cross_encoder`) | `retrieval/reranking.py` | YES — off at `fast` |
| memory-arm NLI | `retrieval/reranking.py` | YES — off at `fast`/`balanced`, on at `full` |
| memory-arm multi_passage | `retrieval/reranking.py` | YES — off at `fast`, on at `balanced`/`full` |
| memory-arm PPR + spreading (signals) | `retrieval/scoring.py` | YES — `fast` = [vector, fts] only (skip PPR/spreading) |
| **cross-type fusion CE** | `providers/fusion.py` | **YES — must be added.** At `rerank_level=fast`, skip the fusion CE pass (union memory+wiki by native score, no CE). This is the new gate the design adds. |

**Contract shift for profile callers (name it).** Today `profile=` is memory-only and ignores `type` (recall.py docstring). Under the unified design, MCP `recall(profile="fast")` callers move from **memory-only** to **memory+wiki** (unless the `fast`→`type=memory` fallback form wins, §3.1 below). This is a behavior change for existing `profile=` MCP callers — distinct from the hook, which calls `Retriever.recall` directly (§5.4) and is unaffected by the MCP-tool routing change. (Also folds in the branch/postmortem-boost change above.) Ranking-affecting → LongMemEval-gated.

**Two honest forms, decide by the hook benchmark (§7 gate):**
- **Preferred (if it hits budget):** `rerank_level=fast` → memory+wiki fanout with **all** CE/NLI/MP + fusion-CE skipped. Cheap union of both sources; the hook gets wiki context for free.
- **Fallback (if fanout+wiki still blows 2.0s):** `rerank_level=fast` implies `type=memory` internally (memory-only, no wiki arm, no fusion CE). Still parameterization, still zero core branches — just not the memory+wiki form. **This is not a defeat; it is the same slogan with a narrower fast path.**

The choice between them is a **measurement** (the hook warm-latency benchmark), not a design assumption. Do NOT ship `fast`-through-fanout without proving it fits the hook budget on the warm box.

**`stage_overrides`** stays supported as a per-call escape hatch (already a `RecallRequest` field) for `nli`/`ce`/`mmr` fine control on top of the coarse `rerank_level`.

### 3.2 landscape → `mode` parameter, distinct backend function — VIABLE (not a fanout stage)

**Verdict: viable as an endpoint parameter, but REJECT "landscape as a post-fanout consensus stage."**

`consensus_retrieve` (astrocyte_pool.py:267) is **fundamentally different candidate-gathering**, not a re-scoring of fanout candidates:
- **Different candidate set** — it iterates domain-partitioned `_processes[domain]["memory_ids"]` (only memories *assigned* to an astrocyte domain), not the exhaustive FTS+vector pool the fanout builds. The sets do not overlap; you cannot "run the fanout, then vote."
- **Different scoring** — per-domain `heat*0.4 + sim*0.6`, weighted by keyword-derived `domain_relevance`, then a multi-domain vote boost (`+15%` per extra vote). No WRRF, no CE, no PPR/spreading.
- **Different output shape** — rows carry `consensus_score` (float) and `voting_domains` (list[str]); the fanout rows carry `_retrieval_score`/`_cross_encoder_score`. The MCP contract already documents these extra keys for landscape (recall.py docstring).

So landscape **resists** being a fanout stage-toggle. **But the user's real goal — zero core branches — is still met** without forcing it into the fanout: `mode=landscape` becomes an endpoint parameter, and the backend `/recall` route **dispatches internally** to `consensus_retrieve` (a distinct function) instead of `_fanout_recall`. `consensus_retrieve` needs only `storage` + `embeddings` + the `_pool` — all backend-side (the backend already runs `init_engines()`, `embed_service.py:906). **The current 400 on `mode=landscape` (embed_service.py:965) is a policy refusal, not a capability gap.** Removing that refusal and wiring the backend `_pool` is the whole change.

**This is the "least-ugly alternative" the crux prompt asked for on the resistant case:** landscape stays a *distinct code path*, but it lives **backend-side, selected by an endpoint param** — so core still has zero recall branches. It is a param of the endpoint, a distinct function inside the backend. Cite memory id 530802: the decision (2026-06-18, WITH the user) is that consensus is a **non-default broad/landscape mode** precisely for latency/completeness/cold-start reasons — keep it distinct, do not make it the default retrieval path, do not fold it into the flat fanout. This plan honors that: landscape stays opt-in, distinct, and now backend-served.

**Output-shape honesty:** the `RecallResponse.results` list already carries free-form dicts (`list[dict]`), so landscape's extra `consensus_score`/`voting_domains` keys travel through the existing response schema unchanged. No response-model change needed.

**Astrocyte pool availability backend-side (open item):** confirm the backend's `init_engines()` builds `_st._pool` when `ASTROCYTE_POOL_ENABLED=True` (landscape returns `[]` gracefully when `_pool is None`, recall.py:78). If the backend does not init the pool today, that wiring is part of this work; if it cannot (pool is a core-consolidation construct), landscape stays the **one** documented core-resident exception (see §5.5 fallback).

### 3.3 The unified contract (net design)

`POST /recall` grows to serve all variants via parameters; core forwards them verbatim:

```
RecallRequest {
  query, directory, current_branch, default_branch,
  max_results, min_heat,
  type:  "all" | "memory" | "wiki",
  mode:  None | "landscape",          # backend dispatches consensus_retrieve when "landscape"
  profile / rerank_level: None | "fast" | "balanced" | "full" | "debug",
  stage_overrides: dict | None,       # per-call CE/NLI/MMR escape hatch
  tags, session_key, prev_top_id,     # session context for backend-side SR/map writes (§4)
}
```

Backend route dispatch: `mode==landscape` → `consensus_retrieve`; else `_fanout_recall(..., profile=rerank_level)`. Core `recall()` collapses to: validate args → assemble request → POST → run **session** side-effects on the returned list → return. Zero compute, zero branches.

---

## 4. Session-context threading

`_apply_recall_side_effects` = DB half (backend) + session half (core). Today the session half runs core-side on the **returned** results using core-process singletons. The prompt asks: thread `session_key` + `prev_top_id` into `/recall` so the **SR/cognitive-map writes** can go backend-side (DB-backed), while keeping the **action-buffer + replay counter** in core (they are core-session lifecycle, not recall compute).

**Design:**
- **Move backend-side (DB-backed):** the SR successor-representation transition (`_cognitive_map.record_transition`/`incremental_update`) and `_last_recalled_ids`. These are *DB-persistable* if the cognitive map is backed by SurrealDB. Thread `session_key` (today hardcoded `"default"`, process-global) and `prev_top_id` in the request so the backend records "prev recall → this recall" against the caller's session. **Precondition:** the cognitive map must be DB-backed for this to be correct across the boundary; if `_cognitive_map` is an in-process-only singleton, moving it backend-side means it lives in the *backend's* process — still fine for a single backend, but confirm there is no core reader of the SR map (grep `_cognitive_map` readers). If a core consolidation cycle reads the SR map, keep SR core-side (see below).
- **Keep core-side (session lifecycle):** `buffer.capture_action("recall", ...)` (feeds consolidation/replay stream) and `_replay.record_tool_call()` (auto-checkpoint interval counter). These are core-process session lifecycle, not recall compute — the forwarder runs them on the returned results, exactly as B3 does today (`_apply_recall_session_side_effects`, recall.py:401).

**Conservative default:** if SR-map DB-backing is not already true, ship forward-only with the **entire** session half staying core-side (status quo of B3 — already correct and tested), and thread `session_key`/`prev_top_id` as a **follow-up** optimization. Forward-only does not *require* moving SR backend-side; it only requires the compute + DB side-effects move (done in B3). Do not couple the SR-relocation risk to the forward-only cutover — sequence it after (§8).

---

## 5. Core rip-out (the forward-only cleanup)

### 5.1 Backend `/recall` contract widening (§3.3)
- Remove the two 400s (`embed_service.py:965` landscape, `:970` profile).
- Add `mode==landscape` dispatch → `consensus_retrieve` (wire backend `_pool`; §3.2).
- Thread `profile`/`rerank_level` into `_fanout_recall` → memory arm `Retriever.recall(profile=)`; add the **fusion-CE gate** so `fast` skips cross-type CE (§3.1).
- `RecallRequest` already has `profile`, `mode`, `stage_overrides`, `tags` fields (embed_service.py:920-923) — the validators exist; only the route body's refusals + dispatch change.

### 5.2 Core `recall()` → pure forwarder
Rip out of `recall.py`:
- B1 `_landscape_recall` call + the function (recall.py:46-91, 288-296).
- B2 `recall_via_pipeline` branch (recall.py:428-443).
- B4 in-core `_fanout_recall` call (recall.py:410-424) **and the try/except fallback** (recall.py:403-408) — no fallback (decision 6).
- B5 legacy monolithic body (recall.py:426-595) and B6 bare-FTS fallback (recall.py:453-484).

**⚠️ Post-fanout Python — verified line-by-line, NOT a clean delete.** The inline post-processing at recall.py:488-595 is reached by B2 (`recall_via_pipeline` falls through) and B5, but the fanout block returns at recall.py:424 *before* it. Verified against `_recall_pipeline.py` (`grep BRANCH_BOOST_WEIGHT|POSTMORTEM_BOOST` → **zero hits in `_fanout_recall`**):

| Post-processing | recall.py | In `_fanout_recall`? | Fate |
|---|---|---|---|
| quality floor (`_apply_quality_floor`) | 515-516 | YES (via MemoryProvider/helper) | delete inline — fanout covers it |
| dedup (`_dedup_by_content`) | 521 | YES (`_recall_pipeline.py:329`) | delete inline — fanout covers it |
| wiki blending | 564-589 | YES — fanout's WikiProvider replaces it (`_recall_pipeline.py:297`) | delete inline |
| **C4 branch boost** (`BRANCH_BOOST_WEIGHT`, convex) | 526-534 | **NO** | **BEHAVIOR CHANGE** — see below |
| **postmortem/incident boost** (`POSTMORTEM_BOOST_FACTOR`) | 540-552 | **NO** | **BEHAVIOR CHANGE** — see below |

**The C4 branch boost and postmortem boost run TODAY for profile (B2) and legacy (B5) callers but NOT for the default fanout path (B3/B4).** Retiring B2 into the fanout therefore **drops both boosts for `profile=` callers** unless the fanout gains them. **Decision required (do not ship the hedge):** either (a) **add the C4 branch boost + postmortem boost into `_fanout_recall`** (recommended — it makes the fanout the single source of the full scoring, and closes a pre-existing parity gap where the default path already lacked them) — gated on LongMemEval (these are ranking changes); or (b) **accept the loss** and name it as a deliberate scoring change for profile callers. Recommend (a): the default path arguably *should* have had branch boost all along; adding it to the fanout unifies scoring and is the honest fix. Whichever is chosen, it is a **ranking-affecting change** → A/B on LongMemEval memory domain (§7 #9).
- The `RECALL_BACKEND_ENABLED` gate (recall.py:387) — forward is unconditional.
Core `recall()` becomes: validate (directory/type/mode/profile) → resolve branch → `observe_recall` shadow (keep, handler-level) → `_forward_to_backend(...)` (now also forwards `mode`, `profile`, `session_key`, `prev_top_id`) → `_apply_recall_session_side_effects(merged, query)` → return. On backend error: **raise** (loud), do not fall back.

### 5.3 Remove the flag
- Delete `RECALL_BACKEND_ENABLED` from `config.py:360` + its `FIELD_META`/`_REGISTRY` rows (three-way-sync test `test_config_three_way_sync.py` must stay green — removing a registered field requires removing all three registrations together).
- `UNIFIED_RECALL_ENABLED` (config.py:353, default True): with legacy (B5) deleted, this flag's `False` branch no longer exists. **Decision needed:** either remove `UNIFIED_RECALL_ENABLED` too (fanout is now the only pipeline) or keep it pinned True as a safety assert. Recommend **remove** — one fewer dead knob — but confirm no test pins it False (grep; the longmemeval harness forces it True at `run_longmemeval.py:571`, which becomes a no-op/removable).

### 5.4 The orphans (in-core `Retriever.recall` callers) — explicit disposition
Forward-only "nothing in core" must account for these, not just the MCP branches:
- **Prompt-recall hook (`http.py:912-919/1524/1653`, `profile="fast"`).** This is the strongest core-bound case: a **2.0s prompt-time budget** (`HOOK_RECALL_TIMEOUT_S`), and a network hop to the backend fights that SLA. **Disposition: wire the hook to forward** `profile=fast` to `/recall` (now that `/recall` serves `rerank_level=fast`), **gated on the hook-latency benchmark** (§7). If the forward-with-timeout beats or matches the local budget on the warm box → forward. If the network hop cannot hit 2.0s → the hook is the **one accepted core-resident exception** (documented), still calling `Retriever.recall(profile="fast")` locally. Decide by measurement, not slogan.
- **Memorize reinjection (`_phase_post_write.py:181-189`).** Gated `REINJECT_ON_WRITE=False` default (off in prod). **Disposition: wire to forward** (`type=memory`, `rerank_level=fast`) for consistency, low priority since default-off. Acceptable to leave as a documented core island until the flag is turned on.
- **CLI drain/restore replay (`cli/_shared.py:25`).** Decision 4 = kill it. The CLI builds `Retriever` via `init_replay_lightweight` for `CheckpointRestore`. **Disposition: remove the CLI-side `Retriever` construction**; if `CheckpointRestore` genuinely needs recall during replay, that path must forward to the backend too (CLI may run with no backend — accept that CLI replay requires a running backend, consistent with dropping stdio). Confirm what `CheckpointRestore` does with the retriever before deleting (grep its usage) — it may only need it for a non-recall path, in which case removal is clean.

### 5.5 Landscape fallback (if backend `_pool` cannot be wired)
If §3.2's precondition fails (backend cannot host the astrocyte pool), landscape stays the **single** documented core-resident recall branch. That is the least-ugly degradation and still leaves the default path zero-branch. Note it loudly; do not pretend zero-branch if this fires.

---

## 6. stdio deprecation

**Decision (user):** drop/deprecate stdio. HTTP is the deployed transport; stdio was the no-backend fallback that made a local in-core path *necessary*. Removing stdio removes the reason B4/B6 existed.

**What to remove/gate:**
- `__main__.py:8` `VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")` — drop `"stdio"` (or gate it behind an explicit opt-in env for dev, but the user wants it gone; recommend **remove**).
- `__main__.py:50-55` `--transport` default `"stdio"` → default `"streamable-http"`.
- `lifecycle.py:804` `def main(transport="stdio")` default → `"streamable-http"`.
- `setup.py:128` stdio fallback (Docker-unavailable path) — remove/deprecate.
- `hooks/prompt-recall.py:75` process-mode direct-FTS fallback (the daemon-down cheap path) — this exists *because* there may be no daemon; with stdio+no-backend gone, decide whether the daemon-down fallback still has a purpose (daemon can still be transiently down). Recommend **keep** the daemon-down FTS fallback as an availability net (it is not a *transport* fallback), but it is orthogonal to stdio.

**Consequence (owned explicitly):** dropping stdio means **every user runs the backend container** — the streamable-HTTP core+backend deployment becomes mandatory. This is the largest UX change in the plan. The `/tmp` audit flagged this as "option (a), likely non-starter for the primary use case"; the user has now explicitly chosen it ("stdio is not the purpose of yadgar"). The plan owns it: this is a deliberate narrowing of supported deployment to the Docker HTTP topology.

**Migration note (MIGRATION_NOTES.md, hand to user):** any existing stdio-based Claude Code MCP config (`"command": "yadgar", "args": ["--transport", "stdio"]` style) must migrate to the streamable-HTTP endpoint. Document the exact `.mcp.json` / client config change; do not auto-apply.

---

## 7. Tests (no fallback → airtight)

With no runtime fallback, tests carry all safety. Required suite:

1. **Backend-env contract test (the #44 catcher).** Boot the backend with offload-on and **no `EMBED_URL`** (the #44 bootstrap-500 condition) and assert `/recall` returns 200 with `init_engines(local_engines=True)`. This is the regression guard for the fixed bootstrap crash. (No such test exists today — `test_recall_pipeline_unit.py` covers the split + forwarder but not backend bootstrap under offload.)
2. **Forward-only e2e per variant** — drive core `recall()` (forward is unconditional) against a real backend for EVERY variant: fanout (`type=all`), `type=memory`, `type=wiki`, `mode=landscape`, `rerank_level=fast`/`balanced`/`full`. Assert non-empty ranked results with the right shape (landscape → `consensus_score`/`voting_domains`; fast → no `_cross_encoder_score`).
3. **Loud-failure-on-backend-error** — backend down/500 → core `recall()` **raises** (not returns `[]`, not local fanout). Explicitly assert no in-core `_fanout_recall` is called (the deleted fallback must stay deleted). Replaces `test_recall_backend_enabled_fallback_on_error` (which asserts the *opposite* — that test is deleted).
4. **Side-effect WRITE parity** (not just ids+scores) — assert which memory ids got heat-boosted + metamemory-bumped **backend-side**, and that SR/buffer/replay fired **core-side**, on the same corpus. This closes the gap the `/tmp` audit flagged: the old "byte-identical dual-path" test asserts ranking, not side-effect *location*. Assert the DB half ran once backend-side (no double-boost) and the session half ran once core-side.
5. **`rerank_level=fast` stage-skip assertion** — assert `fast` runs NO CE (memory-arm) AND NO fusion CE (the new gate); assert `balanced` runs CE+MP but not NLI; `full` runs NLI. Guards the §3.1 gate wiring.
6. **Landscape backend parity** — `mode=landscape` via `/recall` returns the same consensus rows as the old core `_landscape_recall` on the same corpus (pin the migration).
7. **Flag-removal guards** — `RECALL_BACKEND_ENABLED` absent from config registry (three-way-sync stays green); no code references it.
8. **Hook forward/latency test** — if the hook forwards (§5.4), assert it respects `HOOK_RECALL_TIMEOUT_S` and returns within budget; if it stays core-resident, assert it still calls `Retriever.recall(profile="fast")` locally.
9. **Quality gate (standing directive)** — `make longmemeval` on the memory domain, forward path, non-regressing vs the pre-change baseline. A regression aborts (same discipline as ADR-0043).

**TDD order:** write the loud-failure test + backend-env contract test + per-variant e2e as **failing** first (they fail today because the flag is OFF and profile/landscape 400), then implement to green.

---

## 8. Sequencing / PRs

**Two PRs, not one.** Blast radius is THE core feature; split so the backend can serve everything *before* core loses its fallback.

**PR-1 — Backend contract widening (additive, reversible).**
- Widen `/recall`: remove the profile+landscape 400s, add `mode=landscape`→`consensus_retrieve` dispatch (+ backend `_pool` wiring), thread `profile`/`rerank_level` into `_fanout_recall`, add the fusion-CE `fast` gate.
- Tests: per-variant backend e2e (#2 backend-side), landscape parity (#6), fast stage-skip (#5), #44 backend-env contract (#1).
- **Backend still serves the old contract too** (core still on flag-OFF default), so this PR is safe to land alone. `BACKEND_VERSION` bump (currently `5.13.0`, `__init__.py:21`) — the recall contract moves service tracks; asserted by `check_backend_bump.py` + the canonical-version drift-guard.

**PR-2 — Core rip-out + flag removal + stdio drop (the cutover).**
- Gut `recall()` to pure forwarder; delete B1/B2/B4/B5/B6; remove `RECALL_BACKEND_ENABLED` (+ decide `UNIFIED_RECALL_ENABLED`); loud-failure-on-error.
- Orphans: wire/kill per §5.4. stdio deprecation per §6. CLI recall/replay kill (decision 4).
- Tests: forward-only e2e (#2 core-driven), loud-failure (#3), side-effect write parity (#4), flag-removal guards (#7), hook (#8), longmemeval quality gate (#9). Delete `test_recall_backend_enabled_fallback_on_error`.
- Core version bump.
- **Session-context threading (§4) is a SEPARATE follow-up (PR-3), not part of the cutover** — it carries independent SR-DB-backing risk; do not couple it to forward-only. Ship forward-only with the session half staying core-side (already correct/tested), then relocate SR if the map is DB-backed.

**Rollback:** PR-1 is additive (backend serves both contracts). PR-2 is the irreversible cutover (no flag to flip back) — that is the point of decision 6. Mitigation is the test suite + landing PR-2 only after PR-1 soaks. If PR-2 must be reverted, it is a git revert of the core changes (backend PR-1 stays; it is a strict superset).

---

## 9. Risks

1. **No fallback = a backend outage is a hard recall outage.** Decision 6 accepts this; mitigation is tests + the mandatory-backend deployment (stdio dropped, §6). Honest: recall availability now equals backend availability. This is the deliberate cost of forward-only.
2. **`fast`-through-fanout may blow the hook's 2.0s budget** (wiki arm + fusion CE). Mitigation: §3.1's measured fork (memory+wiki form vs memory-only form); §7 hook-latency test gates it. Do not assume the memory+wiki form is free.
3. **Two memory pipelines** — picking `Retriever.recall` (monolithic) over `recall_via_pipeline` (plugin) retires the plugin path from production recall. Risk: a hidden production caller of `recall_via_pipeline`. Mitigation: grep before deleting B2; keep the plugin pipeline in-package for `recall_compare`/debug.
4. **Backend astrocyte pool** may not be wirable backend-side (pool is a core-consolidation construct). Mitigation: §5.5 — landscape stays the one documented core exception if so. Cite 530802: landscape is non-default and rare, so a core-resident landscape is acceptable.
5. **SR-map relocation correctness** (§4) — moving SR backend-side is wrong if a core consolidation cycle reads the map. Mitigation: decoupled into PR-3; forward-only ships with SR core-side (status quo).
6. **stdio drop = UX regression** (every user runs the backend container). Mitigation: user-chosen and owned (§6); MIGRATION_NOTES for client config.
7. **CLI replay may need recall** — killing `init_replay_lightweight`'s `Retriever` could break `CheckpointRestore`. Mitigation: §5.4 — grep `CheckpointRestore` retriever usage before deleting; forward or accept backend-required CLI replay.
8. **Three-way-sync test** on flag removal — removing `RECALL_BACKEND_ENABLED` from one registration and not the others breaks the test. Mitigation: remove all three (env/yaml/registry) atomically; the test is the guard.

---

## 10. Advisor input (both passes)

**Pass 1 (after mapping landscape+profile internals, before committing the design):**
- **Landscape → NOT a fanout stage-toggle; YES a backend-served mode param.** `consensus_retrieve` gathers a *different candidate set* (domain-assigned only), *different scoring* (per-domain heat+sim vote, no WRRF/CE), *different output shape* (consensus_score/voting_domains). Can't "run fanout then vote." Least-ugly: core forwards `mode=landscape`, backend dispatches to `consensus_retrieve` (a distinct function); the 400 is policy, not capability. Cite memory 530802 (decided WITH the user 2026-06-18) — keep consensus a distinct non-default mode; don't relitigate. → **Adopted as §3.2.**
- **Profile → viable as `rerank_level`, but two decisions gate "how":** (a) the two-memory-pipeline wart — folding profile into the fanout forces picking ONE; `Retriever.recall` already takes `profile: str|None`, may be the hook the fanout threads. (b) `fast`'s purpose is a latency budget — through the fanout it adds the wiki arm AND cross-type fusion CE; only cheap if `rerank_level=fast` gates BOTH the memory-arm rerankers AND the fusion CE; if not, honest form is `type=memory + rerank_level=none`. → **Adopted as §3.1 (monolithic pipeline chosen; fusion-CE gate added; two honest forms decided by benchmark).**
- **Verify stale premises (observed-state-wins):** the task said profile=fast at http.py:897/918/etc.; the `/tmp` audit claimed "NOT profile=fast." → **Explore re-checked: the hook DOES pass `profile="fast"` to the monolithic `Retriever.recall` (http.py:912-919/1524/1653) — the `/tmp` audit's correction was itself wrong.** Resolved in §2/§5.4. stdio-drop has real UX cost (option (a)) — plan must own it → §6 owns it explicitly.
- **Don't let "zero core recall paths" skip the orphans** (reinjection, CLI replay, hook) — the rip-out section must say what happens to each. → **Adopted as §5.4.**

**Pass 2 (before finalizing):**
- **BLOCKER caught — the §5.2 "either moves backend-side or is legacy-only" hedge hid a possible silent regression.** B2 (`recall_via_pipeline`) falls through to the inline post-processing (recall.py:488-595); the fanout returns at recall.py:424 *before* it. **Verified by grep:** `_fanout_recall` applies quality-floor + dedup + wiki-blend (all safe) but **NOT** the C4 branch boost (recall.py:526-534) or postmortem boost (recall.py:540-552) — zero `BRANCH_BOOST_WEIGHT`/`POSTMORTEM_BOOST` hits in `_recall_pipeline.py`. So retiring B2 into the fanout **drops both boosts for profile callers** unless the fanout gains them. → **Resolved in §5.2: decision required, recommend adding both boosts to the fanout (closes a pre-existing default-path parity gap), LongMemEval-gated.** Not a hedge anymore.
- **Profile-caller contract shift** (memory-only → memory+wiki) must be named. → **Added to §3.1.**
- **Light-verify: monolithic `Retriever.recall(profile=)` honors `full`/`debug` (NLI on).** `profiles.py:8-10` states the legacy `cross_encoder`/`nli`/`multi_passage` keys feed `_RerankingMixin._apply_rerank_pipeline` (the monolithic reranker), so the monolithic path likely honors all four rerank toggles — `fast`/`balanced` are prod-proven (hook + default arm); `full`/`debug` via the monolithic path are unproven but plumbed. **Action:** confirm `full`/`debug` produce NLI-on results through the fanout memory arm during PR-1 e2e (§7 #2 covers this).
- **Everything else confirmed sound** — two-PR split, loud-failure test replacing `test_recall_backend_enabled_fallback_on_error`, #44 backend-env catcher, side-effect write-parity test, landscape-as-backend-mode with §5.5 pool-availability fallback, session-threading decoupled to PR-3. No scope expansion.

---

## 11. References

- `docs/plans/recall-3-train-overhaul-2026-07-04.md` — Train 1 (backend move, shipped behind flag).
- `docs/plans/recall-pipeline-to-backend-2026-07-04.md` — § Statefulness+Fanout audit (GO-WITH-CAVEATS), side-effect split rationale.
- `/tmp/recall_core_paths_audit.md` — full core-path inventory (6 MCP branches + 3 orphans + stdio problem).
- Memory id 530802 — consensus-as-non-default-mode decision (2026-06-18, with user).
- Key code: `recall.py:177` (MCP tool), `:99` (forwarder), `:46/288` (landscape), `:410` (in-core fanout, to delete); `embed_service.py:943` (`/recall` route), `:965/970` (the 400s to remove), `:910` (`RecallRequest`); `_recall_pipeline.py:198/360/402` (fanout + side-effect halves); `astrocyte_pool.py:267` (`consensus_retrieve`); `retrieval/core.py:484/491` (monolithic `recall(profile=)`), `:372` (`recall_via_pipeline`, to retire); `profiles.py:21-103` (profile dicts); `http.py:912-919/1524/1653` (hook, profile=fast); `config.py:360` (flag), `:353` (UNIFIED), `:377` (HOOK_RECALL_TIMEOUT_S), `:718` (REINJECT_ON_WRITE); `__main__.py:8/50` (transport); `entrypoint.sh:41` (streamable-http); `__init__.py:21` (BACKEND_VERSION=5.13.0).
