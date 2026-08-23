# Protocol interfaces + typed payloads at the in-process seams that actually carry the coupling defects

**Date:** 2026-08-01
**Task:** #0116
**Status:** DRAFT — not started. Nothing implemented, nothing committed.
**Train:** **NONE. This is not a v5.172 train car and must not be folded into one.** It stands
alone, lands seam-by-seam, and each seam is independently revertible. A train car implies a shared
branch and a shared PR; this plan's whole risk control is that it is neither.
**Depends on:** nothing. **Blocks:** nothing.
**Sibling:** task #0117 measures transport cost (in-process vs HTTP/gRPC). **This plan assumes
nothing about 0117's answer** — it is deliberately transport-neutral, and §6 is the reason that is
cheap rather than speculative.

---

## 0. Verdict up front

Three facts decide everything below. Read them before any section.

**(1) This repo has no static type checker.** Not mypy, not pyright, not `ty`, not pyrefly — not in
`pyproject.toml`, not in `.pre-commit-config.yaml`, not in `.forgejo/workflows/`, not in the
`Makefile`. `ruff` runs `select = ["E","W","F","I","UP","B","C901","PLR0913"]`
(`pyproject.toml:280-286` region) — no `ANN`, no `TC`, nothing type-shaped.

> **Therefore: in this plan, "typed" means *runtime-enforced* or *lint-enforced*. Never
> annotation-enforced.** An annotation nobody checks is a comment. Every guarantee claimed below
> must cash out as a `TypeError`/`FrozenInstanceError`/`ValidationError` at call time, or as a
> `scripts/check_*.py` AST guard that fails pre-commit. If a proposal cannot be cashed out that
> way, it does not belong in this plan.

**(2) The seam that already went remote still passes untyped dicts.**
`yadgar/backend/embed_service/embed_service_models.py:88-91`:

```python
class RecallResponse(BaseModel):
    """Response body for POST /recall."""
    results: list[dict]
```

ADR-0078 sank all retrieval into the backend; core became an HTTP forwarder
(`yadgar/core/server/tools/recall.py:242` `_forward_to_backend`). The request side got a real
contract — `RecallRequest` with `model_config = {"extra": "forbid"}` and a `@field_validator`
(`:60-87`). The response side is `list[dict]`. **The transport did not buy the contract.** That
single line is the strategic argument for this whole plan: putting HTTP between two modules gives
you a socket, not a schema. The schema is a separate, cheaper decision, and it is the one that
prevents the defects.

**(3) A typed payload already exists at this exact boundary, and the pipeline throws it away.**
`yadgar/backend/retrieval/providers/base.py:44-73` defines `Candidate` — a proper normalized
dataclass with `type`, `id`, `title`, `content`, `native_score`, `directory_context`, `branch`. Its
own docstring says the escape hatch out loud (`:11-13`): *"``raw`` holds the provider's native dict
for lossless pass-through; the fan-out orchestrator (Step 2) returns raw dicts, not Candidates, so
existing recall callers see no schema change."* And it does:
`yadgar/backend/retrieval/recall_pipeline.py:170-183` (`_candidates_to_dicts`) unwraps `cand.raw`,
mutates it, `raw.pop("embedding", None)`, and returns `list[dict]`.

This is not a criticism of that code — the pass-through was a deliberate compatibility choice for a
gated rollout. It is the **empirical proof of §5's thesis**: a typed seam with an untyped escape
hatch and no enforcement reverts to dicts within one call frame. Enforcement is not the optional
part of this plan. It is the plan.

**The shape decision, in one table** (justified in §2):

| payload kind | shape | what enforces it at runtime |
|---|---|---|
| control / parameter objects crossing a module seam | `@dataclass(frozen=True, kw_only=True)` | `TypeError` on positional call; `FrozenInstanceError` on mutation |
| wire payloads (already at or plausibly at a process boundary) | pydantic `BaseModel`, `extra="forbid"` | `ValidationError` |
| bulk in-process candidate rows (N per call, 23 downstream write-sites) | **stays a `dict`** | AST guard + key constants — *not* a dataclass. §2.4 is the evidence. |

**If only one seam ever gets done: S2 (§3.2) — harden every cross-seam parameter object to
`frozen=True, kw_only=True` and ratchet it.** Rationale and the dissenting case in §3.6.

---

## 1. Problem — with evidence at HEAD

### 1.1 The three severe defects the audit named

`docs/plans/archive/bug-cause-audit-203-defects-2026-08-01.md` classified 203 shipped defects.
IN-PROCESS-COUPLING is 12 rows / 5.9% — small, but it holds the three the user's complaint
describes, and all three are untyped dict/positional passing across a module seam.

| audit id | defect | seam | live at HEAD? |
|---|---|---|---|
| `5056-4` | `retrieval/core.py` FTSParams caller — argument order broken by the yellow-batch refactor | param object | **class still live** — see §1.2 |
| `5097-2` | fusion `mem.pop("embedding")` starved MMR, forcing a per-candidate re-fetch | candidate row | **partially by design** — see §1.4 |
| `5086-4` | `resolved_by` edges never produced — extractor/handler type mismatch | positional tuple | fixed; seam unchanged — §1.5 |

### 1.2 `5056-4` — the param object is still positionally callable

`yadgar/backend/retrieval/scoring.py:44-53`:

```python
@dataclass
class FTSParams:
    """Cohesive parameter object for FTS signal collection."""
    query: str
    enabled_signals: object  # set | None
    open_domain_subqueries: list
    open_domain_mode: bool
    candidate_k: int
    min_heat: float
    branch_filter: BranchFilter | None = None
```

Plain `@dataclass`. Not `frozen`. Not `kw_only`. So `FTSParams("q", {"fts"}, [], False, 20, 0.0)`
still type-checks at runtime, and swapping `open_domain_mode` with `candidate_k` — both truthy
scalars — is silently accepted and silently wrong. The v5.55 refactor that introduced this object
was a genuine improvement (it collapsed a 9-positional-param signature), and it broke a caller in
exactly the way the object was meant to prevent, because the object itself was still positional.

**Repo-wide: 41 `@dataclass` declarations in production code, 11 `frozen=True`, and `kw_only`
appears ZERO times.** That is the ratchet opportunity, and it is why S2 is ranked first.

### 1.3 The carrier is typed; its contents are not

`yadgar/backend/retrieval/stages/base.py` already declares the interface —
`RetrievalStage(ABC)` with `apply(state: RetrievalState) -> RetrievalState` and `is_enabled`. The
pipeline runner is `yadgar/backend/retrieval/pipeline.py`. **The seam exists.** What crosses it does
not: `yadgar/backend/retrieval/state.py:40-68`

```python
scores: dict = field(default_factory=dict)          # memory_id -> {signal: score}
query_embedding: object = None
query_analysis: dict = field(default_factory=dict)
result_memories: list[dict] = field(default_factory=list)
stage_overrides: dict = field(default_factory=dict)
stage_stats: dict = field(default_factory=dict)
```

