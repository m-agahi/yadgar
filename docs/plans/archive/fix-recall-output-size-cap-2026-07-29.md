# Bound `recall()` MCP output size — 2026-07-29

**Task:** `task:0085` — unbounded `recall()` output size.
**Status:** PLANNED. One car. Core-only (`core 5.168.0 → 5.169.0`; `BACKEND_VERSION 5.59.0` **unchanged**).
**Related:** `task:0080` (context-window budget) — same class, different surface.

## Context

Every subagent dispatch in this repo is instructed to `recall()` first. On an unlucky
topic that single call consumes most of the agent's budget or fails outright.

Observed this session:

| Call | Result |
| --- | --- |
| planning agent, systemd/linger topic, default `max_results` | **~78,000 chars** — exceeded the tool-output token cap, result **unusable**; agent abandoned recall and fell back to grep |
| second agent, same topic, `max_results=4` | ~28,000 chars — survivable, still ~7 KB/row |
| this planning agent, `recall(max_results=3)` | ~15,000 chars for **three rows** |
| `adr_list(directory=…)` | 57.1 KB — harness spilled it to a persisted-output file |

The failure is self-defeating: the memory system pushes agents back to grep. `max_results`
is the only lever and it is a **row-count** proxy for a **byte** problem — a 3-row recall
can be larger than a 10-row one.

### Measured row anatomy (real, not estimated)

Serialised the exact row `recall()` returned for memory `529839` (compact JSON separators):

```
full row            : 4132 B  (45 fields)
  content           : 2515 B  (60.9%)
  metadata          : 1233 B  (29.8%)
  contextual_prefix :  384 B  ( 9.3%)
  → non-content share: 38.8%
```

Two different problems by row type — **both mechanisms are needed**:

- **Memory rows are metadata-heavy.** 38.8% of the payload is scoring/thermodynamic
  internals no caller reads. Field projection alone is a **−24.9%** win at zero
  information cost.
- **Wiki rows are content-heavy.** The two ADR pages in the same 3-row recall carried
  ~350 B of metadata each and 3–4 KB of *full page body*. The 28 KB / 4-row case
  averages 7 KB/row — that is wiki-body dominated. Only a content cap touches it.

Combined (denylist projection + per-row content cap):

| content cap | row bytes | vs raw | 10 rows |
| --- | --- | --- | --- |
| 800 | 1466 B | −64.5% | ~14 KB |
| **1200** | **1866 B** | **−54.8%** | **~18 KB** |
| 2000 | 2666 B | −35.5% | ~26 KB |

## Investigation findings (verified against code)

1. **`recall()` is a pure forwarder with zero size bound.**
   `yadgar/core/server/tools/recall.py:128-288`. Line 252 assigns
   `merged = _forward_to_backend(...)`; line 273 `return merged` — raw, unshaped.
   The only mention of size is advisory prose in the docstring
   (`recall.py:152-153`: *"Higher = slower + more tokens"*).

2. **The backend does not project either.**
   `POST /recall` (`yadgar/backend/embed_service/embed_service_routes.py:124-222`)
   returns `RecallResponse(results=results)` where
   `results: list[dict]` (`embed_service_models.py:92-95`) — untyped passthrough.
   The single field ever removed is the embedding blob
   (`yadgar/backend/retrieval/recall_pipeline.py:182`, `raw.pop("embedding", None)`;
   also `reranking.py:150,348`, `fusion.py:309,390`).

3. **`contextual_prefix` (384 B, 9.3%) has no read-side consumer.**
   It is a *write-side* artifact: generated at ingest and concatenated to build the
   embedding text — `backend/write_exec/_memorize_phases/_phase_embed.py:70-75`,
   `backend/curation/ingestion.py:105`, `backend/retrieval/core.py:181`. Nothing on
   the retrieval or hook-render path reads it back. It is also pure duplication:
   its `[Project:]`/`[Directory:]`/`[Tags:]` segments restate `directory_context`
   and `tags`, which are already structured fields on the same row.

4. **No usable compression/chunking boundary exists for byte truncation.**
   - `compression_level` is a memory-lifecycle counter (written `0` on every insert:
     `_shared/storage/memory.py:122`, `_shared/storage/wiki.py:861,917,978,1049,1109`;
     mutated by `backend/curation/strengthen.py:210,231`). Not a projection API.
   - `_chunk_id` / `_position_reason` come from Cowan 4±1 cognitive-load management
     (`_shared/metacognition/cognitive_load.py:97,131,142`). `manage_context` **does**
     already drop low-ranked groups and emit `_chunk_id: -1` /
     `_position_reason: "overflow_summary"` rows — useful *prior art for a visible
     overflow marker*, but it bounds **row count**, never bytes.

