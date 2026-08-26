# Yadgar Capability Registry

**The single source of truth for every feature, algorithm, and behaviour Yadgar
has — wired or not.** If a capability exists in the codebase (a setting that
controls something, an MCP tool, a migration, a retrieval/consolidation
algorithm, a brain-dynamics mechanism), it has an entry here with: what it does,
how it is reached at runtime (its wiring), and its status.

This registry is **enforced** by invariant **I32**
(`scripts/check_capability_coverage.py`): every Settings field, every MCP
`@_tool`, every migration, and every `BC-*` behaviour in
`docs/contracts/BEHAVIOR_CONTRACT.md` MUST be referenced by at least one entry below.
Adding a setting/tool/migration/behaviour without cataloguing it here fails the
lint. See **Scope of the guarantee** below for what a green lint does and does
not prove.

> **Maintenance rule (HARD):** when you add or remove a Settings field, an MCP
> tool, a migration, or a `BC-*` row, update this file in the SAME change. The
> I32 lint runs in pre-commit and CI `invariant-checks`. This file is the place
> the e2e behavior contract, the v6 plan, and the dead-config audit (#41) all
> draw "what exists" from — keep it true.

---

## Scope of the guarantee (read this before trusting a status)

I32 is a **coverage** lint, not a **correctness** lint.

A green I32 proves the catalogue is **complete and well-formed**:
- every enumerable surface item (setting / tool / migration / BC) is catalogued,
- every entry's `status:` is a valid enum value,
- every `refs:` file path resolves,
- no entry cites a surface item that no longer exists (no stale rows).

A green I32 does **NOT** prove a `status:` is **accurate**. Whether a
`LIVE`-marked capability is truly reachable, or a `DEAD`-marked one truly has no
caller, requires call-graph / runtime truth that this lint does not compute.
**Status accuracy is a human + review responsibility.** Treat a status as a
documented claim, re-verified when you touch the subsystem — exactly like a
`BC-*` marker.

---

## Status vocabulary

| Status        | Meaning |
|---------------|---------|
| `LIVE`        | Wired and reachable with the **default** config. The normal path exercises it. |
| `DORMANT`     | Reachable in code but **disabled by default** config. A single flag flip turns it on (e.g. surprise write-gate at threshold 0). |
| `SHADOW`      | Computed / recorded on every relevant event, but its result is **not acted on** (evaluate-only). E.g. the v5.73 surprise-gate shadow mode stamps `would_reject` but never drops. |
| `DEAD`        | **No caller / unreachable.** Kept for archaeology or pending removal. A `DEAD` entry that owns settings ⇒ those settings are dead config (#41 cleanup target). |
| `CONFIG-ONLY` | A knob that exists but whose consumer is dead or absent — the setting reads but nothing meaningful acts on it. |

---

## Entry schema

Each capability is a level-3 heading followed by a fixed set of markdown list
fields. The I32 lint parses these fields, so the format is load-bearing:

```
### CAP-<DOMAIN>-<NNN> — <Human name>
- **status:** LIVE
- **category:** retrieval | storage | write-path | consolidation | enrichment | gate | wiki | curation | mcp-tool | observability | security | ops | brain-dynamics | viz | config
- **settings:** `FOO_BAR`, `BAZ_QUX`        (Settings fields that control it; omit the field or use `—` if none)
- **tools:** `recall`, `memorize`           (MCP tools that expose it; `—` if none)
- **migrations:** `022`, `008`              (`—` if none)
- **bc:** `BC-EN3a`, `BC-W1`                 (behaviour-contract rows it implements; `—` if none)
- **refs:** `yadgar/backend/retrieval/fusion.py::convex_fuse`, `yadgar/core/server/tools/recall.py`
- **wiring:** how it is reached at runtime (caller chain) OR, if not reached, exactly why (dead/dormant reason).
- **explanation:** what it does + the algorithm/behaviour, in 2–5 sentences. Enough that a reader needn't open the code to understand it.
```

Backtick-quoted tokens in `settings`/`tools`/`migrations`/`bc`/`refs` are what
the lint reads. Prose in `wiring`/`explanation` is for humans.

DOMAIN codes: `RETR` retrieval · `STOR` storage/schema · `WRITE` write-path/gates
· `ENR` enrichment · `CONS` consolidation/brain-dynamics · `WIKI` wiki/curation ·
`OPS` admin/ops/observability/security · `VIZ` visualization · `CFG` standalone
config knobs.

---

<!-- ENTRIES BELOW — grouped by domain. Keep within-domain entries together. -->

### CAP-RETR-001 — WRRF / Convex Signal Fusion

- **status:** LIVE
- **category:** retrieval
- **settings:** `WRRF_VECTOR_WEIGHT`, `WRRF_FTS_WEIGHT`, `WRRF_PPR_WEIGHT`, `WRRF_SPREADING_WEIGHT`, `FUSION_METHOD`, `FUSION_NORM`, `COMBMNZ_ENABLED`, `WRRF_GRAPH_PRIOR_WEIGHT`, `WRRF_COFIRE_PRIOR_WEIGHT`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR10`, `BC-RR11`
- **refs:** `yadgar/backend/retrieval/fusion.py::_FusionMixin._wrrf_fuse`, `yadgar/backend/retrieval/fusion.py::_convex_fuse`, `yadgar/backend/retrieval/fusion.py::_FusionMixin._fuse_scores`
- **wiring:** `recall` tool → `Retriever.recall()` → `_FusionMixin._fuse_scores()`. When `FUSION_METHOD="convex"` (default), calls module-level `_convex_fuse()` (min-max normalised weighted sum). When `FUSION_METHOD="wrrf"`, calls `_FusionMixin._wrrf_fuse()` (per-signal z-score or minmax normalisation then weighted sum). After fusion, `WRRF_GRAPH_PRIOR_WEIGHT` and `WRRF_COFIRE_PRIOR_WEIGHT` MULTIPLICATIVE boosts are applied via `_apply_prior_boost()` — `fused * (1 + w * prior)`, O(1) reads from precomputed columns. Car 8 (task 283): the boost was additive and therefore query-independent, so a row with no query match could be lifted over one that matched; multiplicative leaves a zero score at zero. `COMBMNZ_ENABLED=False` by default — flipping to True multiplies the fused score by the number of signals that fired.
- **explanation:** Merges four retrieval signals (vector KNN, FTS BM25, PPR, spreading activation) into a single ranked list. The default `convex` fusion method normalises each signal's scores with min-max and computes a weighted sum; the alternate `wrrf` method uses z-score normalisation before weighting. After fusion two additive priors are applied: a graph-centrality prior (`graph_prior` column, weight 0.2) and a co-recall frequency prior (`cofire_prior` column, weight 0.15), both precomputed at consolidation time to avoid per-query graph traversal. `COMBMNZ_ENABLED` (default False) scales the fused score by the count of non-zero signals to reward multi-signal agreement.

---

### CAP-RETR-002 — Candidate Pool Sizing

- **status:** LIVE
- **category:** retrieval
- **settings:** `CANDIDATE_POOL_MULTIPLIER`, `FAST_PROFILE_CANDIDATE_MULTIPLIER`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/core.py::Retriever._resolve_query_and_candidate_k`, `yadgar/backend/retrieval/stages/knn.py`, `yadgar/backend/retrieval/stages/fts.py`
- **wiring:** Called from `Retriever.recall()` and the pipeline stage modules. `CANDIDATE_POOL_MULTIPLIER` (default 20) sizes the per-signal candidate fetch as `max_results × multiplier`. The fast profile overrides this with `FAST_PROFILE_CANDIDATE_MULTIPLIER` (default 3) to bound hook-handler latency.
- **explanation:** Controls how many raw candidates each retrieval signal fetches before fusion and reranking. A larger pool improves recall at the cost of CPU; the fast profile uses a much smaller multiplier to keep hook handlers low-latency. The final result list is always trimmed to `max_results` after reranking.

---

### CAP-RETR-003 — Dead WRRF-K / Dead Candidate-Multiplier Config

- **status:** DEAD (v6 T3 — deleted from config.py; WRRF_K removed)
- **category:** retrieval
- **settings:** `WRRF_CANDIDATE_MULTIPLIER` (retained CONFIG-ONLY); WRRF_K deleted
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/config/config.py`
- **wiring:** `WRRF_K` deleted from `config.py` in v6 T3 dead-config cleanup. `WRRF_CANDIDATE_MULTIPLIER` retained as CONFIG-ONLY — no production consumer; `CANDIDATE_POOL_MULTIPLIER` is the live setting.
- **explanation:** `WRRF_K` was the RRF constant `k` used in rank-based Reciprocal Rank Fusion — no production caller. Deleted. `WRRF_CANDIDATE_MULTIPLIER` superseded by `CANDIDATE_POOL_MULTIPLIER` but retained pending explicit removal.

---

### CAP-RETR-004 — Retrieval Profiles (fast / balanced / full)

- **status:** LIVE
- **category:** retrieval
- **settings:** `RETRIEVAL_PROFILE`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/fusion.py::PROFILES`, `yadgar/backend/retrieval/core.py::Retriever.recall`
- **wiring:** `recall` → `Retriever.recall()` reads `RETRIEVAL_PROFILE` (default `"balanced"`) to select a profile dict from `PROFILES`. Hook handlers pass `profile="fast"` explicitly. The profile controls which signals are active (`signals`), whether CE/NLI/multi-passage reranking runs, and whether query analysis is skipped.
- **explanation:** Three named retrieval profiles trade quality for latency. `fast` enables only vector + FTS signals, skips all heavy reranking and query analysis — used by every hook handler. `balanced` (default) uses all four signals plus cross-encoder. `full` adds NLI entailment on top of `balanced`. The active profile is propagated through the entire pipeline as the `profile` dict, gating each reranking stage.

---

### CAP-RETR-005 — Query Routing and Analysis

- **status:** LIVE
- **category:** retrieval
- **settings:** `QUERY_ROUTING_ENABLED`, `TEMPORAL_KEYWORDS`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR8`
- **refs:** `yadgar/backend/retrieval/query_analysis.py::analyze_query`, `yadgar/backend/retrieval/query_analysis.py::_classify_query_type`
- **wiring:** `Retriever.recall()` → `_resolve_query_and_candidate_k()` → `analyze_query()` (when `skip_query_analysis` is False, i.e. non-fast profiles). When `QUERY_ROUTING_ENABLED=True` (default), the enabled_signals from query analysis are intersected with the profile's signal set to narrow which retrieval signals actually run.
- **explanation:** `analyze_query` classifies each query into one of: `temporal`, `open_domain`, `factoid`, `code`, `relational`, `keyword`, `simple`, or `complex`. The classification drives `enabled_signals` — e.g. temporal queries run only vector + FTS, factoid queries add PPR, relational and complex queries activate all four signals. `TEMPORAL_KEYWORDS` is a comma-separated list used to detect temporal queries. `QUERY_ROUTING_ENABLED=False` bypasses the intersection, giving every query the full profile signal set.

---

### CAP-RETR-006 — Pseudo-HyDE Query Expansion

- **status:** LIVE
- **category:** retrieval
- **settings:** `QUERY_EXPANSION_ENABLED`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR9`
- **refs:** `yadgar/backend/retrieval/query_analysis.py::_pseudo_hyde_expand`, `yadgar/backend/retrieval/scoring.py::_ScoringMixin._build_vector_search_list`
- **wiring:** `_collect_vector_scores()` → `_build_vector_search_list()` checks `QUERY_EXPANSION_ENABLED` (default True). When enabled, calls `_pseudo_hyde_expand(query)` and appends the result as a second vector search at strength 0.95.
- **explanation:** Converts question-form queries into declarative pseudo-documents before vector embedding to bridge the query–document semantic gap (a lightweight HyDE approximation). A regex table maps "What is X?" → "X is", "When did X?" → "X", etc. The generated statement is embedded and used as a second HNSW search alongside the original query; both hits are merged into the score dict. When `QUERY_EXPANSION_ENABLED=False`, only the original query string is embedded.

---

### CAP-RETR-007 — Open-Domain Subquery Expansion

- **status:** LIVE
- **category:** retrieval
- **settings:** —
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR9`
- **refs:** `yadgar/backend/retrieval/query_analysis.py::_build_open_domain_subqueries`, `yadgar/backend/retrieval/query_analysis.py::_collect_semantic_expansions`, `yadgar/backend/retrieval/query_analysis.py::_build_boosted_fts_query`
- **wiring:** `Retriever.recall()` calls `_build_open_domain_subqueries()` when `open_domain_mode=True` (detected by `analyze_query`). Generated subqueries are passed to both `_collect_fts_scores()` and `_collect_vector_scores()` at reduced strength (0.8 FTS, 0.85 vector). `_build_boosted_fts_query` duplicates high-information terms to sharpen BM25 intent on every FTS call.
- **explanation:** For inference-style questions (comparison, modal, topic-expansion queries), generates up to four auxiliary subqueries from named entities, content terms, comparison options, and semantic topic expansions. Each subquery is searched in parallel with the main query at a reduced weight. `_build_boosted_fts_query` additionally doubles capitalized tokens in the FTS query string to amplify their BM25 score.

---

### CAP-RETR-008 — Personalized PageRank (PPR) Signal

- **status:** LIVE
- **category:** retrieval
- **settings:** `PPR_DAMPING`, `PPR_ITERATIONS`, `GRAPH_ENTITY_MIN_LENGTH`, `GRAPH_MAX_HOPS`, `GRAPH_MIN_EDGE_WEIGHT`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-KG4`
- **refs:** `yadgar/backend/retrieval/core.py::Retriever.ppr_retrieve`, `yadgar/backend/retrieval/scoring.py::_ScoringMixin._collect_ppr_scores`, `yadgar/backend/retrieval/graph_helpers.py::_GraphHelpersMixin._build_networkx_graph`
- **wiring:** `_collect_ppr_scores()` is called from `Retriever.recall()` when the `ppr` signal is in `enabled_signals` (active for `balanced`/`full` profiles on `factoid`, `relational`, `complex` query types). It calls `ppr_retrieve()` → `_build_networkx_graph()` → `nx.pagerank()`. Results are merged into the `scores` dict and fused by `_fuse_scores()`.
- **explanation:** Runs Personalized PageRank over the entity co-occurrence graph seeded by entities extracted from the query. Entities shorter than `GRAPH_ENTITY_MIN_LENGTH` characters are filtered. The graph is built up to `GRAPH_MAX_HOPS` hops, discarding edges below `GRAPH_MIN_EDGE_WEIGHT`. High-PPR entities are mapped back to memories via FTS on entity name, producing a graph-coherent ranking signal that surfaces memories connected through shared entities even when direct term overlap is weak.

---

### CAP-RETR-009 — Spreading Activation Signal

- **status:** LIVE
- **category:** retrieval
- **settings:** `GRAPH_SPREADING_DECAY`, `GRAPH_SPREADING_MAX_DEPTH`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-KG5`
- **refs:** `yadgar/backend/retrieval/core.py::Retriever.spreading_activation`, `yadgar/backend/retrieval/scoring.py::_ScoringMixin._collect_spreading_scores`
- **wiring:** `_collect_spreading_scores()` is called from `Retriever.recall()` when `spreading` signal is in `enabled_signals` (active for `balanced`/`full` on `relational`/`complex`). Seeds from the top-5 vector hits, then BFS through the entity graph up to `GRAPH_SPREADING_MAX_DEPTH` hops with activation decaying by `GRAPH_SPREADING_DECAY` per hop.
- **explanation:** Starting from the most vector-similar memories, spreads activation through the entity co-occurrence graph. Each hop reduces activation by a geometric factor (`GRAPH_SPREADING_DECAY`, default 0.5). Activated memories that are not in the seed set accumulate scores and are merged into the fusion pool. This surfaces associatively related memories that share entity neighborhoods with the top seed results.

---

### CAP-RETR-010 — Entity Extraction and Knowledge Graph Linking

- **status:** LIVE
- **category:** retrieval
- **settings:** `GRAPH_ENTITY_MIN_LENGTH`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-KG1`, `BC-KG2`, `BC-KG3`
- **refs:** `yadgar/backend/retrieval/graph_helpers.py::_GraphHelpersMixin._find_memories_for_entity`, `yadgar/backend/retrieval/graph_helpers.py::_GraphHelpersMixin._find_entities_in_content`, `yadgar/backend/retrieval/scoring.py::_ScoringMixin._run_entity_fts`
- **wiring:** Entity-based FTS is called from `_collect_fts_scores()` via `_run_entity_fts()`. Entity membership in memory content is resolved via `_find_entities_in_content()` (called from `spreading_activation`) and `_find_memories_for_entity()` (called from `ppr_retrieve`). These helpers are on the `_GraphHelpersMixin` which is part of `Retriever`'s MRO.
- **explanation:** Queries are scanned for capitalized tokens that serve as named entities (people, places, projects). Entity-focused FTS re-searches the corpus with these tokens at boosted weight (0.5–0.7 depending on open-domain mode). For PPR and spreading, entity names are looked up in the knowledge-graph store to find graph node IDs; adjacency is walked to discover related memories. `GRAPH_ENTITY_MIN_LENGTH` filters out short tokens unlikely to be meaningful entities.

---

### CAP-RETR-011 — Temporal Retrieval Signal

- **status:** LIVE
- **category:** retrieval
- **settings:** `TEMPORAL_RETRIEVAL_ENABLED`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR12`
- **refs:** `yadgar/backend/retrieval/temporal.py::parse_temporal_expression`, `yadgar/backend/retrieval/scoring.py::_ScoringMixin._collect_temporal_scores`
- **wiring:** `Retriever.recall()` calls `_collect_temporal_scores()` after FTS/vector/PPR/spreading. When `TEMPORAL_RETRIEVAL_ENABLED=True` (default) and the query contains a temporal expression, the temporal signal contributes to fusion at weight `w_temporal` (0.8 for content-date matches, 0.6 for month matches).
- **explanation:** Parses natural-language temporal expressions (ISO dates, "last week", "in May 2023", session references) from the query and searches memories by content-embedded date hints and by `created_at` month proximity. Matching memories receive a temporal score inversely proportional to their rank in the temporal hit list. The temporal signal weight is set dynamically (0.0 if no temporal expression found) and injected into fusion alongside the other signals.

---

### CAP-RETR-012 — Dead Temporal Boost / Decay Config

- **status:** DEAD (v6 T3 — deleted from config.py)
- **category:** retrieval
- **settings:** — (TEMPORAL_BOOST_WEIGHT, TEMPORAL_DECAY_DAYS, TEMPORAL_EXACT_MATCH_BOOST all deleted)
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** Deleted from `config.py` in v6 T3. No production caller ever read these. The live temporal scoring in `_collect_temporal_scores` uses hardcoded weights 0.8/0.6 and rank-based scoring.
- **explanation:** Three temporal tuning knobs that were planned but never wired into the retrieval pipeline. Removed in v6 T3 dead-config cleanup (#41).

---

### CAP-RETR-013 — Heuristic Reranker

- **status:** LIVE
- **category:** retrieval
- **settings:** `RERANKER_ENABLED`, `RERANKER_TOP_K`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/_reranking_heuristic.py::_HeuristicMixin.heuristic_rerank`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_heuristic`
- **wiring:** `_apply_rerank_pipeline()` calls `_rerank_heuristic()` first (when `RERANKER_ENABLED=True`, default True, and profile is not `fast`). `heuristic_rerank` reads `RERANKER_TOP_K` (default 50) when CE is active; otherwise uses `max_results`.
- **explanation:** A lightweight lexical reranker that scores each candidate memory against the query using four signals: entity coverage (capitalized query tokens in content), content term coverage (non-stop words), bigram overlap (phrase matching), and exact substring match. Weights are fixed at 0.35/0.30/0.20/0.15. The resulting `_rerank_score` is blended with the fusion score (85% fusion + 15% heuristic). Runs on CPU, no ML model required.

---

### CAP-RETR-014 — Cross-Encoder Reranker

- **status:** LIVE
- **category:** retrieval
- **settings:** `CROSS_ENCODER_ENABLED`, `CROSS_ENCODER_MODEL`, `CROSS_ENCODER_TOP_K`, `CROSS_ENCODER_WEIGHT`, `HEAVY_RERANK_ENABLED`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR1`
- **refs:** `yadgar/backend/retrieval/_reranking_cross_encoder.py::_CrossEncoderMixin.cross_encoder_rerank`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_cross_encoder`
- **wiring:** `_apply_rerank_pipeline()` → `_rerank_cross_encoder()` when `use_cross_encoder=True` (profile allows CE AND `CROSS_ENCODER_ENABLED=True`, default True). CE is blocked entirely by `HEAVY_RERANK_ENABLED=False`. The ML client (`LocalMLClient` or `RemoteMLClient`) does the model inference; this mixin handles normalization and blending.
- **explanation:** Scores each candidate memory against the query using a cross-encoder model. This entry is the rerank STAGE plus its degraded fallback tier — the model actually loaded on the hot path is CAP-RETR-015's `GTE_RERANKER_MODEL` (Ettin-32m). `CROSS_ENCODER_MODEL` (`cross-encoder/ms-marco-MiniLM-L-6-v2`) is reached only when the Ettin primary is disabled or has failed, and is deliberately not baked into `Dockerfile.backend`, so inside the offline container that tier scores zeros (ADR-0192). In open-domain mode, each memory may contribute a second "implied facts" variant (derived from content inference) alongside its base text; the max score across variants is kept. Scores are min-max normalised and blended with the existing retrieval score at `CROSS_ENCODER_WEIGHT` (default 0.6). `HEAVY_RERANK_ENABLED=False` is a kill switch that bypasses CE, NLI, and multi-passage entirely to eliminate CPU burst on constrained hosts.

---

### CAP-RETR-015 — Advanced CE Reranker (Ettin-32m primary / GTE-ModernBERT rollback)

- **status:** LIVE
- **category:** retrieval
- **settings:** `GTE_RERANKER_ENABLED`, `GTE_RERANKER_MODEL`, `GTE_RERANKER_MAX_LENGTH`, `GTE_RERANKER_FALLBACK_TO_FLASHRANK` (onnx-int8 backend knobs removed in the 5.131.0 deps train). The env/field prefix is kept as GTE for back-compat; T4 flipped the default model to Ettin-32m.
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/ml_client/ml_client.py`, `yadgar/_shared/config/config.py`, `Dockerfile.backend`
- **wiring:** `GTE_RERANKER_ENABLED=True` and `GTE_RERANKER_MODEL="cross-encoder/ettin-reranker-32m-v1"` in config (`config.py:288–289`). Ettin-32m weights are **baked into `Dockerfile.backend`** as the CE primary, so the reranker is live by default — no operator staging required. GTE-ModernBERT is also baked one cycle purely as the config-revert rollback. `GTE_RERANKER_FALLBACK_TO_FLASHRANK` keeps its name for env back-compat but is a failure-mode selector, not a FlashRank switch: on reranker failure, `true` falls through to CAP-RETR-014's degraded sentence-transformers tier and `false` returns zeros. The FlashRank tier itself was removed in ADR-0192 — `flashrank` was never a dependency, so its import could not succeed.
- **explanation:** The advanced cross-encoder reranker. Train 4 flipped the primary model from GTE-ModernBERT to **Ettin-32m** (`cross-encoder/ettin-reranker-32m-v1`, 32.8M, ModernBERT-lineage, Apache-2.0) after a LongMemEval memory-domain A/B: recall@5/@10 parity-or-better on every type, +0.108 recall@5 on multi-session, ~4.7× per-pass CE speedup. `GTE_RERANKER_MAX_LENGTH` caps input token length. Rollback to GTE-ModernBERT is a `config.yaml` model swap (weights already in the image).

---

### CAP-RETR-016 — NLI Entailment Reranker

- **status:** DORMANT
- **category:** retrieval
- **settings:** `NLI_RERANKING_ENABLED`, `NLI_MODEL`, `NLI_WEIGHT`, `NLI_ONLY_FOR_OPEN_DOMAIN`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR2`
- **refs:** `yadgar/backend/retrieval/_reranking_nli.py::_NLIMixin.nli_rerank`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_nli`
- **wiring:** `_rerank_nli()` checks `profile["nli"] AND NLI_RERANKING_ENABLED`. `NLI_RERANKING_ENABLED` defaults to `False` (changed from True in v5.6.6 due to ~55s per call on CPU). Additionally gated by `NLI_ONLY_FOR_OPEN_DOMAIN=True` (default), meaning even if enabled it only fires for open-domain queries.
- **explanation:** Converts the query into a declarative hypothesis and scores each candidate memory as premise via an NLI model (default `cross-encoder/nli-deberta-v3-base`). High entailment probability indicates the memory is a likely answer. The NLI score is blended with the cross-encoder score at `NLI_WEIGHT` (default 0.3). Disabled by default because the DeBERTa model averages 55 seconds per call on CPU — set `NLI_RERANKING_ENABLED=true` to enable.

---

### CAP-RETR-017 — Multi-Passage Evidence Aggregation

- **status:** LIVE
- **category:** retrieval
- **settings:** `MULTI_PASSAGE_RERANKING_ENABLED`, `MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD`, `MULTI_PASSAGE_MAX_CLUSTER_SIZE`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR6`
- **refs:** `yadgar/backend/retrieval/_reranking_multi_passage.py::_MultiPassageMixin.multi_passage_rerank`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_multi_passage`
- **wiring:** `_rerank_multi_passage()` fires after CE reranking when `MULTI_PASSAGE_RERANKING_ENABLED=True` (default True) and the profile allows it (balanced/full). Calls `cluster_memories()` (Jaccard on token sets) then `score_single_pair()` (CE) for each cluster's concatenated text.
- **explanation:** Groups the top-20 candidate memories into clusters of related passages using Jaccard token overlap. For each cluster with ≥2 members, concatenates up to 3 passages and re-scores the combined text with the cross-encoder. If the combined-passage CE score exceeds any individual member's score, a boost equal to half the gap is applied to all cluster members. This captures cases where no single passage answers the query but two or three together do.

---

### CAP-RETR-018 — Adversarial Detection and MMR Diversity

- **status:** LIVE
- **category:** retrieval
- **settings:** `ADVERSARIAL_DETECTION_ENABLED`, `ADVERSARIAL_SCORE_GAP_THRESHOLD`, `ADVERSARIAL_MIN_CONFIDENCE`, `ADVERSARIAL_DIVERSITY_ENFORCEMENT`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR3`, `BC-RR7`
- **refs:** `yadgar/backend/retrieval/_reranking_confidence.py::_ConfidenceMixin.detect_adversarial`, `yadgar/backend/retrieval/_reranking_mmr.py::_MMRMixin.mmr_rerank`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_adversarial_detect`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_mmr`
- **wiring:** `_apply_rerank_pipeline()` calls `_rerank_mmr()` (when `ADVERSARIAL_DIVERSITY_ENFORCEMENT=True`, default True) before trimming to `max_results`, then `_rerank_adversarial_detect()` (when `ADVERSARIAL_DETECTION_ENABLED=True`, default True) after trimming. MMR uses stored embeddings fetched from storage.
- **explanation:** Two complementary defences. MMR (Maximal Marginal Relevance) reranking diversifies results by iteratively selecting candidates that are both relevant and dissimilar to already-selected ones (balancing at `lambda=0.7`), preventing the top-k from being dominated by near-duplicate passages. Adversarial detection runs z-score gap analysis on the final scores to produce a `_retrieval_confidence` field (0–1) that callers can use to detect low-confidence retrievals; below `ADVERSARIAL_MIN_CONFIDENCE` a `is_uncertain` flag is set.

---

### CAP-RETR-019 — Confidence Gating (Signal Weight Zeroing)

- **status:** DEAD (v6 T3 — code deleted)
- **category:** retrieval
- **settings:** — (CONFIDENCE_GATING_ENABLED, CONFIDENCE_MIN_RESULTS, CONFIDENCE_SCORE_SPREAD_THRESHOLD, CONFIDENCE_TOP_SCORE_THRESHOLD, CONFIDENCE_FALLBACK_STRATEGY all deleted)
- **tools:** —
- **migrations:** —
- **bc:** `BC-RR5` (now satisfied by CAP-RETR-027)
- **refs:** —
- **wiring:** `_apply_confidence_gating()` and its call site in `_fuse_scores()` removed from `yadgar/backend/retrieval/fusion.py` in v6 T3. `CONFIDENCE_GATING_ENABLED` and related settings removed from `config.py`.
- **explanation:** Confidence gating zeroed signal weights below per-signal thresholds before fusion. Removed in v6 T3 dead-config/code cleanup (#41) — default was `True` (behavior change). `compute_signal_confidence` in `_reranking_confidence.py` remains for use by adversarial detection (BC-RR5 coverage continues via CAP-RETR-027).

---

### CAP-RETR-020 — Dead Confidence Fallback Config

- **status:** DEAD (v6 T3 — merged into CAP-RETR-019 retirement)
- **category:** retrieval
- **settings:** — (CONFIDENCE_MIN_RESULTS, CONFIDENCE_SCORE_SPREAD_THRESHOLD, CONFIDENCE_TOP_SCORE_THRESHOLD, CONFIDENCE_FALLBACK_STRATEGY all deleted)
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** All four settings deleted from `config.py` in v6 T3. Never read by production code.
- **explanation:** Dead config from v8 confidence-fallback feature design, removed in v6 T3 (#41).

---

### CAP-RETR-021 — Comparison Query Dual-Search

- **status:** DORMANT
- **category:** retrieval
- **settings:** `COMPARISON_DUAL_SEARCH_ENABLED`, `COMPARISON_TOP_K_PER_OPTION`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR13`
- **refs:** `yadgar/backend/retrieval/fusion.py::_FusionMixin._comparison_dual_search`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_comparison_merge`
- **wiring:** `_rerank_comparison_merge()` fires when `COMPARISON_DUAL_SEARCH_ENABLED=True` (default True) AND `query_analysis["comparison_options"]` is non-empty (requires an "or" in the query and option extraction to succeed). The dual search queries for each option separately with vector + FTS and merges results. `COMPARISON_DUAL_SEARCH_ENABLED` is True by default, so activation depends entirely on whether `_extract_comparison_options` finds options.
- **explanation:** Detects "A or B?" query structure and performs separate vector+FTS searches for each option at `COMPARISON_TOP_K_PER_OPTION` candidates. Results for each option are tagged with `_comparison_option` and merged into the main result list, allowing both sides of a comparison to be represented. The option extraction is conservative — it looks for 1–3 word spans around "or" that are not stop words — so the feature triggers only on clear comparison queries.

---

### CAP-RETR-022 — Dual-Vector Architecture

- **status:** DEAD (v6 T3 — code deleted; IMPLICIT_EMBEDDING_MODEL retained CONFIG-ONLY)
- **category:** retrieval
- **settings:** `IMPLICIT_EMBEDDING_MODEL` (CONFIG-ONLY, retained for future use); DUAL_VECTORS_ENABLED deleted
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** `_dual_vector_search()` and `DUAL_VECTORS_ENABLED` deleted from `yadgar/backend/retrieval/core.py` and `config.py` in v6 T3. `IMPLICIT_EMBEDDING_MODEL` retained in config.py as CONFIG-ONLY — no production consumer after method removal, but kept as placeholder for future DualCSE implementation.
- **explanation:** Prep scaffold for dual-embedding architecture removed in v6 T3 (#41). `DUAL_VECTORS_ENABLED` setting and `_dual_vector_search()` method deleted. `IMPLICIT_EMBEDDING_MODEL` remains for future DualCSE work.

---

### CAP-RETR-023 — Hopfield Engram Configuration

- **status:** LIVE
- **category:** retrieval
- **settings:** `HOPFIELD_BETA`, `HOPFIELD_MAX_PATTERNS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-H1`, `BC-H2`, `BC-H3`
- **refs:** `yadgar/_shared/contracts/engram.py`
- **wiring:** `HOPFIELD_MAX_PATTERNS` is used by `EngramAllocator.__init__` to size the engram slot table (`self._num_slots`). `HOPFIELD_BETA` is defined in config and checked by `check_invariants` (I invariant verifies engram_slot row count equals `HOPFIELD_MAX_PATTERNS`). The `_engram` object is attached to `Retriever.set_engram()` and its temporal links are injected into results by `_rerank_engram_links()`.
- **explanation:** Controls the Hopfield-inspired engram memory allocator. `HOPFIELD_MAX_PATTERNS` (default 5000) sets the maximum number of engram slots; `HOPFIELD_BETA` (default 8.0) is the sharpness parameter for pattern completion. The engram allocator assigns slot IDs to memories and records temporal co-activation links. During retrieval, `_rerank_engram_links()` enriches result memories with their temporal neighbors. Hook handlers (prompt-recall, subagent-start, session-end) trigger engram-based context injection, satisfying BC-H1/H2/H3.

---

### CAP-RETR-024 — Successor Representation (SR) Cognitive Map

- **status:** LIVE
- **category:** retrieval
- **settings:** `SR_DISCOUNT`, `SR_UPDATE_RATE`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/restoration/cognitive_map.py::CognitiveMap`, `yadgar/_shared/runtime/sr_session.py::SRTransitionRecorder`, `yadgar/core/server/tools/recall.py`
- **wiring:** `recall` tool records a transition from `_last_recalled_ids[session_key]` to the new top result via `_st._cognitive_map.record_transition()` and `incremental_update()` on every call — since T2 Car B the CORE process holds the session-side `SRTransitionRecorder` (record only; `incremental_update` is a documented no-op there), while the numpy SR compute lives in the backend `CognitiveMap` subclass behind `POST /restore`. `CognitiveMap.__init__` reads `SR_DISCOUNT` (γ, default 0.9) and `SR_UPDATE_RATE` (η, default 0.1). The SR matrix is rebuilt per backend restore (cross-process `_dirty` cannot be trusted).
- **explanation:** Maintains a Successor Representation matrix over memory transitions — a TD-learning model that predicts which memories are likely to be retrieved after the current one. Every `recall` call logs the (previous_top_id → current_top_id) transition. The discount factor γ controls how far into the future the representation looks; the learning rate η controls update speed. The SR matrix is primarily used by the restoration and project-brief systems to predict likely next-retrieval candidates.

---

### CAP-RETR-025 — Astrocyte Pool Consensus Retrieve

- **status:** LIVE
- **category:** retrieval
- **settings:** `ASTROCYTE_POOL_ENABLED` (controls pool init; False → landscape returns [])
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-AC3a`, `BC-AC3b`
- **refs:** `yadgar/_shared/astrocyte_pool/astrocyte_pool.py::AstrocytePool.consensus_retrieve`, `yadgar/core/server/tools/recall.py::_landscape_recall`
- **wiring:** Exposed via `recall(mode="landscape", directory=...)`. The `recall()` MCP tool dispatches to `_landscape_recall()` when `mode="landscape"` — this calls `_st._pool.consensus_retrieve(query, top_k=max_results)` and post-filters results with `is_directory_eligible()` (v5.65 directory contract). Results carry `consensus_score` (float) and `voting_domains` (list[str]) metadata. Landscape dispatch is exclusive: it short-circuits before fan-out and profile routing. Pool is `_st._pool` (set from `_st._consolidation.pool` in `lifecycle.py`). `BC-AC3a` flipped ✅ in v5.81 (build #67). `BC-AC3b` remains ⏳.
- **explanation:** Merges retrieval results across all astrocyte domain-specialist processes (code-patterns, decisions, errors, dependencies) into a single ranked consensus list. Each domain scores the query's memories independently (heat + semantic similarity × domain weight). Memories voted by multiple domains receive a multi-domain boost (+15% per additional vote). Exposed as an opt-in recall mode; the default (mode=None) is unchanged. Previously DEAD (defined but never called from any MCP path); now LIVE via the landscape dispatch in the recall tool.

---

### CAP-RETR-026 — Postmortem-Boost

- **status:** LIVE
- **category:** retrieval
- **settings:** `POSTMORTEM_BOOST_FACTOR`, `POSTMORTEM_BOOST_KEYWORDS`, `FANOUT_BOOST_SCOPE`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/recall_pipeline.py`
- **wiring:** Applied in the fanout recall pipeline via `_apply_fanout_boosts`. The postmortem boost fires when the query contains a keyword from `POSTMORTEM_BOOST_KEYWORDS` and a result has `_postmortem` or `_incident` tags. It uses the convex combination formula `base + (1 - base) × weight` to keep scores in [0, 1]. `FANOUT_BOOST_SCOPE` gates which callers receive the boost: `scoped` (default) applies only when `profile` is not None (hook=fast path), `global` always applies, `off` disables it. ADR-0215 deleted the C4 branch boost that used to live in the same function.
- **explanation:** A score-adjustment pass applied in the fanout pipeline after retriever output. Postmortem boost (`POSTMORTEM_BOOST_FACTOR=0.3`) elevates memories tagged `_postmortem` or `_incident` when the query sounds like incident investigation. `FANOUT_BOOST_SCOPE=scoped` (default) preserves pre-forward-only prod parity where the hook (profile=fast) path boosted and the default (profile=None) path did not, avoiding the −0.02 recall@5 regression observed on the default path in A/B testing. A sibling branch-match boost was removed by ADR-0215 along with branch scoping.

---

### CAP-RETR-027 — Recall Quality Floor and Dedup

- **status:** LIVE
- **category:** retrieval
- **settings:** `RECALL_QUALITY_FLOOR`, `RECALL_BOOST`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR5`
- **refs:** `yadgar/core/server/tools/recall.py::_apply_quality_floor`, `yadgar/core/server/tools/recall.py::_dedup_by_content`
- **wiring:** Applied in the `recall` tool immediately after retriever output and the postmortem boost. `_apply_quality_floor` drops results with `_cross_encoder_score` below `RECALL_QUALITY_FLOOR` (default 0.0 = disabled). `_dedup_by_content` collapses identical-content rows. `RECALL_BOOST` (default 0.05) is applied per-access in `consolidation/heat_decay.py` to increase memory heat on each retrieval.
- **explanation:** Two final hygiene passes. The quality floor targets keyword-only co-occurrence rows that score near zero on the cross-encoder; calibrated thresholds are 0.15–0.20 for production but default is 0.0 to avoid breaking tests with short synthetic content. Content deduplication collapses multiple rows with identical text (common for co-occurrence entries). `RECALL_BOOST` separately governs how much each `recall` access raises a memory's heat score in the heat-decay model.

---

### CAP-RETR-028 — Session Coherence Boost

- **status:** LIVE
- **category:** retrieval
- **settings:** `SESSION_COHERENCE_BONUS`, `SESSION_COHERENCE_WINDOW_HOURS`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/config/config.py`, `yadgar/backend/consolidation/heat_decay.py`
- **wiring:** `SESSION_COHERENCE_BONUS` is read by the heat-decay consolidation when computing memory scores for retrieval. Memories accessed within `SESSION_COHERENCE_WINDOW_HOURS` (default 4.0 hours) receive the bonus (default 0.2) applied during heat decay calculation. This effectively gives recently-accessed memories a higher base heat at retrieval time.
- **explanation:** Provides a temporal relevance signal at the heat-decay level: memories that were accessed in the current session window have their heat amplified so they are more likely to resurface in the same session context. Unlike the fusion-layer temporal signal (which parses date expressions), this is a continuous background nudge applied during consolidation.

---

### CAP-RETR-029 — Profile Search and Belief Search at Retrieval

- **status:** LIVE
- **category:** retrieval
- **settings:** `PROFILE_SEARCH_WEIGHT`, `BELIEF_HIGH_CONFIDENCE_BOOST`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/fusion.py::_FusionMixin._search_profiles_and_beliefs`, `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_profile_belief_merge`
- **wiring:** `_rerank_profile_belief_merge()` is called from `_apply_rerank_pipeline()` after CE reranking. It calls `_search_profiles_and_beliefs()` which searches structured profile rows via `storage.search_profiles_fts()` (gated on `PROFILE_EXTRACTION_ENABLED`) and derived beliefs via `storage.search_beliefs_fts()` (gated on `DERIVED_BELIEFS_ENABLED`). Profile results use `PROFILE_SEARCH_WEIGHT` as their retrieval score; belief results get `confidence × BELIEF_HIGH_CONFIDENCE_BOOST` if `confidence > 0.7`.
- **explanation:** After CE reranking, structured knowledge from the profile and belief stores is blended into results. Profile entries (entity–attribute–value triples) are synthetic memory-like dicts with negative IDs to distinguish them. High-confidence beliefs (> 0.7) are boosted by `BELIEF_HIGH_CONFIDENCE_BOOST` (default 1.2). This ensures that fact-like knowledge extracted during enrichment surfaces alongside episodic memories.

---

### CAP-RETR-030 — Dead Belief Config

- **status:** DEAD (v6 T3 — deleted from config.py)
- **category:** retrieval
- **settings:** — (BELIEF_MIN_CONFIDENCE, BELIEF_SEARCH_PRIORITY_FOR_OPEN_DOMAIN both deleted)
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** Both settings deleted from `config.py` in v6 T3. No production code ever read them.
- **explanation:** Two belief-retrieval tuning knobs that were planned but never implemented. Removed in v6 T3 dead-config cleanup (#41).

---

### CAP-RETR-031 — Embedding Model and Query Prefix Config

- **status:** LIVE
- **category:** retrieval
- **settings:** `EMBEDDING_MODEL`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/embeddings/embeddings.py::EmbeddingEngine`, `yadgar/_shared/embeddings/remote_embeddings.py`
- **wiring:** `EMBEDDING_MODEL` (default `"all-MiniLM-L6-v2"`) is read by `EmbeddingEngine.__init__` to load the sentence-transformer model. The engine is created at server startup and passed to `Retriever`. All `encode_query()` calls flow through it.
- **explanation:** Selects the sentence-transformer embedding model used for vector encoding of both stored memories (at write time) and queries (at retrieve time). Changing this requires re-embedding all stored memories (`reembed_all` admin operation). Model-specific query prefixes (e.g. "query: " for E5 models) are defined in the `MODEL_QUERY_PREFIX` constant dict in `embeddings.py`, not via `QUERY_PREFIX` setting.

---

### CAP-RETR-032 — Dead Query Prefix and Embedding Cache Size Config

- **status:** DEAD (v6 T3 — deleted from config.py)
- **category:** retrieval
- **settings:** — (QUERY_PREFIX, EMBEDDING_CACHE_SIZE both deleted)
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** Both settings deleted from `config.py` in v6 T3. Never read by production code — embedding engine uses `MODEL_QUERY_PREFIX` constant dict; the query-embedding LRU cache (`embeddings.py:14`, `remote_embeddings.py:25`) hard-codes `_CACHE_MAX = 512`, no env var. `YADGAR_EMBED_CACHE_MAX_ENTRIES` is a different, backend-side embed-service cache knob (`embed_service_config.py`), itself superseded by a byte-budget (`YADGAR_BACKEND_CACHE_RAM_PCT`) and not what `EMBEDDING_CACHE_SIZE` sized.
- **explanation:** Two dead configuration knobs removed in v6 T3 (#41). `QUERY_PREFIX` was never wired into `encode_query()`. `EMBEDDING_CACHE_SIZE` was NOT superseded by an env-var-driven cache — corrected 2026-08-13 (Car 7): the query-embedding LRU caches it once sized remain hard-coded at `_CACHE_MAX = 512`.

---

### CAP-RETR-033 — Reranking Semaphore and Backend Timeout

- **status:** LIVE
- **category:** retrieval
- **settings:** `RERANK_BACKEND_TIMEOUT_SEC`, `RERANK_MAX_CONCURRENCY`, `RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC`, `MODEL_IDLE_EVICTION_SECONDS`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/embed_service/embed_service.py`, `yadgar/backend/ml_client/ml_client.py`
- **wiring:** `RERANK_MAX_CONCURRENCY` (default 8 — raised from 1 in Fix A O7 in lockstep with `TOOL_POOL_WORKERS`; read by the BACKEND container, needs a backend rebump/env to take effect) and `RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` (default 2.0) are read at `embed_service` module initialisation to configure the asyncio semaphore that gates concurrent CE/NLI scoring requests. `RERANK_BACKEND_TIMEOUT_SEC` (default 90) is used as the timeout for remote ML backend calls in `LocalMLClient`. `MODEL_IDLE_EVICTION_SECONDS` (default 0 = never evict; v5.95 config-integrity — promoted from env-only to config.yaml-authoritative via `resolve_knob`) is read by `ml_client.py` to evict a loaded model after that many idle seconds.
- **explanation:** Guards the cross-encoder and NLI inference path against concurrent overload. A semaphore with `RERANK_MAX_CONCURRENCY` slots serialises concurrent scoring requests; if the semaphore cannot be acquired within `RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` the request is dropped with a counter increment. `RERANK_BACKEND_TIMEOUT_SEC` gives the backend inference itself a hard ceiling before aborting.

---

### CAP-RETR-034 — Recall MCP Tool

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RU3`, `BC-RU1`, `BC-RU2`
- **refs:** `yadgar/core/server/tools/recall.py::recall`
- **wiring:** Registered with `@_tool()` decorator. Invoked by Claude Code agents directly. Routes to `Retriever.recall_via_pipeline()` when `profile` kwarg is set, else `Retriever.recall()`. Applies directory validation, the postmortem boost, quality floor, dedup, and wiki blending.
- **explanation:** The primary MCP tool for episodic and semantic memory retrieval. Accepts a natural-language query, optional `max_results`, `min_heat`, `profile`, and `directory`. Orchestrates the full retrieval pipeline: multi-signal scoring → fusion → heuristic reranking → CE reranking → NLI → multi-passage → profile/belief merge → MMR → adversarial detection → rules → engram links → quality floor → dedup → postmortem boost → wiki blending. Returns a ranked list of memory dicts with `_retrieval_score`, `_cross_encoder_score`, heat, and metadata fields.

---

### CAP-RETR-035 — Rules Engine Reranking

- **status:** LIVE
- **category:** retrieval
- **settings:** —
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR4`, `BC-RU1`, `BC-RU2`, `BC-RU3`
- **refs:** `yadgar/backend/retrieval/reranking.py::_RerankingMixin._rerank_rules`
- **wiring:** `_apply_rerank_pipeline()` calls `_rerank_rules()` after MMR and trim. Requires `self._rules_engine` to be non-None (set via `Retriever.set_rules_engine()`). Delegates to `rules_engine.apply_rules(result_memories, directory)`.
- **explanation:** Applies neuro-symbolic rules stored via `add_rule` to boost or penalise candidates based on content patterns, tags, or directory context. Rules are directory-scoped (added for a specific project) and applied at the end of the rerank pipeline, after all ML scoring. The rules engine is set externally on the retriever during server startup; if not set (rules engine not configured), this stage is a no-op.

---

### CAP-RETR-036 — Similarity Matrix Cap (Consolidation-Adjacent)

- **status:** LIVE
- **category:** retrieval
- **settings:** `SIMILARITY_MATRIX_MAX_CANDIDATES`
- **tools:** —
- **migrations:** —
- **bc:** `BC-SC2`, `BC-SC3`
- **refs:** `yadgar/backend/consolidation/cls.py`
- **wiring:** Read by `yadgar/backend/consolidation/cls.py` at three points: community detection and cluster summarization candidate selection are both bounded by this cap (default 4000). Not a retrieval-path setting but controls memory graph operations that feed the retrieval index.
- **explanation:** Caps the number of memory candidates processed in one similarity-matrix computation during nightly consolidation (community detection, cluster summarization). On large corpora, computing the full N×N similarity matrix is O(N²); this cap bounds memory and CPU consumption per consolidation cycle. Indirectly affects retrieval quality by limiting how many memories are considered for clustering and summarization.

---

### CAP-RETR-037 — Sleep Cycle (Dream Replay, Community, Compress, Narrate)

- **status:** LIVE
- **category:** retrieval
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-SC1a`, `BC-SC1b`, `BC-SC4`, `BC-SC5`, `BC-SC6`
- **refs:** `yadgar/backend/consolidation/cls.py`
- **wiring:** Triggered by the nightly consolidation cycle (idle-based). Each sub-phase (dream replay, community detection, cluster summarization, reembed_stale, compress_old_memories, auto_narrate) runs sequentially within the cycle. BC-SC1b and BC-SC1a are covered by tests verifying dream replay fires and produces a derived link.
- **explanation:** The nightly sleep cycle runs six consolidation sub-phases that maintain the retrieval index quality: dream replay surfaces latent memory pairs into derived links; community detection clusters the graph; cluster summarization writes semantic summaries; reembed_stale re-embeds memories after a model change; compress_old_memories gists aged memories to save tokens; auto_narrate writes a project story. All phases read from and write to the same storage/graph that `recall` queries.

---

### CAP-RETR-038 — Astrocyte Pool Domain Assignment and Consolidation

- **status:** LIVE
- **category:** retrieval
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-AC1`, `BC-AC2`
- **refs:** `yadgar/_shared/astrocyte_pool/astrocyte_pool.py`
- **wiring:** Called during consolidation cycles. `assign_memory()` routes memories to domain-specialist astrocyte processes. `consolidate_domain()` runs domain-level summarization. Both are exercised in e2e tests (BC-AC1, BC-AC2 marked ✅).
- **explanation:** Domain-aware consolidation: memories are assigned to semantic domains (e.g. "code", "decisions") by the astrocyte pool. Each domain runs its own consolidation pass, producing domain summaries. This supplements the global consolidation by maintaining domain-coherent clusters. The pool can be disabled via `ASTROCYTE_POOL_ENABLED=False`; when disabled a startup warning is emitted (BC-C5b pending #40).

### CAP-RETR-040 — Recall Backend Forwarding (Train 1)

- **status:** LIVE
- **category:** retrieval
- **settings:** — (Phase 2a: recall is always forwarded to backend; flag removed)
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/recall_pipeline.py::_fanout_recall`, `yadgar/backend/retrieval/recall_pipeline.py::_apply_recall_db_side_effects`, `yadgar/backend/retrieval/recall_pipeline.py::_apply_recall_session_side_effects`, `yadgar/backend/embed_service/embed_service.py::recall_route`, `yadgar/core/server/tools/recall.py::_forward_to_backend`
- **wiring:** Phase 2a (always-on): core recall() unconditionally POSTs to the backend /recall endpoint via _forward_to_backend(); backend runs _fanout_recall (all modes/profiles) + _apply_recall_db_side_effects; core runs _apply_recall_session_side_effects on results. RECALL_BACKEND_ENABLED and UNIFIED_RECALL_ENABLED flags removed — no fallback path.
- **explanation:** Train 1 of the recall pipeline backend migration. Extracts the _fanout_recall orchestrator and related helpers into _recall_pipeline.py (app-free module) so both core and backend share the pipeline code without import-side-effects. Splits _apply_recall_side_effects into DB half (backend) and session half (core). Backend /recall route wired with same Bearer auth as /rerank. Default False — safe no-op merge.

---

### CAP-RETR-041 — Recall Output Size Cap (task:0085)

- **status:** LIVE
- **category:** retrieval
- **settings:** `RECALL_MAX_CONTENT_CHARS`, `RECALL_MAX_TOTAL_BYTES`
- **tools:** `recall`, `adr_list`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/recall.py::_shape_recall_results`, `yadgar/core/server/tools/recall.py::_RECALL_PROJECTION_DENYLIST`, `yadgar/core/server/tools/recall.py::_fetch_hint`, `yadgar/core/server/tools/recall.py::_resolve_shape_limit`, `yadgar/core/server/tools/adr.py::adr_list`
- **wiring:** `recall()` calls `_shape_recall_results()` on the backend rows before returning, applying (1) `_RECALL_PROJECTION_DENYLIST` field projection, (2) a per-row content cap from `RECALL_MAX_CONTENT_CHARS` (default 1200) with a visible `_truncated` marker, and (3) a total-byte backstop from `RECALL_MAX_TOTAL_BYTES` (default 65536) that drops the lowest-ranked rows behind one `_dropped` marker. Resolution is three-layer: per-call `recall(max_chars=N)` → per-directory ADR-0163 runtime-config rows `recall.max_content_chars` / `recall.max_total_bytes` via `_resolve_shape_limit` → the `Settings` default. The seam is inside `recall()` only — the prompt-recall hook path (`http.py:1309-1323`) keeps its own `max_chars=3000` budget and is untouched, and the backend `/recall` wire contract is unchanged (no `BACKEND_VERSION` bump). `adr_list` gains `limit` (default 50) / `offset` pagination.
- **explanation:** `recall()` was a pure forwarder with zero size bound, so `max_results` — a row-count proxy for a byte problem — was the only lever; an unlucky topic produced ~78 KB, exceeded the harness tool-output cap and returned unusable, pushing agents off the memory system and back to grep. Memory rows and wiki rows fail differently (memory rows spent 38.8% of a measured 4132 B row on scoring/thermodynamic internals; wiki rows carry full page bodies), so projection and content-capping are both required. The projection is a DENYLIST so fields the retrieval pipeline adds later default to visible — an allowlist would have silently deleted `consensus_score` / `voting_domains`, which `mode="landscape"` stamps as part of the documented return contract. Truncation is deliberately VISIBLE: the `_truncated` marker carries `kept` / `total` and an exact-ID `fetch` hint (`memory_get(<id>)` / `wiki_read("<slug>")`) so a trimmed row is recoverable, improving on `recent_memories`' bare `"..."`. Shaping runs strictly after retrieval, rerank and fusion — ranking and recall quality are untouched. The deferred session-side-effect closure keeps receiving the UNTRIMMED rows. `RECALL_MAX_TOTAL_BYTES=65536` is UNCALIBRATED — chosen as comfortably under the observed 78 KB failure, not measured against the real harness cap; it is a knob so it can be retuned without a code change. `adr_list` had no `limit` at all and a full listing measured 57 KB; its rows are already narrow, so the size is row COUNT and pagination is the fix.

---

### CAP-RETR-039 — Unified Scoped Recall Fan-Out (v6 T6)

- **status:** LIVE
- **category:** retrieval
- **settings:** `RECALL_MEMORY_QUOTA`, `RECALL_WIKI_QUOTA`, `RECALL_MEMORY_PRIOR_WEIGHT`, `RECALL_WIKI_PRIOR_WEIGHT` (UNIFIED_RECALL_ENABLED removed Phase 2a — fanout always-on; the recall downweight factor was removed by Car C7 — see wiring)
- **tools:** `recall`, `wiki_query`
- **migrations:** `023`
- **bc:** `BC-G11`, `BC-U1`, `BC-U2`, `BC-U3`, `BC-U4`, `BC-U5`, `BC-U6`, `BC-U7`, `BC-U8`
- **refs:** `yadgar/backend/retrieval/providers/base.py::SourceProvider`, `yadgar/backend/retrieval/providers/base.py::Candidate`, `yadgar/backend/retrieval/providers/memory.py::MemoryProvider`, `yadgar/backend/retrieval/providers/wiki.py::WikiProvider`, `yadgar/backend/retrieval/providers/fusion.py::fuse_candidates`, `yadgar/_shared/storage/directory.py::build_recall_scope_clause`, `yadgar/core/server/tools/recall.py::_fanout_recall`
- **wiring:** `recall()` routes through `_fanout_recall()` by default (v5.80: `UNIFIED_RECALL_ENABLED=True`). Steps 0–5 (v6 T6): (0) eval harness; (3a) ScopeFilter dataclass; (3b) directory scoping in MemoryProvider + WikiProvider; (4) cross-type CE fusion via `fuse_candidates` (per-type quotas → CE rerank → additive prior boost → provenance dedup → trim); (5) `recall(type=)` param for source-type filtering + `wiki_query` deprecation log. `RECALL_MEMORY_QUOTA`/`RECALL_WIKI_QUOTA` bound each source's candidate pool before CE rerank. `RECALL_MEMORY_PRIOR_WEIGHT`/`RECALL_WIKI_PRIOR_WEIGHT` are additive priors folded into CE scores. **Car C7 (0047 §5 C7): scoping moved into the STAGE-1 `WHERE`.** `build_recall_scope_clause` emits one predicate carrying the project (`project_id = $p`), the cross-project `global` reach tag, and a `page_type` exclusion DERIVED from `POLICY_BY_TYPE` — replacing the `ScopeFilter` bundle and the `is_directory_eligible` Python post-filters, which spent the query's LIMIT before filtering. The scoped vector arm uses brute-force cosine rather than HNSW + predicate, because the KNN operator picks its neighbours before any predicate is applied (top-K-then-filter silently under-returns). The recall downweight factor and its `downweight_multiplier` helper are DELETED: their only user (`task_list`) is now `recall_disposition="exclude"`, and the multiply carried a sign bug — `placement_score` is `ce + w*native` and `ce` is a raw cross-encoder logit that is commonly negative, so a sub-1.0 factor RAISED the score and promoted the pages it was meant to sink. Phase 2a: fanout path is always-on; UNIFIED_RECALL_ENABLED flag removed.
- **explanation:** Unified recall architecture (v6 T6 — `[[unified-scoped-recall]]`). `SourceProvider` ABC with `type: str` + `candidates(query, scope, limit) -> list[Candidate]`. Both providers apply Python-side `is_directory_eligible()` post-filter matching the legacy path. `fuse_candidates` in `yadgar/backend/retrieval/providers/fusion.py` runs CE rerank, additive prior boost, and cross-type provenance dedup (memory id ∈ wiki.source_memory_ids → keep higher-CE). `recall(type="memory"|"wiki"|"all")` routes to the appropriate provider subset. `wiki_query` emits INFO deprecation log when flag is ON. LIVE as of v5.80 (always-on). Phase 2a: flag removed; fanout path unconditional. Fusion fixes shipped in v5.80 (single-provider bypass + memory-order-stable fuse_candidates) ensure no memory-ranking regression. The single-provider bypass triggers whenever EITHER pool is empty — covering explicit `type="memory"`/`"wiki"` AND `type="all"` where one pool returned no candidates (e.g. no relevant wiki); `fuse_candidates` runs only when both pools are non-empty, so a memory-only pool is never CE-reranked a second time (BC-U6/U7/U8).

---

### CAP-STOR-001 — SurrealDB transport layer and batch writes
- **status:** LIVE
- **category:** storage
- **settings:** `DB_PATH`, `MAX_BATCH_STATEMENTS`, `MAX_BATCH_BYTES`
- **tools:** —
- **migrations:** —
- **bc:** `BC-ST1`, `BC-ST2`, `BC-ST3`, `BC-ST4`
- **refs:** `yadgar/_shared/storage/client.py::_ClientMixin`, `yadgar/_shared/storage/client.py::batch_writes`
- **wiring:** Every MCP tool and consolidation cycle calls `StorageEngine._q()` or `batch_writes()`. `_q()` routes to `_q_server()` (HTTP POST to SurrealDB v3) when `YADGAR_DB_URL` is set, or `_q_embedded()` (Python surrealdb SDK, SurrealDB v2) otherwise. `DB_PATH` sets the embedded database file location (default `~/.local/share/yadgar/surreal_db/`). `batch_writes()` splits statements into chunks bounded by `MAX_BATCH_STATEMENTS` (default 500) and `MAX_BATCH_BYTES` (default 1 MB), each sent as a `BEGIN…COMMIT` HTTP transaction. Embedded mode executes statements individually without HTTP transactions. `BC-ST3`: embedded mode rewrites `type::record('t', $id)` → `t:{int}` via `_inline_int_record_ids`. `BC-ST4`: server vs embedded mode selected by presence of `YADGAR_DB_URL` env var.
- **explanation:** The transport layer is the only interface between all storage mixins and the SurrealDB database. In server mode it serialises queries as LET-preamble + SQL over HTTP and raises on `status=ERR` entries. In embedded mode it delegates to the Python SDK with a retry on read-only statements. `batch_writes` prevents oversized HTTP bodies by measuring the real serialised body size recursively; a single-statement chunk that still exceeds the limit is sent with a warning rather than silently dropped.

### CAP-STOR-002 — Schema initialisation and migration runner
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `001`, `002`, `003`, `004`, `005`, `006`, `007`, `008`, `009`, `010`, `011`, `012`, `013`, `014`, `015`, `016`, `018`, `019`, `020`, `021`, `022`, `023`, `024`, `025`
- **bc:** `BC-ST2`
- **refs:** `yadgar/_shared/storage/migrations.py::_MigrationsMixin`, `yadgar/_shared/storage/migrations.py::_run_migrations`, `yadgar/_shared/storage/migrations.py::_init_schema`
- **wiring:** `StorageEngine._init_schema()` is called once on startup from `StorageEngine.__init__()` (via `_ClientMixin` assembly). It defines all tables, analysers, and indexes, then calls `_run_migrations()`. `_run_migrations()` is a no-op in embedded mode; in server mode it acquires a file lock and calls `_run_migrations_locked()` which iterates `_MIGRATIONS` in order, skipping already-applied versions recorded in the `schema_version` table. Each migration is applied exactly once; the version string is appended to `schema_version` atomically after `fn(storage)` returns.
- **explanation:** The migration runner enforces forward-only, exactly-once schema evolution. It uses an flock on `STATE_DIR/.migration.lock` to serialise concurrent daemon starts. Migrations are append-only (never reordered or edited). The `_MIGRATIONS` list contains 23 entries (versions 001–024, with 017 reserved). All migrations use `DEFINE FIELD IF NOT EXISTS` or `DEFINE INDEX IF NOT EXISTS` DDL which is idempotent, so a failed-then-rerun migration does not corrupt the schema.

### CAP-STOR-003 — Migration 001: HNSW vector index upgrade
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `001`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_001_hnsw_indexes`
- **wiring:** Applied once on first server-mode startup after SurrealDB v3 upgrade. Drops old MTREE indexes on `memory.embedding`, `memory.implicit_embedding`, and `wiki_page.embedding`; recreates them as HNSW (EFC=150, M=12, COSINE, F32). Subsequent startups skip via `schema_version` guard.
- **explanation:** SurrealDB v3 introduced HNSW (Hierarchical Navigable Small World) vector indexes which outperform MTREE for ANN search at scale. This migration upgrades all three vector indexes to HNSW while leaving MTREE in place for the embedded (v2) SDK path defined in `_init_schema`.

### CAP-STOR-004 — Migration 002: relationship table performance indexes
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `002`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_002_relationship_indexes`
- **wiring:** Applied once at server-mode startup after v4.4.1. Adds `rel_source_target_idx` and `rel_target_source_idx` composite indexes on the `relationship` table.
- **explanation:** Adds bidirectional composite indexes (source→target and target→source) on the `relationship` table to speed up neighbourhood traversal and entity-linking lookups. Before this migration, relationship queries with `source_entity_id` or `target_entity_id` predicates required full-table scans.

### CAP-STOR-005 — Migration 003: memory_similarity_link table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `003`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_003_memory_similarity_link_table`, `yadgar/_shared/storage/cluster.py::_ClusterMixin`
- **wiring:** Applied once at server-mode startup after v4.4.2. Creates the `memory_similarity_link` SCHEMALESS table with a UNIQUE pair index. Used by `_ClusterMixin.insert_memory_similarity_link()` during consolidation CLS phase.
- **explanation:** Extracts memory-to-memory similarity edges from the entity table into a dedicated `memory_similarity_link` table, stopping entity-table bloat. The UNIQUE index on `(source_memory_id, target_memory_id)` prevents duplicate edges. The table stores cosine weight, creation/update timestamps, and bi-temporal validity columns added by migration 007.

### CAP-STOR-006 — Migration 004: branch field on memory and wiki_page
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `004`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_004_branch_field`
- **wiring:** Applied once at server-mode startup after v5. Adds `branch option<string>` column to `memory` and `wiki_page`; backfills pre-v5 rows to `'master'` in a single transaction. All subsequent memory and wiki inserts stamp the current branch. ADR-0215 retired every READER of this field; the column itself is dropped by migration 029.
- **explanation:** Enabled per-branch memory and wiki scoping: rows with `branch IS NONE` or `branch = default_branch` were visible to any branch context, while rows stamped with a feature branch were visible only when that branch was active. ADR-0215 removed that scoping entirely — the retrieval-side readers are gone as of the read-path car, and the column is retired by migration 029. This entry is retained because migration 004 itself is immutable history.

### CAP-STOR-007 — Migration 005: provenance_agent field
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `005`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_005_provenance_agent_field`, `yadgar/_shared/storage/memory.py::_validate_provenance_agent`
- **wiring:** Applied once at server-mode startup after v5.3. Adds `provenance_agent string DEFAULT 'default'` to `memory`; backfills `NULL` rows to `'default'`. The CRDT post-write phase stamps this field with `CRDT_AGENT_ID` on every new memory.
- **explanation:** Records which agent (Claude Code instance, daemon, consolidation process) created each memory row. The field is constrained to ASCII alphanumeric/hyphen/underscore (≤64 chars) by `_validate_provenance_agent` to prevent SQL-injection. Default value `'default'` applies to all pre-v5.3 rows and any write that does not specify an agent.

### CAP-STOR-008 — Migration 006: source_memory_id citation provenance on KG edges
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `006`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_006_source_memory_id`
- **wiring:** Applied once at server-mode startup after v5.3.3. Adds nullable `source_memory_id` to `causal_dag_edge` and `relationship`, and `citation_source_memory_id` to `memory_similarity_link`. Written by `insert_typed_relationship` (C3) and `insert_memory_similarity_link` when a triggering memory is known.
- **explanation:** Adds citation provenance to all three KG edge tables so that downstream graph traversal and audit tools can trace which memory observation caused a given entity link, causal edge, or similarity link. All columns are nullable; existing rows receive NULL (backward-compatible). The field is named differently on `memory_similarity_link` because `source_memory_id`/`target_memory_id` are already the edge endpoint keys on that table.

### CAP-STOR-009 — Migration 007: bi-temporal validity columns on KG edge tables
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `007`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_007_bitemporal_edges`, `yadgar/_shared/storage/entity.py::_EntityMixin.insert_typed_relationship`
- **wiring:** Applied once at server-mode startup after v5.3.4. Adds `valid_from option<string>` and `valid_until option<string>` to `causal_dag_edge`, `relationship`, and `memory_similarity_link`. Backfills `valid_from = created_at` for existing rows. New inserts via `insert_relationship` and `insert_typed_relationship` set `valid_from = now()`; `valid_until = NULL` means currently valid.
- **explanation:** Implements bi-temporal edge validity (C1) across all three KG edge tables. The filter `valid_until IS NONE OR valid_until > now()` selects currently-valid edges. Storing timestamps as ISO-8601 strings (not SurrealDB `datetime`) avoids type-coerce issues between SurrealDB v2 and v3.

### CAP-STOR-010 — Migration 008: anchor tier, valid_until, migration_grace on memory
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_CONDITIONAL_TTL_DAYS`
- **tools:** `anchor`
- **migrations:** `008`
- **bc:** `BC-AN1`, `BC-AN2`
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_008_anchor_tier`, `yadgar/_shared/storage/memory.py::get_anchored_memories`
- **wiring:** Applied once at server-mode startup after v5.8.0. Adds `tier`, `valid_until`, and `migration_grace` columns to `memory`. Backfills existing `_anchor`-tagged rows without `tier` to `tier='conditional'`, `valid_until=now()+ANCHOR_CONDITIONAL_TTL_DAYS`, `migration_grace=True`. The `get_anchored_memories` and `get_anchored_memories_scoped` queries filter by `valid_until IS NONE OR valid_until > now()` to exclude expired anchors.
- **explanation:** Introduces the three-tier anchor system (semantic_immortal / conditional / ephemeral). Pre-v5.8 anchors receive `migration_grace=True` so they can be identified and reviewed by `audit_anchors` rather than silently expiring. The `valid_until` field drives anchor expiry without requiring a separate cleanup table.

### CAP-STOR-011 — Migration 009: wiki_bookmark table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `009`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_009_wiki_bookmark_table`, `yadgar/_shared/storage/bookmarks.py`
- **wiring:** Applied once at server-mode startup after v5.23.0. Creates `wiki_bookmark` SCHEMALESS table with a UNIQUE slug index and a position index. Used by `BookmarksMixin` CRUD methods called from `bookmark_add`, `bookmark_remove`, `bookmark_list`, and `bookmark_reorder` MCP tools.
- **explanation:** Adds a dedicated table for pinned wiki pages. The UNIQUE index on `slug` ensures exactly one bookmark per page. The `position` field is a dense integer (0-based) managed application-side by the storage layer. Also defined redundantly in `_init_wiki_indexes` for fresh installs (idempotent `IF NOT EXISTS`).

### CAP-STOR-012 — Migration 010: bi-temporal user_profile
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `010`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_010_bitemporal_user_profile`
- **wiring:** Applied once at server-mode startup after v5.29.0 (Adopt-3). Adds `valid_from` and `valid_until` to `user_profile`; drops the old UNIQUE constraint on (entity_name, attribute_type, attribute_key, directory_context) since SurrealDB v3 does not support partial/conditional UNIQUE indexes. Uniqueness for active rows is enforced application-side in `insert_profile`.
- **explanation:** Pivots user_profile from "UPSERT in-place" to "close prior row + insert new row" semantics, enabling a temporal history of attribute values per entity. The removed DB-level UNIQUE constraint is replaced by an application-side check for existing rows with `valid_until IS NONE` before inserting.

### CAP-STOR-013 — Migration 011: bi-temporal derived_belief
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `011`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_011_bitemporal_derived_belief`
- **wiring:** Applied once at server-mode startup after v5.29.0 (Adopt-3). Adds `valid_from` and `valid_until` to `derived_belief`; backfills `valid_from = created_at`.
- **explanation:** Extends bi-temporal semantics to the `derived_belief` table. Beliefs are append-only (no UPSERT), so no UNIQUE constraint change was needed. The `valid_until IS NONE` condition identifies currently-active beliefs; closing a belief is done by setting `valid_until` on the prior row.

### CAP-STOR-014 — Migration 012: memory_block table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `012`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_012_memory_block_table`, `yadgar/_shared/storage/blocks.py`
- **wiring:** Applied once at server-mode startup after v5.33.0 (Adopt-4). Creates the `memory_block` SCHEMALESS table with indexes on `(name, scope, directory)` and `(scope, directory)`. Used by the `block_create`, `block_get`, `block_list`, `block_update`, `block_delete`, `block_append`, `block_replace` MCP tools via `BlocksMixin`.
- **explanation:** Adds named in-context memory blocks as a dedicated table separate from the main `memory` table. Isolation prevents cross-contamination with anchor audit, heat decay, and secret-gate scans. The table schema is SCHEMALESS; uniqueness on `(name, scope, directory)` is enforced application-side.

### CAP-STOR-015 — Migration 013: wiki_page_version table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `013`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_013_wiki_page_version`
- **wiring:** Applied once at server-mode startup after v5.41.0. Creates `wiki_page_version` with three indexes (page_id, UNIQUE (page_id, version), created_at); seeds version=1 rows from existing `wiki_page` rows on first run. Subsequent `wiki_add` and `wiki_update` calls append new version rows. Idempotency guard: skips pages that already have version rows.
- **explanation:** Implements per-write immutable version history for wiki pages. Every subsequent `wiki_add` or `wiki_update` appends a new row with an incremented version number. The embedding field is intentionally excluded from version rows (storage cost; recomputed on restore). The UNIQUE `(page_id, version)` index enforces correct monotonic ordering.

### CAP-STOR-016 — Migration 014: wiki_page embedding backfill registration
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `014`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_014_wiki_page_embedding_backfill`
- **wiring:** Applied once at server-mode startup after v5.42.1. The migration itself only logs a count of NULL-embedding wiki_page rows and marks the slot as applied. The actual backfill runs via `WikiStore.backfill_null_embeddings()` called from `server/lifecycle.py` after both `StorageEngine` and `EmbeddingEngine` are ready (lifecycle startup, not the migration runner).
- **explanation:** Registers a schema version slot for the wiki embedding backfill without executing the backfill in the migration itself (which runs before the EmbeddingEngine is initialised). The split is intentional: migrations must be thin and dependency-free; the actual backfill is a one-time lifecycle startup cost.

### CAP-STOR-017 — Migration 015: branch column on wiki_draft
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `015`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_015_wiki_draft_branch`
- **wiring:** Applied once at server-mode startup after v5.42.3. Adds `branch option<string>` to `wiki_draft`. (table dropped in migration 026, v5.157.0 — this historical migration is retained for immutability)
- **explanation:** Prior to this migration, draft content stored in `wiki_draft` carried no branch context. Adding `branch` to `wiki_draft` allowed per-branch scoping of draft rows. The `wiki_draft` table and the draft-workflow tools (`wiki_drafts`, `wiki_approve`, `wiki_discard`) were subsequently removed in migration 026 (v5.157.0, Fix #76) — no production path ever produced drafts; `wiki_add` always committed directly.

### CAP-STOR-018 — Migration 016: directory_context on wiki_page, memory, wiki_draft
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `016`
- **bc:** `BC-DC1`, `BC-DC2`
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_016_directory_context`, `yadgar/_shared/storage/migrations.py::_classify_directory_by_tags`
- **wiring:** Applied once at server-mode startup after v5.42.5. Multi-phase: (A) backfill wiki_page rows using tag-based heuristic; (B) define NOT NULL schema constraint on `wiki_page.directory_context`; (C) add index; (D) backfill memory rows (NULL/'' → 'global'); (E) define NOT NULL constraint on `memory.directory_context`; (F) add nullable `directory_context` to `wiki_draft` (table subsequently dropped in migration 026). The tag heuristic maps `'yadgar'→/home/max/git/yadgar`, `'nix'→/home/max/git/nix`, `'ledger'→/home/max/git/ledger`, AWS tags→`/home/max/aws-work`, else `'global'`.
- **explanation:** Enforces `directory_context` as a non-nullable field on both `memory` and `wiki_page` so every record is scoped to a project or `'global'`. This is the foundation for BC-DC1 (eligible set predicate) and BC-DC2 (no container fallback). A bug in this migration (described in migration 018) caused a partial backfill on deployed databases.

### CAP-STOR-019 — Migration 018: directory_context backfill repair
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `018`
- **bc:** `BC-DC1`, `BC-DC2`
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_018_directory_context_backfill_repair`
- **wiring:** Applied once at server-mode startup after v5.42.6, after migration 016. Re-runs the tag-based backfill using a Python-side filter (rather than SurrealDB `WHERE IS NONE`) which correctly detects field-absent rows that the database query missed. Temporarily relaxes the NOT NULL constraint (DEFINE FIELD OVERWRITE → option<string>) before the UPDATE, then re-tightens it. Note: migration 017 is reserved for v5.61 wiki_source_hash.
- **explanation:** Fixes a SurrealDB v3 behaviour where `WHERE directory_context IS NONE` only matches rows with an explicit NULL value, not rows where the field is entirely absent (pre-DEFINE records). The workaround — relax constraint, backfill, re-tighten — is required because SurrealDB validates ALL defined fields on every UPDATE, causing coerce errors for field-absent rows even when the field being updated is unrelated.

### CAP-STOR-020 — Migration 019: wiki_page_type and wiki_schema_version fields
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `019`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_019_wiki_page_type`
- **wiring:** Applied once at server-mode startup after v5.53.2. Adds `page_type option<string>` and `wiki_schema_version option<int>` to `wiki_page`. Both are nullable; existing pages have `NONE` which is the correct untyped state. New typed pages written via `wiki_add(page_type=...)` set these fields.
- **explanation:** Introduces a typed wiki page system (B-schema). `page_type` is one of the registered page type keys (function, module, service, architecture, decision, analysis). `wiki_schema_version` stamps which schema generation the row was written under (1 = v5.53.2 B-schema; 0/absent = pre-5.53.2). Both fields are nullable for backward compatibility with legacy pages.

### CAP-STOR-021 — Migration 020: graph_prior field on memory
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `020`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_020_memory_graph_prior`, `yadgar/_shared/storage/memory.py::update_memory_graph_prior`, `yadgar/_shared/storage/memory.py::get_memory_graph_priors`
- **wiring:** Applied once at server-mode startup after v5.54.1. Adds `graph_prior option<float>` to `memory`. Computed by `ConsolidationScheduler._compute_graph_priors()` during each consolidation cadence and stored via `update_memory_graph_prior()`. Read back during retrieval fusion by `get_memory_graph_priors()`.
- **explanation:** Stores a precomputed entity-graph centrality scalar on each memory row to avoid graph traversal on the request path. A higher `graph_prior` means the memory is more central in the entity-relationship graph; the fusion layer uses it as an additive boost during reranking. Absent or NULL is treated as 0.0 (no boost). Staleness window is one consolidation cycle.

### CAP-STOR-022 — Migration 021: cofire_prior (co-recall transition) field on memory
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `021`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_021_memory_cofire_prior`, `yadgar/_shared/storage/memory.py::update_memory_cofire_prior`, `yadgar/_shared/storage/memory.py::get_memory_cofire_priors`
- **wiring:** Applied once at server-mode startup after v5.54.2. Adds `cofire_prior option<float>` to `memory`. Computed by `ConsolidationScheduler._compute_cofire_priors()` from `memory_transition.count` sums, normalised to [0,1]. Stored via `update_memory_cofire_prior()` and read by `get_memory_cofire_priors()` during retrieval fusion.
- **explanation:** Records a precomputed co-recall (transition-edge) prior: the sum of `memory_transition.count` for all transitions where the memory appears as `from_memory_id` or `to_memory_id`, normalised across the candidate set. "Recalled together before" signals learned associations. The fusion layer adds this as a secondary boost signal alongside `graph_prior`.

### CAP-STOR-023 — Migration 022: shadow-gate fields on memory
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `022`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_022_shadow_gate_fields`, `yadgar/_shared/storage/client.py::_MEMORY_UPDATABLE_FIELDS`
- **wiring:** Applied once at server-mode startup after v5.73.0. Adds `surprise_score option<float>` and `would_reject option<bool>` to `memory`. Both are nullable; no backfill needed. Written by the write-gate shadow mode via `update_memory_fields`. `WRITE_GATE_THRESHOLD` stays 0.0 (nothing dropped); this is observation-only.
- **explanation:** Stores the write-gate's surprisal score and shadow rejection decision on each memory row. `surprise_score` is the gate's surprisal value (distinct from the thermodynamics `compute_surprise()` score used for heat boost). `would_reject` is `True` when the gate would reject the memory at `WRITE_GATE_SHADOW_THRESHOLD`, enabling shadow analysis without actual rejection. Both fields are in `_MEMORY_UPDATABLE_FIELDS` so they can be patched via `update_memory_fields`.

### CAP-STOR-025 — Directory scoping enforcement
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `anchor`, `memory_get`, `memory_update`, `forget`, `validate_memory`
- **migrations:** —
- **bc:** `BC-DC1`, `BC-DC2`
- **refs:** `yadgar/_shared/storage/memory.py::get_memories_for_directory`, `yadgar/core/server/tools/wiki.py`, `yadgar/backend/queue_drainer/dlq.py`
- **wiring:** Unconditional at write time in `wiki.py` and `queue_drainer/dlq.py` — C5 of the 0047 remediation train DELETED the enforcement knob that used to gate it (ADR-0227: identity is never defaulted, so the gate has no OFF position; ADR-0225's end condition for the knob is met by C6's registry check). Memory reads use `directory_context` predicates to scope results (BC-DC1: eligible set = {caller_dir, global, '', None}) until C7 re-keys them onto `project_id`. BC-DC2: hard-require directory on reads; no `os.getcwd()` container fallback allowed.
- **explanation:** Enforces per-directory isolation so that a tool call in project A cannot read memories or wiki pages stamped for project B. The eligible set predicate (`directory_context IN (caller_dir, 'global', '', NULL)`) is the I31 invariant. The former enforcement-off escape hatch — which removed the rejection guard for legacy or test contexts — is GONE: relaxed enforcement was the mode in which unscoped rows entered the corpus.

### CAP-STOR-026 — Memory CRUD and field management
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `memory_get`, `memory_update`, `forget`, `validate_memory`
- **migrations:** —
- **bc:** `BC-D1`, `BC-D2`, `BC-D3`
- **refs:** `yadgar/_shared/storage/memory.py::_MemoryMixin`, `yadgar/core/server/tools/admin_other.py::memory_get`, `yadgar/core/server/tools/admin_other.py::memory_update`, `yadgar/core/server/tools/admin_other.py::forget`, `yadgar/core/server/tools/admin_other.py::validate_memory`
- **wiring:** `memory_get(id)` calls `_st._storage.get_memory(id)` and strips embedding bytes. `memory_update(id, fields)` validates against `_MEMORY_UPDATE_ALLOWED` (content, tags, is_protected, is_stale, importance, tier, project_id), calls `update_memory_fields`, then re-fetches. `forget(id)` calls `storage.delete_memory(id)` which cascades to `memory_archive`, `memory_transition`, `memory_similarity_link`, and the entity/relationship rows. `validate_memory(id)` delegates to `_st._staleness.validate_memory()` (file-hash comparison) or falls back to direct file-hash check. `BC-D1/D2/D3`: these operations are part of the nightly cycle path.
- **explanation:** The four MCP-exposed memory admin tools provide controlled read/write/delete/validate access to individual memory rows. `memory_update` intentionally restricts the updatable field set at the MCP layer (7 fields) compared to the storage-layer `_MEMORY_UPDATABLE_FIELDS` allowlist (22+ fields) to prevent unintended mutation of heat, embeddings, or temporal metadata via the public API. Core is the ONLY gate: the backend `memory_update` op forwards `**fields` without re-validating, and `de_anchor` calls `_forward_admin("memory_update", …)` directly, bypassing `_MEMORY_UPDATE_ALLOWED` entirely.

### CAP-STOR-027 — remember tool (deprecated stub)
- **status:** DEAD (v6 T3 — stub deleted)
- **category:** storage
- **settings:** —
- **tools:** — (remember deleted)
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** `remember` `@_tool()` registration and function deleted from `yadgar/core/server/tools/memorize.py` in v6 T3. Also removed from `__init__.py` imports and `__all__`. Clients using `remember` will now receive a tool-not-found error from FastMCP.
- **explanation:** `remember` was the original name of the `memorize` tool, renamed in v5.x. The no-op redirect stub was removed in v6 T3 dead-code cleanup (#41). Callers must update to `memorize()`.

### CAP-STOR-028 — Anchor tier system (anchor tool)
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_CONDITIONAL_TTL_DAYS`, `ANCHOR_EPHEMERAL_TTL_DAYS`, `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON`
- **tools:** `anchor`
- **migrations:** `008`
- **bc:** `BC-AN1`, `BC-AN2`
- **refs:** `yadgar/core/server/tools/misc.py::anchor`, `yadgar/core/server/tools/memorize.py::_compute_valid_until`
- **wiring:** `anchor()` MCP tool → `_validate_anchor_inputs()` (validates tier + reason requirement) → `normalize_write_context()` (collapses worktree paths to the canonical repo root) → enqueues to file queue (async default) or synchronously calls `replay.anchor_memory()` (drain replay path). The file queue drainer calls the sync path. `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true` (default): semantic_immortal tier requires a non-empty `reason`. `ANCHOR_CONDITIONAL_TTL_DAYS=90`: default expiry for conditional anchors. `ANCHOR_EPHEMERAL_TTL_DAYS=14`: default expiry for ephemeral anchors.
- **explanation:** The anchor system creates compaction-resistant memories (is_protected=True, max heat, max importance, tagged `_anchor`). Three tiers control expiry: `semantic_immortal` (no expiry, requires reason), `conditional` (expires in ANCHOR_CONDITIONAL_TTL_DAYS), and `ephemeral` (expires in ANCHOR_EPHEMERAL_TTL_DAYS). Valid_until can also be set explicitly via `valid_until` or `ttl_days` parameters.

### CAP-STOR-029 — Anchor audit (audit_anchors tool)
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_REDUNDANCY_COSINE`, `ANCHOR_CROSS_PROJECT_COSINE`, `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN`, `ANCHOR_AUDIT_THRESHOLD`, `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`, `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS`
- **tools:** `audit_anchors`
- **migrations:** —
- **bc:** `BC-AN3`
- **refs:** `yadgar/core/server/tools/audit.py::audit_anchors`, `yadgar/core/server/tools/audit.py::_run_anchor_audit_pass`
- **wiring:** `audit_anchors()` MCP tool: resolves project root, fetches cfg, calls `_build_expire_actions`, `_build_verify_grace_actions`, `_build_promote_actions`, `_build_merge_actions` per directory, caps at `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN`. When `dry_run=False` applies mutations via `_apply_mutations`. Also computes cross-project redundancy via `_fetch_cross_project_candidates` (uses `ANCHOR_CROSS_PROJECT_COSINE=0.95`). `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`: auto-run dry-pass during `consolidate_now(mode='full')` for dirs with anchor count ≥ `ANCHOR_AUDIT_THRESHOLD` (default 15). `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS=30` is defined but has no active consumer beyond the config definition (CONFIG-ONLY for that sub-knob).
- **explanation:** Scans all anchors for a directory for four conditions: expiry (valid_until < now), migration-grace expiry (migration_grace=True + expired), size-based promotion candidates (prose-only archives), and cosine-based redundancy (pairwise cosine ≥ ANCHOR_REDUNDANCY_COSINE). The tool never auto-applies semantic_immortal or is_protected=True rows. Cross-project candidates are always surfaced but never mutated. The nightly auto-pass (via `_run_anchor_audit_pass`) writes a sentinel memory tagged `_audit_anchors` after each pass.

### CAP-STOR-048 — Anchor renew (anchor_renew tool — the time-box renewal surface)
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_CONDITIONAL_TTL_DAYS`, `ANCHOR_EPHEMERAL_TTL_DAYS`
- **tools:** `anchor_renew`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::anchor_renew`, `yadgar/backend/admin_exec/memory.py::anchor_renew`, `yadgar/_shared/storage/memory.py::clear_memory_valid_until`
- **wiring:** `anchor_renew(memory_id, ttl_days=None, tier=None, reason="")` MCP tool: fetches the memory core-side, rejects it unless the `_anchor` TAG is present (NOT `is_protected` — the corpus holds many `is_protected` rows without the tag, e.g. `_active_work`, and both surfacing queries require the tag), then resolves a new expiry via `_compute_valid_until(effective_tier, None, ttl_days, settings)` and forwards the `anchor_renew` admin op. The effective tier is explicit `tier` → the ROW's existing tier → `conditional`; `semantic_immortal` is reachable ONLY by naming it, never by omission (a bare `_compute_valid_until(None, None, None, …)` returns `None`, so a naive pass-through would silently mint immortal anchors). `migration_grace` is ALWAYS set to `False`. `reason` is REQUIRED, secret-gated, and recorded as an `anchor:<reason>` tag mirroring `_apply_tag_injection`. The backend half applies `update_memory_fields` and, for the immortal case, `clear_memory_valid_until` — a bare `SET valid_until = NONE`, because `valid_until` is `option<string>` and a Python `None` serialises to JSON null which SurrealDB rejects (`Expected 'none | string' but found 'NULL'`). Deliberately does NOT widen `_MEMORY_UPDATE_ALLOWED`, which still rejects `valid_until` and `migration_grace` for every `memory_update` caller. Returns the RESOLVED `valid_until` so the caller can see the new expiry. Missing memory / non-anchor / missing reason / invalid tier / `ttl_days` conflicting with `semantic_immortal` all return `{ok: False, error}`.
- **explanation:** The RENEW path, completing the anchor lifecycle alongside `anchor()` (create), `de_anchor` (retire) and `forget` (delete). Every anchor surfacing query filters `valid_until IS NONE OR valid_until > now`, so at its expiry instant an anchor silently stops surfacing while nothing deletes it — and for `migration_grace` rows no signal fires either (`project.py` excludes them by design, ADR-0083), making them invisible undeleted zombies. Before this tool there was no sanctioned way back: `memory_update`'s allowlist rejects the expiry fields and `db_inspect` is read-only, so a reviewed-and-kept anchor could not be saved from its cliff. Clearing `migration_grace` is what makes the renewal permanent rather than merely moving the cliff.

### CAP-STOR-031 — Anchor retire (de_anchor tool + stop-hook maintenance scheduler)
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_AUDIT_STOP_INTERVAL`
- **tools:** `de_anchor`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::de_anchor`, `yadgar/core/hooks/stop-memory-checkpoint.py`
- **wiring:** `de_anchor(memory_id)` MCP tool (Car #85): fetches the memory, strips `_anchor` + any `anchor:*` tags, then forwards a `memory_update` with `is_protected=False`, `importance=0.5`, and `tier='ephemeral'`. Clearing `is_protected` re-admits the row to the decay query (`get_all_memories_for_decay_scalar` excludes protected rows); resetting importance to 0.5 re-enables the fast `DECAY_FACTOR` (`compute_decay` uses the slow `IMPORTANCE_DECAY_FACTOR` only when importance>0.7). Requires `_MEMORY_UPDATE_ALLOWED` widened to include `importance` + `tier`. Missing memory returns `{ok: False, error}`. The single Stop hook runs an ordered `MaintenanceItem` registry (checkpoint priority 0, anchor-audit priority 1); it injects exactly one `{decision: block}` (FIRST DUE WINS) and advances only the injected item's per-session counter. `ANCHOR_AUDIT_STOP_INTERVAL` (default 100) is the human-message cadence between anchor-audit injections; a due checkpoint (`INTERVAL=25`) preempts the audit, which then fires on the next eligible stop. The injected `anchor_audit_prompt.md` template gates on an empty candidate list (no-nag), shows+confirms before `de_anchor`, and reserves `forget` for explicit user delete requests.
- **explanation:** The RETIRE path is the gentle counterpart to `forget` — instead of deleting an anchor outright, `de_anchor` undoes its protection + importance boost so it ages out of the surfacing channels naturally over months (a de-anchored row falls below `COLD_THRESHOLD` within ~2 years of no access). This lets the periodic stop-hook maintenance pass shrink an over-large anchor set without data loss: stale anchors decay away while still-useful ones keep their compaction-proof slot.

### CAP-STOR-030 — CRDT vector clock and agent identity
- **status:** LIVE
- **category:** storage
- **settings:** `CRDT_AGENT_ID`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/write_exec/_memorize_phases/_phase_post_write.py`, `yadgar/_shared/storage/memory.py::_build_memory_insert_clause`
- **wiring:** `CRDT_AGENT_ID` (default `'default'`). After a new memory is inserted, `_phase_post_write.py` reads `settings.CRDT_AGENT_ID`, increments the memory's vector clock JSON (`{agent_id: seq}`), and updates `provenance_agent` and `vector_clock` fields on the memory row via `_q`. The vector clock is stored as a JSON string in the `vector_clock` field.
- **explanation:** Provides a lightweight CRDT (Conflict-free Replicated Data Type) stamping mechanism for multi-agent environments. Each agent has a unique ID; the vector clock tracks per-agent write counts to enable causal ordering and conflict detection if memories are replicated across agents. In single-agent deployments the clock always has one entry. The field is initialised to `'{}'` on insert and incremented in the post-write phase.

### CAP-STOR-031 — Decision auto-protect
- **status:** LIVE
- **category:** storage
- **settings:** `DECISION_AUTO_PROTECT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/write_exec/_memorize_phases/_phase_post_write.py`
- **wiring:** `DECISION_AUTO_PROTECT=true` (default). Checked in `_phase_post_write.py` after memory insert: if the content matches `_DECISION_STRONG_RE` (regex for decision-language patterns), the memory is automatically protected (`is_protected=True`). Applied on the `memorize` tool write path.
- **explanation:** Automatically protects memories that contain strong decision language (e.g., "we decided", "the decision is") from heat decay and archival. This is a heuristic layer on top of explicit `is_protected` flag. Disabling via `DECISION_AUTO_PROTECT=false` removes the heuristic protection, requiring all protection to be explicit.

### CAP-STOR-032 — File hash content validation (MAX_HASH_BYTES)
- **status:** LIVE
- **category:** storage
- **settings:** `MAX_HASH_BYTES`
- **tools:** `validate_memory`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/_helpers.py::_file_hash`
- **wiring:** `_file_hash(path)` is called by `validate_memory` and the memorize write path to compute a SHA-256 hash of a file for staleness tracking. `MAX_HASH_BYTES=10485760` (10 MiB): files larger than this are skipped (returns None). Called from `validate_memory` MCP tool and `_memorize_phases/_phase_validate.py`.
- **explanation:** Controls the maximum file size that yadgar will hash for staleness detection. Files exceeding `MAX_HASH_BYTES` are skipped to prevent memory/time overhead on large binaries or log files. `validate_memory` compares the stored `file_hash` against the current file hash to determine if a file-backed memory has gone stale.

### CAP-STOR-033 — Memory similarity links (SIMILARITY_LINK_THRESHOLD, MAX_SIMILARITY_LINKS_PER_MEMORY)
- **status:** LIVE
- **category:** storage
- **settings:** `SIMILARITY_LINK_THRESHOLD`, `MAX_SIMILARITY_LINKS_PER_MEMORY`
- **tools:** —
- **migrations:** `003`, `007`
- **bc:** —
- **refs:** `yadgar/backend/consolidation/cls.py`, `yadgar/_shared/storage/cluster.py::_ClusterMixin.insert_memory_similarity_link`
- **wiring:** `SIMILARITY_LINK_THRESHOLD=0.78` (default minimum cosine to create a link). `MAX_SIMILARITY_LINKS_PER_MEMORY=15` (default degree cap). Both consumed by `consolidation/cls.py` during the CLS (episodic→semantic) phase: for each memory pair with cosine ≥ threshold, if the degree cap is not exceeded, a `memory_similarity_link` row is created via `insert_memory_similarity_link`. Also checked by `admin_invariants.py` for the invariant ceiling.
- **explanation:** Bounds the `memory_similarity_link` graph to prevent unbounded growth. `SIMILARITY_LINK_THRESHOLD` sets the cosine floor for edge creation; `MAX_SIMILARITY_LINKS_PER_MEMORY` caps the per-node degree so the graph remains sparse and fast to traverse. The CLS phase runs during the nightly consolidation cycle and during `consolidate_now(mode='full')`.

### CAP-STOR-035 — Incremental similarity-linking + full reconcile (OT-C4)
- **status:** DORMANT
- **category:** storage
- **settings:** `SIMILARITY_LINKING_INCREMENTAL_ENABLED`, `SIMILARITY_LINKING_RECONCILE_INTERVAL_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/consolidation/cls.py::_link_similar_memories_incremental`, `yadgar/backend/consolidation/cls.py::_collect_link_candidates_rect`, `yadgar/backend/consolidation/orchestrator.py::_run_graph_phases`, `yadgar/backend/consolidation/__init__.py::run_nightly_consolidation`, `yadgar/_shared/storage/ops.py::get_consolidation_watermark`
- **wiring:** v5.86 car #1 (OT-C4). DEFAULT OFF — `SIMILARITY_LINKING_INCREMENTAL_ENABLED=False` so production runs the full N×N `_link_similar_memories` every cycle exactly as before. When True, `_run_graph_phases` calls `_link_similar_memories_incremental(stats, since=<watermark>)` (probe = memories created since the persisted `similarity_linking` watermark, corpus = full candidate set), then bumps the watermark to the cycle-start timestamp. `run_nightly_consolidation` runs a MANDATORY full reconcile (`_link_similar_memories`) after `_maybe_sleep_cycle` whenever that sleep cycle re-embedded/compressed memories (old↔old similarity changed) OR `SIMILARITY_LINKING_RECONCILE_INTERVAL_DAYS` (default 7) elapsed since the last `full_reconcile` watermark. Watermarks persist in the `consolidation_meta` singleton table.
- **explanation:** Re-embedding mutates existing embeddings, so old↔old cosine similarity changes invisibly to an incremental-by-`created_at` pass. The full reconcile is the safety net: it re-runs the complete pass whenever embeddings actually changed or weekly, guaranteeing eventual consistency of the link graph while the per-cycle incremental path keeps consolidation O(N_new × N) instead of O(N²). With the flag OFF the incremental path and the post-sleep reconcile call are both inert — behavior is byte-identical to the prior full-only pass.

### CAP-STOR-034 — Memory cluster and prospective memory retention
- **status:** LIVE
- **category:** storage
- **settings:** `MEMORY_CLUSTER_RETENTION_DAYS`, `PROSPECTIVE_MEMORY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/consolidation/cleanup.py`, `yadgar/_shared/storage/cluster.py::_ClusterMixin`
- **wiring:** Both settings consumed by `consolidation/cleanup.py` in the nightly cleanup phase: `memory_cluster` rows older than `MEMORY_CLUSTER_RETENTION_DAYS` (default 30) are pruned; `prospective_memory` rows older than `PROSPECTIVE_MEMORY_RETENTION_DAYS` (default 30) are pruned. Pruning runs during `consolidate_now()` and the nightly daemon cycle.
- **explanation:** Controls the retention window for two ephemeral table types. Memory clusters are recreated each consolidation cycle; stale clusters from prior runs are pruned after the retention window. Prospective memories (future reminders) are automatically pruned after their retention window even if they have not been activated. Both defaults are 30 days.

### CAP-STOR-035 — Secret gate at storage layer (BC-S1, BC-S2, BC-S3)
- **status:** LIVE
- **category:** security
- **settings:** —
- **tools:** `memorize`, `anchor`
- **migrations:** —
- **bc:** `BC-S1`, `BC-S2`, `BC-S3`
- **refs:** `yadgar/_shared/storage/memory.py::_validate_memory_secrets`, `yadgar/_shared/storage/memory.py::_MemoryMixin.insert_memory`
- **wiring:** `_validate_memory_secrets()` is called by `insert_memory()` before every memory write. It calls `check_secrets()` on content, tags, and reason fields. If a secret pattern is detected it raises `SecretLeakBlocked` and increments the `rejected_secret_at_storage` metric. The env var `YADGAR_SECRET_GATE_DISABLED=1` bypasses the check with a warning (emergency kill switch only). The API-boundary gate (`gate_or_reject()`) is Layer 2; the storage gate is Layer 1 (last line of defence).
- **explanation:** Two-layer secret gate prevents credential and API-key leakage into the memory store. The storage-layer gate (this entry) fires even if the API boundary was bypassed. BC-S1: blocked at both layers. BC-S2: allowlist bypass (implemented at the API layer via `gate_or_reject()`). BC-S3: every allowlist bypass is audited via invariant I28.

### CAP-STOR-036 — Checkpoint and restore (BC-CK1)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CK1`
- **refs:** `yadgar/_shared/storage/ops.py`, `yadgar/core/server/tools/misc.py`
- **wiring:** `checkpoint()` MCP tool stores task context (decisions, next-steps, open questions) as a structured `checkpoint` table row scoped by `directory_context`. `restore()` reads the latest checkpoint for the directory and returns it as part of the context bundle (alongside anchors and hot memories). Both paths go through `_OpsMixin` methods on `StorageEngine`.
- **explanation:** Provides session continuity across `/clear` or `/compact` operations. A checkpoint captures the current task state as a serialised record; `restore()` reconstructs it and injects it into the next session's context. BC-CK1 requires that `checkpoint(dir, ...)` followed by `restore(dir)` returns the task, decisions, and next-steps.

### CAP-STOR-037 — Nightly cycle storage operations (BC-D1, BC-D2, BC-D3)
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-D1`, `BC-D2`, `BC-D3`
- **refs:** `yadgar/_shared/storage/ops.py`, `yadgar/_shared/storage/memory.py::get_all_memories_for_decay`
- **wiring:** The nightly cron script calls the daemon's consolidation cycle which reads memories via `get_all_memories_for_decay()`, applies heat decay, archives cold memories, runs CLS and causal phases, and writes results back. `BC-D1`: nightly exits 0 against a seeded DB (e2e proven). `BC-D2`: pre-backup snapshot uses real `YADGAR_DATA_DIR`/XDG path. `BC-D3`: interpreter shutdown is clean (no SEGV).
- **explanation:** The nightly cycle is the primary path through which storage-layer read/write/decay operations exercise the full stack. Most storage primitives (heat update, archival, cluster insert, similarity link, consolidation log) run exclusively in this path, not on the real-time request path. BC-D1 is e2e green; BC-D2 and BC-D3 are also e2e green.

### CAP-STOR-038 — Migration 023: memory directory_context pre-flip backfill
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `023`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_023_memory_directory_context_backfill`
- **wiring:** Applied once at server-mode startup after v5.80. Defensive pre-flip gate immediately before `UNIFIED_RECALL_ENABLED` defaults to `True`. Phases: (A) relax `memory.directory_context` to `option<string>` via `DEFINE FIELD OVERWRITE`; (B) Python-side fetch-all + filter for absent/empty/NULL rows, UPDATE each to `'global'`; (C) re-tighten to `string ASSERT NOT NULL, len > 0`. On any DB that ran migration 018, this migration touches 0 rows (field-absent memory inserts were already blocked by the 018 ASSERT).
- **explanation:** Guarantees all memory rows have a non-empty `directory_context` before the unified fan-out recall path (v6 T6) becomes the default. Migration 018 Phase D/E/F already covered this for deployed databases; migration 023 is a belt-and-suspenders idempotent repair for any edge-case DB that ran 016 without 018, or was written before the ASSERT was applied. Uses the same `DEFINE FIELD OVERWRITE` relax/backfill/re-tighten pattern as migration 018 (necessary because SurrealDB v3 validates ASSERT on every UPDATE — field-absent rows trigger a coerce error without the relax step).

### CAP-STOR-039 — Migration 024: hash + source_file fields on wiki_page
- **status:** DEAD
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `024`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_024_wiki_page_hash_source`
- **wiring:** Applied once at startup (immutable, append-only — never reversed). Added `hash` and `source_file` (`option<string>`) fields to `wiki_page`. All producers/consumers (repo_wiki generator's stamping, `insert_wiki_page`'s passthrough, `_scan_stale_wiki_slugs_db`) were removed with repo_wiki's decommission (#33/ADR-0162); the columns remain on the schema as inert nullable fields — no code reads or writes them anymore.
- **explanation:** Originally bridged the store divergence: the wiki staleness checker previously only scanned `.local-review/wiki/*.md` on disk, while module pages written by `repo_wiki_generate` lived in the DB with no hash/source_file fields (v5.85.0 car #36). The consumer (`_scan_stale_wiki_slugs_db`, querying `page_type='code'`) never actually matched any real page — the generator stamped `page_type='repo_wiki'`, not `'code'` — so this bridge was dead-on-arrival even before the decommission. Migration 024 itself is kept (append-only migration history); its consumer code was removed as dead repo_wiki-specific plumbing.

### CAP-STOR-040 — Migration 025: agent-prompt -vN slug collapse (v5.85 S7)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `025`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_025_agent_prompt_slug_collapse`
- **wiring:** Applied once at startup. Queries all wiki_page rows tagged `agent-prompt` with slugs matching `-v\d+$`. Groups by pattern, keeps highest-version content, creates a bare `agent-prompt-<pattern>` page, then deletes the versioned pages. Idempotent: no-op when no versioned slugs exist.
- **explanation:** v5.85 S7 rework changes the storage convention from one-page-per-version (slug `agent-prompt-<pattern>-vN`) to one-page-per-pattern (slug `agent-prompt-<pattern>`), with wiki versioning carrying history. This migration collapses existing versioned pages for any installations that ran the Phase 1 code (v5.3.0 A4). New installs see no -vN pages and the migration is a no-op.

### CAP-WRITE-001 — `memorize` MCP tool (write-path entry point)
- **status:** LIVE
- **category:** write-path
- **settings:** —
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/memorize.py::memorize`, `yadgar/_shared/write_exec/context.py`
- **wiring:** MCP client calls `memorize()` → registered via `@_tool()` decorator in `memorize.py` → constructs `MemorizeContext` → sequentially calls `phase_validate`, `phase_embed`, `phase_contradiction`, `phase_store`, `phase_post_write`. Each phase returns a rejection dict (short-circuit) or `None` (continue). Final response is the stored memory dict.
- **explanation:** `memorize` is the primary write-path MCP tool. It accepts `content`, `context` (must be an absolute directory path), `tags`, and optional protection/tier/TTL fields. It orchestrates five phases: input validation + secret-gate, write-gate + embedding + thermo scoring, contradiction detection, storage (via curator or direct insert), and post-write hooks (synaptic boost, reinjection, shadow-gate stamp, CRDT clock, viz event). The `remember` no-op redirect stub was deleted in v6 T3 (see CAP-STOR-027).

---

### CAP-WRITE-002 — `memory_stats` MCP tool
- **status:** LIVE
- **category:** observability
- **settings:** —
- **tools:** `memory_stats`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::memory_stats`
- **wiring:** MCP client calls `memory_stats()` → registered in `admin_other.py`, re-exported through `admin.py` → calls `storage.get_memory_stats()` then decorates with write-gate rejection count, engram slot utilization, active rule count, CLS episodic/semantic counts, cognitive map state, causal edge count, metacognition load limit, DB size, and a Prometheus metrics summary block.
- **explanation:** Returns a comprehensive system statistics dict covering row counts, DB size per-table, Prometheus queue/lag/recall metrics, and states of optional subsystems (write gate, engram, rules engine, CLS, cognitive map, causal graph, metacognition). The metrics block is always present (stubbed with zeros when prometheus_client is absent) to satisfy the I8 backpressure-observability invariant.

---

### CAP-WRITE-003 — Write-path input validation & secret gate (phase_validate)
- **status:** LIVE
- **category:** gate
- **settings:** —
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/write_exec/validate.py::phase_validate`, `yadgar/_shared/security/secrets.py::gate_or_reject`, `yadgar/_shared/rules_engine/rules_engine.py`
- **wiring:** Called as first phase from `memorize()` → validates tier enum, valid_until/ttl_days conflict, unicode surrogates, content size ≤ 32 768 bytes, provenance_agent format → then calls `gate_or_reject(content, tags=...)` from `yadgar.secrets` → if non-None, returns rejection dict. Then runs `_rules_engine.check_write_policy()` if the rules engine is loaded.
- **explanation:** Phase 1 of the memorize pipeline enforces hard pre-write guards: tier validation, anchor tag injection, content-size limit, provenance agent name validation, the built-in secret-pattern scanner (`gate_or_reject`), the user-defined write-path policy rules, and a Unicode surrogate check. Secret-gate fires before any state mutation and cannot be disabled — it is the Layer 2 (API-boundary) defence that precedes the Layer 1 (storage-level) `SecretLeakBlocked` exception.

---

### CAP-WRITE-004 — Secret-gate pattern scanner
- **status:** LIVE
- **category:** security
- **settings:** —
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/security/secrets.py::gate_or_reject`, `yadgar/_shared/security/secrets.py::check_secrets`, `yadgar/_shared/security/allowlist.py`
- **wiring:** `phase_validate` → `gate_or_reject(content, tags=tags)` → `check_secrets(content)` scans all `_SECRET_PATTERNS` regexes → if a match is found and no allowlist bypass applies, returns rejection dict `{"stored": False, "reason": "secret_detected: <name>"}`. Allowlist (v5.13.0) is loaded from `YADGAR_SECRET_GATE_ALLOWLIST_PATH`; every bypass is audited to a date-rotated JSONL file.
- **explanation:** Built-in, non-disableable regex scanner covering AWS access keys, private keys, JWTs, GitHub/GitLab/Stripe/Slack/Anthropic/OpenAI tokens, database connection strings, GCP service-account JSON, and a generic credential-pattern catch-all. Fires before any write at the API boundary (`gate_or_reject`) and again at the storage boundary (`SecretLeakBlocked` exception). The v5.13.0 allowlist allows per-tag bypass of specific glob patterns, with every bypass recorded to an append-only audit log.

---

### CAP-WRITE-005 — Contextual prefix generation
- **status:** LIVE
- **category:** write-path
- **settings:** `CONTEXTUAL_PREFIX_ENABLED`
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/write_exec/_memorize_phases/_phase_embed.py::phase_embed`, `yadgar/backend/retrieval/core.py::generate_contextual_prefix`
- **wiring:** `phase_embed` checks `settings.CONTEXTUAL_PREFIX_ENABLED` (default `True`) and `_st._retriever is not None` → calls `retriever.generate_contextual_prefix(content, context, tags, now)` → prefix is prepended to content before embedding: `embed_text = f"{prefix}{content}"`. The prefix is also stored on the memory row via `update_memory_fields`.
- **explanation:** Before generating the embedding for a new memory, the write path prepends a structured metadata prefix: `[Project: <basename>] [Directory: <path>] [Tags: <comma-list>] [Recorded: <timestamp>] [Related entities: <top-5>]`. This enriches the embedding space with contextual signals, improving retrieval specificity for project-scoped queries. Enabled by default; set `CONTEXTUAL_PREFIX_ENABLED=False` to skip. Disabled only if the retriever is unavailable.

---

### CAP-WRITE-006 — Sensory-buffer overlap / episode capture
- **status:** LIVE
- **category:** write-path
- **settings:** `OVERLAP_TOKENS`
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/sensory_buffer/sensory_buffer.py::ActionLogger`, `yadgar/backend/write_exec/_memorize_phases/_phase_store.py::phase_store`
- **wiring:** `phase_store` calls `buffer.capture(ctx.content, ctx.context)` after the memory is written → `ActionLogger.capture` appends content to the current episode's `raw_content`. When `raw_content` exceeds `MAX_EPISODE_TOKENS * 4` chars, the episode is rotated with a trailing overlap of `OVERLAP_TOKENS * 4` chars to preserve continuity across episode boundaries.
- **explanation:** The sensory buffer accumulates raw content from each `memorize` call into rolling episodes. `OVERLAP_TOKENS` (default 2 000, converted to characters by `× 4`) controls how many characters are carried over from the end of one episode into the start of the next, preventing context loss at episode boundaries. The buffer also logs every tool invocation to an action stream (maxlen 200) for pattern extraction.

---

### CAP-WRITE-007 — Write-gate reinjection on write (`REINJECT_ON_WRITE`)
- **status:** DORMANT
- **category:** write-path
- **settings:** `REINJECT_ON_WRITE`, `REINJECTION_ENABLED`, `REINJECTION_MAX_RESULTS`
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/write_exec/_memorize_phases/_phase_post_write.py::_zero_gap_6_reinjection`
- **wiring:** `phase_post_write` → `_zero_gap_6_reinjection(ctx, settings)` → if `settings.REINJECT_ON_WRITE` is `False` (default), function returns immediately after logging once. If both `REINJECT_ON_WRITE=True` and `REINJECTION_ENABLED=True` and the retriever is available, it calls `retriever.recall(content[:300], max_results=REINJECTION_MAX_RESULTS+1)` and populates `ctx.related_context`, which appears in the response under `"related_context"`.
- **explanation:** After a memory is stored, this hook surfaces the most semantically similar existing memories and attaches them to the write response as `related_context`. The intent is to immediately inform the caller what Yadgar already knows about the written topic, bridging the write/recall gap. Disabled by default (`REINJECT_ON_WRITE=False`); requires also `REINJECTION_ENABLED=True`. Enable by setting `YADGAR_REINJECT_ON_WRITE=1`.

---

### CAP-WRITE-008 — Profile extraction on recall
- **status:** LIVE
- **category:** retrieval
- **settings:** `PROFILE_EXTRACTION_ENABLED`, `PROFILE_CONFIDENCE_DIRECT`, `PROFILE_CONFIDENCE_INFERRED`, `PROFILE_SUMMARY_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/fusion.py::_search_profiles_and_beliefs`, `yadgar/_shared/config/config.py`
- **wiring:** During recall/fusion, `_search_profiles_and_beliefs` checks `getattr(settings, "PROFILE_EXTRACTION_ENABLED", False)` (default `True`) → calls `storage.search_profiles_fts(query, limit=max_results)` → injects results as synthetic recall hits with `_source="profile"` and `_retrieval_score=PROFILE_SEARCH_WEIGHT`. `PROFILE_CONFIDENCE_DIRECT` (0.7) and `PROFILE_CONFIDENCE_INFERRED` (0.4) are threshold constants used when extracting/scoring profile attributes; `PROFILE_SUMMARY_ENABLED` controls summary generation. Enabled on default config.
- **explanation:** Structured user/entity profiles stored in a `profile` table are searchable via full-text search during recall. When a query matches profile entries (entity name, attribute type, attribute value), they are injected into the result set as synthetic memories with a configurable score weight. This allows recall to surface factual profile data (e.g. "user speaks German", "project uses Python") that would not appear in the embedding vector index. `PROFILE_CONFIDENCE_DIRECT` and `PROFILE_CONFIDENCE_INFERRED` tune confidence scoring for directly-stated vs. inferred attributes.

---

### CAP-WRITE-009 — Predictive coding write gate (`should_store`)
- **status:** SHADOW
- **category:** gate
- **settings:** `WRITE_GATE_THRESHOLD`, `WRITE_GATE_SHADOW_THRESHOLD`, `WRITE_GATE_CONTINUITY_DISCOUNT`, `WRITE_GATE_CONTINUITY_WINDOW`
- **tools:** `memorize`
- **migrations:** —
- **bc:** `BC-PCd2`
- **refs:** `yadgar/backend/predictive_coding/predictive_coding.py::WriteGate.should_store`, `yadgar/backend/predictive_coding/predictive_coding.py::WriteGate.would_reject_at`, `yadgar/backend/write_exec/_memorize_phases/_phase_embed.py::phase_embed`, `yadgar/backend/write_exec/_memorize_phases/_phase_store.py::phase_store`, `yadgar/_shared/write_exec/context.py`
- **wiring:** `phase_embed` calls `_st._write_gate.should_store(content, context, tags)` → when `WRITE_GATE_THRESHOLD <= 0.0` (default `0.0`), `should_store` immediately returns `(True, 0.0, "gate_disabled")` — nothing is ever dropped. The gate then calls `would_reject_at(content, context, tags, settings.WRITE_GATE_SHADOW_THRESHOLD, surprisal=surprisal)` (default shadow threshold `0.15`) → sets `ctx.gate_surprisal` and `ctx.would_reject`. In `phase_store`, these shadow fields are written to the memory row via `storage.update_memory_fields(ctx.memory_id, surprise_score=ctx.gate_surprisal, would_reject=ctx.would_reject)`. The gate NEVER drops a memory at the current default config.
- **explanation:** The predictive coding write gate models Friston-style free-energy minimization: it computes surprisal for each candidate memory as a weighted sum of four novelty signals (embedding novelty 0.4, entity novelty 0.25, temporal novelty 0.2, structural novelty 0.15). With `WRITE_GATE_THRESHOLD=0.0` (default), the gate is in **shadow mode**: it computes surprisal and the adaptive `would_reject` shadow decision at `WRITE_GATE_SHADOW_THRESHOLD=0.15`, stamps both on the memory row, but never actually drops a write. To activate the gate as a real filter, set `WRITE_GATE_THRESHOLD` to a positive value (e.g. 0.15). Bypass conditions (error/exception/decision keywords, `important`/`critical` tags) always pass regardless of threshold. Task continuity (directory match + temporal proximity + semantic similarity to recent stores) reduces the effective threshold by up to `WRITE_GATE_CONTINUITY_DISCOUNT` (default 0.15).

---

### CAP-WRITE-010 — Predictive coding entity cache (write-gate TTL cache)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `PREDICTIVE_CODING_ENTITY_TTL_SECONDS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/predictive_coding/predictive_coding.py::WriteGate._get_cached_entities`, `yadgar/backend/predictive_coding/predictive_coding.py::WriteGate.invalidate_entity_cache`
- **wiring:** `WriteGate._compute_entity_novelty` and `_compute_structural_novelty` call `_get_cached_entities()`, which uses a monotonic-clock TTL check against `PREDICTIVE_CODING_ENTITY_TTL_SECONDS` (default 300 s = 5 min). On expiry or cache miss, it fetches all entities from storage; `invalidate_entity_cache()` is called after entity inserts/deletes to force refresh.
- **explanation:** The write gate's surprisal computation queries the knowledge-graph entity set on every call. To avoid O(N·M) DB fetches during batch write sessions, the entity list is cached in memory with a configurable TTL. When TTL is 0, caching is disabled (always fetches). The cache is also proactively invalidated after any entity mutation via `invalidate_entity_cache()`. This setting only matters when the write gate is active (i.e. `WRITE_GATE_THRESHOLD > 0`), but the cache operates regardless since the entity set is fetched even in shadow mode.

---

### CAP-ENR-001 — Index-time enrichment pipeline (orchestrator + guard)
- **status:** LIVE
- **category:** enrichment
- **settings:** `INDEX_ENRICHMENT_ENABLED`, `ENRICHMENT_MIN_CONTENT_LENGTH`
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/enrichment/__init__.py::EnrichmentPipeline.enrich`, `yadgar/_shared/storage/memory.py::_enrich_memory_if_enabled`
- **wiring:** `storage.insert_memory()` → `_enrich_memory_if_enabled(mid, memory, settings, embeddings_engine, embedding)` → guards: `INDEX_ENRICHMENT_ENABLED=True` (default), `len(content) >= ENRICHMENT_MIN_CONTENT_LENGTH` (default 20 chars), embeddings engine and embedding not None → fetches/creates `EnrichmentPipeline` singleton → calls `pipeline.enrich(content, embedding, settings)` → writes results to `enrichment_concepts`, `enrichment_comet`, `enrichment_queries`, `enrichment_logic`, `enriched_content`, `enrichment_model_versions` columns → re-embeds with enriched content if non-empty.
- **explanation:** After a memory is inserted, the enrichment pipeline runs four sub-enrichers (ConceptNet, COMET, doc2query, logic) in sequence, gated individually by their respective settings. Results that pass the FPA cosine filter are concatenated as `\n[enrichment] term1 | term2 | ...` and appended to the content. The enriched content string is then re-embedded, replacing the original embedding on the memory row to bake enrichment signals into the retrieval index. The minimum content length guard (`ENRICHMENT_MIN_CONTENT_LENGTH`) prevents trivially short strings from entering the enrichment pipeline.

---

### CAP-ENR-002 — FPA (False Positive Attenuation) cosine filter
- **status:** LIVE
- **category:** enrichment
- **settings:** `FPA_SIMILARITY_THRESHOLD`
- **tools:** —
- **migrations:** —
- **bc:** `BC-EN2a`
- **refs:** `yadgar/_shared/enrichment/fpa.py::FPAFilter.filter`, `yadgar/_shared/enrichment/__init__.py::EnrichmentPipeline._apply_fpa`
- **wiring:** After each sub-enricher (ConceptNet, COMET, doc2query) produces candidate terms, `_apply_fpa(embedding, texts, threshold)` is called → creates/reuses `FPAFilter(embedding_engine)` → for each enrichment text, calls `embedding_engine.encode_query(text)` → computes `np.dot(original_vec, text_vec)` → keeps only terms with cosine similarity ≥ `FPA_SIMILARITY_THRESHOLD` (default 0.25). Logic enrichment bypasses FPA entirely (structural terms, no external model).
- **explanation:** FPA is a noise-reduction filter applied to every enrichment sub-pipeline output before results are stored. It encodes each candidate enrichment term and rejects those whose cosine similarity to the original memory embedding falls below the threshold. This prevents semantically distant inferences from polluting the enriched content and degrading retrieval precision. The FPA filter is the root cause of `BC-EN2a`'s `❌` status: COMET generates valid commonsense triples, but their abstract language (e.g. "PersonX wants to help") typically scores below the 0.25 cosine threshold against the concrete memory content, producing an empty `enrichment_comet` field.

---

### CAP-ENR-003 — ConceptNet concept expansion
- **status:** DORMANT
- **category:** enrichment
- **settings:** `CONCEPTNET_ENRICHMENT_ENABLED`, `CONCEPTNET_MIN_EDGE_WEIGHT`, `CONCEPTNET_MAX_TERMS`, `CONCEPTNET_RELATIONS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-EN1a`, `BC-EN1b`
- **refs:** `yadgar/_shared/enrichment/conceptnet.py::ConceptNetExpander.expand`, `yadgar/_shared/enrichment/__init__.py::EnrichmentPipeline.enrich`
- **wiring:** `EnrichmentPipeline.enrich` checks `settings.CONCEPTNET_ENRICHMENT_ENABLED` (default `True`) → calls `ConceptNetExpander().expand(content, settings)` → extracts content-bearing tokens, tries three sources in order: (1) `conceptnet_lite` local SQLite DB (~9 GB, not bundled), (2) HTTP API (`https://api.conceptnet.io`, disabled by default as `http_enabled=False`), (3) hardcoded expansions dict. Results pass through `_apply_fpa` before storage.
- **explanation:** ConceptNet expansion extracts nouns/verbs from the memory content, queries ConceptNet for related concepts (e.g. IsA, RelatedTo, UsedFor, PartOf, HasA — configurable via `CONCEPTNET_RELATIONS`), and appends survivors of the FPA filter to the enriched content. The local `conceptnet_lite` SQLite path fires automatically but is gated on the `conceptnet_lite` package and the ~9 GB DB being installed. The HTTP API path (`api.conceptnet.io`) is network-gated and disabled by default in the `ConceptNetExpander` constructor (`http_enabled=False`). The hardcoded expansion dict provides a fallback for ~20 hobby/activity terms. Status is DORMANT: the flag is enabled by default but both primary data sources (lite DB, HTTP) are unavailable in the standard install, so only the hardcoded fallback fires — functional enrichment requires explicit deployment of the lite DB. (`BC-EN1a` is ⏳ network-gated in CI; `BC-EN1b` per #39.)

---

### CAP-ENR-004 — COMET commonsense inference
- **status:** DORMANT
- **category:** enrichment
- **settings:** `COMET_ENRICHMENT_ENABLED`, `COMET_MODEL`, `COMET_NUM_BEAMS`, `COMET_TOP_K_PER_RELATION`, `COMET_MIN_CONFIDENCE`, `COMET_RELATIONS`, `COMET_QUERY_EXPANSION_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** `BC-EN2a`, `BC-EN2b`
- **refs:** `yadgar/_shared/enrichment/comet.py::CometInferencer.infer`, `yadgar/_shared/enrichment/__init__.py::EnrichmentPipeline.enrich`
- **wiring:** `EnrichmentPipeline.enrich` checks `settings.COMET_ENRICHMENT_ENABLED` (default `False` (RETIRED/DORMANT per ADR-0004)) → `CometInferencer().infer(content, settings)` → `_ensure_model(COMET_MODEL)` lazy-loads `mismayil/comet-bart-ai2` via `_load_seq2seq_model` → extracts subject-verb predicates → for each predicate × relation (default `xAttr,xIntent,xWant`), generates sequences with beam search (`COMET_NUM_BEAMS=5`), scores via softmax, keeps sequences scoring ≥ `COMET_MIN_CONFIDENCE=0.3` up to `COMET_TOP_K_PER_RELATION=3` per relation → results passed through FPA filter. `COMET_QUERY_EXPANSION_ENABLED` (default `False`) controls a separate query-time expansion path not part of the write pipeline.
- **explanation:** COMET-BART infers commonsense consequences for memories using the ATOMIC relations (e.g. xAttr: what the subject is like, xIntent: what the subject intends, xWant: what the subject wants). Despite the model loading and generating valid inferences, the FPA cosine filter (threshold 0.25) consistently rejects these abstract commonsense triples as semantically distant from the concrete memory content — the result is that `enrichment_comet` is empty in practice (`BC-EN2a` status `❌`). The write path is fully wired and functional; COMET was RETIRED to dormant 2026-06-24 (ADR-0004): the en2a ablation decided the v6 FPA-tuning question — un-FPA'd COMET is net-negative for recall (multi-session R@5 -4.2pt) — so the flag now defaults False and the code is retained dormant.

---

### CAP-ENR-005 — Doc2Query synthetic query generation
- **status:** LIVE
- **category:** enrichment
- **settings:** `DOC2QUERY_ENRICHMENT_ENABLED`, `DOC2QUERY_MODEL`, `DOC2QUERY_NUM_QUERIES`
- **tools:** —
- **migrations:** —
- **bc:** `BC-EN3a`, `BC-EN3b`
- **refs:** `yadgar/_shared/enrichment/doc2query.py::Doc2QueryExpander.expand`, `yadgar/_shared/enrichment/__init__.py::EnrichmentPipeline.enrich`
- **wiring:** `EnrichmentPipeline.enrich` checks `settings.DOC2QUERY_ENRICHMENT_ENABLED` (default `True`) → `Doc2QueryExpander().expand(content, settings)` → lazy-loads `doc2query/msmarco-t5-small-v1` → tokenizes content (max 512 tokens), generates `DOC2QUERY_NUM_QUERIES * 2` sequences with beam search, deduplicates and filters out near-copies (token overlap > 0.8), returns top `DOC2QUERY_NUM_QUERIES=5` → passed through FPA filter → stored as `enrichment_queries`. Confirmed LIVE in yadgar-ci:5.72.0.
- **explanation:** Doc2Query uses a T5-based model to generate the natural-language questions a stored memory could answer. These synthetic queries are appended to the enriched content and baked into the memory's embedding, bridging the semantic gap between how a user asks a question and how the answer was phrased when stored. Unlike COMET, doc2query's queries are close in phrasing to the source content and typically survive the FPA cosine filter. `BC-EN3a` is confirmed `✅` (model-skip-guarded so host `make-e2e` skips offline).

---

### CAP-ENR-006 — Logic enrichment (rule-based hypernym + verb nominalization)
- **status:** LIVE
- **category:** enrichment
- **settings:** `LOGIC_ENRICHMENT_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/enrichment/logic.py::LogicExpander.expand`, `yadgar/_shared/enrichment/__init__.py::EnrichmentPipeline.enrich`
- **wiring:** `EnrichmentPipeline.enrich` checks `settings.LOGIC_ENRICHMENT_ENABLED` (default `True`) → `LogicExpander().expand(content)` → scans content against two static maps: `_HYPERNYM_MAP` (named-entity → category, e.g. "python" → "programming_language") and `_VERB_NOMINALIZATIONS` (gerund → noun phrase, e.g. "camping" → "camping trip") → results stored as `enrichment_logic`. Logic expansion **bypasses FPA** entirely — its structural labels are assumed to be reliably on-topic.
- **explanation:** Logic expansion applies two purely rule-based transforms with no external dependencies: (1) hypernym lifting — recognises named entities (national parks, composers, programming languages, cities, animals) and injects their category label; (2) verb nominalization — detects gerund-phrase patterns ("went camping") and injects the corresponding noun phrase ("camping trip"). Because these expansions are structurally derived from the content they are exempt from FPA filtering. This makes logic the most reliable enrichment channel in offline/network-gated deployments.

---

### CAP-ENR-007 — Predictive coding brain-dynamics (BC-PC1 – BC-PC4, BC-PCd1, BC-PCd2)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-PC1`, `BC-PC2`, `BC-PC3`, `BC-PC4`, `BC-PCd1`, `BC-PCd2`
- **refs:** `yadgar/backend/predictive_coding/predictive_coding.py::WriteGate`, `yadgar/backend/write_exec/_memorize_phases/_phase_embed.py`
- **wiring:** The write-gate `should_store` computes surprisal then applies a thermo heat boost via `thermo.apply_surprise_boost(1.0, ctx.surprise)` — surprising memories start hotter (`BC-PCd1`). The gate's `should_store` in shadow mode never drops (`BC-PCd2` — full enforcement requires `WRITE_GATE_THRESHOLD > 0`). `BC-PC1`–`BC-PC4` govern project_brief scoping and seed_project, covered by the project-context subsystem not the write gate.
- **explanation:** This entry groups the behaviour-contract rows assigned to the predictive-coding and project-context subsystems that are exercised via the write path or write-gate logic. `BC-PCd1` (novel memory triggers surprise heat boost) is wired: `initial_heat = thermo.apply_surprise_boost(1.0, ctx.surprise)` in `phase_embed`, so high-surprisal writes receive a heat multiplier. `BC-PCd2` (should_store gates redundant writes) is shadow-only at default config — the gate computes but does not enforce. `BC-PC1`–`BC-PC4` cover project_brief and seed_project and are tracked against those tools' entries.

---

### CAP-ENR-008 — Engram slot allocation (BC-EG1, BC-EG2)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** `memorize`
- **migrations:** —
- **bc:** `BC-EG1`, `BC-EG2`
- **refs:** `yadgar/backend/write_exec/_memorize_phases/_phase_post_write.py::_run_engram`
- **wiring:** `phase_post_write` → `_run_engram(ctx)` → if `_st._engram is not None`, calls `_st._engram.allocate(ctx.memory_id)` → returns `{"slot_index": ..., "temporally_linked": ..., "link_count": ...}` which is included in the response as `engram_slot`, `temporal_links`, `temporal_link_count`.
- **explanation:** After a memory is stored, it is allocated to a competitive engram slot — a biologically-inspired fixed-capacity memory store where each slot has excitability, plasticity, and stability fields (BC-EG1). Slot allocation involves competitive selection among low-excitability slots and temporal linking to recently allocated memories (BC-EG2 — Hopfield-style pattern recall via the engram graph). The engram subsystem is optional (`_st._engram` may be None); failures are silently swallowed with a debug log.

---

### CAP-ENR-009 — Wiki gate / project-context behaviours (BC-G1 – BC-G10)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-G1`, `BC-G2`, `BC-G4`, `BC-G6`, `BC-G7`, `BC-G9`, `BC-G10`
- **refs:** `yadgar/core/server/tools/wiki.py`
- **wiring:** Wiki tools (`wiki_add`, `wiki_query`, `wiki_read`, etc.) implement the behaviours listed in BC-G1–BC-G10 (excluding BC-G5, removed in v5.157.0). These are all in ⏳ (unverified by automated test) status in the behaviour contract and are surfaced here for coverage completeness. The write gate cluster owns this coverage entry because the assignment JSON includes all BC-G rows.
- **explanation:** BC-G1–BC-G10 specify wiki subsystem invariants: directory stamping on `wiki_add`, cross-project isolation on `wiki_query`, §25 slug resolution order (`dir → global → not-found`), immutable version creation on every write, similarity-based near-duplicate blocking, bookmark CRUD, positional edit primitives, and multi-row `wiki_set_metadata` reach. BC-G5 (draft/approve workflow) was removed in v5.157.0 (Fix #76) — no production path ever produced drafts; `wiki_add` always committed directly. All remaining rows are ⏳ (unverified by automated test) or ⏳[u] in the contract as of v5.49.x.

---

### CAP-ENR-010 — Vacuum/backup safety (BC-E1, BC-E2, BC-E3, BC-E5, BC-E6, BC-E7, BC-E8)
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-E1`, `BC-E2`, `BC-E3`, `BC-E5`, `BC-E6`, `BC-E7`, `BC-E8`
- **refs:** `yadgar/core/server/tools/admin_vacuum.py`, `yadgar/core/vacuum/__init__.py`, `yadgar/core/vacuum/phases.py`, `yadgar/core/daemon/systemd.py`, `yadgar/core/daemon/units.py`, `yadgar/core/server/routes/admin_ops.py`
- **wiring:** The vacuum tool (`vacuum_now`, `vacuum_checkpoints`) implements the three safety contracts. `BC-E1` (row counts unchanged) is verified by comparing pre/post snapshot counts. `BC-E2` (atomic swap — mid-failure leaves DB intact) is enforced by a copy-then-swap pattern with verification before the rename. `BC-E3` (sensitive-job lock blocks restart) is enforced by checking the job-lock state before allowing external shutdown. `BC-E5` (a rolled-back run reports zero saving) is enforced in `_vacuum_report_and_log`, which derives the saving itself and hard-zeroes it on the rollback path so no caller can report a positive figure for a reverted run; `after_bytes` is measured after `_vacuum_finalize` returns. `BC-E6` (core running after every abort) is enforced by `_restart_services_after_abort`, wired into `_abort_restart`, the quiescence gate, the snapshot/drop failure path and `_restore_db`, with backend and core each in their own try/except. `BC-E7` (no core URL served nowhere) is enforced by a test that resolves the daemon's real `mcp_server._custom_starlette_routes` table; `POST /api/check_invariants` is served by `yadgar/core/server/routes/admin_ops.py`. `BC-E8` (a vacuum stops the backend only) is enforced in `_vacuum_snapshot_and_drop`, which calls `ServiceController.stop_backend()` rather than `stop()`, plus the `Wants=`-not-`Requires=` core→backend dependency emitted by the unit builder (`yadgar/core/daemon/units.py:build_core_unit`, task:0110 Stage D retired the `.in` template that carried the other copy) so the backend stop does not cascade; `_vacuum_finalize` starts nothing and gates on the core's readiness `/health`, which round-trips the backend.
- **explanation:** BC-E1/E2/E3 were flipped to ✅ in v5.69 with dedicated e2e tests in `tests/e2e/test_vacuum_backup_safety.py`. BC-E1 proves row counts are preserved across vacuum. BC-E2 proves the atomic swap is safe under failure injection (import failure, verification failure, crash mid-swap, recovery ordering). BC-E3 proves an external restart/shutdown request is refused while a sensitive vacuum job holds the lock. BC-E5/E6/E7 were added by task:0045 + task:0027a after seven consecutive vacuums swapped in a compacted DB, rolled it back one minute later because the verification endpoint did not exist, and each reported a ~2 GB saving. BC-E7 is the anti-recurrence contract for that class specifically: six tests mocked the missing URL to 200, so the defect was invisible to every mock-based check. BC-E8 was added by task:0111 (ADR-0188): every prior vacuum car fixed mechanics and left the stop-the-world *shape* untouched, so the run kept taking the whole memory engine down for ~68 s and dropping every connected MCP session — for a store only the backend holds.

### CAP-CONS-001 — Heat Decay (thermodynamic memory decay)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `DECAY_FACTOR`, `COLD_THRESHOLD`, `HOT_THRESHOLD`, `IMPORTANCE_DECAY_FACTOR`, `EMOTIONAL_DECAY_RESISTANCE`
- **tools:** —
- **migrations:** —
- **bc:** `BC-HT1`, `BC-HT2`, `BC-HT3`, `BC-C2`, `BC-CSW1`
- **refs:** `yadgar/backend/consolidation/heat_decay.py::_HeatDecayMixin._apply_decay`, `yadgar/backend/consolidation/heat_decay.py::_HeatDecayMixin._decay_memories`, `yadgar/backend/consolidation/heat_decay.py::_HeatDecayMixin._decay_entities`, `yadgar/_shared/storage/heat_writer.py::HeatWriter`, `yadgar/_shared/thermodynamics/thermodynamics.py`
- **wiring:** `ConsolidationScheduler._consolidation_cycle()` → `_run_episodic_phases()` → `_apply_decay()`. Runs every consolidation cycle (force_consolidate MCP or nightly cron). `_decay_memories` iterates all non-protected memories: computes elapsed hours from `max(last_accessed, last_decay_at)` (watermark fix prevents quadratic over-decay), applies domain multiplier from AstrocytePool if enabled, then calls `MemoryThermodynamics.compute_decay()` which uses IMPORTANCE_DECAY_FACTOR and EMOTIONAL_DECAY_RESISTANCE. Heat below COLD_THRESHOLD is zeroed. HOT_THRESHOLD defaults to 0.0 (all memories accessible). Entity decay uses DECAY_FACTOR directly. T4 (BC-CSW1): intents from both tables are merged by `_reconcile_heat_intents` and applied via a single `HeatWriter.apply_heat_intents()` call — one `batch_writes` per cycle.
- **explanation:** Implements exponential heat decay on every memory and entity after each consolidation cycle. Heat represents recency × importance; it decays as `DECAY_FACTOR^hours`, modulated by emotional valence (high |valence| slows decay via EMOTIONAL_DECAY_RESISTANCE) and importance. Memories crossing COLD_THRESHOLD are archived (heat→0) and excluded from normal recall. The watermark (`last_decay_at`) prevents compounding decay across cycles — only the elapsed time since the last decay pass is charged, not since last access.

### CAP-CONS-002 — Surprise & Synaptic Boosting
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `SURPRISE_BOOST`, `SYNAPTIC_BOOST`, `SYNAPTIC_WINDOW_MINUTES`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/thermodynamics/thermodynamics.py`, `yadgar/backend/consolidation/cls.py::_CLSMixin._process_new_episodes`
- **wiring:** SURPRISE_BOOST is applied in `MemoryThermodynamics.compute_heat_with_surprise()` at write time when a memory has a non-zero surprise_score. SYNAPTIC_BOOST fires in `_process_new_episodes()`: if a source episode's linked memory has importance > 0.7, `self._thermo.synaptic_boost()` is called which finds all memories created within `SYNAPTIC_WINDOW_MINUTES` of the event and boosts their heat by `SYNAPTIC_BOOST * event_heat`.
- **explanation:** Two related heat-amplification mechanisms. Surprise boost elevates the initial heat of high-surprise memories (discovery events, unexpected outcomes) by adding `surprise_score * SURPRISE_BOOST` to base heat at write time. Synaptic boost (Hebbian-inspired) amplifies temporally adjacent memories when a high-importance event is processed during episode consolidation: memories written within SYNAPTIC_WINDOW_MINUTES of the trigger event receive a heat increment proportional to SYNAPTIC_BOOST, implementing a basic coincidence-based strengthening rule.

### CAP-CONS-003 — CLS Dual-Store Consolidation (Go-CLS)
- **status:** LIVE
- **category:** consolidation
- **settings:** `CLS_PATTERN_MAX_CANDIDATES`, `CLUSTER_SIMILARITY_THRESHOLD`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CLS1`, `BC-CLS2`, `BC-CLS3`
- **refs:** `yadgar/backend/cls_store/__init__.py::DualStoreCLS.consolidation_cycle`, `yadgar/backend/cls_store/clustering.py::_ClusteringMixin.find_recurring_patterns`, `yadgar/backend/cls_store/promotion.py::_PromotionMixin._promote_pattern`, `yadgar/backend/cls_store/patterns.py::_PatternsMixin`
- **wiring:** `ConsolidationScheduler._consolidation_cycle()` → `_run_curation_phases()` → `self._cls.consolidation_cycle()`. Runs every cycle. `DualStoreCLS` is initialized in `ConsolidationScheduler.__init__()`. Pattern detection is capped at `CLS_PATTERN_MAX_CANDIDATES` most-recent episodic memories. Clusters with cosine similarity ≥ `CLUSTER_SIMILARITY_THRESHOLD` and meeting session/directory diversity requirements are promoted to semantic memories.
- **explanation:** Implements the Go-CLS model (McClelland et al. 1995; Sun et al. 2023): episodic (hippocampal-fast) memories that recur across multiple sessions are abstracted into semantic (neocortical-slow) memories. `find_recurring_patterns()` builds a numpy pairwise cosine similarity matrix over recent episodic memories, performs greedy clustering at `CLUSTER_SIMILARITY_THRESHOLD`, and filters for clusters with ≥ 3 occurrences across ≥ 2 sessions. Qualifying clusters pass consistency checking (negation-pattern contradiction detection), then `abstract_to_schema()` generates a "Recurring pattern…" summary. The schema is promoted to a new semantic memory, episodic sources are linked via `derived_from` edges, and no episodic memories are deleted.

### CAP-CONS-004 — AstrocytePool (domain-aware consolidation)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `ASTROCYTE_POOL_ENABLED`, `ASTROCYTE_PROCESS_RETENTION_DAYS`, `NUM_ASTROCYTE_PROCESSES`
- **tools:** —
- **migrations:** —
- **bc:** `BC-C5a`, `BC-C5b`, `BC-AS1`, `BC-AS2`
- **refs:** `yadgar/_shared/astrocyte_pool/astrocyte_pool.py::AstrocytePool`, `yadgar/backend/consolidation/__init__.py::ConsolidationScheduler._run_domain_consolidation`, `yadgar/backend/consolidation/heat_decay.py::_HeatDecayMixin._build_domain_multiplier_map`
- **wiring:** Initialized in `ConsolidationScheduler.__init__()` if `ASTROCYTE_POOL_ENABLED=True` (default). During `_consolidation_cycle()`, after all memory-producing phases, the orchestrator checks `_pool is not None and ASTROCYTE_POOL_ENABLED`, then calls `_run_domain_consolidation()` which iterates `pool.get_process_stats()` → `pool.consolidate_domain(name)` for each of the four domain processes (code-patterns, decisions, errors, dependencies). The domain-multiplier map used by heat decay (`_build_domain_multiplier_map()`) also reads the pool to apply per-domain decay rates. Prior audit #40 noted domain consolidation "never fires" — this was the old daemon path; the code is now confirmed wired at line 232-244 in `orchestrator.py`. Status: LIVE.
- **explanation:** Modeled on astrocyte glial cells which support domain-specific neuronal populations. Four specialized processes each track a domain (code-patterns, decisions, errors, dependencies) with distinct `decay_multiplier` values (e.g. decisions decay 1.5× slower, errors 0.7× faster). Each domain consolidation pass re-scans assigned memories, extracts domain-typed entities (file/function, decision, error/solution, dependency), and reinforces or creates entity graph nodes. The pool also supports consensus retrieval (multi-domain voting with 15% multi-domain boost) used during recall. Process records are pruned after `ASTROCYTE_PROCESS_RETENTION_DAYS` days.

### CAP-CONS-005 — Cold-Memory Retention (dry-run / purge gate)
- **status:** DORMANT
- **category:** consolidation
- **settings:** `COLD_MEMORY_PURGE_ENABLED`, `COLD_MEMORY_PURGE_DRY_RUN`, `COLD_MEMORY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/consolidation/cold_retention.py::_cold_memory_retention_report`, `yadgar/backend/consolidation/cleanup.py::_CleanupMixin._run_retention_tasks`
- **wiring:** Called from `_run_retention_tasks()` in every consolidation cycle when `COLD_MEMORY_RETENTION_DAYS > 0`. Identifies candidate memories (heat < COLD_THRESHOLD, age > COLD_MEMORY_RETENTION_DAYS, access_count = 0, not protected, no `_anchor` tag). By default (`COLD_MEMORY_PURGE_ENABLED=False`, `COLD_MEMORY_PURGE_DRY_RUN=True`), the function only reports candidates and emits a Prometheus gauge — it deletes nothing. Real deletion requires both gates explicitly set. Status is DORMANT because the deletion path requires non-default config; the report path runs every cycle (LIVE for visibility, DORMANT for the purge itself).
- **explanation:** Addresses the #44 data-loss risk: cold immortal user memories that have no access history and exceed the retention age. The two-gate design (`PURGE_ENABLED=False` AND `DRY_RUN=True`) requires both to be overridden before any memory is deleted, preventing accidental data loss. The report path always fires and emits a `yadgar_cold_purge_candidates` Prometheus gauge so operators can observe the population before enabling deletion. Conservative candidate criteria exclude protected memories and anchors.

### CAP-CONS-006 — Episode Processing & Entity Extraction
- **status:** LIVE
- **category:** consolidation
- **settings:** `EPISODE_RETENTION_DAYS`, `MAX_EPISODE_TOKENS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CA1`
- **refs:** `yadgar/backend/consolidation/cls.py::_CLSMixin._process_new_episodes`, `yadgar/backend/consolidation/cleanup.py::_CleanupMixin._prune_old_episodes_safe`, `yadgar/_shared/sensory_buffer/sensory_buffer.py`
- **wiring:** `_run_episodic_phases()` → `_process_new_episodes()`. Fetches all episodes with ID > `_last_consolidated_episode_id`. For each episode: typed entity extraction (`_graph.extract_entities_typed()`) + legacy regex extraction → `_upsert_entities()` → bulk co-occurrence relationship writes. Episodes older than `EPISODE_RETENTION_DAYS` are pruned by `_prune_old_episodes_safe()` after each pass. `MAX_EPISODE_TOKENS` controls episode chunking in `SensoryBuffer` at capture time (1 token ≈ 4 chars).
- **explanation:** The episodic processing phase ingests raw episodes captured by the PostToolCall hook and promotes them into the entity knowledge graph. Each episode is scanned for file paths, Python/JS definitions, error types, imports, and decision keywords to extract typed entities. Co-occurrence relationships are batch-written for all entity pairs found in the same episode. This builds the substrate for both the causal discovery (PC algorithm) and the graph-prior computation. Episodes are pruned after `EPISODE_RETENTION_DAYS` to keep the table bounded.

### CAP-CONS-007 — Dream Replay (sleep cycle phase 1)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `DREAM_REPLAY_PAIRS`, `DREAM_INSIGHT_MAX_AGE_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`
- **refs:** `yadgar/backend/sleep_compute/dream.py::_DreamMixin.dream_replay`, `yadgar/backend/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`, `yadgar/backend/consolidation/__init__.py::ConsolidationScheduler.run_nightly_consolidation`
- **wiring:** `run_nightly_consolidation()` → `_maybe_sleep_cycle()` → `SleepComputeEngine.run_sleep_cycle()` → `dream_replay()`. This runs at most once every 6 hours (6-hour guard in `_maybe_sleep_cycle`). Also reachable via `consolidate_now(mode='full')` MCP tool. The nightly cron path was dead from v5.7.0 until v5.72 (#61, PR-1) re-wired `_maybe_sleep_cycle()` into `run_nightly_consolidation()`. DREAM_INSIGHT_MAX_AGE_DAYS is used in curation prune passes (`yadgar/backend/curation/prune_passes.py:139`) to purge stale dream insight memories.
- **explanation:** Implements offline memory replay inspired by hippocampal sharp-wave ripples during sleep. Selects up to `DREAM_REPLAY_PAIRS` random pairs of memories with embeddings that are not yet connected in the entity graph. For each pair with cosine similarity > 0.4, a weak co-occurrence link (weight=0.5) is created. Pairs with similarity > 0.7 additionally generate a synthetic "Dream connection" memory with surprise_score=0.8 and heat=0.5. These insights are pruned by the curation pass when older than `DREAM_INSIGHT_MAX_AGE_DAYS` days.

### CAP-CONS-008 — Community Detection & Cluster Summarization (sleep cycle phases 2-3)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`, `BC-VZ-R3`
- **refs:** `yadgar/backend/sleep_compute/community.py::_CommunityMixin.detect_communities`, `yadgar/backend/sleep_compute/community.py::_CommunityMixin.generate_cluster_summaries`, `yadgar/backend/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`, `yadgar/_shared/storage/cluster.py::_ClusterMixin.get_memory_clusters`, `yadgar/_shared/storage/cluster.py::_ClusterMixin.get_cluster_members`
- **wiring:** `run_sleep_cycle()` → `detect_communities()` then `generate_cluster_summaries()`. Both run in the nightly sleep cycle gated by the 6-hour guard. `detect_communities()` builds a networkx Graph from all active entity relationships and runs Louvain community detection (fallback: label propagation). `generate_cluster_summaries()` generates text summaries and centroid embeddings for clusters with > 3 members, then groups level-1 clusters into level-2 root clusters by directory context. `FRACTAL_LEVELS` was deleted in v6 T3 — only 2 levels (community + root) are built; deeper clustering remains future work. v5.80 (#80 viz-fidelity-v2): memory_cluster viz-consumption is now LIVE — `get_memory_clusters()` + `get_cluster_members(cid)` added to `_ClusterMixin` and consumed by `GraphAPI.get_full_graph()` to emit clusters[] in the graph payload (BC-VZ-R3).
- **explanation:** Identifies coherent memory clusters using graph-community detection on the entity co-occurrence graph. Communities (groups of entities that co-occur frequently) are stored as `memory_cluster` records, and memories mentioning those entities are assigned to clusters (via cluster_id field on memory rows). Level-2 (root) clusters group level-1 communities by dominant directory context, implementing a two-level hierarchical structure. `FRACTAL_LEVELS` was CONFIG-ONLY (deleted v6 T3) — only 2 levels are built regardless. Cluster viz-consumption was previously DORMANT (write path only); v5.80 wires the read path through CAP-VIZ-011.

### CAP-CONS-009 — Re-embedding & Memory Compression (sleep cycle phases 4-5)
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`
- **refs:** `yadgar/backend/sleep_compute/embed_compress.py::_EmbedCompressMixin.reembed_stale`, `yadgar/backend/sleep_compute/embed_compress.py::_EmbedCompressMixin.compress_old_memories`, `yadgar/backend/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`
- **wiring:** `run_sleep_cycle()` → `reembed_stale()` → `compress_old_memories()`. Both run nightly in the sleep cycle. `reembed_stale()` fetches memories whose `embedding_model` differs from the current model and re-encodes them in batches of 50. `compress_old_memories()` uses a `days_threshold=30` hard-coded value. `COMPRESSION_GIST_AGE_HOURS` and `COMPRESSION_TAG_AGE_HOURS` were CONFIG-ONLY and deleted in v6 T3.
- **explanation:** Two maintenance passes run during the nightly sleep cycle. Re-embedding updates embeddings when the active model changes. Memory compression extracts key sentences from verbose old memories (> 1000 chars, older than 30 days) using entity-pattern regex. `COMPRESSION_GIST_AGE_HOURS` and `COMPRESSION_TAG_AGE_HOURS` were never read by `compress_old_memories()` — deleted in v6 T3 (#41).

### CAP-CONS-010 — Narrative Auto-Generation (sleep cycle phase 6)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `NARRATIVE_INTERVAL_HOURS`, `NARRATIVE_ENTRY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`
- **refs:** `yadgar/backend/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`, `yadgar/backend/narrative/narrative.py::NarrativeEngine.auto_narrate`
- **wiring:** `run_sleep_cycle()` → `self._narrative.auto_narrate()`. `NarrativeEngine` is instantiated in `SleepComputeEngine.__init__()`. `auto_narrate()` checks if a narrative entry exists in the last `NARRATIVE_INTERVAL_HOURS` hours; if not, it generates one. Narrative entries older than `NARRATIVE_ENTRY_RETENTION_DAYS` are pruned by `_run_retention_tasks()` in the main consolidation cycle.
- **explanation:** Generates periodic narrative summaries of recent memory activity to provide a human-readable chronicle of what the system has learned. Runs at most once per `NARRATIVE_INTERVAL_HOURS` (default 24h) as phase 6 of the nightly sleep cycle. The narrative is stored as a `narrative_entry` record and pruned after `NARRATIVE_ENTRY_RETENTION_DAYS` days (default 90). This is distinct from wiki content — it's an autobiographical memory of system activity rather than curated knowledge.

### CAP-CONS-011 — Causal Discovery (PC algorithm)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `CAUSAL_THRESHOLD`, `MAX_CAUSED_BY_ROWS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CA2`, `BC-CA3`
- **refs:** `yadgar/backend/consolidation/causal.py::_CausalMixin._run_causal_discovery_phase`, `yadgar/backend/causal_discovery/pc.py::pc_algorithm`, `yadgar/backend/causal_discovery/__init__.py`
- **wiring:** `_consolidation_cycle()` → `_run_causal_discovery_phase()`. Runs periodically: fires when `_events_since_last_discovery >= 50` (hardcoded threshold, not CAUSAL_THRESHOLD). `CAUSAL_THRESHOLD` (default 3) controls how many times an entity must co-occur to be considered causally relevant in `CausalDiscovery.detect_causality()` — a separate simpler method. `MAX_CAUSED_BY_ROWS` bounds the `caused_by` table query. The PC algorithm is initialized in `ConsolidationScheduler.__init__()` via lazy import of `CausalDiscovery`.
- **explanation:** Implements the PC (Peter-Clark) constraint-based causal discovery algorithm to discover directed causal relationships between entities in the knowledge graph. Builds a time-aligned binary event matrix (1-hour buckets over 168 hours) where rows = time windows and columns = entity variables. Phase 1 removes undirected edges where conditional independence is detected (Fisher's z-test). Phase 2 orients v-structures and applies Meek's rules (R1/R2/R3) to produce a Partially Directed Acyclic Graph (PDAG). Results are stored as directed and undirected edge records in the knowledge graph. Fires only when ≥ 50 new memories have been added since the last discovery run.

### CAP-CONS-012 — Consolidation Cycle & Cooldown
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** `consolidate_now`
- **migrations:** —
- **bc:** `BC-C1`
- **refs:** `yadgar/backend/consolidation/__init__.py::ConsolidationScheduler.force_consolidate`, `yadgar/backend/consolidation/__init__.py::ConsolidationScheduler.run_nightly_consolidation`, `yadgar/core/server/tools/admin_other.py::consolidate_now`
- **wiring:** `consolidate_now` MCP tool → `force_consolidate()` → `_consolidation_cycle()`. The nightly cron calls `run_nightly_consolidation()` which runs the cycle then `_maybe_sleep_cycle()`. `CONSOLIDATION_COOLDOWN_SECONDS` and `IDLE_THRESHOLD_SECONDS` were CONFIG-ONLY (daemon removed in v5.7.0) — both deleted in v6 T3.
- **explanation:** The main consolidation cycle orchestrates six phases: (1) episodic phases — decay, episode processing, prune, duplicate merge; (2) graph phases — similarity linking, causality detection, graph/cofire priors; (3) curation phases — memify, CLS consolidation, action log; (4) domain consolidation via AstrocytePool; (5) formal causal discovery (periodic); (6) retention tasks. The `consolidate_now` MCP tool exposes this on-demand. `CONSOLIDATION_COOLDOWN_SECONDS` and `IDLE_THRESHOLD_SECONDS` deleted in v6 T3 (#41) — the background daemon was removed in v5.7.0; these settings had no runtime consumer.

### CAP-CONS-013 — Vacuum (DB compaction)
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `vacuum_now`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_vacuum.py::vacuum_now`, `yadgar/core/ops/ops.py`
- **wiring:** `vacuum_now` MCP tool → `_fire_vacuum_service()` in `yadgar/core/ops/ops.py`. Writes a trigger file to `YADGAR_VACUUM_TRIGGER_PATH`, which the surfaces that ship a watcher (macOS launchd, the repo `flake.nix`, the private nix module) point at `~/.local/state/yadgar/triggers/` via a host bind mount; the watcher fires `yadgar-vacuum.service`. **Not every surface ships a watcher** — `scripts/install/generate_systemd.sh` (non-nix systemd) and `yadgar/core/daemon/systemd.py` (named `/data` volume, no host path to watch) ship none and deliberately leave the env unset, so `vacuum_now()` returns `started=False, skipped_reason="no_trigger_path_configured"` there instead of writing into a void (task:0044). The per-surface invariant is enforced by `yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py`. Auto-vacuum also fires from `_maybe_auto_vacuum()` called in `_run_post_cycle_tasks()` after each consolidation cycle when DB size exceeds threshold and we're in the configured time window.
- **explanation:** Triggers SurrealDB compaction (VACUUM) to reclaim disk space from deleted/updated records. Intentionally runs out-of-process via a trigger file to avoid blocking the consolidation cycle. The MCP tool `vacuum_now` accepts a `force` flag that bypasses the in-window check. Auto-vacuum (from consolidation post-cycle tasks) respects a 6-hour in-memory cooldown and the VACUUM_AUTO_WINDOW_START/END config window to avoid running during peak usage hours.

### CAP-CONS-014 — Engram Allocator (Josselyn-Frankland slot model)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `EXCITABILITY_BOOST`, `EXCITABILITY_HALF_LIFE_HOURS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/contracts/engram.py::EngramAllocator`, `yadgar/_shared/contracts/engram.py::EngramAllocator.allocate`, `yadgar/_shared/contracts/engram.py::EngramAllocator.boost_excitability`
- **wiring:** `EngramAllocator` is instantiated at server startup. Called when memories are written: `allocate(memory_id)` selects a slot, boosts its excitability by `EXCITABILITY_BOOST`, applies lateral inhibition to ±2 neighboring slots (at 50% of boost), and updates the memory's `excitability` field. Slot excitability decays exponentially with `EXCITABILITY_HALF_LIFE_HOURS` (default 6h). Warm slots (excitability ≥ 0.05) attract nearby writes, creating automatic temporal clusters.
- **explanation:** Implements the Josselyn & Frankland (2007) / Rashid et al. (2016) engram cell model — NOT Hopfield networks. Neurons (memory slots) compete via CREB-like excitability: the most excited slot wins the allocation competition, and memories written during the same excited window share a slot, creating temporal associations with zero explicit logic. After ~3 half-lives (~18 hours with default settings), a slot's excitability drops below the warm threshold and the next write starts a new temporal cluster. Lateral inhibition (reducing neighbors' excitability) sharpens cluster boundaries.

### CAP-CONS-015 — Plasticity / Stability / Reconsolidation (schema fields — dead config)
- **status:** DEAD (v6 T3 — all 5 settings deleted from config.py)
- **category:** brain-dynamics
- **settings:** — (PLASTICITY_SPIKE, PLASTICITY_HALF_LIFE_HOURS, STABILITY_INCREMENT, RECONSOLIDATION_LOW_THRESHOLD, RECONSOLIDATION_HIGH_THRESHOLD all deleted)
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/contracts/models.py` (DB schema fields retained)
- **wiring:** Settings deleted from `config.py` in v6 T3. The memory schema still has `plasticity`, `stability`, and `reconsolidation_count` fields (written at insert time with hardcoded values: plasticity=1.0, stability=0.0). No production code ever read these settings.
- **explanation:** Reconsolidation theory (Nader et al. 2000) schema support remains in DB fields but the behavioral logic was never implemented. Config settings deleted in v6 T3 (#41). DB schema fields retained for future implementation.

### CAP-CONS-016 — Derived Beliefs
- **status:** LIVE
- **category:** enrichment
- **settings:** `DERIVED_BELIEFS_ENABLED`, `DERIVED_BELIEF_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-MC3`
- **refs:** `yadgar/backend/retrieval/fusion.py`, `yadgar/backend/curation/__init__.py::MemoryCurator._memify_derive`, `yadgar/backend/curation/strengthen.py::_memify_derive`
- **wiring:** `DERIVED_BELIEFS_ENABLED` is checked in `yadgar/backend/retrieval/fusion.py:422` (getattr default False — note mismatch with config default True). `_memify_derive()` is called from `MemoryCurator.memify_cycle()` → called from `_run_curation_phases()` during each consolidation cycle. `DERIVED_BELIEF_RETENTION_DAYS` controls pruning of `derived_belief` table rows in `_run_retention_tasks()`.
- **explanation:** Derives new beliefs by finding co-occurring entity clusters in the memory store and creating summary "derived" memories tagged `["derived", "auto-generated"]`. The derive pass scans entities that frequently appear together, generates a co-occurrence summary, and inserts it as a new episodic memory with importance=0.6. Derived beliefs are pruned from the `derived_belief` table after `DERIVED_BELIEF_RETENTION_DAYS` days. The retrieval path checks `DERIVED_BELIEFS_ENABLED` to decide whether to include derived belief results in fusion — note a potential config default mismatch between config.py (True) and fusion.py (getattr default False).

### CAP-CONS-017 — Cognitive Load Limiting
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `COGNITIVE_LOAD_LIMIT`
- **tools:** —
- **migrations:** —
- **bc:** `BC-MC4`
- **refs:** `yadgar/_shared/metacognition/__init__.py`, `yadgar/_shared/metacognition/cognitive_load.py::_CognitiveLoadMixin`
- **wiring:** `MetacognitionEngine.__init__()` sets `self._chunk_limit = settings.COGNITIVE_LOAD_LIMIT`. `_CognitiveLoadMixin.manage_context()` enforces this limit on recall result sets. Called from the recall pipeline when metacognition is enabled. Default is 4 (Cowan's 4±1 model of working memory capacity).
- **explanation:** Implements Cowan's (2001) model of working memory capacity: the number of independently retrievable chunks is limited to 4 ± 1. `COGNITIVE_LOAD_LIMIT` caps how many memory chunks can be returned in a single recall context window. Memories exceeding the limit are either summarized (overflow handling) or dropped, preventing context saturation. This is the retrieval-side counterpart to the storage-side episode chunking controlled by MAX_EPISODE_TOKENS.

### CAP-CONS-018 — Successor Representation / Cognitive Map
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CM1`
- **refs:** `yadgar/backend/restoration/cognitive_map.py::CognitiveMap`, `yadgar/backend/restoration/checkpoint_restore.py`, `yadgar/backend/restoration/__init__.py::ensure_restoration_engines`, `yadgar/_shared/runtime/sr_session.py::SRTransitionRecorder`
- **wiring:** T2 Car B: the shared root (`_init_secondary_engines`) builds only the session-side `SRTransitionRecorder`; the backend composition point `ensure_restoration_engines()` upgrades the slot to the full `CognitiveMap` and passes it to `CheckpointRestore`. Every `recall` call records a transition via `_st._cognitive_map.record_transition()` + `incremental_update()` (recall.py:329-338). During `restore` MCP tool, `CheckpointRestore._predict_memories()` calls `has_sufficient_data()` (needs ≥20 transitions) and then `navigate_to()` — results are iterated into the `predicted` list and included in the restore output. Spatial layout methods (extract_coordinates/update_memory_sr_coords/get_neighborhood/get_sr_scores) were retired in v5.71.0 (#47). BC-CM1 e2e-proven in v5.75 (train T5): `tests/e2e/test_phase3_closure.py::TestBCCM1_SRTransitionMatrixBuilt`.
- **explanation:** The Successor Representation (Dayan 1993) predicts future states from current position in a learned transition graph. `build_transition_matrix()` counts memory-to-memory transitions from the `memory_transition` table, applies discount factor γ, and computes the SR matrix as `(I - γT)^{-1}`. `navigate_to()` uses the SR to rank memories by expected future relevance given a query memory as starting state. The SR result feeds `CheckpointRestore._predict_memories()` and surfaces in the `restore` tool output as predicted next-retrieval candidates. BC-CM1 (SR transition matrix built) is LIVE and e2e-proven.

### CAP-CONS-019 — Action Log Processing
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-AS1`, `BC-AS2`
- **refs:** `yadgar/backend/consolidation/cleanup.py::_CleanupMixin._process_action_log`
- **wiring:** `_run_curation_phases()` → `self._process_action_log()`. Fetches up to 200 unprocessed `action_log` rows per cycle. Groups them by (directory, 30-minute bucket). Groups with ≥ 3 actions generate a summary memory tagged `["_action_stream", "_auto"]` with heat=0.4 and directory_context from the action. Processed rows are marked processed; old processed rows are pruned by `_prune_action_log_safe()`.
- **explanation:** The hot path (PostToolCall hook) writes raw tool-call records to the `action_log` table. The cold path (this cleanup pass) consolidates them into retrievable memories: actions within a 30-minute window and same directory are grouped, tool-call counts and summaries are built, and a session-activity memory is inserted for groups with ≥ 3 calls. Secret-blocked entries are quarantined to `~/.local/state/yadgar/quarantine/action_log_poison.jsonl`. This implements BC-AS1 (action records persist) and provides the substrate for BC-AS2 (directory-scoped recall of action memories).

### CAP-CONS-020 — Compression Settings (CONFIG-ONLY)
- **status:** DEAD (v6 T3 — all 3 settings deleted from config.py)
- **category:** config
- **settings:** — (COMPRESSION_GIST_AGE_HOURS, COMPRESSION_TAG_AGE_HOURS, FRACTAL_LEVELS all deleted)
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** All three settings deleted from `config.py` and `config_yaml.py` in v6 T3. `compress_old_memories()` uses hardcoded `days_threshold=30`. `FRACTAL_LEVELS` was intended for multi-level clustering but only 2 levels are built.
- **explanation:** Three planned-but-unimplemented rate-distortion compression controls. Never read by production code. Removed in v6 T3 dead-config cleanup (#41). The schema (compression_level field) and 2-level cluster hierarchy remain implemented.

### CAP-CONS-021 — Derived Memory Pruning (memify_prune)
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-C3`
- **refs:** `yadgar/backend/curation/prune_passes.py::_memify_prune`, `yadgar/backend/curation/__init__.py::MemoryCurator.memify_cycle`
- **wiring:** `_run_curation_phases()` → `self._curator.memify_cycle()` → `_memify_prune()`. Runs every consolidation cycle. Operates in multiple passes: (1) cold unaccessed auto-generated memories, (2) cold unaccessed auto-abstracted, (3) cold unaccessed dream insights, (4) hard-cap dream insights by `DREAM_INSIGHT_MAX_AGE_DAYS`, (5) stale action-stream memories by recency (v5.66), (6) degenerate auto-abstracted schemas. Protected memories and recently-accessed memories are always spared.
- **explanation:** Implements the retention policy for system-generated (non-user) memories. Pruning is structured in ordered passes to catch different memory classes: action-stream summaries use a combined created_at + last_accessed recency check (v5.66 fix — a single accidental recall no longer grants immortality), dream insights have a hard age cap regardless of heat, and degenerate CLS schemas (no meaningful subject) are deleted unconditionally. User-created memories are never touched by this pass; the `cold_retention` pass (CAP-CONS-005) handles those via separate gated logic.

### CAP-CONS-022 — Metacognitive Gap Detection
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-MC1`, `BC-MC2`
- **refs:** `yadgar/_shared/metacognition/gap_detection.py::_GapDetectionMixin.detect_gaps`, `yadgar/backend/restoration/checkpoint_restore.py::CheckpointRestore._detect_gaps_safe`
- **wiring:** `RestorationEngine._detect_gaps_safe()` calls `self._metacognition.detect_gaps(directory)` during context restore (up to 3 gaps included in restore output). `detect_gaps()` runs five detection passes: isolated entities (≤1 connection), stale memory regions (heat < 0.3), low-confidence memories (< 0.5), missing co-occurrence links (entities co-occurring in ≥ 2 memories but without a graph edge), and one-sided knowledge (errors with no resolved_by edge). Wired into the restore path, so it runs on every `restore` invocation.
- **explanation:** Implements MetaRAG Signal 2 — awareness of what the system does NOT know. BC-MC1 (coverage scored by entity/topic distribution) is partially implemented via the entity-graph analysis in the five gap passes. BC-MC2 (gap detection flags missing topics) is implemented: `detect_gaps()` returns structured gap records with type, description, severity, affected entities, and remediation suggestions. These surface knowledge holes: isolated entities, stale knowledge regions, low-confidence beliefs, missing co-occurrence relationships, and unresolved errors.

### CAP-CONS-023 — Retired Cognitive Map Capabilities (BC-CM2/CM3)
- **status:** DEAD
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CM2`, `BC-CM3`
- **refs:** `yadgar/backend/restoration/cognitive_map.py`
- **wiring:** BC-CM2 (topological/spatial layout via `extract_coordinates`/`update_memory_coordinates`) and BC-CM3 (`get_neighborhood`/`get_sr_scores`/`is_dirty`) were RETIRED in v5.71.0 (#47). The methods were deleted from the codebase. These BCs are marked 🗑 RETIRED in BEHAVIOR_CONTRACT.md — not failing specs, but permanently removed capability. The SR matrix and `sr_x`/`sr_y` coordinate fields remain on the memory schema (used by the active `CognitiveMap.compute_sr_matrix()` path), but the spatial layout and neighborhood-query methods are gone.
- **explanation:** The spatial/topological visualization capabilities — `extract_coordinates()` for 2D layout and `get_neighborhood()`/`get_sr_scores()`/`is_dirty()` for proximity queries — were removed as dead code in v5.71.0 during the #41 dead-config audit. They were never wired to any MCP tool or recall path. The `CognitiveMap` class remains but is scoped to: `build_transition_matrix()`, `compute_sr_matrix()`, `navigate_to()`, and `has_sufficient_data()` — the SR-based recall navigation path (active via RestorationEngine).

### CAP-CONS-025 — Replay / Restoration Settings
- **status:** LIVE
- **category:** consolidation
- **settings:** `REPLAY_ANCHOR_HEAT`, `REPLAY_CHECKPOINT_AUTO_INTERVAL`, `REPLAY_MAX_RESTORE_MEMORIES`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/restoration/checkpoint_restore.py`
- **wiring:** All three settings are read by `CheckpointRestore` in `yadgar/backend/restoration/checkpoint_restore.py` (moved backend-side in T2 Car B, behind `POST /restore`). `REPLAY_CHECKPOINT_AUTO_INTERVAL` (default 50) triggers auto-checkpoint every N tool calls. `REPLAY_ANCHOR_HEAT` (default 1.0) sets the heat of anchored memories when they are loaded in a restore pass. `REPLAY_MAX_RESTORE_MEMORIES` (default 8) caps the number of memories included in a restoration context packet.
- **explanation:** Controls the behavior of the checkpoint/restore system used to resume context after `/clear` or session restart. Auto-checkpointing fires every `REPLAY_CHECKPOINT_AUTO_INTERVAL` tool calls to keep a recent state snapshot. On restore, up to `REPLAY_MAX_RESTORE_MEMORIES` memories are included in the context reconstruction, and anchored memories are assigned `REPLAY_ANCHOR_HEAT` to ensure they remain hot and at the top of ranked results.

### CAP-CONS-026 — Consolidation compute backend (POST /consolidate)
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/consolidation/service.py::run_consolidation_cycle`, `yadgar/backend/embed_service/embed_service.py::consolidate_route`, `yadgar/core/consolidation/orchestrator.py::_forward_to_backend`, `yadgar/core/consolidation/orchestrator.py::run_consolidate_now`, `yadgar/core/consolidation/orchestrator.py::run_nightly_consolidation`
- **wiring:** R3 Car 1 (forward-only): the core consolidation orchestrator (`run_consolidate_now` / `run_nightly_consolidation`) POSTs the requested mode to the backend `/consolidate` endpoint via `_forward_to_backend`, mirroring the `/recall` forward path (same Bearer admin auth). Backend `consolidate_route` lazily builds the compute engine set and runs one cycle in a worker thread (`asyncio.to_thread`) so the event loop is not blocked; `run_consolidation_cycle` holds the `ConsolidationScheduler` as a process singleton so the 6-hour sleep-cycle gate survives across calls (nightly + `consolidate_now(full)` share one gate — double-fire avoidance is automatic). Core keeps the orchestration tail (vacuum / graph-layout / invariants) and `StalenessDetector`; the 8 compute engines (heat-decay, CLS, cleanup, cold-retention, causal, curation, sleep-compute, narrative/predictive) run backend-side.
- **explanation:** R3 Car 1 moves the async-DB-write drainer and the consolidation COMPUTE cycle into the backend, correcting the R2a transitive mis-park where curation/cls_store/predictive/narrative/sleep/causal were parked "core-only" merely because their consumers had not yet moved. The compute is exposed as `POST /consolidate` (mode: light / full / nightly); core shells forward-only. No compat shims; import-linter 4/0.

### CAP-STOR-041 — CRUD write forward dispatch (POST /admin)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/forward.py::_forward_admin`, `yadgar/backend/embed_service/embed_service.py::admin_route`, `yadgar/backend/admin_exec/__init__.py::run_admin_op`, `yadgar/backend/admin_exec/bookmarks.py`, `yadgar/backend/admin_exec/blocks.py`
- **wiring:** R3 Car 3a (R5 forward pattern): the pure-CRUD write MCP tools (`bookmark_add`/`bookmark_remove`/`bookmark_reorder`, `block_create`/`block_update`/`block_delete`/`block_replace`/`block_append`) keep their `@_tool` shell + validation + secret gate (I26) in core and forward the storage write to the backend `POST /admin` endpoint via `_forward_admin` (HTTP only, no core→backend import; same Bearer admin auth as `/recall` + `/consolidate`). The `admin_route` lazily builds the slim engine set (`_ensure_recall_engines`, for storage) and dispatches via `run_admin_op(op, payload)` in a worker thread (`asyncio.to_thread`); `run_admin_op` maps the op name (= tool name, single source of truth in `_ADMIN_OPS`) to its undecorated backend impl in `admin_exec.bookmarks` / `admin_exec.blocks`. Unknown op → 400. Forward-only: `YADGAR_EMBED_URL` unset → RuntimeError (no in-core storage fallback).
- **explanation:** R3 Car 3a is the first R5 group: it establishes the generic `/admin` write-forward dispatch (parallel to the `/recall` + `/consolidate` compute forwards from Cars 1/D) and moves the storage-WRITE half of the bookmark + block CRUD tools to the backend, so core is a thin router that touches zero DB directly. Read tools (`bookmark_list`, `block_get`, `block_list`) stay core. Later R5 groups (forget/memory_update, wiki delete/restore, audit_anchors, project/dispatch/invariant writes) extend `_ADMIN_OPS` with more ops. No compat shims; import-linter 4/0.

### CAP-STOR-042 — memory/rules write ops → /admin (R3 Car 3b)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `forget`, `memory_update`, `reembed_all`, `add_rule`, `archive_purge`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::forget`, `yadgar/core/server/tools/admin_other.py::memory_update`, `yadgar/core/server/tools/admin_other.py::reembed_all`, `yadgar/core/server/tools/admin_other.py::add_rule`, `yadgar/core/server/tools/admin_archive.py::archive_purge`, `yadgar/backend/admin_exec/memory.py`
- **wiring:** R3 Car 3b (second R5 group) extends `_ADMIN_OPS` (CAP-STOR-041) with the non-wiki, non-audit memory/rules DB writes. Core keeps validation + the I26 secret gate and forwards the storage write to `POST /admin` via `_forward_admin`; the undecorated impls live in `admin_exec.memory`. Per op: `forget` (delete + structural-epoch bump — the bump is file-backed on the shared queue volume (Car 2), so a backend-side bump still busts the core `project_brief` cache for that directory), `memory_update` (allowed-key validation stays core, raising `ValueError` on disallowed keys before any forward; the `update_memory_fields` write forwards), `reembed_all` (heavy — forwards with a 1800s timeout so a large backlog does not trip the default 30s), `add_rule` (rules-engine `insert_rule`; the backend clears ITS rules cache, which is the cache the drainer's write-policy enforcement in `phase_validate` reads — enforcement stays coherent; the core `wiki.py` write-policy PRE-check uses a separate core RulesEngine cache — a pre-existing cross-process advisory drift, not introduced here), `archive_purge` (memory_archive purge — power-gated + secret-gated core, DB delete forwards). The backend slim engine set already builds `_embeddings` + `_rules_engine`, so `reembed_all` + `add_rule` run backend-side unchanged.
- **explanation:** Second R5 group. Reads stay core with direct storage access (`recent_memories`, `memory_stats`, `memory_get`, `get_rules`, `dlq_inspect`) — "zero DB" is a write-side goal. `dlq_requeue`/`dlq_dismiss` stay core: they are queue-FILE ops (rename/unlink on the shared queue volume), not DB writes. `validate_memory` stays core: its conditional `update_memory_staleness` write is inseparable from a host-filesystem file-hash read (`_compute_file_hash`) that the backend container cannot perform, and `_staleness` is a core-only engine (not in the slim set) — deferred like `vacuum_checkpoints`. So core still has two residual direct DB writes (`validate_memory`, `vacuum_checkpoints`), tracked honestly. No compat shims; import-linter 4/0.

### CAP-STOR-043 — wiki-edit family + agent_prompt writes → /admin (R3 Car 3c)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `wiki_delete`, `wiki_autolink`, `wiki_update`, `wiki_restore`, `wiki_append_section`, `wiki_set_metadata`, `wiki_set_mutability`, `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_delete_at`, `wiki_insert_at`, `wiki_replace_markdown_block`, `agent_prompt_save`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py`, `yadgar/core/server/tools/admin_other.py::wiki_update`, `yadgar/core/server/tools/agent_prompts.py::agent_prompt_save`, `yadgar/backend/admin_exec/wiki.py`
- **wiring:** R3 Car 3c (third R5 group) extends `_ADMIN_OPS` (CAP-STOR-041/042) with the wiki-EDIT family + `agent_prompt_save`. Core keeps the `@_tool` shell + input validation + the I26 secret gate (on `content`/`new_text`/`new_content`) and forwards the storage write to `POST /admin` via `_forward_admin`; the undecorated impls live in `admin_exec.wiki`. **Slug→page_id resolution stays CORE.** `_resolve_page_id_by_slug` resolves against the caller's directory; the backend container has a different cwd, so backend-side resolution would land the wrong `directory_context` row. Core resolves the slug to a `page_id` (reads are allowed core-side — "zero DB" is a write-side goal — and core+backend share the same DB), then forwards the write keyed by `page_id` (`wiki_restore`/`wiki_append_section`/`wiki_replace_text`/`wiki_delete_text`/`wiki_insert_after`/`wiki_insert_before`/`wiki_replace_at`/`wiki_delete_at`/`wiki_insert_at`/`wiki_replace_markdown_block`). `wiki_delete` + `wiki_set_metadata` + `wiki_autolink` are slug/args-keyed (no page_id resolution). One tool keeps core-side SIDE-EFFECTS after the forward returns: `wiki_delete` (SSE `_push_event` on core's bus + file-queue mirror cleanup). `wiki_update` (in `admin_other`) is page_id-keyed already: allowed-key validation + secret gate stay core, the `update_wiki_page` write forwards. `agent_prompt_save`: directory-validation + I26 secret gate + content-wrap stay core; the wiki body write (`wiki.add`) forwards as one op (`_st._wiki` is in the slim engine set). **0047 Car I** split the path: wiki body write forward + ledger row write forward are two separate ops (page-first ordering per §9 — crash leaves an orphan page, not an orphan row). The TOC upsert + library discovery anchor were RETIRED (D35a); the slug survives as a kept-ignored pointer for one cycle, but no row is written. **Cache epoch:** every `_st._wiki.*` write funnels through `storage.wiki.insert/update/delete/set_metadata` → `_bump_wiki_epoch → bump_epoch(None)`, a GLOBAL bump that is file-backed on the shared queue volume (Car 2), so a backend-side bump busts the core process's cached `wiki_read` / `wiki_query` / `agent_dispatch_prelude` namespaces cross-process.
- **explanation:** Third R5 group. Read tools stay core with direct storage access: `wiki_read`, `wiki_query`, `wiki_list`, `wiki_get`, `wiki_history`, `wiki_diff`, `wiki_read_version`, `wiki_check_duplicate`, `wiki_lint`, `bookmark_list`. `wiki_add` was already enqueue-only (Car 1) — not touched. `adr_add` stays core as a pure ORCHESTRATOR: it does zero direct `_st._wiki` writes, composing the core tools `wiki_read` (core read) + `wiki_append_section` (now forwards) + `wiki_add` (enqueue-only), so its writes forward transitively through its callees. `wiki_coverage` and `wiki_refresh_stale` were removed in #83 Car C (ADR-0157: container-blind anti-pattern; these tools ran daemon-side but needed host filesystem access). No compat shims; import-linter 4/0.

### CAP-STOR-044 — restore compute forward (POST /restore) + pre_compact_drain op (T2 Car B)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `restore`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/forward.py::_forward_restore`, `yadgar/backend/embed_service/embed_service.py::restore_route`, `yadgar/backend/restoration/__init__.py`, `yadgar/backend/admin_exec/restoration.py::pre_compact_drain`, `yadgar/core/cli/_shared.py`
- **wiring:** T2 layer-boundary Car B (census verdict #7): `CheckpointRestore` + `CognitiveMap` moved `_shared` → `yadgar.backend.restoration` (PEP-562 shims at the old paths for tests). The backend `POST /restore` route (same Bearer admin auth as `/recall`) lazily builds the slim engine set, then `run_restore(directory)` invalidates the SR matrix (transitions are recorded core-side, so the backend in-process `_dirty` flag cannot see them) and runs the restore compute in a worker thread, returning the exact pre-Car-B payload as `{"result": ...}`. Core callers of the forward: the `restore` MCP tool, `/hooks/post-compact`, and the `yadgar restore` CLI subcommand. The write-only `pre_compact_drain` (epoch increment + auto-checkpoint upsert, no compute) rides `POST /admin` instead — callers: `/hooks/pre-compact` + the `yadgar drain` CLI subcommand. Backend composition point: `ensure_restoration_engines()` (called from `_ensure_recall_engines`, the drainer's `ensure_write_engines`, and `run_admin_op`) builds `_st._replay` and upgrades `_st._cognitive_map` from the session-side `SRTransitionRecorder` to the full `CognitiveMap`; the shared root no longer constructs either (the ADR-0056 composition-root waivers stay ml_client/cache-only).
- **explanation:** Live-proven motivation (task #16 successor): `restore()` on core's 1 CPU exceeded the 95s tool-offload ceiling — the SR transition-matrix build + inversion is exactly the "compute over DB rows" the semantic placement law sends to the backend's 7 CPUs next to the DB. Core keeps only the session seam (SR transition RECORDING via `SRTransitionRecorder`, census verdict #5) and thin HTTP forwarders. Forward-only: `YADGAR_EMBED_URL` unset → RuntimeError (no in-core fallback; the impl no longer exists in the core process). Import-linter stays 4 kept / 0 broken with no new waivers.

### CAP-STOR-045 — Migration 026: drop dead wiki_draft table (Fix #76, v5.157.0)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `026`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py`
- **wiring:** Applied once at server-mode startup after v5.157.0. Drops the `wiki_draft` table and any associated indexes. The three draft-workflow MCP tools (`wiki_drafts`, `wiki_approve`, `wiki_discard`) were removed from `yadgar/core/server/tools/wiki.py` in the same release (Fix #76).
- **explanation:** No production path ever created draft rows — `wiki_add` has always committed directly to `wiki_page`. The `wiki_draft` table was added in migrations 015–016 alongside a draft-workflow tool set (`wiki_drafts`, `wiki_approve`, `wiki_discard`) that was never wired into any real caller. Removing the table and tools (Fix #76) eliminates dead schema weight and the associated STALE tool surface.

### CAP-STOR-046 — Migration 027: runtime_config table (ADR-0163, Car G1)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `027`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_027_runtime_config_table`, `yadgar/_shared/storage/runtime_config.py`, `yadgar/backend/admin_exec/runtime_config.py`
- **wiring:** Applied once at server-mode startup after ADR-0163 (Car G1). Creates the `runtime_config` SCHEMALESS table with a non-unique index on `(key, directory)`. Backs the DB-backed, directory-scoped, typed runtime config store via `_RuntimeConfigMixin` (`set_config_row` / `get_config_row` / `list_config_rows` / `delete_config_row`). Write ops forward through the backend `runtime_config_set` / `runtime_config_delete` admin ops; reads stay core via `_get_storage()` (mirrors the memory_block pattern — no read admin op).
- **explanation:** Car G1 of the runtime config store replaces code_graph's env-only `CODE_GRAPH_ENABLED` flag + `.code-graph-disable` repo file with a proper DB row. A row is `{key, directory(None=global), value(JSON — bool/int/str/list/dict), created_at, updated_at}`; per-dir overrides global (resolution is Car G2's getter). Uniqueness on `(key, directory)` is enforced application-side (like `memory_block`), so the index is deliberately non-UNIQUE (a UNIQUE index over a nullable `directory` is avoided). Values round-trip typed via JSON encode-on-write / decode-on-read. Cache/resolver (G2), MCP tools + HTTP route + host client (G3), and the code_graph migration (G4) build on this storage layer.

### CAP-STOR-050 — Migration 030: mutability_override column on wiki_page (Car J, 0047 spine, D25/D26)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `wiki_set_mutability`
- **migrations:** `030`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_030_wiki_mutability_override`, `yadgar/_shared/wiki/policy.py::MUTABILITY_BY_TYPE`, `yadgar/_shared/storage/wiki.py::_enforce_mutability`, `yadgar/_shared/wiki/store.py::set_mutability_by_slug`, `yadgar/core/server/tools/wiki.py::wiki_set_mutability`, `yadgar/backend/admin_exec/wiki.py::wiki_set_mutability`
- **wiring:** Migration 030 runs once at server-mode startup, adding `DEFINE FIELD IF NOT EXISTS mutability_override ON TABLE wiki_page TYPE option<string>;` (idempotent; no backfill — pre-migration rows resolve NONE→per-type default). The storage chokepoint (`_enforce_mutability` in `_shared/storage/wiki.py`) reads `mutability_override` ∪ `MUTABILITY_BY_TYPE` and rejects non-sanctioned writes when the effective mutability is `locked` or `derived`. Per-type defaults (D26): `adr`/`adr_superseded` → `locked`, `task`/`agent_prompt*` → `free`, `wiki_rollup` → `derived`. The `wiki_set_mutability(slug, value, reason)` MCP tool (power-gated) is the sole sanctioned override path — it writes `mutability_override` via `WikiStore.set_mutability_by_slug` (all-rows pattern). A `_sanctioned=True` kwarg on `update_wiki_page`/`delete_wiki_page` lets the Car G supersede retype + Car K nightly sweep bypass the gate without deadlocking the lifecycle.
- **explanation:** D25 closes the well-intentioned-repair vector (rewriting a derived rollup, stripping an ADR's superseded tag) and the dangling-pointer vector (deleting a locked page mutability). Per-page `mutability_override` is the privilege-escalation surface that prevents the gate from being a permanent wall — the security model is "locked by default, override with reason logged". The `_sanctioned` seam is the lifecycle escape hatch (a locked ADR can still be SUPERSEDED by the retype op, can still be subject to a nightly archive sweep); without it the guard deadlocks its own lifecycle.

### CAP-STOR-053 — Migration 032: retire wiki_page_version.branch and the seeding kwargs (Car C12, ADR-0226)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `032`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_032_drop_wiki_page_version_branch`, `yadgar/_shared/storage/migrations.py::Migration032Abort`, `yadgar/_shared/storage/wiki.py::insert_wiki_page`, `yadgar/_shared/storage/wiki.py::update_wiki_page`, `yadgar/_shared/storage/wiki.py::set_wiki_page_metadata`, `yadgar/_shared/storage/wiki.py::insert_wiki_page_version`, `yadgar/_shared/storage/memory.py::insert_memory`, `yadgar/_shared/storage/memory.py::_build_memory_insert_clause`, `yadgar/backend/restoration/checkpoint_restore.py::CheckpointRestore.anchor_memory`, `yadgar/backend/causal_discovery/pc.py::_fetch_filtered_episodes`
- **wiring:** Runs once at server-mode startup, between 031 and 033. Three ordered statements against `wiki_page_version`: `SELECT count() … WHERE branch != NONE`, then (only when that count is non-zero) `UPDATE wiki_page_version SET branch = NONE WHERE branch != NONE`, then a re-count that raises `Migration032Abort` if any value survived, then `REMOVE FIELD IF EXISTS branch ON TABLE wiki_page_version`. Rows are NULLED, never deleted — the version row is the audit trail; only the retired concept leaves. Replay is a no-op: the count is 0 so the UPDATE is skipped and `REMOVE FIELD IF EXISTS` does nothing. The migration is HALF the capability; the other half is the writer kill, in the same car — the `ver_branch` binding is gone from all three `wiki_page_version` snapshot paths in `_shared/storage/wiki.py`, from `insert_wiki_page_version`'s `snapshot.get("branch")`, and from `_migration_013_wiki_page_version`'s seeder (dict key + both the server-mode LET preamble and the embedded SQL, which must move together or `$branch` is left unbound); the seeding kwargs `insert_memory(branch=)`, `insert_wiki_page(branch=)` and `anchor_memory(branch=)` are deleted, `checkpoint_restore.anchor_memory` being their one live non-test caller.
- **explanation:** ADR-0215 removed branch scoping and migration 029 dropped the column from `memory` + `wiki_page`, but the implementing train left two deliberate survivors, both recorded as "do not tidy": `wiki_page_version.branch` (Car 9, as an audit-trail snapshot, with a boundary test asserting 029 leaves it alone) and the low-level seeding kwargs (Car 10, because removing them turned ~33 tests red). ADR-0226 revokes both — a history table holding a retired column is a second source of truth, and the boundary test actively pinned the survivor in place. **The data step is the substance, not the DDL.** `wiki_page_version` is SCHEMALESS and migration 013 never issued a `DEFINE FIELD` for `branch`, so on most databases there is no field definition to remove and a body consisting only of `REMOVE FIELD IF EXISTS` would be a no-op that still passes an `INFO FOR TABLE` assertion — 031's dead-filter shape in different clothing. This was verified empirically before the real body was written: with a `REMOVE FIELD`-only body the e2e row-level assertion fails and the field-definition assertion passes. **Killing the writers is the actual safety property, not the schema statement:** because every table involved is SCHEMALESS, a surviving writer re-creates the column untyped while `INFO FOR TABLE` still reports clean. Two of the killed writers were live re-creation paths for a column 029 had *already* dropped — `insert_wiki_page(branch=)` appended `branch = $branch` to the `wiki_page` SET clause and `insert_memory(branch=)` did the same on `memory` — which is precisely ADR-0226's "the kwargs … are in fact the exact mechanism by which the dropped column comes back". Both boundary tests were REWRITTEN rather than deleted, per ADR-0226: 029's blast radius is unchanged and still asserted, with new siblings asserting that 032 completes it and that no writer puts the column back. Survivors deliberately NOT swept: `default_branch` (git's real default branch — code-graph indexing, PR bases, merge-base resolution) and `branch_labels` (Alembic's own required module-level variable in `sql/migrations/versions/*.py`; sweeping it breaks the migration chain).

---

### CAP-STOR-054 — Migration 034 + `stamp_project_id`: the graph tables get an identity (Car 1, ledger tasks 309 + 89)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `034`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_034_project_id_graph_tables`, `yadgar/_shared/storage/migrations.py::_CAR1_GRAPH_PROJECT_ID_TABLES`, `yadgar/backend/admin_exec/identity_stamp.py::stamp_project_id`, `yadgar/backend/admin_exec/identity_stamp.py::_preflight_write_guards`, `yadgar/backend/admin_exec/identity_stamp.py::_WRITE_PATH_GUARDS`, `yadgar/_shared/storage/_project_id_writer.py::resolve_project_id_from_rows`, `yadgar/core/cli/backfill.py::_run_stamp_identity`
- **wiring:** Two halves. The migration runs once at server-mode startup after 033 and issues exactly two statements per table — `DEFINE FIELD IF NOT EXISTS project_id ON TABLE <t> TYPE option<string>` and `DEFINE INDEX IF NOT EXISTS <t>_project_id_idx ON TABLE <t> FIELDS project_id` — for `entity`, `relationship`, `memory_cluster`. **No data step.** The data half is the `stamp_project_id` admin op, reachable over `POST /admin` and from `yadgar backfill --stamp-identity` (dry-run by default; `--apply` writes, `--mapping-file` supplies operator overrides). The op scans six tables (the three above plus `checkpoint`, `memory_block`, `episode`), classifies every row into stamped / cross_project / undecidable-with-a-named-reason, and returns the manifest. `--apply` replays the manifest's exact ids in chunked `UPDATE <t> SET project_id = $pid WHERE meta::id(id) IN $ids` statements, so it cannot widen the reviewed set.
- **explanation:** C11's criterion for migration 033 was "carries a legacy `directory` / `directory_context` column"; these three carry neither, because their owner is inherited from the rows that produced them. So 033 never reached them and they had no `project_id` declaration at all — and all three are SCHEMALESS, so the backfill's `UPDATE` would have created the column UNTYPED: invisible to `INFO FOR TABLE` review, unindexed, and table-scanning 5,560 relationship rows under the scope predicate every reader is about to move onto. Measured live 2026-08-21: `entity` 2052/2052 unstamped, `relationship` 5560/5560, `memory_cluster` 3175/3175, `checkpoint` 157/160, `memory_block` 50/52, `episode` 3/3 — 10,997 rows with no identity, all of which go invisible when `checkpoint_restore` flips onto `project_id`. **The op inherits, it does not derive.** Every stamp traces through `resolve_project_id_from_rows` to a row a host-resolved, operator-reviewed backfill already adjudicated, which is why it does not repeat migration 031's ADR-0227 failure (a container with no git inventing `local/<basename>`). `entity.name = 'memory:N'` inherits that memory's owner (1789 of 2052 live rows, every pointer live); a content-extracted entity such as `ValidationError` is held UNDECIDABLE forever, because `insert_entity` is preceded by a global `get_entity_by_name` and the row is one object every project reinforces — a single owner would not be ambiguous but wrong. A relationship takes its endpoints' shared owner or nothing; a cluster takes its member memories'. `checkpoint` / `memory_block` / `episode` key through a `directory → project_id` map built from the already-stamped corpus, with the reach markers (`global`, `system`, `unresolved`, `""` — never owners, ADR-0227) and the genuinely-conflicted directories kept in SEPARATE buckets, because they take different operator actions. Dry-run parity follows Car 19 / ledger task 176: `_WRITE_PATH_GUARDS` names `assert_project_registered` (the gate that runs inside the ledger write; the standalone `admin_exec` guard Car 5 proved had no call site was deleted by task 384) — invoked with `refresh=False` on both paths so the preview reaches the apply's verdict without the apply's `last_validated_at` write and `_preflight_write_guards` runs it over EVERY derived target on the preview as well as the apply, treating an absent handle and an absent method as errors rather than clean previews. The op also censuses ledger task 89's dangling relationships — 487 live (403 `derived_from` + 84 `co_occurrence`, 0 `caused_by`), reported with ids and deliberately NOT repaired, since deleting them is a separate destructive decision.

---

### CAP-STOR-052 — Migration 033: project_id on the other nine directory-bearing tables (Car C11, 0047 PR#40 §5)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `033`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_033_project_id_other_tables`, `yadgar/_shared/storage/migrations.py::_C11_PROJECT_ID_TABLES`, `yadgar/_shared/storage/blocks.py::create_block`, `yadgar/_shared/storage/blocks.py::list_blocks`, `yadgar/_shared/storage/episode.py::insert_episode`, `yadgar/_shared/storage/queue.py::insert_action_log`, `yadgar/_shared/storage/ops.py::insert_checkpoint`, `yadgar/_shared/storage/ops.py::get_active_checkpoint`, `yadgar/backend/restoration/checkpoint_restore.py::CheckpointRestore.restore`
- **wiring:** Runs once at server-mode startup, after 031 and after C12's 032 (CAP-STOR-053). Issues exactly two statements per table across nine tables — `DEFINE FIELD IF NOT EXISTS project_id ON TABLE <t> TYPE option<string>` and `DEFINE INDEX IF NOT EXISTS <t>_project_id_idx ON TABLE <t> FIELDS project_id` — for `memory_block`, `episode`, `action_log`, `runtime_config` (the legacy `directory` COLUMN owners) and `checkpoint`, `narrative_entry`, `user_profile`, `derived_belief`, `wiki_page_version` (the SCHEMALESS `directory_context` users no `DEFINE FIELD` covered). **No data step, no `SELECT`, no derivation.** The writers are re-keyed in the same car and DUAL-WRITE: `create_block`, `insert_episode`, `insert_checkpoint` stamp `project_id` and keep writing the legacy column; `insert_action_log` (C4) and `insert_profile` / `BeliefRecord` (C13f) already stamped and only needed the declaration. The two `restore()` sinks C10g left path-keyed (`get_active_checkpoint`, `list_blocks`) move onto a two-arm predicate — `project_id = $pid OR <legacy> = $dir` — spelled out at the site rather than via `build_project_scope_clause`, whose reach arm (`'global' IN tags`) names a `tags` column neither table has.
- **explanation:** ADR-0225's carve-out 2 names only `directory_context`; the survey found these tables each carrying their own scoping column with no `project_id` to move onto, which is why C9a/C9c/C10/C10g/C13f each deferred a signature here. The migration is deliberately schema-only: 031's phase filter was dead code because it filtered on a column it never projected, and a migration with no row-touching statement cannot repeat that shape — the replay proof is therefore structural rather than behavioural. The legacy columns are NOT dropped and their writers are NOT silenced, because the backfill derives from them (a row with `project_id` but no `directory` would be unattributable in both directions) and three live consumers read them today (`causal_discovery/pc.py`, `consolidation/cls.py`, `consolidation/cleanup.py`). The ADR-0225/0226 "killing the writers is the safety property" warning applies to the NEXT PR's drop: every table here is SCHEMALESS, so `REMOVE FIELD` removes only the type definition while a surviving writer re-creates the column untyped and `INFO FOR TABLE` still looks clean. `project_backfill._TABLES` is `("memory", "wiki_page")` and §8 names no backfill step for these nine, so a `project_id`-only read predicate would not be a degraded window but permanent silent loss of the historical corpus — hence the transitional legacy arm on both restore sinks. The plan's fourth table `queue` does not exist (the cited line is inside `insert_action_log`'s docstring; the queue is file-backed) and no column was declared on it; `runtime_config` — the gap three cars reported upward with no owner — takes its place.

### CAP-STOR-051 — Migration 031: project_id + legacy_directory on wiki_page + memory (Car L, 0047 spine, D32 ①)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `031`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_031_project_id_backfill`, `yadgar/_shared/storage/migrations.py::_classify_directory_for_migration`, `yadgar/_shared/storage/wiki.py::_resolve_project_id_for_wiki_write`, `yadgar/_shared/storage/memory.py::_resolve_project_id_for_memory_write`, `yadgar/backend/admin_exec/reslug.py::reslug_adr_pages`, `yadgar/backend/admin_exec/__init__.py::_ADMIN_OPS["reslug"]`, `yadgar/backend/consolidation/cleanup.py::_resolve_project_id_for_write`, `yadgar/backend/queue_drainer/apply.py::_apply_inner` (wiki_add branch), `yadgar/core/identity.py::derive_project_id`
- **wiring:** Migration 031 runs once at server-mode startup. (1) `DEFINE FIELD IF NOT EXISTS project_id ON TABLE wiki_page TYPE option<string>` and the same on `memory`; (2) `DEFINE FIELD IF NOT EXISTS legacy_directory` on both — the quarantine marker for rows whose `directory_context` no longer maps to a live project; (3) per-row backfill: `SELECT id, directory_context FROM wiki_page` (then `memory`) → Python-side classify via `_classify_directory_for_migration` (the inspector seam; failure returns `'unresolved'` so the boot never blocks on a path-resolution error) → `UPDATE ... SET project_id = $project_id [, legacy_directory = $legacy_directory]` per row (skipped when `project_id IS NOT NONE` — idempotent on a second run); (4) `DEFINE INDEX IF NOT EXISTS wiki_page_project_id_idx` + `memory_project_id_idx`. Live write paths stamp `project_id` alongside `directory_context` via lazy `derive_project_id` resolves: `cleanup._try_store_action_summary` (action-log summarizer), `apply._apply_inner` (wiki_add replay branch), `insert_wiki_page`, `insert_memory`. The `reslug` admin op (D32 ③) is the one-shot rollout companion — re-slugs ADR pages from `yadgar-adr-NNNN` to `{project_id}_adr-NNNN` (slash → underscore), rewrites inline `[[old-slug]]` body links, updates `wiki_crossref.from_slug`/`to_slug`, and stamps `adr.body_slug` via `MariaStorageEngine.set_adr_body_slug` (sync bridge via `asyncio.run` — acceptable for a one-shot admin op, and the same shape that made the deleted `admin_exec` registry guard unusable on a hot write path). `README.md` includes the dry-run → confirm → apply flow because the op is non-reversible by hand (the inverse is another `reslug` call with the previous project_id, which the audit trail in the manifest records).
- **explanation:** D32 ① closes the one-way backfill trapdoor: post-migration writes MUST stamp `project_id` or they leave rows backfill-incomplete. The classifier is the seam between raw `directory_context` (a filesystem path) and `project_id` (a stable identifier) — the migration runs at boot so the lazy import is amortized across the whole corpus. The quarantine column (`legacy_directory`) holds rows the classifier could not resolve (path gone, no remote, git error) — re-running the migration with a fixed classifier is a no-op via the `project_id IS NOT NONE` skip; an operator-driven re-classify uses `update_memory_fields` (allowed by the new `_MEMORY_UPDATABLE_FIELDS` entries). The `directory_context` column is NOT dropped — Car M flips the readers onto `project_id`, then a later migration drops the directory column after Car M is confirmed green. The live-write stamp is the second half of the trapdoor: any path that writes to `wiki_page` or `memory` MUST also stamp `project_id`, or the migration is incomplete and the column has rows it can never classify. The `reslug` op is destructive (rewrites slugs in-place) but idempotent (the discovery query `WHERE slug STARTSWITH 'yadgar-adr-'` only matches the old format, so a second run is a no-op).

### CAP-STOR-049 — Migration 029: retire branch scoping — null the data, drop the column (ADR-0215, Car 9)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `029`
- **bc:** —
- **refs:** `yadgar/_shared/storage/migrations.py::_migration_029_drop_branch_column`, `yadgar/_shared/wiki/store.py`, `yadgar/_shared/wiki/contract.py`, `yadgar/_shared/storage/client.py`, `yadgar/core/export/schema.py`, `yadgar/core/export/views.sql`, `yadgar/backend/write_exec/wiki_add_impl.py`, `yadgar/backend/admin_exec/wiki.py`
- **wiring:** Applied once at server-mode startup, at the tail of the migration registry. Runs as one ordered unit, each step aborting with `Migration029Abort` rather than continuing on a bad state: (1/2) `DELETE memory WHERE branch != NONE AND branch != 'master' AND branch != 'main' AND is_protected = false`, guarded ahead of the write by a 300-row circuit breaker and followed by an assert that the PROTECTED branch-scoped count is unchanged; (3) collapse the one reviewed `(slug, directory_context)` collision — `aws-org-migration-terraform-automation` @ `/home/max/aws-work`, keep `wiki_page:6706`, delete `wiki_page:6705` — **by record id**, since both rows share the slug and `delete_wiki_page` would additionally strip the survivor's slug-keyed `wiki_crossref` rows; (4/5) `UPDATE ... SET branch = NONE` on `wiki_page` then `memory`; then assert both tables hold exactly one branch group BEFORE (6) `REMOVE FIELD IF EXISTS branch` on both. Migrations 004 and 015 are untouched (026's docstring sets the immutability precedent) and `wiki_page_version.branch` is deliberately out of scope as an audit-trail snapshot. Code-side in the same car: `WikiAddOptions.branch`, `WikiStore._METADATA_FIELDS["branch"]`, `_MEMORY_UPDATABLE_FIELDS["branch"]`, both `Column("branch", ...)` export entries and the `v_branch_distribution` DuckDB view are all removed.
- **explanation:** ADR-0215's ordering hazard is that readers must retire before values are nulled and values before the column is dropped — reversed, reads break mid-train. Cars 1-3 retired the five read-path filters; this migration performs the data steps and the structural drop atomically so no deploy window exists in which the column is gone but the values are not (or vice versa). **The column drop is NOT the safety property.** Both tables are `SCHEMALESS`, so `REMOVE FIELD` removes only the `option<string>` type definition: it does not delete stored values (hence the explicit nulling steps) and it does not stop a future write from re-creating `branch` as an untyped field — `INFO FOR TABLE` would still look clean while the data went dirty. Killing the writers is what actually retires the column, which is why the code-side removals ship in the same car rather than as later hygiene. Two deviations from the plan, both user-approved: branch-scoped `wiki_page` rows are **nulled, not deleted** (the memory side has `is_protected` to separate durable knowledge from branch litter; the wiki side has no equivalent, so a blanket delete would destroy exactly the durable project knowledge ADR-0215 exists to make reachable), and the collision pair — whose rows already carry `branch = null`, making it a pre-existing `LIMIT 1` non-determinism bug unrelated to branch scoping — is resolved here because `get_wiki_page_by_slug_directory` otherwise returns an arbitrary row for that slug forever, silently.

### CAP-STOR-047 — Runtime config MCP tools + HTTP read/write routes + host client (ADR-0163, Car G3+G5)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `config_get`, `config_list`, `config_set`, `config_delete`
- **migrations:** —
- **bc:** `BC-CONFIG-1`, `BC-CONFIG-2`, `BC-CONFIG-3`
- **refs:** `yadgar/core/server/tools/runtime_config.py::config_get`, `yadgar/core/server/tools/runtime_config.py::config_list`, `yadgar/core/server/tools/runtime_config.py::config_set`, `yadgar/core/server/tools/runtime_config.py::config_delete`, `yadgar/core/server/tools/runtime_config.py::_apply_config_set`, `yadgar/core/server/tools/runtime_config.py::_apply_config_delete`, `yadgar/core/server/http.py::api_runtime_config`, `yadgar/core/server/http.py::api_runtime_config_set`, `yadgar/core/server/http.py::api_runtime_config_delete`, `yadgar/core/runtime_config_client.py`
- **wiring:** Four `@_tool`-registered tools in `runtime_config.py`. Reads (`config_get`, `config_list`) are `@_tool(power=False)` and stay core via the G2 resolver (`_runtime_config.config_get`) / `_get_storage().list_config_rows` (no read admin op, matching `memory_block`). Writes (`config_set`, `config_delete`) are `@_tool(power=True)` and delegate to the plain `_apply_config_set`/`_apply_config_delete` helpers: validate scope (`global`|`project`; `project` requires `directory`) + JSON-serializable value type (bool/int/str/list/dict), map scope→directory (`global`→`None`, `project`→given dir), forward to the backend `runtime_config_set`/`runtime_config_delete` admin ops via `_forward_admin`, then whole-flush the resolver cache via `invalidate_config_cache`. None are `always_load` (ADR-0047 — config reads are not session-critical). HTTP routes (all under the `/api/` bearer-protected prefix): `GET /api/runtime-config/{key}?directory=…` (`api_runtime_config`) resolves the value core-side (fail-safe → `{"value": null}` on DB-down, not a 5xx); `POST /api/runtime-config/{key}` (`api_runtime_config_set`, body `{value, scope, directory}`) and `DELETE /api/runtime-config/{key}?scope=…&directory=…` (`api_runtime_config_delete`) call the SAME `_apply_config_set`/`_apply_config_delete` helpers as the tools (tool + route cannot drift — mirrors how the GET route reuses the plain resolver), mapping a `{ok: False}` validation result → 400 and success → 200. The host client `runtime_config_client` (stdlib `urllib` only — runs on the host, outside the container) provides `get(key, directory, default)` — fail-OPEN (daemon-down / timeout / non-200 / malformed JSON / null → `default`, NEVER raises; the stop-hook opt-out depends on it) — and `set(key, value, *, scope, directory)` / `delete(key, *, scope, directory)` — NOT fail-open (any failure → `False` so the caller can report "couldn't enable"). All carry the optional `Bearer YADGAR_MCP_AUTH_TOKEN` and a 2s timeout; a caught `HTTPError` is closed deterministically (py3.14 `tempfile`-wrapper ResourceWarning guard).
- **explanation:** Car G3 exposes the runtime config store to (a) in-session model tools, (b) the viz/debug HTTP surface, and (c) host-side hook scripts + the `yadgar` CLI; Car G5 adds the host WRITE path (POST/DELETE route + `set`/`delete` client) so `yadgar setup` can PERSIST an enable. The fail-open host READ client is the contract the stop-hook opt-out depends on — a dead daemon must never crash the hook (mirrors `session-start-context.py`); the WRITE client is deliberately NOT fail-open so `setup` can distinguish "enabled" from "daemon down — enable it later". Car G4 migrated code_graph's `is_enabled`/opt-out to `runtime_config_client.get("code_graph.enabled", dir, default=True)` (flipped from `default=False` 2026-07-27 once the digest PII leak was fixed and the pilot proved out — see ADR-0163 addendum) and Car G5 wired its `setup` to `runtime_config_client.set("code_graph.enabled", true, scope=global)`.

### CAP-WIKI-001 — Wiki similarity gate (duplicate prevention)
- **status:** LIVE
- **category:** wiki
- **settings:** `WIKI_SIM_GATE_ENABLED`, `WIKI_SIM_MODE`, `WIKI_SIM_CONTENT_THRESHOLD`, `WIKI_SIM_TITLE_THRESHOLD`, `WIKI_SIM_TOP_K`
- **tools:** `wiki_add`, `wiki_check_duplicate`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::_check_similarity_gate`, `yadgar/core/server/tools/wiki.py::wiki_add`
- **wiring:** MCP caller → `wiki_add()` → (async path) enqueued job → `QueueDrainer._apply()` → similarity gate runs in drainer pre-apply stage via `_sim_gate_for_drainer`. For `wait=True` callers, result surfaces synchronously via `wait_for_job()` + `get_job_result()`. `wiki_check_duplicate` exposes the gate as a read-only dry-run (no write). Gate is bypassed when `force=True`, `replace_slug` is set, or `append=True`.
- **explanation:** Prevents near-duplicate wiki pages by comparing the candidate content + title against existing pages using embedding cosine similarity. Configured via `WIKI_SIM_CONTENT_THRESHOLD` (default 0.80) and `WIKI_SIM_TOP_K` (default 5 candidates). In `hard` mode (default `WIKI_SIM_MODE`), a match causes the write to be rejected with a `duplicate_detected` reason. In `soft` mode, the match is logged but the write is allowed. As of v5.41.5 the gate runs in the drainer (not the request thread) to satisfy the I9 latency budget; `wait=False` callers receive a deferred check, `wait=True` callers receive synchronous rejection.

### CAP-WIKI-002 — Wiki write-wait path (read-your-writes via wait=True)
- **status:** LIVE
- **category:** wiki
- **settings:** `WIKI_WRITE_WAIT_TIMEOUT_SECONDS`
- **tools:** `wiki_add`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::_wiki_add_wait_path`
- **wiring:** `wiki_add(wait=True)` → `_wiki_add_wait_path()` → enqueues job to `FileQueue`, then calls `QueueDrainer.wait_for_job(job_id, timeout=WIKI_WRITE_WAIT_TIMEOUT_SECONDS)` → after completion, retrieves rejection via `get_job_result()` → returns synchronous result. Falls back to sync write if no drainer is running or `replace_slug` is set.
- **explanation:** Provides a read-your-writes guarantee for `wiki_add` callers who need to know immediately whether their write committed or was rejected by the similarity gate. The caller blocks for up to `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` (default 5.0 s) while the drainer processes the job in FIFO order. On timeout, returns `{stored: False, reason: "wait_timeout"}` with the job still queued. This is an opt-in slow path — the default async path (`wait=False`) returns immediately.

### CAP-WIKI-003 — Wiki CRUD tool surface (add/read/list/delete/query/lint/drafts/approve/discard)
- **status:** LIVE
- **category:** wiki
- **settings:** `WIKI_SLUG_PREFIX`, `WIKI_EMBED_FAILURE_BLOCKS_WRITE`
- **tools:** `wiki_add`, `wiki_read`, `wiki_list`, `wiki_delete`, `wiki_query`, `wiki_lint`, `wiki_autolink`, `wiki_check_duplicate`, `wiki_get`, `wiki_update`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::wiki_read`, `yadgar/core/server/tools/wiki.py::wiki_list`, `yadgar/core/server/tools/wiki.py::wiki_delete`, `yadgar/core/server/tools/wiki.py::wiki_query`, `yadgar/core/server/tools/wiki.py::wiki_lint`, `yadgar/core/server/tools/wiki.py::wiki_autolink`, `yadgar/core/server/tools/admin_other.py::wiki_get`, `yadgar/core/server/tools/admin_other.py::wiki_update`
- **wiring:** All tools are `@_tool()`-registered and reachable directly via MCP. `wiki_add` enqueues to `FileQueue` (async) or uses sync write path. `wiki_read`, `wiki_list`, `wiki_delete`, `wiki_get` delegate to `_st._wiki` (WikiStore). `wiki_query` performs keyword+semantic search with §25 directory filtering. `wiki_lint` calls `_st._wiki.lint()`. `wiki_autolink` calls `_st._wiki.autolink()` (dry-run by default; on apply it re-passes each page's own metadata to avoid clobber). `wiki_update` delegates to `_st._storage.update_wiki_page()`. `WIKI_SLUG_PREFIX` is injected into the `FileQueue` wiki mirror path at lifecycle init. `WIKI_EMBED_FAILURE_BLOCKS_WRITE` controls whether an embedding failure causes the write to be blocked.
- **explanation:** The core wiki management surface. `wiki_add` creates or upserts pages (async-queued by default, with similarity gate and secret gate). `wiki_read` resolves a slug using §25 directory resolution. `wiki_list` returns metadata-only page listings scoped to a directory. `wiki_query` performs combined FTS + semantic search with directory-scoped filtering. `wiki_lint` identifies orphan pages, broken cross-references, and stale/low-confidence pages. `wiki_autolink` scans page bodies for verbatim mentions of other pages' titles and inserts `[[slug]]` cross-refs (dry-run default; verbatim/length/similarity/idempotency guards). `wiki_get`/`wiki_update` provide integer-ID-based fetch and field-patch access.

### CAP-WIKI-004 — Wiki versioning and history (history/read_version/diff/restore)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::wiki_history`, `yadgar/core/server/tools/wiki.py::wiki_read_version`, `yadgar/core/server/tools/wiki.py::wiki_diff`, `yadgar/core/server/tools/wiki.py::wiki_restore`, `yadgar/core/server/tools/wiki.py::_resolve_page_id_by_slug`
- **wiring:** All tools call `_resolve_page_id_by_slug(slug, directory)` which applies §25 directory resolution, then delegate to `_st._wiki.history()`, `.read_version()`, `.diff()`, `.restore_version()` respectively. These operations write synchronously (no async queue). `wiki_restore` creates a new version (N+1) and bypasses the similarity gate.
- **explanation:** Full version-control surface for wiki pages introduced in v5.41.0. `wiki_history` lists versions (metadata only) newest-first. `wiki_read_version` fetches the full snapshot for a specific version number. `wiki_diff` produces a unified-text or structured JSON diff between two version numbers. `wiki_restore` rolls a page back to a historical version by creating a new version whose content matches the target — it preserves intervening history and bypasses the similarity gate (explicit user intent). Note: `wiki_history` may show stale data immediately after `wiki_add(wait=False)` since the write is async; use `wait=True` for read-your-writes consistency.

### CAP-WIKI-005 — Wiki surgical edit surface (section-atomic, anchor-text, positional, structural)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_append_section`, `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_delete_at`, `wiki_insert_at`, `wiki_replace_markdown_block`, `wiki_set_metadata`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::wiki_append_section`, `yadgar/core/server/tools/wiki.py::wiki_replace_text`, `yadgar/core/server/tools/wiki.py::wiki_delete_text`, `yadgar/core/server/tools/wiki.py::wiki_insert_after`, `yadgar/core/server/tools/wiki.py::wiki_insert_before`, `yadgar/core/server/tools/wiki.py::wiki_replace_at`, `yadgar/core/server/tools/wiki.py::wiki_delete_at`, `yadgar/core/server/tools/wiki.py::wiki_insert_at`, `yadgar/core/server/tools/wiki.py::wiki_replace_markdown_block`, `yadgar/core/server/tools/wiki.py::wiki_set_metadata`, `yadgar/_shared/wiki/store.py::WikiStore.set_metadata_by_slug`, `yadgar/_shared/storage/wiki.py::_WikiMixin.get_wiki_page_ids_by_slug`
- **wiring:** All tools call `_resolve_page_id_by_slug(slug, directory)` then delegate to corresponding `_st._wiki` methods. All bypass the v5.39 similarity gate. Write-bearing tools (`wiki_replace_text`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_insert_at`, `wiki_replace_markdown_block`, `wiki_append_section`) run `gate_or_reject()` (I26 secret gate). Deletion tools (`wiki_delete_text`, `wiki_delete_at`) do not run secret gate (nothing new written). `wiki_set_metadata` (BC-G10 fix): calls `WikiStore.set_metadata_by_slug(slug, field, value)` which uses `storage.get_wiki_page_ids_by_slug(slug)` (NO LIMIT — returns ALL page_ids for the slug across all branches and global stragglers) then applies `set_metadata(page_id, ...)` per row.
- **explanation:** Layer 1–4 surgical edit primitives introduced in v5.61.0 to prevent whole-page replacement errors. Layer 1 (anchor-text): `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_after`, `wiki_insert_before` locate content by unique text strings. Layer 2 (positional): `wiki_replace_at`, `wiki_delete_at`, `wiki_insert_at` operate by line/col coordinates with a mandatory `anchor_hint` (≥20 chars) to guard against off-by-one errors. Layer 3 (structural): `wiki_replace_markdown_block` addresses the Nth block of a markdown block type (paragraph, heading, code_fence, etc.). Section-atomic: `wiki_append_section` patches a named section (by heading) without touching the rest of the document — this was introduced specifically to prevent the 2026-05-31 corruption pattern. `wiki_set_metadata` is the Layer 4 metadata primitive for repositioning pages across branches/directories; fixed in BC-G10 to reach ALL rows for a slug instead of only the single row returned by §25 LIMIT 1 resolution. Ledger task 246 widened its `field` set from `directory_context` alone to `{project_id, directory_context}`: ADR-0233 made `project_id` the sole scoping key, and this is the only path that restamps it on an existing page (`wiki_add` with `replace_slug` / `force` / `upsert` updates the row without restamping it). `directory_context` is retained as legacy. Note the metadata write reaches `set_wiki_page_metadata` directly rather than `update_wiki_page`, so it does NOT pass Car J's `enforce_mutability` gate — a `page_type='adr'` page (effective mutability `locked`) is restampable, which is what makes the corpus re-key runnable over the ADR cohort.

### CAP-WIKI-006 — Wiki §25 directory scoping and resolution
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_read`, `wiki_query`, `wiki_list`, `wiki_add`, `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`, `wiki_append_section`, `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_delete_at`, `wiki_insert_at`, `wiki_replace_markdown_block`, `wiki_set_metadata`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::_resolve_page_id_by_slug`, `yadgar/core/server/tools/wiki.py::wiki_read`
- **wiring:** All read/write tools that accept a `directory` parameter call `_resolve_page_id_by_slug(slug, directory)` or `_st._wiki.read_by_directory()` for §25 resolution.
- **explanation:** The §25 directory scoping system ensures wiki pages are correctly scoped to the project (`directory_context`) they were written for. Resolution order for reads: (1) caller-dir, (2) 'global', (3) not-found. For writes, scope is validated unconditionally at the MCP boundary (the enforcement knob was deleted by C5 / ADR-0227). This prevents cross-project wiki leakage. A leading resolution step keyed on the caller's git branch, its write-side enforcement gate, and the `branch_hint` parameter added in v5.42.3–v5.42.6 for container/CI scenarios were all retired by ADR-0215: measured, that axis hid 78% of the corpus from any non-default branch while isolating 0.6% of rows.

### CAP-WIKI-007 — Stale-wiki-count signal
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/project.py::_scan_stale_wiki_slugs`
- **wiring:** The stale-count signals path (`_scan_stale_wiki_slugs` → `_compute_stale_wiki_count`) runs host-side; the count is TTL-cached and surfaced in `project_brief(mode='signals')`. No MCP tool is exposed for detection.
- **explanation:** Stale-page detection (`_scan_stale_wiki_slugs`, TTL-cached, 300 s default) surfaces a `stale_wiki_count` signal in `project_brief(mode='signals')` but does not expose a separate MCP tool — per ADR-0157, host-source operations that require filesystem access (`.local-review/wiki/*.md` hash scan) are CLI-only; `wiki_refresh_stale` was removed in #83 Car C. This capability previously also owned a branch-cleanup tool that garbage-collected pages scoped to merged git branches; ADR-0215 removed branch scoping, so the tool became meaningless and was deleted (ADR-0215 Car 6).

### CAP-WIKI-008 — Curation similarity threshold (near-duplicate merge gate)
- **status:** LIVE
- **category:** curation
- **settings:** `CURATION_SIMILARITY_THRESHOLD`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CU3`
- **refs:** `yadgar/backend/curation/__init__.py::MemoryCurator.curate_on_remember`, `yadgar/backend/curation/ingestion.py::find_similar_memories`
- **wiring:** `memorize()` → `MemoryCurator.curate_on_remember()` → `find_similar_memories()` (cosine search) → for any pair with similarity ≥ `CURATION_SIMILARITY_THRESHOLD` AND textual Jaccard > 0.5, existing memory is merged via `merge_memory()`. Also used by `cls_store/promotion.py` for cluster promotion decisions.
- **explanation:** Controls the cosine similarity threshold above which two memories with sufficient textual overlap are merged (deduplicated) rather than stored as separate records. Default 0.95 (near-exact duplicates only). The merge operation keeps the highest-heat memory, combining tags and updating the embedding. This prevents accumulating semantically-identical records across sessions. Lower values cause more aggressive merging; the textual-overlap guard prevents merging memories that score high on embeddings but carry genuinely different information (e.g. two functions with similar names).

### CAP-WIKI-009 — Curation prune passes (BC-CU1, BC-CU2)
- **status:** LIVE
- **category:** curation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CU1`, `BC-CU2`
- **refs:** `yadgar/backend/curation/prune_passes.py`, `yadgar/backend/curation/__init__.py`
- **wiring:** Called from `MemoryCurator` during the consolidation cycle (`consolidate_now` → memify self-improvement → `_memify_prune()`). The co-occurrence strengthen pass (`_memify_strengthen`) and recency prune gate (`_memify_prune`) run as part of the nightly or on-demand consolidation cycle.
- **explanation:** BC-CU1 covers the co-occurrence memify pass: memories that co-occur frequently are strengthened (heat boost) and stamped with the originating directory context (v5.64). BC-CU2 covers the recency prune gate (v5.66): very old, cold memories below a recency threshold are pruned during the consolidation cycle. Both passes are components of the `MemoryCurator` self-improvement cycle alongside the derive and reweight passes.

### CAP-WIKI-010 — Memory block CRUD (block_create/get/update/delete/list/replace/append)
- **status:** LIVE
- **category:** storage
- **settings:** `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT`, `MEMORY_BLOCK_HARD_CHAR_LIMIT`, `MEMORY_BLOCK_MAX_PER_SCOPE`, `MEMORY_BLOCK_TOTAL_BUDGET_CHARS`
- **tools:** `block_create`, `block_get`, `block_update`, `block_delete`, `block_list`, `block_replace`, `block_append`
- **migrations:** —
- **bc:** `BC-IC1`, `BC-IC2`, `BC-IC3`, `BC-IC4`
- **refs:** `yadgar/core/server/tools/blocks.py::block_create`, `yadgar/core/server/tools/blocks.py::block_get`, `yadgar/core/server/tools/blocks.py::block_update`, `yadgar/core/server/tools/blocks.py::block_delete`, `yadgar/core/server/tools/blocks.py::block_list`, `yadgar/core/server/tools/blocks.py::block_replace`, `yadgar/core/server/tools/blocks.py::block_append`, `yadgar/backend/admin_exec/blocks.py`
- **wiring:** All tools are `@_tool(power=True)`-registered. `scope='project'` requires a non-empty `directory` parameter (enforced core-side via `_require_directory_for_project_scope`). R3 Car 3a (R5 forward): the READ tools (`block_get`, `block_list`) delegate to `_get_storage()._BlocksMixin` in core, but the WRITE tools (`block_create`/`block_update`/`block_delete`/`block_replace`/`block_append`) keep the directory-guard + secret gate (I26) in core and forward the storage write to the backend `POST /admin` endpoint via `_forward_admin` (op-name = tool-name); the backend `admin_exec.blocks` impls run the `_BlocksMixin` write (forward-only — core touches zero DB directly). `block_create` initialises char_limit from `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT` (default 2000) when not specified; `MEMORY_BLOCK_HARD_CHAR_LIMIT` (default 8000) is the absolute ceiling. `bootstrap_project` auto-seeds default blocks (`current_task`, `gotchas`) via `_seed_default_blocks`. Block writes touch NO core cache namespace, so no epoch bump.
- **explanation:** Letta-style named memory blocks introduced in v5.33.0. Blocks are always-injected, named text containers scoped to either a project directory or globally. `block_create` creates a new block with a char limit; `block_get` retrieves by name+scope; `block_update` full-replaces content (char limit enforced); `block_delete` removes idempotently; `block_list` returns all blocks for a scope+directory; `block_replace` and `block_append` are surgical patch operations that avoid re-emitting full content. `MEMORY_BLOCK_TOTAL_BUDGET_CHARS` controls the aggregate character budget across all blocks injected into context. All write operations run `gate_or_reject()` for secret detection (I26).

### CAP-WIKI-011 — Wiki bookmarks (bookmark_add/remove/list/reorder)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `bookmark_add`, `bookmark_remove`, `bookmark_list`, `bookmark_reorder`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/bookmarks.py::bookmark_add`, `yadgar/core/server/tools/bookmarks.py::bookmark_remove`, `yadgar/core/server/tools/bookmarks.py::bookmark_list`, `yadgar/core/server/tools/bookmarks.py::bookmark_reorder`, `yadgar/backend/admin_exec/bookmarks.py`
- **wiring:** All tools are `@_tool()`-registered. `bookmark_list` (read) delegates to `_get_storage()._BookmarksMixin` in core. R3 Car 3a (R5 forward): the WRITE tools (`bookmark_add`/`bookmark_remove`/`bookmark_reorder`) validate the slug core-side and forward the storage write to the backend `POST /admin` endpoint via `_forward_admin` (op-name = tool-name); the backend `admin_exec.bookmarks` impls run the `_BookmarksMixin` write (`add_bookmark`, `remove_bookmark`, `reorder_bookmark`) — forward-only, core touches zero DB directly. No secret gate (bookmarks are slug references, not user content). `bookmark_add` is idempotent on slug (updates label if already present).
- **explanation:** User-curated ordered list of wiki page slugs for quick navigation. `bookmark_add` pins a slug at the next available position with an optional display label override. `bookmark_remove` unpins idempotently. `bookmark_list` returns all bookmarks sorted by position. `bookmark_reorder` moves a bookmark to a new 0-based position using dense-integer semantics (all positions compact to 0, 1, 2, … after reorder). Bookmarks are stored in the `wiki_bookmark` table and are not scoped to a directory.

### CAP-WIKI-012 — Project brief (project_brief, BRIEF_MODE_DEFAULT)
- **status:** LIVE
- **category:** wiki
- **settings:** `BRIEF_MODE_DEFAULT`, `PROJECT_BRIEF_MAX_ANCHORS`, `PROJECT_CONTEXT_MIN_HEAT`, `SIGNALS_TOKEN_BUDGET_SOFT`
- **tools:** `project_brief`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/project.py::project_brief`, `yadgar/core/server/tools/project.py::_project_brief_signals`, `yadgar/core/server/tools/project.py::_project_brief_restore`, `yadgar/core/server/tools/project.py::_project_brief_catalog_full`
- **wiring:** MCP caller → `project_brief(directory, mode)` → resolves project root via `_resolve_project_root()`, fetches presence rows (`_project_init`, `_active_work`, checkpoint) → dispatches to mode-specific builder: `_project_brief_signals()`, `_project_brief_restore()`, or `_project_brief_catalog_full()`. The signals mode also calls `_compute_anchor_signals()`, `_apply_roadmap_signal()`, and `_apply_rejection_signal()`. Stop hook calls `project_brief(mode='signals')` on session end; restore hook calls `project_brief(mode='restore')`.
- **explanation:** Layered project context snapshot with four modes. `signals` (<100 tokens): binary presence flags, age numerics, anchor hygiene signals, roadmap lag, DLQ rejection count, and `recommended_actions` list — designed for the stop hook. `restore` (<800 tokens): anchors + hot memories + checkpoint + wiki catalog — designed for post-`/clear` context restoration. `catalog` (~500 tokens, deprecated since v5.7.12): full shape with anchors + presence + hot memories + wiki keys + `_render`. `full` (~1050 tokens): superset of catalog with inlined init_memory and active_work. `SIGNALS_TOKEN_BUDGET_SOFT` (default 350) emits an observability metric when the signals payload exceeds the budget. `PROJECT_BRIEF_MAX_ANCHORS` (default 12) caps the anchor list in restore mode. `PROJECT_CONTEXT_MIN_HEAT` (default 0.01) is a filter threshold for nearly-cold memories in hot_memories sections.

### CAP-WIKI-013 — Bootstrap and seed project tools
- **status:** LIVE
- **category:** ops
- **settings:** `PROJECT_INIT_CAP_CHARS`
- **tools:** `bootstrap_project`, `seed_project`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/project.py::bootstrap_project`, `yadgar/core/server/tools/misc.py::seed_project`, `yadgar/core/seed/_generate.py::seed_project`
- **wiring:** `bootstrap_project(directory, content)` is `@_tool(power=True)` in `project.py`; it calls `_resolve_project_root()`, `_get_storage().upsert_project_init()`, then `_seed_default_blocks()` to create default `current_task` and `gotchas` blocks. `seed_project(directory, dry_run)` is `@_tool(power=True)` in `misc.py`; it delegates to `yadgar.seed.seed_project()` which scans the project directory for config files, docs, and source structure and creates `_seed`-tagged memories. Re-running is idempotent — old seed memories are replaced.
- **explanation:** Two complementary project bootstrapping tools. `bootstrap_project` is lightweight: it takes a caller-supplied concise markdown string (capped at `PROJECT_INIT_CAP_CHARS` chars, default 2000) and stores it as the `_project_init` memory for the directory, also seeding default memory blocks. `seed_project` is heavyweight: it auto-scans the project directory tree to discover config files, documentation, CI configs, and key source files, synthesising foundational `_seed`-tagged memories without any manual input.

### CAP-WIKI-013a — Project-registry seed tool (CLI + MCP)
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `project_seed`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/misc.py::project_seed`, `yadgar/core/cli/project.py::parse_map`, `yadgar/core/cli/project.py::classify_row`, `yadgar/core/cli/project.py::infer_kind`, `yadgar/core/cli/project.py::seed_row`, `yadgar/core/cli/project.py::cmd_project_seed`, `yadgar/core/cli/project.py::register`, `yadgar/backend/admin_exec/ledger.py::create_project_row`, `yadgar/backend/admin_exec/__init__.py::_ADMIN_OPS["create_project_row"]`
- **wiring:** Two surfaces for the same operation, both routed through `yadgar.core.cli.project` helpers (`parse_map`, `classify_row`, `infer_kind`, `seed_row`). (1) CLI: `yadgar project seed [--map <path>]` — registered in `yadgar/__main__.py` via `project.register(subparsers)`; handler is `cmd_project_seed`. (2) MCP: `@_tool(power=True) project_seed(map_path=...)` in `misc.py`. Both call `_forward_admin("create_project_row", ...)` per row over the backend `/admin` route (the seam at `backend/admin_exec/__init__.py:152`); per-row outcomes are `created` / `skipped` (duplicate-key idempotent) / `failed`. Rows whose TSV column 2 is `DROP` or `REVIEW` are skipped — those are operator decisions, not registry rows. Idempotent: a second run on the same map is a no-op for already-present keys. **Failure signal (ledger task 13 defect 1, 2026-08-20):** the CLI exits 1, and the MCP tool's `ok` is `counts["failed"] == 0`, whenever at least one row genuinely failed — duplicates stay classified `skipped` and do not trip this. Previously the CLI always returned 0 and the MCP `ok` was a literal `True` regardless of `counts["failed"]`, unless the map file itself was structurally malformed.
- **explanation:** Car A (2026-08-14 identity train, §2) closes the gap where `backend.admin_exec.ledger.create_project_row` existed and was registered but had no CLI / MCP path. The registry is the FK target for every `task` / `adr` / `agent_prompt` ledger row — with zero rows, engine-#2 refuses every write via the `assert_project_registered` guard (ADR-0078). This tool is the SEED that lets the guard ever succeed; the guard itself is NOT relaxed by this tool. The TSV first column is named `source_directory` — a host-side origin hint captured at mint time, NOT a scoping key (ADR-0225). `infer_kind` classifies by `project_id` shape alone (`/` → git, `local/` → local, prose → local). The MCP surface takes a single `map_path` keyword; the previous v1 draft's `directory` parameter was dropped to satisfy the ADR-0225 residue sweep without an allowlist entry.

### CAP-WIKI-013b — Project-registry staleness surface (CLI)
- **status:** LIVE
- **category:** ops
- **settings:** `PROJECT_STALENESS_DAYS`
- **tools:** —
- **migrations:** `005_project_last_validated`
- **bc:** —
- **refs:** `yadgar/core/cli/project.py::cmd_project_list`, `yadgar/core/cli/project.py::register`, `yadgar/backend/admin_exec/ledger.py::list_stale_projects`, `yadgar/backend/admin_exec/ledger.py::list_project_rows`, `yadgar/backend/admin_exec/__init__.py::_ADMIN_OPS["list_stale_projects"]`, `yadgar/_shared/storage/sql/registry.py::list_stale_projects`, `yadgar/_shared/storage/sql/migrations/versions/005_project_last_validated.py`, `yadgar/_shared/storage/sql/registry.py::assert_project_registered`
- **wiring:** Two CLI subcommands under `yadgar project list`. (1) `yadgar project list` (no flag) — calls `_forward_admin("list_project_rows", ...)` and prints `key` + `kind` per registered project. It does NOT print `last_validated_at`, and `list_project_rows` does not SELECT it (task 384): that same statement is what `core/server/tools/_project_registry` forwards to answer `assert_project_registered_for_create` on every `memorize` / `wiki_add`, so naming an optional column there would let `005`'s `downgrade()` fail the SELECT with MySQL 1054 and silently degrade the create gate to a shape check. (2) `yadgar project list --stale` — calls `_forward_admin("list_stale_projects", ...)`; the backend op reads `Settings.PROJECT_STALENESS_DAYS` (env `YADGAR_PROJECT_STALENESS_DAYS`, default 90) and filters rows where `last_validated_at IS NULL OR last_validated_at < (CURRENT_TIMESTAMP - INTERVAL :days DAY)`, selecting the column itself. The result echoes `threshold_days` so the CLI can render "stale since N days" without re-reading settings. The migration `005` backfills existing rows with `CURRENT_TIMESTAMP` so the threshold does NOT trip on day-zero after deploy. `MariaStorageEngine.assert_project_registered` bumps `last_validated_at = CURRENT_TIMESTAMP` on every successful registry check, in its own transaction after the check and wrapped in try/except so a refresh failure can never fail the ledger write that called it.
- **explanation:** Car C11-#88 (task #88, 2026-08-24) — the project table had `created_at` (one-shot) but no `last_validated_at`, so 81 days of silent drift on the canonical repo went unmeasured. This car adds the column, the threshold, and the operator-visible staleness surface. `PROJECT_STALENESS_DAYS` defaults to 90 to match the anchor-conditional TTL and the action-stream archive sweep — three months is the signal this car exists to catch; tighten via env if a deployment wants a shorter window. NULL `last_validated_at` rows are included in the stale set because "never validated" is the failure mode (a row that pre-dates the column cannot be older than anything, but it IS stale in the operator's intent). The refresh-on-guard-check design means every successful `assert_project_registered` call resets the clock; the threshold therefore measures "rows the registry has not seen in N days", not "rows older than N days since creation". Task 384 re-homed that bump: #88 put it on a standalone `admin_exec` guard with zero call sites, so as shipped nothing would ever have bumped the column — `005`'s day-zero backfill would have been the last write, and from day 91 `--stale` would have reported EVERY project stale, permanently, i.e. the exact inverse of its signal. Scope of the fix, stated rather than implied: `assert_project_registered` is reached from `create_task_row` / `create_adr_row` only, so the clock now measures LEDGER activity. A project that only stores memories or wiki pages goes through `assert_project_registered_for_create`, which answers from a cached forwarded read and does not bump — it still ages past the threshold.

### CAP-WIKI-014 — Sync instructions tool
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `sync_instructions`
- **migrations:** —
- **bc:** `BC-HK2`
- **refs:** `yadgar/core/server/tools/misc.py::sync_instructions`
- **wiring:** `@_tool(power=True)`-registered in `misc.py`. Called directly by MCP clients or the Claude Code `install_hooks` flow. Reads `~/.claude/CLAUDE.md`, finds or appends the `## Memory System — Yadgar` section using a regex, and atomically replaces the file via `tempfile.mkstemp` + `os.replace`.
- **explanation:** Writes or updates the Yadgar protocol block in the user's global `CLAUDE.md` file so Claude Code sessions receive up-to-date tool usage instructions. The section is version-stamped and idempotent: re-running replaces only the Yadgar section, leaving the rest of `CLAUDE.md` intact. Uses atomic write (tmp + rename) to prevent corruption on crash. BC-HK2: a stale block is replaced, not duplicated.

### CAP-WIKI-015 — Agent prompt library (save + dispatch_prelude slug-read)
- **status:** LIVE
- **category:** wiki
- **settings:** `DISPATCH_PRELUDE_DUE_WARN_HOURS`
- **tools:** `agent_prompt_save`, `agent_prompt_get`, `agent_prompt_list`, `agent_dispatch_prelude`
- **migrations:** —
- **bc:** `BC-AP1`, `BC-AP2`, `BC-AP3`
- **refs:** `yadgar/core/server/tools/agent_prompts.py::agent_prompt_save`, `yadgar/core/server/tools/agent_prompts.py::agent_prompt_get`, `yadgar/core/server/tools/agent_prompts.py::agent_prompt_list`, `yadgar/core/server/tools/agent_prompts.py::_read_agent_prompt`, `yadgar/core/server/tools/dispatch_helper.py::agent_dispatch_prelude`, `yadgar/backend/admin_exec/ledger.py::list_agent_pattern_rows_uses_desc`, `yadgar/backend/admin_exec/ledger.py::get_agent_pattern_row`
- **wiring:** `agent_prompt_save` is `@_tool()`-registered in `agent_prompts.py`; routes through `_st._wiki.add()` with `directory` provenance (v5.42.5), one page per pattern at deterministic slug `agent-prompt-<pattern>` (wiki versioning carries history). The internal `_read_agent_prompt(slug, storage)` helper (no decorator, NOT an MCP tool) does the exact-key slug read via `get_wiki_page_by_slug` + `get_max_version_for_page`. `agent_dispatch_prelude` is `@_tool()`-registered in `dispatch_helper.py`; calls `_read_agent_prompt(f"agent-prompt-{pattern}")` then optionally `recall()` + `wiki_query()` (when `include_context=True`) to build a markdown prelude string capped at 2000 chars (4000 with context). **0047 Car I** added `agent_prompt_get(pattern, directory)` and `agent_prompt_list(status=None, directory=None, limit=20)`, both `@_tool(power=True)`: the discovery surface is the `agent_pattern` ledger table (MariaDB engine #2), reached via `_forward_admin("list_agent_pattern_rows_uses_desc" / "get_agent_pattern_row", ...)`. The wiki TOC page (slug `agent-prompt-toc`) and its memory-row library anchor are RETIRED (D35a/D35d); the slug survives as a kept-ignored pointer for one cycle so old callers do not crash, but its content is sourced from the ledger.
- **explanation:** An agent-prompt library stored as wiki pages (v5.85 S4/S5 rework). `agent_prompt_save(pattern, content)` upserts one page per pattern at slug `agent-prompt-<pattern>`; the second save bumps the wiki page version, not a new `-vN` page. The exact-key lookup is the internal `_read_agent_prompt(slug)` helper (the bespoke `agent_prompt_get` MCP tool was removed); semantic lookup is `recall(type="wiki", tags=["agent-prompt"])` (the bespoke `agent_prompt_search` tool collapsed into the tag-aware recall path — see CAP-WIKI-017). `agent_dispatch_prelude(pattern, task_topic)` composes a standard markdown prelude for subagent dispatch: the Yadgar protocol contract block, the latest saved prompt for the pattern via the slug-read (if any), and a recall hint for the task topic. The v5.44.0 X1 extension adds opt-in `include_context=True` mode which auto-prefetches recall + wiki_query results and embeds them in the prelude.

### CAP-WIKI-017 — Agent-prompt semantic lookup via tag-aware recall (collapse)
- **status:** LIVE
- **category:** wiki
- **settings:** `AGENT_PROMPT_LIBRARY_ENABLED`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/recall.py::recall`, `yadgar/_shared/storage/wiki.py::search_wiki_vectors_tagged`
- **wiring:** Semantic retrieval of agent-prompt pages is `recall(type="wiki", tags=["agent-prompt"])` (v5.85 S3/S4 collapse — the bespoke `agent_prompt_search` tool was removed). The `tags` include-filter threads `recall → _fanout_recall → WikiProvider → WikiStore.query`, routing the wiki vector collector to the SQL pre-filter `StorageEngine.search_wiki_vectors_tagged(embedding, include_tag, top_k)` — a tag-scoped brute-force cosine over `tags CONTAINS $tag` rows, avoiding the global-corpus dilution of the generic HNSW `search_wiki_vectors`. Conversely, general recall (no `tags`) excludes `agent-prompt` via a post-rank `exclude_tags` filter so the library does not pollute normal recall; requesting `tags=["agent-prompt"]` suppresses that default exclude (precedence).
- **explanation:** v5.85 car #6 rework (ADR-0007) — the agent-prompt passive library. The previously-bespoke `agent_prompt_search` semantic tool collapsed into the unified recall path: targeted lookup is `recall(type="wiki", tags=["agent-prompt"])`, which returns the best-matching saved dispatch prompts ranked by cosine similarity over only the agent-prompt subset (SQL pre-filter, dilution-safe). The `AGENT_PROMPT_LIBRARY_ENABLED` kill-gate is intended to make the library inert when False. Heat-based ranking is deliberately deferred: `wiki_page` has no heat column, so ranking is by semantic similarity only.

### CAP-WIKI-016 — Session-end sentinel capture
- **status:** LIVE
- **category:** ops
- **settings:** `SESSION_END_CAPTURE_ENABLED`, `SESSION_END_MIN_MESSAGES`, `SESSION_END_RETENTION_DAYS`, `SESSION_END_SNIPPET_TURNS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-HK1`
- **refs:** `yadgar/core/hooks/session-end-capture.py`, `yadgar/core/server/tools/project.py::_check_session_end_sentinel`
- **wiring:** Claude Code `SessionEnd` hook → `yadgar-session-end-capture.py` script (installed by `install_hooks`) → checks `SESSION_END_CAPTURE_ENABLED`; skips if `message_count < SESSION_END_MIN_MESSAGES`. On qualifying sessions, writes a JSON sentinel atomically to `~/.local/state/yadgar/session-ends/`. On next `project_brief(mode='signals')`, `_check_session_end_sentinel()` detects the unprocessed sentinel and returns an `extract_last_session_findings` recommended action pointing to the transcript path.
- **explanation:** Captures a lightweight sentinel at session end so the next session knows a transcript exists to mine. The sentinel records `ended_at`, `message_count`, `transcript_path`, the last N human turns (`SESSION_END_SNIPPET_TURNS`, default 5), and recently touched files. The `project_brief` signals mode surfaces an `extract_last_session_findings` action when an unprocessed sentinel is found, prompting the agent to read the transcript and call `memorize` with key findings. `SESSION_END_RETENTION_DAYS` (default 30) controls how long sentinel files are retained. BC-HK1: `install_hooks` writes the hook config idempotently.

### CAP-WIKI-017 — Directory filter enforcement in recall and wiki_query
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_query`
- **migrations:** —
- **bc:** `BC-B1`, `BC-B2`, `BC-B3`, `BC-B4`, `BC-B5`, `BC-B6`
- **refs:** `yadgar/core/server/tools/wiki.py::wiki_query`
- **wiring:** `wiki_query()` validates that `directory` is non-empty at the function boundary (raises `ValueError` if absent — BC-B3). Results are post-filtered via `is_directory_eligible(r.get("directory_context"), _dir_stripped)` from `yadgar/_shared/storage/directory.py`, which allows pages whose `directory_context` matches the caller directory or is `global` (BC-B1, BC-B2). Entries tagged with `system` are handled by the storage/recall layer eligibility rules (BC-B4). Profile-sourced memories surface via the recall retrieval layer (BC-B5, exercised in the recall pipeline; wiki_query does not directly surface profiles but is part of the same retrieval surface). Belief-sourced results surface via the same recall retrieval layer (BC-B6): the fusion belief branch narrows its `except` to `(KeyError, TypeError, ValueError)` so a config/storage error no longer silently drops every belief.
- **explanation:** The directory scoping contract (BC-B1 through BC-B5) governs how `wiki_query` (and `recall`) filter results to the calling project. BC-B1: results include the caller's directory and 'global' pages, excluding pages from other directories. BC-B2: the same directory filter applies to wiki results returned within the recall flow. BC-B3: `wiki_query` hard-raises `ValueError` when `directory` is absent or empty (v5.65 Fix D), making the caller supply real context. BC-B4: 'system'-tagged or system-directory entries are excluded from eligibility. BC-B5: profile-sourced memories surface in retrieval results when a profile exists for the queried context. BC-B6: belief-sourced results likewise surface when a derived belief exists (the belief branch except is narrowed so a config/storage error surfaces instead of silently dropping all beliefs).

### CAP-WIKI-019 — Anchor promote-to-wiki signal
- **status:** LIVE
- **category:** wiki
- **settings:** `ANCHOR_PROMOTE_HEADERS`, `ANCHOR_PROMOTE_WORDS`
- **tools:** `project_brief`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/project.py::_fetch_anchor_promote_ids`, `yadgar/core/server/tools/project.py::_compute_anchor_signals`
- **wiring:** `project_brief(mode='signals')` → `_project_brief_signals()` → `_compute_anchor_signals()` → `_fetch_anchor_promote_ids()`. Queries all project anchors, filters by triple AND: `word_count > ANCHOR_PROMOTE_WORDS`, `header_count >= ANCHOR_PROMOTE_HEADERS`, and `tags ∩ {rule, pattern, convention, playbook, workflow, recipe} ≠ ∅`. Returns IDs of qualifying anchors, capped at 3 (`_SIGNALS_CANDIDATES_K`).
- **explanation:** Detects oversized, structured anchors that have grown rich enough to warrant promotion to wiki pages. The triple-AND filter ensures only anchors with substantial content (`ANCHOR_PROMOTE_WORDS` words, default 500), multiple markdown headers (`ANCHOR_PROMOTE_HEADERS`, default 2), AND playbook/pattern tags qualify. When candidates are found, a `promote_anchor_to_wiki` recommended_action appears in the `project_brief(mode='signals')` payload, nudging the agent to create a wiki page and replace the anchor with a reference.

### CAP-CODEGRAPH-001 — code_graph refresh stop-hook cadence + SessionStart soft-suggest (#83 Car D, ADR-0162)

- **status:** LIVE
- **category:** wiki
- **settings:** `CODE_GRAPH_REFRESH_STOP_INTERVAL`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CODEGRAPH-1`, `BC-CODEGRAPH-2`, `BC-CODEGRAPH-3`, `BC-CODEGRAPH-4`, `BC-CODEGRAPH-5`, `BC-CODEGRAPH-6`, `BC-CODEGRAPH-7`
- **refs:** `yadgar/core/hooks/stop-memory-checkpoint.py`, `yadgar/core/hooks/templates/code_graph_refresh_prompt.md`, `yadgar/core/code_graph/config.py`, `yadgar/core/code_graph/default_branch.py`, `yadgar/core/code_graph/runner.py`, `yadgar/core/code_graph/digest.py`, `yadgar/core/cli/code_graph.py`, `yadgar/core/server/http.py`, `yadgar/core/runtime_config_client.py`, `yadgar/core/server/tools/_runtime_config.py`
- **wiring:** Owns the Stop hook's priority-2 slot outright, gated on `code_graph.config.is_enabled(cwd)`, which reads the `code_graph.enabled` **runtime-config row** (ADR-0163 — SUPERSEDES the old `CODE_GRAPH_ENABLED` env flag). **Dir-aware (ADR-0163):** the Stop payload's `cwd` is threaded through `is_due` so a per-repo opt-out (`code_graph.enabled=false` at that dir) → code_graph NOT due there (no wasted nudge). Fail-open: daemon down → host client returns default True (flipped 2026-07-27, ADR-0163 addendum) → code_graph active. Cadence `CODE_GRAPH_REFRESH_STOP_INTERVAL` (default 200, I25 three-way registered) is the human-message cadence between injections. The injected `code_graph_refresh_prompt.md` template is a dumb pipe: run `yadgar code-graph refresh <repo>`; on `skipped:true` do nothing (silent no-op); else `block_update(name="code_graph", scope="project", directory=<payload.directory>, content=<payload.content>)`, falling back to `block_create(...)` when the block does not exist. SessionStart soft-suggest (`session_suggest_line` → `_code_graph_suggest_line` in `http.py`, appended after `render_blocks_section`): when code_graph is enabled + not opted out (via `is_opted_out`, which reads the `code_graph.enabled` store row) AND no `code_graph` block exists for cwd, append a one-line nudge to run the refresh CLI; a present digest block is already injected so no suggestion; never auto-runs. **Container-blindness FIXED (ADR-0163):** `http.py` injects the in-process daemon resolver (`_runtime_config.config_get`) so the daemon reads the flag from its OWN DB — no longer blind to a host env var it never saw.
- **explanation:** Owns the priority-2 maintenance slot with the code_graph architecture digest, a memory BLOCK (recall-free) rather than recall pages. (Formerly a GATED SWAP shared with `repo_wiki_refresh` / CAP-WIKI-023 — repo_wiki, whose pages proved a recall-noise anti-pattern, was decommissioned #33/ADR-0162 once code_graph was proven on ≥1 non-Python repo + yadgar itself; CAP-WIKI-020/CAP-WIKI-023 and the `REPO_WIKI_REFRESH_STOP_INTERVAL` setting were removed from this registry along with the code.) Enable/opt-out is DB-backed + directory-scoped (ADR-0163): the old `CODE_GRAPH_ENABLED` env var (runtime enable) and `.code-graph-disable` repo-marker file are GONE. **2026-07-27 addendum:** the pilot-gate is now satisfied (digest-layers PII/URL-literal leak fixed; proven live on Java/Go/PHP + yadgar itself) — `code_graph.enabled` DEFAULTS TO TRUE (opt-out) rather than requiring `config_set`/the setup prompt to flip it on. **task:0082 addendum (supersedes the two sentences above about `CODE_GRAPH_ENABLED` and the setup prompt):** `CODE_GRAPH_ENABLED` is now read NOWHERE — `cli/setup.py`'s host-binary INSTALL trigger was its last use and is gone. `yadgar setup` no longer prompts and no longer reads stdin: it installs the codebase-memory-mcp host binary BY DEFAULT and persists `code_graph.enabled=true`, so an unattended/scripted install needs no flags. The `--code-graph` opt-in flag was REMOVED (a no-op for a default-on feature); the sole `--no-code-graph` opt-out skips the binary AND persists `code_graph.enabled=false`, and a genuinely impossible install (offline / unsupported platform) does the same instead of aborting setup — so the persisted flag never claims a feature whose binary is absent (`BC-CODEGRAPH-6`). Best-effort caveat: the persist needs a live daemon and `yadgar setup` normally precedes `yadgar daemon start`, so on a fresh box the disable paths print a remediation step rather than landing the row. **task:0067 addendum (`BC-CODEGRAPH-7`) — the `stale @ <sha>` marker is now REACHABLE:** `digest._stale_line` AND-guards `identity["stale"]` and `identity["head_sha"]`, and the one production producer (`cli/code_graph._cmd_refresh`) set neither, so the marker shipped dead — the only coverage hand-built an identity dict and called `render_digest` directly, never driving the producer→renderer seam. `default_branch.refresh_index` now returns `head_sha` from `git rev-parse origin/<default>` on the success path (post-fetch, so it IS the indexed snapshot's sha) and on the `fetch_failed` skip (the stale local remote-tracking ref = the commit the cached index describes); `_cmd_refresh` stamps both keys, and on `fetch_failed` — guarded on a cached architecture AND a resolvable sha, both evaluated before any runner subprocess — re-emits the CACHED digest with `skipped: false` plus the marker instead of writing nothing. `opted_out` and `no_remote_or_default_branch` stay bit-for-bit hard skips (the latter is reached precisely because no `<default>` resolved, so no sha exists by construction); a `CodeGraphError` from the added `get_architecture` call degrades to that same hard skip, so a binary-less box still gets one clean JSON object on stdout rather than an exit-2. **Declared deviation from ADR-0162:** staleness uses git `rev-parse` rather than ADR-0162's nominal `list_projects`/`detect_changes` signature — those need the 259 MB host-side binary, which would make the CI-visible seam test un-runnable. `runner.list_projects` / `runner.detect_changes` therefore remain caller-less/dormant. The hook template's step 2 prose was corrected accordingly (offline/fetch-fail can now produce a written block); its step-3 mechanics are unchanged. **Second, load-bearing half of task:0067 — DETERMINISTIC project naming (found by the real-binary e2e, invisible to every mocked test):** without an explicit `--name` the indexer derives the project from the indexed PATH, which on this flow is a random `tempfile.mkdtemp` worktree — so the name differed on EVERY refresh. Consequences: a cached index was unaddressable by any later run (the stale re-render fell back to the repo basename, never matched, and would have hard-skipped forever — the marker shipping dead a second time), and each refresh leaked a fresh orphan project into the indexer's SQLite. `default_branch._project_name(canonical_root, subdir)` now returns `<sanitised-leaf>-<12-hex sha256(canonical_root\0subdir)>` and is passed as `runner.index_repository(..., name=)`; the name is sanitised to `[A-Za-z0-9._-]` on OUR side so the indexer's own `--name` normalisation is a no-op and the round-trip is an identity (verified against the real v0.9.0 binary: computed == returned `project`, and a later fetch-failing run recomputes the same name with no index). This finally implements ADR-0162's own stated "Project/page key = canonical_root + relative subdir (avoid monorepo leaf/worktree collisions)", which the code had never honoured, and fixes the orphan-project leak as a side effect. Pre-existing orphan projects under old temp-derived names remain in `~/.cache/codebase-memory-mcp` until cleared manually.

### CAP-WIKI-021 — Canonical-write model (Car 0)

- **status:** LIVE
- **category:** write-path
- **settings:** —
- **tools:** `wiki_add`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::_check_wiki_add_context`, `yadgar/core/server/tools/wiki.py::_wiki_write_canonical`, `yadgar/core/server/tools/wiki.py::CANONICAL_PAGE_TYPES`, `yadgar/backend/queue_drainer/dlq.py`
- **wiring:** At wiki-write time `_check_wiki_add_context` rejects a write that names no scope, unconditionally: empty directory → REJECT with the structured `unresolved_project` payload (C5; the enforcement knob and `_missing_directory_error` are both deleted). `_wiki_write_canonical` no longer resolves an identity on a caller's behalf either — it RAISES when the sanctioned caller arrived without a `project_id` stamp. Sanctioned server-side callers reach the canonical write seam via `_wiki_write_canonical` (sets `_internal`; asserts `page_type ∈ CANONICAL_PAGE_TYPES` as defense-in-depth). The drainer honors `_internal` and strips it before the DB write — unchanged. ADR-0215/0217: the four-flow branch router is DELETED, and so is the trusted host-side git fact it consulted along with its whole hook → endpoint → persist → cache → read chain (ADR-0217 found it redundant with project identity).
- **explanation:** `_check_wiki_add_context` tests whether a directory was supplied, which is all it ever did in practice — and after C5 it does so with no knob to turn it off. Car 0 originally paired it with a trusted, non-forgeable host-side git fact so the canonical-vs-branch-scoped write decision fell out of something a model could not assert; ADR-0215 removed branch scoping, and ADR-0217 then deleted that fact after verifying directory enforcement never read it. `CANONICAL_PAGE_TYPES = {task_list, adr}` is a spoofable defense-in-depth assertion inside `_wiki_write_canonical`, NOT the gate — the real boundary is server-side-only reachability of that seam. The forgeable `__canonical__` sentinel from the prior draft is KILLED. Coverage: `tests/core/test_directory_enforcement_chain_e2e.py`.

---

### CAP-WIKI-022 — Sanctioned task-list mirror writer (Car 1)

- **status:** LIVE
- **category:** write-path
- **settings:** —
- **tools:** `wiki_write_task_list`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/wiki.py::wiki_write_task_list`, `yadgar/core/server/tools/wiki.py::_wiki_write_canonical`, `yadgar/core/server/tools/wiki.py::CANONICAL_PAGE_TYPES`, `yadgar/core/hooks/templates/stop_checkpoint_prompt.md`
- **wiring:** The stop-hook checkpoint protocol (step 4c of `stop_checkpoint_prompt.md`) calls `wiki_write_task_list(project, content, directory, project_id=…)` to persist the harness task list. **C5 (0047 PR#40 §5) added the keyword-only `project_id`, and it is REQUIRED**: `project` is the slug key (a bare name) and has never been an identity, so it cannot double as one — `_wiki_write_canonical` used to paper over the gap by resolving with a fallback, and with the fallback deleted the writer must arrive with its owner named. The tool builds a fixed payload (`slug={project}-task-list`, `title="{project} task list"`, `page_type="task_list"`, `tags=["task-list"]`, `replace_slug={project}-task-list`), applies the same secret-gate / size / surrogate guards as `wiki_add`, then routes through the server-side `_wiki_write_canonical` (CAP-WIKI-021: sets `_internal`, `wait` param threaded to the existing `_wiki_add_wait_path`). The page resolves by directory alone, so the SessionStart restore-nudge finds it from any working tree and from a non-git project.
- **explanation:** Fixes the field-broken task-list mirror. The shipped template told the model to `wiki_add(page_type="task_list")` with no branch context, which the branch router hard-rejected in a git dir — `page_type` was never a canonical gate (forgeable) — so the mirror never persisted. `wiki_write_task_list` is a dedicated sanctioned writer whose sanction is STRUCTURAL (purpose-built, bounded to the `{project}-task-list` slug), NOT a spoofable arg — a model cannot use it to write an arbitrary page through the canonical seam. ADR-0215 removed branch scoping, so the original hard-reject is gone; the structural sanction and its coverage (`tests/core/test_car1_task_list_writer.py`) remain.

---

### CAP-WIKI-024 — Discipline write path with asymmetric removal guard (ADR-0208 prerequisite)

- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `discipline_save`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/agent_prompts.py::discipline_save`, `yadgar/_shared/wiki/prompt_guard.py::removed_prompt_lines`, `yadgar/core/server/tools/agent_prompts.py::_save_discipline_page`, `yadgar/_shared/wiki/store.py::_reject_if_discipline_weakening`, `yadgar/core/server/tools/admin_other.py::_reject_discipline_content_removal`
- **wiring:** `discipline_save(name, content, purpose=None, confirm_removal=False)` is `@_tool()`-registered in `agent_prompts.py` and exported in `tools/__init__.py` (this package has no autodiscovery — a decorated function is not a live tool until listed there). It is the first MCP exposure of the upsert `_save_discipline_page` already implements; previously the only caller was the create-if-absent seeder (`_seed_discipline_pages`), so updating a live discipline required a code change plus a release. On call: I26 secret-gate on `content`, then unwrap any accidental `## Purpose`/`## Prompt` wrapper, then (if the page already exists and `confirm_removal` is not set) diff the existing page's unwrapped `## Prompt` body against the incoming body via `_removed_prompt_lines` — a pure set-membership helper, not a line-position diff, so a rule that moved elsewhere in the file is not flagged. A clean diff (or a brand-new page — never a removal) forwards to `_save_discipline_page`, which rides the existing `agent_prompt_save` write path (backend `/admin` op, TOC row, wiki-epoch bump).
- **explanation:** ADR-0208's asymmetric weakening guard, scoped to disciplines: additions to a discipline's rule body flow freely; a net removal of any existing non-empty line is REJECTED (`{"saved": False, "error": "removal_requires_confirmation", "removed_lines": [...]}`, naming exactly which line(s) would be lost) unless `confirm_removal=True` ratifies it. Rationale: a discipline binds every future dispatch, so an instance able to rewrite it unguarded could weaken its own constraints. Mirrors `scripts/check_test_weakening.py`'s delta-counting shape (count what changed, don't ban edits) rather than banning edits outright. Deliberately does NOT implement `baseline_hash`/`content_hash`/drift-detection-against-the-packaged-seed/three-way-merge — ADR-0209 scopes those to a later car; this entry is purely the additions-flow / removal-needs-ratification gate. The seeder's create-if-absent behavior (never reseeds over a live page) is unchanged and unaffected by this tool. **Task 23 addendum — the guard now covers every write path, not one door.** As shipped it protected only `discipline_save`'s front door, while `wiki_delete_text`, `wiki_replace_text`, `wiki_append_section` and the positional edit family resolved `agent-discipline-*` slugs like any other page and could strip rule lines with zero ratification — a guard the same instance can walk around is not a guard. The line-delta primitive moved to `_shared/wiki/prompt_guard.py::removed_prompt_lines` (re-bound, not reimplemented, as `agent_prompts._removed_prompt_lines`) so `WikiStore._reject_if_discipline_weakening` can apply the identical rule below all of them: `_apply_text_edit` is the single chokepoint for the eight anchor-text/positional ops, and `append_section` — which writes directly — carries its own call. Keyed on `page_type == "agent_discipline"`, which is what ADR-0209's split makes possible; before it, patterns and disciplines shared a type and this could only have been a slug-prefix test. Additions still flow. There is deliberately NO `confirm_removal` escape at that layer — `discipline_save` is the sanctioned path and already carries the flag, so adding one to five generic tools would re-open the door this closes. **`wiki_restore` is EXEMPT**: ADR-0208's own consequences name it as the recovery path for auto-applied merges ("every apply creates a version, so `wiki_restore` is one call away"), and reverting to a previously-ratified version is not an unratified weakening. **`wiki_update` needed a SECOND enforcement point.** Its backend op calls `storage.update_wiki_page` directly and never enters `WikiStore`, so the store-level chokepoint cannot see it — and `content` is in its allowed-keys list, so one call could strip every rule line. `_reject_discipline_content_removal` in the `@_tool` shell (`core/server/tools/admin_other.py`) applies the same rule against the same shared primitive, reading the page core-side. It is not a double-gate on the sanctioned path: `discipline_save` reaches the DB via `_save_discipline_page` → `_forward_admin("agent_prompt_save")` → `wiki.add`, a disjoint entry point that never passes through this shell. A patch that does not touch `content` cannot remove a rule and is never gated, and a read failure degrades to allowing the write rather than blocking it.

### CAP-WIKI-025 — Agent page-type split + search-path recall exclusion (ADR-0209, task 0134)

- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** —
- **migrations:** `028`
- **bc:** —
- **refs:** `yadgar/_shared/wiki/wiki_meta.py`, `yadgar/_shared/wiki/policy.py::is_recall_visible`, `yadgar/_shared/storage/migrations.py::_migration_028_agent_page_type_split`, `yadgar/_shared/schemas/wiki_page_types.yaml`, `yadgar/core/server/tools/agent_prompts.py::agent_prompt_save`, `yadgar/core/server/tools/agent_prompts.py::_save_discipline_page`, `yadgar/core/server/tools/wiki.py::wiki_query`, `yadgar/backend/admin_exec/wiki.py::agent_prompt_save`, `yadgar/backend/retrieval/providers/wiki.py`
- **wiring:** The four page-type constants live in `_shared/wiki/wiki_meta.py` (the one module both core and backend may import — the import-linter contract forbids a backend→core edge): `PAGE_TYPE_AGENT_PATTERN`, `PAGE_TYPE_AGENT_DISCIPLINE`, `PAGE_TYPE_AGENT_INDEX` and the retained `PAGE_TYPE_AGENT_PROMPT_LEGACY`. The FAMILY is decided core-side and carried on the `agent_prompt_save` admin payload — `agent_prompt_save` sends `agent_pattern` (excepting the contract slug, which sends `agent_discipline`), `_save_discipline_page` sends `agent_discipline`; the backend op reads `payload["page_type"]` at all three stamp sites (`wiki.add` opts, fallback update, fallback insert) and `_upsert_toc_row` stamps `agent_index` on every re-upsert. All four resolve to one shared `_AGENT_LIBRARY_POLICY` in `POLICY_BY_TYPE` (`recall_disposition="exclude"`, `storage_scope="global"`) so the entries cannot drift. `agent_pattern` + `agent_discipline` are registered in `wiki_page_types.yaml` with the `[Purpose, Prompt]` shape; `agent_index` deliberately is NOT (the TOC is a link list with no such sections and `check_page_type_format` returns `[]` for unregistered types, so policy-only registration buys the exclusion with no permanent lint warning). Migration `028` re-types the live corpus keyed on the SLUG prefix (`startswith`, never `CONTAINS 'agent-'`), only writing rows whose type differs — a second run issues zero updates.
- **explanation:** ADR-0209 splits `page_type=agent_prompt` because `page_type` is the policy lever, not decoration: `get_policy(page_type).recall_disposition` is read at every search seam, while ADR-0198 splits the two families into separate TABLES and ADR-0208 gives them genuinely different governance (disciplines carry the asymmetric removal guard). Keying that governance off a slug prefix is string-matching where a type belongs. The prelude CONTRACT stays INSIDE the discipline type — flagged by ADR-0198's `always_applied`, not promoted to a third type, preserving that ADR's refusal of a singleton special case. The split is taxonomy-only: routing is identical across all three, and a test pins that equality so a behaviour change cannot ride in with the migration. Task 0134 is fixed in the same pass because the new types would otherwise inherit the identical hazard. Three defects, one rule: (1) `agent-prompt-toc` carried `page_type=null`, which falls through to `DEFAULT_POLICY` *include* — the library index was recall-visible, and a page_type-keyed migration would have missed exactly that row; (2) the provider gated the whole exclusion on `if not self._tags`, so passing ANY tag disabled the filter for EVERY page in the result set, and the TOC (tagged `agent-prompt-toc`, not `agent-prompt`) surfaced on the documented `recall(tags=["agent-prompt"])` lookup; (3) `wiki_query` called `WikiStore.query` directly and never consulted `get_policy` at all. Both search paths now share one predicate, `is_recall_visible(page, opt_in_tags)`, whose opt-in is PER PAGE — the caller's tag filter is the consent signal, and consent to see agent-prompt pages is not consent to see every excluded page that ranks beside them. `wiki_read` / `wiki_get` / `wiki_list` stay unfiltered: those are exact-key and enumerative reads, not search.

---

### CAP-OPS-001 — DLQ inspection and replay (dead-letter queue)
- **status:** LIVE
- **category:** ops
- **settings:** `QUEUE_DLQ_RETENTION_DAYS`, `QUEUE_MAX_PERMANENT_ATTEMPTS`, `QUEUE_MAX_TRANSIENT_ATTEMPTS`, `QUEUE_BACKOFF_BASE_S`, `QUEUE_BACKOFF_MAX_S`
- **tools:** `dlq_inspect`, `dlq_requeue`, `dlq_dismiss`
- **migrations:** —
- **bc:** `BC-ADM4`
- **refs:** `yadgar/core/server/tools/admin_dlq.py`, `yadgar/backend/queue_drainer/dlq.py`
- **wiring:** MCP client → `dlq_inspect()` / `dlq_requeue()` / `dlq_dismiss()` registered via `@_tool` in `admin_dlq.py`, imported by shim `admin.py`. `dlq_inspect` reads `*.json.error.json` sidecars from `FileQueue.dlq_dir`. `dlq_requeue` moves a file from `dlq_dir/` back to `queue_dir/` atomically and resets the in-memory retry counter on `_queue_drainer`. `dlq_dismiss` deletes both the payload and its sidecar. All three are power-gated.
- **explanation:** Queue writes that exhaust all retry attempts (permanent errors after `QUEUE_MAX_PERMANENT_ATTEMPTS` tries, transient errors after `QUEUE_MAX_TRANSIENT_ATTEMPTS`) are moved to the dead-letter directory with a `.json.error.json` sidecar recording `failure_reason`, `attempts`, and `last_error`. `dlq_inspect` lists DLQ entries filterable by failure taxonomy (`all`, `rejections` for duplicate/policy entries, `failures` for permanent errors). `dlq_requeue` moves an entry back to the active queue so it is retried on the next drain pass; it blocks requeue of rejection-taxonomy entries unless `force=True` to prevent immediate re-rejection. `dlq_dismiss` permanently discards an entry after operator review.

### CAP-OPS-002 — File queue async write pipeline
- **status:** LIVE
- **category:** ops
- **settings:** `DATA_DIR`, `QUEUE_DRAIN_INTERVAL`, `QUEUE_BACKOFF_BASE_S`, `QUEUE_BACKOFF_MAX_S`, `QUEUE_DLQ_RETENTION_DAYS`, `QUEUE_MAX_PERMANENT_ATTEMPTS`, `QUEUE_MAX_TRANSIENT_ATTEMPTS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/file_queue/queue.py`, `yadgar/backend/queue_drainer/apply.py`, `yadgar/backend/queue_drainer/__init__.py`
- **wiring:** All MCP write tools (`memorize`, `wiki_add`, `checkpoint`, `anchor`, `update_active_work`) call `_get_file_queue().enqueue()` to write an atomic `.json` file under `DATA_DIR/queue/`. A background `QueueDrainer` thread polls every `QUEUE_DRAIN_INTERVAL` seconds, applies each operation via `apply.py` handlers, archives successes to `DATA_DIR/archive/`, and moves exhausted entries to `DATA_DIR/dlq/`.
- **explanation:** The file queue is the write-path backbone: MCP tools return immediately after writing a timestamped JSON payload to the filesystem (`queue_dir`), decoupling write latency from DB commit latency. The drainer thread processes entries in arrival order, applies exponential back-off (`QUEUE_BACKOFF_BASE_S` → `QUEUE_BACKOFF_MAX_S`) on transient failures, and promotes entries to the DLQ after `QUEUE_MAX_PERMANENT_ATTEMPTS` or `QUEUE_MAX_TRANSIENT_ATTEMPTS` exhaustion. `wait=True` callers in `wiki_add` can block on a per-job `threading.Event` for read-your-writes semantics.

### CAP-OPS-003 — Vacuum: threshold-driven auto-vacuum backstop
- **status:** LIVE
- **category:** ops
- **settings:** `VACUUM_AUTO_ENABLED`, `VACUUM_AUTO_THRESHOLD_BYTES`, `VACUUM_AUTO_WINDOW_START`, `VACUUM_AUTO_WINDOW_END`, `VACUUM_SNAPSHOT_RETENTION`, `VACUUM_SNAPSHOT_MAX_AGE_DAYS`, `VACUUM_AUTO_COOLDOWN_HOURS`
- **tools:** `vacuum_now`
- **migrations:** —
- **bc:** `BC-E4`
- **refs:** `yadgar/core/server/tools/admin_vacuum.py`, `yadgar/core/vacuum/phases.py`, `yadgar/core/vacuum/__init__.py`, `yadgar/core/ops/ops.py`
- **wiring:** `vacuum_now()` MCP tool → `_fire_vacuum_service()` in `yadgar/core/ops/ops.py` writes a trigger file at `YADGAR_VACUUM_TRIGGER_PATH`; on surfaces that ship one, a host-side watcher (systemd `.path` unit on nix, launchd `WatchPaths` on macOS) picks it up and starts `yadgar-vacuum.service`. `YADGAR_VACUUM_TRIGGER_PATH` has **no code default** (task:0044): each watcher-bearing surface sets it explicitly to a path under a bind mount it also declares, and unset means "no watcher here" — `_fire_vacuum_service()` then raises `VacuumTriggerNotConfiguredError`, `vacuum_now()` returns `started=False, skipped_reason="no_trigger_path_configured"`, and the backstop logs an error without stamping its cooldown. The threshold backstop path lives in `ConsolidationScheduler._maybe_auto_vacuum()`: when DB size exceeds `VACUUM_AUTO_THRESHOLD_BYTES` AND local time falls inside `[VACUUM_AUTO_WINDOW_START, VACUUM_AUTO_WINDOW_END)`, it fires the same trigger. The nightly cron (`yadgar-vacuum.timer`) is the primary trigger; this backstop fires only when growth outpaces the nightly schedule.
- **explanation:** Vacuum reclaims SurrealKV disk space by exporting, compacting, and atomically swapping in a new DB file. The MCP `vacuum_now()` tool writes a trigger file to decouple the vacuum from the daemon process (avoiding mid-swap crashes). The auto-backstop (`VACUUM_AUTO_ENABLED`) triggers only within the configured daily time window when the DB exceeds the size threshold, preventing unbounded growth between scheduled vacuum runs. `VACUUM_SNAPSHOT_RETENTION` (default 2) controls how many pre-vacuum snapshots to keep and `VACUUM_SNAPSHOT_MAX_AGE_DAYS` (default 14) reaps ones older than that; both are pruned from a single `finally` in `cmd_vacuum_impl` so every exit path is covered (task:0046), and both are floored so at least one rollback anchor always survives — `max(1, keep_n)` in `_reap_stale_pre_vacuum_snapshots`, and an unconditional newest-exempt in `_reap_snapshots_by_age`.

### CAP-OPS-004 — Vacuum: atomic side-build and crash-mid-swap recovery
- **status:** LIVE
- **category:** ops
- **settings:** `SENSITIVE_LOCK_TTL_SEC`, `SENSITIVE_DRAIN_TIMEOUT_SEC`, `BACKEND_IMPORT_TIMEOUT_SEC`, `VACUUM_SIDE_LAUNCHER`, `MAINTENANCE_TTL_SEC`
- **tools:** —
- **migrations:** —
- **bc:** `BC-F1`, `BC-F2`, `BC-F3`
- **refs:** `yadgar/core/vacuum/phases.py::_atomic_swap`, `yadgar/core/vacuum/phases.py::_recover_interrupted_swap`, `yadgar/core/vacuum/phases.py::_vacuum_snapshot_and_drop`, `yadgar/core/vacuum/launcher.py::select_side_launcher`, `yadgar/core/vacuum/launcher.py::_resolve_surreal_binary`
- **wiring:** Called by `yadgar-vacuum.service` (not via MCP). `_vacuum_snapshot_and_drop` stops the real backend before copying the DB (quiesced snapshot), writes a `surreal_db.building-<ts>` side path, verifies row counts, then calls `_atomic_swap` (two same-directory `os.rename` calls). `_recover_interrupted_swap` runs at each vacuum start to detect and complete or roll back any crash-interrupted swap. The sensitive-job lock (`SENSITIVE_LOCK_TTL_SEC`) prevents SIGTERM from interrupting the swap window; `SENSITIVE_DRAIN_TIMEOUT_SEC` bounds how long the signal handler waits. Before the swap, `select_side_launcher()` picks HOW the side build obtains its throwaway SurrealDB (Car 0092): a host `surreal` binary or a one-shot backend container. Task 0107 made that resolution env-independent — `_resolve_surreal_binary()` checks `YADGAR_SURREAL_BIN`, then PATH, then fixed candidate dirs (`~/.local/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`) — instead of a bare PATH-dependent `shutil.which`, and `VACUUM_SIDE_LAUNCHER` (`auto`/`host`/`container`) lets an operator pin the branch explicitly. Task 0113 wraps the whole run in the core write-gate: `cmd_vacuum_impl` POSTs `/api/control/maintenance/enter` (with `MAINTENANCE_TTL_SEC` as a self-heal deadline) before the count baseline, nudges the backend `/admin` `drain_now` op to flush the residual file queue, and releases the gate after finalize — only when its own enter opened the window, so the nightly's outer window is never un-wedged mid-cycle.
- **explanation:** The vacuum performs a stop-then-copy (P2 quiesce order, BC-F3) to avoid copying a torn live DB, then builds the compacted side DB and verifies exact row-count parity before the atomic swap. The row-count parity check compares two PRE-stop snapshots, so it is structurally blind to a write landing between the export and the backend stop — that write reached only the canonical, and the canonical is `rmtree`'d as `.old` after a retained swap. `MAINTENANCE_TTL_SEC` exists because the gate's release runs in a `finally`, which cannot fire on SIGKILL or OOM-kill; an expired deadline lets the core clear the flag itself rather than staying read-only until an operator notices. The two-rename swap (canonical→.old-ts, building→canonical) is atomic per-rename on POSIX; a crash between renames leaves canonical absent, which `_recover_interrupted_swap` detects at next vacuum start and resolves deterministically (promote verified `.new` if present, else roll back `.old`). The sensitive-lock mechanism ensures no SIGTERM can arrive mid-swap. `VACUUM_SIDE_LAUNCHER=host`/`container` fail loud (SKIP naming the pin) rather than silently falling through to the other branch when the pinned option is unresolvable — see ADR-0186 and ADR-0191.

### CAP-OPS-005 — check_invariants: DB consistency audit and auto-repair
- **status:** LIVE
- **category:** ops
- **settings:** `CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS`, `DB_SIZE_WARNING_BYTES`, `DBSIZE_CACHE_TTL_SEC`
- **tools:** `check_invariants`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_invariants.py::_run_check_invariants`, `yadgar/core/server/tools/admin_invariants.py::check_invariants`
- **wiring:** MCP client → `check_invariants()` (power-gated, `@_tool(power=True)`) → `_run_check_invariants(storage)`. Also called from the nightly consolidation cycle via `ConsolidationScheduler`. Each per-table check runs with a `CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS` timeout; timed-out tables are skipped (logged at WARN) while the rest still run.
- **explanation:** Runs a suite of DB consistency checks: dangling `memory_similarity_link`, `memory_transition`, `relationship` (caused_by), `wiki_crossref`, orphan `memory:<N>` entity rows, row-count ceilings for `action_log`/`episode`/`wiki_page`, MSL ceiling (dynamic, based on memory count), `engram_slot` table integrity, and a DB-size telemetry pass. Fixable violations (dangling FKs with no information loss) are auto-repaired by DELETE and reported in the `fixed` list. Non-fixable issues (structural ceiling breaches, slot anomalies) appear in `violations`. `ok=True` only when `violations` and `timeouts` are both empty.

### CAP-OPS-006 — archive_purge: memory_archive retention enforcement
- **status:** LIVE
- **category:** ops
- **settings:** `MEMORY_ARCHIVE_RETENTION_DAYS`, `MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER`, `MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS`
- **tools:** `archive_purge`
- **migrations:** —
- **bc:** `BC-ADM5`
- **refs:** `yadgar/core/server/tools/admin_archive.py::archive_purge`, `yadgar/_shared/storage/ops.py::purge_expired_archives`
- **wiring:** MCP client → `archive_purge(dry_run, retention_days)` (power-gated, secret-gated) → `yadgar.storage.ops.purge_expired_archives(storage, dry_run, retention_days_override)`. Also triggered nightly by the consolidation scheduler.
- **explanation:** Purges `memory_archive` rows older than `MEMORY_ARCHIVE_RETENTION_DAYS` days. Protected rows (anchors, `is_protected=True`) and rows created more recently than `MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS` are always skipped. A circuit-breaker cap (`MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER`, default 500) limits the maximum rows deleted per call to prevent runaway deletes. `dry_run=True` (default) returns candidate count and a 10-ID sample without deleting; `dry_run=False` performs the actual purge.

### CAP-OPS-007 — vacuum_checkpoints: stale checkpoint collapse
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `vacuum_checkpoints`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::vacuum_checkpoints`, `yadgar/_shared/storage/ops.py::vacuum_checkpoints`
- **wiring:** MCP client → `vacuum_checkpoints(dry_run)` (power-gated, `@_tool(power=True)`) → `yadgar.storage.ops.vacuum_checkpoints(storage, dry_run)`. One-shot idempotent admin operation; not called automatically by the scheduler.
- **explanation:** Collapses stale checkpoint rows by keeping only the latest checkpoint per `directory_context`, deleting all older ones. The v5.6.5 per-directory scoping change created multiple rows per directory; this tool is the migration aid to collapse accumulated rows to one-per-directory. `dry_run=True` (default) reports stale count and survivor count without deleting. Returns `{stale_count, deleted, survivors, dry_run}`.

### CAP-OPS-008 — restore and checkpoint: hippocampal session replay
- **status:** LIVE
- **category:** ops
- **settings:** `CHECKPOINT_STALE_HOURS`, `CHECKPOINT_WARN_HOURS`, `MICRO_CHECKPOINT_ENABLED`, `MICRO_CHECKPOINT_COOLDOWN`
- **tools:** `checkpoint`, `restore`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/misc.py::checkpoint`, `yadgar/core/server/tools/misc.py::restore`, `yadgar/core/forward.py::_forward_restore`, `yadgar/backend/restoration/checkpoint_restore.py`, `yadgar/backend/restoration/__init__.py::run_restore`
- **wiring:** `checkpoint()` → `_get_file_queue().enqueue("checkpoint", payload)` (async path, normal sessions); sync path via `_get_replay().create_checkpoint()` during drain replay (backend drainer). T2 Car B: `restore()` is a thin forwarder — `_forward_restore(directory)` → backend `POST /restore` → `run_restore()` → `CheckpointRestore.restore()` next to the DB (the compute exceeded the 95s offload ceiling on core's 1 CPU). The `/hooks/post-compact` handler and the `yadgar restore` CLI subcommand use the same forward; `/hooks/pre-compact` + `yadgar drain` forward the write-only `pre_compact_drain` via `POST /admin`. Micro-checkpoints fire automatically inside the drainer write pipeline (when `MICRO_CHECKPOINT_ENABLED=True`) after every `MICRO_CHECKPOINT_COOLDOWN` tool calls.
- **explanation:** `checkpoint()` captures a structured snapshot of working state (current task, files being edited, key decisions, open questions, next steps, active errors, custom context) and enqueues it for persistent storage. `restore()` reconstructs working context from the latest checkpoint plus anchored memories, thermodynamically hot project memories, and SR-map predicted context, returning a structured restoration report. `CHECKPOINT_STALE_HOURS` / `CHECKPOINT_WARN_HOURS` drive the signals-mode staleness watchdog that suggests periodic `checkpoint` calls.

### CAP-OPS-009 — update_active_work: atomic project active-work memory
- **status:** LIVE
- **category:** ops
- **settings:** `ACTIVE_WORK_STALE_HOURS`, `ACTIVE_WORK_WARN_HOURS`, `AUTO_REFRESH_ACTIVE_WORK`
- **tools:** `update_active_work`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/project.py::update_active_work`
- **wiring:** MCP client → `update_active_work(directory, content)` (power-gated) → `storage.upsert_active_work(resolved, content)` + `_register_active_work_directory(resolved)`. The watchdog (`AUTO_REFRESH_ACTIVE_WORK=True`) auto-writes a stub `_active_work` when staleness exceeds `ACTIVE_WORK_STALE_HOURS`.
- **explanation:** Replaces a directory's `_active_work` memory atomically — deletes all existing `_active_work` rows for the directory in a single transaction and inserts the new content. The `_active_work` memory is the canonical "what I'm currently doing" signal used by `project_brief()` signals mode, which emits `refresh_active_work` soft-action suggestions when the memory is older than `ACTIVE_WORK_WARN_HOURS`. `AUTO_REFRESH_ACTIVE_WORK` enables an optional watchdog that writes a stub entry on stale detection.

### CAP-OPS-010 — install_hooks: Claude Code hook installation
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `install_hooks`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/misc.py::install_hooks`, `yadgar/core/install/clients/install.py::install_client`, `yadgar/core/install/clients/hooks_render.py::register_hooks`, `yadgar/core/install/clients/hooks_render.py::_emit_claude_json`, `yadgar/core/install/install_hooks_lib.py::is_running_in_container`
- **wiring:** MCP client → `install_hooks(project_directory, scope)` (power-gated). Car 7 (2026-07-26): the tool now delegates to `install_client("claude-code", mcp=False, rules=False, hooks=True, scope=scope, project_dir=project_directory, home_dir=Path.home(), dry_run=False)` — the unified orchestrator — which dispatches to `register_hooks` → `_emit_claude_json` → `install_hooks_impl(home_dir, scope, project_directory, dry_run=False)`. Refused when running inside a container (hostname-based detection in `install_hooks_lib.is_running_in_container`); the refused response's `host_command` points at the new canonical command (`yadgar install --client claude-code --hooks --scope=global`). Writes `~/.claude/settings.json` (global scope) or `.claude/settings.json` (project scope). The legacy `yadgar install-hooks` CLI subcommand is hard-removed in Car 7 of the opencode port train (v5.166.0) — see the new CAP-INFRA-034 (opencode hook emitter) for the per-kind emitter architecture that the MCP tool delegates into.
- **explanation:** Installs five Claude Code hook types: `PreCompact` (drain context before compaction), `SessionStart/compact` (restore context after compaction), `SessionStart/all` (inject project context on every new session), `PostToolUse` (capture tool actions into action_log), and `UserPromptSubmit` (auto-recall on every user turn). The `scope` parameter controls whether hooks write to the project-local or global settings. Container environments are rejected because the container filesystem is ephemeral and `$HOME` resolves to `/root` rather than the host user home.

### CAP-OPS-011 — reembed_all: bulk embedding backfill
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `reembed_all`
- **migrations:** —
- **bc:** `BC-ADM1`
- **refs:** `yadgar/core/server/tools/admin_other.py::reembed_all`
- **wiring:** MCP client → `reembed_all()` (power-gated) → `storage.get_memories_without_embeddings()` → batch encode via `embeddings.encode_batch()` → `storage.update_memory_embedding()` for each result. Runs synchronously; large corpora may take minutes.
- **explanation:** Generates embeddings for all memories that are missing them, typically after a bulk import. Queries the DB for rows with null embeddings, filters out null/empty content (which would cause the remote encode-batch endpoint to return all-None), then encodes in batches of 64. Each successful embedding is written back with the current model name. Returns `{reembedded, total_missing, model}` so operators can verify coverage.

### CAP-OPS-012 — get_rules / add_rule: neuro-symbolic rules engine
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `add_rule`, `get_rules`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::add_rule`, `yadgar/core/server/tools/admin_other.py::get_rules`
- **wiring:** MCP client → `add_rule()` (power-gated) / `get_rules()` (power-gated) → `_st._rules_engine.add_rule()` / `get_all_rules()` / `get_applicable_rules()`. `_rules_engine` is a `RulesEngine` singleton initialised during server lifecycle startup.
- **explanation:** `add_rule()` registers a neuro-symbolic rule on the in-memory `RulesEngine`: `hard` rules must be satisfied (filter action) or `soft` rules express preferences (boost or penalty). Scope can be global, per-directory, or per-file. `get_rules()` retrieves all active rules or those applicable to a given directory. Rules are applied during retrieval scoring to filter or re-rank memories according to operator-defined policies.

### CAP-OPS-013 — memory_stats: system statistics dashboard
- **status:** LIVE
- **category:** ops
- **settings:** `STATS_CACHE_TTL_S`, `CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::memory_stats`
- **wiring:** `memory_stats()` (non-power `@_tool()`) → `storage.get_memory_stats()` + engram slot statistics + rules count + episodic/semantic counts + SR dimensions + causal edge count + cognitive load limit + DB-size telemetry + Prometheus metrics snapshot (queue depth, drainer lag p95, recall p95, circuit-breaker states). Called directly by MCP clients; also surfaced at `/memory://stats` MCP resource endpoint.
- **explanation:** Returns a comprehensive system health snapshot: raw DB memory stats, write-gate rejection count, engram slot utilisation ratio, active rule count, episodic/semantic store counts, SR cognitive-map readiness, causal edge count, metacognition chunk limit, per-table row/byte counts, DB file size with warning flag, and a Prometheus metrics block. The metrics block (I8: backpressure must be observable via `memory_stats`) includes queue depth, p95 drainer lag, p95 recall duration, and per-endpoint circuit-breaker states.

### CAP-INFRA-034 — opencode hook emitter: thin TS plugin shim
- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/install/clients/hooks_render.py::_emit_opencode_plugin`, `yadgar/core/install/clients/hooks_render.py::_OPENCODE_PLUGIN_TEMPLATE`, `yadgar/core/install/clients/hooks_render.py::_EXECA_DEP_BLOCK`, `yadgar/core/install/clients/install.py::_render_hooks_fragment`, `yadgar/core/install/clients/install.py::install_client`, `yadgar/core/cli/install.py::cmd_install`, `docs/plans/archive/opencode-hook-port-train-2026-07-26.md` (active; supersedes the archived re-audit at `docs/plans/archive/port-opencode-re-audit-2026-07-26.md`), `docs/plans/followup-opencode-port-2026-07-26.md` (F1-F3 deferred)
- **wiring:** OpenCode has no Claude-Code-style hooks; the install path is a JavaScript/TypeScript plugin (one file) the opencode runtime discovers on startup. The user-facing command is `yadgar install --client opencode` (CAR 2 of the opencode port train), which dispatches to the per-kind emitter `_emit_opencode_plugin` in `hooks_render.py`. The emitter (a) writes `~/.config/opencode/plugins/yadgar-hooks.ts` (global) or `.opencode/plugins/yadgar-hooks.ts` (project) — a thin TS shim that imports `execa` + the typed `Plugin` from `@opencode-ai/plugin`, and subscribes to `experimental.session.compacting` (typed hook, output.context.push), `tool.execute.after` (typed hook), and a generic `event` callback that dispatches on `session.created`/`session.compacted`/`session.idle`; (b) ensures the `execa` dep is merged into `~/.config/opencode/package.json` (Bun installs it at opencode startup). The plugin's IPC is `execa("yadgar", ["hook", "--event", <event>, ...])` — the plugin does NOT call the Yadgar MCP directly (per the re-audit plan §1.1, `ctx.client` is opencode's own typed SDK with no generic MCP invoker; HTTP-to-MCP from inside a plugin is not a working pattern). Coverage: 4/5 functional events wired (sessionStart, sessionStart-restore, postToolUse, preCompact) + 1/5 non-blocking observer (session.idle, blocked on sst/opencode#16626) + 1/5 deferred (chat.message parts[] mutation, gated on a headless `opencode run` test per the re-audit plan §4.5). Idempotency: replace-in-place, marker-detected (`// @yadgar-managed` on the first line). Foreign-preserve: N/A — plugin files are single-file, no shared hooks.json. CAR 2 of the train wired this into the `install_client` orchestrator alongside `register_mcp` and `write_rules`; CAR 3 added a Node-based syntax+structure smoke (skipped when `node` not in PATH, LEGIT-CONDITIONAL skip-inventory entry `opencode-plugin-smoke-01`). **Note on existing emitter cataloguing:** the claude_code (`_emit_claude_json`) and cursor (`_emit_cursor_hooks`) emitters pre-existed in the codebase before this entry and are not yet catalogued. This entry is scoped to the new opencode emitter; broader per-emitter cataloguing is a follow-up cleanup.
- **explanation:** The third per-client hook emitter added to the `_EMITTERS` dispatch table in `hooks_render.py` (after claude_code and cursor). The 4 wired events + 1 non-blocking + 1 deferred match the re-audit plan's 3/5/1/1 coverage assessment; the deferred chat.message handler is intentionally absent from the template (gated on a separate headless test per the plan). The template uses `output.context.push` (NOT clobber of `output.prompt`) for the preCompact drain — `output.prompt` set would replace opencode's framing entirely, which the re-audit §3.1 flags as wrong. The `execa` import is the IPC boundary; replacing it with a fabricated `ctx.client.app[...].call()` would add a runtime dep on `@opencode-ai/plugin` and break in environments without it (caught by the smoke test as a regression guard). The `verify_date` field on the opencode `_OPENCODE` ClientDescriptor row is still `2026-07-18` (the original registry snapshot); bumping it to `2026-07-26` is deferred to a follow-up that touches the registry's `_VERIFIED` constant (the constant is shared across all 9 clients, so changing it affects 8 unrelated rows).


### CAP-OPS-014 — Prometheus metrics endpoint
- **status:** LIVE
- **category:** observability
- **settings:** `METRICS_ENABLED`, `PHASE_DURATION_WARN_MS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/observability/metrics.py`
- **wiring:** `METRICS_ENABLED=True` (default) → `metrics.py` registers collectors in an isolated `CollectorRegistry`; the Starlette app mounts `/metrics` returning `generate_latest()`. `/metrics` is exempt from auth (always unauthenticated on loopback per §2 design). `PHASE_DURATION_WARN_MS` controls a CRITICAL log emitted when any consolidation phase exceeds the threshold.
- **explanation:** Exposes a Prometheus `/metrics` endpoint with collectors covering queue depth by queue type, request counts by route, consolidation phase duration histograms, embedding/CE cache hit/miss counters, action-batch size, per-tool token estimates, loop health gauges (last-run timestamp, error counters), archive retention counters, hook recall timeout counters, and cold-purge candidate gauge.

### CAP-OPS-015 — OTLP distributed tracing
- **status:** DORMANT
- **category:** observability
- **settings:** `OTLP_ENDPOINT`, `OTLP_HEADERS`, `OTLP_TIMEOUT_SEC`, `OTLP_INSECURE`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/observability/tracing.py`
- **wiring:** `yadgar/_shared/observability/tracing.py::setup_tracing()` called at daemon startup. When `OTLP_ENDPOINT` is non-empty, builds an `OTLPSpanExporter` (HTTP/proto) with the configured headers and timeout, **wraps it in `_CircuitBreakerSpanExporter`** (v5.83 obs-train), and wires a `BatchSpanProcessor` into a `TracerProvider`. DORMANT because `OTLP_ENDPOINT` defaults to `""` (empty string = disabled); the code path exists and is reachable but is a no-op unless the operator sets the endpoint. Activation target: the nix otel-collector now listens at `:4318` (added in the obs-train nix car, commit `4fc96b8`).
- **explanation:** Optional OpenTelemetry distributed tracing exporter. When `OTLP_ENDPOINT` is set (e.g. `http://tempo:4318/v1/traces`), the daemon exports W3C TraceContext-propagated spans to the configured collector. `OTLP_HEADERS` passes comma-separated `k=v` authentication or tenant headers. `OTLP_TIMEOUT_SEC` (default 3 s) keeps a dead collector from blocking the export path. **`OTLP_INSECURE` is reserved / no-op for the HTTP exporter** (v5.83 obs-train): transport security for the `opentelemetry-exporter-otlp-proto-http` exporter is decided by the `OTLP_ENDPOINT` URL scheme (`http://` vs `https://`), not by this flag. The knob is kept (not removed) to avoid churning the I25 three-way config sync. **OTLP resilience (v5.83 obs-train):** `_CircuitBreakerSpanExporter` (CB-1 pattern, see `ARCHITECTURE_INVARIANTS.md`) OPENS after 5 consecutive export failures, short-circuits for a 60 s window, then half-open-probes — stopping the retry/log flood when the collector is down while keeping OTLP enabled. Span logs are emitted off the event-loop thread via a `QueueHandler`/`QueueListener` so an export flood cannot stall request handlers.

### CAP-OPS-016 — Bearer-token authentication middleware
- **status:** LIVE
- **category:** security
- **settings:** `REQUIRE_AUTH`, `MCP_AUTH_TOKEN`, `ALLOWED_ORIGINS`, `DEBUG_APIS_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/auth_middleware/auth_middleware.py::BearerAuthMiddleware`
- **wiring:** `BearerAuthMiddleware` wraps the Starlette ASGI app at server startup (unconditionally installed). On each HTTP/WebSocket request it checks: (1) exempt paths (`/health`, `/metrics`) pass through; (2) debug-API paths (`/api/control/config`, `/api/control/action/*`, `/api/control/restart/*`, `/api/logs/*`) require `DEBUG_APIS_ENABLED=True` or return 403; (3) protected prefixes (`/admin/`, `/api/`, `/hooks/`, `/mcp`) require `REQUIRE_AUTH=True` + valid `Authorization: Bearer <MCP_AUTH_TOKEN>` or return 401. Auth env vars are read per-request to enable live token rotation without restart.
- **explanation:** All API and hook routes are bearer-token protected when `REQUIRE_AUTH=True` (default). The token is compared with `hmac.compare_digest` to resist timing attacks. When `REQUIRE_AUTH=True` but `MCP_AUTH_TOKEN` is empty the server returns 503 (fail-secure rather than open). CORS `ALLOWED_ORIGINS` constrains which browser origins the HTTP transport accepts; defaults to loopback only. A separate `DEBUG_APIS_ENABLED` gate restricts powerful control-API paths even from authenticated callers.

### CAP-OPS-017 — Secret-gate allowlist and audit trail
- **status:** LIVE
- **category:** security
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/security/allowlist.py::is_allowlisted`, `yadgar/_shared/security/allowlist.py::_write_audit`
- **wiring:** `gate_or_reject()` in `yadgar/_shared/security/secrets.py` is called by every MCP write tool before any state mutation. It first calls `is_allowlisted(content, tags, source)` from this module; if matched, the write is permitted and an audit entry is appended to a JSONL file under `YADGAR_SECRET_GATE_AUDIT_DIR` (daily rotation). Non-matching content proceeds to pattern scanning.
- **explanation:** The allowlist enables structured bypass of the secret-gate pattern scanner for known-safe content (e.g. test fixtures containing token-shaped strings). Entries in `~/.config/yadgar/secret-gate-allowlist.yaml` specify required tags, glob-prefix patterns, and a human-readable reason. When a write call's content matches an entry's patterns and the call-site tags are a superset of the entry's required tags, the write is allowed through — but every bypass is logged to an immutable JSONL audit trail (date-based rotation) for later review. The allowlist is loaded lazily and thread-safely from disk on first use.

### CAP-OPS-018 — Update version-check mechanism
- **status:** DORMANT
- **category:** ops
- **settings:** `UPDATE_CHECK_ON_START`, `UPDATE_CHECK_TIMEOUT_SECONDS`, `UPDATE_PYPI_URL`, `UPDATE_USER_AGENT_TEMPLATE`, `UPDATE_DEBUG_APIS_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/update/check.py`, `yadgar/core/update/orchestrator.py`
- **wiring:** At daemon startup, when `UPDATE_CHECK_ON_START=True`, `probe_latest_version()` in `yadgar/core/update/check.py` issues an HTTP GET to `UPDATE_PYPI_URL` within `UPDATE_CHECK_TIMEOUT_SECONDS`. Result is surfaced in `/api/control/update` (gated by `UPDATE_DEBUG_APIS_ENABLED`). DORMANT because `UPDATE_CHECK_ON_START=False` by default (opt-in, privacy: avoid phone-home without explicit consent).
- **explanation:** Probes PyPI for the latest published yadgar version to support the upgrade flow. The check uses a configurable User-Agent (`UPDATE_USER_AGENT_TEMPLATE`, `{version}` replaced at runtime). `UPDATE_DEBUG_APIS_ENABLED` enables the `/api/control/update` HTTP endpoint for the Control-tab UI integration. The check is deliberately opt-in (`UPDATE_CHECK_ON_START=False`) to avoid unexpected outbound requests on air-gapped or privacy-sensitive deployments.

### CAP-OPS-019 — Upgrade orchestrator state machine
- **status:** DORMANT
- **category:** ops
- **settings:** `UPDATE_INSTALL_ENABLED`, `UPDATE_LOCK_MAX_AGE_SECONDS`, `UPDATE_SNAPSHOT_RETENTION`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/update/orchestrator.py::run_install`, `yadgar/core/update/snapshot.py`
- **wiring:** Called by the `yadgar update --install` CLI subcommand. `run_install()` checks `UPDATE_INSTALL_ENABLED`; if False, returns immediately with an error pointing to documentation. If enabled, executes the state machine: acquire lock → probe PyPI → snapshot prev state → pull container image → write env-file → graceful stop → restart service → health check → CLI upgrade → re-exec. DORMANT because `UPDATE_INSTALL_ENABLED=False` by default (opt-in safety gate).
- **explanation:** The upgrade orchestrator is a 10-state machine that coordinates a self-upgrade: file lock with PID stale-detection (`UPDATE_LOCK_MAX_AGE_SECONDS`) prevents concurrent upgrades, snapshots capture pre-upgrade state for rollback, and rollback fires automatically on failure at any state from `PULLING_IMAGE` through `HEALTH_CHECKING`. `UPDATE_SNAPSHOT_RETENTION` controls how many upgrade snapshots are retained. In production `os.execvp` replaces the process at `RE_EXECING` state; `DONE` is set by the subsequent `--finalize` subcommand.

### CAP-OPS-020 — Circuit breaker for ML backend endpoints
- **status:** LIVE
- **category:** ops
- **settings:** `CIRCUIT_BREAKER_ENABLED`, `CIRCUIT_BREAKER_FAILURE_THRESHOLD`, `CIRCUIT_BREAKER_OPEN_DURATION_SEC`, `CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC`, `CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC`, `CIRCUIT_BREAKER_BACKOFF_FACTOR`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/ml_client/ml_client.py`
- **wiring:** `RemoteMLClient` in `yadgar/backend/ml_client/ml_client.py` holds per-endpoint circuit breaker instances (`_cb_ce`, `_cb_nli`, `_cb_pair`). After `CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive failures the breaker opens. OPEN state blocks all requests to that endpoint for `CIRCUIT_BREAKER_OPEN_DURATION_SEC` seconds, then transitions to HALF_OPEN for a single probe. Failed probes extend the cooldown with exponential back-off (factor `CIRCUIT_BREAKER_BACKOFF_FACTOR`) up to `CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC`. States are surfaced in `memory_stats()`.
- **explanation:** Protects the daemon from cascading failure when the ML reranking backend (`/rerank/ce`, `/rerank/nli`, `/rerank/pair`) becomes unavailable or slow. The three-state machine (CLOSED → OPEN → HALF_OPEN) stops saturating a degraded backend and allows the retrieval pipeline to fall back to non-reranked results while the breaker is open. Exponential backoff with a cap prevents premature probe storms after repeated backend outages.

### CAP-OPS-021 — Model preload warm-up
- **status:** LIVE
- **category:** ops
- **settings:** `MODEL_PRELOAD`, `MODEL_PRELOAD_DELAY_SEC`, `RERANKER_IDLE_UNLOAD_SEC`, `RERANKER_IDLE_CHECK_INTERVAL_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/`
- **wiring:** After daemon startup, a background thread waits `MODEL_PRELOAD_DELAY_SEC` seconds then sends a warm-up request to the ML backend to load rerank models into memory. Controlled by `MODEL_PRELOAD=True` (default). LIVE because the default is True and the daemon starts this thread unconditionally unless `MODEL_PRELOAD=False`. The mirror side — the `_reranker_idle_loop()` background thread (`yadgar/_shared/runtime/lifecycle.py`) — sleeps `RERANKER_IDLE_CHECK_INTERVAL_SEC` (default 60) between checks and unloads rerankers after `RERANKER_IDLE_UNLOAD_SEC` (default 600.0) of no recall activity, freeing ~500 MB. Both were hardcoded before v5.95; now config.yaml-authoritative.
- **explanation:** Triggers eager loading of reranking model weights (cross-encoder/GTE) in the ML backend process immediately after the daemon is healthy, rather than waiting for the first real rerank request. This amortises the cold-start latency (model loading can take 5-30 s on CPU) so the first user `recall()` call does not experience the full model-load delay. `MODEL_PRELOAD_DELAY_SEC` allows the backend to finish its own startup before the warm-up probe arrives.

### CAP-OPS-022 — CE and embedding LRU cache with snapshot persistence
- **status:** LIVE
- **category:** ops
- **settings:** `CE_CACHE_ENABLED`, `CE_CACHE_MAX_ENTRIES`, `EMBED_CACHE_ENABLED`, `EMBED_CACHE_MAX_ENTRIES`, `CACHE_SNAPSHOT_INTERVAL_SEC`, `CACHE_SNAPSHOT_DIR`, `BACKEND_CACHE_RAM_PCT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/ml_client/ml_client.py`, `yadgar/backend/cache/cache.py`
- **wiring:** `RemoteMLClient` maintains two LRU caches: CE score cache (keyed on query+passage pairs, `CE_CACHE_ENABLED`) and embedding vector cache (`EMBED_CACHE_ENABLED`). Backend caching train Car 0 (#49) folds both into the unified backend `Cache` class (`yadgar/backend/cache/cache.py`, one class / N named namespaces / policy bound at construction) with **byte-bounded LRU eviction**: the byte budget is `BACKEND_CACHE_RAM_PCT` % of the backend container memory limit (cgroup `memory.max` / `memory.limit_in_bytes`), split weighted across the `ce`/`embed` namespaces. A background snapshotting thread persists both caches to `CACHE_SNAPSHOT_DIR/ce.snap` and `CACHE_SNAPSHOT_DIR/embed.snap` every `CACHE_SNAPSHOT_INTERVAL_SEC` seconds. Caches are loaded from snapshot on startup.
- **explanation:** LRU caches absorb repeated CE reranking and embedding requests for the same content, avoiding redundant GPU/CPU inference. The snapshot mechanism persists warm cache state across restarts, so the cache hit rate remains high even after a daemon restart. `BACKEND_CACHE_RAM_PCT` (Car 0) bounds total backend cache memory as a fraction of container RAM — byte-bounded eviction, superseding the legacy count-cap `CE_CACHE_MAX_ENTRIES` / `EMBED_CACHE_MAX_ENTRIES` (kept as inert config for back-compat). Cache hit/miss/evict counts are surfaced as Prometheus counters (`yadgar_embedding_cache_hits_total`, `yadgar_embedding_cache_misses_total`, and the generic `yadgar_cache_*{cache=<namespace>}` family).

### CAP-OPS-041 — Core read-tool cache with RAM-% byte budget
- **status:** LIVE
- **category:** ops
- **settings:** `CORE_CACHE_RAM_PCT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/cache/cache.py`, `yadgar/core/server/tools/project.py`, `yadgar/core/server/tools/wiki.py`, `yadgar/core/server/tools/dispatch_helper.py`
- **wiring:** The unified core `Cache` class (`yadgar/core/cache/cache.py`, #164 — one class / N named instances / policy bound at construction) backs the core read-tool caches: `project_brief` (`project.py`), `wiki_read` + `wiki_query` (`wiki.py`), and `agent_prompt_prelude` (`dispatch_helper.py`). Core caching (#49, v5.112.0) retrofits its LRU bound from a fixed `max_entries` count-cap to **byte-bounded eviction**: the byte budget is `CORE_CACHE_RAM_PCT` % of the CORE container memory limit (cgroup `memory.max` / `memory.limit_in_bytes`, 1 GiB fallback matching the `--memory 1g` core container — NOT the backend's 4 GiB), split weighted across the four core namespaces which share ONE process budget. Byte size is estimated via `msgpack.packb` length; the LRU evicts until `current_bytes ≤ max_bytes`. Mirrors backend Car 0 (`CAP-OPS-022`) but for the core process's own container + namespaces, with its own knob and cgroup reader kept separate from the backend's.
- **explanation:** The core read-tool caches absorb repeated `project_brief` / `wiki_read` / `wiki_query` / agent-prompt-prelude calls within a session (freshness via TTL + structural-epoch-in-key invalidation). `CORE_CACHE_RAM_PCT` bounds their total memory as a fraction of the core container RAM — byte-bounded eviction superseding the earlier per-cache count-caps. The change is behaviour-neutral: at 10 % × 1 GiB ≈ 107 MB / 4 ≈ 25.6 MiB per namespace the byte ceiling dwarfs the small read-tool dicts and never triggers in practice, so TTL + epoch-in-key still do all real eviction with identical keys/values/hit-miss. Hit/miss/evict counts are surfaced as the generic `yadgar_cache_*{cache=<namespace>}` Prometheus family.

### CAP-OPS-023 — ASGI graceful shutdown and daemon lifecycle
- **status:** LIVE
- **category:** ops
- **settings:** `ASGI_SHUTDOWN_TIMEOUT_SEC`, `HOST`, `PORT`, `BACKEND_HTTP_TIMEOUT_SEC`, `BACKEND_IMPORT_TIMEOUT_SEC`, `BACKEND_LOG_LEVEL`, `CORE_LOG_LEVEL`, `LOG_FORMAT`
- **tools:** —
- **migrations:** —
- **bc:** `BC-F2`
- **refs:** `yadgar/core/server/_app.py`, `yadgar/core/daemon/daemon.py`, `yadgar/_shared/observability/log_config.py`
- **wiring:** uvicorn serves the Starlette app at `HOST:PORT`. On SIGTERM, `ASGI_SHUTDOWN_TIMEOUT_SEC` caps the wait for in-flight requests to drain before abandoning them. `HOST`/`PORT` configure where the MCP HTTP server listens. `LOG_FORMAT` (`json`|`text`|`human`) and `CORE_LOG_LEVEL`/`BACKEND_LOG_LEVEL` configure structured logging.
- **explanation:** The daemon lifecycle: the Starlette ASGI app is started by uvicorn with configurable bind address and graceful-shutdown timeout. Structured JSON logging (default `LOG_FORMAT=json`) feeds log aggregators; `text`/`human` modes are for local development. `BACKEND_HTTP_TIMEOUT_SEC` caps operational DB requests; `BACKEND_IMPORT_TIMEOUT_SEC` allows longer bulk-import operations. Note: `DAEMON_CHECK_INTERVAL` was removed in v5.139.1 — the astrocyte loop it once drove is gone (consolidation runs via nightly systemd timer only).

### CAP-OPS-024 — Action stream and action log retention
- **status:** LIVE
- **category:** ops
- **settings:** `ACTION_STREAM_ENABLED`, `ACTION_STREAM_COLD_THRESHOLD`, `ACTION_STREAM_MAX_AGE_DAYS`, `ACTION_LOG_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/runtime/state.py`, `yadgar/backend/consolidation/`
- **wiring:** `ACTION_STREAM_ENABLED=True` (default) enables the sensory action buffer (`_buffer`), which captures tool actions via the `PostToolUse` hook into the `action_log` table. During consolidation, `_memify_prune` pass 5 deletes `_action_stream`-tagged memories older than `ACTION_STREAM_MAX_AGE_DAYS` days — a HARD age cap since ledger task 386: pass 5 has no heat gate of its own and pass 1's needs ~300 days to fire, so this is the only cap these rows have and neither recent access nor heat may cancel it. `is_protected` is the sole escape. `ACTION_LOG_RETENTION_DAYS` governs pruning of raw `action_log` rows each consolidation cycle. `ACTION_STREAM_COLD_THRESHOLD` gates archival of action-stream memories (they decay faster than normal memories).
- **explanation:** The action stream captures every tool call (via PostToolUse hook) into `action_log` for later consolidation into semantic memories. `ACTION_STREAM_ENABLED` is the master switch. Raw `action_log` rows older than `ACTION_LOG_RETENTION_DAYS` days are pruned each cycle to bound table size. Processed action-stream memories (tagged `_action_stream`) are subject to an age cap (`ACTION_STREAM_MAX_AGE_DAYS`) separate from heat-based decay because they start warm but become stale faster than user-authored memories. `ACTION_STREAM_COLD_THRESHOLD` (default 0.1, higher than the global 0.02) archives these memories sooner.

### CAP-OPS-025 — Auto-generated and auto-abstracted memory retention
- **status:** LIVE
- **category:** ops
- **settings:** `AUTO_GENERATED_MEMORY_MAX_AGE_DAYS`, `AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/consolidation/`
- **wiring:** `_memify_prune` in the consolidation cycle runs age-cap pruning passes for memories tagged `auto-generated` (pass 2) and `auto-abstracted` (pass 3). Pass 2 deletes cold, unaccessed memories older than its threshold. Pass 3 is a HARD age cap since ledger task 386 — neither heat nor recency of access spares a row past `AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS`. Called unconditionally each consolidation cycle when `ACTION_STREAM_ENABLED=True`.
- **explanation:** System-generated memories (CLS semantic promotions, action-stream pattern summaries, narrative digests) accumulate rapidly and would exhaust the DB if not capped. `AUTO_GENERATED_MEMORY_MAX_AGE_DAYS` (default 30) and `AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS` (default 30) impose age limits on these low-stakes auto-created rows. Setting either to 0 disables the respective prune pass. **Neither pass consults `last_accessed` any more (ledger task 386).** `recall()` is what writes that column, so a row that kept surfacing kept renewing its own reprieve — the more generic-and-matchy it was, the longer it lived. Pass 3 (`auto-abstracted`) has no other cap at all, so the reprieve cancelled the cap outright rather than deferring it; `is_protected` is now its only escape, matching pass 4's rule for dream insights. Pass 2 (`auto-generated`) keeps its HEAT gate and that is the deliberate difference: heat DECAYS, so one recall buys ~134 days at `DECAY_FACTOR=0.9995` before falling under `COLD_THRESHOLD` and then expires on its own, where a raw timestamp renewed in full on every hit. The timestamp branch was in fact unreachable in pass 2 — `boost_memories_access` writes `heat` and `last_accessed` in one statement, so any row cold enough to clear the heat gate had not been recalled in ~134 days, more than 4x the 30-day window it tested — but at a configured cap above ~134 days it became pass 3's bug, which is why it is gone rather than merely dead.

### CAP-OPS-026 — Auto-capture rate limiting
- **status:** LIVE
- **category:** ops
- **settings:** `AUTO_CAPTURE_RATE_LIMIT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/hooks/`
- **wiring:** The `PostToolUse` hook handler checks a per-directory rate-limit counter before enqueuing an auto-capture write. When the counter exceeds `AUTO_CAPTURE_RATE_LIMIT` requests per directory key per minute, the capture is suppressed. LIVE because the hook fires unconditionally and the rate-limit check is always evaluated.
- **explanation:** Prevents runaway auto-capture from flooding the write queue when a project is generating very high tool-call volume (e.g. CI test runs, large bulk operations). The rate limit is per `directory_context` key, so a noisy directory does not block captures from other directories. Default 30 requests/minute/directory is generous for interactive development while bounding worst-case queue growth.

### CAP-OPS-027 — DB size warning telemetry
- **status:** LIVE
- **category:** observability
- **settings:** `DB_SIZE_WARNING_BYTES`, `DBSIZE_CACHE_TTL_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_invariants.py::_check_db_size`, `yadgar/_shared/storage/`
- **wiring:** `_check_db_size()` is called by `_run_check_invariants()` each time the tool is invoked or the scheduler runs the nightly invariant check. When `db_size_bytes > DB_SIZE_WARNING_BYTES`, a CRITICAL log is emitted — throttled to at most once per hour via `_st._db_size_warn_last_logged_hour`. `/admin/dbsize` HTTP endpoint results are cached for `DBSIZE_CACHE_TTL_SEC` seconds.
- **explanation:** Monitors total SurrealKV storage directory size (vlog + sstables + WAL) against `DB_SIZE_WARNING_BYTES` (default 1 GiB). When breached, a once-per-hour CRITICAL log alerts the operator to consider vacuuming. The size breakdown (`vlog_size_bytes`, `sstables_size_bytes`, `wal_size_bytes`) is included in `check_invariants` and `memory_stats` responses. The `/admin/dbsize` endpoint result is cached (`DBSIZE_CACHE_TTL_SEC`, default 60 s) to avoid repeated filesystem stats on every request.

### CAP-OPS-028 — Anchor audit hygiene pass
- **status:** LIVE
- **category:** ops
- **settings:** `ANCHOR_AUDIT_THRESHOLD`, `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`, `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN`, `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/audit.py::_run_anchor_audit_pass`, `yadgar/core/server/tools/admin_other.py::consolidate_now`
- **wiring:** Called from `consolidate_now(mode='full')` when `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=True` (default). Iterates over known project directories, calls `audit_anchors()` for each, collects expired/redundant/promotable anchors, and returns a summary. Also triggered from `project_brief()` signals mode when anchor count exceeds `ANCHOR_AUDIT_THRESHOLD`.
- **explanation:** Automatically audits anchor hygiene during full consolidation cycles: identifies expired anchors (past `valid_until`), cosine-similar redundant pairs (merge candidates), and oversized anchors suitable for wiki promotion. `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` caps the total actions returned per run to respect token budgets. `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS` controls how long audit snapshots are retained. Results feed the `audit_anchors` recommended action in signals mode.

### CAP-OPS-029 — Sensitive-job lock (vacuum shutdown guard)
- **status:** LIVE
- **category:** ops
- **settings:** `SENSITIVE_LOCK_TTL_SEC`, `SENSITIVE_DRAIN_TIMEOUT_SEC`
- **tools:** —
- **migrations:** —
- **bc:** `BC-F3`
- **refs:** `yadgar/core/vacuum/__init__.py`, `yadgar/core/ops/ops.py`
- **wiring:** The vacuum service acquires a sensitive-job lock file under `YADGAR_DATA_DIR` before beginning the stop-then-copy-then-swap sequence. The daemon's SIGTERM signal handler checks for the lock and refuses shutdown (returns without shutting down) if the lock is held and the holder PID is alive, waiting up to `SENSITIVE_DRAIN_TIMEOUT_SEC` for the job to finish. A lock older than `SENSITIVE_LOCK_TTL_SEC` (2 h) is treated as stale (crashed job) and reaped.
- **explanation:** Prevents a SIGTERM from arriving mid-swap and leaving the database in a partially swapped state (the BC-F3 hazard). The lock is a lightweight file-based mutex: the vacuum job writes its PID and start timestamp; the shutdown handler reads the lock and either waits for the job to release it or skips shutdown if the drain timeout is exceeded. The TTL prevents a crashed vacuum from permanently blocking all future shutdowns.

### CAP-OPS-030 — Stats and stale-count caches
- **status:** LIVE
- **category:** ops
- **settings:** `STATS_CACHE_TTL_S`, `STALE_COUNT_CACHE_TTL_S`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/_app.py`, `yadgar/core/server/tools/project.py`
- **wiring:** `/api/stats` response is cached in-process for `STATS_CACHE_TTL_S` seconds (default 5 s). The stale wiki count used in signals mode is cached per resolved directory for `STALE_COUNT_CACHE_TTL_S` seconds (default 300 s, module-level dict in `project.py`). Both caches are invalidated on demand or by TTL expiry.
- **explanation:** Short TTL caches bound the cost of `/api/stats` calls (which enumerate DB metrics) and the stale-wiki-count scan (which walks the archive directory). Without caching, burst traffic from the Control-tab UI or repeated `project_brief()` signals calls would repeatedly recompute the same values. `STATS_CACHE_TTL_S=0` disables the stats cache; `STALE_COUNT_CACHE_TTL_S=0` disables the stale-count cache.

### CAP-OPS-031 — Hook recall timeout budget + bounded pool
- **status:** LIVE
- **category:** ops
- **settings:** `HOOK_RECALL_TIMEOUT_S`, `HOOK_RECALL_POOL_WORKERS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/hooks/`, `yadgar/core/server/http.py`
- **wiring:** Hook handlers (`prompt-recall`, `instructions-loaded`, `subagent-start`) wrap `recall()` in `asyncio.wait_for(loop.run_in_executor(_HOOK_RECALL_POOL, recall...), timeout=HOOK_RECALL_TIMEOUT_S)`. On timeout: logs WARN, increments `yadgar_hook_recall_timeout_total{handler=<name>}`, returns empty. The recall runs in a DEDICATED BOUNDED `ThreadPoolExecutor` of `HOOK_RECALL_POOL_WORKERS` threads (not `asyncio.to_thread`'s unbounded default executor) — since `run_in_executor` work is uncancellable, bounding the threads prevents a slow leaked recall from cascading into event-loop starvation → P0 SIGKILL (#81 / ADR-0022). LIVE because hooks are always installed in production.
- **explanation:** Bounds both the LATENCY and the CONCURRENCY of auto-recall inside hook handlers. `HOOK_RECALL_TIMEOUT_S` (default 2.0 s) is the hard per-call deadline. `HOOK_RECALL_POOL_WORKERS` (default 2 since ADR-0077 — post-#166 the hook recall is a forwarded HTTP wait, an idle thread rather than a GIL-holding in-core recall, and pool=1 starved the second of every concurrent session pair) caps how many recall threads ever run at once: on the `--cpus 1` core, fewer threads compete less with the event loop, so a box-saturated slow recall cannot starve the loop into a freeze. Read once at import (restart to apply). Both are observable/tunable so operators can trade latency vs. freeze-safety.

### CAP-OPS-032 — query-routing code and relational keyword lists
- **status:** LIVE
- **category:** ops
- **settings:** `CODE_KEYWORDS`, `RELATIONAL_KEYWORDS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/retrieval/query_analysis.py`
- **wiring:** `CODE_KEYWORDS` and `RELATIONAL_KEYWORDS` are comma-separated keyword lists loaded via `Settings` and used by the query router (`query_analysis.py`) to classify an incoming `recall()` query into one of: code, relational, temporal, open-domain, or comparison profile. Classification drives retrieval profile selection and signal weighting.
- **explanation:** The query router tokenises the query and checks for membership in the configured keyword lists to determine query type. Code queries (containing function/class/API terms) and relational queries (containing relationship/causal terms) route to specialised retrieval sub-pipelines optimised for their patterns. The lists are configurable so operators can tune routing precision for domain-specific vocabularies without changing code.

### CAP-OPS-033 — BC-A1/A2/A3: memorize write guarantees
- **status:** LIVE
- **category:** write-path
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-A1`, `BC-A2`, `BC-A3`
- **refs:** `yadgar/core/server/tools/memorize.py`, `yadgar/backend/queue_drainer/apply.py`
- **wiring:** `memorize()` → file-queue enqueue → drainer apply → DB write. BC-A1: the drainer stamps `directory_context` from the enqueued payload, making the memory retrievable by directory. BC-A2: the write-gate evaluates novelty; near-identical content is deduplicated. BC-A3: the drainer calls the embedding service for every committed memory; on failure, the row is stored with null embedding (unless `WIKI_EMBED_FAILURE_BLOCKS_WRITE=True`).
- **explanation:** These three behaviour-contract rows describe the three core memorize guarantees. BC-A1 ensures directory-stamped retrievability: `memorize(content, context=D)` always stores with `directory_context=D`. BC-A2 ensures deduplication: the write-gate rejects near-identical content (high similarity to existing memories). BC-A3 ensures embedding coverage: every committed memory has an embedding generated at write time, enabling similarity search during consolidation and retrieval.

### CAP-OPS-034 — forget and validate_memory: individual memory lifecycle ops
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-ADM2`, `BC-ADM3`
- **refs:** `yadgar/core/server/tools/admin_other.py::forget`, `yadgar/core/server/tools/admin_other.py::validate_memory`
- **wiring:** `forget(memory_id)` is a non-power `@_tool()` → `storage.delete_memory(memory_id)` directly (synchronous, not queued). `validate_memory(memory_id)` (power-gated) → `_st._staleness.validate_memory(memory_id)` if staleness detector is active, else falls back to `_file_hash(directory_context)` comparison. Both are called directly by MCP clients.
- **explanation:** `forget()` permanently deletes a memory record by ID: loads it to confirm existence, then calls `storage.delete_memory()` returning `{memory_id, status: "deleted"}` or `"not_found"` (BC-ADM2). `validate_memory()` checks whether a file-backed memory's stored hash still matches the file on disk: if the file is gone the memory is marked stale; if the hash differs it is marked stale; if it matches the memory is valid. This catches the fallback bug where file-backed memories accumulated without staleness detection (BC-ADM3).

### CAP-OPS-035 — memory_update: field-level memory patching
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-ADM6`
- **refs:** `yadgar/core/server/tools/admin_other.py::memory_update`, `yadgar/backend/admin_exec/memory.py::memory_update`
- **wiring:** MCP client → `memory_update(memory_id, fields)` (power-gated) → validates `fields` against `_MEMORY_UPDATE_ALLOWED = {"content", "tags", "is_protected", "is_stale", "importance", "tier", "project_id"}` → forwards to the backend `memory_update` admin op → `update_memory_fields(memory_id, **fields)`. Car 2 (Part B) added a CONTENT-CHANGE re-embed guard: when `content` is in `fields` AND differs from the stored content, the backend re-encodes the new content and calls `update_memory_embedding` so the vector stays coherent with the text (fixes the stale-vector latent bug). Metadata-only patches (tags/is_protected/is_stale, or a same-value content) skip the re-embed. Ledger task 262 added `project_id` plus a SHAPE gate on its value (`_project_id_update_error`): non-empty string, and not one of `_NON_IDENTIFYING_PROJECT_IDS` — the same authority the wiki half of the fix reads (ledger task 246, branch `fix/wiki-set-metadata-project-id` @ `6fa99512`, not yet on master). No registry check runs on this correction path; the memory CREATE path gates on `assert_project_registered_for_create` (Car 5) instead.
- **explanation:** Provides surgical patching of individual memory fields without a full delete-and-recreate cycle (BC-ADM6). Allowed fields are `content`, `tags`, `is_protected`, `is_stale`, `importance`, `tier`, and `project_id`; structural fields (`heat`, `embedding`, `id`, `created_at`) and unknown keys are rejected with a `ValueError` listing the allowed set. Empty `fields` is a no-op read (returns current state). Before Car 2 a content patch left the OLD embedding in place, so a semantically-changed memory stayed unfindable by its new text; the content-change re-embed guard closes that. Embedding bytes are stripped from the response to keep MCP payloads small. `project_id` is the memory half of the `wiki_set_metadata` fix (ledger task 246, merged as `b96360fd`, PR #60): it is the sole memory scoping key (`build_project_scope_clause` narrows on `project_id = $p OR <reach-tag> IN tags`), and until task 262 a row stamped with the wrong project was unreachable from every project-scoped read with no correction path through any MCP surface — which is what blocked the corpus re-key (ledger task 41) on the memory side.

### CAP-OPS-036 — memorize soft-gate: non-blocking near-duplicate surface (car 2)
- **status:** LIVE
- **category:** ops
- **settings:** `MEMORIZE_SIM_GATE_ENABLED`, `MEMORIZE_SIM_THRESHOLD`, `MEMORIZE_SIM_TOP_K`
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/write_exec/memorize_impl.py`, `yadgar/backend/write_exec/_memorize_phases/_phase_contradiction.py`
- **wiring:** During the backend `run_memorize_replay` pipeline, after `phase_embed` computes the content vector, `phase_soft_gate` runs for DURABLE writes only — triggered on caller-settable signals `tags ∩ {feedback, decision, _anchor}` OR `is_protected=True` OR any `tier` set (NOT `store_type`, which is "episodic" at gate time and set by the CLS classifier post-gate). It runs a KNN over existing memory vectors at `MEMORIZE_SIM_THRESHOLD` (default 0.85) and attaches `near_duplicates: [{id, content, score}]` (up to `MEMORIZE_SIM_TOP_K`) to the DRAINER-side replay RESULT + emits an INFO log, WITHOUT blocking the store. Gate is a no-op when `MEMORIZE_SIM_GATE_ENABLED=false` or the write is episodic (none of the durable signals present).
- **explanation:** The write-side counterpart to the read-first-write discipline: when a durable memory (feedback rule, decision, anchor) is stored near an existing one, the gate surfaces the near-duplicates so redundant memories can be spotted (and UPDATE-in-place via `memory_update` chosen over accumulation). Deliberately NON-BLOCKING (mirrors the wiki 0.80 gate's calibration methodology but never rejects) — the memory always stores; the dups are advisory. **SURFACING (honest scope):** `memorize` is async — the MCP call enqueues and returns `{queued: True}` BEFORE the drainer runs the gate, so `near_duplicates` lands in the async drainer-replay result (job archive) + the INFO log, NOT in the synchronous MCP return. This is observability-grade in v5.141.0; a synchronous surface to the caller (e.g. a `memorize(wait=True)` return) is a follow-up. Episodic writes bypass entirely to keep the hot episodic path cheap. Threshold is a configurable knob (0.85, stricter than the wiki 0.80, because memories are shorter/noisier).

### CAP-OPS-036 — Migration HTTP timeout
- **status:** LIVE
- **category:** ops
- **settings:** `MIGRATION_HTTP_TIMEOUT_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/storage/__init__.py`
- **wiring:** `StorageEngine.__init__()` applies schema migrations by issuing HTTP requests to the SurrealDB backend. Each migration request uses `MIGRATION_HTTP_TIMEOUT_SEC` (default 30 s) as the httpx timeout, distinct from the operational `BACKEND_HTTP_TIMEOUT_SEC` (5 s). This is set once at startup during `StorageEngine` initialisation.
- **explanation:** Schema migrations (DDL statements, backfill queries) can take significantly longer than operational read/write queries due to lock contention and large-table scans. `MIGRATION_HTTP_TIMEOUT_SEC` (default 30 s) gives migrations a longer deadline than the operational 5 s cap, preventing spurious migration failures on large databases or loaded backends without also allowing operational queries to time out slowly.

### CAP-OPS-037 — recent_memories: time-ranked memory surface without classifier
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `recent_memories`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/admin_other.py::recent_memories`, `yadgar/_shared/storage/memory.py::get_recent_memories_since`
- **wiring:** MCP client → `recent_memories(limit, since, directory)` → `_parse_since_duration(since)` converts duration string or ISO cutoff → `storage.get_recent_memories_since(since, limit, directory)` → returns rows ordered by `created_at DESC`. `limit` is capped at 100; `since` accepts `'24h'`, `'7d'`, `'30m'` duration strings or ISO-8601 UTC datetime; `directory='global'` or empty queries all directories. Content truncated to 300 chars per entry. Also feeds `_project_brief_restore()` via `_build_recent_writes()` to populate the `recent_writes` section of `restore` output.
- **explanation:** Surfaces recently stored memories ordered by creation time without invoking the embedding/classifier pipeline. Useful after context compaction (when `restore` has already fired) to inspect what was written in the last N hours. The `restore` tool's output now includes a `recent_writes` section (last 10 memories in last 24h) built from this storage method, helping agents reconstruct work that was stored just before compaction. `memorize()` responses also now include an explicit `memory_id` field alongside the full memory dict for stable programmatic access.

### CAP-OPS-038 — /health endpoint: 200-ok / 503-degraded contract
- **status:** LIVE
- **category:** observability
- **settings:** `HEALTH_HANDLER_TIMEOUT_SEC`, `HEALTH_PROBE_TIMEOUT_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/http.py::health_check`, `yadgar/core/server/http.py::_build_health_payload`, `yadgar/core/daemon/daemon.py`, `docker-compose.yml`
- **wiring:** `GET /health` (auth-exempt) builds a payload that probes the `YADGAR_DB_URL` and `YADGAR_EMBED_URL` dependencies CONCURRENTLY (`asyncio.gather`, ~2 s vs the old ~4 s serial) and is bounded by an outer `asyncio.wait_for(_HEALTH_TIMEOUT_SEC=3.0)`. If any probe fails (or the outer bound trips) `status="degraded"`. **The handler returns HTTP 200 only when `status=="ok"`, and HTTP 503 on any non-ok status** (v5.83 obs-train, C1). The handler is STATELESS — anti-flap is delegated to the container healthcheck retries (`docker-compose.yml` core service: `interval 10s`, `timeout 5s`, `retries 6`, so ~60 s of sustained degradation before the container is marked unhealthy), NOT an in-handler failure counter. Consumer side (`yadgar/core/daemon/daemon.py`): `status()` reads the `HTTPError` body on a 503 and shows the degraded detail (not "unreachable"); `_health_ok()` treats a responding-but-503 server as ALIVE — liveness (server responding) is distinct from full-health/readiness (db+embed ok), which the container `curl -f` healthcheck enforces.
- **explanation:** Before v5.83 the endpoint always returned 200 even when degraded, so the container `curl -f` healthcheck read a db/embed outage as healthy (the outage-masking false-negative). 503-on-degraded makes the healthcheck actually detect outages. The 3 s handler cap sits inside the compose `timeout: 5s` with margin; the compose `urlopen(..., timeout=2)` inner probe and `retries: 6` already meet/exceed the nix healthcheck intent (interval 15 s / timeout 8 s / retries 3), so no compose change was needed — a 503 raises `HTTPError` → non-zero exit → correctly unhealthy. Tested by `yadgar/tests/test_transport.py` (`test_health_returns_503_when_db_probe_degraded`, `test_health_returns_200_when_probes_ok`, `test_health_outer_timeout_trips_503_on_hang`, concurrent-probe timing) and the daemon-side 503-tolerance in `yadgar/tests/test_daemon_module.py`.

### CAP-OPS-039 — Tool-body offload off the asyncio loop (Fix A)
- **status:** LIVE
- **category:** ops
- **settings:** `OFFLOAD_TOOLS`, `TOOL_POOL_WORKERS`, `TOOL_TIMEOUT_SEC`, `TOOL_SATURATION_GRACE_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/runtime/offload.py`, `yadgar/core/server/_app.py::_tool`, `yadgar/_shared/runtime/lifecycle.py`, `yadgar/core/server/http.py::_apply_tool_pool_health`
- **wiring:** `_app._instrumented` is `async def`; when `OFFLOAD_TOOLS` is on it dispatches the trace-wrapped sync tool body onto a bounded `ThreadPoolExecutor(max_workers=TOOL_POOL_WORKERS)` via `loop.run_in_executor`, wrapped in `asyncio.wait_for(TOOL_TIMEOUT_SEC)`, so the asyncio loop stays free to serve `/health`. The in-flight counter is decremented on the WORKER thread at true completion (not coroutine-side) so `pool_saturated()` reflects true occupancy even when a `wait_for` times out and the worker keeps its slot. `/health` (`_apply_tool_pool_health`) goes `status=degraded` → 503 when the pool is saturated for > `TOOL_SATURATION_GRACE_SEC` (completion-staleness), so the P0 `curl -f` health-kill can catch a pool-dead-but-loop-alive daemon (O2). Lifecycle asserts remote engines when offload is on (Claim-1) and tears the pool down on shutdown (O10).
- **explanation:** Fix A (daemon-offload-A) removes the *cause* of the v5.88 core daemon hang: ~60 sync MCP tool bodies ran INLINE on the single asyncio loop (FastMCP `func_metadata.py:92` sync branch), so a blocking body (the proven inline `git` subprocess) froze the loop and starved `/health`. Default OFF for the first release (proven trigger stays inline, covered by the deployed P0 health-kill); flipped ON after live soak. `TOOL_SATURATION_GRACE_SEC` MUST exceed `TOOL_TIMEOUT_SEC` so legitimate ops keep resetting the staleness clock and only leaked workers trip the O2 503 signal. `TOOL_POOL_WORKERS` (v5.95: default 8→2) is bounded to minimize CPU competition on the `--cpus 1` core; kept strictly greater than `RECALL_HEAVY_CONCURRENCY` to preserve the rerank fan-out gate (CAP-OPS-040).

### CAP-OPS-040 — Offload salvage: liveness/readiness split + rerank fan-out gate (#74)
- **status:** LIVE
- **category:** ops
- **settings:** `RECALL_HEAVY_CONCURRENCY`, `RERANK_GATE_ACQUIRE_TIMEOUT_SEC`, `HEALTH_READINESS_FAIL_THRESHOLD`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/runtime/offload.py::acquire_rerank_slot`, `yadgar/backend/ml_client/ml_client.py::RemoteMLClient._rerank_rpc`, `yadgar/core/server/http.py::liveness_check`, `yadgar/core/server/http.py::_build_health_payload`, `yadgar/core/auth_middleware/auth_middleware.py::_EXEMPT_PATHS`
- **wiring:** A process-singleton `threading.Semaphore(RECALL_HEAVY_CONCURRENCY)` in `_offload` gates every backend `/rerank` POST issued by `RemoteMLClient._rerank_rpc` (HALF_OPEN breaker probes bypass it; a gate-acquire timeout of `RERANK_GATE_ACQUIRE_TIMEOUT_SEC` degrades to `None` → pre-rerank order, reusing the breaker-open path — never blocking a worker on the gate past the tool timeout). `RECALL_HEAVY_CONCURRENCY` defaults to 1 (v5.95: 3→1, in lockstep with `TOOL_POOL_WORKERS` 8→2), strictly below `TOOL_POOL_WORKERS` (2), so N offload workers cannot drive N concurrent backend reranks. The new `GET /health/live` (`liveness_check`, auth-exempt) answers from the loop alone — 200 normally, 503 ONLY when `pool_saturated()` (in-memory counters, no backend probe) — and is what the container P0 `--health-on-failure=kill` healthcheck watches. `GET /health` (readiness) keeps the db+embed probe but anti-flaps: it degrades to 503 only after `HEALTH_READINESS_FAIL_THRESHOLD` CONSECUTIVE probe misses (a single success resets), so a transiently-busy backend can't 503-storm.
- **explanation:** #74 — with `OFFLOAD_TOOLS` on, the freed loop let up to `TOOL_POOL_WORKERS` (was 8, now 2 after v5.95) concurrent recalls drive concurrent backend `/rerank` calls. The backend (fewer cores than 8) saturated → its `/health` slowed → the core's readiness `/health` probed it with a 2s timeout → timed out → core returned 503 → the container `curl -f` healthcheck failed → P0 `--health-on-failure=kill` SIGKILLed the core → restart loop. Two coupled defects: liveness was coupled to a busy dependency, and the rerank fan-out was unbounded. The fix decouples liveness from the backend (P0 watches `/health/live`, which never probes the backend, so backend busyness can never SIGKILL the core) while preserving the O2 P0-kill (a genuinely wedged pool still trips `pool_saturated()` → liveness 503), anti-flaps readiness (monitoring signal, no longer the P0 trigger), bounds the fan-out to the backend's real serving capacity, and reconciles the timeout cascade (`TOOL_SATURATION_GRACE_SEC` > `TOOL_TIMEOUT_SEC` ≥ `RERANK_BACKEND_TIMEOUT_SEC`) so a `wait_for` cancellation can't leak an uncancellable worker mid-rerank.

### CAP-OPS-041 — Recall side-effect fork (both halves off the response path)
- **status:** LIVE
- **category:** ops
- **settings:** `RECALL_SIDEEFFECT_FORK`, `RECALL_SIDEEFFECT_SESSION_MAX_PENDING`, `RECALL_SIDEEFFECT_DB_MAX_INFLIGHT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/runtime/recall_side_effects_fork.py`, `yadgar/backend/embed_service/embed_service.py::recall_route`, `yadgar/backend/retrieval/recall_pipeline.py::_compute_db_boost`, `yadgar/core/server/tools/recall.py`, `yadgar/_shared/runtime/lifecycle.py::shutdown`
- **wiring:** T3 Car 2 forks BOTH inline recall side-effect halves off the tool-response critical path when `RECALL_SIDEEFFECT_FORK` is on (default). BACKEND DB half: `recall_route` runs `_compute_db_boost` INLINE (the in-place heat/`last_accessed` mutations that feed the response — byte-identical), then forks the batched `storage.boost_memories_access` write via `schedule_db_write` as a tracked `asyncio.create_task` (contextvars carry the request span → `recall.side_effects.db` nests). Over `RECALL_SIDEEFFECT_DB_MAX_INFLIGHT` (default 64) tracked tasks, `schedule_db_write` returns False and the caller awaits the same coroutine inline. Drained at the FastAPI lifespan teardown (`drain_db_tasks`) BEFORE `_stop_queue_drainer`/surreal stop (#181 writers-stop seam). CORE SESSION half: `recall()` defers `_apply_recall_session_side_effects` (SR-transition storage writes + action buffer + replay tick) via `submit_session_side_effect` onto a single-worker `ThreadPoolExecutor` (max_workers=1 → global FIFO preserves the per-session SR chain order; `contextvars.copy_context().run()` carries the OTEL span across the executor boundary). Over `RECALL_SIDEEFFECT_SESSION_MAX_PENDING` (default 64) queued items, submit runs the work inline (backpressure). Drained in `lifecycle.shutdown` (`drain_session_side_effects`) BEFORE `storage.close()`.
- **explanation:** Both side-effect halves were inline on the recall latency path — the backend batched heat DB write (the ~407ms recall tail) and the core session SR-transition storage writes (I/O on the 1-CPU core; `incremental_update` is a documented no-op on the core `SRTransitionRecorder` since T2 Car B). The fork removes their latency from the tool response while holding the must-holds: side-effects always execute (drained on shutdown, backpressure-inline never drops), errors are logged not raised, task pile-up is bounded, OTEL span parentage is preserved across each fork boundary, and per-session SR ordering is kept via the single FIFO worker. The response payload stays byte-identical because the backend heat mutations that feed it stay inline (only the DB WRITE is forked). Flip `RECALL_SIDEEFFECT_FORK` False to restore inline behavior.

### CAP-OPS-042 — CPU-aware, parallel-ready recall pipeline
- **status:** LIVE
- **category:** ops
- **settings:** `RECALL_PARALLELISM`, `AVAILABLE_CPUS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/runtime/cpu.py`, `yadgar/backend/retrieval/recall_pipeline.py::_gather_provider_candidates`, `yadgar/_shared/runtime/offload.py::_heavy_concurrency`, `yadgar/backend/embed_service/embed_service.py::lifespan`
- **wiring:** T3 Car 3 makes every recall concurrency budget a pure function of `available_cpus()` (`yadgar/_shared/runtime/cpu.py`) — cgroup-v2 `cpu.max` quota → cgroup-v1 `cpu.cfs_quota_us`/`cpu.cfs_period_us` → `os.cpu_count()`, cached, floored to ≥ 1. `os.cpu_count()` reports the HOST cores in a cgroup-limited container, so the quota is read first. `AVAILABLE_CPUS` (default 0 = auto-detect) pins the count when the cgroup read is unavailable/wrong. `RECALL_PARALLELISM` (default `auto`; `1` = force sequential) is the no-thrash / ops escape hatch. Budgets: `recall_gather_budget()` = 1 at ncpu ≤ 2 (sequential provider gather — byte-identical to the pre-Car-3 inline calls) else `min(ncpu-1, 2 providers)`; `_fanout_recall` runs the memory + wiki `.candidates()` storage-I/O calls through `_gather_provider_candidates` (a bounded `ThreadPoolExecutor(max_workers=min(budget, ntasks))`, results keyed by slot name so completion order never reorders the pools). `torch_intraop_threads()` = 1 at ncpu ≤ 2 else `ncpu//2`, applied process-global via `torch.set_num_threads` at the backend `lifespan`. `RECALL_HEAVY_CONCURRENCY` default flipped to the sentinel `0` = auto (CAP-OPS-040), resolved to `recall_heavy_concurrency_default()` in `_heavy_concurrency`. Composition: gather_arms (≤ 2) × torch_threads (≤ ncpu//2) ≤ ncpu (no oversubscription).
- **explanation:** User decision 2026-07-11 (option B, capability-first): build the parallel substrate NOW so raising the backend `--cpus` fans the pipeline out WITHOUT another code change. At ≤ 2 CPUs behavior is byte-identical to today (every budget floors to 1 → the existing sequential path); at more CPUs the provider fan-out and torch intra-op threads scale from the single CPU-derived source. Only genuinely GIL-releasing work is parallelized (provider storage I/O); GIL-bound graph compute (fusion/PPR/spreading) is untouched. Bounded pools everywhere (the ADR-0011-class onnx-thrash lesson); `RECALL_PARALLELISM=1` forces sequential for ops. Concurrent-CE / model-replication is DEFERRED (needs replicated model instances or a batching server; revisit at > 2 backend CPUs post-Ettin).

### CAP-OPS-043 — Config-panel destructive-knob 428 armed gate + JSONL config-audit + restart rate-limit
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/routes/control_audit.py::is_destructive`, `yadgar/core/server/routes/control_audit.py::audit_config_event`, `yadgar/core/server/routes/control_audit.py::restart_rate_limited`, `yadgar/core/server/routes/control.py::control_config_post_handler`, `yadgar/core/server/routes/control.py::control_restart_handler`, `yadgar/_shared/config/config_yaml.py::FIELD_META`
- **wiring:** Config-panel Car D adds a `"destructive": True` metadata key to five retention/purge FIELD_META knobs (`memory_archive_retention_days`, `cold_memory_purge_enabled`, `cold_memory_purge_dry_run`, `queue_dlq_retention_days`, `action_log_retention_days`) — an additive per-knob dict key (the `"choices"` precedent), invisible to the I25 three-way-sync lint (no new Settings field). `_enrich_knob` surfaces it on GET `/api/control/config`; `control_config_post_handler` gates the write via `control_audit.is_destructive` — a destructive knob without `"armed": true` in the body is refused **428** AFTER the write-blocked 400 + env-lock 409 security guards (never before them; the POST does its own FIELD_META lookup because it never calls `_enrich_knob`). Every config write / restart / action appends ONE JSONL line to `$XDG_STATE_HOME/yadgar/config-audit.jsonl` via `audit_config_event` (a dedicated `propagate=False` logger + `RotatingJSONLFileHandler` rebuilt when the resolved state dir changes; fields ride `extra=` as top-level I14 keys, the knob emitted as `knob` since `name` is a reserved LogRecord attr). `control_restart_handler` checks confirm-mismatch (400) FIRST, then the in-memory `restart_rate_limited` (429, 30s monotonic window), stamping the window ONLY on a successful sentinel write (a mismatch never consumes it). Frontend (`control.js` + `control_helpers.js`): destructive rows render a `.destructive` class + ⚠ marker + a typed-confirm arm input (type the knob name to arm; control disabled until armed), `applyOne` POSTs `{armed:true}` and treats a 428 defensively as needs-arming, and the pending bar shows the destructive count.
- **explanation:** The config panel can write knobs that permanently delete data (retention windows, cold-memory purge, DLQ pruning). Car D adds an accidental-destruction guard (a 428 typed-confirm arm), a forensic JSONL audit trail of every config-surface mutation attempt (success AND refusal, with old→new + best-effort client host/UA), and a single-process restart flood guard. ACTOR IDENTITY (ADR-0013): Bearer auth carries no principal, so the audit actor is best-effort remote-addr + User-Agent, NOT an authenticated identity. No new Settings field, no new MCP tool, no migration — the destructive flag is FIELD_META metadata, the rate-limit window is a module constant, and the audit path is derived from `XDG_STATE_HOME`. Sentinel-only restart mechanism (writes a file, never execs) is preserved unchanged.

### CAP-OPS-044 — Sanctioned read-only DB inspection surface (`db_inspect` / `POST /api/debug/read_query`)
- **status:** DORMANT
- **category:** ops
- **settings:** `DEBUG_APIS_ENABLED`
- **tools:** `db_inspect`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/storage/__init__.py::_resolve_ro_db_credentials`, `yadgar/_shared/storage/__init__.py::StorageEngine._get_ro_http`, `yadgar/_shared/storage/client.py::_ClientMixin._q_ro`, `yadgar/backend/embed_service/embed_service_routes.py::read_query_route`, `yadgar/core/server/routes/debug_query.py::read_query_handler`, `yadgar/core/forward.py::_forward_read_query`, `yadgar/core/server/tools/db_inspect.py::db_inspect`
- **wiring:** `db_inspect(query, params, limit)` MCP tool (core) re-checks `YADGAR_DEBUG_APIS_ENABLED` itself (the MCP call bypasses the HTTP auth middleware) → `_forward_read_query` → backend `POST /read_query` (`Depends(_require_admin_token)`) → `_get_storage()._q_ro(surql, params, timeout_ms=...)` on the VIEWER-authed RO httpx client (`_get_ro_http`, built from `YADGAR_RO_USER`/`YADGAR_RO_PASS`). The `POST /api/debug/read_query` core route is the HTTP twin: bearer + `YADGAR_DEBUG_APIS_ENABLED`-gated via `_DEBUG_API_PREFIXES` (auth_middleware) → same `_forward_read_query`. Backend parse-guard (`_contains_write_keyword`) rejects write keywords with 400 (defense-in-depth). Row-cap 500 (`_RO_QUERY_ROW_CAP`, hard ceiling) + per-call timeout are module constants (not knobs).
- **explanation:** A compliant read-only ad-hoc query surface against SurrealDB for debugging (ADR-0132, the named ADR-0078 debug read path). Safety is by construction: the query runs on a read-only VIEWER-role DB connection, so a write does NOT persist regardless of query text (verified by read-back — SurrealDB VIEWER may silently no-op a record write or hard-error, but nothing persists) — the keyword parse-guard is defense-in-depth only (SurrealQL is multi-statement, defeating a naive prefix check). Core touches zero DB (forwards HTTP only). DORMANT: gated OFF by default (`YADGAR_DEBUG_APIS_ENABLED` unset) — a single flag flip turns it on; row-cap + timeout bound the blast radius even when enabled.

### CAP-OPS-045 — Bounded wait-for-backend gate at core startup
- **status:** LIVE
- **category:** ops
- **settings:** `BACKEND_READY_WAIT_SEC`, `BACKEND_READY_POLL_SEC`, `BACKEND_READY_POLL_MAX_SEC`, `BACKEND_READY_LONG_BAKE_OUT_AFTER`, `BACKEND_READY_LONG_BAKE_OUT_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/bootstrap/backend_ready.py::await_backend_ready`, `yadgar/core/bootstrap/bootstrap.py::core_init_engines`, `yadgar/core/server/_startup.py::main`
- **wiring:** `core_init_engines()` calls `await_backend_ready()` BEFORE delegating to `lifecycle.init_engines` (full engine set only), so it runs before `StorageEngine.__init__` — whose inline `_init_schema()` issues HTTP immediately. The gate GETs `${YADGAR_EMBED_URL}/health` for up to `BACKEND_READY_WAIT_SEC` (default 60 s), counting only HTTP 200 as ready. Probing cadence: starts at `BACKEND_READY_POLL_SEC` (default 2.0 s) and grows exponentially per consecutive failure, capped at `BACKEND_READY_POLL_MAX_SEC` (default 30 s). Once `BACKEND_READY_LONG_BAKE_OUT_AFTER` (default 5) consecutive probes have failed, the loop enters long-bake-out: one `long-bake-out` INFO log line, then a coarse per-probe sleep of `min(BACKEND_READY_LONG_BAKE_OUT_SEC, BACKEND_READY_POLL_MAX_SEC)` until the budget expires. The clamp is Car-J (ledger #367), so one 60 s sleep cannot drain the host CPU budget — at the 60/30 defaults the configured value has no effect on the sleep and only shortens it when set below `POLL_MAX_SEC`; the audit line names both the configured cadence and the honoured one so a journal reader can tell them apart. One INFO line per attempt — `journalctl` can answer "down for minutes" vs a probe storm from a single grep. On exhaustion it raises `BackendNotReadyError`, which `main()` converts into a one-line non-zero `SystemExit` naming the URL. No-op when `YADGAR_EMBED_URL` is unset (local/in-process engines) or `BACKEND_READY_WAIT_SEC=0` (escape hatch).
- **explanation:** Without the gate, a core started while the backend was down raised `httpx.ConnectError` out of the storage constructor in ~1 s; `Restart=on-failure` + `RestartSec=5` restarted it into the identical failure, and the ~6 s cycle stayed under systemd's default `StartLimitBurst`, so the unit looped indefinitely instead of failing. The fixed-interval half of the original gate (60 s budget × 30 probes × fixed 2 s cadence) ALSO drained the host CPU budget during a long backend outage — every probe was a fresh `Restart=on-failure` cycle of the core unit, and `journalctl` filled with `INFO waiting for backend` lines indistinguishable from a transient network hiccup. Task #61 swaps the cadence for bounded exponential backoff (cap 30 s) + long-bake-out (a coarse sleep after 5 failures, with one audit log line) so a 10-minute outage runs ~24 probes instead of ~300 AND a journal audit can grep one `long-bake-out` line out of the per-probe spam. Startup-only — the runtime request path already handles per-call connection errors. Placement is load-bearing: the BACKEND's own bootstrap calls `lifecycle.init_engines(engine_set="slim")` directly, so a gate one level down would make the backend poll its own `/health` during its own startup (guarded by `test_backend_slim_bootstrap_does_not_wait_for_itself`). `/health` (200 ⇔ `db_ok and engine_loaded`) is the correct signal because it is exactly the precondition schema init has; `daemon._embed_health_ok` is deliberately weaker (any HTTP response counts) and is NOT reused. The 60 s budget is arithmetic, not a measurement — it must stay strictly inside the core unit's `TimeoutStartSec=120` or a slow-but-fine start becomes a timeout kill (asserted as a relation against the rendered unit). This is NOT the cold-boot mechanism; `After=yadgar-backend.service` is.

### CAP-VIZ-001 — Wiki node category colors

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_CAT_COLOR_ANALYSIS`, `VIZ_CAT_COLOR_ARCHITECTURE`, `VIZ_CAT_COLOR_CONVENTION`, `VIZ_CAT_COLOR_DEBUGGING`, `VIZ_CAT_COLOR_DECISION`, `VIZ_CAT_COLOR_FACT`, `VIZ_CAT_COLOR_PATTERN`, `VIZ_CAT_COLOR_REFERENCE`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/viz/viz_meta.py::build_category_colors`, `yadgar/core/server/http.py::api_viz_config`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → `build_category_colors(settings)` iterates `WikiStore.CATEGORIES`, calls `getattr(settings, f"VIZ_CAT_COLOR_{cat.upper()}", fallback)` for each. Result returned under `node.category_colors` in the JSON response. Frontend fetches on init and assigns to `YADGAR_VIZ_CONFIG.node.category_colors`; wiki nodes are colored by category at render time.
- **explanation:** Eight settings, one per wiki category (analysis, architecture, convention, debugging, decision, fact, pattern, reference), define the hex color used to fill wiki nodes in the force graph. `build_category_colors` builds the map dynamically by iterating the canonical `WikiStore.CATEGORIES` set rather than a hardcoded list, so new categories get an automatic grey fallback (`#8b949e`) without code changes. Colors flow to the frontend via `GET /api/viz/config` and are applied at render time in the 2D canvas and 3D ForceGraph renderers.

### CAP-VIZ-002 — Edge colors

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_EDGE_COLOR_MEMORY_WIKI`, `VIZ_EDGE_COLOR_SEMANTIC`, `VIZ_EDGE_COLOR_TEMPORAL`, `VIZ_EDGE_COLOR_TRANSITION`, `VIZ_EDGE_COLOR_WIKI_CROSSREF`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/viz/viz_meta.py::build_edge_colors`, `yadgar/core/viz/viz_meta.py::EDGE_TYPES`, `yadgar/core/server/http.py::api_viz_config`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → `build_edge_colors(settings)` iterates `EDGE_TYPES`; for entries with a `settings_color_key` it reads the matching `Settings` attribute, otherwise uses the fallback color. Result returned under `edge.color` in the config JSON. Frontend stores in `YADGAR_VIZ_CONFIG.edge.color` and applies per edge type at render time.
- **explanation:** Five settings control the display color (hex) for the five named edge types whose color is configurable: `memory_wiki` (memory→wiki provenance), `semantic` (cosine-similarity), `temporal` (engram slot co-membership), `transition` (co-recall pattern), and `wiki_crossref` (explicit wiki page links). Edge types without a `settings_color_key` (causal, co_occurrence, imports, calls, resolved_by, caused_by) use hardcoded fallback colors defined in `EDGE_TYPES`. All five configurable colors are served via `GET /api/viz/config` and consumed by the ForceGraph2D/3D link-color accessor in `index.html`.

### CAP-VIZ-003 — Edge styling (opacity, arrow length, 3D width multiplier, variant)

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_EDGE_ARROW_LEN`, `VIZ_EDGE_OPACITY`, `VIZ_EDGE_WIDTH_3D_MULTIPLIER`, `VIZ_EDGE_VARIANT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/http.py::api_viz_config`, `yadgar/core/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → reads `s.VIZ_EDGE_ARROW_LEN`, `s.VIZ_EDGE_OPACITY`, `s.VIZ_EDGE_WIDTH_3D_MULTIPLIER`, `s.VIZ_EDGE_VARIANT` → returned under `edge.*`. Frontend assigns to `YADGAR_VIZ_CONFIG.edge` and applies: `linkOpacity(opacity)`, `linkWidth(l => _linkWidth(l) * width_3d_multiplier)` (3D only), `arrowLen` via `_arrowLen(l)` (transition/memory_wiki/wiki_crossref edges only), and `variant` is informational (logged in the debug info tab).
- **explanation:** Four settings govern overall edge rendering style. `VIZ_EDGE_OPACITY` sets the global link opacity for all edges (default 0.9, Variant C). `VIZ_EDGE_WIDTH_3D_MULTIPLIER` scales the computed link width in 3D mode only. `VIZ_EDGE_ARROW_LEN` sets the directional-arrow length for transition, memory_wiki, and wiki_crossref edge types (others use zero). `VIZ_EDGE_VARIANT` is a string label ("C") for the current edge styling scheme; it appears in the debug info tab but has no functional effect on rendering.

### CAP-VIZ-004 — Heat-to-HSL color mapping for memory/entity nodes

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_HEAT_HUE_END`, `VIZ_HEAT_HUE_START`, `VIZ_HEAT_LIGHT_BASE`, `VIZ_HEAT_LIGHT_GAIN`, `VIZ_HEAT_SAT_BASE`, `VIZ_HEAT_SAT_GAIN`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/http.py::api_viz_config`, `yadgar/core/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns all six heat parameters under `node.heat`. Frontend receives them into `YADGAR_VIZ_CONFIG.node.heat`; the `heatToHsl(h)` function (line ~1530 of `index.html`) computes `hsl((1-h)*hue_start + h*hue_end, sat_base + h*sat_gain%, light_base + h*light_gain%)` for every memory/entity node at render time.
- **explanation:** Six settings parameterise the continuous heat-to-colour gradient applied to memory and entity nodes. `VIZ_HEAT_HUE_START` (default 240 = blue) and `VIZ_HEAT_HUE_END` (default 0 = red) define the hue extremes for cold (h=0) and hot (h=1) nodes. `VIZ_HEAT_SAT_BASE`/`VIZ_HEAT_SAT_GAIN` and `VIZ_HEAT_LIGHT_BASE`/`VIZ_HEAT_LIGHT_GAIN` control the HSL saturation and lightness as linear functions of heat. Wiki nodes are unaffected (they use category colors).

### CAP-VIZ-005 — Node sizing (2D and 3D)

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_NODE_SIZE_2D`, `VIZ_NODE_SIZE_3D`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/http.py::api_viz_config`, `yadgar/core/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `size_3d` and `size_2d` under `node`. Frontend assigns: `graph.nodeRelSize(YADGAR_VIZ_CONFIG.node.size_3d)` for ForceGraph3D and uses `size_2d` as the base radius in the 2D canvas draw callback.
- **explanation:** Two settings control node sphere size: `VIZ_NODE_SIZE_3D` (default 8.0) sets `nodeRelSize` in the 3D ForceGraph renderer (2× the ForceGraph3D library default of 4), and `VIZ_NODE_SIZE_2D` (default 4.0) sets the base canvas draw radius in the 2D renderer. Both are served via `GET /api/viz/config` and consumed during graph initialisation in `index.html`.

### CAP-VIZ-006 — Force-directed physics parameters

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_PHYSICS_CHARGE_STRENGTH`, `VIZ_PHYSICS_LINK_DISTANCE_2D`, `VIZ_PHYSICS_LINK_DISTANCE_3D`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/http.py::api_viz_config`, `yadgar/core/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `charge_strength`, `link_distance_2d`, `link_distance_3d` under `physics`. Frontend applies in graph init: `graph.d3Force('charge').strength(charge_strength)` (2D) and `graph.d3Force('link').distance(link_distance_2d)` / `link_distance_3d` (mode-dependent).
- **explanation:** Three settings tune the d3-force simulation. `VIZ_PHYSICS_CHARGE_STRENGTH` (default −18.0) controls the many-body repulsion strength between nodes. `VIZ_PHYSICS_LINK_DISTANCE_2D` (default 30.0) and `VIZ_PHYSICS_LINK_DISTANCE_3D` (default 36.0) set the natural link rest-length in each render mode. These are applied once when the graph initialises; changing them requires a page reload or graph reinit.

### CAP-VIZ-007 — Auto-zoom-fit layout settings

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_LAYOUT_ZOOM_FIT_PADDING`, `VIZ_LAYOUT_ZOOM_FIT_TICK`, `VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/http.py::api_viz_config`, `yadgar/core/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `auto_zoom_fit_tick_threshold`, `zoom_fit_padding`, `zoom_fit_transition_ms` under `layout`. In `index.html` the `onEngineTickCallback` checks `_engineTickCount === auto_zoom_fit_tick_threshold`; when matched calls `graph.zoomToFit(zoom_fit_transition_ms, zoom_fit_padding)` once.
- **explanation:** Three settings control the automatic zoom-to-fit behaviour triggered after the force simulation settles. `VIZ_LAYOUT_ZOOM_FIT_TICK` (default 80) is the engine-tick count at which auto-zoom fires. `VIZ_LAYOUT_ZOOM_FIT_PADDING` (default 50 px) is passed to ForceGraph's `zoomToFit()` as the padding inset. `VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS` (default 800 ms) is the animation duration for the zoom transition. The auto-zoom fires exactly once per graph load.

### CAP-VIZ-008 — Search highlight colors and dim opacity

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_SEARCH_DIM_OPACITY`, `VIZ_SEARCH_MATCH_COLOR`, `VIZ_SEARCH_PINNED_COLOR`
- **tools:** —
- **migrations:** —
- **bc:** `BC-VZ2`
- **refs:** `yadgar/core/server/http.py::api_viz_search`, `yadgar/core/server/http.py::api_viz_config`, `yadgar/core/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `match_color`, `pinned_color`, `dim_opacity` under `search`. Frontend stores in `YADGAR_VIZ_CONFIG.search`; 2D canvas draw applies `dim_opacity` to `__dimmed` nodes and uses `pinned_color` / `match_color` as stroke for search-matched and clicked-to-pin nodes. `GET /api/viz/search?q=<query>` → `api_viz_search` → dispatches `recall()` + wiki `query()` (both whole-DB, no directory= param) → returns matching node IDs from ALL projects; frontend marks them for highlight/dim.
- **explanation:** Three settings control the visual feedback of the in-graph search feature. `VIZ_SEARCH_MATCH_COLOR` (default `#ffffff`) is the stroke color for nodes returned by a search query. `VIZ_SEARCH_PINNED_COLOR` (default `#ffd700` gold) is the stroke color for nodes manually pinned by clicking. `VIZ_SEARCH_DIM_OPACITY` (default 0.18) sets the opacity for all non-matching nodes when a search is active, creating a dimming/highlight effect. The search endpoint `GET /api/viz/search` dispatches retrieval recall and wiki query with no directory scoping — this is INTENTIONAL: the viz is a god's-eye admin overlay (localhost, auth-gated) rendering every project's nodes in one graph; search-highlight must find any visible node regardless of project directory. The directory-scoping bypass is documented in the code (see `api_viz_search`, http.py, BC-VZ2 comment) and locked by e2e test BC-VZ2.

### CAP-VIZ-009 — Health refresh interval (daemon health scraper)

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_HEALTH_REFRESH_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/viz/viz_daemon_health.py::run_health_scraper`, `yadgar/_shared/config/config.py`
- **wiring:** Server lifespan starts `run_health_scraper()` as a background asyncio task. Each iteration: scrape → sleep `get_settings().VIZ_HEALTH_REFRESH_SEC`. Setting is re-read each iteration so live env changes take effect without restart. `GET /api/daemon-health` returns the cached scrape result.
- **explanation:** `VIZ_HEALTH_REFRESH_SEC` (default 5.0 s) controls how often the server-side daemon health scraper polls the backend's `/metrics` endpoint and updates the in-process `_health_cache`. The cache is served via `GET /api/daemon-health`, which the viz debug tab reads for real-time process metrics (RSS, CPU, queue depth, circuit-breaker state). The scraper re-reads this setting on every cycle so the cadence can be changed via env var without restarting the server.

### CAP-VIZ-010 — Wiki node shape setting

- **status:** DORMANT
- **category:** viz
- **settings:** `VIZ_WIKI_SHAPE`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/config/config.py`, `yadgar/core/server/http.py::api_viz_config`, `yadgar/core/static/index.html`, `yadgar/core/static/graph-node-factory.js`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `s.VIZ_WIKI_SHAPE` under `node.wiki_shape`. Frontend receives it into `YADGAR_VIZ_CONFIG.node.wiki_shape`. The 3D node renderer in `index.html` reads it at line ~1563 and `graph-node-factory.js` line 27–28: `if (node.type !== 'wiki' || shape !== 'octahedron') return null`. When the setting is `'octahedron'` (default) the octahedron mesh IS created; when set to anything else the mesh factory returns null, falling back to a sphere. The mesh factory is wired; the capability is not disabled by a flag flip, but the config comment (`renderer not wired pending v5.10.7.3 resolution`) indicates this is under active review. The default value `'octahedron'` does exercise the octahedron path, so technically `LIVE`, but the inline comment marks it as configuration-only pending a design decision. Status is `DORMANT` — the setting is served and partially consumed, but the v5.10.7.3 plan may revert the renderer; treat as transitional.
- **explanation:** `VIZ_WIKI_SHAPE` (default `'octahedron'`) configures the desired 3D mesh shape for wiki nodes. When set to `'octahedron'`, the `graph-node-factory.js` module creates a Three.js octahedron geometry for wiki nodes in 3D view. When set to any other value the factory returns null and ForceGraph3D renders the default sphere. The setting is served via `GET /api/viz/config` and applied at graph init; the v5.10.7.3 plan tracks a potential revert of the custom mesh renderer to defaults.

### CAP-VIZ-011 — Graph REST endpoints (full graph, neighborhood, lazy edges, clusters, sim-links)

- **status:** LIVE
- **category:** viz
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-VZ1`, `BC-VZ-R1`, `BC-VZ-R2`, `BC-VZ-R3`, `BC-VZ-R4`
- **refs:** `yadgar/backend/graph/graph_api.py::GraphAPI`, `yadgar/core/server/http.py::api_graph`, `yadgar/core/server/http.py::api_graph_neighborhood`, `yadgar/core/server/http.py::api_graph_edges_lazy`, `yadgar/backend/graph/graph_nodes.py::GraphAPINodesMixin._assemble_memory_nodes`, `yadgar/backend/graph/graph_edges.py::GraphAPIEdgesMixin._build_transition_edges`, `yadgar/backend/graph/graph_edges.py::GraphAPIEdgesMixin._build_entity_rel_edges`, `yadgar/backend/graph/graph_edges.py::GraphAPIEdgesMixin._build_astrocyte_clusters`, `yadgar/_shared/storage/cluster.py::_ClusterMixin.get_memory_clusters`, `yadgar/_shared/storage/cluster.py::_ClusterMixin.get_cluster_members`
- **wiring:** Three HTTP routes on the daemon: `GET /api/graph` → `api_graph` → `GraphAPI.get_full_graph()` assembles memory + wiki + entity nodes with all typed edges (temporal, transition, wiki_crossref, memory_wiki, causal, entity-typed-relations, memory_similarity_link) plus clusters[] from real memory_cluster rows (v5.80); `GET /api/graph/neighborhood/{node_id}` → `api_graph_neighborhood` → `GraphAPI.get_neighborhood()` returns the 1–2 hop subgraph around a node (partial implementation — currently returns nodes only, edges=[]) satisfying BC-VZ1; `GET /api/graph/edges?type=semantic` → `api_graph_edges_lazy` → `GraphAPI.get_edges_by_type()` computes O(n²) KNN semantic edges on demand. All routes are reachable with default config. viz-rest (#55/#89/#14): each memory node now carries `last_accessed` (recency, shown in the detail panel); `GET /api/graph?include_weak=1` threads through `_op_graph` → `get_full_graph(include_weak=...)` → `_build_transition_edges` so count<2 weak transition edges render on demand (default OFF preserves the prior payload, `weak_edges_hidden` still counts them); clusters[] gains a second source `astrocyte_domain` from `_build_astrocyte_clusters` (via `get_astrocyte_processes()`) alongside `memory_cluster`. viz-rest (#209): `derived_from` added to `_build_entity_rel_edges`' `_ENTITY_REL_TYPES` so the entity typed-relation edge scan emits it in the default payload.
- **explanation:** `GraphAPI` is the server-side assembly layer that builds the knowledge graph JSON for the visualization frontend. `get_full_graph` fetches memory nodes (heat-ranked, up to 500), wiki nodes, entity nodes, and assembles all edge types from the storage engine; orphan edges (where an endpoint node is absent) are filtered and counted. v5.80 (#80 viz-fidelity-v2): role vocabulary renamed display→informational; clusters[] added from real memory_cluster rows via get_memory_clusters()+get_cluster_members() — memory_cluster viz-consumption is now LIVE (was DORMANT); memory_similarity_link edges added from CLS-phase near-duplicate links with role=informational; semantic edges remain lazy (off by default, on-demand via /api/graph/edges?type=semantic). `get_neighborhood` provides a node-centric subgraph view for BC-VZ1. `get_edges_by_type` handles the lazy semantic edge path. viz-rest (#55/#89/#14): memory nodes carry `last_accessed`; the `include_weak` query param opts count<2 transitions into the render (F4 `weak_edges_hidden` unchanged); astrocyte_process rows surface as `source=astrocyte_domain` clusters — no new Settings knob (the weak-edge control is a per-request query param, not a config field), so the capability adds payload fields under the existing routes rather than new settings. viz-rest (#209): `derived_from` (the LARGEST relationship type — 3304 rows live) was previously excluded from the payload, so entities whose only edges were `derived_from` rendered as misleading "0 connections" lone spheres. Now emitted with `role="retrieval"` (it feeds PPR + spreading-activation frontier expansion, which traverse all relationship types via `_get_adjacent_batch(..., None)`), toggleable, default ON; shares the `caps.relationships` cap (no per-type cap). Frontend auto-generates its toggle/legend/reheat from the `EDGE_TYPES` registry (data-driven — no frontend code change). `semantic_similarity` stays hidden (retired by ADR-0009).

### CAP-VIZ-012 — Configurable graph node caps

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_MAX_MEMORIES`, `VIZ_MAX_WIKI`, `VIZ_MAX_ENTITIES`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/http.py::api_graph`, `yadgar/backend/graph/graph_api.py::GraphAPI.get_full_graph`, `yadgar/backend/graph/graph_api.py::_limit_clause`
- **wiring:** `GET /api/graph` → `api_graph` reads `get_settings().VIZ_MAX_MEMORIES`/`VIZ_MAX_WIKI`/`VIZ_MAX_ENTITIES` (query params `max_memories`/`max_wiki`/`max_entities` override per request) → passes them to `GraphAPI.get_full_graph(max_memories, top_k, include_invalidated, as_of, max_wiki, max_entities)`. Memory + wiki caps become SQL `LIMIT` clauses via the shared `_limit_clause(cap)` helper; the entity cap is a post-fetch slice in `_assemble_entity_nodes` (because `get_all_entities` is shared by 9 callers and already returns `ORDER BY heat DESC`).
- **explanation:** Three settings bound how many nodes of each type the `/api/graph` payload contains, replacing the prior inconsistent caps (memory hard-defaulted to 500, wiki a hardcoded `LIMIT 200`, entities uncapped). Defaults: `VIZ_MAX_MEMORIES`=500, `VIZ_MAX_WIKI`=200, `VIZ_MAX_ENTITIES`=2000 (entities were unbounded; the live graph holds ~1783). For each knob, a value of `0` or `-1` means unlimited — memory/wiki omit the `LIMIT` clause and entities skip the slice. The knobs live in FIELD_META section `viz_config` (category `viz`) so they are editable from the System→Config editor without a new UI. **finish-viz F1 cap-affordance:** when a node cap actually truncates, `get_full_graph` surfaces `nodes_hidden` in the payload (`_count_nodes_hidden`: one `count()` per capped type, gated on cap>0 so it is a NO-OP at the default) + a frontend status-line "N nodes hidden (cap)" (mirrors `weak_edges_hidden`).

### CAP-VIZ-013 — Precomputed server-side graph layout

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_LAYOUT_ITERATIONS`
- **tools:** `consolidate_now`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/graph/graph_layout.py::compute_graph_layout`, `yadgar/backend/graph/graph_layout.py::graph_signature`, `yadgar/_shared/storage/ops.py::get_graph_layout_cache`, `yadgar/_shared/storage/ops.py::set_graph_layout_cache`, `yadgar/backend/consolidation/service.py::_maybe_precompute_graph_layout`, `yadgar/core/server/http.py::api_graph`
- **wiring:** UNCONDITIONAL (viz-render-perf, Car A — the `VIZ_PRECOMPUTED_LAYOUT_ENABLED` knob was removed, superseding ADR-0010's default-OFF stance). The nightly/full consolidation cycle always calls `_maybe_precompute_graph_layout` — gated by a graph-signature check so it is a fast no-op when the graph shape is unchanged, and gated to the nightly/full path so it never blocks the ≤30s light `consolidate_now` budget. When the signature changed it runs `compute_graph_layout` (seeded `networkx.spring_layout(dim=3, iterations=VIZ_LAYOUT_ITERATIONS)`) and persists `{signature, positions, computed_at}` via `set_graph_layout_cache`. On backend boot, if the cache is empty, the precompute is kicked once in a background thread (non-blocking, non-fatal) so a fresh deploy warms itself. `GET /api/graph` → `api_graph` always attaches `x`/`y`/`z` to each node from `get_graph_layout_cache` when a cache row exists; new/uncached nodes and the empty-cache first-load get no position (the client places them via its d3-force fallback).
- **explanation:** The viz historically ran a d3-force COLD layout client-side on every load (~15s for thousands of nodes). This capability moves the layout server-side: computed once during consolidation, cached keyed by an order-independent graph signature (node ids + edge endpoints), and served via `/api/graph` so the viz seeds positions and runs a tiny cooldown for a near-instant first paint. Compute uses capped-iteration seeded `spring_layout` (networkx is already a dep; deterministic via fixed seed + sorted node ids). It is gated two ways — signature-unchanged no-op, and nightly/full-only — so it never blocks the daemon. Composes with the v5.87 localStorage warm-start (server positions take priority), v5.87.1 camera-fit, and v5.86 idle-pause. The client cold layout is retained as the seed-miss fallback (empty cache on first load, or nodes newer than the last precompute).

### CAP-VIZ-014 — Configurable graph edge caps

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_MAX_TRANSITIONS`, `VIZ_MAX_WIKI_CROSSREFS`, `VIZ_MAX_CAUSAL_EDGES`, `VIZ_MAX_RELATIONSHIPS`, `VIZ_MAX_SIMILARITY_LINKS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/viz_exec/__init__.py::_op_graph`, `yadgar/backend/graph/graph_api.py::GraphAPI.get_full_graph`, `yadgar/backend/graph/graph_edges.py::_build_transition_edges`, `yadgar/backend/graph/graph_edges.py::_build_wiki_crossref_edges`, `yadgar/backend/graph/graph_edges.py::_build_causal_edges`, `yadgar/backend/graph/graph_edges.py::_build_entity_rel_edges`, `yadgar/backend/graph/graph_edges.py::_build_similarity_link_edges`
- **wiring:** `_op_graph` reads the five settings into an `EdgeCaps` dataclass and threads it into `GraphAPI.get_full_graph(..., edge_caps=EdgeCaps(...))` (bundled into one arg to stay within the I13 8-param cap). Each `_build_*` edge builder forwards its cap field to the shared storage method as an optional `limit=` param (default unlimited) applied as a SQL `LIMIT` after a deterministic `ORDER BY` (count for transitions, confidence for causal, weight for relationships + similarity links, slug pair for wiki crossrefs). The caps are NOT read from settings inside the builders — they are parameters, so the nightly precompute (which calls `get_full_graph` without `edge_caps`) lays out the full uncapped graph.
- **explanation:** The five default-render edge scans (`get_all_transitions`, `get_all_wiki_crossrefs`, `get_all_causal_edges`, `get_relationships_by_types`, `get_all_memory_similarity_links`) were full-table reads with no cap — the node queries got `_limit_clause` caps in v5.88 FIX2 but the edge queries never did. These five knobs extend the same cap philosophy to edges. Default `0` (= unlimited, same as `-1`) preserves day-one behavior exactly; a non-zero value keeps the strongest edges (deterministic order) so a capped render is stable per request rather than a random subset. The shared storage methods gain an optional `limit=` defaulting to unlimited, so their non-viz consumers (admin invariants, memify_derive, CLS pre-load) are untouched. Knobs live in FIELD_META section `viz_config` (category `viz`). **finish-viz F1 cap-affordance:** when the TRANSITION cap truncates, `get_full_graph` surfaces `edges_hidden` (`_count_edges_hidden`: one `count(... WHERE count >= 2)` gated on cap>0 → NO-OP at the default) + a frontend status-line "N edges hidden (cap)". SCOPE: only the transition cap is surfaced — it is the one edge type with a cheap predicate-matched total (the default gate is `count >= 2`); the other four caps carry distinct builder-side predicates whose totals are not cheaply derivable, so a plain table `count()` would report a wrong number (the `weak_edges_hidden` lesson) — they are intentionally left uncounted rather than lied about.

### CAP-VIZ-014 — Trace-replay "Traces" tab (Tempo mesh + oscilloscope replay)

- **status:** DORMANT
- **category:** viz
- **settings:** `TEMPO_QUERY_URL`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/trace_mesh.py::build_mesh`, `yadgar/core/server/routes/traces.py`, `yadgar/core/static/traces-tab.js`, `yadgar/core/static/traces-replay.js`, `yadgar/core/static/traces-tab.css`, `docs/diagrams/simplify_trace.py`
- **wiring:** OFF by default (`TEMPO_QUERY_URL` empty = disabled). When the operator sets `TEMPO_QUERY_URL` (e.g. `http://localhost:3200`), the viz "Traces" tab fetches `GET /api/traces/recent` (TraceQL search matching any `tool.*` boundary span) for the sidebar and `GET /api/traces/{id}/mesh` for the replay mesh. `routes/traces.py` ports `docs/diagrams/capture_trace.py` over httpx, flattens the by-id Tempo span table, and feeds it to `trace_mesh.build_mesh` (the pure simplify_trace aggregation: start-containment tree, plumbing collapse, storm ×N, lane assignment, ≤MAX_BOXES stage selection). Mesh compute is on-demand, LRU-cached (module-level OrderedDict, size 20 / 10 min TTL) — never on the daemon hot loop (ADR-0074, `--cpus 1`). Bearer-auth (`/api/` prefix), NOT debug-gated. Graceful degrade — empty `TEMPO_QUERY_URL` / Tempo-down / non-200 / empty trace all return a 200 typed-empty payload, never 500.
- **explanation:** A phosphor-oscilloscope observability instrument that replays a captured MCP-tool trace as an animated waterfall over fixed core (phosphor-green) / backend (signal-cyan) lanes, with per-stage drill-down and fault-red error stages. "Live" = replay-on-completion (Tempo by-id fetch is fresh ~100 ms), not span streaming. DORMANT because it is inert until `TEMPO_QUERY_URL` is set; the routes + mesh pipeline + tab exist and are reachable but return empty payloads while disabled. Shares the `simplify_trace` aggregation logic with the offline diagram generator (`docs/diagrams/simplify_trace.py` is now a consumer of `trace_mesh`).

### CAP-VIZ-015 — Backend→core SSE event relay + heat_updated emit

- **status:** LIVE
- **category:** viz
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-VZ-F2`
- **refs:** `yadgar/backend/viz_exec/__init__.py::_op_events`, `yadgar/core/server/http.py::_poll_backend_events`, `yadgar/core/server/http.py::_make_event_stream`, `yadgar/backend/consolidation/heat_decay.py::_apply_decay`, `yadgar/backend/consolidation/heat_decay.py::_build_heat_updates`, `yadgar/_shared/server_helpers/server_helpers.py::_push_event`, `yadgar/_shared/runtime/state.py::_backend_event_cursor`
- **wiring:** The `_event_queue` ring buffer is a PROCESS-LOCAL deque; after the R3 write-path/consolidation split (ADR-0063) the write-exec (`memory_added`/`wiki_added`/`wiki_updated`) and heat decay (`heat_updated`) push events run in the BACKEND process, so their pushes land in a buffer the CORE-served `/api/graph/events` SSE stream can never read. Relay option (a): backend `_op_events` (registered in `_VIZ_OPS`, I32) returns ring-buffer entries with `seq > since` (read under `_event_lock`) plus `latest_seq`; core's `_poll_backend_events` is invoked once per `_make_event_stream` loop tick via `asyncio.to_thread` (blocking httpx off the event loop, ADR-0018) and re-pushes each new backend event onto core's own queue via `_push_event`, which RE-STAMPS a fresh core seq (the backend `seq` is stripped first so the `{"seq": core, **event}` merge cannot let the backend value overwrite the core cursor). A process-global cursor (`_backend_event_cursor`, seeded to the backend head on first poll to skip the stale backlog) + a poll-lock serialize N concurrent SSE clients to one backend round-trip per tick and keep read→fetch→advance atomic across the HTTP call. `heat_updated` is emitted at the end of `_apply_decay` from the reconciled heat intents (`_build_heat_updates`: `mem:N` + `entity:N` typed ids, persisted heat, one update per changed row, skipped when empty). `_poll_backend_events` carries `@observe(span=False)` (I33 / ADR-0074 span-storm — it runs every ~0.5s).
- **explanation:** Fixes the split-deployment blind spot where every backend-pushed viz event was silently discarded from the browser's perspective — the frontend `heat_updated` handler (index.html, v5.80) had no Python emitter, and `memory_added`/`wiki_added` were invisible too. The relay is a pull (core polls backend) because ADR-0063 forbids backend→core imports; core-polls-backend is the only legal direction. Best-effort: any backend/transport error is swallowed (the periodic full-graph reload still shows the data). BC-VZ-F2 stays ⏳ — the three layers are unit-covered (`tests/backend/test_viz_f2_heat_sse.py`) but the real browser-SSE end-to-end path is a user smoke-check, not driven in-harness. **finish-viz:** the relay also carries `trace_complete` (see CAP-VIZ-017) — the same `_push_event` → `_op_events` → `_poll_backend_events` → SSE path.

### CAP-VIZ-016 — Milky-Way galaxy graph layout

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_GALAXY_LAYOUT`, `VIZ_GALAXY_ARMS`, `VIZ_GALAXY_SPIRAL_PITCH`, `VIZ_GALAXY_CORE_DENSITY`
- **tools:** `consolidate_now`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/graph/graph_layout.py::galaxy_layout`, `yadgar/backend/graph/graph_layout.py::_galaxy_node_membership`, `yadgar/backend/graph/graph_layout.py::_rank_clusters`, `yadgar/backend/graph/graph_layout.py::attach_cached_positions`, `yadgar/backend/consolidation/service.py::_maybe_precompute_graph_layout`, `yadgar/_shared/storage/ops.py::set_graph_layout_cache`, `yadgar/_shared/storage/ops.py::get_graph_layout_cache`, `yadgar/core/static/galaxy-view.js`, `yadgar/core/static/galaxy-view.css`, `yadgar/core/static/index.html`
- **wiring:** When `VIZ_GALAXY_LAYOUT` is on (default), `_maybe_precompute_graph_layout` runs `galaxy_layout(nodes, edges, clusters, arms=VIZ_GALAXY_ARMS, spiral_pitch=VIZ_GALAXY_SPIRAL_PITCH, core_density=VIZ_GALAXY_CORE_DENSITY)` instead of `compute_graph_layout` (spring). Cluster membership is derived from the same `/api/graph` `clusters[]` (`member_node_ids` + `member_count`) that CAP-VIZ-011 renders — a node is ARM material iff it belongs to a real multi-member cluster (`member_count >= 2`), else LOOSE (core). The cache row records `layout_mode` ("galaxy"|"spring") via `set_graph_layout_cache(..., layout_mode)`; `attach_cached_positions` stamps `data["layout_mode"]` onto the `/api/graph` payload. **Since ADR-0135, Galaxy is a THIRD render mode — a dedicated raw-Three.js scene (`galaxy-view.js`), NOT positions fed to 3d-force-graph.** The toolbar Galaxy button tears down the FG instance (global `graph=null`) and mounts `window._galaxyView` (glow sprites, dual core-glow halos, 900-star starfield, FogExp2, faint intra-arm LineSegments, MiniOrbit auto-rotate — all lifted from the mockup on the already-loaded `window.THREE` r0.158). The scene runs a CLIENT-SIDE `layoutPositions()` recompute (deterministic `mulberry32` PRNG reseeded per layout + sorted ids) so the galaxy-only right-panel controls (arms/pitch/core-density/bulge/rotate) drive the shape live; server x/y/z feed only the FG warm-seed. Picking = `THREE.Raycaster` → `idToIndex` → existing `showDetail(node)`. All ~51 `graph.*` sites in `index.html` are `_isGalaxy()`-guarded/routed (applyFilters→setVisible, loadGraph→mount/relayout, SSE→no-op). The signature no-op folds `layout_mode` into its comparison so a knob flip recomputes on the next cycle.
- **explanation:** Port of the user-approved `docs/plans/viz-galaxy.mockup.html` — its ACTUAL Three.js scene, not just positions (#209's positions-only approach retired by ADR-0135). Loose/single nodes pack into a DENSE spheroidal CORE bulge (exponential inverse-CDF radius sampler, mockup `expRadius`; core-density tightens the packing); real multi-member clusters string along K log-spiral ARMS (top-K bucketed round-robin into arms by rank; overflow scatters inter-arm); exponential radial density (dense center + arm-roots, sparse rim). Heat is NOT position — it stays the client's brightness/size channel. Deterministic PRNG so both the server cache signature and the client layout stay stable across runs. Teardown disposes all geo/mat/textures + both core-glow sprites + starfield + edges + `renderer.dispose()+forceContextLoss()` (the ~16 WebGL-context ceiling). Falls back to spring (`VIZ_GALAXY_LAYOUT=false`) and to the client cold d3-force layout on a seed-miss, same as CAP-VIZ-013. Deferred v1 (state kept): galaxy SSE live-heat-patch (`patchHeat` hook exists, unwired from SSE) + galaxy search-highlight. Render/picking/teardown = user smoke-check (no browser harness); pure fns unit-covered (`galaxy-view.test.js`).

### CAP-VIZ-017 — Trace-complete SSE (live Traces-tab append)

- **status:** LIVE
- **category:** viz
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/_app.py::_build_tool_wrappers`, `yadgar/_shared/server_helpers/server_helpers.py::_push_event`, `yadgar/backend/viz_exec/__init__.py::_op_events`, `yadgar/core/server/http.py::_poll_backend_events`, `yadgar/core/static/traces-tab.js`, `yadgar/core/static/index.html`
- **wiring:** When an MCP tool boundary finalizes, the tool wrappers' `finally` (same site as `_emit_metrics`, both sync + async paths) call `_emit_trace_complete`, which reads the enclosing trace id via `get_current_trace_id()` and pushes `{event:"trace_complete", trace_id, tool, total_ms, status}` via `_push_event`. It rides the existing F2 relay (CAP-VIZ-015): backend `_op_events` → core `_poll_backend_events` → the `/api/graph/events` SSE stream. The frontend `connectSSE` dispatcher routes `trace_complete` to `window._ingestTraceComplete` (exposed from `traces-tab.js::ingestTraceComplete`), which prepends the entry to the recent-traces sidebar (de-dupe by trace_id, cap 50), re-rendering only when the tab is already built. Best-effort: `_emit_trace_complete` never raises and skips entirely when there is no active trace (internal/test direct calls).
- **explanation:** trace-replay Phase 3. The Traces tab previously only refreshed its recent list on tab open (`GET /api/traces/recent`, a Tempo TraceQL search). This adds a push so a trace completing while the tab is open live-appends without a manual refresh. The **live p95/rate badges were DROPPED** — no per-stage Prometheus metrics exist to source them (the plan self-guarded this); the emit carries only the fields the sidebar already renders. The browser-SSE live-append is a user smoke-check (no-browser-harness convention); the emit layer is unit-covered (`tests/server/test_trace_complete_sse.py`).

### CAP-INFRA-001 — Request-path thinness + async threading invariants

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I1`, `BC-I2`, `BC-I3`, `BC-I4`, `BC-I6`, `BC-I9`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** Structural architecture guarantee — the FastAPI request handler in `yadgar/core/server/http.py` offloads all ML and heavy compute to the drainer thread (via `asyncio.to_thread` or the drainer queue) rather than blocking the event loop. The drainer is a single background lane; no second processor competes. Opt-in features (e.g. enrichment, dream replay) test their feature flag before initializing heavy models. Embedding and rerank are cached within a single request to prevent double-pay. Write-path latency is kept within the ≤5 ms p50 budget. All of these are runtime invariants enforced by design and validated by unit tests or real-path coverage, not a dedicated CI script.
- **explanation:** These six invariants collectively define the request/async threading model: the event-loop thread never blocks on ML compute (I1, I4), the drainer is the one and only catch-up worker (I2), disabled features bail out before expensive init (I3), embeddings and rerankers are not computed twice per request (I6), and new write-path code stays within a 5 ms p50 latency budget (I9). Together they prevent event-loop stalls, competing background processors, and accidental performance regressions on the hot write path.

---

### CAP-INFRA-002 — Queue durability boundary

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I7`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** The write queue is the durability checkpoint: a write is acknowledged to the caller only after it is persisted to the queue. A subsequent process crash before the drainer consumes the item does not lose the write. Validated by real-path coverage (`[r]`).
- **explanation:** I7 makes the durable queue the only guarantee boundary. Callers receive a success acknowledgement the moment the write is enqueued; processing by the drainer is best-effort from that point. Any crash between enqueue and drainer-commit is safe because the queue entry survives restart. This property eliminates data loss on sudden process termination.

---

### CAP-INFRA-003 — Module decomposition boundary preservation

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I5`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** Review-time invariant only — no automated CI check exists. Enforced by pull-request review: refactors must not relocate work units across module boundaries (e.g. moving drainer logic into the HTTP layer). No pre-commit hook or runtime probe enforces this.
- **explanation:** I5 is a structural discipline rule: when refactoring module boundaries, the computation that runs inside each module must stay inside that module. Moving work is a design change that requires explicit review sign-off. This prevents accidental introduction of blocking compute on the request thread during reorganisations.

---

### CAP-INFRA-004 — Backpressure observability

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I8`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`, `yadgar/_shared/observability/metrics.py`
- **wiring:** Queue depth and backpressure state are exposed as Prometheus metrics in `yadgar/_shared/observability/metrics.py`. The drainer writes to these metrics on each cycle. Observable via the `/metrics` endpoint at runtime.
- **explanation:** I8 requires that queue backpressure is externally visible so operators can detect write-queue buildup before it causes latency spikes or data loss. The queue depth gauge and any backpressure flag are registered Prometheus metrics written by the drainer. This is both a runtime invariant and a metric-writer requirement (linked with I23).

---

### CAP-INFRA-005 — Config overrides explicit

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I10`, `BC-I12`, `BC-I27`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** Process/design invariants only — no automated CI check. I10: config overrides must be logged or visible at startup so operators know what was changed. I12: performance optimisations must be backed by a recorded measurement, not intuition. I27: reproducible bugs or fixes >10 LOC must be accompanied by a plan doc in the same session.
- **explanation:** These three process discipline invariants (I10, I12, I27) have no automated enforcement — they depend on review culture and documented convention. I10 prevents silent config mutations that are invisible in logs. I12 prevents premature optimisation without evidence. I27 ensures complex fixes are planned before implementation, reducing rework. All are P3 priority and enforced through code review.

---

### CAP-INFRA-006 — Backend image size ratchet

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I11`
- **refs:** `scripts/check_image_size.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by `check-image-size-backend` and `check-image-size-core` pre-commit hooks, both wired at `--hook-stage manual` (not the default commit stage). Must be run explicitly before a release: `pre-commit run check-image-size-backend --hook-stage manual`. Caps: backend ≤ 2.0 GB, core ≤ 0.8 GB, verified via `podman history` or `docker history`.
- **explanation:** I11 separates heavy, stable ML artifacts (large language models, embedding weights) into the backend image and keeps the core image thin. The script checks actual image layer sizes against configured caps. Because it requires a built image it is a manual-stage hook, not default pre-commit. A failed cap blocks the release gate and forces the developer to either remove large artifacts or justify a cap increase via the allowlist mechanism.

---

### CAP-INFRA-007 — Bounded file and function complexity

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I13`
- **refs:** `scripts/check_complexity.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-complexity` pre-commit hook on every commit touching `.py` files in `yadgar/` (excludes `yadgar/tests/` and `scripts/`). Hard caps: function cyclomatic ≤15, function LOC ≤150, nesting ≤4, params ≤8, file LOC ≤1000, class depth ≤3. Soft caps are lower; violations require a `# noqa: C901 - cohesive: <reason>` comment. HARD violations require an entry in `.complexity-allowlist.json`.
- **explanation:** I13 prevents unbounded growth in code complexity by enforcing numeric caps on cyclomatic complexity, function/file length, nesting depth, parameter count, class size, and inheritance depth. The ratchet is maintained via `.complexity-baseline.json` (4819 entries as of v5.49); new violations require either a refactor or an explicit allowlist entry with a 40-char rationale. Soft violations are flagged as warnings; hard violations block the commit. Ruff's C901 and PLR0913 cover cyclomatic and parameter count respectively; the custom script covers the remaining metrics.

---

### CAP-INFRA-008 — Complexity-cap integrity (allowlist governance)

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I30`
- **refs:** `scripts/check_complexity_allowlist.py`, `scripts/check_complexity.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-complexity-allowlist` pre-commit hook. Fires on changes to `yadgar/**/*.py`, `scripts/**/*.py`, `.complexity-allowlist.json`, or `.complexity-config.json`. Four checks: (a) every HARD violation in production code is in the allowlist, (b) each entry has ≥40-char rationale, (c) no stale entries (every entry maps to a current HARD violation), (d) drift check — recorded metrics must not exceed current measurement by >20%.
- **explanation:** I30 governs the allowlist that permits HARD complexity violations. Without this gatekeeper, the allowlist could accumulate stale entries (exemptions for code that was later refactored), entries with no rationale, or entries whose metrics drift far from the current measurement. The four-part check ensures the allowlist is always accurate, justified, and tight — it cannot become a blanket bypass for complexity debt.

---

### CAP-INFRA-009 — Structured logging contract

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I14`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** Process invariant with no dedicated CI script. In-scope log sites must use structured JSON logging (via the established logging config) rather than bare `print` or unstructured `logging.info`. Enforced through code review. The logging setup is performed in `configure_logging()` which must be called before any tracing init (see I19).
- **explanation:** I14 mandates structured (JSON) logging for in-scope log sites so that log aggregation tools (e.g. Loki, Datadog) can parse fields without regex. The scope is deliberately limited (not every third-party library), so this is enforced at review time rather than by a blanket lint. Structured logs enable field-level filtering, alerting on specific error codes, and latency histogram extraction without log parsing.

---

### CAP-INFRA-010 — Boundary fuzz tests + config yaml-backing

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I15`, `BC-I25`
- **refs:** `scripts/check_complexity.py`, `yadgar/_shared/config/config.py`, `yadgar/_shared/config/config_yaml.py`, `yadgar/_shared/config/config_registry.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** I15 (fuzz tests) — unit-level, scoped: boundary-property fuzz tests must exist for in-scope interfaces; no dedicated CI hook, validated by unit test presence. I25 (yaml-backing) — enforced by the `check-config-three-way-sync` pre-commit hook, which runs `pytest yadgar/tests/test_config_three_way_sync.py`. Fires on changes to `config.py`, `config_yaml.py`, `config_registry.py`, or the env-only allowlist.
- **explanation:** I15 requires property-based (fuzz) tests at module boundaries so that edge-case inputs are explored programmatically. I25 requires every config knob in `Settings` to have a corresponding yaml-backed default so that container deployments with yaml config files get the same defaults as env-var deployments. The three-way sync check verifies `config.py`, `config_yaml.py`, and `config_registry.py` all agree. Both invariants have unit-level coverage; I25 also has a wired CI hook.

---

### CAP-INFRA-011 — OTel tracing: file handler ordering + span coverage

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I19`, `BC-I20`, `BC-I21`, `BC-I24`
- **refs:** `scripts/check_trace_spans.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** I24 (and companion I19) enforced by the `check-trace-spans` pre-commit hook on changes to `yadgar/core/server/http.py`. The script scans for public top-level async functions lacking `@trace_span`. I19 (file handler before tracing init) is structurally enforced by call order in entry points; the contract cites `check_trace_spans.py` as its enforcement mechanism. I20 (FastAPIInstrumentor) and I21 (background thread root spans) are runtime invariants verified at startup/integration time with no dedicated CI hook.
- **explanation:** These four invariants describe the OTel observability layer. I19 ensures log output is captured before tracing starts (preventing lost logs during init). I20 ensures every FastAPI/Starlette app is wrapped with `FastAPIInstrumentor` so HTTP requests generate spans automatically. I21 ensures background threads (drainer, sleep cycle) each open a new OTel root span per work unit so distributed traces are complete. I24 ensures every public HTTP handler in `http.py` carries `@trace_span` so individual RPC latencies are always visible. The `check-trace-spans` hook enforces I24/I19 structurally.

---

### CAP-INFRA-012 — Trust boundary: single-user single-host

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I22`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** Design invariant — no automated CI check. All listeners bind `127.0.0.1` per `docker-compose.yml`. No multi-tenant authentication is assumed or implemented. Enforced through architecture review: any change that opens a listener to 0.0.0.0 or adds per-user auth requires explicit design approval.
- **explanation:** I22 declares that Yadgar is a single-user, single-host tool: it assumes that anything able to reach its listening port is already the authorised user. This simplifies the security model by removing the need for per-request authentication. Binding to 127.0.0.1 (loopback only) enforces the boundary at the network level. Multi-tenant or networked-access deployments would require a redesign of the trust model.

---

### CAP-INFRA-013 — Metric writer completeness

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I23`
- **refs:** `scripts/check_metric_writers.py`, `yadgar/_shared/observability/metrics.py`, `yadgar/backend/embed_service/embed_service_metrics.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-metric-writers` pre-commit hook on any change to `yadgar/**/*.py`. The script scans `yadgar/_shared/observability/metrics.py` and `yadgar/backend/embed_service/embed_service_metrics.py` for Prometheus metric declarations (Gauge, Counter, Histogram, Summary), then verifies each has ≥1 writer or reference site elsewhere in `yadgar/`. A declared metric with no writer is a dead metric.
- **explanation:** I23 prevents the proliferation of Prometheus metrics that are declared but never written — which wastes cardinality budget and misleads operators with always-zero gauges. The lint performs a two-pass scan: first it collects all declared metric objects by variable name, then it searches the codebase for call sites that increment, set, or observe each metric. Any metric with zero call sites fails the lint. This is tightly coupled with I8 (backpressure observability) and the overall metrics contract.

---

### CAP-INFRA-014 — Secret gate single chokepoint

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I26`
- **refs:** `scripts/check_secret_gate.py`, `yadgar/_shared/security/secrets.py`, `yadgar/_shared/security/allowlist.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-secret-gate` pre-commit hook on changes to `yadgar/core/server/tools/**/*.py`. Every `@_tool`-decorated function with write parameters (`content`, `current_task`, etc.) must call `gate_or_reject()` or carry a `# secret-gate: skip` annotation. Known delegating tools (`seed_project`) are explicitly exempted (`remember` deleted in v6 T3; `wiki_approve` removed in v5.157.0 Fix #76). This invariant ties directly to BC-S1 (secret patterns blocked at API).
- **explanation:** I26 mandates that secret scanning is a single-entry-point gate: the `gate_or_reject()` function in `yadgar/_shared/security/secrets.py`. No write tool is permitted to bypass this gate without an explicit annotation. The gate checks incoming content against known secret patterns (API keys, tokens, passwords) and either rejects the write or routes it through the allowlist bypass path (which triggers an audit — I28). Keeping the gate at a single chokepoint ensures that new write tools cannot accidentally skip secret scanning.

---

### CAP-INFRA-015 — Allowlist bypass audit

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I28`
- **refs:** `scripts/check_allowlist_audit.py`, `yadgar/_shared/security/allowlist.py`, `yadgar/_shared/security/secrets.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-allowlist-audit` pre-commit hook on changes to `yadgar/_shared/security/allowlist.py` or `yadgar/_shared/security/secrets.py`. The script structurally verifies: `is_allowlisted()` and `_write_audit()` co-exist in `allowlist.py`; `gate_or_reject()` in `secrets.py` calls both; `YADGAR_SECRET_GATE_AUDIT_DIR` env knob is documented. This ties to BC-S3.
- **explanation:** I28 ensures that every time an allowlist bypass is used (content that would normally be blocked passes the secret gate), an audit record is written. Without this, a compromised allowlist entry could silently permit secret leakage. The structural check verifies that the `is_allowlisted()` function and the `_write_audit()` function are always called together in the gate implementation, making it impossible for a refactor to separate them.

---

### CAP-INFRA-016 — No dead capability: edge types contracted

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I29`
- **refs:** `scripts/check_dead_capability.py`, `yadgar/backend/graph/graph_api.py`, `yadgar/core/viz/viz_meta.py`, `docs/contracts/EDGE_CONTRACT.md`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-dead-capability` pre-commit hook on changes to `yadgar/backend/graph/graph_api.py`, `yadgar/core/viz/viz_meta.py`, or `docs/contracts/EDGE_CONTRACT.md`. The script AST-scans `graph_api.py` and `viz_meta.py` for edge-type literals and `EDGE_TYPES` registry keys, cross-references against `docs/contracts/EDGE_CONTRACT.md`, and fails on orphan edge types (produced but not contracted), drop-still-produced (removed from contract but still produced), or stale contract rows (contracted but never produced).
- **explanation:** I29 applies the "no dead capability" principle specifically to the knowledge graph edge-type layer: every edge type that the code produces must appear in `EDGE_CONTRACT.md`, and every contracted edge type must actually be produced. This three-way consistency check (stored ≡ used ≡ shown) prevents documentation drift where the contract describes edge types that no longer exist, or code produces edges that are undocumented and therefore invisible to operators and maintainers.

---

### CAP-INFRA-017 — Directory scoping: single eligible predicate

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I31`
- **refs:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** Runtime invariant with real-path coverage. The single `is_directory_eligible()` predicate is the only gate for directory-scoping decisions throughout the codebase — no inline directory eligibility logic is permitted. Ties to BC-DC1 (eligible set = {caller_dir, global, '', None}) and BC-DC2 (hard-require directory on reads). Validated by real-path integration tests.
- **explanation:** I31 enforces that directory-scoping logic is centralised in a single `is_directory_eligible()` predicate. Without this, each module might implement its own eligibility check with subtly different rules, leading to data leaking across project boundaries. The predicate defines the canonical eligible set: the caller's directory, global (no directory), empty string, or None. Any code that bypasses this predicate and implements inline directory filtering is a contract violation.

---

### CAP-INFRA-018 — Contract coverage self-lint + tamper-protection layers

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `scripts/check_contract_coverage.py`, `scripts/check_e2e_assertions.py`, `scripts/check_test_weakening.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** Three enforcement layers: (1) `check_contract_coverage.py` runs as a non-e2e pytest to validate ✅ refs, header counts, and the ✅-count floor (≥12). (2) `check-e2e-assertions` pre-commit hook — every `def test_*` under `yadgar/tests/e2e/` must have ≥1 real assertion. (3) `check-test-weakening` pre-commit hook — staged diff must not net-remove `assert` statements from e2e tests or decrease the ✅ count in BEHAVIOR_CONTRACT.md.
- **explanation:** These three lints form the tamper-protection stack for the behavior contract. Layer 3 (`check_e2e_assertions.py`) prevents hollow e2e tests (functions named `test_*` that make no assertion); its scan set is `yadgar/tests/e2e/**/*.py` ∪ `yadgar/tests/**/*e2e*.py` (widened 2026-07-29 — the old `tests/e2e/`-only root left six `*e2e*` modules unlinted). Layer 4 (`check_test_weakening.py`) prevents silent degradation: a BRANCH (merge-base of `origin/master`..HEAD, unioned with the staged diff) that nets a removal of `assert` statements **in any single file** of that scan set, or that drops the ✅ green count, is blocked. Branch scope replaced staged-only scope because a CI checkout stages nothing, so the guard could never fail there; per-file netting replaced a global sum because over a branch-sized window an addition in one e2e module masks a removal in another. A genuinely sanctioned deletion is recorded per file in `.test-weakening-allowlist.json` as `{"path": {"allowed_delta": -N, "rationale": "..."}}` — an entry grants **exactly** its recorded delta, so a file whose measured delta is worse than its entry still fails and no entry can absorb further weakening of the same file; a file with no entry fails as before. Stale entries (file absent from the diff, or delta now better than recorded) are warned about on every run but are not a hard error, because the merge-base this guard diffs against moves and a correct entry goes stale on merge. This replaced an `ALLOW_TEST_WEAKEN=1` environment override (removed 2026-08-08) that silenced every file in a run at once, left no trace in the reviewed diff, and was wired to a PR label in both CI workflows. Together they make it structurally hard to weaken the contract without detection.

---

### CAP-INFRA-019 — Version consistency enforcement

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `scripts/check_versions.py`, `scripts/check_backend_bump.py`
- **wiring:** Two pre-commit hooks: `check-versions` fires on changes to `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock`, or `flake.nix` and verifies all five agree on the core version; also checks `server.json` backend_version matches docker-compose. `check-backend-bump` fires on changes to `entrypoint-backend.sh`, `Dockerfile.backend`, or any `backend/` file and requires `server.json` to be staged with a bumped `backend_version`.
- **explanation:** These two scripts prevent version skew across the multi-file version declaration system. Without `check-versions`, a developer could bump `pyproject.toml` and forget `flake.nix`, causing a Nix build with the wrong Python version. Without `check-backend-bump`, a backend Dockerfile change could ship without a corresponding backend image version bump, making it impossible to tell from the version string whether the backend image is stale. Together they keep the version surface consistent across all deployment artifacts.

---

### CAP-INFRA-020 — MCP tool surface: write-path tools (memorize / recall)

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T1`, `BC-T2`, `BC-T3`
- **refs:** `yadgar/core/server/tools/memorize.py`, `yadgar/core/server/tools/recall.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `memorize` is a registered `@_tool` function in `memorize.py`. `recall` is a registered `@_tool` in `recall.py`. Both are reachable via the FastMCP server on every client request. Contract rows BC-T1/T2/T3 cross-reference BC-A1 (memorize stores novel content) and BC-B1..B4 (recall respects directory scoping). `remember` stub deleted in v6 T3 (see CAP-STOR-027).
- **explanation:** These two tools form the primary episodic write and read surface. `memorize` accepts content and a directory, runs it through the write gate, queues it for drainer processing (embedding + dedup), and returns a memory id. `recall` performs vector + FTS retrieval, applies the full reranking pipeline (CE, NLI, MMR, rules, confidence gate), and returns directory-scoped results. Both are callable from any MCP client session.

---

### CAP-INFRA-021 — MCP tool surface: project context tools

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T4`, `BC-T5`, `BC-T6`, `BC-T7`
- **refs:** `yadgar/core/server/tools/project.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `project_brief`, `seed_project`, `bootstrap_project`, and `update_active_work` are `@_tool` functions in `project.py`. All four are reachable via the FastMCP server. Cross-refs: BC-T4 = BC-PC1, BC-T5 = BC-PC2, BC-T7 = BC-PC3. `bootstrap_project` (BC-T6) creates initial project scaffolding.
- **explanation:** These tools manage project-level context. `project_brief` assembles and returns a directory-scoped context bundle: top anchors, hot memories, and wiki pages for the caller's directory — with no cross-project leakage. `seed_project` creates initial anchor memories to bootstrap a new project context. `bootstrap_project` runs a broader initial scaffold. `update_active_work` stamps the active task description so it surfaces in subsequent `project_brief` calls. Together they form the "project orientation" surface used at session start and after context resets.

---

### CAP-INFRA-022 — MCP tool surface: checkpoint / restore

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T8`, `BC-T9`
- **refs:** `yadgar/core/server/tools/misc.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `checkpoint` and `restore` are `@_tool` functions in `misc.py`. Both are reachable via the FastMCP server. Cross-ref: BC-T8/T9 = BC-CK1 (checkpoint then restore returns task/decisions/next-steps).
- **explanation:** `checkpoint` serialises the current session's task state (description, decisions, next steps, anchors) into a durable store keyed by directory. `restore` reads back the latest checkpoint for a directory and returns the saved context, enabling seamless continuation after a `/clear` or session expiry. The round-trip guarantee (BC-CK1) requires that all fields written by `checkpoint` are returned intact by `restore`.

---

### CAP-INFRA-023 — MCP tool surface: anchors

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T10`, `BC-T11`
- **refs:** `yadgar/core/server/tools/misc.py`, `yadgar/core/server/tools/audit.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `anchor` is in `misc.py`; `audit_anchors` is in `audit.py`. Both registered `@_tool`. Cross-refs: BC-T10 = BC-AN1, BC-T11 = BC-AN3.
- **explanation:** `anchor` creates a pinned `_anchor`-tagged memory that is exempt from heat decay and always surfaces in the `top_anchors` field of `project_brief`. `audit_anchors` reports anchor count for a directory and flags malformed or duplicate anchors. Together they provide the stable long-term context layer that survives heat decay — used for foundational project facts that must always be present regardless of how old they are.

---

### CAP-INFRA-024 — MCP tool surface: hook install / sync

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T12`, `BC-T13`
- **refs:** `yadgar/core/server/tools/misc.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `install_hooks` and `sync_instructions` are `@_tool` functions in `misc.py`. Both reachable via FastMCP. Cross-refs: BC-T12 = BC-HK1, BC-T13 = BC-HK2.
- **explanation:** `install_hooks` writes the Claude Code hook configuration to the target directory (idempotent — re-running does not add duplicate entries). `sync_instructions` writes the agent-instruction block to the directory; a stale or outdated block is replaced rather than duplicated. These tools allow the Yadgar server to push its client-side hook configuration to any project directory, enabling tool-usage capture and session-end hooks without manual setup.

---

### CAP-INFRA-025 — MCP tool surface: rules engine

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T14`, `BC-T15`
- **refs:** `yadgar/core/server/tools/admin_other.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `add_rule` and `get_rules` are `@_tool` functions in `admin_other.py`. Both reachable via FastMCP. Cross-refs: BC-T14 = BC-RU1, BC-T15 = BC-RU1/RU2.
- **explanation:** `add_rule` stores a retrieval-reranking rule scoped to a directory. `get_rules` returns all rules for a directory (no cross-project leakage). Rules stored via this surface are consumed by the retrieval pipeline's rules-rerank stage (BC-RU3/BC-RR4) to boost or penalise specific candidates. The two tools together form the directory-scoped rule CRUD surface.

---

### CAP-INFRA-026 — MCP tool surface: agent-prompt library

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T16`, `BC-T17`, `BC-T18`
- **refs:** `yadgar/core/server/tools/agent_prompts.py`, `yadgar/core/server/tools/dispatch_helper.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `agent_prompt_save` is in `agent_prompts.py`; `agent_dispatch_prelude` is in `dispatch_helper.py`. Both registered `@_tool`. The exact-key lookup is the internal `_read_agent_prompt(slug)` helper (the `agent_prompt_get` tool was removed in v5.85 S4); semantic lookup is `recall(type="wiki", tags=["agent-prompt"])`. Cross-refs: BC-T16 = BC-AP1, BC-T17 = BC-AP1/AP2, BC-T18 = BC-AP3.
- **explanation:** `agent_prompt_save` stores a named prompt template (one wiki page per pattern, wiki-versioned) for later retrieval. Exact-name retrieval is the internal `_read_agent_prompt(slug)` helper (returning not-found for unknown slugs, never a stale match); semantic retrieval is the tag-aware recall path. `agent_dispatch_prelude` assembles the standard agent-dispatch prelude, injecting directory-scoped context from `project_brief`. Together these support the orchestrator pattern: the main thread stores reusable subagent prompts and retrieves them at dispatch time, enriched with current project context.

---

### CAP-INFRA-027 — MCP tool surface: in-context blocks

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T19`, `BC-T20`, `BC-T21`, `BC-T22`, `BC-T23`, `BC-T24`, `BC-T25`
- **refs:** `yadgar/core/server/tools/blocks.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** All seven `block_*` tools (`block_create`, `block_get`, `block_list`, `block_append`, `block_replace`, `block_update`, `block_delete`) are `@_tool` functions in `blocks.py`. All reachable via FastMCP. Cross-refs: BC-T19/T20 = BC-IC1, BC-T21 = BC-IC2, BC-T22/T23 = BC-IC3, BC-T24/T25 = BC-IC4.
- **explanation:** In-context blocks are directory-scoped named key-value stores for ephemeral session data that does not belong in episodic memory. `block_create` creates a labelled block; `block_get` retrieves it; `block_list` returns all blocks for a directory (no cross-project leakage); `block_append` and `block_replace` mutate the stored value; `block_update` patches label or value fields; `block_delete` removes the block so subsequent `block_get` returns not-found. Useful for scratch state, task queues, or inter-agent communication within a session.

---

### CAP-INFRA-028 — MCP tool surface: wiki bookmarks

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T26`, `BC-T27`, `BC-T28`, `BC-T29`
- **refs:** `yadgar/core/server/tools/bookmarks.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `bookmark_add`, `bookmark_list`, `bookmark_remove`, `bookmark_reorder` are `@_tool` functions in `bookmarks.py`. All reachable via FastMCP. Cross-refs: BC-T26..T29 = BC-G7.
- **explanation:** Wiki bookmarks allow pinning frequently-accessed wiki slugs for quick retrieval without a full text search. `bookmark_add` pins a slug; `bookmark_list` returns the ordered list; `bookmark_remove` unpins; `bookmark_reorder` changes the display order. These four tools implement the bookmark CRUD surface (BC-G7) using real-path storage (SurrealDB), meaning bookmarks persist across sessions and are returned by real-path integration tests.

---

### CAP-INFRA-029 — MCP tool surface: wiki core read/write

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T30`, `BC-T31`, `BC-T32`, `BC-T33`, `BC-T34`, `BC-T35`, `BC-T39`, `BC-T40`, `BC-T41`, `BC-T42`, `BC-T43`, `BC-T44`, `BC-T45`
- **refs:** `yadgar/core/server/tools/wiki.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** All 13 tools (`wiki_add`, `wiki_get`, `wiki_read`, `wiki_query`, `wiki_list`, `wiki_update`, `wiki_check_duplicate`, `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`, `wiki_set_metadata`, `wiki_lint`) are `@_tool` functions in `wiki.py`. All reachable via FastMCP.
- **explanation:** These thirteen tools form the core wiki CRUD and versioning surface. `wiki_add`/`wiki_update` write pages with directory scoping and create immutable `wiki_page_version` records (BC-G4). `wiki_read`/`wiki_get` look up by slug with §25 directory resolution. `wiki_query` performs semantic search scoped to a directory (BC-G2). `wiki_check_duplicate` runs the similarity gate (BC-G6). `wiki_history`/`wiki_read_version`/`wiki_diff`/`wiki_restore` expose the immutable version history. `wiki_set_metadata` patches metadata across all rows for a slug (BC-G10). `wiki_lint` validates a page against the wiki lint rules.

---

### CAP-INFRA-030 — MCP tool surface: wiki edit primitives

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T46`, `BC-T47`, `BC-T48`, `BC-T49`, `BC-T50`, `BC-T51`, `BC-T52`, `BC-T53`, `BC-T54`, `BC-T55`, `BC-T56`, `BC-T58`
- **refs:** `yadgar/core/server/tools/wiki.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `wiki_append_section`, `wiki_insert_at`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_replace_text`, `wiki_replace_markdown_block`, `wiki_delete`, `wiki_delete_at`, `wiki_delete_text` are in `wiki.py`. All `@_tool`, all reachable via FastMCP.
- **explanation:** These tools provide surgical positional edit operations on wiki pages (BC-G9): append a section, insert at/after/before a line, replace at a position, replace by text pattern, replace a markdown block, delete a page, delete at a line, delete by text pattern. All edits are versioned (each write creates a new `wiki_page_version` record per BC-G4). Note: `wiki_coverage` and `wiki_refresh_stale` were removed in #83 Car C (ADR-0157 — container-blind anti-pattern).

---

### CAP-INFRA-031 — MCP tool surface: consolidation + vacuum + admin ops

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T59`, `BC-T60`, `BC-T61`, `BC-T62`, `BC-T63`, `BC-T64`, `BC-T65`, `BC-T66`, `BC-T67`, `BC-T68`, `BC-T69`, `BC-T70`, `BC-T71`, `BC-T72`
- **refs:** `yadgar/core/server/tools/admin.py`, `yadgar/core/server/tools/admin_vacuum.py`, `yadgar/core/server/tools/admin_archive.py`, `yadgar/core/server/tools/admin_dlq.py`, `yadgar/core/server/tools/admin_invariants.py`, `yadgar/core/server/tools/admin_other.py`, `docs/contracts/BEHAVIOR_CONTRACT.md`
- **wiring:** `consolidate_now` (=BC-C1) and `check_invariants` (=BC-C1) are in `admin.py` or `admin_invariants.py`; `reembed_all` (=BC-ADM1) in `admin_other.py` or `admin.py`; `vacuum_now` (=BC-E1..E3) and `vacuum_checkpoints` in `admin_vacuum.py`; `archive_purge` (=BC-ADM5) in `admin_archive.py`; `forget` (=BC-ADM2), `memory_get`, `memory_update` (=BC-ADM6), `memory_stats`, `validate_memory` (=BC-ADM3) in `admin_other.py` or `admin.py`; `dlq_inspect`, `dlq_requeue` (=BC-ADM4), `dlq_dismiss` in `admin_dlq.py`. All `@_tool`, all reachable via FastMCP.
- **explanation:** These fourteen tools expose the administrative and operational surface. `consolidate_now` triggers an immediate consolidation cycle (episodic→semantic, sleep phases). `reembed_all` re-embeds every row with a missing embedding. `vacuum_now` performs the atomic database vacuum (BC-E1: row counts preserved, BC-E2: atomicity, BC-E3: sensitive-job lock). `vacuum_checkpoints` cleans up stale checkpoint records. `archive_purge` deletes archived memories older than a threshold. `forget`/`memory_get`/`memory_update`/`memory_stats`/`validate_memory` provide individual memory lifecycle management. `check_invariants` runs the full invariant check suite and returns violation counts. `dlq_inspect`/`dlq_requeue`/`dlq_dismiss` manage the dead-letter queue for failed write operations.


### CAP-INFRA-032 — Capability-registry coverage lint (I32, this document)

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I32`
- **refs:** `scripts/check_capability_coverage.py`, `yadgar/tests/core/test_capability_coverage.py`, `docs/contracts/CAPABILITY_REGISTRY.md`
- **wiring:** Enforced by `scripts/check_capability_coverage.py` (pre-commit hook `check-capability-coverage` + CI `invariant-checks` step) and the pytest `yadgar/tests/core/test_capability_coverage.py`. AST-enumerates the four authoritative surfaces (Settings fields in `config.py`, `@_tool` decorators in `server/tools/`, `_migration_NNN` functions, `BC-*` rows in BEHAVIOR_CONTRACT) and asserts every item is referenced by some entry in this file; flags ORPHAN (uncatalogued), STALE (entry cites a vanished item), and MALFORMED (bad status / unresolved ref).
- **explanation:** This is the self-referential invariant that keeps THIS registry honest. It guarantees catalogue completeness, not status correctness (see "Scope of the guarantee" at the top). It makes the registry the durable source of truth the e2e behavior contract, the v6 plan, and the #41 dead-config audit all draw "what exists" from — adding any surface item without cataloguing it here fails the build.


### CAP-INFRA-033 — Tri-signal observe-coverage lint (I33)

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `scripts/check_observe_coverage.py`, `yadgar/_shared/observability/observe.py`, `.observe-allowlist.json`, `yadgar/tests/scripts/test_check_observe_coverage.py`, `yadgar/tests/server/test_observe_decorator.py`, `docs/contracts/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by `scripts/check_observe_coverage.py` (pre-commit hook `check-observe-coverage` + CI `invariant-checks` step). AST-classifies every function under `yadgar/` (excluding tests) as SATISFIED (`@trace_span`/`@_tool`/`@observe`/`_rpc_span` span source), auto-exempt (dunder/property/trivial), allowlisted-exempt (`.observe-allowlist.json`), or MISSING. Ships **warn-mode** (`--warn`, exit 0, baseline 1555 MISSING); allowlist integrity (stale / rationale ≥40 chars / valid category) is always hard, mirroring I30. The `@observe(tier=...)` decorator (`yadgar/_shared/observability/observe.py`) emits span+metric+log via the shared bounded families `yadgar_observe_{requests_total,request_duration_seconds,stage_duration_seconds,stage_errors_total}`.
- **explanation:** The ratchet that makes the full-observability standard durable rather than a decaying one-time sweep (I33). Per-area rollout waves flip `--area <name>` to hard-fail as each reaches 100%, ending in a global hard-fail. The tier is the "documented reason not to instrument" the directive demands; the anti-cardinality design (shared families, no per-function histogram) keeps the incremental series ceiling ≤ ~6,500 vs ~19,500 naive.


### CAP-EVAL-001 — v6 Phase 0 eval harness (native golden set + make eval)

- **status:** LIVE
- **category:** quality
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `benchmarks/run_eval.py`, `benchmarks/build_golden_bootstrap.py`, `benchmarks/golden/golden_set.jsonl`, `benchmarks/reports/`, `Makefile`, `docs/plans/PLAN_V6_QUALITY_FOUNDATION.md`
- **wiring:** `make eval` → `benchmarks/run_eval.py` → loads `benchmarks/golden/golden_set.jsonl`, spawns isolated SurrealDB via `spawn_surreal_for_benchmark()` (reused from `run_longmemeval.py`), runs yadgar `recall()` per golden pair, scores recall@k/MRR/nDCG@k + latency p50/p95, prints summary table, writes JSON report to `benchmarks/reports/`. Reuses `compute_recall()`/`compute_ndcg()` primitives from `run_longmemeval.py`. Bootstrap generator: `benchmarks/build_golden_bootstrap.py` samples stored memories and derives paraphrased queries — outputs a BOOTSTRAP-marked set REQUIRING HUMAN CURATION. CI: non-gating `workflow_dispatch` job in `.forgejo/workflows/eval.yaml`.
- **explanation:** The v6 Phase 0 keystone. Converts "I believe recall is good" into a measured number against a committed baseline. The golden set is intentionally marked as a bootstrap (auto-drafted from stored memories) and must be curated before the harness graduates to a gating quality check. The baseline report in `benchmarks/reports/baseline-v5.74.md` is committed so there is a number to track regressions against. Phase-0 exit criterion: `make eval` runs locally and in CI; baseline numbers are committed.


### CAP-EVAL-002 — v6 Phase 0.2 data-quality metrics (Prometheus + yadgar stats)

- **status:** LIVE
- **category:** quality
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/_shared/observability/metrics.py`, `yadgar/core/cli/stats.py`, `yadgar/tests/scripts/test_v6_data_quality_stats.py`, `docs/plans/PLAN_V6_QUALITY_FOUNDATION.md`
- **wiring:** Seven Prometheus Gauges declared in `yadgar/_shared/observability/metrics.py` (v6 Phase 0.2 block): `yadgar_data_quality_embedding_valid_ratio`, `yadgar_data_quality_null_embedding_count`, `yadgar_data_quality_duplicate_rate`, `yadgar_data_quality_zombie_rate`, `yadgar_data_quality_domain_coverage`, `yadgar_data_quality_surprise_p50`, `yadgar_data_quality_surprise_p95`. Writers: `_collect_data_quality()` in `yadgar/_shared/observability/metrics.py` (called on every `/metrics` scrape, alongside `_collect_queue_depths()`). Stats CLI: `_query_data_quality()` in `yadgar/core/cli/stats.py` populates `StatsData.dq_*` fields, printed in the `DATA QUALITY (v6 Phase 0.2)` section of `yadgar stats` output and included in the JSON output from `yadgar stats --format json`. I23 compliance: `check_metric_writers.py` verifies `_collect_data_quality()` as the writer for all seven gauges.
- **explanation:** The Phase-0.2 dashboard metrics that make corpus health visible without running the full eval harness. Null-embedding count is the hardest signal: `embedding_valid_ratio < 1.0` indicates the corruption class that the v5.66 zombie purge and today's reembed_all fix targeted. Duplicate-rate (sim-links / active memories) measures write-gate efficiency. Zombie-rate (stale / total) measures consolidation health. Domain-coverage measures astrocyte effectiveness. Surprise distribution (p50/p95) provides a histogram summary for Phase-1 write-gate tuning. All seven are best-effort (swallowed DB errors) so a degraded DB doesn't break the /metrics endpoint.


### CAP-WIKI-001 — ADR-consultable: per-ADR canonical pages + index + adr_add/adr_get/adr_list (car 2)

- **status:** LIVE
- **category:** mcp-tool
- **settings:** `ADR_DUE_WARN_HOURS`
- **tools:** `adr_add`, `adr_get`, `adr_list`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/adr.py`, `yadgar/core/server/tools/project.py`, `yadgar/tests/core/test_adr.py`
- **wiring:** `adr_add`/`adr_get`/`adr_list` are `@_tool(power=True)` functions in `yadgar/core/server/tools/adr.py`. Car 2 replaced the write-only `<project>-adr-log` monolith with recall-native records: one canonical wiki page per ADR (`<project>-adr-NNNN`, page_type `adr`, tags `["adr","decisions","adr-status:<status>","adr-<NNNN>"]`) plus a thin `<project>-adr-index` metadata table. Both are written via the server-side `_wiki_write_canonical` seam so they resolve from any working tree AND in non-git dirs — closing the memory-531352 default-branch-pin bug (`aws-work-adr-log` mis-pinned "master"). `adr_add` assigns the next ID from the index (max+1 under the per-project `_adr_log_lock`), writes the per-ADR page, appends the index row, and flips supersede targets' `adr-status:*` tag; the index create/first-row path uses `wait=True` for read-your-writes ID correctness. `adr_get(adr_id)` reads `<project>-adr-NNNN` canonical; `adr_list(status=None)` reads the index rows with an optional status filter. `_build_adr_log` + `_get_adr_log_updated_at` + project_brief `## Recent ADRs` re-point to the canonical index. The `_apply_adr_signal` nudge stays.
- **explanation:** Architecture Decision Records are durable artefacts linking decisions to context and consequences. Car 2 makes them consultable (recall-native): wiki pages never decay, the default recall profile fuses the wiki arm, and `"adr"` is not in `wiki_exclude` so ADR pages surface in `recall`. `adr_add` provides the 10-field structured capture surface; `adr_get`/`adr_list` give deterministic direct-fetch and "show all open" without a branch footgun. ID assignment is sequential from the index (regex `^## ADR-(\d{4})` re.MULTILINE header scan for the migration's monolith parse). CANONICAL branch resolves the ADR-log asymmetry (feature-branch / non-git sessions previously read an empty or master-pinned log).


### CAP-TASK-001 — SQL task ledger: task_write / task_list / task_get (Car D, 0047 spine train)

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** `task_write`, `task_list`, `task_get`
- **migrations:** `002_ledger_tables` (Car A — creates `task` + `task_blocked_by`)
- **bc:** —
- **refs:** `yadgar/core/server/tools/task.py`, `yadgar/backend/admin_exec/ledger.py`, `yadgar/_shared/storage/sql/mariadb.py`, `yadgar/tests/core/test_task_tools.py`
- **wiring:** Car D of the 0047 spine train — three MCP tools that sit on top of the `task` ledger table (Car A) and the backend `yadgar.backend.admin_exec.ledger` op bodies (Car B read surface + Car D write surface). All three are `@_tool()`-registered in `yadgar/core/server/tools/task.py`: `task_write` is `@_tool(power=True)`, `task_list` and `task_get` are read-only. Per §15 / ADR-0078, the tools forward over HTTP to the backend PTC via `_forward_admin` (op-name = tool-name); they do NOT call `_get_storage()` directly (the PR #32 reference implementation violated §15 — fixed by Car D). Car D also adds the corresponding backend write ops `create_task_row` / `update_task_row` in `yadgar/backend/admin_exec/ledger.py` (Car B delivered only the read surface); they translate the dict payload into the typed call into `MariaStorageEngine`, then reconcile the `task_blocked_by` join edges (D39) via delete-then-insert on UPDATE.
- **explanation:** Replaces the markdown `{project}-task-list` wiki page as the **source of truth** for task tracking (ADR-0133). After Car D, task reads/writes go through the ledger, not through page parsing; the wiki page becomes a derived mirror via `wiki_write_task_list` (the stop-hook checkpoint writer). Key invariants: (a) `task_write` create returns the AUTO_INCREMENT `id` — no `number` column / no allocation step (ADR-0197, §14.1); (b) `task_list` defaults to open-only `status IN (pending, in_progress)` (D37); (c) `task_write` clears `state` to NULL when `status` → `completed`/`archived` (§16.10, tool-layer enforcement); (d) `blocked_by`/`blocks` manage the `task_blocked_by` join edges (D39); (e) no `origin` parameter (§14.1 dropped); (f) title ≤ 200 chars (D12, reject-on-write); (g) payload keys use `id`, never `number` (§13.2 blocker 2). `project_id` arrives from the caller per ADR-0202 — passing a different value IS the cross-project override (§16.6); the tool never derives it internally. `body_slug` is stamped only when the caller supplies one — task bodies are optional, the description lives in `title`/`active_form`/`plan_path`. The `_format_task_id(id)` helper emits the `[<id>]` harness-render prefix (D11); the base32 display (D10) is applied at render time, not stored. `task_list` returns the rows list extracted from the backend's `{"rows": [...]}` envelope; `task_get` returns the row dict or `None`.


### CAP-WIKI-002 — Agent-prompt starter library seed (v5.85 S8)

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** `seed_agent_prompts`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/agent_prompts.py::seed_agent_prompts`, `yadgar/core/server/tools/agent_prompts.py::STARTER_PROMPTS`, `yadgar/core/server/http.py::hook_seed_agent_prompts`, `yadgar/core/cli/seed.py::_seed_agent_prompts`, `scripts/install/yadgar-setup.sh::_step_seed_agent_prompts`
- **wiring:** `seed_agent_prompts` is `@_tool(power=True)` in `agent_prompts.py`. It iterates the 4 `STARTER_PROMPTS` constants (patterns: `code-review`, `debug-investigate`, `explore-codebase`, `implement-tdd`), checks each via `_read_agent_prompt(slug)` (create-if-absent), and calls `agent_prompt_save` for absent patterns. TOC and library anchor are managed by `agent_prompt_save`; this function does not duplicate that logic. REST hook `/hooks/seed-agent-prompts` in `http.py` wraps the function for daemon-side execution. CLI: `yadgar seed --agent-prompts` posts to `/hooks/seed-agent-prompts`; dry-run prints without HTTP calls. Install step `_step_seed_agent_prompts` in `yadgar-setup.sh` (step 11/11) waits for daemon, then calls `yadgar seed --agent-prompts`.
- **explanation:** Bootstraps the agent-prompt library with 4 opinionated dispatch starters so a fresh installation has immediately usable patterns for the most common subagent tasks (code review, debug, codebase exploration, TDD implementation). Idempotent: any pattern already saved is skipped; the TOC and discovery anchor are guaranteed by `agent_prompt_save`'s existing S6 logic, so the seeder never creates duplicates regardless of how many times it runs.

### CAP-INFRA-033 — Test-only offload-dispatch probe tools (daemon-offload-A)

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** `_test_sleep`, `_test_thread_id`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/_test_tools.py::register_test_tools`, `yadgar/core/server/tools/__init__.py::_register_test_tools`
- **wiring:** `register_test_tools()` is called at import time from `yadgar/core/server/tools/__init__.py`. It is a no-op unless `YADGAR_TEST_TOOLS=1` (or `true`/`yes`/`on`) is set in the environment. When enabled, registers two `@_tool`-decorated functions: `_test_sleep(seconds)` blocks the calling thread via `time.sleep` (proves offload boundary — inline → loop starvation, offloaded → loop free); `_test_thread_id()` returns the executing thread's ident/name (proves worker dispatch). Both tools are never exposed in production; the env gate ensures zero surface area outside e2e test runs.
- **explanation:** Fix A (daemon-offload-A) requires end-to-end verification that blocking tool bodies run on worker threads when `OFFLOAD_TOOLS=true`, not on the asyncio loop thread. A deterministic sleep body and a thread-identity probe are the smallest reproducible fixtures for this — they have no external dependencies (no git, no DB, no HTTP) and produce deterministic, flake-free results. Gated behind `YADGAR_TEST_TOOLS` so the production MCP surface is unchanged.

### CAP-INFRA-035 — claude_code hook emitter: settings.json writer via shared `install_hooks_impl`
- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/install/clients/hooks_render.py::_emit_claude_json`, `yadgar/core/install/clients/hooks_render.py::_YADGAR_HOOK_MARKER` (foreign-preserve: not applicable — single artifact, not shared with user hooks), `yadgar/core/install/install_hooks_lib.py::install_hooks_impl`, `yadgar/core/install/clients/hooks_render.py::register_hooks`
- **wiring:** The first per-client hook emitter added to the `_EMITTERS` dispatch table (Car 0 of the multi-client harness hooks train). The per-kind emitter in `hooks_render._emit_claude_json` delegates to the shared `install_hooks_impl` in `yadgar/core/install/install_hooks_lib.py` (one path for Claude Code; Car 0 specifically chose delegation over re-implementation to inherit idempotency + dry-run support). `install_hooks_impl` writes `~/.claude/settings.json` (global scope) or `.claude/settings.json` (project scope), the durable-interpreter resolver (pipx shebang), and 5 hook types: SessionStart (all + compact), PreCompact, PostToolUse, UserPromptSubmit, PreToolUse. Container detection via `is_running_in_container()` short-circuits to a refused status pointing at the host-side command. Reached via `install_client("claude-code", ..., hooks=True)` (per Car 2 of the opencode port train, all clients with a hooks_kind now route through the unified orchestrator). Foreign-preserve: the settings.json writer uses `_append_if_absent` for yadgar-managed entries; user-installed foreign hooks survive. The MCP `install_hooks` tool (CAP-OPS-010) wraps this emitter; the legacy `yadgar install-hooks` CLI is hard-removed in Car 7 of the opencode port train (migrates users to the unified command).
- **explanation:** The original yadgar hooks subsystem predates the multi-client porting work. Car 0 added the per-kind emitter seam so all clients with a hook surface could share the dispatch table; this entry (CAP-INFRA-035, written in Car 9 of the opencode port train's follow-up F5) catalogues the existing emitter explicitly so the I32 capability-registry coverage lint (which fires on new emitters) doesn't get re-litigated every time someone touches the hooks_render module. The 5 hook types are the ones yadgar has shipped since v5.1.x; the dispatch table integration is what makes them pluggable per-client rather than Claude-Code-only.

### CAP-INFRA-036 — cursor hook emitter: hooks.json writer with foreign-append
- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/install/clients/hooks_render.py::_emit_cursor_hooks`, `yadgar/core/install/clients/hooks_render.py::_cursor_hooks_path`, `yadgar/core/install/clients/hooks_render.py::_CURSOR_EVENT_MAP`, `yadgar/core/install/clients/hooks_render.py::_YADGAR_HOOK_MARKER`, `yadgar/core/install/clients/hooks_render.py::_merge_cursor_hook_entry`, `yadgar/core/install/clients/hooks_render.py::register_hooks`
- **wiring:** The second per-client hook emitter added to the `_EMITTERS` dispatch table (Car B of the multi-client harness hooks train, 2026-07-20). The per-kind emitter in `hooks_render._emit_cursor_hooks` writes `.cursor/hooks.json` (project scope) or `~/.cursor/hooks.json` (global scope) with the schema `{version: 1, hooks: {<event>: [{...}]}}`. Cursor runs EVERY command registered for an event, so the writer uses `_merge_cursor_hook_entry` to APPEND yadgar's entry alongside any user-installed ones (NOT clobber). Foreign-preserve is real here (unlike opencode's plugin-file single-artifact model): the writer keeps the user's version + all non-yadgar event entries, and replaces yadgar's entry in place on re-run (matched by the `yadgar hook ` marker in the command). Coverage: only 2 of Cursor's ~18 hook types — `postToolUse` (→ `yadgar hook post-tool-capture`) and `preCompact` (→ `yadgar hook pre-compact-drain`). The remaining 16 (including `sessionStart`, `beforeSubmitPrompt`, `stop`) are NOT wired because Cursor's inject path (`additional_context` on `sessionStart`/`postToolUse` + `beforeSubmitPrompt` output) is broken upstream (accepted+merged but never surfaced to the model — see ADR-0143 re-verification notes) and Cursor's `stop` is observation-only (`followup_message` auto-continues, does not block). Wiring these would fake a non-functional hook (plan R7 forbids). The Car 1 cursor hook re-audit confirmed: NO inject, NO blocking stop, just the 2 fire-and-POST hooks.
- **explanation:** The cursor port deliberately ships a SMALL surface (2 events) because the larger surface isn't actually functional. Per plan R7, never faking a hook is the load-bearing rule. The entry catalogues the cursor-specific constraint (additional_context bug + observation-only stop) so a future maintainer reading the registry doesn't think "0/5 = bug, why isn't cursor wired up" — it's 2/2-FUNCTIONAL = 0/18-claimed = honest, and the missing 16 are explicitly out-of-scope due to upstream bugs, not yadgar's work. Foreign-preserve via the marker-detect + append pattern is the load-bearing piece (vs claude_code's settings.json which is foreign-preserve-by-format) — Cursor's hooks.json requires explicit per-entry handling because the format doesn't auto-dedupe.

### CAP-INFRA-037 — backup_restore MCP tool + snapshot restore CLI (Car J, 2026-08-14 train)
- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** `backup_restore`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/core/server/tools/misc.py::backup_restore`, `yadgar/core/cli/snapshot.py::restore`, `yadgar/__main__.py` (snapshot subcommand wiring), `yadgar/core/backup/backup.py::restore_snapshot`
- **wiring:** `backup_restore(snapshot_id, backend_url=None)` is a registered `@_tool` in `yadgar/core/server/tools/misc.py` that triggers a corpus restore from a SurrealDB snapshot. The argument name is `snapshot_id` for the MCP contract, but the value is the path to the snapshot artifact (a `.surql` file from `create_snapshot(backend_url=...)`, or a directory in the quiesced-backend representation); the underlying helper resolves it as a `Path` and delegates to `yadgar.core.backup.restore_snapshot`. The CLI mirrors it: `yadgar snapshot restore <snapshot_id> [--backend-url URL]`. `backend_url` defaults to `$YADGAR_DB_URL` (the same default the vacuum snapshot path uses). 22 new tests (11 CLI + 11 MCP) cover happy path, backend-down failures, snapshot-id validation, and the default-backend-url resolution. Complements CAP-ENR-010 (vacuum/backup safety) which owns the snapshot creation side.
- **explanation:** Adds the operator path to roll a corpus back to a known-good snapshot. Distinct from `vacuum_now` (which compacts in place) and `vacuum_checkpoints` (which lists checkpoints): this tool's job is to RESTORE from a snapshot the operator already took via `vacuum_now --snapshot`. The MCP tool is intentionally write-dangerous (it mutates state across the whole backend) so it sits alongside the vacuum/ops tools rather than the routine admin tools; auth scoping keeps it out of agent reach.