Every inner shape is documented in the class docstring (`:24-37`) and enforced by nothing. The
docstring is the schema. `query_embedding: object` is the tell.

### 1.4 The candidate row is polymorphic by provenance, and only prose records that

`5097-2`'s fix removed one `mem.pop("embedding")`. **Five survive**, and they are not equivalent:

| site | context |
|---|---|
| `fusion.py:309` | `_inject_ce_diversity` — CE-diversity injects, pop RETAINED |
| `fusion.py:390` | `_comparison_dual_search` — comparison-query results, pop RETAINED |
| `reranking.py:150` | rerank merge path |
| `reranking.py:348` | final strip before return |
| `recall_pipeline.py:182` | `_candidates_to_dicts` — fan-out output strip |

`fusion.py:339-343` states the invariant in a comment — *"the `embedding` bytes are intentionally
kept on the row here … so MMR (`_reranking_mmr._collect_candidate_embeddings`) can read it in-place
instead of re-fetching per candidate"* — and 30 lines earlier, in a helper called by that same
function (`:359`), `:309` pops it. `_reranking_mmr.py:38-46` documents the resulting fallback
honestly: *"Only fall back to `storage.get_memory` for candidates without an in-dict embedding —
e.g. CE-diversity / comparison injects that never went through the batched fusion hydration."*

**Be precise about this: it is NOT a live bug.** It is a deliberate, documented, measured
trade (parity for injected candidates at the cost of a per-candidate fetch for them). What it *is*
is the exact structural problem this plan targets: **one `list[dict]` holds rows with different key
sets depending on which code path produced them, and the only record of which is which is three
prose comments in three files.** `_reranking_mmr` gets this right today because someone wrote the
comment. Nothing makes the next reader.

`recall_pipeline.py:174-181` is the same shape, worse:

```python
existing_src = raw.get("_source")
if existing_src not in (None, "memory", "wiki"):
    pass  # keep the structured-knowledge annotation (profile, belief, …)
else:
    raw["_source"] = cand.type
```

That is a **discriminated union implemented as a string comparison against a comment**. `5059-2`
(recall heat-boost loop raised `KeyError` on synthetic profile/belief dicts injected into the shared
result list by the rerank merge) is that union going wrong.

### 1.5 The extractor → handler seam

`yadgar/_shared/knowledge_graph/knowledge_graph.py:45-59` — `_error_fix_entities` returns
`list[tuple[str, str, str]]`: `(name, entity_type, relationship_context)`.
`yadgar/backend/consolidation/cls.py:182-195` — `_apply_one_typed_relationship(self, name, ctx,
entity_map)` consumes `(name, ctx)` pairs and branches on `ctx == "resolved_by"` /
`ctx == "imports"`.

Three same-typed `str` slots in a positional tuple, produced in `_shared`, consumed in `backend`,
with the meaning of slot 2 vs slot 3 carried only by the docstring. `5086-4` was that meaning
getting crossed, and it shipped a feature that produced **zero** edges for an unknown span — the
worst failure signature in the corpus, because nothing errored.

`VALID_REL_TYPES` (`knowledge_graph.py:15-26`) exists as a `frozenset` but is not applied at the
boundary.

### 1.6 Process-global mutable state — the honest count

`grep -rEn 'os\.environ\[[^]]+\] *=' --include="*.py" yadgar/ | grep -v /tests/` returns **4 write
sites**, and one is a comment:

| site | verdict |
|---|---|
| `yadgar/__main__.py:133,135` (`YADGAR_HOST`, `YADGAR_PORT`) | entrypoint — allowed |
| `yadgar/_shared/embeddings/embeddings.py:191,199` (`HF_HUB_OFFLINE`) | **library code**, save/restore around a HF call. The one real offender. |
| `yadgar/core/server/routes/control.py:458` | a comment describing the removed `5089-1` write |
| `yadgar/core/scripts/nightly_cycle.py:57` (`setdefault`) | entrypoint — allowed |

`logging.disable` outside tests: **one site**, `yadgar/core/cli/_shared.py:16` — the CLI silencer,
an entrypoint helper by definition.

**The audit oversells this.** Its verdict §3 says a lint on `os.environ[...] =` and
`logging.disable` "kills 4 of the 12 coupling bugs outright." Reading the 12 IN-PROCESS-COUPLING
rows, that lint as literally specified reaches exactly **2**: `5545-17` (`logging.disable(CRITICAL)`
in `init_replay_lightweight`) and `5545-18` (control-API route mutating `os.environ`). Both are
already fixed. The other three the audit groups under "process-global mutable state" are a
different guard entirely — `5090-2` (unguarded `_query_cache` / breakers / `_enrichment_pipeline`
double-init) needs a module-level-mutable rule, `5094-1` (unbounded default executor) needs an
executor-injection rule, and `5008-1` is **browser JavaScript**, which no Python lint reaches.

So state the yield plainly: **this guard is a ratchet against regression, not a cleanup.** Its value
is that `5545-17` / `5545-18` / `5089-1` cannot come back. It deletes essentially no current code.
An enforcement section that oversells its own yield is what a cold reader discounts the rest of the
plan by.

---

## 2. The shape decision

### 2.1 The binding constraint (proof)

```
$ grep -rn "mypy\|pyright\|ty check\|pyrefly" pyproject.toml .pre-commit-config.yaml \
      .forgejo/workflows/*.yaml Makefile
(no output)
```

Every shape below is chosen against that.

### 2.2 Interfaces: `typing.Protocol`, in the home that already exists

**Decision: `typing.Protocol` in `yadgar/_shared/contracts/protocols.py`. Do not create a new
package, do not invent a new standard.**

That module's docstring already states the house standard (`:8-10`): *"Protocol lives in
``_shared``, the concrete implementation lives in the owning subpackage, and the object is injected
at the one composition root (``_shared/runtime/lifecycle.init_engines``)."* It already ships
`MLClientProtocol`, `CacheProtocol`, `StorageProtocol`, all `@runtime_checkable`, plus `NullCache` /
`NullMLClient` / `NullScopeVersions` null objects that exist specifically so a `_shared` consumer
never needs a `backend` import. This plan extends that module. It introduces no new pattern.

Honest limit: `@runtime_checkable` `isinstance()` checks **method names only**, never signatures.
That is worth having (it catches "you passed the wrong object") and worth not overstating (it does
not catch "you passed the right object with a changed signature"). Where signature drift is the
risk, the guarantee comes from §2.3's `kw_only`, not from the Protocol.

**Protocol vs the existing `ABC`.** `RetrievalStage` (`stages/base.py`) and `SourceProvider`
(`providers/base.py`) are `ABC`s today. **Do not convert them.** `ABC` gives a stronger runtime
guarantee than `Protocol` — `@abstractmethod` refuses instantiation of an incomplete subclass, which
is real enforcement with no checker. Protocol's advantage is structural typing for objects you do
not own, which is not the situation at either seam. Rule for this plan: **`ABC` where we own every
implementation and inherit; `Protocol` where the dependency crosses a layer and is injected.**