5. **Sibling tools — who caps what.**

   | Tool | Bound today | File:line |
   | --- | --- | --- |
   | `recall` | **none** | `tools/recall.py:273` |
   | prompt-recall hook | **`max_chars = 3000` total + `"..."` marker + content-only projection** | `core/server/http.py:1309-1323` |
   | `recent_memories` | 8-field projection + `content[:297] + "..."` | `tools/admin_other.py:217-233` |
   | `project_brief` | aggressive: `[:80]`, `[:100]`, `[:150]`, `[:600]`, `PROJECT_BRIEF_MAX_ANCHORS` | `tools/project.py:233,272,316,699` |
   | `wiki_query` | `max_results` only, no byte cap | `tools/wiki.py:608` |
   | `wiki_read` | none — **correct**, an explicit single-page read must not be trimmed | `tools/wiki.py:1123-1126` |
   | `adr_list` | **no `limit` param at all** | `tools/adr.py:319-342` |

6. **Config plumbing (ADR-0163).** Both paths exist:
   - Static default → `Settings` field in `_shared/config/config.py`
     (neighbours: `RECALL_MEMORY_QUOTA:383`, `RECALL_WIKI_QUOTA:384`), plus a
     `ConfigEntry` in `_shared/config/config_registry.py` (pattern at line 423), plus
     a three-way `env > yaml > default` integrity test
     (`tests/_shared/test_core_config_integrity.py`).
   - Per-directory override without restart → the ADR-0163 runtime-config resolver,
     `_resolver_get` behind `tools/runtime_config.py:106` (PTC read-through cache,
     never raises, falls back to `default`).

## Seam decision

**Put the cap inside `recall()`, immediately after `recall.py:263`** (after
`merged = _forward_to_backend(...)`, before the side-effect submit and `return`).

`_forward_to_backend` has exactly **two** production callers, and the other one is
already bounded:

- `recall()` — `tools/recall.py:252` — **unbounded, the failure surface.**
- prompt-recall hook — `core/server/http.py:222` (short `HOOK_RECALL_TIMEOUT_S`
  timeout + `deadline_ms`) — which then applies its **own** `max_chars = 3000` total
  budget and content-only render at `http.py:1309-1323`.

So the three candidate seams resolve cleanly:

- ✗ **Backend `/recall` route** — forces a `BACKEND_VERSION` bump + image rebuild +
  redeploy coupling for a pure presentation concern, and would silently shrink the
  hook's input before *its* budget runs.
- ✗ **`_forward_to_backend`** — would double-cap the hook path with a coarser,
  wrong-shaped budget (hook wants one flat 3000-char blob, not per-row JSON).
- ✓ **Inside `recall()`** — single file, single function, covers both `mode=None`
  and `mode="landscape"` (both flow through the same `merged`), leaves the hook path
  untouched, core-only version bump.

**One constraint from the existing seam:** `recall.py:269-272` documents that the
deferred side-effect closure holds the same list the caller returns.
`_apply_recall_session_side_effects` must keep receiving the **untrimmed** rows (SR
transitions and the action buffer read fields the denylist removes). Build a new
projected list; pass the original `merged` to `_submit_session_side_effect`; return
the projected list.

## The car — `fix/recall-output-size-cap`

**Files touched (4):**

| File | Change |
| --- | --- |
| `yadgar/core/server/tools/recall.py` | new `_shape_recall_results()` + call site after line 263 + docstring |
| `yadgar/_shared/config/config.py` | 3 `Settings` fields |
| `yadgar/_shared/config/config_registry.py` | 3 `ConfigEntry` rows |
| `yadgar/tests/core/test_recall_output_cap.py` | new (TDD — written first) |

### 1. Field projection — **denylist**, not allowlist

Drop the scoring/thermodynamic/write-side internals; keep everything else.

```
contextual_prefix, vector_clock, sr_x, sr_y, plasticity, stability, excitability,
last_excitability_update, cofire_prior, graph_prior, surprise_score,
emotional_valence, reconsolidation_count, last_reconsolidated, slot_index,
source_episode_id, file_hash, embedding_model, compression_level,
access_count_since_decay, original_content, valid_until, _rerank_score,
_cross_encoder_score, _retrieval_confidence, _chunk_id, _position_reason,
wiki_schema_version
```

