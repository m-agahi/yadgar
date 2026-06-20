# Yadgar Capability Registry

**The single source of truth for every feature, algorithm, and behaviour Yadgar
has — wired or not.** If a capability exists in the codebase (a setting that
controls something, an MCP tool, a migration, a retrieval/consolidation
algorithm, a brain-dynamics mechanism), it has an entry here with: what it does,
how it is reached at runtime (its wiring), and its status.

This registry is **enforced** by invariant **I32**
(`scripts/check_capability_coverage.py`): every Settings field, every MCP
`@_tool`, every migration, and every `BC-*` behaviour in
`docs/BEHAVIOR_CONTRACT.md` MUST be referenced by at least one entry below.
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
- **refs:** `yadgar/retrieval/fusion.py::convex_fuse`, `yadgar/server/tools/recall.py`
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
- **refs:** `yadgar/retrieval/fusion.py::_FusionMixin._wrrf_fuse`, `yadgar/retrieval/fusion.py::_convex_fuse`, `yadgar/retrieval/fusion.py::_FusionMixin._fuse_scores`
- **wiring:** `recall` tool → `Retriever.recall()` → `_FusionMixin._fuse_scores()`. When `FUSION_METHOD="convex"` (default), calls module-level `_convex_fuse()` (min-max normalised weighted sum). When `FUSION_METHOD="wrrf"`, calls `_FusionMixin._wrrf_fuse()` (per-signal z-score or minmax normalisation then weighted sum). After fusion, `WRRF_GRAPH_PRIOR_WEIGHT` and `WRRF_COFIRE_PRIOR_WEIGHT` additive boosts are applied via `_apply_prior_boost()` (O(1) reads from precomputed columns). `COMBMNZ_ENABLED=False` by default — flipping to True multiplies the fused score by the number of signals that fired.
- **explanation:** Merges four retrieval signals (vector KNN, FTS BM25, PPR, spreading activation) into a single ranked list. The default `convex` fusion method normalises each signal's scores with min-max and computes a weighted sum; the alternate `wrrf` method uses z-score normalisation before weighting. After fusion two additive priors are applied: a graph-centrality prior (`graph_prior` column, weight 0.2) and a co-recall frequency prior (`cofire_prior` column, weight 0.15), both precomputed at consolidation time to avoid per-query graph traversal. `COMBMNZ_ENABLED` (default False) scales the fused score by the count of non-zero signals to reward multi-signal agreement.

---

### CAP-RETR-002 — Candidate Pool Sizing

- **status:** LIVE
- **category:** retrieval
- **settings:** `CANDIDATE_POOL_MULTIPLIER`, `FAST_PROFILE_CANDIDATE_MULTIPLIER`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/retrieval/core.py::Retriever._resolve_query_and_candidate_k`, `yadgar/retrieval/stages/knn.py`, `yadgar/retrieval/stages/fts.py`
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
- **refs:** `yadgar/config.py`
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
- **refs:** `yadgar/retrieval/fusion.py::PROFILES`, `yadgar/retrieval/core.py::Retriever.recall`
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
- **refs:** `yadgar/retrieval/query_analysis.py::analyze_query`, `yadgar/retrieval/query_analysis.py::_classify_query_type`
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
- **refs:** `yadgar/retrieval/query_analysis.py::_pseudo_hyde_expand`, `yadgar/retrieval/scoring.py::_ScoringMixin._build_vector_search_list`
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
- **refs:** `yadgar/retrieval/query_analysis.py::_build_open_domain_subqueries`, `yadgar/retrieval/query_analysis.py::_collect_semantic_expansions`, `yadgar/retrieval/query_analysis.py::_build_boosted_fts_query`
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
- **refs:** `yadgar/retrieval/core.py::Retriever.ppr_retrieve`, `yadgar/retrieval/scoring.py::_ScoringMixin._collect_ppr_scores`, `yadgar/retrieval/graph_helpers.py::_GraphHelpersMixin._build_networkx_graph`
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
- **refs:** `yadgar/retrieval/core.py::Retriever.spreading_activation`, `yadgar/retrieval/scoring.py::_ScoringMixin._collect_spreading_scores`
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
- **refs:** `yadgar/retrieval/graph_helpers.py::_GraphHelpersMixin._find_memories_for_entity`, `yadgar/retrieval/graph_helpers.py::_GraphHelpersMixin._find_entities_in_content`, `yadgar/retrieval/scoring.py::_ScoringMixin._run_entity_fts`
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
- **refs:** `yadgar/retrieval/temporal.py::parse_temporal_expression`, `yadgar/retrieval/scoring.py::_ScoringMixin._collect_temporal_scores`
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
- **refs:** `yadgar/retrieval/_reranking_heuristic.py::_HeuristicMixin.heuristic_rerank`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_heuristic`
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
- **refs:** `yadgar/retrieval/_reranking_cross_encoder.py::_CrossEncoderMixin.cross_encoder_rerank`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_cross_encoder`
- **wiring:** `_apply_rerank_pipeline()` → `_rerank_cross_encoder()` when `use_cross_encoder=True` (profile allows CE AND `CROSS_ENCODER_ENABLED=True`, default True). CE is blocked entirely by `HEAVY_RERANK_ENABLED=False`. The ML client (`LocalMLClient` or `RemoteMLClient`) does the model inference; this mixin handles normalization and blending.
- **explanation:** Scores each candidate memory against the query using a cross-encoder model (default FlashRank ONNX, fast on CPU). In open-domain mode, each memory may contribute a second "implied facts" variant (derived from content inference) alongside its base text; the max score across variants is kept. Scores are min-max normalised and blended with the existing retrieval score at `CROSS_ENCODER_WEIGHT` (default 0.6). `HEAVY_RERANK_ENABLED=False` is a kill switch that bypasses CE, NLI, and multi-passage entirely to eliminate CPU burst on constrained hosts.

---

### CAP-RETR-015 — GTE Reranker (Advanced CE Backend)

- **status:** DORMANT
- **category:** retrieval
- **settings:** `GTE_RERANKER_ENABLED`, `GTE_RERANKER_MODEL`, `GTE_RERANKER_MAX_LENGTH`, `GTE_RERANKER_FALLBACK_TO_FLASHRANK`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/ml_client.py`, `yadgar/config.py`
- **wiring:** `GTE_RERANKER_ENABLED=True` in config, but the ML client (`LocalMLClient`) uses it only when the GTE model is actually loaded (conditional in `ml_client.py` line 356: `getattr(settings, "GTE_RERANKER_ENABLED", False)`). Loading requires the `Alibaba-NLP/gte-reranker-modernbert-base` model to be present. If unavailable, `GTE_RERANKER_FALLBACK_TO_FLASHRANK=True` falls back to FlashRank. In practice, GTE is not auto-downloaded — it is dormant unless the operator has staged the model weights.
- **explanation:** A higher-quality cross-encoder backend based on GTE-ModernBERT-base that replaces or supplements FlashRank for more accurate relevance scoring. `GTE_RERANKER_MAX_LENGTH` caps the input token length. When `GTE_RERANKER_FALLBACK_TO_FLASHRANK=True` (default), the system degrades gracefully to FlashRank if GTE cannot be loaded. The setting is technically enabled by default but effectively dormant because the model weights are not included.

---

### CAP-RETR-016 — NLI Entailment Reranker