### 2.3 Control / parameter payloads: `@dataclass(frozen=True, kw_only=True)`

`kw_only=True` is the highest-value single token in this plan.

> With `kw_only=True`, `FTSParams(query, signals, subqueries, mode, k, heat)` raises
> `TypeError: __init__() takes 1 positional argument but 7 were given` **at call time, in every
> environment, with no type checker.** A positional-order break — `5056-4`, the user's exact
> complaint, "a single refactor to a function creates cascades of bugs" — becomes structurally
> impossible rather than statically detectable.

`frozen=True` adds `FrozenInstanceError` on post-construction mutation, so a param object cannot be
used as a covert out-parameter.

Cost: none in the hot path. These objects are constructed **once per call**, not once per candidate.
`__slots__` is available too but is a behaviour change around `__dict__` and is out of scope.

### 2.4 Bulk candidate rows: they stay `dict`. This is the load-bearing negative decision.

The tempting move is a frozen `MemoryRow` dataclass. **It is wrong, and the evidence is countable.**
Write-sites into candidate rows downstream of hydration:

| file:line | write |
|---|---|
| `fusion.py:308`, `fusion.py:349` | `mem["_retrieval_score"] = …` |
| `fusion.py:309`, `fusion.py:390` | `mem.pop("embedding", None)` |
| `_reranking_heuristic.py:146,150` | `_rerank_score`, `_retrieval_score` |
| `_reranking_cross_encoder.py:88,89` | `_cross_encoder_score`, `_retrieval_score` |
| `_reranking_nli.py:30,37,41` | `_nli_entailment_score` |
| `_reranking_multi_passage.py:54` | `_retrieval_score` += boost |
| `reranking.py:150,348` | `pop("embedding")` |
| `reranking.py:181,240,271` | `_retrieval_score`, `_retrieval_confidence`, `temporal_links` |
| `recall_pipeline.py:182` | `pop("embedding")` |
| `recall_pipeline.py:274,291` | `_retrieval_score` |
| `recall_pipeline.py:607,608` | `heat`, `last_accessed` |

**23 write-sites across 8 modules.** `frozen=True` on the row converts every one into a
`dataclasses.replace()` plus a rebind, across the entire reranking stack, in one change. That is a
rewrite, not a refactor, and it directly contradicts §7's incremental-and-revertible constraint.
Any plan that proposes it has not counted.

Second reason, structural: **21 of the 23 writes are derived-score annotation with a `_`-prefixed
key.** The row is not being *edited*; it is being *annotated*. `RetrievalState.scores` already does
annotation correctly for the pre-fusion signals — a side map `memory_id -> {signal: score}`
(`state.py:49`). Post-fusion the code abandons that pattern and stamps onto the row. The principled
end-state is one annotation map for the whole pipeline. **That is a big refactor and it is NOT in
this plan** (§12) — but naming it stops a future implementer from reaching for `frozen` and calling
it the same thing.

So the row's guarantees come from lint, not from a class:

1. **Key constants.** `_retrieval_score` is a magic string, and its reach is **wider than the
   retrieval package** — verified. It is read *and written* in three other packages:
   `yadgar/core/server/tools/wiki.py:722-724` (`r["_retrieval_score"] = base * 1.5`, then sorts by
   it), `yadgar/_shared/rules_engine/rules_engine.py:229-234` (boost/penalty writes it in place)
   and `:385` (sorts by it), `yadgar/_shared/wiki/store.py:1025`. `rules_engine.py:369` even states
   the contract in prose — *"memories: List of memory dicts (must have `_retrieval_score`)"*.

   **So this key is a cross-package annotation contract spanning `backend`, `core` and `_shared`,
   not a retrieval-package detail.** A typo silently creates a new field and silently drops the
   candidate's score to a `.get(…, 0.0)` default — in any of four packages. Define the constants
   once in `yadgar/_shared/contracts/` (a `retrieval_keys.py` or equivalent — `_shared` is the only
   placement all three consumers may import under ADR-0057) and scope the guard to **all four
   packages**, not to `yadgar/backend/retrieval/` alone. §5.1 carries the scope decision; a guard
   covering half the write-sites is the "green but vacuous" failure this plan is supposed to avoid.
2. **A pop guard.** `.pop("embedding")` / `del row["embedding"]` inside `yadgar/backend/retrieval/`
   requires an allowlist entry with a reason. The five existing sites are the seed allowlist, each
   carrying the reason already written in its neighbouring comment. This makes the §1.4 prose
   invariant machine-checked for the first time.
3. **A provenance discriminator.** `_source` already exists and is already the discriminator
   (`recall_pipeline.py:174-181`). Promote the legal values to a constant set and validate at the
   two places rows enter the shared list, rather than comparing against a comment.

### 2.5 Wire payloads: pydantic, matching the house precedent

`embed_service_models.py` is already the idiom — `BaseModel`, `model_config = {"extra": "forbid"}`,
`@field_validator`. Extend it; do not introduce a second serialization library.

`RecallResponse.results: list[dict]` → a `RecallResultItem` model. `extra="forbid"` on the item
turns a foreign dict shape into a `ValidationError` at the boundary — `5059-2`'s failure mode,
caught where it enters rather than where it explodes.

**Honest cost, stated because it is the one place validation is not free:** this validates N rows
per recall, on the response path, on every call. Measure before landing (§8), and if it is material,
the fallback is `model_construct()` (skips validation) in production with full validation forced on
in tests via a config knob — which keeps the schema as documentation-plus-test-gate and drops the
production tax to ~zero. Decide with a number, not a preference. **Do not land the item model
unmeasured.**

### 2.6 Rejected alternatives

| option | verdict |
|---|---|
| **`TypedDict` for row payloads** | **Rejected — the central rejection.** Zero-copy and zero-cost, and mypy would genuinely catch `mem.pop("embedding")` on a required key and a wrong-shape row via a discriminated union. **But this repo runs no type checker, so `TypedDict` enforces literally nothing at runtime.** It would be an annotation that reads like a guarantee. That is worse than an honest `dict`. |
| **Adopt mypy repo-wide** | Rejected. ~100k LOC, essentially unannotated, no gradual-typing history. A multi-month project with its own failure modes, and it would gate this plan behind itself. |
| **Scoped mypy (seam modules only)** | Rejected, but worth the line. It would make `TypedDict` real for ~8 files. Rejected because a CI gate covering 8 files out of 400 is exactly the "green but vacuous guard" the #0110 plan spends §6.4 on: it reports type-safety while 98% of the code is unchecked, and the first `# type: ignore` under deadline pressure retires it silently. Revisit only as a deliberate standalone decision with its own ADR, never as a rider on this plan. |
| **Frozen dataclass for candidate rows** | Rejected — §2.4, 23 write-sites. |
| **pydantic everywhere, including in-process rows** | Rejected. Validation per row per stage on the hot path, for a payload that never leaves the process. §2.5 already flags this as the one place to measure; extending it to every stage boundary multiplies the cost by the stage count for no additional guarantee. |
| **`attrs` / `msgspec` / protobuf** | Rejected. New dependency on a path that already has pydantic and stdlib `dataclasses`, and (protobuf) a codegen step. ADR-0183's *"the mechanism is an INTERFACE, not a query compiler"* reasoning applies by analogy: hand-written contracts beat a generated layer here. |
| **Do nothing; rely on tests** | Rejected — this is the status quo that produced the 12 rows. |