**Why denylist over allowlist.** An allowlist silently drops any field the retrieval
pipeline adds later — and this pipeline adds fields often. The live trap:
`mode="landscape"` stamps `consensus_score` and `voting_domains` per row
(`_shared/astrocyte_pool/astrocyte_pool.py:330-331`), a documented part of the return
contract (`recall.py:166-168,177-178`). An allowlist would have deleted them. With a
denylist, new fields default to visible — no silent information loss — and the hard
size guarantee comes from the byte budget below, not from the projection. The
projection is then a pure constant-factor win with **zero correctness risk**.

`original_content` is on the denylist deliberately: on a compressed memory it holds
the full pre-compression text and would roughly *double* the row.

Measured: **−24.9%** on a real row, 45 fields → 18.

### 2. Per-row content cap — visible

`RECALL_MAX_CONTENT_CHARS`, default **1200**.

When `len(content) > cap`, replace with `content[:cap]` and add **one** key:

```json
"_truncated": {"kept": 1200, "total": 2515, "fetch": "memory_get(529839)"}
```

`fetch` is `wiki_read("<slug>")` for `_source == "wiki"` rows.

**Truncation must be visible (design question (c)) — argued.** The alternative is
what happens today: the harness cap cuts mid-JSON, opaquely, producing malformed
output with no recovery path (the 78 KB case — the agent could not even tell what it
had lost, and abandoned the tool). An explicit marker gives the model three things it
currently has none of: (i) *knowledge* that the row is partial, (ii) the *magnitude*
of what is missing, so it can judge whether the tail matters, and (iii) a *recovery
path* by exact ID. And the JSON stays well-formed. `recent_memories`
(`admin_other.py:220-221`) sets a bare `"..."` with no count and no fetch hint — this
should improve on that precedent, not copy it.

**Constraint:** emit `_truncated` **only** on rows actually trimmed. Untrimmed rows
must be byte-identical modulo the denylist. This keeps small results transparent and
keeps `tests/core/test_recall_pipeline_unit.py:325` (`assert result == fake_results`)
green.

### 3. Total-byte backstop

`RECALL_MAX_TOTAL_BYTES`, default **65536**.

Per-row capping alone is not a hard bound (`max_results=50` × 1.9 KB still overflows).
After projection + per-row capping, walk rows in rank order accumulating serialised
size; when the budget is exhausted, **drop the remaining low-ranked rows** and append
a single trailing marker object:

```json
{"_dropped": {"rows": 7, "reason": "total_byte_budget", "budget": 65536}}
```

Dropping whole low-ranked rows beats truncating everything further: the top-ranked
results are what the agent came for, and CE rank order already says which rows are
least valuable. This is the same shape as the existing Cowan overflow behaviour
(`cognitive_load.py:142`), so the pattern is already in the codebase.

### 4. Per-call override

Add `max_chars: int | None = None` to the `recall()` MCP signature — a per-call
override of `RECALL_MAX_CONTENT_CHARS`. `None` = use the resolved default.
`0` / negative → `ValueError` (validated early, alongside the existing `type` / `mode`
/ `profile` guards at `recall.py:194-213`).

**Answer to design question (a): both, three layers.**
per-call `max_chars` → per-directory ADR-0163 row → global `Settings` default.
Rationale: the *default* must be right because agents will not pass the parameter
(the whole failure mode is that they call `recall(query, directory)` and nothing
else); the *override* must exist because a deliberate deep-dive legitimately wants
full rows. Resolution reads the runtime-config store via `_resolver_get` directly
(**not** the `@_tool`-decorated `config_get`) so no MCP tool dispatches inside a tool.

Config keys: `recall.max_content_chars`, `recall.max_total_bytes`.

### 5. Not in this car

`adr_list` — see open decisions.

## Acceptance criteria

- [unit] Denylist projection removes exactly the listed fields and nothing else;
  `id` / `content` / `tags` / `slug` / `title` / `directory_context` survive.
- [unit] `mode="landscape"` rows retain `consensus_score` and `voting_domains`.
  *(explicit regression guard for the allowlist trap)*
- [unit] Row under the cap → **no** `_truncated` key; row byte-identical modulo denylist.
- [unit] Row over the cap → `_truncated` present with correct `kept` / `total`, and
  `fetch` = `memory_get(<id>)` for `_source="memory"`, `wiki_read("<slug>")` for
  `_source="wiki"`.
- [unit] `_apply_recall_session_side_effects` receives the **untrimmed** rows while
  the caller receives the shaped list (patch the closure, assert both).