- **status:** DORMANT
- **category:** retrieval
- **settings:** `NLI_RERANKING_ENABLED`, `NLI_MODEL`, `NLI_WEIGHT`, `NLI_ONLY_FOR_OPEN_DOMAIN`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR2`
- **refs:** `yadgar/retrieval/_reranking_nli.py::_NLIMixin.nli_rerank`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_nli`
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
- **refs:** `yadgar/retrieval/_reranking_multi_passage.py::_MultiPassageMixin.multi_passage_rerank`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_multi_passage`
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
- **refs:** `yadgar/retrieval/_reranking_confidence.py::_ConfidenceMixin.detect_adversarial`, `yadgar/retrieval/_reranking_mmr.py::_MMRMixin.mmr_rerank`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_adversarial_detect`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_mmr`
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
- **wiring:** `_apply_confidence_gating()` and its call site in `_fuse_scores()` removed from `yadgar/retrieval/fusion.py` in v6 T3. `CONFIDENCE_GATING_ENABLED` and related settings removed from `config.py`.
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
- **refs:** `yadgar/retrieval/fusion.py::_FusionMixin._comparison_dual_search`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_comparison_merge`
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
- **wiring:** `_dual_vector_search()` and `DUAL_VECTORS_ENABLED` deleted from `yadgar/retrieval/core.py` and `config.py` in v6 T3. `IMPLICIT_EMBEDDING_MODEL` retained in config.py as CONFIG-ONLY — no production consumer after method removal, but kept as placeholder for future DualCSE implementation.
- **explanation:** Prep scaffold for dual-embedding architecture removed in v6 T3 (#41). `DUAL_VECTORS_ENABLED` setting and `_dual_vector_search()` method deleted. `IMPLICIT_EMBEDDING_MODEL` remains for future DualCSE work.

---

### CAP-RETR-023 — Hopfield Engram Configuration

- **status:** LIVE
- **category:** retrieval
- **settings:** `HOPFIELD_BETA`, `HOPFIELD_MAX_PATTERNS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-H1`, `BC-H2`, `BC-H3`
- **refs:** `yadgar/engram.py`
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
- **refs:** `yadgar/cognitive_map.py::CognitiveMap`, `yadgar/server/tools/recall.py`
- **wiring:** `recall` tool records a transition from `_last_recalled_ids[session_key]` to the new top result via `_st._cognitive_map.record_transition()` and `incremental_update()` on every call. `CognitiveMap.__init__` reads `SR_DISCOUNT` (γ, default 0.9) and `SR_UPDATE_RATE` (η, default 0.1). The SR matrix is built during consolidation and can be used for context-predictive retrieval.
- **explanation:** Maintains a Successor Representation matrix over memory transitions — a TD-learning model that predicts which memories are likely to be retrieved after the current one. Every `recall` call logs the (previous_top_id → current_top_id) transition. The discount factor γ controls how far into the future the representation looks; the learning rate η controls update speed. The SR matrix is primarily used by the restoration and project-brief systems to predict likely next-retrieval candidates.

---

### CAP-RETR-025 — Astrocyte Pool Consensus Retrieve

- **status:** DEAD
- **category:** retrieval
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-AC3a`, `BC-AC3b`
- **refs:** `yadgar/astrocyte_pool.py::AstrocytePool.consensus_retrieve`
- **wiring:** `consensus_retrieve()` is defined on `AstrocytePool` and tested in `tests/test_astrocyte_pool.py`. No production call site exists outside tests — grep for `consensus_retrieve` in non-test code finds only the definition. The BEHAVIOR_CONTRACT marks BC-AC3a as `❌ #41` (failing).
- **explanation:** Intended to merge retrieval results across all domain-specialist astrocyte processes into a single ranked consensus list. Each domain retriever's results would be normalized and score-fused. Currently implemented but not called from any MCP tool, hook, or consolidation cycle. Listed in the behavior contract as a failing invariant (#41). Dead code; BC-AC3b covers the "if disabled, emit warning" case which is also pending.

---

### CAP-RETR-026 — Branch-Boost and Postmortem-Boost

- **status:** LIVE
- **category:** retrieval
- **settings:** `BRANCH_BOOST_WEIGHT`, `POSTMORTEM_BOOST_FACTOR`, `POSTMORTEM_BOOST_KEYWORDS`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/recall.py`
- **wiring:** Applied in the `recall` tool after retriever output, before final trimming. Branch boost fires when `_current_branch` is not None and iterates all merged results; postmortem boost fires when the query contains a keyword from `POSTMORTEM_BOOST_KEYWORDS` (default: action verbs like "fix", "incident") and a result has `_postmortem` or `_incident` tags. Both use the convex combination formula `base + (1 - base) × weight` to keep scores in [0, 1].
- **explanation:** Two score-adjustment passes applied in the MCP tool layer, not in the core retriever. Branch boost (`BRANCH_BOOST_WEIGHT=0.2`) elevates results from the caller's current git branch, surfacing branch-relevant context. Postmortem boost (`POSTMORTEM_BOOST_FACTOR=0.3`) elevates memories tagged `_postmortem` or `_incident` when the query sounds like incident investigation. After both passes the list is re-sorted and trimmed to `max_results`.

---

### CAP-RETR-027 — Recall Quality Floor and Dedup

- **status:** LIVE
- **category:** retrieval
- **settings:** `RECALL_QUALITY_FLOOR`, `RECALL_BOOST`
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR5`
- **refs:** `yadgar/server/tools/recall.py::_apply_quality_floor`, `yadgar/server/tools/recall.py::_dedup_by_content`
- **wiring:** Applied in the `recall` tool immediately after retriever output and branch/postmortem boosts. `_apply_quality_floor` drops results with `_cross_encoder_score` below `RECALL_QUALITY_FLOOR` (default 0.0 = disabled). `_dedup_by_content` collapses identical-content rows. `RECALL_BOOST` (default 0.05) is applied per-access in `consolidation/heat_decay.py` to increase memory heat on each retrieval.
- **explanation:** Two final hygiene passes. The quality floor targets keyword-only co-occurrence rows that score near zero on the cross-encoder; calibrated thresholds are 0.15–0.20 for production but default is 0.0 to avoid breaking tests with short synthetic content. Content deduplication collapses multiple rows with identical text (common for co-occurrence entries). `RECALL_BOOST` separately governs how much each `recall` access raises a memory's heat score in the heat-decay model.

---

### CAP-RETR-028 — Session Coherence Boost

- **status:** LIVE
- **category:** retrieval
- **settings:** `SESSION_COHERENCE_BONUS`, `SESSION_COHERENCE_WINDOW_HOURS`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/config.py`, `yadgar/consolidation/heat_decay.py`
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
- **refs:** `yadgar/retrieval/fusion.py::_FusionMixin._search_profiles_and_beliefs`, `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_profile_belief_merge`
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
- **refs:** `yadgar/embeddings.py::EmbeddingEngine`, `yadgar/remote_embeddings.py`
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
- **wiring:** Both settings deleted from `config.py` in v6 T3. Never read by production code — embedding engine uses `MODEL_QUERY_PREFIX` constant dict; embed service cache uses `YADGAR_EMBED_CACHE_MAX_ENTRIES`.
- **explanation:** Two dead configuration knobs removed in v6 T3 (#41). `QUERY_PREFIX` was never wired into `encode_query()`. `EMBEDDING_CACHE_SIZE` was superseded by env-var-driven LRU cache.

---

### CAP-RETR-033 — Reranking Semaphore and Backend Timeout

- **status:** LIVE
- **category:** retrieval
- **settings:** `RERANK_BACKEND_TIMEOUT_SEC`, `RERANK_MAX_CONCURRENCY`, `RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/embed_service.py`, `yadgar/backend/ml_client.py`
- **wiring:** `RERANK_MAX_CONCURRENCY` (default 1) and `RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` (default 2.0) are read at `embed_service` module initialisation to configure the asyncio semaphore that gates concurrent CE/NLI scoring requests. `RERANK_BACKEND_TIMEOUT_SEC` (default 90) is used as the timeout for remote ML backend calls in `LocalMLClient`.
- **explanation:** Guards the cross-encoder and NLI inference path against concurrent overload. A semaphore with `RERANK_MAX_CONCURRENCY` slots serialises concurrent scoring requests; if the semaphore cannot be acquired within `RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` the request is dropped with a counter increment. `RERANK_BACKEND_TIMEOUT_SEC` gives the backend inference itself a hard ceiling before aborting.

---

### CAP-RETR-034 — Recall MCP Tool

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RU3`, `BC-RU1`, `BC-RU2`
- **refs:** `yadgar/server/tools/recall.py::recall`
- **wiring:** Registered with `@_tool()` decorator. Invoked by Claude Code agents directly. Routes to `Retriever.recall_via_pipeline()` when `profile` kwarg is set, else `Retriever.recall()`. Applies directory validation, branch detection, branch/postmortem boosts, quality floor, dedup, and wiki blending.
- **explanation:** The primary MCP tool for episodic and semantic memory retrieval. Accepts a natural-language query, optional `max_results`, `min_heat`, `profile`, `directory`, and `branch_hint`. Orchestrates the full retrieval pipeline: multi-signal scoring → fusion → heuristic reranking → CE reranking → NLI → multi-passage → profile/belief merge → MMR → adversarial detection → rules → engram links → quality floor → dedup → branch/postmortem boosts → wiki blending. Returns a ranked list of memory dicts with `_retrieval_score`, `_cross_encoder_score`, heat, and metadata fields.

---

### CAP-RETR-035 — Rules Engine Reranking

- **status:** LIVE
- **category:** retrieval
- **settings:** —
- **tools:** `recall`
- **migrations:** —
- **bc:** `BC-RR4`, `BC-RU1`, `BC-RU2`, `BC-RU3`
- **refs:** `yadgar/retrieval/reranking.py::_RerankingMixin._rerank_rules`
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
- **refs:** `yadgar/consolidation/cls.py`
- **wiring:** Read by `yadgar/consolidation/cls.py` at three points: community detection and cluster summarization candidate selection are both bounded by this cap (default 4000). Not a retrieval-path setting but controls memory graph operations that feed the retrieval index.
- **explanation:** Caps the number of memory candidates processed in one similarity-matrix computation during nightly consolidation (community detection, cluster summarization). On large corpora, computing the full N×N similarity matrix is O(N²); this cap bounds memory and CPU consumption per consolidation cycle. Indirectly affects retrieval quality by limiting how many memories are considered for clustering and summarization.

---

### CAP-RETR-037 — Sleep Cycle (Dream Replay, Community, Compress, Narrate)

- **status:** LIVE
- **category:** retrieval
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-SC1a`, `BC-SC1b`, `BC-SC4`, `BC-SC5`, `BC-SC6`
- **refs:** `yadgar/consolidation/cls.py`
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
- **refs:** `yadgar/astrocyte_pool.py`
- **wiring:** Called during consolidation cycles. `assign_memory()` routes memories to domain-specialist astrocyte processes. `consolidate_domain()` runs domain-level summarization. Both are exercised in e2e tests (BC-AC1, BC-AC2 marked ✅).
- **explanation:** Domain-aware consolidation: memories are assigned to semantic domains (e.g. "code", "decisions") by the astrocyte pool. Each domain runs its own consolidation pass, producing domain summaries. This supplements the global consolidation by maintaining domain-coherent clusters. The pool can be disabled via `ASTROCYTE_POOL_ENABLED=False`; when disabled a startup warning is emitted (BC-C5b pending #40).

### CAP-RETR-039 — Unified Scoped Recall Fan-Out (v6 T6)

- **status:** DORMANT
- **category:** retrieval
- **settings:** `UNIFIED_RECALL_ENABLED`
- **tools:** `recall`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/retrieval/providers/base.py::SourceProvider`, `yadgar/retrieval/providers/base.py::Candidate`, `yadgar/retrieval/providers/memory.py::MemoryProvider`, `yadgar/retrieval/providers/wiki.py::WikiProvider`, `yadgar/server/tools/recall.py::_fanout_recall`
- **wiring:** When `UNIFIED_RECALL_ENABLED=True`, `recall()` routes through `_fanout_recall()` which builds a `[MemoryProvider, WikiProvider]` list, calls `candidates()` on each with a shared `Scope`, pools the results, and deduplicates by content. When `UNIFIED_RECALL_ENABLED=False` (default), the legacy path is taken with zero behavior change. Steps 3–5 (DB-level DirectoryFilter, cross-encoder fusion, `type=` param) are not yet wired.
- **explanation:** First step of the unified recall architecture (v6 T6 — `[[unified-scoped-recall]]`). `SourceProvider` is an ABC with `type: str` + `candidates(query, scope, limit) -> list[Candidate]`. `MemoryProvider` wraps `Retriever.recall()`; `WikiProvider` wraps `WikiStore.query()`. Both return `Candidate` dataclasses with a unified schema and a `raw` field for lossless pass-through. The fan-out is gated behind `UNIFIED_RECALL_ENABLED` (default False) so existing callers see no change until the full pipeline (Steps 3–5) ships.

---

### CAP-STOR-001 — SurrealDB transport layer and batch writes
- **status:** LIVE
- **category:** storage
- **settings:** `DB_PATH`, `MAX_BATCH_STATEMENTS`, `MAX_BATCH_BYTES`
- **tools:** —
- **migrations:** —
- **bc:** `BC-ST1`, `BC-ST2`, `BC-ST3`, `BC-ST4`
- **refs:** `yadgar/storage/client.py::_ClientMixin`, `yadgar/storage/client.py::batch_writes`
- **wiring:** Every MCP tool and consolidation cycle calls `StorageEngine._q()` or `batch_writes()`. `_q()` routes to `_q_server()` (HTTP POST to SurrealDB v3) when `YADGAR_DB_URL` is set, or `_q_embedded()` (Python surrealdb SDK, SurrealDB v2) otherwise. `DB_PATH` sets the embedded database file location (default `~/.local/share/yadgar/surreal_db/`). `batch_writes()` splits statements into chunks bounded by `MAX_BATCH_STATEMENTS` (default 500) and `MAX_BATCH_BYTES` (default 1 MB), each sent as a `BEGIN…COMMIT` HTTP transaction. Embedded mode executes statements individually without HTTP transactions. `BC-ST3`: embedded mode rewrites `type::record('t', $id)` → `t:{int}` via `_inline_int_record_ids`. `BC-ST4`: server vs embedded mode selected by presence of `YADGAR_DB_URL` env var.
- **explanation:** The transport layer is the only interface between all storage mixins and the SurrealDB database. In server mode it serialises queries as LET-preamble + SQL over HTTP and raises on `status=ERR` entries. In embedded mode it delegates to the Python SDK with a retry on read-only statements. `batch_writes` prevents oversized HTTP bodies by measuring the real serialised body size recursively; a single-statement chunk that still exceeds the limit is sent with a warning rather than silently dropped.

### CAP-STOR-002 — Schema initialisation and migration runner
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `001`, `002`, `003`, `004`, `005`, `006`, `007`, `008`, `009`, `010`, `011`, `012`, `013`, `014`, `015`, `016`, `018`, `019`, `020`, `021`, `022`
- **bc:** `BC-ST2`
- **refs:** `yadgar/storage/migrations.py::_MigrationsMixin`, `yadgar/storage/migrations.py::_run_migrations`, `yadgar/storage/migrations.py::_init_schema`
- **wiring:** `StorageEngine._init_schema()` is called once on startup from `StorageEngine.__init__()` (via `_ClientMixin` assembly). It defines all tables, analysers, and indexes, then calls `_run_migrations()`. `_run_migrations()` is a no-op in embedded mode; in server mode it acquires a file lock and calls `_run_migrations_locked()` which iterates `_MIGRATIONS` in order, skipping already-applied versions recorded in the `schema_version` table. Each migration is applied exactly once; the version string is appended to `schema_version` atomically after `fn(storage)` returns.
- **explanation:** The migration runner enforces forward-only, exactly-once schema evolution. It uses an flock on `STATE_DIR/.migration.lock` to serialise concurrent daemon starts. Migrations are append-only (never reordered or edited). The `_MIGRATIONS` list contains 21 entries (versions 001–022, with 017 reserved). All migrations use `DEFINE FIELD IF NOT EXISTS` or `DEFINE INDEX IF NOT EXISTS` DDL which is idempotent, so a failed-then-rerun migration does not corrupt the schema.

### CAP-STOR-003 — Migration 001: HNSW vector index upgrade
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `001`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_001_hnsw_indexes`
- **wiring:** Applied once on first server-mode startup after SurrealDB v3 upgrade. Drops old MTREE indexes on `memory.embedding`, `memory.implicit_embedding`, and `wiki_page.embedding`; recreates them as HNSW (EFC=150, M=12, COSINE, F32). Subsequent startups skip via `schema_version` guard.
- **explanation:** SurrealDB v3 introduced HNSW (Hierarchical Navigable Small World) vector indexes which outperform MTREE for ANN search at scale. This migration upgrades all three vector indexes to HNSW while leaving MTREE in place for the embedded (v2) SDK path defined in `_init_schema`.

### CAP-STOR-004 — Migration 002: relationship table performance indexes
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `002`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_002_relationship_indexes`
- **wiring:** Applied once at server-mode startup after v4.4.1. Adds `rel_source_target_idx` and `rel_target_source_idx` composite indexes on the `relationship` table.
- **explanation:** Adds bidirectional composite indexes (source→target and target→source) on the `relationship` table to speed up neighbourhood traversal and entity-linking lookups. Before this migration, relationship queries with `source_entity_id` or `target_entity_id` predicates required full-table scans.

### CAP-STOR-005 — Migration 003: memory_similarity_link table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `003`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_003_memory_similarity_link_table`, `yadgar/storage/cluster.py::_ClusterMixin`
- **wiring:** Applied once at server-mode startup after v4.4.2. Creates the `memory_similarity_link` SCHEMALESS table with a UNIQUE pair index. Used by `_ClusterMixin.insert_memory_similarity_link()` during consolidation CLS phase.
- **explanation:** Extracts memory-to-memory similarity edges from the entity table into a dedicated `memory_similarity_link` table, stopping entity-table bloat. The UNIQUE index on `(source_memory_id, target_memory_id)` prevents duplicate edges. The table stores cosine weight, creation/update timestamps, and bi-temporal validity columns added by migration 007.

### CAP-STOR-006 — Migration 004: branch field on memory and wiki_page
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `004`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_004_branch_field`, `yadgar/storage/branch.py::BranchFilter`
- **wiring:** Applied once at server-mode startup after v5. Adds `branch option<string>` column to `memory` and `wiki_page`; backfills pre-v5 rows to `'master'` in a single transaction. All subsequent memory and wiki inserts stamp the current branch. `BranchFilter` and `_build_branch_clause` use this field for scoped retrieval.
- **explanation:** Enables per-branch memory and wiki scoping. Rows with `branch IS NONE` or `branch = default_branch` are visible to any branch context; rows stamped with a feature branch are only visible when that branch is active. Backfilling pre-v5 rows to `'master'` ensures they surface correctly in the default-branch context.

### CAP-STOR-007 — Migration 005: provenance_agent field
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `005`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_005_provenance_agent_field`, `yadgar/storage/memory.py::_validate_provenance_agent`
- **wiring:** Applied once at server-mode startup after v5.3. Adds `provenance_agent string DEFAULT 'default'` to `memory`; backfills `NULL` rows to `'default'`. The CRDT post-write phase stamps this field with `CRDT_AGENT_ID` on every new memory.
- **explanation:** Records which agent (Claude Code instance, daemon, consolidation process) created each memory row. The field is constrained to ASCII alphanumeric/hyphen/underscore (≤64 chars) by `_validate_provenance_agent` to prevent SQL-injection. Default value `'default'` applies to all pre-v5.3 rows and any write that does not specify an agent.

### CAP-STOR-008 — Migration 006: source_memory_id citation provenance on KG edges
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `006`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_006_source_memory_id`
- **wiring:** Applied once at server-mode startup after v5.3.3. Adds nullable `source_memory_id` to `causal_dag_edge` and `relationship`, and `citation_source_memory_id` to `memory_similarity_link`. Written by `insert_typed_relationship` (C3) and `insert_memory_similarity_link` when a triggering memory is known.
- **explanation:** Adds citation provenance to all three KG edge tables so that downstream graph traversal and audit tools can trace which memory observation caused a given entity link, causal edge, or similarity link. All columns are nullable; existing rows receive NULL (backward-compatible). The field is named differently on `memory_similarity_link` because `source_memory_id`/`target_memory_id` are already the edge endpoint keys on that table.

### CAP-STOR-009 — Migration 007: bi-temporal validity columns on KG edge tables
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `007`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_007_bitemporal_edges`, `yadgar/storage/entity.py::_EntityMixin.insert_typed_relationship`
- **wiring:** Applied once at server-mode startup after v5.3.4. Adds `valid_from option<string>` and `valid_until option<string>` to `causal_dag_edge`, `relationship`, and `memory_similarity_link`. Backfills `valid_from = created_at` for existing rows. New inserts via `insert_relationship` and `insert_typed_relationship` set `valid_from = now()`; `valid_until = NULL` means currently valid.
- **explanation:** Implements bi-temporal edge validity (C1) across all three KG edge tables. The filter `valid_until IS NONE OR valid_until > now()` selects currently-valid edges. Storing timestamps as ISO-8601 strings (not SurrealDB `datetime`) avoids type-coerce issues between SurrealDB v2 and v3.

### CAP-STOR-010 — Migration 008: anchor tier, valid_until, migration_grace on memory
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_CONDITIONAL_TTL_DAYS`
- **tools:** `anchor`
- **migrations:** `008`
- **bc:** `BC-AN1`, `BC-AN2`
- **refs:** `yadgar/storage/migrations.py::_migration_008_anchor_tier`, `yadgar/storage/memory.py::get_anchored_memories`
- **wiring:** Applied once at server-mode startup after v5.8.0. Adds `tier`, `valid_until`, and `migration_grace` columns to `memory`. Backfills existing `_anchor`-tagged rows without `tier` to `tier='conditional'`, `valid_until=now()+ANCHOR_CONDITIONAL_TTL_DAYS`, `migration_grace=True`. The `get_anchored_memories` and `get_anchored_memories_scoped` queries filter by `valid_until IS NONE OR valid_until > now()` to exclude expired anchors.
- **explanation:** Introduces the three-tier anchor system (semantic_immortal / conditional / ephemeral). Pre-v5.8 anchors receive `migration_grace=True` so they can be identified and reviewed by `audit_anchors` rather than silently expiring. The `valid_until` field drives anchor expiry without requiring a separate cleanup table.

### CAP-STOR-011 — Migration 009: wiki_bookmark table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `009`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_009_wiki_bookmark_table`, `yadgar/storage/bookmarks.py`
- **wiring:** Applied once at server-mode startup after v5.23.0. Creates `wiki_bookmark` SCHEMALESS table with a UNIQUE slug index and a position index. Used by `BookmarksMixin` CRUD methods called from `bookmark_add`, `bookmark_remove`, `bookmark_list`, and `bookmark_reorder` MCP tools.
- **explanation:** Adds a dedicated table for pinned wiki pages. The UNIQUE index on `slug` ensures exactly one bookmark per page. The `position` field is a dense integer (0-based) managed application-side by the storage layer. Also defined redundantly in `_init_wiki_indexes` for fresh installs (idempotent `IF NOT EXISTS`).

### CAP-STOR-012 — Migration 010: bi-temporal user_profile
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `010`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_010_bitemporal_user_profile`
- **wiring:** Applied once at server-mode startup after v5.29.0 (Adopt-3). Adds `valid_from` and `valid_until` to `user_profile`; drops the old UNIQUE constraint on (entity_name, attribute_type, attribute_key, directory_context) since SurrealDB v3 does not support partial/conditional UNIQUE indexes. Uniqueness for active rows is enforced application-side in `insert_profile`.
- **explanation:** Pivots user_profile from "UPSERT in-place" to "close prior row + insert new row" semantics, enabling a temporal history of attribute values per entity. The removed DB-level UNIQUE constraint is replaced by an application-side check for existing rows with `valid_until IS NONE` before inserting.

### CAP-STOR-013 — Migration 011: bi-temporal derived_belief
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `011`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_011_bitemporal_derived_belief`
- **wiring:** Applied once at server-mode startup after v5.29.0 (Adopt-3). Adds `valid_from` and `valid_until` to `derived_belief`; backfills `valid_from = created_at`.
- **explanation:** Extends bi-temporal semantics to the `derived_belief` table. Beliefs are append-only (no UPSERT), so no UNIQUE constraint change was needed. The `valid_until IS NONE` condition identifies currently-active beliefs; closing a belief is done by setting `valid_until` on the prior row.

### CAP-STOR-014 — Migration 012: memory_block table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `012`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_012_memory_block_table`, `yadgar/storage/blocks.py`
- **wiring:** Applied once at server-mode startup after v5.33.0 (Adopt-4). Creates the `memory_block` SCHEMALESS table with indexes on `(name, scope, directory)` and `(scope, directory)`. Used by the `block_create`, `block_get`, `block_list`, `block_update`, `block_delete`, `block_append`, `block_replace` MCP tools via `BlocksMixin`.
- **explanation:** Adds named in-context memory blocks as a dedicated table separate from the main `memory` table. Isolation prevents cross-contamination with anchor audit, heat decay, and secret-gate scans. The table schema is SCHEMALESS; uniqueness on `(name, scope, directory)` is enforced application-side.

### CAP-STOR-015 — Migration 013: wiki_page_version table
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `013`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_013_wiki_page_version`
- **wiring:** Applied once at server-mode startup after v5.41.0. Creates `wiki_page_version` with three indexes (page_id, UNIQUE (page_id, version), created_at); seeds version=1 rows from existing `wiki_page` rows on first run. Subsequent `wiki_add` and `wiki_update` calls append new version rows. Idempotency guard: skips pages that already have version rows.
- **explanation:** Implements per-write immutable version history for wiki pages. Every subsequent `wiki_add` or `wiki_update` appends a new row with an incremented version number. The embedding field is intentionally excluded from version rows (storage cost; recomputed on restore). The UNIQUE `(page_id, version)` index enforces correct monotonic ordering.

### CAP-STOR-016 — Migration 014: wiki_page embedding backfill registration
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `014`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_014_wiki_page_embedding_backfill`
- **wiring:** Applied once at server-mode startup after v5.42.1. The migration itself only logs a count of NULL-embedding wiki_page rows and marks the slot as applied. The actual backfill runs via `WikiStore.backfill_null_embeddings()` called from `server/lifecycle.py` after both `StorageEngine` and `EmbeddingEngine` are ready (lifecycle startup, not the migration runner).
- **explanation:** Registers a schema version slot for the wiki embedding backfill without executing the backfill in the migration itself (which runs before the EmbeddingEngine is initialised). The split is intentional: migrations must be thin and dependency-free; the actual backfill is a one-time lifecycle startup cost.

### CAP-STOR-017 — Migration 015: branch column on wiki_draft
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `015`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_015_wiki_draft_branch`
- **wiring:** Applied once at server-mode startup after v5.42.3. Adds `branch option<string>` to `wiki_draft`. The `wiki_add` draft-creation path now stores the branch; `wiki_approve` reads and propagates it. Legacy NULL-branch drafts use the `_internal=True` carve-out (backward-compat path).
- **explanation:** Prior to this migration, `wiki_approve` always wrote to the NULL-branch canonical slot regardless of the originating branch context. Adding `branch` to `wiki_draft` allows per-branch wiki approvals and prevents feature-branch draft content from silently landing in the default-branch page.

### CAP-STOR-018 — Migration 016: directory_context on wiki_page, memory, wiki_draft
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `016`
- **bc:** `BC-DC1`, `BC-DC2`
- **refs:** `yadgar/storage/migrations.py::_migration_016_directory_context`, `yadgar/storage/migrations.py::_classify_directory_by_tags`
- **wiring:** Applied once at server-mode startup after v5.42.5. Multi-phase: (A) backfill wiki_page rows using tag-based heuristic; (B) define NOT NULL schema constraint on `wiki_page.directory_context`; (C) add index; (D) backfill memory rows (NULL/'' → 'global'); (E) define NOT NULL constraint on `memory.directory_context`; (F) add nullable `directory_context` to `wiki_draft`. The tag heuristic maps `'yadgar'→/home/max/git/yadgar`, `'nix'→/home/max/git/nix`, `'ledger'→/home/max/git/ledger`, AWS tags→`/home/max/aws-work`, else `'global'`.
- **explanation:** Enforces `directory_context` as a non-nullable field on both `memory` and `wiki_page` so every record is scoped to a project or `'global'`. This is the foundation for BC-DC1 (eligible set predicate) and BC-DC2 (no container fallback). A bug in this migration (described in migration 018) caused a partial backfill on deployed databases.

### CAP-STOR-019 — Migration 018: directory_context backfill repair
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `018`
- **bc:** `BC-DC1`, `BC-DC2`
- **refs:** `yadgar/storage/migrations.py::_migration_018_directory_context_backfill_repair`
- **wiring:** Applied once at server-mode startup after v5.42.6, after migration 016. Re-runs the tag-based backfill using a Python-side filter (rather than SurrealDB `WHERE IS NONE`) which correctly detects field-absent rows that the database query missed. Temporarily relaxes the NOT NULL constraint (DEFINE FIELD OVERWRITE → option<string>) before the UPDATE, then re-tightens it. Note: migration 017 is reserved for v5.61 wiki_source_hash.
- **explanation:** Fixes a SurrealDB v3 behaviour where `WHERE directory_context IS NONE` only matches rows with an explicit NULL value, not rows where the field is entirely absent (pre-DEFINE records). The workaround — relax constraint, backfill, re-tighten — is required because SurrealDB validates ALL defined fields on every UPDATE, causing coerce errors for field-absent rows even when the field being updated is unrelated.

### CAP-STOR-020 — Migration 019: wiki_page_type and wiki_schema_version fields
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `019`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_019_wiki_page_type`
- **wiring:** Applied once at server-mode startup after v5.53.2. Adds `page_type option<string>` and `wiki_schema_version option<int>` to `wiki_page`. Both are nullable; existing pages have `NONE` which is the correct untyped state. New typed pages written via `wiki_add(page_type=...)` set these fields.
- **explanation:** Introduces a typed wiki page system (B-schema). `page_type` is one of the registered page type keys (function, module, service, architecture, decision, analysis). `wiki_schema_version` stamps which schema generation the row was written under (1 = v5.53.2 B-schema; 0/absent = pre-5.53.2). Both fields are nullable for backward compatibility with legacy pages.

### CAP-STOR-021 — Migration 020: graph_prior field on memory
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `020`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_020_memory_graph_prior`, `yadgar/storage/memory.py::update_memory_graph_prior`, `yadgar/storage/memory.py::get_memory_graph_priors`
- **wiring:** Applied once at server-mode startup after v5.54.1. Adds `graph_prior option<float>` to `memory`. Computed by `ConsolidationScheduler._compute_graph_priors()` during each consolidation cadence and stored via `update_memory_graph_prior()`. Read back during retrieval fusion by `get_memory_graph_priors()`.
- **explanation:** Stores a precomputed entity-graph centrality scalar on each memory row to avoid graph traversal on the request path. A higher `graph_prior` means the memory is more central in the entity-relationship graph; the fusion layer uses it as an additive boost during reranking. Absent or NULL is treated as 0.0 (no boost). Staleness window is one consolidation cycle.

### CAP-STOR-022 — Migration 021: cofire_prior (co-recall transition) field on memory
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `021`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_021_memory_cofire_prior`, `yadgar/storage/memory.py::update_memory_cofire_prior`, `yadgar/storage/memory.py::get_memory_cofire_priors`
- **wiring:** Applied once at server-mode startup after v5.54.2. Adds `cofire_prior option<float>` to `memory`. Computed by `ConsolidationScheduler._compute_cofire_priors()` from `memory_transition.count` sums, normalised to [0,1]. Stored via `update_memory_cofire_prior()` and read by `get_memory_cofire_priors()` during retrieval fusion.
- **explanation:** Records a precomputed co-recall (transition-edge) prior: the sum of `memory_transition.count` for all transitions where the memory appears as `from_memory_id` or `to_memory_id`, normalised across the candidate set. "Recalled together before" signals learned associations. The fusion layer adds this as a secondary boost signal alongside `graph_prior`.

### CAP-STOR-023 — Migration 022: shadow-gate fields on memory
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** `022`
- **bc:** —
- **refs:** `yadgar/storage/migrations.py::_migration_022_shadow_gate_fields`, `yadgar/storage/client.py::_MEMORY_UPDATABLE_FIELDS`
- **wiring:** Applied once at server-mode startup after v5.73.0. Adds `surprise_score option<float>` and `would_reject option<bool>` to `memory`. Both are nullable; no backfill needed. Written by the write-gate shadow mode via `update_memory_fields`. `WRITE_GATE_THRESHOLD` stays 0.0 (nothing dropped); this is observation-only.
- **explanation:** Stores the write-gate's surprisal score and shadow rejection decision on each memory row. `surprise_score` is the gate's surprisal value (distinct from the thermodynamics `compute_surprise()` score used for heat boost). `would_reject` is `True` when the gate would reject the memory at `WRITE_GATE_SHADOW_THRESHOLD`, enabling shadow analysis without actual rejection. Both fields are in `_MEMORY_UPDATABLE_FIELDS` so they can be patched via `update_memory_fields`.

### CAP-STOR-024 — Branch scoping enforcement (BRANCH_ENFORCEMENT)
- **status:** LIVE
- **category:** storage
- **settings:** `BRANCH_ENFORCEMENT`
- **tools:** `anchor`
- **migrations:** —
- **bc:** `BC-ST1`
- **refs:** `yadgar/storage/branch.py::BranchFilter`, `yadgar/storage/branch.py::_build_branch_clause`, `yadgar/server/tools/wiki.py`
- **wiring:** `BRANCH_ENFORCEMENT=true` (default). Checked via `_enforcement_on("YADGAR_BRANCH_ENFORCEMENT")` in `wiki.py` and `file_queue/dlq.py` at write time. When enforced, writes without a resolvable branch are rejected. `BranchFilter` carries per-request branch context; `_build_branch_clause` generates the SQL predicate injected into memory and wiki queries to restrict results to NULL, default_branch, or current_branch rows.
- **explanation:** Ensures that wiki and memory writes are stamped with the correct git branch, and that reads do not leak cross-branch content. The predicate `(branch IS NONE OR branch = $default OR branch = $current)` allows canonical (NULL/default) rows to surface in any branch context while isolating feature-branch content. Disabling via `YADGAR_BRANCH_ENFORCEMENT=false` removes the rejection guard (intended only for non-git contexts or testing).

### CAP-STOR-025 — Directory scoping enforcement (DIRECTORY_ENFORCEMENT)
- **status:** LIVE
- **category:** storage
- **settings:** `DIRECTORY_ENFORCEMENT`
- **tools:** `anchor`, `memory_get`, `memory_update`, `forget`, `validate_memory`
- **migrations:** —
- **bc:** `BC-DC1`, `BC-DC2`
- **refs:** `yadgar/storage/memory.py::get_memories_for_directory`, `yadgar/server/tools/wiki.py`, `yadgar/file_queue/dlq.py`
- **wiring:** `DIRECTORY_ENFORCEMENT=true` (default). Checked via `_enforcement_on("YADGAR_DIRECTORY_ENFORCEMENT")` in `wiki.py` and `file_queue/dlq.py` at write time. Memory reads use `directory_context` predicates to scope results (BC-DC1: eligible set = {caller_dir, global, '', None}). BC-DC2: hard-require directory on reads; no `os.getcwd()` container fallback allowed.
- **explanation:** Enforces per-directory isolation so that a tool call in project A cannot read memories or wiki pages stamped for project B. The eligible set predicate (`directory_context IN (caller_dir, 'global', '', NULL)`) is the I31 invariant. `DIRECTORY_ENFORCEMENT=false` removes the rejection guard for legacy or test contexts.

### CAP-STOR-026 — Memory CRUD and field management
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** `memory_get`, `memory_update`, `forget`, `validate_memory`
- **migrations:** —
- **bc:** `BC-D1`, `BC-D2`, `BC-D3`
- **refs:** `yadgar/storage/memory.py::_MemoryMixin`, `yadgar/server/tools/admin_other.py::memory_get`, `yadgar/server/tools/admin_other.py::memory_update`, `yadgar/server/tools/admin_other.py::forget`, `yadgar/server/tools/admin_other.py::validate_memory`
- **wiring:** `memory_get(id)` calls `_st._storage.get_memory(id)` and strips embedding bytes. `memory_update(id, fields)` validates against `_MEMORY_UPDATE_ALLOWED` (content, tags, is_protected, is_stale), calls `update_memory_fields`, then re-fetches. `forget(id)` calls `storage.delete_memory(id)` which cascades to `memory_archive`, `memory_transition`, `memory_similarity_link`, and the entity/relationship rows. `validate_memory(id)` delegates to `_st._staleness.validate_memory()` (file-hash comparison) or falls back to direct file-hash check. `BC-D1/D2/D3`: these operations are part of the nightly cycle path.
- **explanation:** The four MCP-exposed memory admin tools provide controlled read/write/delete/validate access to individual memory rows. `memory_update` intentionally restricts the updatable field set at the MCP layer (4 fields) compared to the storage-layer `_MEMORY_UPDATABLE_FIELDS` allowlist (22+ fields) to prevent unintended mutation of heat, embeddings, or temporal metadata via the public API.

### CAP-STOR-027 — remember tool (deprecated stub)
- **status:** DEAD (v6 T3 — stub deleted)
- **category:** storage
- **settings:** —
- **tools:** — (remember deleted)
- **migrations:** —
- **bc:** —
- **refs:** —
- **wiring:** `remember` `@_tool()` registration and function deleted from `yadgar/server/tools/memorize.py` in v6 T3. Also removed from `__init__.py` imports and `__all__`. Clients using `remember` will now receive a tool-not-found error from FastMCP.
- **explanation:** `remember` was the original name of the `memorize` tool, renamed in v5.x. The no-op redirect stub was removed in v6 T3 dead-code cleanup (#41). Callers must update to `memorize()`.

### CAP-STOR-028 — Anchor tier system (anchor tool)
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_CONDITIONAL_TTL_DAYS`, `ANCHOR_EPHEMERAL_TTL_DAYS`, `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON`
- **tools:** `anchor`
- **migrations:** `008`
- **bc:** `BC-AN1`, `BC-AN2`
- **refs:** `yadgar/server/tools/misc.py::anchor`, `yadgar/server/tools/memorize.py::_compute_valid_until`
- **wiring:** `anchor()` MCP tool → `_validate_anchor_inputs()` (validates tier + reason requirement) → `_resolve_anchor_branch()` (daemon-side git detection + branch_hint fallback + YADGAR_CI_BRANCH) → enqueues to file queue (async default) or synchronously calls `replay.anchor_memory()` (drain replay path). The file queue drainer calls the sync path. `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true` (default): semantic_immortal tier requires a non-empty `reason`. `ANCHOR_CONDITIONAL_TTL_DAYS=90`: default expiry for conditional anchors. `ANCHOR_EPHEMERAL_TTL_DAYS=14`: default expiry for ephemeral anchors.
- **explanation:** The anchor system creates compaction-resistant memories (is_protected=True, max heat, max importance, tagged `_anchor`). Three tiers control expiry: `semantic_immortal` (no expiry, requires reason), `conditional` (expires in ANCHOR_CONDITIONAL_TTL_DAYS), and `ephemeral` (expires in ANCHOR_EPHEMERAL_TTL_DAYS). Valid_until can also be set explicitly via `valid_until` or `ttl_days` parameters.

### CAP-STOR-029 — Anchor audit (audit_anchors tool)
- **status:** LIVE
- **category:** storage
- **settings:** `ANCHOR_REDUNDANCY_COSINE`, `ANCHOR_CROSS_PROJECT_COSINE`, `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN`, `ANCHOR_AUDIT_THRESHOLD`, `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`, `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS`
- **tools:** `audit_anchors`
- **migrations:** —
- **bc:** `BC-AN3`
- **refs:** `yadgar/server/tools/audit.py::audit_anchors`, `yadgar/server/tools/audit.py::_run_anchor_audit_pass`
- **wiring:** `audit_anchors()` MCP tool: resolves project root, fetches cfg, calls `_build_expire_actions`, `_build_verify_grace_actions`, `_build_promote_actions`, `_build_merge_actions` per directory, caps at `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN`. When `dry_run=False` applies mutations via `_apply_mutations`. Also computes cross-project redundancy via `_fetch_cross_project_candidates` (uses `ANCHOR_CROSS_PROJECT_COSINE=0.95`). `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`: auto-run dry-pass during `consolidate_now(mode='full')` for dirs with anchor count ≥ `ANCHOR_AUDIT_THRESHOLD` (default 15). `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS=30` is defined but has no active consumer beyond the config definition (CONFIG-ONLY for that sub-knob).
- **explanation:** Scans all anchors for a directory for four conditions: expiry (valid_until < now), migration-grace expiry (migration_grace=True + expired), size-based promotion candidates (prose-only archives), and cosine-based redundancy (pairwise cosine ≥ ANCHOR_REDUNDANCY_COSINE). The tool never auto-applies semantic_immortal or is_protected=True rows. Cross-project candidates are always surfaced but never mutated. The nightly auto-pass (via `_run_anchor_audit_pass`) writes a sentinel memory tagged `_audit_anchors` after each pass.

### CAP-STOR-030 — CRDT vector clock and agent identity
- **status:** LIVE
- **category:** storage
- **settings:** `CRDT_AGENT_ID`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/_memorize_phases/_phase_post_write.py`, `yadgar/storage/memory.py::_build_memory_insert_clause`
- **wiring:** `CRDT_AGENT_ID` (default `'default'`). After a new memory is inserted, `_phase_post_write.py` reads `settings.CRDT_AGENT_ID`, increments the memory's vector clock JSON (`{agent_id: seq}`), and updates `provenance_agent` and `vector_clock` fields on the memory row via `_q`. The vector clock is stored as a JSON string in the `vector_clock` field.
- **explanation:** Provides a lightweight CRDT (Conflict-free Replicated Data Type) stamping mechanism for multi-agent environments. Each agent has a unique ID; the vector clock tracks per-agent write counts to enable causal ordering and conflict detection if memories are replicated across agents. In single-agent deployments the clock always has one entry. The field is initialised to `'{}'` on insert and incremented in the post-write phase.

### CAP-STOR-031 — Decision auto-protect
- **status:** LIVE
- **category:** storage
- **settings:** `DECISION_AUTO_PROTECT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/_memorize_phases/_phase_post_write.py`
- **wiring:** `DECISION_AUTO_PROTECT=true` (default). Checked in `_phase_post_write.py` after memory insert: if the content matches `_DECISION_STRONG_RE` (regex for decision-language patterns), the memory is automatically protected (`is_protected=True`). Applied on the `memorize` tool write path.
- **explanation:** Automatically protects memories that contain strong decision language (e.g., "we decided", "the decision is") from heat decay and archival. This is a heuristic layer on top of explicit `is_protected` flag. Disabling via `DECISION_AUTO_PROTECT=false` removes the heuristic protection, requiring all protection to be explicit.

### CAP-STOR-032 — File hash content validation (MAX_HASH_BYTES)
- **status:** LIVE
- **category:** storage
- **settings:** `MAX_HASH_BYTES`
- **tools:** `validate_memory`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/_helpers.py::_file_hash`
- **wiring:** `_file_hash(path)` is called by `validate_memory` and the memorize write path to compute a SHA-256 hash of a file for staleness tracking. `MAX_HASH_BYTES=10485760` (10 MiB): files larger than this are skipped (returns None). Called from `validate_memory` MCP tool and `_memorize_phases/_phase_validate.py`.
- **explanation:** Controls the maximum file size that yadgar will hash for staleness detection. Files exceeding `MAX_HASH_BYTES` are skipped to prevent memory/time overhead on large binaries or log files. `validate_memory` compares the stored `file_hash` against the current file hash to determine if a file-backed memory has gone stale.

### CAP-STOR-033 — Memory similarity links (SIMILARITY_LINK_THRESHOLD, MAX_SIMILARITY_LINKS_PER_MEMORY)
- **status:** LIVE
- **category:** storage
- **settings:** `SIMILARITY_LINK_THRESHOLD`, `MAX_SIMILARITY_LINKS_PER_MEMORY`
- **tools:** —
- **migrations:** `003`, `007`
- **bc:** —
- **refs:** `yadgar/consolidation/cls.py`, `yadgar/storage/cluster.py::_ClusterMixin.insert_memory_similarity_link`
- **wiring:** `SIMILARITY_LINK_THRESHOLD=0.78` (default minimum cosine to create a link). `MAX_SIMILARITY_LINKS_PER_MEMORY=15` (default degree cap). Both consumed by `consolidation/cls.py` during the CLS (episodic→semantic) phase: for each memory pair with cosine ≥ threshold, if the degree cap is not exceeded, a `memory_similarity_link` row is created via `insert_memory_similarity_link`. Also checked by `admin_invariants.py` for the invariant ceiling.
- **explanation:** Bounds the `memory_similarity_link` graph to prevent unbounded growth. `SIMILARITY_LINK_THRESHOLD` sets the cosine floor for edge creation; `MAX_SIMILARITY_LINKS_PER_MEMORY` caps the per-node degree so the graph remains sparse and fast to traverse. The CLS phase runs during the nightly consolidation cycle and during `consolidate_now(mode='full')`.

### CAP-STOR-034 — Memory cluster and prospective memory retention
- **status:** LIVE
- **category:** storage
- **settings:** `MEMORY_CLUSTER_RETENTION_DAYS`, `PROSPECTIVE_MEMORY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/consolidation/cleanup.py`, `yadgar/storage/cluster.py::_ClusterMixin`
- **wiring:** Both settings consumed by `consolidation/cleanup.py` in the nightly cleanup phase: `memory_cluster` rows older than `MEMORY_CLUSTER_RETENTION_DAYS` (default 30) are pruned; `prospective_memory` rows older than `PROSPECTIVE_MEMORY_RETENTION_DAYS` (default 30) are pruned. Pruning runs during `consolidate_now()` and the nightly daemon cycle.
- **explanation:** Controls the retention window for two ephemeral table types. Memory clusters are recreated each consolidation cycle; stale clusters from prior runs are pruned after the retention window. Prospective memories (future reminders) are automatically pruned after their retention window even if they have not been activated. Both defaults are 30 days.

### CAP-STOR-035 — Secret gate at storage layer (BC-S1, BC-S2, BC-S3)
- **status:** LIVE
- **category:** security
- **settings:** —
- **tools:** `memorize`, `anchor`
- **migrations:** —
- **bc:** `BC-S1`, `BC-S2`, `BC-S3`
- **refs:** `yadgar/storage/memory.py::_validate_memory_secrets`, `yadgar/storage/memory.py::_MemoryMixin.insert_memory`
- **wiring:** `_validate_memory_secrets()` is called by `insert_memory()` before every memory write. It calls `check_secrets()` on content, tags, and reason fields. If a secret pattern is detected it raises `SecretLeakBlocked` and increments the `rejected_secret_at_storage` metric. The env var `YADGAR_SECRET_GATE_DISABLED=1` bypasses the check with a warning (emergency kill switch only). The API-boundary gate (`gate_or_reject()`) is Layer 2; the storage gate is Layer 1 (last line of defence).
- **explanation:** Two-layer secret gate prevents credential and API-key leakage into the memory store. The storage-layer gate (this entry) fires even if the API boundary was bypassed. BC-S1: blocked at both layers. BC-S2: allowlist bypass (implemented at the API layer via `gate_or_reject()`). BC-S3: every allowlist bypass is audited via invariant I28.

### CAP-STOR-036 — Checkpoint and restore (BC-CK1)
- **status:** LIVE
- **category:** storage
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CK1`
- **refs:** `yadgar/storage/ops.py`, `yadgar/server/tools/misc.py`
- **wiring:** `checkpoint()` MCP tool stores task context (decisions, next-steps, open questions) as a structured `checkpoint` table row scoped by `directory_context`. `restore()` reads the latest checkpoint for the directory and returns it as part of the context bundle (alongside anchors and hot memories). Both paths go through `_OpsMixin` methods on `StorageEngine`.
- **explanation:** Provides session continuity across `/clear` or `/compact` operations. A checkpoint captures the current task state as a serialised record; `restore()` reconstructs it and injects it into the next session's context. BC-CK1 requires that `checkpoint(dir, ...)` followed by `restore(dir)` returns the task, decisions, and next-steps.

### CAP-STOR-037 — Nightly cycle storage operations (BC-D1, BC-D2, BC-D3)
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-D1`, `BC-D2`, `BC-D3`
- **refs:** `yadgar/storage/ops.py`, `yadgar/storage/memory.py::get_all_memories_for_decay`
- **wiring:** The nightly cron script calls the daemon's consolidation cycle which reads memories via `get_all_memories_for_decay()`, applies heat decay, archives cold memories, runs CLS and causal phases, and writes results back. `BC-D1`: nightly exits 0 against a seeded DB (e2e proven). `BC-D2`: pre-backup snapshot uses real `YADGAR_DATA_DIR`/XDG path. `BC-D3`: interpreter shutdown is clean (no SEGV).
- **explanation:** The nightly cycle is the primary path through which storage-layer read/write/decay operations exercise the full stack. Most storage primitives (heat update, archival, cluster insert, similarity link, consolidation log) run exclusively in this path, not on the real-time request path. BC-D1 is e2e green; BC-D2 and BC-D3 are also e2e green.

### CAP-WRITE-001 — `memorize` MCP tool (write-path entry point)
- **status:** LIVE
- **category:** write-path
- **settings:** —
- **tools:** `memorize`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/memorize.py::memorize`, `yadgar/server/tools/_memorize_phases/context.py`
- **wiring:** MCP client calls `memorize()` → registered via `@_tool()` decorator in `memorize.py` → constructs `MemorizeContext` → sequentially calls `phase_validate`, `phase_resolve_branch`, `phase_embed`, `phase_contradiction`, `phase_store`, `phase_post_write`. Each phase returns a rejection dict (short-circuit) or `None` (continue). Final response is the stored memory dict.
- **explanation:** `memorize` is the primary write-path MCP tool. It accepts `content`, `context` (must be an absolute directory path), `tags`, optional protection/tier/TTL fields, and a `branch_hint`. It orchestrates six phases: input validation + secret-gate, branch resolution, write-gate + embedding + thermo scoring, contradiction detection, storage (via curator or direct insert), and post-write hooks (synaptic boost, reinjection, shadow-gate stamp, CRDT clock, viz event). The `remember` no-op redirect stub was deleted in v6 T3 (see CAP-STOR-027).

---

### CAP-WRITE-002 — `memory_stats` MCP tool
- **status:** LIVE
- **category:** observability
- **settings:** —
- **tools:** `memory_stats`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_other.py::memory_stats`
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
- **refs:** `yadgar/server/tools/_memorize_phases/_phase_validate.py::phase_validate`, `yadgar/secrets.py::gate_or_reject`, `yadgar/rules_engine.py`
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
- **refs:** `yadgar/secrets.py::gate_or_reject`, `yadgar/secrets.py::check_secrets`, `yadgar/security/allowlist.py`
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
- **refs:** `yadgar/server/tools/_memorize_phases/_phase_embed.py::phase_embed`, `yadgar/retrieval/core.py::generate_contextual_prefix`
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
- **refs:** `yadgar/sensory_buffer.py::ActionLogger`, `yadgar/server/tools/_memorize_phases/_phase_store.py::phase_store`
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
- **refs:** `yadgar/server/tools/_memorize_phases/_phase_post_write.py::_zero_gap_6_reinjection`
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
- **refs:** `yadgar/retrieval/fusion.py::_search_profiles_and_beliefs`, `yadgar/config.py`
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
- **refs:** `yadgar/predictive_coding.py::WriteGate.should_store`, `yadgar/predictive_coding.py::WriteGate.would_reject_at`, `yadgar/server/tools/_memorize_phases/_phase_embed.py::phase_embed`, `yadgar/server/tools/_memorize_phases/_phase_store.py::phase_store`, `yadgar/server/tools/_memorize_phases/context.py`
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
- **refs:** `yadgar/predictive_coding.py::WriteGate._get_cached_entities`, `yadgar/predictive_coding.py::WriteGate.invalidate_entity_cache`
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
- **refs:** `yadgar/enrichment/__init__.py::EnrichmentPipeline.enrich`, `yadgar/storage/memory.py::_enrich_memory_if_enabled`
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
- **refs:** `yadgar/enrichment/fpa.py::FPAFilter.filter`, `yadgar/enrichment/__init__.py::EnrichmentPipeline._apply_fpa`
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
- **refs:** `yadgar/enrichment/conceptnet.py::ConceptNetExpander.expand`, `yadgar/enrichment/__init__.py::EnrichmentPipeline.enrich`
- **wiring:** `EnrichmentPipeline.enrich` checks `settings.CONCEPTNET_ENRICHMENT_ENABLED` (default `True`) → calls `ConceptNetExpander().expand(content, settings)` → extracts content-bearing tokens, tries three sources in order: (1) `conceptnet_lite` local SQLite DB (~9 GB, not bundled), (2) HTTP API (`https://api.conceptnet.io`, disabled by default as `http_enabled=False`), (3) hardcoded expansions dict. Results pass through `_apply_fpa` before storage.
- **explanation:** ConceptNet expansion extracts nouns/verbs from the memory content, queries ConceptNet for related concepts (e.g. IsA, RelatedTo, UsedFor, PartOf, HasA — configurable via `CONCEPTNET_RELATIONS`), and appends survivors of the FPA filter to the enriched content. The local `conceptnet_lite` SQLite path fires automatically but is gated on the `conceptnet_lite` package and the ~9 GB DB being installed. The HTTP API path (`api.conceptnet.io`) is network-gated and disabled by default in the `ConceptNetExpander` constructor (`http_enabled=False`). The hardcoded expansion dict provides a fallback for ~20 hobby/activity terms. Status is DORMANT: the flag is enabled by default but both primary data sources (lite DB, HTTP) are unavailable in the standard install, so only the hardcoded fallback fires — functional enrichment requires explicit deployment of the lite DB. (`BC-EN1a` is ⏳ network-gated in CI; `BC-EN1b` per #39.)

---

### CAP-ENR-004 — COMET commonsense inference
- **status:** LIVE
- **category:** enrichment
- **settings:** `COMET_ENRICHMENT_ENABLED`, `COMET_MODEL`, `COMET_NUM_BEAMS`, `COMET_TOP_K_PER_RELATION`, `COMET_MIN_CONFIDENCE`, `COMET_RELATIONS`, `COMET_QUERY_EXPANSION_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** `BC-EN2a`, `BC-EN2b`
- **refs:** `yadgar/enrichment/comet.py::CometInferencer.infer`, `yadgar/enrichment/__init__.py::EnrichmentPipeline.enrich`
- **wiring:** `EnrichmentPipeline.enrich` checks `settings.COMET_ENRICHMENT_ENABLED` (default `True`) → `CometInferencer().infer(content, settings)` → `_ensure_model(COMET_MODEL)` lazy-loads `mismayil/comet-bart-ai2` via `_load_seq2seq_model` → extracts subject-verb predicates → for each predicate × relation (default `xAttr,xIntent,xWant`), generates sequences with beam search (`COMET_NUM_BEAMS=5`), scores via softmax, keeps sequences scoring ≥ `COMET_MIN_CONFIDENCE=0.3` up to `COMET_TOP_K_PER_RELATION=3` per relation → results passed through FPA filter. `COMET_QUERY_EXPANSION_ENABLED` (default `False`) controls a separate query-time expansion path not part of the write pipeline.
- **explanation:** COMET-BART infers commonsense consequences for memories using the ATOMIC relations (e.g. xAttr: what the subject is like, xIntent: what the subject intends, xWant: what the subject wants). Despite the model loading and generating valid inferences, the FPA cosine filter (threshold 0.25) consistently rejects these abstract commonsense triples as semantically distant from the concrete memory content — the result is that `enrichment_comet` is empty in practice (`BC-EN2a` status `❌`). The write path is fully wired and functional; the behaviour gap is in FPA tuning, flagged for resolution in the v6 enrichment plan.

---

### CAP-ENR-005 — Doc2Query synthetic query generation
- **status:** LIVE
- **category:** enrichment
- **settings:** `DOC2QUERY_ENRICHMENT_ENABLED`, `DOC2QUERY_MODEL`, `DOC2QUERY_NUM_QUERIES`
- **tools:** —
- **migrations:** —
- **bc:** `BC-EN3a`, `BC-EN3b`
- **refs:** `yadgar/enrichment/doc2query.py::Doc2QueryExpander.expand`, `yadgar/enrichment/__init__.py::EnrichmentPipeline.enrich`
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
- **refs:** `yadgar/enrichment/logic.py::LogicExpander.expand`, `yadgar/enrichment/__init__.py::EnrichmentPipeline.enrich`
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
- **refs:** `yadgar/predictive_coding.py::WriteGate`, `yadgar/server/tools/_memorize_phases/_phase_embed.py`
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
- **refs:** `yadgar/server/tools/_memorize_phases/_phase_post_write.py::_run_engram`
- **wiring:** `phase_post_write` → `_run_engram(ctx)` → if `_st._engram is not None`, calls `_st._engram.allocate(ctx.memory_id)` → returns `{"slot_index": ..., "temporally_linked": ..., "link_count": ...}` which is included in the response as `engram_slot`, `temporal_links`, `temporal_link_count`.
- **explanation:** After a memory is stored, it is allocated to a competitive engram slot — a biologically-inspired fixed-capacity memory store where each slot has excitability, plasticity, and stability fields (BC-EG1). Slot allocation involves competitive selection among low-excitability slots and temporal linking to recently allocated memories (BC-EG2 — Hopfield-style pattern recall via the engram graph). The engram subsystem is optional (`_st._engram` may be None); failures are silently swallowed with a debug log.

---

### CAP-ENR-009 — Wiki gate / project-context behaviours (BC-G1 – BC-G10)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-G1`, `BC-G2`, `BC-G3`, `BC-G4`, `BC-G5`, `BC-G6`, `BC-G7`, `BC-G8`, `BC-G9`, `BC-G10`
- **refs:** `yadgar/server/tools/wiki.py`
- **wiring:** Wiki tools (`wiki_add`, `wiki_query`, `wiki_read`, etc.) implement the behaviours listed in BC-G1–BC-G10. These are all in ⏳ (unverified by automated test) status in the behaviour contract and are surfaced here for coverage completeness. The write gate cluster owns this coverage entry because the assignment JSON includes all BC-G rows.
- **explanation:** BC-G1–BC-G10 specify wiki subsystem invariants: directory stamping on `wiki_add`, cross-project isolation on `wiki_query`, §25 slug resolution order (`dir+branch → dir+null → global → not-found`), immutable version creation on every write, the draft/approve workflow, similarity-based near-duplicate blocking, bookmark CRUD, branch-page cleanup, positional edit primitives, and multi-row `wiki_set_metadata` reach. All are ⏳ (unverified by automated test) or ⏳[u] in the contract as of v5.49.x.

---

### CAP-ENR-010 — Vacuum/backup safety (BC-E1, BC-E2, BC-E3)
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-E1`, `BC-E2`, `BC-E3`
- **refs:** `yadgar/server/tools/admin_vacuum.py`
- **wiring:** The vacuum tool (`vacuum_now`, `vacuum_checkpoints`) implements the three safety contracts. `BC-E1` (row counts unchanged) is verified by comparing pre/post snapshot counts. `BC-E2` (atomic swap — mid-failure leaves DB intact) is enforced by a copy-then-swap pattern with verification before the rename. `BC-E3` (sensitive-job lock blocks restart) is enforced by checking the job-lock state before allowing external shutdown.
- **explanation:** BC-E1/E2/E3 were flipped to ✅ in v5.69 with dedicated e2e tests in `tests/e2e/test_vacuum_backup_safety.py`. BC-E1 proves row counts are preserved across vacuum. BC-E2 proves the atomic swap is safe under failure injection (import failure, verification failure, crash mid-swap, recovery ordering). BC-E3 proves an external restart/shutdown request is refused while a sensitive vacuum job holds the lock.

### CAP-CONS-001 — Heat Decay (thermodynamic memory decay)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `DECAY_FACTOR`, `COLD_THRESHOLD`, `HOT_THRESHOLD`, `IMPORTANCE_DECAY_FACTOR`, `EMOTIONAL_DECAY_RESISTANCE`
- **tools:** —
- **migrations:** —
- **bc:** `BC-HT1`, `BC-HT2`, `BC-HT3`, `BC-C2`, `BC-CSW1`
- **refs:** `yadgar/consolidation/heat_decay.py::_HeatDecayMixin._apply_decay`, `yadgar/consolidation/heat_decay.py::_HeatDecayMixin._decay_memories`, `yadgar/consolidation/heat_decay.py::_HeatDecayMixin._decay_entities`, `yadgar/storage/heat_writer.py::HeatWriter`, `yadgar/thermodynamics.py`
- **wiring:** `ConsolidationScheduler._consolidation_cycle()` → `_run_episodic_phases()` → `_apply_decay()`. Runs every consolidation cycle (force_consolidate MCP or nightly cron). `_decay_memories` iterates all non-protected memories: computes elapsed hours from `max(last_accessed, last_decay_at)` (watermark fix prevents quadratic over-decay), applies domain multiplier from AstrocytePool if enabled, then calls `MemoryThermodynamics.compute_decay()` which uses IMPORTANCE_DECAY_FACTOR and EMOTIONAL_DECAY_RESISTANCE. Heat below COLD_THRESHOLD is zeroed. HOT_THRESHOLD defaults to 0.0 (all memories accessible). Entity decay uses DECAY_FACTOR directly. T4 (BC-CSW1): intents from both tables are merged by `_reconcile_heat_intents` and applied via a single `HeatWriter.apply_heat_intents()` call — one `batch_writes` per cycle.
- **explanation:** Implements exponential heat decay on every memory and entity after each consolidation cycle. Heat represents recency × importance; it decays as `DECAY_FACTOR^hours`, modulated by emotional valence (high |valence| slows decay via EMOTIONAL_DECAY_RESISTANCE) and importance. Memories crossing COLD_THRESHOLD are archived (heat→0) and excluded from normal recall. The watermark (`last_decay_at`) prevents compounding decay across cycles — only the elapsed time since the last decay pass is charged, not since last access.

### CAP-CONS-002 — Surprise & Synaptic Boosting
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `SURPRISE_BOOST`, `SYNAPTIC_BOOST`, `SYNAPTIC_WINDOW_MINUTES`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/thermodynamics.py`, `yadgar/consolidation/cls.py::_CLSMixin._process_new_episodes`
- **wiring:** SURPRISE_BOOST is applied in `MemoryThermodynamics.compute_heat_with_surprise()` at write time when a memory has a non-zero surprise_score. SYNAPTIC_BOOST fires in `_process_new_episodes()`: if a source episode's linked memory has importance > 0.7, `self._thermo.synaptic_boost()` is called which finds all memories created within `SYNAPTIC_WINDOW_MINUTES` of the event and boosts their heat by `SYNAPTIC_BOOST * event_heat`.
- **explanation:** Two related heat-amplification mechanisms. Surprise boost elevates the initial heat of high-surprise memories (discovery events, unexpected outcomes) by adding `surprise_score * SURPRISE_BOOST` to base heat at write time. Synaptic boost (Hebbian-inspired) amplifies temporally adjacent memories when a high-importance event is processed during episode consolidation: memories written within SYNAPTIC_WINDOW_MINUTES of the trigger event receive a heat increment proportional to SYNAPTIC_BOOST, implementing a basic coincidence-based strengthening rule.

### CAP-CONS-003 — CLS Dual-Store Consolidation (Go-CLS)
- **status:** LIVE
- **category:** consolidation
- **settings:** `CLS_PATTERN_MAX_CANDIDATES`, `CLUSTER_SIMILARITY_THRESHOLD`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CLS1`, `BC-CLS2`, `BC-CLS3`
- **refs:** `yadgar/cls_store/__init__.py::DualStoreCLS.consolidation_cycle`, `yadgar/cls_store/clustering.py::_ClusteringMixin.find_recurring_patterns`, `yadgar/cls_store/promotion.py::_PromotionMixin._promote_pattern`, `yadgar/cls_store/patterns.py::_PatternsMixin`
- **wiring:** `ConsolidationScheduler._consolidation_cycle()` → `_run_curation_phases()` → `self._cls.consolidation_cycle()`. Runs every cycle. `DualStoreCLS` is initialized in `ConsolidationScheduler.__init__()`. Pattern detection is capped at `CLS_PATTERN_MAX_CANDIDATES` most-recent episodic memories. Clusters with cosine similarity ≥ `CLUSTER_SIMILARITY_THRESHOLD` and meeting session/directory diversity requirements are promoted to semantic memories.
- **explanation:** Implements the Go-CLS model (McClelland et al. 1995; Sun et al. 2023): episodic (hippocampal-fast) memories that recur across multiple sessions are abstracted into semantic (neocortical-slow) memories. `find_recurring_patterns()` builds a numpy pairwise cosine similarity matrix over recent episodic memories, performs greedy clustering at `CLUSTER_SIMILARITY_THRESHOLD`, and filters for clusters with ≥ 3 occurrences across ≥ 2 sessions. Qualifying clusters pass consistency checking (negation-pattern contradiction detection), then `abstract_to_schema()` generates a "Recurring pattern…" summary. The schema is promoted to a new semantic memory, episodic sources are linked via `derived_from` edges, and no episodic memories are deleted.

### CAP-CONS-004 — AstrocytePool (domain-aware consolidation)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `ASTROCYTE_POOL_ENABLED`, `ASTROCYTE_PROCESS_RETENTION_DAYS`, `NUM_ASTROCYTE_PROCESSES`
- **tools:** —
- **migrations:** —
- **bc:** `BC-C5a`, `BC-C5b`, `BC-AS1`, `BC-AS2`
- **refs:** `yadgar/astrocyte_pool.py::AstrocytePool`, `yadgar/consolidation/__init__.py::ConsolidationScheduler._run_domain_consolidation`, `yadgar/consolidation/heat_decay.py::_HeatDecayMixin._build_domain_multiplier_map`
- **wiring:** Initialized in `ConsolidationScheduler.__init__()` if `ASTROCYTE_POOL_ENABLED=True` (default). During `_consolidation_cycle()`, after all memory-producing phases, the orchestrator checks `_pool is not None and ASTROCYTE_POOL_ENABLED`, then calls `_run_domain_consolidation()` which iterates `pool.get_process_stats()` → `pool.consolidate_domain(name)` for each of the four domain processes (code-patterns, decisions, errors, dependencies). The domain-multiplier map used by heat decay (`_build_domain_multiplier_map()`) also reads the pool to apply per-domain decay rates. Prior audit #40 noted domain consolidation "never fires" — this was the old daemon path; the code is now confirmed wired at line 232-244 in `orchestrator.py`. Status: LIVE.
- **explanation:** Modeled on astrocyte glial cells which support domain-specific neuronal populations. Four specialized processes each track a domain (code-patterns, decisions, errors, dependencies) with distinct `decay_multiplier` values (e.g. decisions decay 1.5× slower, errors 0.7× faster). Each domain consolidation pass re-scans assigned memories, extracts domain-typed entities (file/function, decision, error/solution, dependency), and reinforces or creates entity graph nodes. The pool also supports consensus retrieval (multi-domain voting with 15% multi-domain boost) used during recall. Process records are pruned after `ASTROCYTE_PROCESS_RETENTION_DAYS` days.

### CAP-CONS-005 — Cold-Memory Retention (dry-run / purge gate)
- **status:** DORMANT
- **category:** consolidation
- **settings:** `COLD_MEMORY_PURGE_ENABLED`, `COLD_MEMORY_PURGE_DRY_RUN`, `COLD_MEMORY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/consolidation/cold_retention.py::_cold_memory_retention_report`, `yadgar/consolidation/cleanup.py::_CleanupMixin._run_retention_tasks`
- **wiring:** Called from `_run_retention_tasks()` in every consolidation cycle when `COLD_MEMORY_RETENTION_DAYS > 0`. Identifies candidate memories (heat < COLD_THRESHOLD, age > COLD_MEMORY_RETENTION_DAYS, access_count = 0, not protected, no `_anchor` tag). By default (`COLD_MEMORY_PURGE_ENABLED=False`, `COLD_MEMORY_PURGE_DRY_RUN=True`), the function only reports candidates and emits a Prometheus gauge — it deletes nothing. Real deletion requires both gates explicitly set. Status is DORMANT because the deletion path requires non-default config; the report path runs every cycle (LIVE for visibility, DORMANT for the purge itself).
- **explanation:** Addresses the #44 data-loss risk: cold immortal user memories that have no access history and exceed the retention age. The two-gate design (`PURGE_ENABLED=False` AND `DRY_RUN=True`) requires both to be overridden before any memory is deleted, preventing accidental data loss. The report path always fires and emits a `yadgar_cold_purge_candidates` Prometheus gauge so operators can observe the population before enabling deletion. Conservative candidate criteria exclude protected memories and anchors.

### CAP-CONS-006 — Episode Processing & Entity Extraction
- **status:** LIVE
- **category:** consolidation
- **settings:** `EPISODE_RETENTION_DAYS`, `MAX_EPISODE_TOKENS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CA1`
- **refs:** `yadgar/consolidation/cls.py::_CLSMixin._process_new_episodes`, `yadgar/consolidation/cleanup.py::_CleanupMixin._prune_old_episodes_safe`, `yadgar/sensory_buffer.py`
- **wiring:** `_run_episodic_phases()` → `_process_new_episodes()`. Fetches all episodes with ID > `_last_consolidated_episode_id`. For each episode: typed entity extraction (`_graph.extract_entities_typed()`) + legacy regex extraction → `_upsert_entities()` → bulk co-occurrence relationship writes. Episodes older than `EPISODE_RETENTION_DAYS` are pruned by `_prune_old_episodes_safe()` after each pass. `MAX_EPISODE_TOKENS` controls episode chunking in `SensoryBuffer` at capture time (1 token ≈ 4 chars).
- **explanation:** The episodic processing phase ingests raw episodes captured by the PostToolCall hook and promotes them into the entity knowledge graph. Each episode is scanned for file paths, Python/JS definitions, error types, imports, and decision keywords to extract typed entities. Co-occurrence relationships are batch-written for all entity pairs found in the same episode. This builds the substrate for both the causal discovery (PC algorithm) and the graph-prior computation. Episodes are pruned after `EPISODE_RETENTION_DAYS` to keep the table bounded.

### CAP-CONS-007 — Dream Replay (sleep cycle phase 1)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `DREAM_REPLAY_PAIRS`, `DREAM_INSIGHT_MAX_AGE_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`
- **refs:** `yadgar/sleep_compute/dream.py::_DreamMixin.dream_replay`, `yadgar/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`, `yadgar/consolidation/__init__.py::ConsolidationScheduler.run_nightly_consolidation`
- **wiring:** `run_nightly_consolidation()` → `_maybe_sleep_cycle()` → `SleepComputeEngine.run_sleep_cycle()` → `dream_replay()`. This runs at most once every 6 hours (6-hour guard in `_maybe_sleep_cycle`). Also reachable via `consolidate_now(mode='full')` MCP tool. The nightly cron path was dead from v5.7.0 until v5.72 (#61, PR-1) re-wired `_maybe_sleep_cycle()` into `run_nightly_consolidation()`. DREAM_INSIGHT_MAX_AGE_DAYS is used in curation prune passes (`yadgar/curation/prune_passes.py:139`) to purge stale dream insight memories.
- **explanation:** Implements offline memory replay inspired by hippocampal sharp-wave ripples during sleep. Selects up to `DREAM_REPLAY_PAIRS` random pairs of memories with embeddings that are not yet connected in the entity graph. For each pair with cosine similarity > 0.4, a weak co-occurrence link (weight=0.5) is created. Pairs with similarity > 0.7 additionally generate a synthetic "Dream connection" memory with surprise_score=0.8 and heat=0.5. These insights are pruned by the curation pass when older than `DREAM_INSIGHT_MAX_AGE_DAYS` days.

### CAP-CONS-008 — Community Detection & Cluster Summarization (sleep cycle phases 2-3)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`
- **refs:** `yadgar/sleep_compute/community.py::_CommunityMixin.detect_communities`, `yadgar/sleep_compute/community.py::_CommunityMixin.generate_cluster_summaries`, `yadgar/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`
- **wiring:** `run_sleep_cycle()` → `detect_communities()` then `generate_cluster_summaries()`. Both run in the nightly sleep cycle gated by the 6-hour guard. `detect_communities()` builds a networkx Graph from all active entity relationships and runs Louvain community detection (fallback: label propagation). `generate_cluster_summaries()` generates text summaries and centroid embeddings for clusters with > 3 members, then groups level-1 clusters into level-2 root clusters by directory context. `FRACTAL_LEVELS` was deleted in v6 T3 — only 2 levels (community + root) are built; deeper clustering remains future work.
- **explanation:** Identifies coherent memory clusters using graph-community detection on the entity co-occurrence graph. Communities (groups of entities that co-occur frequently) are stored as `memory_cluster` records, and memories mentioning those entities are assigned to clusters. Level-2 (root) clusters group level-1 communities by dominant directory context, implementing a two-level hierarchical structure. `FRACTAL_LEVELS` was CONFIG-ONLY (deleted v6 T3) — only 2 levels are built regardless.

### CAP-CONS-009 — Re-embedding & Memory Compression (sleep cycle phases 4-5)
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`
- **refs:** `yadgar/sleep_compute/embed_compress.py::_EmbedCompressMixin.reembed_stale`, `yadgar/sleep_compute/embed_compress.py::_EmbedCompressMixin.compress_old_memories`, `yadgar/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`
- **wiring:** `run_sleep_cycle()` → `reembed_stale()` → `compress_old_memories()`. Both run nightly in the sleep cycle. `reembed_stale()` fetches memories whose `embedding_model` differs from the current model and re-encodes them in batches of 50. `compress_old_memories()` uses a `days_threshold=30` hard-coded value. `COMPRESSION_GIST_AGE_HOURS` and `COMPRESSION_TAG_AGE_HOURS` were CONFIG-ONLY and deleted in v6 T3.
- **explanation:** Two maintenance passes run during the nightly sleep cycle. Re-embedding updates embeddings when the active model changes. Memory compression extracts key sentences from verbose old memories (> 1000 chars, older than 30 days) using entity-pattern regex. `COMPRESSION_GIST_AGE_HOURS` and `COMPRESSION_TAG_AGE_HOURS` were never read by `compress_old_memories()` — deleted in v6 T3 (#41).

### CAP-CONS-010 — Narrative Auto-Generation (sleep cycle phase 6)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `NARRATIVE_INTERVAL_HOURS`, `NARRATIVE_ENTRY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-C4`
- **refs:** `yadgar/sleep_compute/__init__.py::SleepComputeEngine.run_sleep_cycle`, `yadgar/narrative.py::NarrativeEngine.auto_narrate`
- **wiring:** `run_sleep_cycle()` → `self._narrative.auto_narrate()`. `NarrativeEngine` is instantiated in `SleepComputeEngine.__init__()`. `auto_narrate()` checks if a narrative entry exists in the last `NARRATIVE_INTERVAL_HOURS` hours; if not, it generates one. Narrative entries older than `NARRATIVE_ENTRY_RETENTION_DAYS` are pruned by `_run_retention_tasks()` in the main consolidation cycle.
- **explanation:** Generates periodic narrative summaries of recent memory activity to provide a human-readable chronicle of what the system has learned. Runs at most once per `NARRATIVE_INTERVAL_HOURS` (default 24h) as phase 6 of the nightly sleep cycle. The narrative is stored as a `narrative_entry` record and pruned after `NARRATIVE_ENTRY_RETENTION_DAYS` days (default 90). This is distinct from wiki content — it's an autobiographical memory of system activity rather than curated knowledge.

### CAP-CONS-011 — Causal Discovery (PC algorithm)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `CAUSAL_THRESHOLD`, `MAX_CAUSED_BY_ROWS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CA2`, `BC-CA3`
- **refs:** `yadgar/consolidation/causal.py::_CausalMixin._run_causal_discovery_phase`, `yadgar/causal_discovery/pc.py::pc_algorithm`, `yadgar/causal_discovery/__init__.py`
- **wiring:** `_consolidation_cycle()` → `_run_causal_discovery_phase()`. Runs periodically: fires when `_events_since_last_discovery >= 50` (hardcoded threshold, not CAUSAL_THRESHOLD). `CAUSAL_THRESHOLD` (default 3) controls how many times an entity must co-occur to be considered causally relevant in `CausalDiscovery.detect_causality()` — a separate simpler method. `MAX_CAUSED_BY_ROWS` bounds the `caused_by` table query. The PC algorithm is initialized in `ConsolidationScheduler.__init__()` via lazy import of `CausalDiscovery`.
- **explanation:** Implements the PC (Peter-Clark) constraint-based causal discovery algorithm to discover directed causal relationships between entities in the knowledge graph. Builds a time-aligned binary event matrix (1-hour buckets over 168 hours) where rows = time windows and columns = entity variables. Phase 1 removes undirected edges where conditional independence is detected (Fisher's z-test). Phase 2 orients v-structures and applies Meek's rules (R1/R2/R3) to produce a Partially Directed Acyclic Graph (PDAG). Results are stored as directed and undirected edge records in the knowledge graph. Fires only when ≥ 50 new memories have been added since the last discovery run.

### CAP-CONS-012 — Consolidation Cycle & Cooldown
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** `consolidate_now`
- **migrations:** —
- **bc:** `BC-C1`
- **refs:** `yadgar/consolidation/__init__.py::ConsolidationScheduler.force_consolidate`, `yadgar/consolidation/__init__.py::ConsolidationScheduler.run_nightly_consolidation`, `yadgar/server/tools/admin_other.py::consolidate_now`
- **wiring:** `consolidate_now` MCP tool → `force_consolidate()` → `_consolidation_cycle()`. The nightly cron calls `run_nightly_consolidation()` which runs the cycle then `_maybe_sleep_cycle()`. `CONSOLIDATION_COOLDOWN_SECONDS` and `IDLE_THRESHOLD_SECONDS` were CONFIG-ONLY (daemon removed in v5.7.0) — both deleted in v6 T3.
- **explanation:** The main consolidation cycle orchestrates six phases: (1) episodic phases — decay, episode processing, prune, duplicate merge; (2) graph phases — similarity linking, causality detection, graph/cofire priors; (3) curation phases — memify, CLS consolidation, action log; (4) domain consolidation via AstrocytePool; (5) formal causal discovery (periodic); (6) retention tasks. The `consolidate_now` MCP tool exposes this on-demand. `CONSOLIDATION_COOLDOWN_SECONDS` and `IDLE_THRESHOLD_SECONDS` deleted in v6 T3 (#41) — the background daemon was removed in v5.7.0; these settings had no runtime consumer.

### CAP-CONS-013 — Vacuum (DB compaction)
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `vacuum_now`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_vacuum.py::vacuum_now`, `yadgar/ops.py`
- **wiring:** `vacuum_now` MCP tool → `_fire_vacuum_service()` in `yadgar/ops.py`. Writes a trigger file to `~/.local/state/yadgar/triggers/` watched by launchd/systemd, which fires `yadgar-vacuum.service`. Auto-vacuum also fires from `_maybe_auto_vacuum()` called in `_run_post_cycle_tasks()` after each consolidation cycle when DB size exceeds threshold and we're in the configured time window.
- **explanation:** Triggers SurrealDB compaction (VACUUM) to reclaim disk space from deleted/updated records. Intentionally runs out-of-process via a trigger file to avoid blocking the consolidation cycle. The MCP tool `vacuum_now` accepts a `force` flag that bypasses the in-window check. Auto-vacuum (from consolidation post-cycle tasks) respects a 6-hour in-memory cooldown and the VACUUM_AUTO_WINDOW_START/END config window to avoid running during peak usage hours.

### CAP-CONS-014 — Engram Allocator (Josselyn-Frankland slot model)
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `EXCITABILITY_BOOST`, `EXCITABILITY_HALF_LIFE_HOURS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/engram.py::EngramAllocator`, `yadgar/engram.py::EngramAllocator.allocate`, `yadgar/engram.py::EngramAllocator.boost_excitability`
- **wiring:** `EngramAllocator` is instantiated at server startup. Called when memories are written: `allocate(memory_id)` selects a slot, boosts its excitability by `EXCITABILITY_BOOST`, applies lateral inhibition to ±2 neighboring slots (at 50% of boost), and updates the memory's `excitability` field. Slot excitability decays exponentially with `EXCITABILITY_HALF_LIFE_HOURS` (default 6h). Warm slots (excitability ≥ 0.05) attract nearby writes, creating automatic temporal clusters.
- **explanation:** Implements the Josselyn & Frankland (2007) / Rashid et al. (2016) engram cell model — NOT Hopfield networks. Neurons (memory slots) compete via CREB-like excitability: the most excited slot wins the allocation competition, and memories written during the same excited window share a slot, creating temporal associations with zero explicit logic. After ~3 half-lives (~18 hours with default settings), a slot's excitability drops below the warm threshold and the next write starts a new temporal cluster. Lateral inhibition (reducing neighbors' excitability) sharpens cluster boundaries.

### CAP-CONS-015 — Plasticity / Stability / Reconsolidation (schema fields — dead config)
- **status:** DEAD (v6 T3 — all 5 settings deleted from config.py)
- **category:** brain-dynamics
- **settings:** — (PLASTICITY_SPIKE, PLASTICITY_HALF_LIFE_HOURS, STABILITY_INCREMENT, RECONSOLIDATION_LOW_THRESHOLD, RECONSOLIDATION_HIGH_THRESHOLD all deleted)
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/models.py` (DB schema fields retained)
- **wiring:** Settings deleted from `config.py` in v6 T3. The memory schema still has `plasticity`, `stability`, and `reconsolidation_count` fields (written at insert time with hardcoded values: plasticity=1.0, stability=0.0). No production code ever read these settings.
- **explanation:** Reconsolidation theory (Nader et al. 2000) schema support remains in DB fields but the behavioral logic was never implemented. Config settings deleted in v6 T3 (#41). DB schema fields retained for future implementation.

### CAP-CONS-016 — Derived Beliefs
- **status:** LIVE
- **category:** enrichment
- **settings:** `DERIVED_BELIEFS_ENABLED`, `DERIVED_BELIEF_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-MC3`
- **refs:** `yadgar/retrieval/fusion.py`, `yadgar/curation/__init__.py::MemoryCurator._memify_derive`, `yadgar/curation/strengthen.py::_memify_derive`
- **wiring:** `DERIVED_BELIEFS_ENABLED` is checked in `yadgar/retrieval/fusion.py:422` (getattr default False — note mismatch with config default True). `_memify_derive()` is called from `MemoryCurator.memify_cycle()` → called from `_run_curation_phases()` during each consolidation cycle. `DERIVED_BELIEF_RETENTION_DAYS` controls pruning of `derived_belief` table rows in `_run_retention_tasks()`.
- **explanation:** Derives new beliefs by finding co-occurring entity clusters in the memory store and creating summary "derived" memories tagged `["derived", "auto-generated"]`. The derive pass scans entities that frequently appear together, generates a co-occurrence summary, and inserts it as a new episodic memory with importance=0.6. Derived beliefs are pruned from the `derived_belief` table after `DERIVED_BELIEF_RETENTION_DAYS` days. The retrieval path checks `DERIVED_BELIEFS_ENABLED` to decide whether to include derived belief results in fusion — note a potential config default mismatch between config.py (True) and fusion.py (getattr default False).

### CAP-CONS-017 — Cognitive Load Limiting
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** `COGNITIVE_LOAD_LIMIT`
- **tools:** —
- **migrations:** —
- **bc:** `BC-MC4`
- **refs:** `yadgar/metacognition/__init__.py`, `yadgar/metacognition/cognitive_load.py::_CognitiveLoadMixin`
- **wiring:** `MetacognitionEngine.__init__()` sets `self._chunk_limit = settings.COGNITIVE_LOAD_LIMIT`. `_CognitiveLoadMixin.manage_context()` enforces this limit on recall result sets. Called from the recall pipeline when metacognition is enabled. Default is 4 (Cowan's 4±1 model of working memory capacity).
- **explanation:** Implements Cowan's (2001) model of working memory capacity: the number of independently retrievable chunks is limited to 4 ± 1. `COGNITIVE_LOAD_LIMIT` caps how many memory chunks can be returned in a single recall context window. Memories exceeding the limit are either summarized (overflow handling) or dropped, preventing context saturation. This is the retrieval-side counterpart to the storage-side episode chunking controlled by MAX_EPISODE_TOKENS.

### CAP-CONS-018 — Successor Representation / Cognitive Map
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CM1`
- **refs:** `yadgar/cognitive_map.py::CognitiveMap`, `yadgar/restoration.py`, `yadgar/server/lifecycle.py`
- **wiring:** `CognitiveMap` is instantiated in `server/lifecycle.py::_init_secondary_engines()` and passed to `CheckpointRestore`. Every `recall` call records a transition via `_st._cognitive_map.record_transition()` + `incremental_update()` (recall.py:329-338). During `restore` MCP tool, `CheckpointRestore._predict_memories()` calls `has_sufficient_data()` (needs ≥20 transitions) and then `navigate_to()` — results are iterated into the `predicted` list and included in the restore output. Spatial layout methods (extract_coordinates/update_memory_sr_coords/get_neighborhood/get_sr_scores) were retired in v5.71.0 (#47). BC-CM1 e2e-proven in v5.75 (train T5): `tests/e2e/test_phase3_closure.py::TestBCCM1_SRTransitionMatrixBuilt`.
- **explanation:** The Successor Representation (Dayan 1993) predicts future states from current position in a learned transition graph. `build_transition_matrix()` counts memory-to-memory transitions from the `memory_transition` table, applies discount factor γ, and computes the SR matrix as `(I - γT)^{-1}`. `navigate_to()` uses the SR to rank memories by expected future relevance given a query memory as starting state. The SR result feeds `CheckpointRestore._predict_memories()` and surfaces in the `restore` tool output as predicted next-retrieval candidates. BC-CM1 (SR transition matrix built) is LIVE and e2e-proven.

### CAP-CONS-019 — Action Log Processing
- **status:** LIVE
- **category:** consolidation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-AS1`, `BC-AS2`
- **refs:** `yadgar/consolidation/cleanup.py::_CleanupMixin._process_action_log`
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
- **refs:** `yadgar/curation/prune_passes.py::_memify_prune`, `yadgar/curation/__init__.py::MemoryCurator.memify_cycle`
- **wiring:** `_run_curation_phases()` → `self._curator.memify_cycle()` → `_memify_prune()`. Runs every consolidation cycle. Operates in multiple passes: (1) cold unaccessed auto-generated memories, (2) cold unaccessed auto-abstracted, (3) cold unaccessed dream insights, (4) hard-cap dream insights by `DREAM_INSIGHT_MAX_AGE_DAYS`, (5) stale action-stream memories by recency (v5.66), (6) degenerate auto-abstracted schemas. Protected memories and recently-accessed memories are always spared.
- **explanation:** Implements the retention policy for system-generated (non-user) memories. Pruning is structured in ordered passes to catch different memory classes: action-stream summaries use a combined created_at + last_accessed recency check (v5.66 fix — a single accidental recall no longer grants immortality), dream insights have a hard age cap regardless of heat, and degenerate CLS schemas (no meaningful subject) are deleted unconditionally. User-created memories are never touched by this pass; the `cold_retention` pass (CAP-CONS-005) handles those via separate gated logic.

### CAP-CONS-022 — Metacognitive Gap Detection
- **status:** LIVE
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-MC1`, `BC-MC2`
- **refs:** `yadgar/metacognition/gap_detection.py::_GapDetectionMixin.detect_gaps`, `yadgar/restoration.py::RestorationEngine._detect_gaps_safe`
- **wiring:** `RestorationEngine._detect_gaps_safe()` calls `self._metacognition.detect_gaps(directory)` during context restore (up to 3 gaps included in restore output). `detect_gaps()` runs five detection passes: isolated entities (≤1 connection), stale memory regions (heat < 0.3), low-confidence memories (< 0.5), missing co-occurrence links (entities co-occurring in ≥ 2 memories but without a graph edge), and one-sided knowledge (errors with no resolved_by edge). Wired into the restore path, so it runs on every `restore` invocation.
- **explanation:** Implements MetaRAG Signal 2 — awareness of what the system does NOT know. BC-MC1 (coverage scored by entity/topic distribution) is partially implemented via the entity-graph analysis in the five gap passes. BC-MC2 (gap detection flags missing topics) is implemented: `detect_gaps()` returns structured gap records with type, description, severity, affected entities, and remediation suggestions. These surface knowledge holes: isolated entities, stale knowledge regions, low-confidence beliefs, missing co-occurrence relationships, and unresolved errors.

### CAP-CONS-023 — Retired Cognitive Map Capabilities (BC-CM2/CM3)
- **status:** DEAD
- **category:** brain-dynamics
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CM2`, `BC-CM3`
- **refs:** `yadgar/cognitive_map.py`
- **wiring:** BC-CM2 (topological/spatial layout via `extract_coordinates`/`update_memory_coordinates`) and BC-CM3 (`get_neighborhood`/`get_sr_scores`/`is_dirty`) were RETIRED in v5.71.0 (#47). The methods were deleted from the codebase. These BCs are marked 🗑 RETIRED in BEHAVIOR_CONTRACT.md — not failing specs, but permanently removed capability. The SR matrix and `sr_x`/`sr_y` coordinate fields remain on the memory schema (used by the active `CognitiveMap.compute_sr_matrix()` path), but the spatial layout and neighborhood-query methods are gone.
- **explanation:** The spatial/topological visualization capabilities — `extract_coordinates()` for 2D layout and `get_neighborhood()`/`get_sr_scores()`/`is_dirty()` for proximity queries — were removed as dead code in v5.71.0 during the #41 dead-config audit. They were never wired to any MCP tool or recall path. The `CognitiveMap` class remains but is scoped to: `build_transition_matrix()`, `compute_sr_matrix()`, `navigate_to()`, and `has_sufficient_data()` — the SR-based recall navigation path (active via RestorationEngine).

### CAP-CONS-025 — Replay / Restoration Settings
- **status:** LIVE
- **category:** consolidation
- **settings:** `REPLAY_ANCHOR_HEAT`, `REPLAY_CHECKPOINT_AUTO_INTERVAL`, `REPLAY_MAX_RESTORE_MEMORIES`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/restoration.py`
- **wiring:** All three settings are read by `RestorationEngine` in `yadgar/restoration.py`. `REPLAY_CHECKPOINT_AUTO_INTERVAL` (default 50) triggers auto-checkpoint every N tool calls. `REPLAY_ANCHOR_HEAT` (default 1.0) sets the heat of anchored memories when they are loaded in a restore pass. `REPLAY_MAX_RESTORE_MEMORIES` (default 8) caps the number of memories included in a restoration context packet.
- **explanation:** Controls the behavior of the checkpoint/restore system used to resume context after `/clear` or session restart. Auto-checkpointing fires every `REPLAY_CHECKPOINT_AUTO_INTERVAL` tool calls to keep a recent state snapshot. On restore, up to `REPLAY_MAX_RESTORE_MEMORIES` memories are included in the context reconstruction, and anchored memories are assigned `REPLAY_ANCHOR_HEAT` to ensure they remain hot and at the top of ranked results.

### CAP-WIKI-001 — Wiki similarity gate (duplicate prevention)
- **status:** LIVE
- **category:** wiki
- **settings:** `WIKI_SIM_GATE_ENABLED`, `WIKI_SIM_MODE`, `WIKI_SIM_CONTENT_THRESHOLD`, `WIKI_SIM_TITLE_THRESHOLD`, `WIKI_SIM_TOP_K`
- **tools:** `wiki_add`, `wiki_check_duplicate`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/wiki.py::_check_similarity_gate`, `yadgar/server/tools/wiki.py::wiki_add`
- **wiring:** MCP caller → `wiki_add()` → (async path) enqueued job → `QueueDrainer._apply()` → similarity gate runs in drainer pre-apply stage via `_sim_gate_for_drainer`. For `wait=True` callers, result surfaces synchronously via `wait_for_job()` + `get_job_result()`. `wiki_check_duplicate` exposes the gate as a read-only dry-run (no write). Gate is bypassed when `force=True`, `replace_slug` is set, or `append=True`.
- **explanation:** Prevents near-duplicate wiki pages by comparing the candidate content + title against existing pages using embedding cosine similarity. Configured via `WIKI_SIM_CONTENT_THRESHOLD` (default 0.80) and `WIKI_SIM_TOP_K` (default 5 candidates). In `hard` mode (default `WIKI_SIM_MODE`), a match causes the write to be rejected with a `duplicate_detected` reason. In `soft` mode, the match is logged but the write is allowed. As of v5.41.5 the gate runs in the drainer (not the request thread) to satisfy the I9 latency budget; `wait=False` callers receive a deferred check, `wait=True` callers receive synchronous rejection.

### CAP-WIKI-002 — Wiki write-wait path (read-your-writes via wait=True)
- **status:** LIVE
- **category:** wiki
- **settings:** `WIKI_WRITE_WAIT_TIMEOUT_SECONDS`
- **tools:** `wiki_add`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/wiki.py::_wiki_add_wait_path`
- **wiring:** `wiki_add(wait=True)` → `_wiki_add_wait_path()` → enqueues job to `FileQueue`, then calls `QueueDrainer.wait_for_job(job_id, timeout=WIKI_WRITE_WAIT_TIMEOUT_SECONDS)` → after completion, retrieves rejection via `get_job_result()` → returns synchronous result. Falls back to sync write if no drainer is running or `replace_slug` is set.
- **explanation:** Provides a read-your-writes guarantee for `wiki_add` callers who need to know immediately whether their write committed or was rejected by the similarity gate. The caller blocks for up to `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` (default 5.0 s) while the drainer processes the job in FIFO order. On timeout, returns `{stored: False, reason: "wait_timeout"}` with the job still queued. This is an opt-in slow path — the default async path (`wait=False`) returns immediately.

### CAP-WIKI-003 — Wiki CRUD tool surface (add/read/list/delete/query/lint/drafts/approve/discard/coverage)
- **status:** LIVE
- **category:** wiki
- **settings:** `WIKI_SLUG_PREFIX`, `WIKI_EMBED_FAILURE_BLOCKS_WRITE`
- **tools:** `wiki_add`, `wiki_read`, `wiki_list`, `wiki_delete`, `wiki_query`, `wiki_lint`, `wiki_drafts`, `wiki_approve`, `wiki_discard`, `wiki_check_duplicate`, `wiki_coverage`, `wiki_get`, `wiki_update`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/wiki.py::wiki_read`, `yadgar/server/tools/wiki.py::wiki_list`, `yadgar/server/tools/wiki.py::wiki_delete`, `yadgar/server/tools/wiki.py::wiki_query`, `yadgar/server/tools/wiki.py::wiki_lint`, `yadgar/server/tools/wiki.py::wiki_drafts`, `yadgar/server/tools/wiki.py::wiki_approve`, `yadgar/server/tools/wiki.py::wiki_discard`, `yadgar/server/tools/wiki_coverage.py::wiki_coverage`, `yadgar/server/tools/admin_other.py::wiki_get`, `yadgar/server/tools/admin_other.py::wiki_update`
- **wiring:** All tools are `@_tool()`-registered and reachable directly via MCP. `wiki_add` enqueues to `FileQueue` (async) or uses sync write path. `wiki_read`, `wiki_list`, `wiki_delete`, `wiki_get` delegate to `_st._wiki` (WikiStore). `wiki_query` performs keyword+semantic search with §25 branch and directory filtering. `wiki_lint` calls `_st._wiki.lint()`. Draft tools (`wiki_drafts`, `wiki_approve`, `wiki_discard`) operate on the wiki_draft table via `_get_storage()`. `wiki_coverage` scans filesystem `.py` files and cross-references wiki pages tagged `mod`/`fn`. `wiki_update` delegates to `_st._storage.update_wiki_page()`. `WIKI_SLUG_PREFIX` is injected into the `FileQueue` wiki mirror path at lifecycle init. `WIKI_EMBED_FAILURE_BLOCKS_WRITE` controls whether an embedding failure causes the write to be blocked.
- **explanation:** The core wiki management surface. `wiki_add` creates or upserts pages (async-queued by default, with similarity gate and secret gate). `wiki_read` resolves a slug using §25 4-step directory+branch resolution. `wiki_list` returns metadata-only page listings scoped to a directory. `wiki_query` performs combined FTS + semantic search with branch-aware filtering and a 1.5× current-branch score boost. `wiki_lint` identifies orphan pages, broken cross-references, and stale/low-confidence pages. Draft tools support a review workflow where pages land as candidates before promotion. `wiki_coverage` computes Python module coverage by `mod`/`fn` tagged wiki pages. `wiki_get`/`wiki_update` provide integer-ID-based fetch and field-patch access.

### CAP-WIKI-004 — Wiki versioning and history (history/read_version/diff/restore)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/wiki.py::wiki_history`, `yadgar/server/tools/wiki.py::wiki_read_version`, `yadgar/server/tools/wiki.py::wiki_diff`, `yadgar/server/tools/wiki.py::wiki_restore`, `yadgar/server/tools/wiki.py::_resolve_page_id_by_slug`
- **wiring:** All tools call `_resolve_page_id_by_slug(slug, directory, branch_hint)` which applies §25 directory+branch resolution, then delegate to `_st._wiki.history()`, `.read_version()`, `.diff()`, `.restore_version()` respectively. These operations write synchronously (no async queue). `wiki_restore` creates a new version (N+1) and bypasses the similarity gate.
- **explanation:** Full version-control surface for wiki pages introduced in v5.41.0. `wiki_history` lists versions (metadata only) newest-first. `wiki_read_version` fetches the full snapshot for a specific version number. `wiki_diff` produces a unified-text or structured JSON diff between two version numbers. `wiki_restore` rolls a page back to a historical version by creating a new version whose content matches the target — it preserves intervening history and bypasses the similarity gate (explicit user intent). Note: `wiki_history` may show stale data immediately after `wiki_add(wait=False)` since the write is async; use `wait=True` for read-your-writes consistency.

### CAP-WIKI-005 — Wiki surgical edit surface (section-atomic, anchor-text, positional, structural)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_append_section`, `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_delete_at`, `wiki_insert_at`, `wiki_replace_markdown_block`, `wiki_set_metadata`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/wiki.py::wiki_append_section`, `yadgar/server/tools/wiki.py::wiki_replace_text`, `yadgar/server/tools/wiki.py::wiki_delete_text`, `yadgar/server/tools/wiki.py::wiki_insert_after`, `yadgar/server/tools/wiki.py::wiki_insert_before`, `yadgar/server/tools/wiki.py::wiki_replace_at`, `yadgar/server/tools/wiki.py::wiki_delete_at`, `yadgar/server/tools/wiki.py::wiki_insert_at`, `yadgar/server/tools/wiki.py::wiki_replace_markdown_block`, `yadgar/server/tools/wiki.py::wiki_set_metadata`
- **wiring:** All tools call `_resolve_page_id_by_slug(slug, directory, branch_hint)` then delegate to corresponding `_st._wiki` methods. All bypass the v5.39 similarity gate. Write-bearing tools (`wiki_replace_text`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_insert_at`, `wiki_replace_markdown_block`, `wiki_append_section`) run `gate_or_reject()` (I26 secret gate). Deletion tools (`wiki_delete_text`, `wiki_delete_at`) do not run secret gate (nothing new written). `wiki_set_metadata` updates `directory_context` or `branch` fields only and is idempotent.
- **explanation:** Layer 1–4 surgical edit primitives introduced in v5.61.0 to prevent whole-page replacement errors. Layer 1 (anchor-text): `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_after`, `wiki_insert_before` locate content by unique text strings. Layer 2 (positional): `wiki_replace_at`, `wiki_delete_at`, `wiki_insert_at` operate by line/col coordinates with a mandatory `anchor_hint` (≥20 chars) to guard against off-by-one errors. Layer 3 (structural): `wiki_replace_markdown_block` addresses the Nth block of a markdown block type (paragraph, heading, code_fence, etc.). Section-atomic: `wiki_append_section` patches a named section (by heading) without touching the rest of the document — this was introduced specifically to prevent the 2026-05-31 corruption pattern. `wiki_set_metadata` is the Layer 4 metadata primitive for repositioning pages across branches/directories.

### CAP-WIKI-006 — Wiki §25 directory+branch scoping and resolution
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_read`, `wiki_query`, `wiki_list`, `wiki_add`, `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`, `wiki_append_section`, `wiki_replace_text`, `wiki_delete_text`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_delete_at`, `wiki_insert_at`, `wiki_replace_markdown_block`, `wiki_set_metadata`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/wiki.py::_resolve_page_id_by_slug`, `yadgar/server/tools/wiki.py::wiki_read`
- **wiring:** All read/write tools that accept `directory` + `branch_hint` parameters call `_resolve_page_id_by_slug()` or `_st._wiki.read_by_directory_branch()` for §25 4-step resolution. Branch detection uses `_detect_branch(cwd)` from `project.py` with a 30-second LRU cache; when daemon-side detection returns None (container scenario), `branch_hint` supplied by the caller is used as fallback.
- **explanation:** The §25 directory+branch scoping system ensures wiki pages are correctly scoped to the project (directory_context) and branch they were written on. Resolution order for reads: (1) caller-dir + effective-branch, (2) caller-dir + NULL branch (canonical slot), (3) 'global' + NULL branch, (4) not-found. For writes, branch and directory are validated at the MCP boundary (enforcement gates `YADGAR_BRANCH_ENFORCEMENT`, `YADGAR_DIRECTORY_ENFORCEMENT`). This prevents cross-project wiki leakage and enables per-branch draft pages that fall through to the canonical slot on the default branch. The `branch_hint` parameter was added in v5.42.3–v5.42.6 to handle container/CI scenarios where git is unavailable in the daemon process.

### CAP-WIKI-007 — Wiki stale-detection and branch cleanup (refresh_stale, cleanup_merged_branches)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_refresh_stale`, `wiki_cleanup_merged_branches`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/project.py::wiki_refresh_stale`, `yadgar/server/tools/project.py::wiki_cleanup_merged_branches`, `yadgar/server/tools/project.py::_scan_stale_wiki_slugs`
- **wiring:** Both tools are `@_tool(power=True)`-registered in `project.py`. `wiki_refresh_stale` scans `.local-review/wiki/*.md` frontmatter for hash drift against `source_files`, writes a refresh-queue JSON under `.local-review/wiki/refresh-queue/<ts>.json`, and returns stale slugs with suggested Agent calls. `wiki_cleanup_merged_branches` queries `wiki_page` rows with `branch IS NOT NONE` then cross-references against `git branch -a` to find orphaned branch pages; with `dry_run=False` it deletes them.
- **explanation:** `wiki_refresh_stale` detects repo-wiki pages (`.local-review/wiki/*.md`) whose embedded SHA256 hash of `source_files` has drifted from the current file content, signalling that the generated documentation is out of date. It only runs on the default branch unless `force_branch=True`. The stale count is also TTL-cached (`_stale_count_cache`, 300 s default) and surfaced in `project_brief(mode='signals')`. `wiki_cleanup_merged_branches` finds wiki pages whose branch no longer exists in git and can delete them to prevent accumulation of dead feature-branch pages (§26 hygiene).

### CAP-WIKI-008 — Curation similarity threshold (near-duplicate merge gate)
- **status:** LIVE
- **category:** curation
- **settings:** `CURATION_SIMILARITY_THRESHOLD`
- **tools:** —
- **migrations:** —
- **bc:** `BC-CU3`
- **refs:** `yadgar/curation/__init__.py::MemoryCurator.curate_on_remember`, `yadgar/curation/ingestion.py::find_similar_memories`
- **wiring:** `memorize()` → `MemoryCurator.curate_on_remember()` → `find_similar_memories()` (cosine search) → for any pair with similarity ≥ `CURATION_SIMILARITY_THRESHOLD` AND textual Jaccard > 0.5, existing memory is merged via `merge_memory()`. Also used by `cls_store/promotion.py` for cluster promotion decisions.
- **explanation:** Controls the cosine similarity threshold above which two memories with sufficient textual overlap are merged (deduplicated) rather than stored as separate records. Default 0.95 (near-exact duplicates only). The merge operation keeps the highest-heat memory, combining tags and updating the embedding. This prevents accumulating semantically-identical records across sessions. Lower values cause more aggressive merging; the textual-overlap guard prevents merging memories that score high on embeddings but carry genuinely different information (e.g. two functions with similar names).

### CAP-WIKI-009 — Curation prune passes (BC-CU1, BC-CU2)
- **status:** LIVE
- **category:** curation
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-CU1`, `BC-CU2`
- **refs:** `yadgar/curation/prune_passes.py`, `yadgar/curation/__init__.py`
- **wiring:** Called from `MemoryCurator` during the consolidation cycle (`consolidate_now` → memify self-improvement → `_memify_prune()`). The co-occurrence strengthen pass (`_memify_strengthen`) and recency prune gate (`_memify_prune`) run as part of the nightly or on-demand consolidation cycle.
- **explanation:** BC-CU1 covers the co-occurrence memify pass: memories that co-occur frequently are strengthened (heat boost) and stamped with the originating directory context (v5.64). BC-CU2 covers the recency prune gate (v5.66): very old, cold memories below a recency threshold are pruned during the consolidation cycle. Both passes are components of the `MemoryCurator` self-improvement cycle alongside the derive and reweight passes.

### CAP-WIKI-010 — Memory block CRUD (block_create/get/update/delete/list/replace/append)
- **status:** LIVE
- **category:** storage
- **settings:** `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT`, `MEMORY_BLOCK_HARD_CHAR_LIMIT`, `MEMORY_BLOCK_MAX_PER_SCOPE`, `MEMORY_BLOCK_TOTAL_BUDGET_CHARS`
- **tools:** `block_create`, `block_get`, `block_update`, `block_delete`, `block_list`, `block_replace`, `block_append`
- **migrations:** —
- **bc:** `BC-IC1`, `BC-IC2`, `BC-IC3`, `BC-IC4`
- **refs:** `yadgar/server/tools/blocks.py::block_create`, `yadgar/server/tools/blocks.py::block_get`, `yadgar/server/tools/blocks.py::block_update`, `yadgar/server/tools/blocks.py::block_delete`, `yadgar/server/tools/blocks.py::block_list`, `yadgar/server/tools/blocks.py::block_replace`, `yadgar/server/tools/blocks.py::block_append`
- **wiring:** All tools are `@_tool(power=True)`-registered and delegate to `_get_storage()._BlocksMixin` methods. `scope='project'` requires a non-empty `directory` parameter (enforced via `_require_directory_for_project_scope`). `block_create` initialises char_limit from `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT` (default 2000) when not specified; `MEMORY_BLOCK_HARD_CHAR_LIMIT` (default 8000) is the absolute ceiling. `bootstrap_project` auto-seeds default blocks (`current_task`, `gotchas`) via `_seed_default_blocks`.
- **explanation:** Letta-style named memory blocks introduced in v5.33.0. Blocks are always-injected, named text containers scoped to either a project directory or globally. `block_create` creates a new block with a char limit; `block_get` retrieves by name+scope; `block_update` full-replaces content (char limit enforced); `block_delete` removes idempotently; `block_list` returns all blocks for a scope+directory; `block_replace` and `block_append` are surgical patch operations that avoid re-emitting full content. `MEMORY_BLOCK_TOTAL_BUDGET_CHARS` controls the aggregate character budget across all blocks injected into context. All write operations run `gate_or_reject()` for secret detection (I26).

### CAP-WIKI-011 — Wiki bookmarks (bookmark_add/remove/list/reorder)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `bookmark_add`, `bookmark_remove`, `bookmark_list`, `bookmark_reorder`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/bookmarks.py::bookmark_add`, `yadgar/server/tools/bookmarks.py::bookmark_remove`, `yadgar/server/tools/bookmarks.py::bookmark_list`, `yadgar/server/tools/bookmarks.py::bookmark_reorder`
- **wiring:** All tools are `@_tool()`-registered and delegate to `_get_storage()._BookmarksMixin` methods (`add_bookmark`, `remove_bookmark`, `list_bookmarks`, `reorder_bookmark`). No secret gate (bookmarks are slug references, not user content). `bookmark_add` is idempotent on slug (updates label if already present).
- **explanation:** User-curated ordered list of wiki page slugs for quick navigation. `bookmark_add` pins a slug at the next available position with an optional display label override. `bookmark_remove` unpins idempotently. `bookmark_list` returns all bookmarks sorted by position. `bookmark_reorder` moves a bookmark to a new 0-based position using dense-integer semantics (all positions compact to 0, 1, 2, … after reorder). Bookmarks are stored in the `wiki_bookmark` table and are not scoped to a directory or branch.

### CAP-WIKI-012 — Project brief (project_brief, BRIEF_MODE_DEFAULT)
- **status:** LIVE
- **category:** wiki
- **settings:** `BRIEF_MODE_DEFAULT`, `PROJECT_BRIEF_MAX_ANCHORS`, `PROJECT_CONTEXT_MIN_HEAT`, `SIGNALS_TOKEN_BUDGET_SOFT`
- **tools:** `project_brief`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/project.py::project_brief`, `yadgar/server/tools/project.py::_project_brief_signals`, `yadgar/server/tools/project.py::_project_brief_restore`, `yadgar/server/tools/project.py::_project_brief_catalog_full`
- **wiring:** MCP caller → `project_brief(directory, mode)` → resolves project root via `_resolve_project_root()`, fetches presence rows (`_project_init`, `_active_work`, checkpoint) → dispatches to mode-specific builder: `_project_brief_signals()`, `_project_brief_restore()`, or `_project_brief_catalog_full()`. The signals mode also calls `_compute_anchor_signals()`, `_apply_roadmap_signal()`, and `_apply_rejection_signal()`. Stop hook calls `project_brief(mode='signals')` on session end; restore hook calls `project_brief(mode='restore')`.
- **explanation:** Layered project context snapshot with four modes. `signals` (<100 tokens): binary presence flags, age numerics, anchor hygiene signals, roadmap lag, DLQ rejection count, and `recommended_actions` list — designed for the stop hook. `restore` (<800 tokens): anchors + hot memories + checkpoint + wiki catalog — designed for post-`/clear` context restoration. `catalog` (~500 tokens, deprecated since v5.7.12): full shape with anchors + presence + hot memories + wiki keys + `_render`. `full` (~1050 tokens): superset of catalog with inlined init_memory and active_work. `SIGNALS_TOKEN_BUDGET_SOFT` (default 350) emits an observability metric when the signals payload exceeds the budget. `PROJECT_BRIEF_MAX_ANCHORS` (default 12) caps the anchor list in restore mode. `PROJECT_CONTEXT_MIN_HEAT` (default 0.01) is a filter threshold for nearly-cold memories in hot_memories sections.

### CAP-WIKI-013 — Bootstrap and seed project tools
- **status:** LIVE
- **category:** ops
- **settings:** `PROJECT_INIT_CAP_CHARS`
- **tools:** `bootstrap_project`, `seed_project`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/project.py::bootstrap_project`, `yadgar/server/tools/misc.py::seed_project`, `yadgar/seed/_generate.py::seed_project`
- **wiring:** `bootstrap_project(directory, content)` is `@_tool(power=True)` in `project.py`; it calls `_resolve_project_root()`, `_get_storage().upsert_project_init()`, then `_seed_default_blocks()` to create default `current_task` and `gotchas` blocks. `seed_project(directory, dry_run)` is `@_tool(power=True)` in `misc.py`; it delegates to `yadgar.seed.seed_project()` which scans the project directory for config files, docs, and source structure and creates `_seed`-tagged memories. Re-running is idempotent — old seed memories are replaced.
- **explanation:** Two complementary project bootstrapping tools. `bootstrap_project` is lightweight: it takes a caller-supplied concise markdown string (capped at `PROJECT_INIT_CAP_CHARS` chars, default 2000) and stores it as the `_project_init` memory for the directory, also seeding default memory blocks. `seed_project` is heavyweight: it auto-scans the project directory tree to discover config files, documentation, CI configs, and key source files, synthesising foundational `_seed`-tagged memories without any manual input.

### CAP-WIKI-014 — Sync instructions tool
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `sync_instructions`
- **migrations:** —
- **bc:** `BC-HK2`
- **refs:** `yadgar/server/tools/misc.py::sync_instructions`
- **wiring:** `@_tool(power=True)`-registered in `misc.py`. Called directly by MCP clients or the Claude Code `install_hooks` flow. Reads `~/.claude/CLAUDE.md`, finds or appends the `## Memory System — Yadgar` section using a regex, and atomically replaces the file via `tempfile.mkstemp` + `os.replace`.
- **explanation:** Writes or updates the Yadgar protocol block in the user's global `CLAUDE.md` file so Claude Code sessions receive up-to-date tool usage instructions. The section is version-stamped and idempotent: re-running replaces only the Yadgar section, leaving the rest of `CLAUDE.md` intact. Uses atomic write (tmp + rename) to prevent corruption on crash. BC-HK2: a stale block is replaced, not duplicated.

### CAP-WIKI-015 — Agent prompt versioning (agent_prompt_get/save/dispatch_prelude)
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `agent_prompt_get`, `agent_prompt_save`, `agent_dispatch_prelude`
- **migrations:** —
- **bc:** `BC-AP1`, `BC-AP2`, `BC-AP3`
- **refs:** `yadgar/server/tools/agent_prompts.py::agent_prompt_get`, `yadgar/server/tools/agent_prompts.py::agent_prompt_save`, `yadgar/server/tools/dispatch_helper.py::agent_dispatch_prelude`
- **wiring:** `agent_prompt_save` is `@_tool()`-registered in `agent_prompts.py`; routes through `_st._wiki.add()` with `branch + directory` provenance (v5.42.5). `agent_prompt_get` queries `wiki_page WHERE tags CONTAINS $tag AND tags CONTAINS 'agent-prompt'`, finds the highest `vN` suffix in slug. `agent_dispatch_prelude` is `@_tool()`-registered in `dispatch_helper.py`; calls `agent_prompt_get()` then optionally `recall()` + `wiki_query()` (when `include_context=True`) to build a markdown prelude string capped at 2000 chars (4000 with context).
- **explanation:** A versioned agent-prompt registry stored as wiki pages. `agent_prompt_save(pattern, content)` creates a new version page with slug `agent-prompt-<pattern>-v<N>` where N is auto-incremented. `agent_prompt_get(pattern)` retrieves the highest-versioned page for a pattern. `agent_dispatch_prelude(pattern, task_topic)` composes a standard markdown prelude for subagent dispatch: the Yadgar protocol contract block, the latest saved prompt for the pattern (if any), and a recall hint for the task topic. The v5.44.0 X1 extension adds opt-in `include_context=True` mode which auto-prefetches recall + wiki_query results and embeds them in the prelude.

### CAP-WIKI-016 — Session-end sentinel capture
- **status:** LIVE
- **category:** ops
- **settings:** `SESSION_END_CAPTURE_ENABLED`, `SESSION_END_MIN_MESSAGES`, `SESSION_END_RETENTION_DAYS`, `SESSION_END_SNIPPET_TURNS`
- **tools:** —
- **migrations:** —
- **bc:** `BC-HK1`
- **refs:** `yadgar/hooks/session-end-capture.py`, `yadgar/server/tools/project.py::_check_session_end_sentinel`
- **wiring:** Claude Code `SessionEnd` hook → `yadgar-session-end-capture.py` script (installed by `install_hooks`) → checks `SESSION_END_CAPTURE_ENABLED`; skips if `message_count < SESSION_END_MIN_MESSAGES`. On qualifying sessions, writes a JSON sentinel atomically to `~/.local/state/yadgar/session-ends/`. On next `project_brief(mode='signals')`, `_check_session_end_sentinel()` detects the unprocessed sentinel and returns an `extract_last_session_findings` recommended action pointing to the transcript path.
- **explanation:** Captures a lightweight sentinel at session end so the next session knows a transcript exists to mine. The sentinel records `ended_at`, `message_count`, `transcript_path`, the last N human turns (`SESSION_END_SNIPPET_TURNS`, default 5), and recently touched files. The `project_brief` signals mode surfaces an `extract_last_session_findings` action when an unprocessed sentinel is found, prompting the agent to read the transcript and call `memorize` with key findings. `SESSION_END_RETENTION_DAYS` (default 30) controls how long sentinel files are retained. BC-HK1: `install_hooks` writes the hook config idempotently.

### CAP-WIKI-017 — Directory + branch filter enforcement in recall and wiki_query
- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `wiki_query`
- **migrations:** —
- **bc:** `BC-B1`, `BC-B2`, `BC-B3`, `BC-B4`, `BC-B5`
- **refs:** `yadgar/server/tools/wiki.py::wiki_query`
- **wiring:** `wiki_query()` validates that `directory` is non-empty at the function boundary (raises `ValueError` if absent — BC-B3). Results are post-filtered via `is_directory_eligible(r.get("directory_context"), _dir_stripped)` from `yadgar/storage/directory.py`, which allows pages whose `directory_context` matches the caller directory or is `global` (BC-B1, BC-B2). The `§25` branch filter additionally prunes results to `{current_branch, default_branch, None}` and excludes results from unrelated branches. Entries tagged with `system` are handled by the storage/recall layer eligibility rules (BC-B4). Profile-sourced memories surface via the recall retrieval layer (BC-B5, exercised in the recall pipeline; wiki_query does not directly surface profiles but is part of the same retrieval surface).
- **explanation:** The directory and branch scoping contract (BC-B1 through BC-B5) governs how `wiki_query` (and `recall`) filter results to the calling project. BC-B1: results include the caller's directory and 'global' pages, excluding pages from other directories. BC-B2: the same directory filter applies to wiki results returned within the recall flow. BC-B3: `wiki_query` hard-raises `ValueError` when `directory` is absent or empty (v5.65 Fix D), making the caller supply real context. BC-B4: 'system'-tagged or system-directory entries are excluded from eligibility. BC-B5: profile-sourced memories surface in retrieval results when a profile exists for the queried context.

### CAP-WIKI-019 — Anchor promote-to-wiki signal
- **status:** LIVE
- **category:** wiki
- **settings:** `ANCHOR_PROMOTE_HEADERS`, `ANCHOR_PROMOTE_WORDS`
- **tools:** `project_brief`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/project.py::_fetch_anchor_promote_ids`, `yadgar/server/tools/project.py::_compute_anchor_signals`
- **wiring:** `project_brief(mode='signals')` → `_project_brief_signals()` → `_compute_anchor_signals()` → `_fetch_anchor_promote_ids()`. Queries all project anchors, filters by triple AND: `word_count > ANCHOR_PROMOTE_WORDS`, `header_count >= ANCHOR_PROMOTE_HEADERS`, and `tags ∩ {rule, pattern, convention, playbook, workflow, recipe} ≠ ∅`. Returns IDs of qualifying anchors, capped at 3 (`_SIGNALS_CANDIDATES_K`).
- **explanation:** Detects oversized, structured anchors that have grown rich enough to warrant promotion to wiki pages. The triple-AND filter ensures only anchors with substantial content (`ANCHOR_PROMOTE_WORDS` words, default 500), multiple markdown headers (`ANCHOR_PROMOTE_HEADERS`, default 2), AND playbook/pattern tags qualify. When candidates are found, a `promote_anchor_to_wiki` recommended_action appears in the `project_brief(mode='signals')` payload, nudging the agent to create a wiki page and replace the anchor with a reference.

### CAP-WIKI-020 — Native repo-wiki generation (T8, Option A)

- **status:** LIVE
- **category:** wiki
- **settings:** —
- **tools:** `repo_wiki_generate`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/repo_wiki/__init__.py`, `yadgar/repo_wiki/scanner.py`, `yadgar/repo_wiki/generator.py`, `yadgar/server/tools/repo_wiki.py`, `yadgar/cli/repo_wiki.py`
- **wiring:** MCP: `repo_wiki_generate(directory, ...)` → `scan_repo(directory)` → `generate_wiki_pages(records, directory_context)` → returns page dicts for caller to submit via `wiki_add`. CLI: `yadgar repo-wiki [REPO_PATH]` → same pipeline → submits via daemon REST `/hooks/wiki-generate`. Registered via `@_tool` in `yadgar/server/tools/repo_wiki.py`, imported into `yadgar/server/tools/__init__.py`. CLI registered in `yadgar/__main__.py` via `repo_wiki.register(subparsers)`. No Settings flag (pure read-only operation; no behavioral gating needed).
- **explanation:** Native Python AST-based code-structure wiki generator. Walks a repository (reusing `_should_skip_dir` from `seed._scan`), parses each `.py` file with `ast.parse`, extracts module docstrings, function/method signatures and docstrings, and class hierarchies. Emits one wiki page per module (per-module granularity, not per-function) with `directory_context` stamped to the repo root absolute path — fixing the prior `global` mis-stamp that affected 364 `fn-`/`mod-` pages written by the external `/repo-wiki:repo-wiki` skill. Option B (AST-graph via tree-sitter + leidenalg community detection) is a noted follow-on, not built here.

---

### CAP-OPS-001 — DLQ inspection and replay (dead-letter queue)
- **status:** LIVE
- **category:** ops
- **settings:** `QUEUE_DLQ_RETENTION_DAYS`, `QUEUE_MAX_PERMANENT_ATTEMPTS`, `QUEUE_MAX_TRANSIENT_ATTEMPTS`, `QUEUE_BACKOFF_BASE_S`, `QUEUE_BACKOFF_MAX_S`
- **tools:** `dlq_inspect`, `dlq_requeue`, `dlq_dismiss`
- **migrations:** —
- **bc:** `BC-ADM4`
- **refs:** `yadgar/server/tools/admin_dlq.py`, `yadgar/file_queue/dlq.py`
- **wiring:** MCP client → `dlq_inspect()` / `dlq_requeue()` / `dlq_dismiss()` registered via `@_tool` in `admin_dlq.py`, imported by shim `admin.py`. `dlq_inspect` reads `*.json.error.json` sidecars from `FileQueue.dlq_dir`. `dlq_requeue` moves a file from `dlq_dir/` back to `queue_dir/` atomically and resets the in-memory retry counter on `_queue_drainer`. `dlq_dismiss` deletes both the payload and its sidecar. All three are power-gated.
- **explanation:** Queue writes that exhaust all retry attempts (permanent errors after `QUEUE_MAX_PERMANENT_ATTEMPTS` tries, transient errors after `QUEUE_MAX_TRANSIENT_ATTEMPTS`) are moved to the dead-letter directory with a `.json.error.json` sidecar recording `failure_reason`, `attempts`, and `last_error`. `dlq_inspect` lists DLQ entries filterable by failure taxonomy (`all`, `rejections` for duplicate/policy/missing-branch entries, `failures` for permanent errors). `dlq_requeue` moves an entry back to the active queue so it is retried on the next drain pass; it blocks requeue of rejection-taxonomy entries unless `force=True` to prevent immediate re-rejection. `dlq_dismiss` permanently discards an entry after operator review.

### CAP-OPS-002 — File queue async write pipeline
- **status:** LIVE
- **category:** ops
- **settings:** `DATA_DIR`, `QUEUE_DRAIN_INTERVAL`, `QUEUE_BACKOFF_BASE_S`, `QUEUE_BACKOFF_MAX_S`, `QUEUE_DLQ_RETENTION_DAYS`, `QUEUE_MAX_PERMANENT_ATTEMPTS`, `QUEUE_MAX_TRANSIENT_ATTEMPTS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/file_queue/queue.py`, `yadgar/file_queue/apply.py`, `yadgar/file_queue/__init__.py`
- **wiring:** All MCP write tools (`memorize`, `wiki_add`, `checkpoint`, `anchor`, `update_active_work`) call `_get_file_queue().enqueue()` to write an atomic `.json` file under `DATA_DIR/queue/`. A background `QueueDrainer` thread polls every `QUEUE_DRAIN_INTERVAL` seconds, applies each operation via `apply.py` handlers, archives successes to `DATA_DIR/archive/`, and moves exhausted entries to `DATA_DIR/dlq/`.
- **explanation:** The file queue is the write-path backbone: MCP tools return immediately after writing a timestamped JSON payload to the filesystem (`queue_dir`), decoupling write latency from DB commit latency. The drainer thread processes entries in arrival order, applies exponential back-off (`QUEUE_BACKOFF_BASE_S` → `QUEUE_BACKOFF_MAX_S`) on transient failures, and promotes entries to the DLQ after `QUEUE_MAX_PERMANENT_ATTEMPTS` or `QUEUE_MAX_TRANSIENT_ATTEMPTS` exhaustion. `wait=True` callers in `wiki_add` can block on a per-job `threading.Event` for read-your-writes semantics.

### CAP-OPS-003 — Vacuum: threshold-driven auto-vacuum backstop
- **status:** LIVE
- **category:** ops
- **settings:** `VACUUM_AUTO_ENABLED`, `VACUUM_AUTO_THRESHOLD_BYTES`, `VACUUM_AUTO_WINDOW_START`, `VACUUM_AUTO_WINDOW_END`, `VACUUM_SNAPSHOT_RETENTION`
- **tools:** `vacuum_now`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_vacuum.py`, `yadgar/vacuum/phases.py`, `yadgar/vacuum/__init__.py`, `yadgar/ops.py`
- **wiring:** `vacuum_now()` MCP tool → `_fire_vacuum_service()` in `yadgar/ops.py` writes a trigger file at `YADGAR_VACUUM_TRIGGER_PATH`; a host-side systemd path-watch unit (`yadgar-vacuum.path`) picks it up and starts `yadgar-vacuum.service`. The threshold backstop path lives in `ConsolidationScheduler._maybe_auto_vacuum()`: when DB size exceeds `VACUUM_AUTO_THRESHOLD_BYTES` AND local time falls inside `[VACUUM_AUTO_WINDOW_START, VACUUM_AUTO_WINDOW_END)`, it fires the same trigger. The nightly cron (`yadgar-vacuum.timer`) is the primary trigger; this backstop fires only when growth outpaces the nightly schedule.
- **explanation:** Vacuum reclaims SurrealKV disk space by exporting, compacting, and atomically swapping in a new DB file. The MCP `vacuum_now()` tool writes a trigger file to decouple the vacuum from the daemon process (avoiding mid-swap crashes). The auto-backstop (`VACUUM_AUTO_ENABLED`) triggers only within the configured daily time window when the DB exceeds the size threshold, preventing unbounded growth between scheduled vacuum runs. `VACUUM_SNAPSHOT_RETENTION` controls how many pre-vacuum snapshots to keep.

### CAP-OPS-004 — Vacuum: atomic side-build and crash-mid-swap recovery
- **status:** LIVE
- **category:** ops
- **settings:** `SENSITIVE_LOCK_TTL_SEC`, `SENSITIVE_DRAIN_TIMEOUT_SEC`, `BACKEND_IMPORT_TIMEOUT_SEC`
- **tools:** —
- **migrations:** —
- **bc:** `BC-F1`, `BC-F2`, `BC-F3`
- **refs:** `yadgar/vacuum/phases.py::_atomic_swap`, `yadgar/vacuum/phases.py::_recover_interrupted_swap`, `yadgar/vacuum/phases.py::_vacuum_snapshot_and_drop`
- **wiring:** Called by `yadgar-vacuum.service` (not via MCP). `_vacuum_snapshot_and_drop` stops the real backend before copying the DB (quiesced snapshot), writes a `surreal_db.building-<ts>` side path, verifies row counts, then calls `_atomic_swap` (two same-directory `os.rename` calls). `_recover_interrupted_swap` runs at each vacuum start to detect and complete or roll back any crash-interrupted swap. The sensitive-job lock (`SENSITIVE_LOCK_TTL_SEC`) prevents SIGTERM from interrupting the swap window; `SENSITIVE_DRAIN_TIMEOUT_SEC` bounds how long the signal handler waits.
- **explanation:** The vacuum performs a stop-then-copy (P2 quiesce order, BC-F3) to avoid copying a torn live DB, then builds the compacted side DB and verifies exact row-count parity before the atomic swap. The two-rename swap (canonical→.old-ts, building→canonical) is atomic per-rename on POSIX; a crash between renames leaves canonical absent, which `_recover_interrupted_swap` detects at next vacuum start and resolves deterministically (promote verified `.new` if present, else roll back `.old`). The sensitive-lock mechanism ensures no SIGTERM can arrive mid-swap.

### CAP-OPS-005 — check_invariants: DB consistency audit and auto-repair
- **status:** LIVE
- **category:** ops
- **settings:** `CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS`, `DB_SIZE_WARNING_BYTES`, `DBSIZE_CACHE_TTL_SEC`
- **tools:** `check_invariants`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_invariants.py::_run_check_invariants`, `yadgar/server/tools/admin_invariants.py::check_invariants`
- **wiring:** MCP client → `check_invariants()` (power-gated, `@_tool(power=True)`) → `_run_check_invariants(storage)`. Also called from the nightly consolidation cycle via `ConsolidationScheduler`. Each per-table check runs with a `CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS` timeout; timed-out tables are skipped (logged at WARN) while the rest still run.
- **explanation:** Runs a suite of DB consistency checks: dangling `memory_similarity_link`, `memory_transition`, `relationship` (caused_by), `wiki_crossref`, orphan `memory:<N>` entity rows, row-count ceilings for `action_log`/`episode`/`wiki_page`, MSL ceiling (dynamic, based on memory count), `engram_slot` table integrity, and a DB-size telemetry pass. Fixable violations (dangling FKs with no information loss) are auto-repaired by DELETE and reported in the `fixed` list. Non-fixable issues (structural ceiling breaches, slot anomalies) appear in `violations`. `ok=True` only when `violations` and `timeouts` are both empty.

### CAP-OPS-006 — archive_purge: memory_archive retention enforcement
- **status:** LIVE
- **category:** ops
- **settings:** `MEMORY_ARCHIVE_RETENTION_DAYS`, `MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER`, `MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS`
- **tools:** `archive_purge`
- **migrations:** —
- **bc:** `BC-ADM5`
- **refs:** `yadgar/server/tools/admin_archive.py::archive_purge`, `yadgar/storage/ops.py::purge_expired_archives`
- **wiring:** MCP client → `archive_purge(dry_run, retention_days)` (power-gated, secret-gated) → `yadgar.storage.ops.purge_expired_archives(storage, dry_run, retention_days_override)`. Also triggered nightly by the consolidation scheduler.
- **explanation:** Purges `memory_archive` rows older than `MEMORY_ARCHIVE_RETENTION_DAYS` days. Protected rows (anchors, `is_protected=True`) and rows created more recently than `MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS` are always skipped. A circuit-breaker cap (`MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER`, default 500) limits the maximum rows deleted per call to prevent runaway deletes. `dry_run=True` (default) returns candidate count and a 10-ID sample without deleting; `dry_run=False` performs the actual purge.

### CAP-OPS-007 — vacuum_checkpoints: stale checkpoint collapse
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `vacuum_checkpoints`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_other.py::vacuum_checkpoints`, `yadgar/storage/ops.py::vacuum_checkpoints`
- **wiring:** MCP client → `vacuum_checkpoints(dry_run)` (power-gated, `@_tool(power=True)`) → `yadgar.storage.ops.vacuum_checkpoints(storage, dry_run)`. One-shot idempotent admin operation; not called automatically by the scheduler.
- **explanation:** Collapses stale checkpoint rows by keeping only the latest checkpoint per `directory_context`, deleting all older ones. The v5.6.5 per-directory scoping change created multiple rows per directory; this tool is the migration aid to collapse accumulated rows to one-per-directory. `dry_run=True` (default) reports stale count and survivor count without deleting. Returns `{stale_count, deleted, survivors, dry_run}`.

### CAP-OPS-008 — restore and checkpoint: hippocampal session replay
- **status:** LIVE
- **category:** ops
- **settings:** `CHECKPOINT_STALE_HOURS`, `CHECKPOINT_WARN_HOURS`, `MICRO_CHECKPOINT_ENABLED`, `MICRO_CHECKPOINT_COOLDOWN`
- **tools:** `checkpoint`, `restore`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/misc.py::checkpoint`, `yadgar/server/tools/misc.py::restore`, `yadgar/restoration.py`
- **wiring:** `checkpoint()` → `_get_file_queue().enqueue("checkpoint", payload)` (async path, normal sessions); sync path via `_get_replay().create_checkpoint()` during drain replay. `restore()` → `_get_replay().restore(directory)`. Micro-checkpoints fire automatically inside the consolidation cycle (when `MICRO_CHECKPOINT_ENABLED=True`) after every `MICRO_CHECKPOINT_COOLDOWN` tool calls.
- **explanation:** `checkpoint()` captures a structured snapshot of working state (current task, files being edited, key decisions, open questions, next steps, active errors, custom context) and enqueues it for persistent storage. Branch context is required (resolved from git or `branch_hint`). `restore()` reconstructs working context from the latest checkpoint plus anchored memories, thermodynamically hot project memories, and SR-map predicted context, returning a structured restoration report. `CHECKPOINT_STALE_HOURS` / `CHECKPOINT_WARN_HOURS` drive the signals-mode staleness watchdog that suggests periodic `checkpoint` calls.

### CAP-OPS-009 — update_active_work: atomic project active-work memory
- **status:** LIVE
- **category:** ops
- **settings:** `ACTIVE_WORK_STALE_HOURS`, `ACTIVE_WORK_WARN_HOURS`, `AUTO_REFRESH_ACTIVE_WORK`
- **tools:** `update_active_work`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/project.py::update_active_work`
- **wiring:** MCP client → `update_active_work(directory, content, branch_hint)` (power-gated) → `storage.upsert_active_work(resolved, content)` + `_register_active_work_directory(resolved)`. Branch is resolved from git or `branch_hint`; missing branch is a hard-reject. The watchdog (`AUTO_REFRESH_ACTIVE_WORK=True`) auto-writes a stub `_active_work` when staleness exceeds `ACTIVE_WORK_STALE_HOURS`.
- **explanation:** Replaces a directory's `_active_work` memory atomically — deletes all existing `_active_work` rows for the directory in a single transaction and inserts the new content. The `_active_work` memory is the canonical "what I'm currently doing" signal used by `project_brief()` signals mode, which emits `refresh_active_work` soft-action suggestions when the memory is older than `ACTIVE_WORK_WARN_HOURS`. `AUTO_REFRESH_ACTIVE_WORK` enables an optional watchdog that writes a stub entry on stale detection.

### CAP-OPS-010 — install_hooks: Claude Code hook installation
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `install_hooks`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/misc.py::install_hooks`, `yadgar/install_hooks_lib.py::install_hooks_impl`
- **wiring:** MCP client → `install_hooks(project_directory, scope)` (power-gated) → `install_hooks_impl(home_dir, scope, project_directory, dry_run=False)`. Refused when running inside a container (hostname-based detection). Reads/writes `~/.claude/settings.json` (global scope) or `.claude/settings.json` (project scope).
- **explanation:** Installs five Claude Code hook types: `PreCompact` (drain context before compaction), `SessionStart/compact` (restore context after compaction), `SessionStart/all` (inject project context on every new session), `PostToolUse` (capture tool actions into action_log), and `UserPromptSubmit` (auto-recall on every user turn). The `scope` parameter controls whether hooks write to the project-local or global settings. Container environments are rejected because the container filesystem is ephemeral and `$HOME` resolves to `/root` rather than the host user home.

### CAP-OPS-011 — reembed_all: bulk embedding backfill
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `reembed_all`
- **migrations:** —
- **bc:** `BC-ADM1`
- **refs:** `yadgar/server/tools/admin_other.py::reembed_all`
- **wiring:** MCP client → `reembed_all()` (power-gated) → `storage.get_memories_without_embeddings()` → batch encode via `embeddings.encode_batch()` → `storage.update_memory_embedding()` for each result. Runs synchronously; large corpora may take minutes.
- **explanation:** Generates embeddings for all memories that are missing them, typically after a bulk import. Queries the DB for rows with null embeddings, filters out null/empty content (which would cause the remote encode-batch endpoint to return all-None), then encodes in batches of 64. Each successful embedding is written back with the current model name. Returns `{reembedded, total_missing, model}` so operators can verify coverage.

### CAP-OPS-012 — get_rules / add_rule: neuro-symbolic rules engine
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `add_rule`, `get_rules`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_other.py::add_rule`, `yadgar/server/tools/admin_other.py::get_rules`
- **wiring:** MCP client → `add_rule()` (power-gated) / `get_rules()` (power-gated) → `_st._rules_engine.add_rule()` / `get_all_rules()` / `get_applicable_rules()`. `_rules_engine` is a `RulesEngine` singleton initialised during server lifecycle startup.
- **explanation:** `add_rule()` registers a neuro-symbolic rule on the in-memory `RulesEngine`: `hard` rules must be satisfied (filter action) or `soft` rules express preferences (boost or penalty). Scope can be global, per-directory, or per-file. `get_rules()` retrieves all active rules or those applicable to a given directory. Rules are applied during retrieval scoring to filter or re-rank memories according to operator-defined policies.

### CAP-OPS-013 — memory_stats: system statistics dashboard
- **status:** LIVE
- **category:** ops
- **settings:** `STATS_CACHE_TTL_S`, `CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_other.py::memory_stats`
- **wiring:** `memory_stats()` (non-power `@_tool()`) → `storage.get_memory_stats()` + engram slot statistics + rules count + episodic/semantic counts + SR dimensions + causal edge count + cognitive load limit + DB-size telemetry + Prometheus metrics snapshot (queue depth, drainer lag p95, recall p95, circuit-breaker states). Called directly by MCP clients; also surfaced at `/memory://stats` MCP resource endpoint.
- **explanation:** Returns a comprehensive system health snapshot: raw DB memory stats, write-gate rejection count, engram slot utilisation ratio, active rule count, episodic/semantic store counts, SR cognitive-map readiness, causal edge count, metacognition chunk limit, per-table row/byte counts, DB file size with warning flag, and a Prometheus metrics block. The metrics block (I8: backpressure must be observable via `memory_stats`) includes queue depth, p95 drainer lag, p95 recall duration, and per-endpoint circuit-breaker states.

### CAP-OPS-014 — Prometheus metrics endpoint
- **status:** LIVE
- **category:** observability
- **settings:** `METRICS_ENABLED`, `PHASE_DURATION_WARN_MS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/metrics.py`, `yadgar/observability/timing.py`
- **wiring:** `METRICS_ENABLED=True` (default) → `metrics.py` registers collectors in an isolated `CollectorRegistry`; the Starlette app mounts `/metrics` returning `generate_latest()`. `/metrics` is exempt from auth (always unauthenticated on loopback per §2 design). `yadgar/observability/timing.py` provides `@stage_timer`, `@request_timer`, `@labeled_timer` decorators that observe drain-stage and request-duration histograms. `PHASE_DURATION_WARN_MS` controls a CRITICAL log emitted when any consolidation phase exceeds the threshold.
- **explanation:** Exposes a Prometheus `/metrics` endpoint with collectors covering queue depth by queue type, request counts by route, consolidation phase duration histograms, embedding/CE cache hit/miss counters, action-batch size, per-tool token estimates, loop health gauges (last-run timestamp, error counters), archive retention counters, hook recall timeout counters, and cold-purge candidate gauge. The `observability/timing.py` decorators wrap drain stages and MCP handlers to populate the histograms without coupling source modules to prometheus_client (graceful no-op when not installed).

### CAP-OPS-015 — OTLP distributed tracing
- **status:** DORMANT
- **category:** observability
- **settings:** `OTLP_ENDPOINT`, `OTLP_HEADERS`, `OTLP_TIMEOUT_SEC`, `OTLP_INSECURE`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/tracing.py`
- **wiring:** `yadgar/tracing.py::configure_tracing()` called at daemon startup. When `OTLP_ENDPOINT` is non-empty, builds an `OTLPSpanExporter` (HTTP/proto) with the configured headers and timeout and wires a `BatchSpanProcessor` into a `TracerProvider`. DORMANT because `OTLP_ENDPOINT` defaults to `""` (empty string = disabled); the code path exists and is reachable but is a no-op unless the operator sets the endpoint.
- **explanation:** Optional OpenTelemetry distributed tracing exporter. When `OTLP_ENDPOINT` is set (e.g. `http://tempo:4318/v1/traces`), the daemon exports W3C TraceContext-propagated spans to the configured collector. `OTLP_HEADERS` passes comma-separated `k=v` authentication or tenant headers. `OTLP_INSECURE=True` (default) uses plain HTTP; set to `False` to enable TLS. `OTLP_TIMEOUT_SEC` (default 3 s) keeps a dead collector from blocking the export path.

### CAP-OPS-016 — Bearer-token authentication middleware
- **status:** LIVE
- **category:** security
- **settings:** `REQUIRE_AUTH`, `MCP_AUTH_TOKEN`, `ALLOWED_ORIGINS`, `DEBUG_APIS_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/auth_middleware.py::BearerAuthMiddleware`
- **wiring:** `BearerAuthMiddleware` wraps the Starlette ASGI app at server startup (unconditionally installed). On each HTTP/WebSocket request it checks: (1) exempt paths (`/health`, `/metrics`) pass through; (2) debug-API paths (`/api/control/config`, `/api/control/action/*`, `/api/control/restart/*`, `/api/logs/*`) require `DEBUG_APIS_ENABLED=True` or return 403; (3) protected prefixes (`/admin/`, `/api/`, `/hooks/`, `/mcp`) require `REQUIRE_AUTH=True` + valid `Authorization: Bearer <MCP_AUTH_TOKEN>` or return 401. Auth env vars are read per-request to enable live token rotation without restart.
- **explanation:** All API and hook routes are bearer-token protected when `REQUIRE_AUTH=True` (default). The token is compared with `hmac.compare_digest` to resist timing attacks. When `REQUIRE_AUTH=True` but `MCP_AUTH_TOKEN` is empty the server returns 503 (fail-secure rather than open). CORS `ALLOWED_ORIGINS` constrains which browser origins the HTTP transport accepts; defaults to loopback only. A separate `DEBUG_APIS_ENABLED` gate restricts powerful control-API paths even from authenticated callers.

### CAP-OPS-017 — Secret-gate allowlist and audit trail
- **status:** LIVE
- **category:** security
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/security/allowlist.py::is_allowlisted`, `yadgar/security/allowlist.py::_write_audit`
- **wiring:** `gate_or_reject()` in `yadgar/secrets.py` is called by every MCP write tool before any state mutation. It first calls `is_allowlisted(content, tags, source)` from this module; if matched, the write is permitted and an audit entry is appended to a JSONL file under `YADGAR_SECRET_GATE_AUDIT_DIR` (daily rotation). Non-matching content proceeds to pattern scanning.
- **explanation:** The allowlist enables structured bypass of the secret-gate pattern scanner for known-safe content (e.g. test fixtures containing token-shaped strings). Entries in `~/.config/yadgar/secret-gate-allowlist.yaml` specify required tags, glob-prefix patterns, and a human-readable reason. When a write call's content matches an entry's patterns and the call-site tags are a superset of the entry's required tags, the write is allowed through — but every bypass is logged to an immutable JSONL audit trail (date-based rotation) for later review. The allowlist is loaded lazily and thread-safely from disk on first use.

### CAP-OPS-018 — Update version-check mechanism
- **status:** DORMANT
- **category:** ops
- **settings:** `UPDATE_CHECK_ON_START`, `UPDATE_CHECK_TIMEOUT_SECONDS`, `UPDATE_PYPI_URL`, `UPDATE_USER_AGENT_TEMPLATE`, `UPDATE_DEBUG_APIS_ENABLED`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/update/check.py`, `yadgar/update/orchestrator.py`
- **wiring:** At daemon startup, when `UPDATE_CHECK_ON_START=True`, `probe_latest_version()` in `yadgar/update/check.py` issues an HTTP GET to `UPDATE_PYPI_URL` within `UPDATE_CHECK_TIMEOUT_SECONDS`. Result is surfaced in `/api/control/update` (gated by `UPDATE_DEBUG_APIS_ENABLED`). DORMANT because `UPDATE_CHECK_ON_START=False` by default (opt-in, privacy: avoid phone-home without explicit consent).
- **explanation:** Probes PyPI for the latest published yadgar version to support the upgrade flow. The check uses a configurable User-Agent (`UPDATE_USER_AGENT_TEMPLATE`, `{version}` replaced at runtime). `UPDATE_DEBUG_APIS_ENABLED` enables the `/api/control/update` HTTP endpoint for the Control-tab UI integration. The check is deliberately opt-in (`UPDATE_CHECK_ON_START=False`) to avoid unexpected outbound requests on air-gapped or privacy-sensitive deployments.

### CAP-OPS-019 — Upgrade orchestrator state machine
- **status:** DORMANT
- **category:** ops
- **settings:** `UPDATE_INSTALL_ENABLED`, `UPDATE_LOCK_MAX_AGE_SECONDS`, `UPDATE_SNAPSHOT_RETENTION`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/update/orchestrator.py::run_install`, `yadgar/update/snapshot.py`
- **wiring:** Called by the `yadgar update --install` CLI subcommand. `run_install()` checks `UPDATE_INSTALL_ENABLED`; if False, returns immediately with an error pointing to documentation. If enabled, executes the state machine: acquire lock → probe PyPI → snapshot prev state → pull container image → write env-file → graceful stop → restart service → health check → CLI upgrade → re-exec. DORMANT because `UPDATE_INSTALL_ENABLED=False` by default (opt-in safety gate).
- **explanation:** The upgrade orchestrator is a 10-state machine that coordinates a self-upgrade: file lock with PID stale-detection (`UPDATE_LOCK_MAX_AGE_SECONDS`) prevents concurrent upgrades, snapshots capture pre-upgrade state for rollback, and rollback fires automatically on failure at any state from `PULLING_IMAGE` through `HEALTH_CHECKING`. `UPDATE_SNAPSHOT_RETENTION` controls how many upgrade snapshots are retained. In production `os.execvp` replaces the process at `RE_EXECING` state; `DONE` is set by the subsequent `--finalize` subcommand.

### CAP-OPS-020 — Circuit breaker for ML backend endpoints
- **status:** LIVE
- **category:** ops
- **settings:** `CIRCUIT_BREAKER_ENABLED`, `CIRCUIT_BREAKER_FAILURE_THRESHOLD`, `CIRCUIT_BREAKER_OPEN_DURATION_SEC`, `CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC`, `CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC`, `CIRCUIT_BREAKER_BACKOFF_FACTOR`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/ml_client.py`
- **wiring:** `RemoteMLClient` in `yadgar/backend/ml_client.py` holds per-endpoint circuit breaker instances (`_cb_ce`, `_cb_nli`, `_cb_pair`). After `CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive failures the breaker opens. OPEN state blocks all requests to that endpoint for `CIRCUIT_BREAKER_OPEN_DURATION_SEC` seconds, then transitions to HALF_OPEN for a single probe. Failed probes extend the cooldown with exponential back-off (factor `CIRCUIT_BREAKER_BACKOFF_FACTOR`) up to `CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC`. States are surfaced in `memory_stats()`.
- **explanation:** Protects the daemon from cascading failure when the ML reranking backend (`/rerank/ce`, `/rerank/nli`, `/rerank/pair`) becomes unavailable or slow. The three-state machine (CLOSED → OPEN → HALF_OPEN) stops saturating a degraded backend and allows the retrieval pipeline to fall back to non-reranked results while the breaker is open. Exponential backoff with a cap prevents premature probe storms after repeated backend outages.

### CAP-OPS-021 — Model preload warm-up
- **status:** LIVE
- **category:** ops
- **settings:** `MODEL_PRELOAD`, `MODEL_PRELOAD_DELAY_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/`
- **wiring:** After daemon startup, a background thread waits `MODEL_PRELOAD_DELAY_SEC` seconds then sends a warm-up request to the ML backend to load rerank models into memory. Controlled by `MODEL_PRELOAD=True` (default). LIVE because the default is True and the daemon starts this thread unconditionally unless `MODEL_PRELOAD=False`.
- **explanation:** Triggers eager loading of reranking model weights (cross-encoder/GTE) in the ML backend process immediately after the daemon is healthy, rather than waiting for the first real rerank request. This amortises the cold-start latency (model loading can take 5-30 s on CPU) so the first user `recall()` call does not experience the full model-load delay. `MODEL_PRELOAD_DELAY_SEC` allows the backend to finish its own startup before the warm-up probe arrives.

### CAP-OPS-022 — CE and embedding LRU cache with snapshot persistence
- **status:** LIVE
- **category:** ops
- **settings:** `CE_CACHE_ENABLED`, `CE_CACHE_MAX_ENTRIES`, `EMBED_CACHE_ENABLED`, `EMBED_CACHE_MAX_ENTRIES`, `CACHE_SNAPSHOT_INTERVAL_SEC`, `CACHE_SNAPSHOT_DIR`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/backend/ml_client.py`
- **wiring:** `RemoteMLClient` maintains two LRU caches: CE score cache (keyed on query+passage pairs, `CE_CACHE_ENABLED`, max `CE_CACHE_MAX_ENTRIES`) and embedding vector cache (`EMBED_CACHE_ENABLED`, max `EMBED_CACHE_MAX_ENTRIES`). A background snapshotting thread persists both caches to `CACHE_SNAPSHOT_DIR/ce.snap` and `CACHE_SNAPSHOT_DIR/embed.snap` every `CACHE_SNAPSHOT_INTERVAL_SEC` seconds. Caches are loaded from snapshot on startup.
- **explanation:** LRU caches absorb repeated CE reranking and embedding requests for the same content, avoiding redundant GPU/CPU inference. The snapshot mechanism persists warm cache state across restarts, so the cache hit rate remains high even after a daemon restart. `CE_CACHE_MAX_ENTRIES` and `EMBED_CACHE_MAX_ENTRIES` bound memory consumption. Cache hit/miss counts are surfaced as Prometheus counters (`yadgar_embedding_cache_hits_total`, `yadgar_embedding_cache_misses_total`).

### CAP-OPS-023 — ASGI graceful shutdown and daemon lifecycle
- **status:** LIVE
- **category:** ops
- **settings:** `ASGI_SHUTDOWN_TIMEOUT_SEC`, `DAEMON_CHECK_INTERVAL`, `HOST`, `PORT`, `BACKEND_HTTP_TIMEOUT_SEC`, `BACKEND_IMPORT_TIMEOUT_SEC`, `BACKEND_LOG_LEVEL`, `CORE_LOG_LEVEL`, `LOG_FORMAT`
- **tools:** —
- **migrations:** —
- **bc:** `BC-F2`
- **refs:** `yadgar/server/_app.py`, `yadgar/daemon.py`, `yadgar/log_config.py`
- **wiring:** uvicorn serves the Starlette app at `HOST:PORT`. On SIGTERM, `ASGI_SHUTDOWN_TIMEOUT_SEC` caps the wait for in-flight requests to drain before abandoning them. `DAEMON_CHECK_INTERVAL` is the polling cadence for the daemon watchdog loop (health checks, consolidation trigger). `HOST`/`PORT` configure where the MCP HTTP server listens. `LOG_FORMAT` (`json`|`text`|`human`) and `CORE_LOG_LEVEL`/`BACKEND_LOG_LEVEL` configure structured logging.
- **explanation:** The daemon lifecycle: the Starlette ASGI app is started by uvicorn with configurable bind address and graceful-shutdown timeout. The daemon watchdog polls at `DAEMON_CHECK_INTERVAL` seconds to trigger consolidation when idle and to check backend health. Structured JSON logging (default `LOG_FORMAT=json`) feeds log aggregators; `text`/`human` modes are for local development. `BACKEND_HTTP_TIMEOUT_SEC` caps operational DB requests; `BACKEND_IMPORT_TIMEOUT_SEC` allows longer bulk-import operations.

### CAP-OPS-024 — Action stream and action log retention
- **status:** LIVE
- **category:** ops
- **settings:** `ACTION_STREAM_ENABLED`, `ACTION_STREAM_COLD_THRESHOLD`, `ACTION_STREAM_MAX_AGE_DAYS`, `ACTION_LOG_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/_state.py`, `yadgar/consolidation/`
- **wiring:** `ACTION_STREAM_ENABLED=True` (default) enables the sensory action buffer (`_buffer`), which captures tool actions via the `PostToolUse` hook into the `action_log` table. During consolidation, `_memify_prune` pass 5 deletes `_action_stream`-tagged memories older than `ACTION_STREAM_MAX_AGE_DAYS` days. `ACTION_LOG_RETENTION_DAYS` governs pruning of raw `action_log` rows each consolidation cycle. `ACTION_STREAM_COLD_THRESHOLD` gates archival of action-stream memories (they decay faster than normal memories).
- **explanation:** The action stream captures every tool call (via PostToolUse hook) into `action_log` for later consolidation into semantic memories. `ACTION_STREAM_ENABLED` is the master switch. Raw `action_log` rows older than `ACTION_LOG_RETENTION_DAYS` days are pruned each cycle to bound table size. Processed action-stream memories (tagged `_action_stream`) are subject to an age cap (`ACTION_STREAM_MAX_AGE_DAYS`) separate from heat-based decay because they start warm but become stale faster than user-authored memories. `ACTION_STREAM_COLD_THRESHOLD` (default 0.1, higher than the global 0.02) archives these memories sooner.

### CAP-OPS-025 — Auto-generated and auto-abstracted memory retention
- **status:** LIVE
- **category:** ops
- **settings:** `AUTO_GENERATED_MEMORY_MAX_AGE_DAYS`, `AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/consolidation/`
- **wiring:** `_memify_prune` in the consolidation cycle runs age-cap pruning passes for memories tagged `auto-generated` (pass 4) and `auto-abstracted` (pass 6). Both passes delete cold, unaccessed memories older than the respective threshold. Called unconditionally each consolidation cycle when `ACTION_STREAM_ENABLED=True`.
- **explanation:** System-generated memories (CLS semantic promotions, action-stream pattern summaries, narrative digests) accumulate rapidly and would exhaust the DB if not capped. `AUTO_GENERATED_MEMORY_MAX_AGE_DAYS` (default 30) and `AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS` (default 30) impose age limits on these low-stakes auto-created rows. Only unaccessed, cold rows are pruned — a memory that has been retrieved or boosted survives beyond the age cap. Setting either to 0 disables the respective prune pass.

### CAP-OPS-026 — Auto-capture rate limiting
- **status:** LIVE
- **category:** ops
- **settings:** `AUTO_CAPTURE_RATE_LIMIT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/hooks/`
- **wiring:** The `PostToolUse` hook handler checks a per-directory rate-limit counter before enqueuing an auto-capture write. When the counter exceeds `AUTO_CAPTURE_RATE_LIMIT` requests per directory key per minute, the capture is suppressed. LIVE because the hook fires unconditionally and the rate-limit check is always evaluated.
- **explanation:** Prevents runaway auto-capture from flooding the write queue when a project is generating very high tool-call volume (e.g. CI test runs, large bulk operations). The rate limit is per `directory_context` key, so a noisy directory does not block captures from other directories. Default 30 requests/minute/directory is generous for interactive development while bounding worst-case queue growth.

### CAP-OPS-027 — DB size warning telemetry
- **status:** LIVE
- **category:** observability
- **settings:** `DB_SIZE_WARNING_BYTES`, `DBSIZE_CACHE_TTL_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_invariants.py::_check_db_size`, `yadgar/storage/`
- **wiring:** `_check_db_size()` is called by `_run_check_invariants()` each time the tool is invoked or the scheduler runs the nightly invariant check. When `db_size_bytes > DB_SIZE_WARNING_BYTES`, a CRITICAL log is emitted — throttled to at most once per hour via `_st._db_size_warn_last_logged_hour`. `/admin/dbsize` HTTP endpoint results are cached for `DBSIZE_CACHE_TTL_SEC` seconds.
- **explanation:** Monitors total SurrealKV storage directory size (vlog + sstables + WAL) against `DB_SIZE_WARNING_BYTES` (default 1 GiB). When breached, a once-per-hour CRITICAL log alerts the operator to consider vacuuming. The size breakdown (`vlog_size_bytes`, `sstables_size_bytes`, `wal_size_bytes`) is included in `check_invariants` and `memory_stats` responses. The `/admin/dbsize` endpoint result is cached (`DBSIZE_CACHE_TTL_SEC`, default 60 s) to avoid repeated filesystem stats on every request.

### CAP-OPS-028 — Anchor audit hygiene pass
- **status:** LIVE
- **category:** ops
- **settings:** `ANCHOR_AUDIT_THRESHOLD`, `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`, `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN`, `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/audit.py::_run_anchor_audit_pass`, `yadgar/server/tools/admin_other.py::consolidate_now`
- **wiring:** Called from `consolidate_now(mode='full')` when `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=True` (default). Iterates over known project directories, calls `audit_anchors()` for each, collects expired/redundant/promotable anchors, and returns a summary. Also triggered from `project_brief()` signals mode when anchor count exceeds `ANCHOR_AUDIT_THRESHOLD`.
- **explanation:** Automatically audits anchor hygiene during full consolidation cycles: identifies expired anchors (past `valid_until`), cosine-similar redundant pairs (merge candidates), and oversized anchors suitable for wiki promotion. `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` caps the total actions returned per run to respect token budgets. `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS` controls how long audit snapshots are retained. Results feed the `audit_anchors` recommended action in signals mode.

### CAP-OPS-029 — Sensitive-job lock (vacuum shutdown guard)
- **status:** LIVE
- **category:** ops
- **settings:** `SENSITIVE_LOCK_TTL_SEC`, `SENSITIVE_DRAIN_TIMEOUT_SEC`
- **tools:** —
- **migrations:** —
- **bc:** `BC-F3`
- **refs:** `yadgar/vacuum/__init__.py`, `yadgar/ops.py`
- **wiring:** The vacuum service acquires a sensitive-job lock file under `YADGAR_DATA_DIR` before beginning the stop-then-copy-then-swap sequence. The daemon's SIGTERM signal handler checks for the lock and refuses shutdown (returns without shutting down) if the lock is held and the holder PID is alive, waiting up to `SENSITIVE_DRAIN_TIMEOUT_SEC` for the job to finish. A lock older than `SENSITIVE_LOCK_TTL_SEC` (2 h) is treated as stale (crashed job) and reaped.
- **explanation:** Prevents a SIGTERM from arriving mid-swap and leaving the database in a partially swapped state (the BC-F3 hazard). The lock is a lightweight file-based mutex: the vacuum job writes its PID and start timestamp; the shutdown handler reads the lock and either waits for the job to release it or skips shutdown if the drain timeout is exceeded. The TTL prevents a crashed vacuum from permanently blocking all future shutdowns.

### CAP-OPS-030 — Stats and stale-count caches
- **status:** LIVE
- **category:** ops
- **settings:** `STATS_CACHE_TTL_S`, `STALE_COUNT_CACHE_TTL_S`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/_app.py`, `yadgar/server/tools/project.py`
- **wiring:** `/api/stats` response is cached in-process for `STATS_CACHE_TTL_S` seconds (default 5 s). The stale wiki count used in signals mode is cached per resolved directory for `STALE_COUNT_CACHE_TTL_S` seconds (default 300 s, module-level dict in `project.py`). Both caches are invalidated on demand or by TTL expiry.
- **explanation:** Short TTL caches bound the cost of `/api/stats` calls (which enumerate DB metrics) and the stale-wiki-count scan (which walks the archive directory). Without caching, burst traffic from the Control-tab UI or repeated `project_brief()` signals calls would repeatedly recompute the same values. `STATS_CACHE_TTL_S=0` disables the stats cache; `STALE_COUNT_CACHE_TTL_S=0` disables the stale-count cache.

### CAP-OPS-031 — Hook recall timeout budget
- **status:** LIVE
- **category:** ops
- **settings:** `HOOK_RECALL_TIMEOUT_S`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/hooks/`
- **wiring:** The `UserPromptSubmit` hook handler wraps `recall()` in `asyncio.wait_for(recall(...), timeout=HOOK_RECALL_TIMEOUT_S)`. On timeout: logs WARN, increments `yadgar_hook_recall_timeout_total{handler=<name>}` counter, and returns empty results to avoid blocking the user prompt. LIVE because hooks are always installed in production.
- **explanation:** Bounds the latency budget for auto-recall inside hook handlers. Hook recall runs on every user prompt; if the recall path is slow (cold cache, high load), it must not block the user interaction indefinitely. `HOOK_RECALL_TIMEOUT_S` (default 2.0 s) is the hard deadline. Timeouts are observable via the `yadgar_hook_recall_timeout_total` metric, so operators can raise the budget if the timeout rate is too high or lower it to protect latency more aggressively.

### CAP-OPS-032 — query-routing code and relational keyword lists
- **status:** LIVE
- **category:** ops
- **settings:** `CODE_KEYWORDS`, `RELATIONAL_KEYWORDS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/retrieval/query_analysis.py`
- **wiring:** `CODE_KEYWORDS` and `RELATIONAL_KEYWORDS` are comma-separated keyword lists loaded via `Settings` and used by the query router (`query_analysis.py`) to classify an incoming `recall()` query into one of: code, relational, temporal, open-domain, or comparison profile. Classification drives retrieval profile selection and signal weighting.
- **explanation:** The query router tokenises the query and checks for membership in the configured keyword lists to determine query type. Code queries (containing function/class/API terms) and relational queries (containing relationship/causal terms) route to specialised retrieval sub-pipelines optimised for their patterns. The lists are configurable so operators can tune routing precision for domain-specific vocabularies without changing code.

### CAP-OPS-033 — BC-A1/A2/A3: memorize write guarantees
- **status:** LIVE
- **category:** write-path
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-A1`, `BC-A2`, `BC-A3`
- **refs:** `yadgar/server/tools/memorize.py`, `yadgar/file_queue/apply.py`
- **wiring:** `memorize()` → file-queue enqueue → drainer apply → DB write. BC-A1: the drainer stamps `directory_context` from the enqueued payload, making the memory retrievable by directory. BC-A2: the write-gate evaluates novelty; near-identical content is deduplicated. BC-A3: the drainer calls the embedding service for every committed memory; on failure, the row is stored with null embedding (unless `WIKI_EMBED_FAILURE_BLOCKS_WRITE=True`).
- **explanation:** These three behaviour-contract rows describe the three core memorize guarantees. BC-A1 ensures directory-stamped retrievability: `memorize(content, context=D)` always stores with `directory_context=D`. BC-A2 ensures deduplication: the write-gate rejects near-identical content (high similarity to existing memories). BC-A3 ensures embedding coverage: every committed memory has an embedding generated at write time, enabling similarity search during consolidation and retrieval.

### CAP-OPS-034 — forget and validate_memory: individual memory lifecycle ops
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-ADM2`, `BC-ADM3`
- **refs:** `yadgar/server/tools/admin_other.py::forget`, `yadgar/server/tools/admin_other.py::validate_memory`
- **wiring:** `forget(memory_id)` is a non-power `@_tool()` → `storage.delete_memory(memory_id)` directly (synchronous, not queued). `validate_memory(memory_id)` (power-gated) → `_st._staleness.validate_memory(memory_id)` if staleness detector is active, else falls back to `_file_hash(directory_context)` comparison. Both are called directly by MCP clients.
- **explanation:** `forget()` permanently deletes a memory record by ID: loads it to confirm existence, then calls `storage.delete_memory()` returning `{memory_id, status: "deleted"}` or `"not_found"` (BC-ADM2). `validate_memory()` checks whether a file-backed memory's stored hash still matches the file on disk: if the file is gone the memory is marked stale; if the hash differs it is marked stale; if it matches the memory is valid. This catches the fallback bug where file-backed memories accumulated without staleness detection (BC-ADM3).

### CAP-OPS-035 — memory_update: field-level memory patching
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-ADM6`
- **refs:** `yadgar/server/tools/admin_other.py::memory_update`
- **wiring:** MCP client → `memory_update(memory_id, fields)` (power-gated) → validates `fields` against `_MEMORY_UPDATE_ALLOWED = {"content", "tags", "is_protected", "is_stale"}` → `_st._storage.update_memory_fields(memory_id, **fields)` → re-reads and returns updated record with embedding bytes stripped.
- **explanation:** Provides surgical patching of individual memory fields without a full delete-and-recreate cycle (BC-ADM6). Allowed fields are `content`, `tags`, `is_protected`, and `is_stale`; structural fields (`heat`, `embedding`, `id`, `created_at`) and unknown keys are rejected with a `ValueError` listing the allowed set. Empty `fields` is a no-op read (returns current state). Embedding bytes are stripped from the response to keep MCP payloads small.

### CAP-OPS-036 — Migration HTTP timeout
- **status:** LIVE
- **category:** ops
- **settings:** `MIGRATION_HTTP_TIMEOUT_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/storage/__init__.py`
- **wiring:** `StorageEngine.__init__()` applies schema migrations by issuing HTTP requests to the SurrealDB backend. Each migration request uses `MIGRATION_HTTP_TIMEOUT_SEC` (default 30 s) as the httpx timeout, distinct from the operational `BACKEND_HTTP_TIMEOUT_SEC` (5 s). This is set once at startup during `StorageEngine` initialisation.
- **explanation:** Schema migrations (DDL statements, backfill queries) can take significantly longer than operational read/write queries due to lock contention and large-table scans. `MIGRATION_HTTP_TIMEOUT_SEC` (default 30 s) gives migrations a longer deadline than the operational 5 s cap, preventing spurious migration failures on large databases or loaded backends without also allowing operational queries to time out slowly.

### CAP-OPS-037 — recent_memories: time-ranked memory surface without classifier
- **status:** LIVE
- **category:** ops
- **settings:** —
- **tools:** `recent_memories`
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/tools/admin_other.py::recent_memories`, `yadgar/storage/memory.py::get_recent_memories_since`
- **wiring:** MCP client → `recent_memories(limit, since, directory)` → `_parse_since_duration(since)` converts duration string or ISO cutoff → `storage.get_recent_memories_since(since, limit, directory)` → returns rows ordered by `created_at DESC`. `limit` is capped at 100; `since` accepts `'24h'`, `'7d'`, `'30m'` duration strings or ISO-8601 UTC datetime; `directory='global'` or empty queries all directories. Content truncated to 300 chars per entry. Also feeds `_project_brief_restore()` via `_build_recent_writes()` to populate the `recent_writes` section of `restore` output.
- **explanation:** Surfaces recently stored memories ordered by creation time without invoking the embedding/classifier pipeline. Useful after context compaction (when `restore` has already fired) to inspect what was written in the last N hours. The `restore` tool's output now includes a `recent_writes` section (last 10 memories in last 24h) built from this storage method, helping agents reconstruct work that was stored just before compaction. `memorize()` responses also now include an explicit `memory_id` field alongside the full memory dict for stable programmatic access.

### CAP-VIZ-001 — Wiki node category colors

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_CAT_COLOR_ANALYSIS`, `VIZ_CAT_COLOR_ARCHITECTURE`, `VIZ_CAT_COLOR_CONVENTION`, `VIZ_CAT_COLOR_DEBUGGING`, `VIZ_CAT_COLOR_DECISION`, `VIZ_CAT_COLOR_FACT`, `VIZ_CAT_COLOR_PATTERN`, `VIZ_CAT_COLOR_REFERENCE`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/viz_meta.py::build_category_colors`, `yadgar/server/http.py::api_viz_config`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → `build_category_colors(settings)` iterates `WikiStore.CATEGORIES`, calls `getattr(settings, f"VIZ_CAT_COLOR_{cat.upper()}", fallback)` for each. Result returned under `node.category_colors` in the JSON response. Frontend fetches on init and assigns to `YADGAR_VIZ_CONFIG.node.category_colors`; wiki nodes are colored by category at render time.
- **explanation:** Eight settings, one per wiki category (analysis, architecture, convention, debugging, decision, fact, pattern, reference), define the hex color used to fill wiki nodes in the force graph. `build_category_colors` builds the map dynamically by iterating the canonical `WikiStore.CATEGORIES` set rather than a hardcoded list, so new categories get an automatic grey fallback (`#8b949e`) without code changes. Colors flow to the frontend via `GET /api/viz/config` and are applied at render time in the 2D canvas and 3D ForceGraph renderers.

### CAP-VIZ-002 — Edge colors

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_EDGE_COLOR_MEMORY_WIKI`, `VIZ_EDGE_COLOR_SEMANTIC`, `VIZ_EDGE_COLOR_TEMPORAL`, `VIZ_EDGE_COLOR_TRANSITION`, `VIZ_EDGE_COLOR_WIKI_CROSSREF`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/viz_meta.py::build_edge_colors`, `yadgar/viz_meta.py::EDGE_TYPES`, `yadgar/server/http.py::api_viz_config`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → `build_edge_colors(settings)` iterates `EDGE_TYPES`; for entries with a `settings_color_key` it reads the matching `Settings` attribute, otherwise uses the fallback color. Result returned under `edge.color` in the config JSON. Frontend stores in `YADGAR_VIZ_CONFIG.edge.color` and applies per edge type at render time.
- **explanation:** Five settings control the display color (hex) for the five named edge types whose color is configurable: `memory_wiki` (memory→wiki provenance), `semantic` (cosine-similarity), `temporal` (engram slot co-membership), `transition` (co-recall pattern), and `wiki_crossref` (explicit wiki page links). Edge types without a `settings_color_key` (causal, co_occurrence, imports, calls, resolved_by, caused_by) use hardcoded fallback colors defined in `EDGE_TYPES`. All five configurable colors are served via `GET /api/viz/config` and consumed by the ForceGraph2D/3D link-color accessor in `index.html`.

### CAP-VIZ-003 — Edge styling (opacity, arrow length, 3D width multiplier, variant)

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_EDGE_ARROW_LEN`, `VIZ_EDGE_OPACITY`, `VIZ_EDGE_WIDTH_3D_MULTIPLIER`, `VIZ_EDGE_VARIANT`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/http.py::api_viz_config`, `yadgar/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → reads `s.VIZ_EDGE_ARROW_LEN`, `s.VIZ_EDGE_OPACITY`, `s.VIZ_EDGE_WIDTH_3D_MULTIPLIER`, `s.VIZ_EDGE_VARIANT` → returned under `edge.*`. Frontend assigns to `YADGAR_VIZ_CONFIG.edge` and applies: `linkOpacity(opacity)`, `linkWidth(l => _linkWidth(l) * width_3d_multiplier)` (3D only), `arrowLen` via `_arrowLen(l)` (transition/memory_wiki/wiki_crossref edges only), and `variant` is informational (logged in the debug info tab).
- **explanation:** Four settings govern overall edge rendering style. `VIZ_EDGE_OPACITY` sets the global link opacity for all edges (default 0.9, Variant C). `VIZ_EDGE_WIDTH_3D_MULTIPLIER` scales the computed link width in 3D mode only. `VIZ_EDGE_ARROW_LEN` sets the directional-arrow length for transition, memory_wiki, and wiki_crossref edge types (others use zero). `VIZ_EDGE_VARIANT` is a string label ("C") for the current edge styling scheme; it appears in the debug info tab but has no functional effect on rendering.

### CAP-VIZ-004 — Heat-to-HSL color mapping for memory/entity nodes

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_HEAT_HUE_END`, `VIZ_HEAT_HUE_START`, `VIZ_HEAT_LIGHT_BASE`, `VIZ_HEAT_LIGHT_GAIN`, `VIZ_HEAT_SAT_BASE`, `VIZ_HEAT_SAT_GAIN`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/http.py::api_viz_config`, `yadgar/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns all six heat parameters under `node.heat`. Frontend receives them into `YADGAR_VIZ_CONFIG.node.heat`; the `heatToHsl(h)` function (line ~1530 of `index.html`) computes `hsl((1-h)*hue_start + h*hue_end, sat_base + h*sat_gain%, light_base + h*light_gain%)` for every memory/entity node at render time.
- **explanation:** Six settings parameterise the continuous heat-to-colour gradient applied to memory and entity nodes. `VIZ_HEAT_HUE_START` (default 240 = blue) and `VIZ_HEAT_HUE_END` (default 0 = red) define the hue extremes for cold (h=0) and hot (h=1) nodes. `VIZ_HEAT_SAT_BASE`/`VIZ_HEAT_SAT_GAIN` and `VIZ_HEAT_LIGHT_BASE`/`VIZ_HEAT_LIGHT_GAIN` control the HSL saturation and lightness as linear functions of heat. Wiki nodes are unaffected (they use category colors).

### CAP-VIZ-005 — Node sizing (2D and 3D)

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_NODE_SIZE_2D`, `VIZ_NODE_SIZE_3D`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/http.py::api_viz_config`, `yadgar/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `size_3d` and `size_2d` under `node`. Frontend assigns: `graph.nodeRelSize(YADGAR_VIZ_CONFIG.node.size_3d)` for ForceGraph3D and uses `size_2d` as the base radius in the 2D canvas draw callback.
- **explanation:** Two settings control node sphere size: `VIZ_NODE_SIZE_3D` (default 8.0) sets `nodeRelSize` in the 3D ForceGraph renderer (2× the ForceGraph3D library default of 4), and `VIZ_NODE_SIZE_2D` (default 4.0) sets the base canvas draw radius in the 2D renderer. Both are served via `GET /api/viz/config` and consumed during graph initialisation in `index.html`.

### CAP-VIZ-006 — Force-directed physics parameters

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_PHYSICS_CHARGE_STRENGTH`, `VIZ_PHYSICS_LINK_DISTANCE_2D`, `VIZ_PHYSICS_LINK_DISTANCE_3D`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/http.py::api_viz_config`, `yadgar/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `charge_strength`, `link_distance_2d`, `link_distance_3d` under `physics`. Frontend applies in graph init: `graph.d3Force('charge').strength(charge_strength)` (2D) and `graph.d3Force('link').distance(link_distance_2d)` / `link_distance_3d` (mode-dependent).
- **explanation:** Three settings tune the d3-force simulation. `VIZ_PHYSICS_CHARGE_STRENGTH` (default −18.0) controls the many-body repulsion strength between nodes. `VIZ_PHYSICS_LINK_DISTANCE_2D` (default 30.0) and `VIZ_PHYSICS_LINK_DISTANCE_3D` (default 36.0) set the natural link rest-length in each render mode. These are applied once when the graph initialises; changing them requires a page reload or graph reinit.

### CAP-VIZ-007 — Auto-zoom-fit layout settings

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_LAYOUT_ZOOM_FIT_PADDING`, `VIZ_LAYOUT_ZOOM_FIT_TICK`, `VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/server/http.py::api_viz_config`, `yadgar/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `auto_zoom_fit_tick_threshold`, `zoom_fit_padding`, `zoom_fit_transition_ms` under `layout`. In `index.html` the `onEngineTickCallback` checks `_engineTickCount === auto_zoom_fit_tick_threshold`; when matched calls `graph.zoomToFit(zoom_fit_transition_ms, zoom_fit_padding)` once.
- **explanation:** Three settings control the automatic zoom-to-fit behaviour triggered after the force simulation settles. `VIZ_LAYOUT_ZOOM_FIT_TICK` (default 80) is the engine-tick count at which auto-zoom fires. `VIZ_LAYOUT_ZOOM_FIT_PADDING` (default 50 px) is passed to ForceGraph's `zoomToFit()` as the padding inset. `VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS` (default 800 ms) is the animation duration for the zoom transition. The auto-zoom fires exactly once per graph load.

### CAP-VIZ-008 — Search highlight colors and dim opacity

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_SEARCH_DIM_OPACITY`, `VIZ_SEARCH_MATCH_COLOR`, `VIZ_SEARCH_PINNED_COLOR`
- **tools:** —
- **migrations:** —
- **bc:** `BC-VZ2`
- **refs:** `yadgar/server/http.py::api_viz_search`, `yadgar/server/http.py::api_viz_config`, `yadgar/static/index.html`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `match_color`, `pinned_color`, `dim_opacity` under `search`. Frontend stores in `YADGAR_VIZ_CONFIG.search`; 2D canvas draw applies `dim_opacity` to `__dimmed` nodes and uses `pinned_color` / `match_color` as stroke for search-matched and clicked-to-pin nodes. `GET /api/viz/search?q=<query>` → `api_viz_search` → dispatches `recall()` + wiki `query()` (both whole-DB, no directory= param) → returns matching node IDs from ALL projects; frontend marks them for highlight/dim.
- **explanation:** Three settings control the visual feedback of the in-graph search feature. `VIZ_SEARCH_MATCH_COLOR` (default `#ffffff`) is the stroke color for nodes returned by a search query. `VIZ_SEARCH_PINNED_COLOR` (default `#ffd700` gold) is the stroke color for nodes manually pinned by clicking. `VIZ_SEARCH_DIM_OPACITY` (default 0.18) sets the opacity for all non-matching nodes when a search is active, creating a dimming/highlight effect. The search endpoint `GET /api/viz/search` dispatches retrieval recall and wiki query with no directory scoping — this is INTENTIONAL: the viz is a god's-eye admin overlay (localhost, auth-gated) rendering every project's nodes in one graph; search-highlight must find any visible node regardless of project directory. The directory-scoping bypass is documented in the code (see `api_viz_search`, http.py, BC-VZ2 comment) and locked by e2e test BC-VZ2.

### CAP-VIZ-009 — Health refresh interval (daemon health scraper)

- **status:** LIVE
- **category:** viz
- **settings:** `VIZ_HEALTH_REFRESH_SEC`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/viz_daemon_health.py::run_health_scraper`, `yadgar/config.py`
- **wiring:** Server lifespan starts `run_health_scraper()` as a background asyncio task. Each iteration: scrape → sleep `get_settings().VIZ_HEALTH_REFRESH_SEC`. Setting is re-read each iteration so live env changes take effect without restart. `GET /api/daemon-health` returns the cached scrape result.
- **explanation:** `VIZ_HEALTH_REFRESH_SEC` (default 5.0 s) controls how often the server-side daemon health scraper polls the backend's `/metrics` endpoint and updates the in-process `_health_cache`. The cache is served via `GET /api/daemon-health`, which the viz debug tab reads for real-time process metrics (RSS, CPU, queue depth, circuit-breaker state). The scraper re-reads this setting on every cycle so the cadence can be changed via env var without restarting the server.

### CAP-VIZ-010 — Wiki node shape setting

- **status:** DORMANT
- **category:** viz
- **settings:** `VIZ_WIKI_SHAPE`
- **tools:** —
- **migrations:** —
- **bc:** —
- **refs:** `yadgar/config.py`, `yadgar/server/http.py::api_viz_config`, `yadgar/static/index.html`, `yadgar/static/graph-node-factory.js`
- **wiring:** `GET /api/viz/config` → `api_viz_config` → returns `s.VIZ_WIKI_SHAPE` under `node.wiki_shape`. Frontend receives it into `YADGAR_VIZ_CONFIG.node.wiki_shape`. The 3D node renderer in `index.html` reads it at line ~1563 and `graph-node-factory.js` line 27–28: `if (node.type !== 'wiki' || shape !== 'octahedron') return null`. When the setting is `'octahedron'` (default) the octahedron mesh IS created; when set to anything else the mesh factory returns null, falling back to a sphere. The mesh factory is wired; the capability is not disabled by a flag flip, but the config comment (`renderer not wired pending v5.10.7.3 resolution`) indicates this is under active review. The default value `'octahedron'` does exercise the octahedron path, so technically `LIVE`, but the inline comment marks it as configuration-only pending a design decision. Status is `DORMANT` — the setting is served and partially consumed, but the v5.10.7.3 plan may revert the renderer; treat as transitional.
- **explanation:** `VIZ_WIKI_SHAPE` (default `'octahedron'`) configures the desired 3D mesh shape for wiki nodes. When set to `'octahedron'`, the `graph-node-factory.js` module creates a Three.js octahedron geometry for wiki nodes in 3D view. When set to any other value the factory returns null and ForceGraph3D renders the default sphere. The setting is served via `GET /api/viz/config` and applied at graph init; the v5.10.7.3 plan tracks a potential revert of the custom mesh renderer to defaults.

### CAP-VIZ-011 — Graph REST endpoints (full graph, neighborhood, lazy edges)

- **status:** LIVE
- **category:** viz
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-VZ1`
- **refs:** `yadgar/graph_api.py::GraphAPI`, `yadgar/server/http.py::api_graph`, `yadgar/server/http.py::api_graph_neighborhood`, `yadgar/server/http.py::api_graph_edges_lazy`
- **wiring:** Three HTTP routes on the daemon: `GET /api/graph` → `api_graph` → `GraphAPI.get_full_graph()` assembles memory + wiki + entity nodes with all typed edges (temporal, transition, wiki_crossref, memory_wiki, causal, entity-typed-relations); `GET /api/graph/neighborhood/{node_id}` → `api_graph_neighborhood` → `GraphAPI.get_neighborhood()` returns the 1–2 hop subgraph around a node (partial implementation — currently returns nodes only, edges=[]) satisfying BC-VZ1; `GET /api/graph/edges?type=semantic` → `api_graph_edges_lazy` → `GraphAPI.get_edges_by_type()` computes O(n²) KNN semantic edges on demand. All routes are reachable with default config.
- **explanation:** `GraphAPI` is the server-side assembly layer that builds the knowledge graph JSON for the visualization frontend. `get_full_graph` fetches memory nodes (heat-ranked, up to 500), wiki nodes, entity nodes, and assembles all edge types from the storage engine; orphan edges (where an endpoint node is absent) are filtered and counted. `get_neighborhood` provides a node-centric subgraph view for the BC-VZ1 "entity neighborhood + scores" contract (currently returns a memory node and its immediate neighbourhood). `get_edges_by_type` handles the lazy semantic edge path — expensive pairwise cosine-similarity KNN computed only when the frontend toggle enables it.

### CAP-INFRA-001 — Request-path thinness + async threading invariants

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I1`, `BC-I2`, `BC-I3`, `BC-I4`, `BC-I6`, `BC-I9`
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`
- **wiring:** Structural architecture guarantee — the FastAPI request handler in `yadgar/server/http.py` offloads all ML and heavy compute to the drainer thread (via `asyncio.to_thread` or the drainer queue) rather than blocking the event loop. The drainer is a single background lane; no second processor competes. Opt-in features (e.g. enrichment, dream replay) test their feature flag before initializing heavy models. Embedding and rerank are cached within a single request to prevent double-pay. Write-path latency is kept within the ≤5 ms p50 budget. All of these are runtime invariants enforced by design and validated by unit tests or real-path coverage, not a dedicated CI script.
- **explanation:** These six invariants collectively define the request/async threading model: the event-loop thread never blocks on ML compute (I1, I4), the drainer is the one and only catch-up worker (I2), disabled features bail out before expensive init (I3), embeddings and rerankers are not computed twice per request (I6), and new write-path code stays within a 5 ms p50 latency budget (I9). Together they prevent event-loop stalls, competing background processors, and accidental performance regressions on the hot write path.

---

### CAP-INFRA-002 — Queue durability boundary

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I7`
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`, `yadgar/metrics.py`
- **wiring:** Queue depth and backpressure state are exposed as Prometheus metrics in `yadgar/metrics.py`. The drainer writes to these metrics on each cycle. Observable via the `/metrics` endpoint at runtime.
- **explanation:** I8 requires that queue backpressure is externally visible so operators can detect write-queue buildup before it causes latency spikes or data loss. The queue depth gauge and any backpressure flag are registered Prometheus metrics written by the drainer. This is both a runtime invariant and a metric-writer requirement (linked with I23).

---

### CAP-INFRA-005 — Config overrides explicit

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I10`, `BC-I12`, `BC-I27`
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `scripts/check_image_size.py`, `docs/ARCHITECTURE_INVARIANTS.md`
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
- **refs:** `scripts/check_complexity.py`, `docs/ARCHITECTURE_INVARIANTS.md`
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
- **refs:** `scripts/check_complexity_allowlist.py`, `scripts/check_complexity.py`, `docs/ARCHITECTURE_INVARIANTS.md`
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
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `scripts/check_complexity.py`, `yadgar/config.py`, `yadgar/config_yaml.py`, `yadgar/config_registry.py`, `docs/ARCHITECTURE_INVARIANTS.md`
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
- **refs:** `scripts/check_trace_spans.py`, `docs/ARCHITECTURE_INVARIANTS.md`
- **wiring:** I24 (and companion I19) enforced by the `check-trace-spans` pre-commit hook on changes to `yadgar/server/http.py`. The script scans for public top-level async functions lacking `@trace_span`. I19 (file handler before tracing init) is structurally enforced by call order in entry points; the contract cites `check_trace_spans.py` as its enforcement mechanism. I20 (FastAPIInstrumentor) and I21 (background thread root spans) are runtime invariants verified at startup/integration time with no dedicated CI hook.
- **explanation:** These four invariants describe the OTel observability layer. I19 ensures log output is captured before tracing starts (preventing lost logs during init). I20 ensures every FastAPI/Starlette app is wrapped with `FastAPIInstrumentor` so HTTP requests generate spans automatically. I21 ensures background threads (drainer, sleep cycle) each open a new OTel root span per work unit so distributed traces are complete. I24 ensures every public HTTP handler in `http.py` carries `@trace_span` so individual RPC latencies are always visible. The `check-trace-spans` hook enforces I24/I19 structurally.

---

### CAP-INFRA-012 — Trust boundary: single-user single-host

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I22`
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `scripts/check_metric_writers.py`, `yadgar/metrics.py`, `yadgar/backend/embed_service_metrics.py`, `docs/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-metric-writers` pre-commit hook on any change to `yadgar/**/*.py`. The script scans `yadgar/metrics.py` and `yadgar/backend/embed_service_metrics.py` for Prometheus metric declarations (Gauge, Counter, Histogram, Summary), then verifies each has ≥1 writer or reference site elsewhere in `yadgar/`. A declared metric with no writer is a dead metric.
- **explanation:** I23 prevents the proliferation of Prometheus metrics that are declared but never written — which wastes cardinality budget and misleads operators with always-zero gauges. The lint performs a two-pass scan: first it collects all declared metric objects by variable name, then it searches the codebase for call sites that increment, set, or observe each metric. Any metric with zero call sites fails the lint. This is tightly coupled with I8 (backpressure observability) and the overall metrics contract.

---

### CAP-INFRA-014 — Secret gate single chokepoint

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I26`
- **refs:** `scripts/check_secret_gate.py`, `yadgar/secrets.py`, `yadgar/security/allowlist.py`, `docs/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-secret-gate` pre-commit hook on changes to `yadgar/server/tools/**/*.py`. Every `@_tool`-decorated function with write parameters (`content`, `current_task`, etc.) must call `gate_or_reject()` or carry a `# secret-gate: skip` annotation. Known delegating tools (`seed_project`, `wiki_approve`) are explicitly exempted (`remember` deleted in v6 T3). This invariant ties directly to BC-S1 (secret patterns blocked at API).
- **explanation:** I26 mandates that secret scanning is a single-entry-point gate: the `gate_or_reject()` function in `yadgar/secrets.py`. No write tool is permitted to bypass this gate without an explicit annotation. The gate checks incoming content against known secret patterns (API keys, tokens, passwords) and either rejects the write or routes it through the allowlist bypass path (which triggers an audit — I28). Keeping the gate at a single chokepoint ensures that new write tools cannot accidentally skip secret scanning.

---

### CAP-INFRA-015 — Allowlist bypass audit

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I28`
- **refs:** `scripts/check_allowlist_audit.py`, `yadgar/security/allowlist.py`, `yadgar/secrets.py`, `docs/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-allowlist-audit` pre-commit hook on changes to `yadgar/security/allowlist.py` or `yadgar/secrets.py`. The script structurally verifies: `is_allowlisted()` and `_write_audit()` co-exist in `allowlist.py`; `gate_or_reject()` in `secrets.py` calls both; `YADGAR_SECRET_GATE_AUDIT_DIR` env knob is documented. This ties to BC-S3.
- **explanation:** I28 ensures that every time an allowlist bypass is used (content that would normally be blocked passes the secret gate), an audit record is written. Without this, a compromised allowlist entry could silently permit secret leakage. The structural check verifies that the `is_allowlisted()` function and the `_write_audit()` function are always called together in the gate implementation, making it impossible for a refactor to separate them.

---

### CAP-INFRA-016 — No dead capability: edge types contracted

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I29`
- **refs:** `scripts/check_dead_capability.py`, `yadgar/graph_api.py`, `yadgar/viz_meta.py`, `docs/EDGE_CONTRACT.md`, `docs/ARCHITECTURE_INVARIANTS.md`
- **wiring:** Enforced by the `check-dead-capability` pre-commit hook on changes to `yadgar/graph_api.py`, `yadgar/viz_meta.py`, or `docs/EDGE_CONTRACT.md`. The script AST-scans `graph_api.py` and `viz_meta.py` for edge-type literals and `EDGE_TYPES` registry keys, cross-references against `docs/EDGE_CONTRACT.md`, and fails on orphan edge types (produced but not contracted), drop-still-produced (removed from contract but still produced), or stale contract rows (contracted but never produced).
- **explanation:** I29 applies the "no dead capability" principle specifically to the knowledge graph edge-type layer: every edge type that the code produces must appear in `EDGE_CONTRACT.md`, and every contracted edge type must actually be produced. This three-way consistency check (stored ≡ used ≡ shown) prevents documentation drift where the contract describes edge types that no longer exist, or code produces edges that are undocumented and therefore invisible to operators and maintainers.

---

### CAP-INFRA-017 — Directory scoping: single eligible predicate

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I31`
- **refs:** `docs/ARCHITECTURE_INVARIANTS.md`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `scripts/check_contract_coverage.py`, `scripts/check_e2e_assertions.py`, `scripts/check_test_weakening.py`, `docs/BEHAVIOR_CONTRACT.md`
- **wiring:** Three enforcement layers: (1) `check_contract_coverage.py` runs as a non-e2e pytest to validate ✅ refs, header counts, and the ✅-count floor (≥12). (2) `check-e2e-assertions` pre-commit hook — every `def test_*` under `yadgar/tests/e2e/` must have ≥1 real assertion. (3) `check-test-weakening` pre-commit hook — staged diff must not net-remove `assert` statements from e2e tests or decrease the ✅ count in BEHAVIOR_CONTRACT.md.
- **explanation:** These three lints form the tamper-protection stack for the behavior contract. Layer 3 (`check_e2e_assertions.py`) prevents hollow e2e tests (functions named `test_*` that make no assertion). Layer 4 (`check_test_weakening.py`) prevents silent degradation: a commit that removes more asserts than it adds, or that drops the ✅ green count, is blocked unless the developer sets `ALLOW_TEST_WEAKEN=1` as an explicit one-time override. Together they make it structurally hard to weaken the contract without detection.

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
- **refs:** `yadgar/server/tools/memorize.py`, `yadgar/server/tools/recall.py`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `yadgar/server/tools/project.py`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `yadgar/server/tools/misc.py`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `yadgar/server/tools/misc.py`, `yadgar/server/tools/audit.py`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `yadgar/server/tools/misc.py`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `yadgar/server/tools/admin_other.py`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `yadgar/server/tools/agent_prompts.py`, `yadgar/server/tools/dispatch_helper.py`, `docs/BEHAVIOR_CONTRACT.md`
- **wiring:** `agent_prompt_save` and `agent_prompt_get` are in `agent_prompts.py`; `agent_dispatch_prelude` is in `dispatch_helper.py`. All three registered `@_tool`. Cross-refs: BC-T16 = BC-AP1, BC-T17 = BC-AP1/AP2, BC-T18 = BC-AP3.
- **explanation:** `agent_prompt_save` stores a named prompt template for later retrieval. `agent_prompt_get` retrieves a named template by exact name (returning not-found for unknown names, never a stale match). `agent_dispatch_prelude` assembles the standard agent-dispatch prelude, injecting directory-scoped context from `project_brief`. Together these tools support the orchestrator pattern: the main thread stores reusable subagent prompts and retrieves them at dispatch time, enriched with current project context.

---

### CAP-INFRA-027 — MCP tool surface: in-context blocks

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T19`, `BC-T20`, `BC-T21`, `BC-T22`, `BC-T23`, `BC-T24`, `BC-T25`
- **refs:** `yadgar/server/tools/blocks.py`, `docs/BEHAVIOR_CONTRACT.md`
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
- **refs:** `yadgar/server/tools/bookmarks.py`, `docs/BEHAVIOR_CONTRACT.md`
- **wiring:** `bookmark_add`, `bookmark_list`, `bookmark_remove`, `bookmark_reorder` are `@_tool` functions in `bookmarks.py`. All reachable via FastMCP. Cross-refs: BC-T26..T29 = BC-G7.
- **explanation:** Wiki bookmarks allow pinning frequently-accessed wiki slugs for quick retrieval without a full text search. `bookmark_add` pins a slug; `bookmark_list` returns the ordered list; `bookmark_remove` unpins; `bookmark_reorder` changes the display order. These four tools implement the bookmark CRUD surface (BC-G7) using real-path storage (SurrealDB), meaning bookmarks persist across sessions and are returned by real-path integration tests.

---

### CAP-INFRA-029 — MCP tool surface: wiki core read/write

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T30`, `BC-T31`, `BC-T32`, `BC-T33`, `BC-T34`, `BC-T35`, `BC-T36`, `BC-T37`, `BC-T38`, `BC-T39`, `BC-T40`, `BC-T41`, `BC-T42`, `BC-T43`, `BC-T44`, `BC-T45`
- **refs:** `yadgar/server/tools/wiki.py`, `docs/BEHAVIOR_CONTRACT.md`
- **wiring:** All 16 tools (`wiki_add`, `wiki_get`, `wiki_read`, `wiki_query`, `wiki_list`, `wiki_update`, `wiki_approve`, `wiki_drafts`, `wiki_discard`, `wiki_check_duplicate`, `wiki_history`, `wiki_read_version`, `wiki_diff`, `wiki_restore`, `wiki_set_metadata`, `wiki_lint`) are `@_tool` functions in `wiki.py`. All reachable via FastMCP.
- **explanation:** These sixteen tools form the core wiki CRUD and versioning surface. `wiki_add`/`wiki_update` write pages with directory+branch scoping and create immutable `wiki_page_version` records (BC-G4). `wiki_read`/`wiki_get` look up by slug with §25 resolution: directory+branch → directory+null → global → not-found (BC-G3). `wiki_query` performs semantic search scoped to a directory (BC-G2). `wiki_approve` gates the draft→live workflow (BC-G5). `wiki_check_duplicate` runs the similarity gate (BC-G6). `wiki_history`/`wiki_read_version`/`wiki_diff`/`wiki_restore` expose the immutable version history. `wiki_set_metadata` patches metadata across all branch rows for a slug (BC-G10). `wiki_lint` validates a page against the wiki lint rules.

---

### CAP-INFRA-030 — MCP tool surface: wiki edit primitives

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T46`, `BC-T47`, `BC-T48`, `BC-T49`, `BC-T50`, `BC-T51`, `BC-T52`, `BC-T53`, `BC-T54`, `BC-T55`, `BC-T56`, `BC-T57`, `BC-T58`
- **refs:** `yadgar/server/tools/wiki.py`, `yadgar/server/tools/wiki_coverage.py`, `docs/BEHAVIOR_CONTRACT.md`
- **wiring:** `wiki_append_section`, `wiki_insert_at`, `wiki_insert_after`, `wiki_insert_before`, `wiki_replace_at`, `wiki_replace_text`, `wiki_replace_markdown_block`, `wiki_delete`, `wiki_delete_at`, `wiki_delete_text` are in `wiki.py`; `wiki_coverage` is in `wiki_coverage.py`; `wiki_cleanup_merged_branches` and `wiki_refresh_stale` are in `wiki.py`. All `@_tool`, all reachable via FastMCP.
- **explanation:** These thirteen tools provide surgical positional edit operations on wiki pages (BC-G9): append a section, insert at/after/before a line, replace at a position, replace by text pattern, replace a markdown block, delete a page, delete at a line, delete by text pattern. `wiki_coverage` produces a coverage report of wiki pages vs known slugs. `wiki_cleanup_merged_branches` removes pages scoped to merged git branches (BC-G8). `wiki_refresh_stale` re-embeds or re-validates pages whose embeddings are outdated. All edits are versioned (each write creates a new `wiki_page_version` record per BC-G4).

---

### CAP-INFRA-031 — MCP tool surface: consolidation + vacuum + admin ops

- **status:** LIVE
- **category:** mcp-tool
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-T59`, `BC-T60`, `BC-T61`, `BC-T62`, `BC-T63`, `BC-T64`, `BC-T65`, `BC-T66`, `BC-T67`, `BC-T68`, `BC-T69`, `BC-T70`, `BC-T71`, `BC-T72`
- **refs:** `yadgar/server/tools/admin.py`, `yadgar/server/tools/admin_vacuum.py`, `yadgar/server/tools/admin_archive.py`, `yadgar/server/tools/admin_dlq.py`, `yadgar/server/tools/admin_invariants.py`, `yadgar/server/tools/admin_other.py`, `docs/BEHAVIOR_CONTRACT.md`
- **wiring:** `consolidate_now` (=BC-C1) and `check_invariants` (=BC-C1) are in `admin.py` or `admin_invariants.py`; `reembed_all` (=BC-ADM1) in `admin_other.py` or `admin.py`; `vacuum_now` (=BC-E1..E3) and `vacuum_checkpoints` in `admin_vacuum.py`; `archive_purge` (=BC-ADM5) in `admin_archive.py`; `forget` (=BC-ADM2), `memory_get`, `memory_update` (=BC-ADM6), `memory_stats`, `validate_memory` (=BC-ADM3) in `admin_other.py` or `admin.py`; `dlq_inspect`, `dlq_requeue` (=BC-ADM4), `dlq_dismiss` in `admin_dlq.py`. All `@_tool`, all reachable via FastMCP.
- **explanation:** These fourteen tools expose the administrative and operational surface. `consolidate_now` triggers an immediate consolidation cycle (episodic→semantic, sleep phases). `reembed_all` re-embeds every row with a missing embedding. `vacuum_now` performs the atomic database vacuum (BC-E1: row counts preserved, BC-E2: atomicity, BC-E3: sensitive-job lock). `vacuum_checkpoints` cleans up stale checkpoint records. `archive_purge` deletes archived memories older than a threshold. `forget`/`memory_get`/`memory_update`/`memory_stats`/`validate_memory` provide individual memory lifecycle management. `check_invariants` runs the full invariant check suite and returns violation counts. `dlq_inspect`/`dlq_requeue`/`dlq_dismiss` manage the dead-letter queue for failed write operations.


### CAP-INFRA-032 — Capability-registry coverage lint (I32, this document)

- **status:** LIVE
- **category:** infra
- **settings:** —
- **tools:** —
- **migrations:** —
- **bc:** `BC-I32`
- **refs:** `scripts/check_capability_coverage.py`, `yadgar/tests/test_capability_coverage.py`, `docs/CAPABILITY_REGISTRY.md`
- **wiring:** Enforced by `scripts/check_capability_coverage.py` (pre-commit hook `check-capability-coverage` + CI `invariant-checks` step) and the pytest `yadgar/tests/test_capability_coverage.py`. AST-enumerates the four authoritative surfaces (Settings fields in `config.py`, `@_tool` decorators in `server/tools/`, `_migration_NNN` functions, `BC-*` rows in BEHAVIOR_CONTRACT) and asserts every item is referenced by some entry in this file; flags ORPHAN (uncatalogued), STALE (entry cites a vanished item), and MALFORMED (bad status / unresolved ref).
- **explanation:** This is the self-referential invariant that keeps THIS registry honest. It guarantees catalogue completeness, not status correctness (see "Scope of the guarantee" at the top). It makes the registry the durable source of truth the e2e behavior contract, the v6 plan, and the #41 dead-config audit all draw "what exists" from — adding any surface item without cataloguing it here fails the build.


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
- **refs:** `yadgar/metrics.py`, `yadgar/cli/stats.py`, `yadgar/tests/test_v6_data_quality_stats.py`, `docs/plans/PLAN_V6_QUALITY_FOUNDATION.md`
- **wiring:** Seven Prometheus Gauges declared in `yadgar/metrics.py` (v6 Phase 0.2 block): `yadgar_data_quality_embedding_valid_ratio`, `yadgar_data_quality_null_embedding_count`, `yadgar_data_quality_duplicate_rate`, `yadgar_data_quality_zombie_rate`, `yadgar_data_quality_domain_coverage`, `yadgar_data_quality_surprise_p50`, `yadgar_data_quality_surprise_p95`. Writers: `_collect_data_quality()` in `yadgar/metrics.py` (called on every `/metrics` scrape, alongside `_collect_queue_depths()`). Stats CLI: `_query_data_quality()` in `yadgar/cli/stats.py` populates `StatsData.dq_*` fields, printed in the `DATA QUALITY (v6 Phase 0.2)` section of `yadgar stats` output and included in the JSON output from `yadgar stats --format json`. I23 compliance: `check_metric_writers.py` verifies `_collect_data_quality()` as the writer for all seven gauges.
- **explanation:** The Phase-0.2 dashboard metrics that make corpus health visible without running the full eval harness. Null-embedding count is the hardest signal: `embedding_valid_ratio < 1.0` indicates the corruption class that the v5.66 zombie purge and today's reembed_all fix targeted. Duplicate-rate (sim-links / active memories) measures write-gate efficiency. Zombie-rate (stale / total) measures consolidation health. Domain-coverage measures astrocyte effectiveness. Surprise distribution (p50/p95) provides a histogram summary for Phase-1 write-gate tuning. All seven are best-effort (swallowed DB errors) so a degraded DB doesn't break the /metrics endpoint.