### 2.7 Optional and evolving fields

- **Additive-with-default only.** A new field on a frozen dataclass or pydantic model must have a
  default. Then every existing construction site keeps working untouched. `kw_only=True` removes the
  ordinary blocker here (no "defaults must come last" ordering constraint), which is a second
  independent reason to adopt it.
- **Removing or renaming a field is a breaking change** and needs the same treatment as a signature
  change: find every construction site, change them in the same commit. `kw_only` makes this *loud*
  (`TypeError: unexpected keyword argument`) instead of silent.
- **`extra="forbid"` on wire models is deliberate strictness.** A newer client sending an unknown
  field gets a `400`, not a silent drop. `RecallRequest` already chose this (`:78`); stay consistent.
  The consequence — request models must be additive-with-default and old servers reject new fields —
  is the correct trade when both sides ship in one wheel, which they do.

### 2.8 What "versioned" means for an in-process contract

**Nothing. Do not add a version field to in-process payloads.**

A version field is only meaningful when two independently-deployed sides can disagree. In-process
they cannot: one wheel, one import graph, one commit. A `version: int = 1` on `FTSParams` would be a
field that no code ever branches on, which is dead capability — and `scripts/check_dead_capability.py`
exists precisely to catch that.

Where versioning becomes real, it is already solved by the existing mechanism:

- **At the wire**, the pydantic model *is* the version, and `extra="forbid"` is the enforcement.
  Skew between independently-deployed sides is a **deployment** problem (audit rows `U-09`, `U-12`,
  `4612-1` — core and backend versioning independently), not a payload-schema problem, and it is
  handled by the version-pinning machinery (`scripts/check_backend_bump.py`, `check_versions.py`)
  that already exists.
- **A Protocol subclass** (`RetrievalStageV2(RetrievalStage)`) is the right tool if and only if two
  incompatible implementations must coexist in one process. That is a real situation the day a
  seam is being migrated behind a flag — and it is the *only* situation. Not now.

Record this as an explicit decision in the ADR (§10), because "should we version it?" is the first
question a future reader asks and the honest answer is counter-intuitive.

---

## 3. The seams — ranked, with justification

Selection criteria, applied in order: (a) does it carry one of the audit's coupling defects,
(b) is it a plausible future service boundary, (c) is the fan-in high enough that a break cascades.
A seam scoring only (c) is not enough — `runtime_config_client.get` has fan-in 1350 and typing it
buys nothing (§12).

### 3.1 S1 — the retrieval stage seam: carrier + row (HIGHEST VALUE, HIGHEST COST)

**One seam, two artifacts.** `RetrievalState` (the carrier) and the candidate row (the payload) are
the same boundary seen twice. Splitting them across two efforts produces two half-migrations. One
plan section, one revert.

**Carries:** `5097-2`, `5059-2`, and the `5080-x` fan-out family (`5080-1` double-CE-rerank,
`5080-2`/`5080-3` fan-out path diverging from legacy).
**Service-boundary relevance:** high — this is the pipeline that already lives behind
`POST /recall` (ADR-0078).
**Fan-in:** every stage in `yadgar/backend/retrieval/stages/` (14 modules) plus
`recall_pipeline.py`.

| artifact | change | shape |
|---|---|---|
| `state.py` `RetrievalState` | type the inner shapes; `query_embedding: object` → a named type; keep the dataclass **mutable** (stages mutate it by contract, `base.py` docstring) | dataclass, `kw_only=True`, **not** frozen |
| candidate row | key constants + pop guard + `_source` discriminator | stays `dict` (§2.4) |
| `RetrievalStage` | stays `ABC`; add `__init_subclass__` or a guard asserting `name` is set | ABC |

**Size:** L. **Sequencing:** last. Do S2 and S3 first — they establish the idiom on small surfaces.

### 3.2 S2 — cross-seam parameter objects (SMALLEST DIFF, STRONGEST RATCHET)

**Carries:** `5056-4` directly — the user's named complaint.
**Fan-in:** 41 dataclasses repo-wide; the cross-seam subset is the target, not all 41.

Concrete initial set (verify each is genuinely cross-module before touching it):

| object | file |
|---|---|
| `FTSParams` | `yadgar/backend/retrieval/scoring.py:45` |
| `RerankContext` | `yadgar/backend/retrieval/reranking.py:41` |
| `Scope` | `yadgar/backend/retrieval/providers/base.py:24` |
| `CurateParams` | `yadgar/backend/curation/__init__.py:43` |
| `NewMemorySpec` | `yadgar/backend/curation/ingestion.py:20` |
| `MemorizeContext` | `yadgar/_shared/write_exec/context.py:9` |
| `WikiAddOptions` | `yadgar/_shared/wiki/contract.py:33` |
| `CheckpointContext` | `yadgar/_shared/restoration/contract.py:12` |
| `DrainerConfig` | `yadgar/backend/queue_drainer/__init__.py:104` |
| `RelationshipMeta` | `yadgar/_shared/storage/entity.py:13` |

Change per object: `@dataclass` → `@dataclass(frozen=True, kw_only=True)`, then fix every
construction site the `TypeError` surfaces. `frozen` is the part that can fail — if an object is
mutated after construction, either that mutation is the bug (fix it) or the object is state not a
parameter (leave it mutable, `kw_only` only, and say so in a comment).

**Size:** S per object, M for the sweep. **Parallelisable:** yes, one object per commit.
**This is the seam to do if only one gets done** — §3.6.

### 3.3 S3 — the extractor → handler seam (BEST PROOF-OF-PATTERN)

**Carries:** `5086-4`.
**Crosses:** `yadgar/_shared/knowledge_graph/` → `yadgar/backend/consolidation/` — a real layer
boundary, import-linter-enforced.

`list[tuple[str, str, str]]` → `list[EntityTriple]`, a frozen kw-only dataclass with `name`,
`entity_type`, `relationship_context`, validated against the existing `VALID_REL_TYPES`
frozenset (`knowledge_graph.py:15-26`) in `__post_init__`. Placement: `_shared/contracts/` per the
house standard, or `_shared/knowledge_graph/` if the producer owns it — pick one and note it in the
ADR; do not leave it ambiguous.

**Size:** S. **Blast radius:** 2 modules. **Coverage:** exists (§4).

### 3.4 S4 — the recall wire payload