- [unit] Total-byte backstop drops lowest-ranked rows and appends exactly one
  `_dropped` marker with the correct count.
- [unit] `max_chars=0` and `max_chars=-1` raise `ValueError`; `max_chars=None` resolves
  to the configured default.
- [unit] Three-way config resolution for both keys (`env > yaml > default`), per
  `tests/_shared/test_core_config_integrity.py`.
- [unit] Existing `tests/core/test_recall_pipeline_unit.py:325` (`result == fake_results`)
  still passes unmodified.
- [e2e] Live `recall(max_results=20)` against the real corpus serialises under
  `RECALL_MAX_TOTAL_BYTES`.
- [e2e] `tests/e2e/test_recall_backend_contract_e2e.py` still green — the backend wire
  contract is untouched.
- [manual] Re-run the known-bad systemd/linger query that produced **78,000 chars** and
  record before/after bytes in the PR body. This is the strongest evidence available:
  a reproducible, already-measured failure.
- [manual] Prompt-recall hook output unchanged — `curl` the `/hooks/prompt-recall`
  endpoint before/after and diff. Proves the hook seam was not disturbed.
- `scripts/check_versions.py` green with `core 5.169.0` / `BACKEND_VERSION 5.59.0`.

## Risks

| Risk | Mitigation |
| --- | --- |
| A denylisted field turns out to have a live consumer | Denylist entries were each grepped for read-side use; `contextual_prefix` verified write-side-only (finding 3). Any miss is a one-line revert of a set literal. |
| Recall *quality* regression | None — shaping runs strictly **after** retrieval, rerank and fusion. Ranking is untouched. The LongMemEval gate is not implicated. |
| Truncation hides the answer's tail | The `_truncated` marker gives an exact-ID recovery path; default 1200 chars is ~2× the typical CE passage window (`GTE_RERANKER_MAX_LENGTH: 512`, `config.py:295`). |
| Consumers that parse recall rows positionally | Grep for `recall(` callers before merge; the `_shared/runtime/recall_session.py` consumer is explicitly fed untrimmed rows. |
| New MCP param `max_chars` breaks schema discipline | `tests/scripts/test_v5_43_0_mcp_schema_discipline.py` — add a param-acceptance test mirroring `test_r1_recall_accepts_branch_hint` (`:218`). |
| Latency | Negligible — dict comprehension + one `json.dumps` per row, on a path whose CE stages cost seconds (ADR-0035). |

## Open decisions for the user

1. **`adr_list` — same car or follow-up?** It is a **different bug with a different
   mechanism**: its rows are already narrow (`adr.py:327` — 7 scalar fields), so the
   57 KB is *row count*, not field width. The fix is a `limit` / offset param, not
   projection or truncation (`adr_list` has **no `limit` at all** today, `adr.py:319`).
   - *Same car:* ~3 lines, and it is the case that demonstrably spilled to a file.
   - *Follow-up car:* keeps this car single-seam (`tools/recall.py`) as scoped.
   → **Recommend follow-up**, but it is cheap enough that the call is yours.

2. **Default `RECALL_MAX_CONTENT_CHARS` — 800, 1200, or 2000?** Measured 10-row
   payloads: ~14 KB / ~18 KB / ~26 KB. **Recommend 1200** — ~55% reduction while
   still carrying a full anchor-sized memory (the anchor measured above is 2515 chars,
   so 1200 keeps roughly the first half plus an explicit pointer to the rest).
   800 is more aggressive but starts cutting mid-paragraph on typical anchors.

3. **`RECALL_MAX_TOTAL_BYTES` default 65536.** This is *not* calibrated against a
   verified harness token limit — nobody has measured where the tool-output cap
   actually sits. 64 KB is chosen as "comfortably under the observed 78 KB failure
   with headroom". If you know the real cap, set it from that instead.

4. **Do the sibling tools get capped in this car?** (design question (d))
   → **Recommend no.** `wiki_query` shares the failure mode but is far less used than
   `recall` and is already superseded by `recall(type="wiki")` per its own deprecation
   notice; `wiki_read` must **never** be capped (explicit single-page read);
   `project_brief` is already the best-behaved tool in the repo. Capping `recall`
   fixes the surface that every dispatch actually hits. A follow-up car can generalise
   `_shape_recall_results` into a shared helper once its shape has proven out in
   production.

5. **Denylist vs allowlist** — plan recommends **denylist** (§1). If you would rather
   have a hard field-count guarantee than forward-compatibility, say so and it flips
   to an allowlist, but then the landscape-fields test becomes load-bearing rather
   than a regression guard.