**Carries:** nothing directly, but it is §0(2) — the seam where the strategic claim is proven or not.
`RecallResponse.results: list[dict]` → `list[RecallResultItem]`.

**Size:** M. **Gated on a measurement** (§2.5). **Depends on** S1's `_source` discriminator work,
because the item model needs the legal `_source` values to be a settled set first.

### 3.5 S5 — process-global state (a lint, not a seam)

§1.6 for the honest yield. Guard design in §5.2. **Size:** S. **Independent of everything.**

### 3.6 If only one seam gets done: **S2**

The advisor review of this plan argued for **S3** — self-contained, existing coverage, two modules,
lowest risk of not landing green, and it proves the pattern end-to-end. That is a good argument and
a cold reader should weigh it.

**This plan ranks S2 first anyway, and here is the tradeoff stated plainly:**

- S3 **fixes one defect in one place**. S2 **retires a defect class**. `kw_only=True` makes
  positional-order breakage structurally impossible for every object it touches, and the §5.1 guard
  makes every *future* param object born that way — the "born portable" discipline ADR-0183 chose
  for new tables, applied to new parameter objects.
- S2 is the smallest diff with the largest guarantee: one decorator argument per object, then fix
  whatever `TypeError` surfaces. Zero behaviour change by construction.
- S2 addresses the user's stated pain **verbatim**. "A single refactor to a function creates cascades
  of bugs" is `5056-4` is `FTSParams`. If exactly one thing ships, it should be the one that answers
  the complaint that started this.
- S3's advantage — proving the pattern — only pays if a second seam follows. If exactly one ships,
  proving a pattern nobody extends is worth less than a permanent ratchet.

**Do S2 first, S3 second.** S3 remains the right *second* seam for exactly the advisor's reasons,
and it is the one that seeds the frozen-payload idiom S1 later needs.

---

## 4. Per-seam TDD story, coverage honesty, and rollback

**CI gates BY DIRECTORY** — `.forgejo/workflows/ci-pr.yaml`:
`test-fast` = `yadgar/tests/{scripts,server,hooks,_meta,clients}/` (`:79-83`) · `test-shared` =
`yadgar/tests/_shared/` (`:128`) · `test-backend` = `yadgar/tests/backend/` (`:180`) · `test-core` =
`yadgar/tests/core/` (`:259`).

**`yadgar/tests/integration/` is NOT gated by those four jobs** — a test placed there is not run by
any of them. Two precise carve-outs so nobody mis-reads this: `:310` runs `yadgar/tests/` but
**only `-m perf`**, and a separate viz job runs `yadgar/tests/integration/viz/` with `-m integration`
(`:535`). Nothing in this plan belongs in either. **A new test outside the four gated directories is
a test that does not run.**

**Placement trap specific to this plan:** the retrieval code lives in `yadgar/backend/retrieval/`
but most of its tests live in **`yadgar/tests/_shared/`** — `test_retrieval.py` (702 lines),
`test_retrieval_pipeline.py` (673), `test_fts_scores_params.py` (288), `test_reranking_mmr.py` (322),
`test_reranking.py`, `test_reranking_cross_encoder.py`, `test_reranking_heuristic.py`. Residue of the
ADR-0060/0062 reorg, which moved the code and not the tests. They ARE gated (by `test-shared`), so
this is not a hole — but an implementer who "puts the new test next to the code" in
`yadgar/tests/backend/` splits the suite further. **Put each new test beside the tests for the code
it covers, not beside the code.**

### 4.1 S2 — parameter objects

- **Existing coverage: GOOD, and it is the model.** `yadgar/tests/_shared/test_fts_scores_params.py`
  self-describes (`:1-8`) as *"Characterization tests for `_collect_fts_scores` after FTSParams
  refactor … verifies FTSParams interface produces identical output to the old positional 9-param
  signature."* It already constructs `FTSParams(query=…, enabled_signals=…, …)` with **keywords
  throughout**, so `kw_only=True` is a no-op for it — which is exactly the desired signal: green
  before, green after, behaviour pinned.
- **RED first:** `test_fts_params_rejects_positional_construction` — `pytest.raises(TypeError)` on
  `FTSParams("q", None, [], False, 20, 0.0)`. RED today (it constructs fine and is silently wrong).
  This is the test that *is* the fix; if it can pass with `kw_only` removed, it is mis-written.
- **RED:** `test_fts_params_is_immutable` — `pytest.raises(dataclasses.FrozenInstanceError)`.
- **Per-object:** the same two tests. Consider one parametrized test over the §3.2 list, which
  doubles as the ratchet's test surface.
- **Coverage gap, named:** `RerankContext`, `CurateParams`, `NewMemorySpec`, `DrainerConfig` have no
  dedicated construction tests. For those, **write the characterization test before the decorator
  change** — `test_fts_scores_params.py` is the template.
- **Rollback:** revert the decorator argument. One token per object. No behaviour to unwind.

### 4.2 S3 — extractor → handler

- **Existing coverage: GOOD for the triple shape — and it will all go RED. Verified, not assumed.**
  `yadgar/tests/_shared/test_knowledge_graph.py` (298 lines), class `TestTypedEntityExtraction`
  (`:185+`) has four tests that assert on the triple: `test_import_relationship_context` (`:194`),
  `test_def_and_call_pattern` (`:202`), `test_error_fix_pattern` (`:211`), `test_decision_pattern`
  (`:217`).

  **They assert by positional index** — `names = [r[0] for r in results]`,
  `imports = [r for r in results if r[2] == "imports"]`. So converting to `EntityTriple`
  **breaks all four**, and each must be rewritten to named-field access (`t.name`,
  `t.relationship_context`). That is not a coverage gap; it is the migration cost, and it is the
  *good* kind — those four tests are precisely what proves the conversion is faithful. But "coverage:
  GOOD" must not be read as "tests stay green." **Budget the rewrite: 4 tests, mechanical, same
  commit as the dataclass.**

- **Verified gap that changes S3's test list.** `test_error_fix_pattern` (`:211-214`) asserts only
  `len(resolved) >= 1` — that *an* entity carries `resolved_by`. It does **not** assert the
  `solution`-typed entity is also emitted, and `5086-4` was exactly that: the handler
  (`cls._apply_one_typed_relationship`) searched for a `solution` entity, found none, and produced
  zero edges while every extraction test stayed green. **This confirms the end-to-end edge test below
  is genuinely absent and is the highest-value test in S3.**
- **RED:** `test_error_fix_entities_returns_typed_triples` — assert `EntityTriple` instances with
  named fields, and that `relationship_context` is in `VALID_REL_TYPES` or empty.
- **RED:** `test_invalid_relationship_context_rejected` — `pytest.raises(ValueError)` on
  construction with a junk context. This is the `5086-4` regression: a type/context mismatch becomes
  a construction error instead of zero edges.
- **RED (the real `5086-4` regression):** end-to-end — an error-fix sentence with a resolution clause
  produces a `resolved_by` edge. Assert the **edge**, not the triple. A test that only checks the
  triple would have passed while `5086-4` shipped.
- **Rollback:** the dataclass is additive; revert the producer and the two consumer call sites.

### 4.3 S1 — stage seam

- **Existing coverage: STRONGER than expected on output equality, absent on row shape. Verified.**
  `test_retrieval_pipeline.py` (673 lines) covers the plugin architecture — interface, profile
  selection, per-call overrides, metrics, A/B compare. The load-bearing guard is real:
  **`class TestPipelineMatchesMonolithicRecall` at `:581`**, six tests (`:598-651`), **no `skip` or
  `xfail` anywhere in the file** (the only `skip` matches are two test *names*, `:323` and `:360`).
  `test_fastapi_query_matches` (`:598`) asserts **both** that the pipeline's content order equals the
  monolithic path's **and** that every `_retrieval_score` matches to `abs(p - m) < 1e-6`, over
  deterministic stub embeddings (`_DeterministicEmbeddings`, `:44+`).

  **That is a genuine end-to-end behaviour-preservation gate and S1 can rely on it.** Score equality
  to 1e-6 across the whole pipeline is exactly the assertion a payload refactor needs.
- **Named gap:** nothing tests the *row shape contract* — that a row produced by CE-diversity
  injection and a row produced by batch hydration are interchangeable downstream, which is exactly
  §1.4. `test_reranking_mmr.py` (322 lines) covers MMR mechanics; whether it exercises the
  mixed-provenance list is **unverified — check first, and if not, that characterization test is
  the first commit of S1.**
- **RED:** `test_candidate_row_keys_are_constants` (AST-level: no `"_retrieval_score"` literal
  outside the constants module) and `test_embedding_pop_sites_are_allowlisted`.
- **RED:** `test_mixed_provenance_rows_survive_mmr` — build a `result_memories` containing one
  hydrated row (with `embedding`) and one injected row (without), run MMR, assert both survive and
  the injected one triggers exactly one `storage.get_memory` fallback. This pins §1.4's documented
  invariant as a test for the first time.
- **Rollback:** the constants module and the guard are additive; the guard's allowlist can be
  widened to make it a no-op without reverting code.

### 4.4 S4 — wire payload

- **Existing coverage:** `yadgar/tests/core/test_backend_recall_*.py`, `test_recall_pipeline_unit.py`,
  `test_recall_output_cap.py` exist. **Verify what they assert** — output cap and engine wiring is
  not the same as response-shape.
- **RED:** `test_recall_response_item_rejects_unknown_field` (`ValidationError` under
  `extra="forbid"`), and `test_structured_knowledge_source_values_are_accepted` — pin `profile` and
  `belief` as legal `_source` values so `5059-2` cannot recur as a *rejection* instead of a
  `KeyError`. **That inversion is the specific risk of this seam**: tightening the contract can turn
  a silent corruption into a loud outage. Test both directions.
- **A latency measurement is a deliverable of this seam, not an afterthought** (§2.5, §8).
- **Rollback:** revert to `list[dict]`. The item model is additive.

### 4.5 S5 — the lint

- **RED:** a fixture file containing `os.environ["X"] = "y"` at module scope in a non-entrypoint path
  → guard exits non-zero. Plus: an allowlisted site passes; an allowlist entry with an empty reason
  fails; **an unused allowlist entry fails** (stale-allowlist guard — house law, `#0110` §6.2).
- **Rollback:** remove the pre-commit hook entry. Zero production code touched.

---

## 5. Enforcement — the part that decides whether this rots

§0(3) is the evidence: `Candidate` exists, is bypassed by `raw: dict`, and nothing objects.

### 5.1 The guard that makes bypassing the contract fail CI

**Idiom: a `scripts/check_*.py` AST guard, pre-commit-wired, with a reasoned allowlist.** 27 such
scripts exist and all 27 are wired in `.pre-commit-config.yaml`. ADR-0183's consequences
explicitly call for this shape: *"import-linter's contracts are import-graph only and cannot see
call sites or query literals … an AST guard with an allowlist for pre-existing violations must be
written."* Same reasoning applies here — import-linter cannot see that `FTSParams` is constructed
positionally, because that is a call, not an import.

Proposed: **one** new script covering the payload discipline (do not ship four scripts):

| rule | scope | failure |
|---|---|---|
| a `@dataclass` in the declared seam module set must carry `kw_only=True` | seam modules (explicit list, not a glob) | missing → fail, with the allowlist reason if exempt |
| `"_retrieval_score"` / `"_rerank_score"` / `"_cross_encoder_score"` / `"_nli_entailment_score"` as string literals outside the constants module | **`yadgar/backend/retrieval/` + `yadgar/core/server/tools/` + `yadgar/_shared/rules_engine/` + `yadgar/_shared/wiki/`** — §2.4 verified write-sites in all four | fail |
| `.pop("embedding")` / `del x["embedding"]` | `yadgar/backend/retrieval/` | fail unless allowlisted with a reason |

**Why the score-key scope is four packages and the pop-guard scope is one.** The score key is a
shared annotation contract with verified writers in `backend`, `core` and `_shared` (§2.4). The
`embedding` pop is a retrieval-pipeline invariant only — the five sites are all inside
`yadgar/backend/retrieval/`, and stripping `embedding` at an output boundary is correct behaviour
elsewhere. Scoping each rule to where its invariant actually holds is what keeps the allowlist
short enough to stay honest.

Non-negotiable allowlist rules, matching house law:

- keyed by `file:qualname`, not by file
- **every entry carries a non-empty reason string; the guard asserts that.** A blank reason is how an
  allowlist rots into a mute permit list
- **an unused entry fails.** The repo already treats a stale allowlist entry as a hard failure
  (`scripts/check_allowlist_audit.py`, `check_complexity_allowlist.py`)
- the seam module list is **explicit and short**. A glob over `yadgar/**` makes this an unbounded
  migration and it will be disabled within a month

**Rejected: an import-linter contract.** Four contracts exist and they are the right tool for *layer*
questions. They cannot see construction sites. Not applicable.
**Rejected: a runtime assertion in `__post_init__` on every payload.** Cost on the hot path, and it
fires at runtime in production rather than at commit time.

### 5.2 The process-global lint

Second guard (or a second rule in the same script — decide at implementation, but keep the allowlist
mechanics identical):

- `os.environ[...] = ` / `os.environ.update(...)` / `os.environ.setdefault(...)` outside a declared
  entrypoint set
- `logging.disable(...)` outside the same set

**Entrypoint set (explicit, small, allowlisted with reasons):** `yadgar/__main__.py`,
`yadgar/core/cli/**` (CLI entry — `_shared.py:16` is the `logging.disable` site),
`yadgar/core/scripts/nightly_cycle.py`. Everything else is library code.

Known seed allowlist entries, with the reasons already discoverable in the code:
`yadgar/_shared/embeddings/embeddings.py:191,199` — `HF_HUB_OFFLINE` save/restore around a HF call.
That is the one genuine library-code offender; the allowlist entry should say so and name the
alternative (pass offline mode as a parameter) as the eventual fix rather than pretending it is fine.

**Yield: 2 of 12 audit rows, both already fixed** (§1.6). This is a ratchet. Say so in the commit.

### 5.3 What enforcement deliberately does NOT cover

`5090-2` (module-level mutable singletons), `5094-1` (unbounded default executor), `5008-1` (browser
global scope). Each needs a different guard, and inventing three more here would make this plan the
thing nobody starts. Name them as follow-ups; do not scope them.

---

## 6. The transport-swap story — one worked example

### 6.1 What ADR-0078 actually did, and what it did not

ADR-0078 is the precedent that worked: all DB reads/writes moved into backend pipelines, core became
an HTTP forwarder. Mechanically:

- `yadgar/core/server/tools/recall.py:242` — `_forward_to_backend(...)` `POST`s to
  `{YADGAR_EMBED_URL}/recall` (`:314-315`)
- `:467-468` — *"Phase 2a: forward-only — raise loud on backend error (no in-core fallback)"*
- the changelog records `_st._retriever is None` in core — no dead in-core path survived

**Callers of the MCP `recall()` tool did not change.** But note *why*: the return type was
`list[dict]` before and `list[dict]` after. The swap was easy **because the payload was untyped** —
it was easy for the wrong reason. `RecallResponse.results: list[dict]` is the receipt. The next swap
gets the same ease *and* a schema only if the payload is typed first.

### 6.2 The worked example: S3 in-process today, remote tomorrow

Take the extractor→handler seam. Today (S3 landed):

```
# _shared/contracts/protocols.py
@runtime_checkable
class EntityExtractorProtocol(Protocol):
    def extract(self, text: str) -> list[EntityTriple]: ...

# _shared/knowledge_graph/  — the concrete
class RegexEntityExtractor:      # implements EntityExtractorProtocol
    def extract(self, text: str) -> list[EntityTriple]: ...

# backend/consolidation/cls.py — the consumer
def __init__(self, ..., extractor: EntityExtractorProtocol): ...
```

`EntityTriple` is a frozen kw-only dataclass with three named `str` fields and a `__post_init__`
validating `relationship_context` against `VALID_REL_TYPES`.

Later — if 0117's measurement and product need justify it — the swap is:

```
class RemoteEntityExtractor:     # implements the SAME Protocol
    def extract(self, text: str) -> list[EntityTriple]:
        resp = httpx.post(f"{self._base}/extract", json={"text": text})
        return [EntityTriple(**t) for t in resp.json()["triples"]]
```

and one line changes at the composition root (`_shared/runtime/lifecycle.init_engines`), which is
already where `LocalMLClient` vs `RemoteMLClient` is selected — the identical pattern, already
shipped, already import-linter-waived for exactly this reason
(`pyproject.toml:297-300`). `cls.py` does not change. The pydantic wire model on the remote side is
derived from `EntityTriple`'s fields, so the schema is written once.

**The three properties that make this a swap and not a rewrite, in order of importance:**

1. **The consumer names the Protocol, never the concrete.** This is the whole thing.
2. **The payload is a named type with named fields.** `EntityTriple(**t)` is a two-line
   deserializer. `list[tuple[str, str, str]]` would deserialize by *position* — and JSON round-trips
   through lists, so a producer-side field reorder silently corrupts the consumer. That is `5086-4`
   again, promoted to a network bug.
3. **Selection happens at exactly one place.** Already true here.

### 6.3 What this deliberately does not claim

It does not claim the split is *worth* doing — the audit's verdict is 3.4:1 against, and 0117
measures the cost. It claims only that **if** it is ever done, a typed seam makes it an
implementation swap, and an untyped one makes it a rewrite of every consumer. That asymmetry is why
the discipline is worth adopting now regardless of 0117's answer.

---

## 7. Migration strategy, sequencing, effort

**Every seam lands green, alone, on its own branch, revertible in one commit. No big bang.** 203
recent defects are the evidence that large changes in this codebase go wrong; a plan that ignores
its own motivating data is not credible.

| order | seam | size | parallel? | why here |
|---|---|---|---|---|
| 1 | **S2** param objects | S per object, M total | yes — one object per commit | smallest diff, largest guarantee, answers the user's complaint. §3.6 |
| 2 | **S5** process-global lint | S | yes — touches no production code | fully independent; land it whenever |
| 3 | **S3** extractor→handler | S | serial after S2 | proves the frozen-payload idiom on 2 modules |
| 4 | **S1** stage seam | L | serial | needs S2/S3's idiom settled; biggest blast radius |
| 5 | **S4** wire payload | M | serial after S1 | needs S1's `_source` discriminator set to be settled first, and a measurement |

Parallel-safe: S2 objects against each other, and S5 against everything. Serial: S3 → S1 → S4.

**Per-seam definition of done:** the seam's RED tests are green, the full directory-gated suite for
the touched directories is green, pre-commit is clean, and the change is one revertible commit.
**Do not batch two seams into one PR.**

**Effort is deliberately given as size classes, not hours.** S = a session. M = a day. L = several
days with a real chance of discovering the `state.py` inner shapes are not what the docstring says.

---

## 8. Verification

**All of it is local.** No VM, no container, no infrastructure. This plan touches no install, no
systemd, no deployment surface — which is itself a reason it is low-risk relative to the recent
train cars.

1. Per-seam RED tests green (§4).
2. Directory-gated suites for touched dirs: `pytest yadgar/tests/_shared/` for S1/S2/S3,
   `yadgar/tests/core/` + `yadgar/tests/backend/` for S1/S4.
3. `test_retrieval_pipeline.py::TestPipelineMatchesMonolithicRecall` (`:581-659`) must stay green
   across S1. **This is the single most important behaviour-preservation check in the plan** — it
   asserts pipeline-vs-monolithic content order AND per-result `_retrieval_score` equality to 1e-6.
   Verified present and unskipped at HEAD (§4.3), so this is a gate to hold, not a gap to fill.
4. Pre-commit clean — including `import-linter`, `check-complexity`, `check-allowlist-audit`,
   `check-dead-capability`.
5. **S4 only: a latency number.** Recall p50/p95 with and without the response-item model, on a
   realistic corpus. Land `model_construct()` or full validation based on that number (§2.5). **A
   plan step that says "measure" and ships unmeasured is the step that gets skipped — make the
   number a required artifact in the PR body.**
6. `make eval` / the retrieval quality gate, if it covers the recall path — **verify what it
   actually gates before relying on it.** Audit row `5079-1` records `benchmarks/run_eval.py` calling
   `retriever.recall()` directly, making every `make eval` gate vacuous. Confirm that is fixed rather
   than assuming.

---

## 9. Rollback

Per-seam, in §4. The general property: **every change in this plan is either a decorator argument, an
additive class, or a lint script.** None changes control flow. Reverting any seam restores exactly
prior behaviour, and reverting one seam does not disturb another — which is the entire reason for the
seam-by-seam structure.

The one asymmetry worth naming: **S4 is the only seam that can cause a production outage on rollout
rather than a test failure.** `extra="forbid"` converts an unexpected field from a silent pass into a
`ValidationError`. If an unforeseen `_source` value or an extra key exists in real data that the test
corpus lacks, recall starts failing where it previously degraded. Mitigation: land S4 behind a config
knob defaulting to permissive (`model_construct`, no validation) with validation forced on in tests,
then flip the default in a separate commit after a soak. **Do not land S4 strict on day one.**

---

## 10. ADRs

**A new ADR is warranted, and it should be written before S2 lands, not after.** The interface-shape
decision is exactly the kind of thing a future reader will otherwise re-litigate, and §2.8's answer
("do not version in-process payloads") is counter-intuitive enough to need a recorded reason.

It must state:

- **the constraint that decides everything: no static type checker exists**, therefore typed means
  runtime- or lint-enforced (§0(1)). This is the sentence the whole ADR hangs on
- the shape per payload kind (§0 table), and the **rejection of `TypedDict`** with its reason
- **`kw_only=True` as the primary mechanism**, because it converts positional-order breakage into a
  call-time `TypeError`
- the negative decision on frozen candidate rows, with the 23-write-site count as evidence (§2.4)
- **no version fields on in-process payloads** (§2.8), and what versioning means at the wire instead
- consequences: one new `scripts/check_*.py` guard with reasoned-allowlist mechanics; the seam module
  list becomes a maintained artifact; the annotation-map end-state is named as explicitly deferred

**Existing ADRs — read before writing, all four:**

| ADR | relationship |
|---|---|
| **ADR-0183** (design against interfaces, born portable) | **The direct parent.** Its *"the mechanism is an INTERFACE, not a query compiler"* and *"NEW tables are born portable … expensive to retrofit"* reasoning is this plan's reasoning applied to payloads instead of tables. Its consequences already mandate the AST-guard-with-allowlist idiom §5.1 adopts. **Cross-link; do not supersede.** Confirm the new ADR does not collide with ADR-0183's task-0098 scope (legacy-corpus storage retrofit) — different seam, and §12 says so. |
| **ADR-0078** (retrieval sunk to backend) | The transport precedent §6 uses. **Not superseded** — this plan adds a schema to a boundary 0078 created. Worth a one-line amendment noting that 0078's response payload stayed untyped and this plan closes that. |
| **ADR-0057** (import-linter layer enforcement, `_shared`→core waivers) | Binding on placement: Protocols go in `_shared/contracts/`, concretes in the owning subpackage, injection at `_shared/runtime/lifecycle`. The two waived edges (`pyproject.toml:297-300`) are the composition-root pattern §6.2's swap relies on. **No new waiver should be needed.** If a seam appears to require one, that is a signal the placement is wrong — stop and re-examine, do not add the waiver. |
| **ADR-0060 / ADR-0062** | The core/backend/`_shared` reorg that created these boundaries. Read for placement; no conflict expected. Also the source of the §11 stale-config residue. |

---

## 11. Known traps — read before starting

**11.1 The `per-file-ignores` for the retrieval modules are STALE, and this bites S1/S2.**
`pyproject.toml` still lists:

```
"yadgar/_shared/retrieval/fusion.py"                = ["C901"]
"yadgar/_shared/retrieval/scoring.py"               = ["C901"]
"yadgar/_shared/retrieval/reranking.py"             = ["C901", "PLR0913"]
"yadgar/_shared/retrieval/query_analysis.py"        = ["C901"]
"yadgar/_shared/retrieval/_reranking_heuristic.py"  = ["C901"]
```

**All five paths no longer exist** — verified: the modules moved to `yadgar/backend/retrieval/` in
the ADR-0060/0062 reorg and the ignores were not repointed. Ruff silently ignores entries for
missing files. **Consequence: `C901` (max-complexity 15) and `PLR0913` (max-args 8) are currently
LIVE on exactly the files this plan refactors.** `fusion._inject_ce_diversity` already takes 7
parameters (`fusion.py:280-288`) — one more and pre-commit fails on a change the implementer
believed was ignored.

Do **not** fix the stale ignores as part of this plan (scope creep, and re-enabling five ignores is
its own decision). Just know the gates are armed. If a seam change trips `PLR0913`, the correct move
is a parameter object — which is S2, i.e. the plan already contains its own remedy.

**11.2 Do not "tidy" the `raw: dict` pass-through on `Candidate` while touching S1.** It is
load-bearing for the fan-out compatibility contract (`providers/base.py:11-13`). Removing it is a
separate, larger decision.

**11.3 Test placement** — §4's opening. Tests for `yadgar/backend/retrieval/` live in
`yadgar/tests/_shared/`. Follow the tests, not the code.

**11.4 Tightening a contract can convert silent degradation into a loud failure.** §9's S4 note. This
is generally the correct trade, and it is generally a production incident if it lands unsoaked.

---

## 12. Explicitly NOT in scope

The exclusion list matters as much as the inclusion list.

- **The `StorageEngine` seam.** ADR-0183 assigns the legacy-corpus retrofit to **task 0098** and
  explicitly states the legacy corpus is not portable and that decision does not make it so. Cite it;
  do not re-litigate it here. `StorageProtocol` already exists (`protocols.py:167`) and is unchanged
  by this plan.
- **`runtime_config_client.get`** — fan-in 1350, the largest single coupling point in the tree. Typing
  it buys nothing: it returns a config value, every caller needs config, and the audit's own §"Where
  pain actually is" says a split does not decouple it. High fan-in alone is not a reason.
- **Repo-wide mypy**, and **scoped mypy** (§2.6).
- **Converting `RetrievalStage` / `SourceProvider` from `ABC` to `Protocol`.** §2.2 — `ABC` is the
  stronger runtime guarantee here.
- **The unified annotation-map refactor** — replacing the 21 `_`-prefixed row-stamp writes with a
  side map, extending `RetrievalState.scores` past fusion. Named in §2.4 as the principled end-state.
  It is a large behavioural refactor of the whole reranking stack and belongs in its own plan.
- **Install / systemd / packaging / viz.** The audit's largest hotspot (65 defects) and second
  largest (22). Neither is an in-process seam; neither is touched.
- **Guards for `5090-2` / `5094-1` / `5008-1`** — module-level mutable singletons, executor
  injection, browser global scope. Named in §5.3, not scoped.
- **Any conclusion about transport cost.** Task **#0117** measures it. This plan is transport-neutral
  by construction (§6.3) and must remain readable and correct whichever way 0117 lands.
- **Fixing the stale `per-file-ignores`** (§11.1).
- **Any train-car framing.** Restated because it matters: **this is not a v5.172 car.** It has no
  train dependency, no shared branch, and no shared PR.
