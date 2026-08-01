# Configuration Reference

Yadgar is configured via three mechanisms, applied in priority order:

1. **Environment variables** — prefix `YADGAR_`, e.g. `YADGAR_DECAY_FACTOR=0.999`
2. **YAML config file** — `~/.config/yadgar/config.yaml` (override path with `YADGAR_CONFIG_FILE`)
3. **Built-in defaults** — shown in this document

> **Path note.** The config file and `secrets.env` live under `~/.config/yadgar/`
> (XDG config home). Runtime data — the SurrealDB store, queue, logs, snapshots —
> lives under `~/.local/share/yadgar/` (XDG data home, override with
> `YADGAR_DATA_DIR`). State files (PID, triggers, session-ends) live under
> `~/.local/state/yadgar/`. See `yadgar/paths.py` for the full XDG resolution.

Every key in the tables below maps to a real field on the `Settings` class in
`yadgar/config.py`. The env var is always `YADGAR_<KEY-UPPERCASED>`. Some fields
have no curated description in `yadgar/config_yaml.py` `FIELD_META`; for those the
description here is taken verbatim from the config.py inline comment.

## Quick start

```bash
# Create config file with all defaults commented
yadgar config init

# Inspect / change individual values
yadgar config list
yadgar config list --section thermodynamics
yadgar config get decay_factor
yadgar config set retrieval_profile fast

# Open in $EDITOR
yadgar config edit
```

The YAML file is optional. If it doesn't exist, all defaults apply. Values you don't set in the file are taken from defaults. You only need to add the keys you want to override.

---

## Core

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `host` | `YADGAR_HOST` | str | `127.0.0.1` | Bind address for the MCP HTTP server. Loopback-only by default; set to `0.0.0.0` explicitly for LAN exposure (not recommended without auth + TLS). |
| `port` | `YADGAR_PORT` | int | `8765` | HTTP server port (daemon mode). |
| `db_path` | `YADGAR_DB_PATH` | str | `~/.local/share/yadgar/surreal_db` | SurrealDB storage path. |
| `embedding_model` | `YADGAR_EMBEDDING_MODEL` | str | `all-MiniLM-L6-v2` | Sentence-transformer model (e.g. `all-MiniLM-L6-v2`, `all-mpnet-base-v2`). |
| `model_idle_eviction_seconds` | `YADGAR_MODEL_IDLE_EVICTION_SECONDS` | int | `0` | Seconds a loaded model may sit idle in the backend before eviction (`0` = never evict). Read by the backend RemoteMLClient. v5.95: promoted from env-only to config.yaml-authoritative. |
| `max_episode_tokens` | `YADGAR_MAX_EPISODE_TOKENS` | int | `50000` | Maximum tokens per episode chunk. |
| `overlap_tokens` | `YADGAR_OVERLAP_TOKENS` | int | `2000` | Token overlap between episode chunks. |
| `crdt_agent_id` | `YADGAR_CRDT_AGENT_ID` | str | `default` | Agent identifier for multi-agent CRDT sync. |
| `sensitive_lock_ttl_sec` | `YADGAR_SENSITIVE_LOCK_TTL_SEC` | int | `7200` | Seconds before a sensitive-job lock (vacuum) is treated as stale and reaped. |
| `sensitive_drain_timeout_sec` | `YADGAR_SENSITIVE_DRAIN_TIMEOUT_SEC` | float | `300.0` | Max seconds the signal handler drains an in-process sensitive job before refusing shutdown. |

---

## Daemon / Background Processing

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `num_astrocyte_processes` | `YADGAR_NUM_ASTROCYTE_PROCESSES` | int | `4` | Number of domain-aware background worker processes. |
| `astrocyte_pool_enabled` | `YADGAR_ASTROCYTE_POOL_ENABLED` | bool | `true` | Enable domain-aware astrocyte pool consolidation (set false to disable). |
| `narrative_interval_hours` | `YADGAR_NARRATIVE_INTERVAL_HOURS` | int | `24` | Hours between autobiographical narrative updates. |
| `similarity_linking_incremental_enabled` | `YADGAR_SIMILARITY_LINKING_INCREMENTAL_ENABLED` | bool | `false` | v5.86: link only memories created since the last run (probe×corpus), with a periodic full reconcile. Default off — the full N×N pass runs every cycle until enabled. |
| `similarity_linking_reconcile_interval_days` | `YADGAR_SIMILARITY_LINKING_RECONCILE_INTERVAL_DAYS` | int | `7` | Days between mandatory full similarity-link reconcile passes (safety net for re-embedding that mutates old↔old similarity). |

---

## Memory Lifecycle

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `write_gate_threshold` | `YADGAR_WRITE_GATE_THRESHOLD` | float | `0.0` | Minimum score to store a memory (0.0 = store everything). |
| `write_gate_shadow_threshold` | `YADGAR_WRITE_GATE_SHADOW_THRESHOLD` | float | `0.15` | v5.73.0 shadow-mode threshold: memories below this score are stamped `would_reject=True` but still stored (`write_gate_threshold` stays 0.0). Audits which memories would be dropped at a candidate threshold. |
| `write_gate_continuity_discount` | `YADGAR_WRITE_GATE_CONTINUITY_DISCOUNT` | float | `0.15` | Threshold reduction for task-continuous content. |
| `write_gate_continuity_window` | `YADGAR_WRITE_GATE_CONTINUITY_WINDOW` | int | `10` | Number of recent stores to track for continuity detection. |
| `decision_auto_protect` | `YADGAR_DECISION_AUTO_PROTECT` | bool | `true` | Automatically protect detected decisions from decay. |
| `action_stream_enabled` | `YADGAR_ACTION_STREAM_ENABLED` | bool | `true` | Capture tool actions in sensory buffer for later consolidation. |
| `micro_checkpoint_enabled` | `YADGAR_MICRO_CHECKPOINT_ENABLED` | bool | `true` | Auto-checkpoint on significant events. |
| `micro_checkpoint_cooldown` | `YADGAR_MICRO_CHECKPOINT_COOLDOWN` | int | `5` | Minimum tool calls between micro-checkpoints. |
| `session_coherence_bonus` | `YADGAR_SESSION_COHERENCE_BONUS` | float | `0.2` | Heat bonus applied to memories from the current session. |
| `session_coherence_window_hours` | `YADGAR_SESSION_COHERENCE_WINDOW_HOURS` | float | `4.0` | How long the session coherence bonus lasts. |
| `reinjection_enabled` | `YADGAR_REINJECTION_ENABLED` | bool | `true` | Auto-surface related context when storing a new memory. |
| `reinjection_max_results` | `YADGAR_REINJECTION_MAX_RESULTS` | int | `3` | Max related memories to reinject on store. |
| `curation_similarity_threshold` | `YADGAR_CURATION_SIMILARITY_THRESHOLD` | float | `0.95` | Minimum similarity to trigger memory curation/merging (near-duplicates only). |
| `contextual_prefix_enabled` | `YADGAR_CONTEXTUAL_PREFIX_ENABLED` | bool | `true` | Prepend contextual prefix to improve embedding quality. |
| `similarity_link_threshold` | `YADGAR_SIMILARITY_LINK_THRESHOLD` | float | `0.78` | Minimum cosine to create a `memory_similarity_link`. *(no FIELD_META — from config.py comment)* |
| `max_similarity_links_per_memory` | `YADGAR_MAX_SIMILARITY_LINKS_PER_MEMORY` | int | `15` | Degree cap — bounds `memory_similarity_link` table size. *(no FIELD_META)* |
| `reinject_on_write` | `YADGAR_REINJECT_ON_WRITE` | bool | `false` | v5.4 P7 write-time reinjection gate. When off, the `retriever.recall()` block in `memorize()` is skipped entirely (saves 30–50 ms sync vector search per write). *(no FIELD_META)* |
| `predictive_coding_entity_ttl_seconds` | `YADGAR_PREDICTIVE_CODING_ENTITY_TTL_SECONDS` | int | `300` | TTL (seconds) for the entity-set cache inside WriteGate; avoids a `get_all_entities()` DB call on every write-gate evaluation. `0` = disable caching. Invalidated on entity add/delete. *(no FIELD_META)* |

---

## Memory Thermodynamics (Decay)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `decay_factor` | `YADGAR_DECAY_FACTOR` | float | `0.9995` | Per-hour heat decay multiplier (≈34% heat after 3 months no access). |
| `importance_decay_factor` | `YADGAR_IMPORTANCE_DECAY_FACTOR` | float | `0.9999` | Per-cycle importance decay (≈81% heat after 3 months for important memories). |
| `cold_threshold` | `YADGAR_COLD_THRESHOLD` | float | `0.02` | Heat below which a memory is archived (~6 months no-access floor). |
| `action_stream_cold_threshold` | `YADGAR_ACTION_STREAM_COLD_THRESHOLD` | float | `0.1` | Heat below which an auto-captured action-stream memory is archived (higher than `cold_threshold` so they expire faster). |
| `hot_threshold` | `YADGAR_HOT_THRESHOLD` | float | `0.0` | Minimum heat for hot-memory retrieval (0.0 = include all). |
| `project_context_min_heat` | `YADGAR_PROJECT_CONTEXT_MIN_HEAT` | float | `0.01` | Minimum heat for project context injection. |
| `surprise_boost` | `YADGAR_SURPRISE_BOOST` | float | `0.3` | Heat boost applied to surprising/novel memories. |
| `emotional_decay_resistance` | `YADGAR_EMOTIONAL_DECAY_RESISTANCE` | float | `0.5` | How much emotional salience slows decay (0–1). |
| `synaptic_window_minutes` | `YADGAR_SYNAPTIC_WINDOW_MINUTES` | int | `30` | Time window for synaptic boost propagation. |
| `synaptic_boost` | `YADGAR_SYNAPTIC_BOOST` | float | `0.2` | Heat boost propagated from high-importance nearby memories. |
| `recall_boost` | `YADGAR_RECALL_BOOST` | float | `0.05` | Per-access heat boost added during each decay cycle: `new_heat = min(decay(mem) + access_count_since_decay * RECALL_BOOST, 1.0)`. Set `0.0` for pure exponential decay. *(no FIELD_META)* |
| `branch_boost_weight` | `YADGAR_BRANCH_BOOST_WEIGHT` | float | `0.2` | Convex-combination boost weight for current-branch memories: `boosted = score + (1 - score) * weight`. *(no FIELD_META)* |
| `postmortem_boost_factor` | `YADGAR_POSTMORTEM_BOOST_FACTOR` | float | `0.3` | Boost applied (via the convex formula) when a recall query contains an action verb and a candidate carries `_postmortem`/`_incident` tags. `0.0` = disable. *(no FIELD_META)* |
| `postmortem_boost_keywords` | `YADGAR_POSTMORTEM_BOOST_KEYWORDS` | tuple | `deploy,push,merge,restart,vacuum,rollback,upgrade,migrate,bump,release` | Action verbs that trigger the postmortem boost. *(no FIELD_META)* |

---

## Retrieval & Fusion (WRRF)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `retrieval_profile` | `YADGAR_RETRIEVAL_PROFILE` | str | `balanced` | Preset: `fast`, `balanced`, or `full`. `fast` = memory-only BM25+HNSW+fusion: no CE/NLI/MP, no wiki fanout, no engram-link enrichment (ADR-0077 — the hook-latency-budget profile). |
| `wrrf_candidate_multiplier` | `YADGAR_WRRF_CANDIDATE_MULTIPLIER` | int | `10` | Candidate pool size = `max_results * this`. |
| `wrrf_vector_weight` | `YADGAR_WRRF_VECTOR_WEIGHT` | float | `1.0` | Weight of vector similarity signal in WRRF fusion. |
| `wrrf_fts_weight` | `YADGAR_WRRF_FTS_WEIGHT` | float | `0.5` | Weight of full-text search signal in WRRF fusion. |
| `wrrf_ppr_weight` | `YADGAR_WRRF_PPR_WEIGHT` | float | `0.5` | Weight of personalized PageRank signal. |
| `wrrf_spreading_weight` | `YADGAR_WRRF_SPREADING_WEIGHT` | float | `0.3` | Weight of spreading activation signal. |
| `wrrf_graph_prior_weight` | `YADGAR_WRRF_GRAPH_PRIOR_WEIGHT` | float | `0.2` | v5.54.1: weight of the precomputed entity-graph prior in fusion. Additive boost applied in ALL profiles including fast (O(1) field read). `0.0` = disable. |
| `wrrf_cofire_prior_weight` | `YADGAR_WRRF_COFIRE_PRIOR_WEIGHT` | float | `0.15` | v5.54.2: weight of the precomputed co-recall (transition-edge) prior in fusion. Additive boost in ALL profiles. `0.0` = disable. |
| `fusion_method` | `YADGAR_FUSION_METHOD` | str | `convex` | Fusion method: `convex` or other. |
| `fusion_norm` | `YADGAR_FUSION_NORM` | str | `zscore` | Score normalization before fusion: `zscore`, `minmax`, or `raw`. |
| `combmnz_enabled` | `YADGAR_COMBMNZ_ENABLED` | bool | `false` | Multiply fused score by the number of signals that contributed. |
| `heavy_rerank_enabled` | `YADGAR_HEAVY_RERANK_ENABLED` | bool | `true` | v5.6.6 kill switch. When false, all CE/NLI/MP reranking is skipped; retrieval falls back to BM25+HNSW fusion only (eliminates CPU burst from reranking). *(no FIELD_META)* |

---

## Reranking

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `reranker_enabled` | `YADGAR_RERANKER_ENABLED` | bool | `true` | Enable cross-encoder reranking stage. |
| `reranker_top_k` | `YADGAR_RERANKER_TOP_K` | int | `50` | Number of candidates passed to reranker. |
| `cross_encoder_enabled` | `YADGAR_CROSS_ENCODER_ENABLED` | bool | `true` | Enable the cross-encoder rerank stage. Also gates the fallback model load in `local_ml_client.py` — two meanings, one flag (ADR-0192). |
| `cross_encoder_model` | `YADGAR_CROSS_ENCODER_MODEL` | str | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Degraded-mode fallback CE model, reached only when the primary is disabled or has failed. Not baked into `Dockerfile.backend`, so it scores zeros in the offline container. The live reranker is the `gte_reranker_*` slot — see below. |
| `cross_encoder_top_k` | `YADGAR_CROSS_ENCODER_TOP_K` | int | `10` | Top-k passed to cross-encoder. |
| `cross_encoder_weight` | `YADGAR_CROSS_ENCODER_WEIGHT` | float | `0.6` | Cross-encoder score weight in blend (retrieval gets 1-this). |
| `gte_reranker_enabled` | `YADGAR_GTE_RERANKER_ENABLED` | bool | `true` | Enable the advanced cross-encoder reranker (field name kept `GTE_*` for env/back-compat; T4 flipped the default model to Ettin-32m). |
| `gte_reranker_model` | `YADGAR_GTE_RERANKER_MODEL` | str | `cross-encoder/ettin-reranker-32m-v1` | Primary reranker model (Ettin-32m, Train 4). GTE rollback: set to `Alibaba-NLP/gte-reranker-modernbert-base` (baked into `Dockerfile.backend` one cycle). |
| `gte_reranker_max_length` | `YADGAR_GTE_RERANKER_MAX_LENGTH` | int | `512` | Max token length for GTE reranker. |
| `gte_reranker_fallback_to_flashrank` | `YADGAR_GTE_RERANKER_FALLBACK_TO_FLASHRANK` | bool | `true` | Failure-mode selector, not a FlashRank switch (name kept for env back-compat). When the primary reranker fails: `true` falls through to the `cross_encoder_model` tier, `false` returns zero scores. Never read when `gte_reranker_enabled=false`. The FlashRank tier was removed in ADR-0192. |
| `nli_reranking_enabled` | `YADGAR_NLI_RERANKING_ENABLED` | bool | `false` | Enable NLI entailment scoring stage. v5.6.6 default flipped `true`→`false` — NLI averages ~55 s/call on CPU for marginal gain over CE alone. |
| `nli_model` | `YADGAR_NLI_MODEL` | str | `cross-encoder/nli-deberta-v3-base` | NLI model name. |
| `nli_weight` | `YADGAR_NLI_WEIGHT` | float | `0.3` | NLI signal weight in final blend. |
| `nli_only_for_open_domain` | `YADGAR_NLI_ONLY_FOR_OPEN_DOMAIN` | bool | `true` | Only apply NLI reranking for open-domain queries. |
| `multi_passage_reranking_enabled` | `YADGAR_MULTI_PASSAGE_RERANKING_ENABLED` | bool | `true` | Enable multi-passage evidence aggregation. |
| `multi_passage_cluster_overlap_threshold` | `YADGAR_MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD` | float | `0.3` | Overlap threshold for passage clustering. |
| `multi_passage_max_cluster_size` | `YADGAR_MULTI_PASSAGE_MAX_CLUSTER_SIZE` | int | `3` | Maximum passages per evidence cluster. |

---

## Query Routing

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `query_routing_enabled` | `YADGAR_QUERY_ROUTING_ENABLED` | bool | `true` | Enable automatic query routing to specialized retrievers. |
| `query_expansion_enabled` | `YADGAR_QUERY_EXPANSION_ENABLED` | bool | `true` | Enable query expansion (pseudo-HyDE). |
| `comparison_dual_search_enabled` | `YADGAR_COMPARISON_DUAL_SEARCH_ENABLED` | bool | `true` | Run dual search for comparison queries. |
| `comparison_top_k_per_option` | `YADGAR_COMPARISON_TOP_K_PER_OPTION` | int | `10` | Top-k results per option in comparison search. |
| `temporal_keywords` | `YADGAR_TEMPORAL_KEYWORDS` | str | *(see config)* | Comma-separated keywords that trigger temporal routing. |
| `code_keywords` | `YADGAR_CODE_KEYWORDS` | str | *(see config)* | Comma-separated keywords that trigger code-aware routing. |
| `relational_keywords` | `YADGAR_RELATIONAL_KEYWORDS` | str | *(see config)* | Comma-separated keywords that trigger relational routing. |

---

## Temporal Retrieval

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `temporal_retrieval_enabled` | `YADGAR_TEMPORAL_RETRIEVAL_ENABLED` | bool | `true` | Boost memories that match temporal expressions in query. |

---

## Embedding Enhancement

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `candidate_pool_multiplier` | `YADGAR_CANDIDATE_POOL_MULTIPLIER` | int | `20` | Total candidate pool = `max_results * this` before reranking. |
| `fast_profile_candidate_multiplier` | `YADGAR_FAST_PROFILE_CANDIDATE_MULTIPLIER` | int | `3` | Candidate pool multiplier used only for `profile='fast'`; overrides the global multiplier on the fast path to bound latency. |
| `implicit_embedding_model` | `YADGAR_IMPLICIT_EMBEDDING_MODEL` | str | `""` | v25 dual-vector prep — config-only, retained pending a future DualCSE implementation (`_dual_vector_search` removed in v6 T3). *(no FIELD_META)* |

---

## Graph & Knowledge

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `graph_max_hops` | `YADGAR_GRAPH_MAX_HOPS` | int | `2` | Maximum graph traversal hops for spreading activation. |
| `graph_min_edge_weight` | `YADGAR_GRAPH_MIN_EDGE_WEIGHT` | float | `0.1` | Minimum edge weight to traverse in graph signals. |
| `graph_spreading_decay` | `YADGAR_GRAPH_SPREADING_DECAY` | float | `0.5` | Activation decay factor per hop. |
| `graph_spreading_max_depth` | `YADGAR_GRAPH_SPREADING_MAX_DEPTH` | int | `2` | Maximum depth for spreading activation. |
| `graph_entity_min_length` | `YADGAR_GRAPH_ENTITY_MIN_LENGTH` | int | `3` | Minimum character length for extracted entities. |
| `causal_threshold` | `YADGAR_CAUSAL_THRESHOLD` | int | `3` | Minimum co-occurrence count before inferring causality. |
| `ppr_damping` | `YADGAR_PPR_DAMPING` | float | `0.85` | Personalized PageRank damping factor. |
| `ppr_iterations` | `YADGAR_PPR_ITERATIONS` | int | `50` | Number of PageRank iterations. |
| `cluster_similarity_threshold` | `YADGAR_CLUSTER_SIMILARITY_THRESHOLD` | float | `0.7` | Minimum similarity to assign a memory to a cluster. |

---

## Neuromorphic (Hopfield / HDC / SR)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `cognitive_load_limit` | `YADGAR_COGNITIVE_LOAD_LIMIT` | int | `4` | Max chunks in active context (Cowan's 4±1 rule). |
| `excitability_half_life_hours` | `YADGAR_EXCITABILITY_HALF_LIFE_HOURS` | float | `6.0` | Engram excitability decay half-life in hours. |
| `excitability_boost` | `YADGAR_EXCITABILITY_BOOST` | float | `0.5` | Excitability increase on engram slot activation. |
| `dream_replay_pairs` | `YADGAR_DREAM_REPLAY_PAIRS` | int | `20` | Random memory pairs examined per dream replay cycle. |
| `hopfield_beta` | `YADGAR_HOPFIELD_BETA` | float | `8.0` | Hopfield sharpness (low = blended recall, high = precise). *(no FIELD_META)* |
| `hopfield_max_patterns` | `YADGAR_HOPFIELD_MAX_PATTERNS` | int | `5000` | Max patterns in the Hopfield energy store. *(no FIELD_META)* |
| `sr_discount` | `YADGAR_SR_DISCOUNT` | float | `0.9` | Successor-representation discount factor γ. *(no FIELD_META)* |
| `sr_update_rate` | `YADGAR_SR_UPDATE_RATE` | float | `0.1` | Incremental SR update learning rate. *(no FIELD_META)* |

---

## Index-Time Enrichment

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `index_enrichment_enabled` | `YADGAR_INDEX_ENRICHMENT_ENABLED` | bool | `true` | Enable index-time memory enrichment pipeline. |
| `enrichment_min_content_length` | `YADGAR_ENRICHMENT_MIN_CONTENT_LENGTH` | int | `20` | Minimum content length to run enrichment. |
| `conceptnet_enrichment_enabled` | `YADGAR_CONCEPTNET_ENRICHMENT_ENABLED` | bool | `true` | Expand memories with ConceptNet relations. |
| `conceptnet_min_edge_weight` | `YADGAR_CONCEPTNET_MIN_EDGE_WEIGHT` | float | `1.0` | Minimum ConceptNet edge weight to include. |
| `conceptnet_max_terms` | `YADGAR_CONCEPTNET_MAX_TERMS` | int | `10` | Maximum ConceptNet terms to add per memory. |
| `conceptnet_relations` | `YADGAR_CONCEPTNET_RELATIONS` | str | `IsA,UsedFor,HasProperty,AtLocation,MotivatedByGoal,CausesDesire,CapableOf` | Comma-separated ConceptNet relations to use. |
| `comet_enrichment_enabled` | `YADGAR_COMET_ENRICHMENT_ENABLED` | bool | `false` | Expand memories with COMET commonsense inference. **Retired/dormant** per ADR-0004 (en2a ablation: net-negative recall). |
| `comet_model` | `YADGAR_COMET_MODEL` | str | `mismayil/comet-bart-ai2` | COMET model name. |
| `comet_num_beams` | `YADGAR_COMET_NUM_BEAMS` | int | `5` | Beam search width for COMET generation. |
| `comet_top_k_per_relation` | `YADGAR_COMET_TOP_K_PER_RELATION` | int | `3` | Top-k inferences per COMET relation. |
| `comet_min_confidence` | `YADGAR_COMET_MIN_CONFIDENCE` | float | `0.3` | Minimum COMET inference confidence to include. |
| `comet_relations` | `YADGAR_COMET_RELATIONS` | str | `xAttr,xIntent,xWant` | Comma-separated COMET relations to use. |
| `comet_query_expansion_enabled` | `YADGAR_COMET_QUERY_EXPANSION_ENABLED` | bool | `false` | Apply COMET expansion at query time too. |
| `doc2query_enrichment_enabled` | `YADGAR_DOC2QUERY_ENRICHMENT_ENABLED` | bool | `true` | Generate synthetic queries for each memory (doc2query). |
| `doc2query_model` | `YADGAR_DOC2QUERY_MODEL` | str | `doc2query/msmarco-t5-small-v1` | Doc2query model name. |
| `doc2query_num_queries` | `YADGAR_DOC2QUERY_NUM_QUERIES` | int | `5` | Number of synthetic queries to generate per memory. |
| `logic_enrichment_enabled` | `YADGAR_LOGIC_ENRICHMENT_ENABLED` | bool | `true` | Enable formal logic pattern enrichment. |
| `fpa_similarity_threshold` | `YADGAR_FPA_SIMILARITY_THRESHOLD` | float | `0.25` | Similarity threshold for first-principles analysis. |

---

## Profiles & Beliefs

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `profile_extraction_enabled` | `YADGAR_PROFILE_EXTRACTION_ENABLED` | bool | `true` | Extract and maintain structured user profiles. |
| `profile_confidence_direct` | `YADGAR_PROFILE_CONFIDENCE_DIRECT` | float | `0.7` | Confidence for directly stated profile attributes. |
| `profile_confidence_inferred` | `YADGAR_PROFILE_CONFIDENCE_INFERRED` | float | `0.4` | Confidence for inferred profile attributes. |
| `profile_summary_enabled` | `YADGAR_PROFILE_SUMMARY_ENABLED` | bool | `true` | Generate profile summaries. |
| `profile_search_weight` | `YADGAR_PROFILE_SEARCH_WEIGHT` | float | `1.0` | v5.68 fix #38: profile signal weight in retrieval (mirrors `belief_high_confidence_boost`). Was missing → profiles never surfaced. *(no FIELD_META)* |
| `derived_beliefs_enabled` | `YADGAR_DERIVED_BELIEFS_ENABLED` | bool | `true` | Derive and store higher-order beliefs from episodic memories. |
| `belief_high_confidence_boost` | `YADGAR_BELIEF_HIGH_CONFIDENCE_BOOST` | float | `1.2` | Score multiplier for high-confidence beliefs. |

---

## Adversarial Protection

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `adversarial_detection_enabled` | `YADGAR_ADVERSARIAL_DETECTION_ENABLED` | bool | `true` | Detect and suppress adversarially-crafted memory injection. |
| `adversarial_score_gap_threshold` | `YADGAR_ADVERSARIAL_SCORE_GAP_THRESHOLD` | float | `0.05` | Maximum acceptable score gap between top results. |
| `adversarial_diversity_enforcement` | `YADGAR_ADVERSARIAL_DIVERSITY_ENFORCEMENT` | bool | `true` | Enforce result diversity to prevent manipulation. |
| `adversarial_min_confidence` | `YADGAR_ADVERSARIAL_MIN_CONFIDENCE` | float | `0.3` | Minimum confidence required to surface a memory. |

---

## Recall Quality Floor (v5.62.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `recall_quality_floor` | `YADGAR_RECALL_QUALITY_FLOOR` | float | `0.0` | Minimum cross-encoder score for a recall result to be returned (`0.0` = disabled). Rows without a `_cross_encoder_score` always pass through. Calibration (2026-06-15): co-occurrence junk CE 0.0–0.157; genuine results CE 0.289–0.843. Raise to 0.15–0.20 in production after write-time backfill. |

---

## Unified Scoped Recall (v6 T6)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `recall_memory_quota` | `YADGAR_RECALL_MEMORY_QUOTA` | int | `5` | Max memory candidates in the fusion pool before CE rerank. Prevents memory candidates from starving wiki candidates. |
| `recall_wiki_quota` | `YADGAR_RECALL_WIKI_QUOTA` | int | `5` | Max wiki candidates in the fusion pool before CE rerank. |
| `recall_memory_prior_weight` | `YADGAR_RECALL_MEMORY_PRIOR_WEIGHT` | float | `0.1` | Additive prior weight for memory `native_score` in CE fusion (CE is primary; prior is a tie-shaper). |
| `recall_wiki_prior_weight` | `YADGAR_RECALL_WIKI_PRIOR_WEIGHT` | float | `0.1` | Additive prior weight for wiki `native_score` in CE fusion. |

---

## Memory Blocks (v5.35.1)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `memory_block_max_per_scope` | `YADGAR_MEMORY_BLOCK_MAX_PER_SCOPE` | int | `10` | Maximum blocks per (scope, directory) tuple. |
| `memory_block_default_char_limit` | `YADGAR_MEMORY_BLOCK_DEFAULT_CHAR_LIMIT` | int | `2000` | Default per-block character limit when none specified. |
| `memory_block_hard_char_limit` | `YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT` | int | `8000` | Absolute maximum per-block character limit (hard cap). |
| `memory_block_total_budget_chars` | `YADGAR_MEMORY_BLOCK_TOTAL_BUDGET_CHARS` | int | `12000` | Total character budget across all blocks at restore-time. |

---

## Project Brief

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `project_init_cap_chars` | `YADGAR_PROJECT_INIT_CAP_CHARS` | int | `2000` | Hard character cap for `_project_init` memory content. Raises `ValueError` on overflow. *(no FIELD_META)* |
| `brief_mode_default` | `YADGAR_BRIEF_MODE_DEFAULT` | str | `catalog` | Default mode for `project_brief`: `catalog` or `full`. *(no FIELD_META)* |
| `active_work_stale_hours` | `YADGAR_ACTIVE_WORK_STALE_HOURS` | float | `24.0` | Hours before `active_work` is considered stale (triggers `refresh_active_work` in signals mode). |
| `checkpoint_stale_hours` | `YADGAR_CHECKPOINT_STALE_HOURS` | float | `24.0` | Hours before checkpoint is considered stale (triggers `refresh_checkpoint`). |
| `project_brief_max_anchors` | `YADGAR_PROJECT_BRIEF_MAX_ANCHORS` | int | `12` | Maximum anchors returned in restore mode `top_anchors` list. |
| `active_work_warn_hours` | `YADGAR_ACTIVE_WORK_WARN_HOURS` | float | `12.0` | Hours before `consider_refresh_active_work` soft action fires (must be < `active_work_stale_hours`). |
| `checkpoint_warn_hours` | `YADGAR_CHECKPOINT_WARN_HOURS` | float | `12.0` | Hours before `consider_refresh_checkpoint` soft action fires (must be < `checkpoint_stale_hours`). |
| `signals_token_budget_soft` | `YADGAR_SIGNALS_TOKEN_BUDGET_SOFT` | int | `400` | Token-budget upper bound for signals-mode payload. Raise if new action types added. |
| `adr_due_warn_hours` | `YADGAR_ADR_DUE_WARN_HOURS` | float | `12.0` | Hours of ADR-log inactivity (vs `active_work`) before `capture_adr` fires in signals mode. |

---

## Active-Work Watchdog

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `auto_refresh_active_work` | `YADGAR_AUTO_REFRESH_ACTIVE_WORK` | bool | `false` | Watchdog opt-in: auto-write stub `_active_work` when stale. Default off (preserves user-curated `_active_work`); enable via systemd unit env. |

---

## Anchor Hygiene (TTL)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `anchor_conditional_ttl_days` | `YADGAR_ANCHOR_CONDITIONAL_TTL_DAYS` | int | `90` | Default `valid_until` offset (days) for `tier=conditional` anchors. |
| `anchor_ephemeral_ttl_days` | `YADGAR_ANCHOR_EPHEMERAL_TTL_DAYS` | int | `14` | Default `valid_until` offset (days) for `tier=ephemeral` anchors. |
| `anchor_semantic_immortal_requires_reason` | `YADGAR_ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON` | bool | `true` | Require a non-empty reason when `anchor(tier='semantic_immortal')` is called. |
| `anchor_redundancy_cosine` | `YADGAR_ANCHOR_REDUNDANCY_COSINE` | float | `0.92` | Minimum cosine similarity for an anchor redundancy candidate pair. |
| `anchor_promote_words` | `YADGAR_ANCHOR_PROMOTE_WORDS` | int | `500` | Minimum word count for an anchor promote-to-wiki candidate. |
| `anchor_promote_headers` | `YADGAR_ANCHOR_PROMOTE_HEADERS` | int | `2` | Minimum markdown header count for an anchor promote-to-wiki candidate. |
| `anchor_audit_threshold` | `YADGAR_ANCHOR_AUDIT_THRESHOLD` | int | `15` | `anchor_count_project` threshold above which the `audit_anchors` action is emitted. |
| `anchor_audit_consolidation_enabled` | `YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED` | bool | `true` | Toggle the anchor audit pass inside `consolidate_now()`. |
| `anchor_audit_max_actions_per_run` | `YADGAR_ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` | int | `20` | Hard cap on actions returned per audit run (token budget). |
| `anchor_audit_history_retention_days` | `YADGAR_ANCHOR_AUDIT_HISTORY_RETENTION_DAYS` | int | `30` | How long `_audit_anchors` sentinel snapshots are retained (days). |
| `anchor_cross_project_cosine` | `YADGAR_ANCHOR_CROSS_PROJECT_COSINE` | float | `0.95` | Minimum cosine for a cross-project anchor dedup candidate (higher than within-project 0.92). |

---

## Session-End Capture & Replay

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `session_end_capture_enabled` | `YADGAR_SESSION_END_CAPTURE_ENABLED` | bool | `true` | Kill switch for session-end sentinel capture. |
| `session_end_retention_days` | `YADGAR_SESSION_END_RETENTION_DAYS` | int | `30` | Auto-prune sentinel memory rows older than this many days. |
| `session_end_snippet_turns` | `YADGAR_SESSION_END_SNIPPET_TURNS` | int | `5` | Last N human turns embedded in the sentinel for rotation resilience. |
| `session_end_min_messages` | `YADGAR_SESSION_END_MIN_MESSAGES` | int | `2` | Skip the sentinel if the session had fewer than N human messages. |
| `replay_max_restore_memories` | `YADGAR_REPLAY_MAX_RESTORE_MEMORIES` | int | `8` | Maximum memories included in context restoration. |
| `replay_anchor_heat` | `YADGAR_REPLAY_ANCHOR_HEAT` | float | `1.0` | Heat assigned to anchored (protected) memories. |
| `replay_checkpoint_auto_interval` | `YADGAR_REPLAY_CHECKPOINT_AUTO_INTERVAL` | int | `50` | Auto-checkpoint every N tool calls. |

---

## Agent-Prompt Passive Library (v5.85)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `agent_prompt_library_enabled` | `YADGAR_AGENT_PROMPT_LIBRARY_ENABLED` | bool | `true` | Enable the agent-prompt Tier-1 passive library. When true, agent-prompt pages are retrievable via `recall(type='wiki', tags=['agent-prompt'])` and the save/dispatch surface is active. Pull-only (no auto-injection). |

---

## Logging

| Key | Env var | Type | Default | Choices | Description |
|---|---|---|---|---|---|
| `core_log_level` | `YADGAR_CORE_LOG_LEVEL` | str | `warn` | `DEBUG`,`INFO`,`WARNING`,`ERROR`,`CRITICAL` | Log level for the core yadgar MCP server. |
| `backend_log_level` | `YADGAR_BACKEND_LOG_LEVEL` | str | `warn` | `debug`,`info`,`warn`,`error` | Log level for the backend container (embed service + SurrealDB). |
| `log_format` | `YADGAR_LOG_FORMAT` | str | `json` | `json`,`text`,`human` | Log output format. `json` = one JSON object per line (`timestamp`, `level`, `logger`, `message`, `extra=` fields); `human`/`text` = human-readable. Default flipped `human`→`json` in v5.4.2 for structured log ingest. |

---

## Observability (OTLP / Tracing)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `metrics_enabled` | `YADGAR_METRICS_ENABLED` | bool | `true` | Expose the `/metrics` Prometheus endpoint. Set `false`/`0` to return 404. Always unauthenticated (exempt from bearer-auth) — bind to loopback so only local scrapers reach it. *(no FIELD_META)* |
| `otlp_endpoint` | `YADGAR_OTLP_ENDPOINT` | str | `""` | OTLP/HTTP endpoint for Tempo trace export (e.g. `http://tempo:4318/v1/traces`). Empty = disabled. |
| `otlp_headers` | `YADGAR_OTLP_HEADERS` | str | `""` | Comma-separated `k=v` auth/tenant headers for the OTLP exporter. |
| `otlp_timeout_sec` | `YADGAR_OTLP_TIMEOUT_SEC` | int | `3` | OTLP exporter HTTP timeout in seconds. Short so a dead collector fails fast. |
| `otlp_insecure` | `YADGAR_OTLP_INSECURE` | bool | `true` | Reserved/no-op for the HTTP OTLP exporter — transport security is decided by the endpoint URL scheme (`http://` vs `https://`), not by this flag. Kept to avoid churning the three-way config sync. |

---

## Backend Cache

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `dbsize_cache_ttl_sec` | `YADGAR_DBSIZE_CACHE_TTL_SEC` | int | `60` | `/admin/dbsize` response cache TTL in seconds (`0` = disabled). |

---

## Backend Hot-Path Cache (CE + Embed LRU, backend v5.4.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `ce_cache_enabled` | `YADGAR_CE_CACHE_ENABLED` | bool | `true` | Enable the CE score LRU cache (false/0 = pre-v5.4.0 behaviour). |
| `embed_cache_enabled` | `YADGAR_EMBED_CACHE_ENABLED` | bool | `true` | Enable the embedding-vector LRU cache (false/0 = disabled). |
| `ce_cache_max_entries` | `YADGAR_CE_CACHE_MAX_ENTRIES` | int | `100000` | Maximum entries in the CE score LRU cache (`0` = disabled). |
| `embed_cache_max_entries` | `YADGAR_EMBED_CACHE_MAX_ENTRIES` | int | `100000` | Maximum entries in the embedding-vector LRU cache (`0` = disabled). |
| `cache_snapshot_interval_sec` | `YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC` | int | `600` | Interval (seconds) between periodic cache snapshots to disk. |
| `cache_snapshot_dir` | `YADGAR_CACHE_SNAPSHOT_DIR` | str | `/data/cache` | Directory for cache snapshot files (`ce.snap`, `embed.snap`). |

---

## Backend Model Preload Warm-Up (backend v5.5.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `model_preload` | `YADGAR_MODEL_PRELOAD` | bool | `true` | Preload rerank models (ce/nli/pair) in the background after startup. Set false to keep lazy-load behaviour. |
| `model_preload_delay_sec` | `YADGAR_MODEL_PRELOAD_DELAY_SEC` | int | `10` | Seconds to wait after startup before loading rerank models in the background. |

---

## Stats Cache (v5.51.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `stats_cache_ttl_s` | `YADGAR_STATS_CACHE_TTL_S` | int | `5` | `/api/stats` response TTL in seconds. `0` = recompute every request. Does not affect `/api/system`. |

---

## Wiki Staleness Cache (v5.53.1)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `stale_count_cache_ttl_s` | `YADGAR_STALE_COUNT_CACHE_TTL_S` | int | `300` | Seconds before `stale_wiki_count` is recomputed from disk scan. `0` = scan on every signals call (not recommended). |

---

## Hook Recall Latency Budget (v5.51.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `hook_recall_timeout_s` | `YADGAR_HOOK_RECALL_TIMEOUT_S` | float | `2.0` | Maximum seconds `asyncio.wait_for` waits for `retriever.recall` in hook handlers (prompt-recall, instructions-loaded, subagent-start). On timeout: WARN log + `yadgar_hook_recall_timeout_total` incremented + empty result. Raise to 5.0 if the counter rate is too high. |
| `hook_recall_pool_workers` | `YADGAR_HOOK_RECALL_POOL_WORKERS` | int | `2` | **SEPARATE pool just for hook auto-recalls** (SessionStart/UserPrompt), isolated so hook bursts cannot starve MCP tool calls (ADR-0025). Default 2 (ADR-0077): post-#166 the hook recall is a forwarded HTTP wait (idle thread, not a GIL-holding in-core recall); pool=1 starved the second of every concurrent session pair. Changing this does NOT affect `tool_pool_workers` — the two pools are independent. Lower back to 1 only if loop-lag returns on the `--cpus 1` core (`yadgar_event_loop_lag_max` + `yadgar_hook_recall_timeout_total` metrics). Restart to apply. |

---

## Tool-Body Offload Pool (v5.95.0)

Three knobs gate recall concurrency at three levels. **Effective recall concurrency = min(`tool_pool_workers`, `recall_heavy_concurrency`, `rerank_max_concurrency`).** Bumping only one gate does nothing if the others are lower. See also `rerank_max_concurrency` in the Circuit Breaker / Backend section below (backend-side cap).

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `tool_pool_workers` | `YADGAR_TOOL_POOL_WORKERS` | int | `2` | **Size of the offload ThreadPoolExecutor** — max MCP tool bodies (recall/memorize/wiki/…) running off the `--cpus 1` event loop at once. Offload threads compete with the event loop for the single core; fewer = less loop-starvation risk. v5.95: dropped 8→2. Must be strictly `> recall_heavy_concurrency` (else the rerank sub-gate is a no-op). Restart to apply. |
| `recall_heavy_concurrency` | `YADGAR_RECALL_HEAVY_CONCURRENCY` | int | `1` | **Sub-gate INSIDE the pool**: max concurrent HEAVY recalls (rerank fan-out) the core issues at once. Clamped at runtime to ≤ `tool_pool_workers`. Protects the backend from too many simultaneous rerank waves (#74). v5.95: dropped 3→1. Must be strictly `< tool_pool_workers` or this gate is a no-op. Must be ≤ `rerank_max_concurrency`. |
| `offload_tools` | `YADGAR_OFFLOAD_TOOLS` | bool | `false` | Master switch for tool-body offload off the asyncio loop. **v5.95: now config.yaml-authoritative** — `offload_tools: true` arms the offload path (previously env-only, so a yaml value was silently ignored, #72). Default OFF: arming is **unvalidated live** — soak before relying on it; disarm via `offload_tools: false`. |
| `tool_timeout_sec` | `YADGAR_TOOL_TIMEOUT_SEC` | float | `95.0` | Per-tool offload timeout. Must cover a worst-case recall including backend rerank. |
| `rerank_gate_acquire_timeout_sec` | `YADGAR_RERANK_GATE_ACQUIRE_TIMEOUT_SEC` | float | `2.0` | Seconds a worker waits for a heavy-rerank slot before degrading (skip rerank). |
| `tool_saturation_grace_sec` | `YADGAR_TOOL_SATURATION_GRACE_SEC` | float | `120.0` | O2: idle seconds while the pool is full before `/health` degrades to 503. Must be `> tool_timeout_sec`. |

---

## Hot-path tunables (v5.95.0 config-integrity Phase 4)

Operational literals promoted from hardcoded values to config.yaml-authoritative knobs so they can be tuned without a rebuild.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `reranker_idle_unload_sec` | `YADGAR_RERANKER_IDLE_UNLOAD_SEC` | float | `600.0` | Idle seconds of no recall activity before rerank models are unloaded to free ~500 MB. |
| `reranker_idle_check_interval_sec` | `YADGAR_RERANKER_IDLE_CHECK_INTERVAL_SEC` | int | `60` | Seconds between reranker idle-unload checks (background thread sleep interval). |
| `health_handler_timeout_sec` | `YADGAR_HEALTH_HANDLER_TIMEOUT_SEC` | float | `3.0` | Outer hard bound (s) on the whole `/health` handler body. Keep below the container `--health-timeout 5s`. |
| `health_probe_timeout_sec` | `YADGAR_HEALTH_PROBE_TIMEOUT_SEC` | float | `2.0` | Per-dependency (db/embed) probe HTTP client timeout inside `/health`. Keep `< health_handler_timeout_sec`. |
| `vacuum_auto_cooldown_hours` | `YADGAR_VACUUM_AUTO_COOLDOWN_HOURS` | float | `6.0` | Auto-vacuum cooldown: hours since the last auto-fire before another may trigger. In-memory, resets on restart. |

---

## Wiki Write Wait / Read-Your-Writes (v5.41.2)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `wiki_write_wait_timeout_seconds` | `YADGAR_WIKI_WRITE_WAIT_TIMEOUT_SECONDS` | float | `5.0` | Maximum seconds `wiki_add(wait=True)` may block before returning a timeout error. Only applies to the opt-in `wait=True` path; the default async path is unaffected. |

---

## Wiki Similarity Gate (v5.39.0)

| Key | Env var | Type | Default | Choices | Description |
|---|---|---|---|---|---|
| `wiki_sim_gate_enabled` | `YADGAR_WIKI_SIM_GATE_ENABLED` | bool | `true` | — | Enable the `wiki_add` similarity gate. Set false to disable entirely. |
| `wiki_sim_content_threshold` | `YADGAR_WIKI_SIM_CONTENT_THRESHOLD` | float | `0.80` | — | Minimum cosine similarity on the combined (title+content) embedding to flag a duplicate. Calibrated on `all-MiniLM-L6-v2`: near-clones ~0.91–0.95, distinct pages ~0.50–0.65. Lower to 0.70 for a stricter gate; raise toward 0.90 to reduce false positives. |
| `wiki_sim_title_threshold` | `YADGAR_WIKI_SIM_TITLE_THRESHOLD` | float | `0.85` | — | Reserved: minimum cosine on the title-only embedding. Currently unused (single combined embedding stored); a future schema upgrade will activate it. |
| `wiki_sim_mode` | `YADGAR_WIKI_SIM_MODE` | str | `hard` | `hard`,`soft` | Gate enforcement mode: `hard` rejects duplicate creates; `soft` logs a WARNING but allows the write. |
| `wiki_sim_top_k` | `YADGAR_WIKI_SIM_TOP_K` | int | `5` | — | Max candidate duplicate pages returned in the rejection response. |
| `wiki_embed_failure_blocks_write` | `YADGAR_WIKI_EMBED_FAILURE_BLOCKS_WRITE` | bool | `false` | — | When true, `wiki_add` fails if `_compute_embedding` returns None or raises. Default false: WARN log + metric, proceed with NULL embedding. |
| `directory_enforcement` | `YADGAR_DIRECTORY_ENFORCEMENT` | bool | `true` | — | When true, `wiki_add` rejects payloads missing `directory_context`. Set false as a migration escape hatch (emits WARN + `yadgar_writes_with_enforcement_relaxed` metric). |
| `branch_enforcement` | `YADGAR_BRANCH_ENFORCEMENT` | bool | `true` | — | When true, `wiki_add` and `memorize` reject payloads missing `branch`. Set false as a migration escape hatch (same WARN + metric). |

---

## Update Mechanism (v5.48.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `update_check_on_start` | `YADGAR_UPDATE_CHECK_ON_START` | bool | `false` | Opt-in: probe PyPI for a newer yadgar version on daemon start. Anonymous version-only check (no user-ID, no telemetry). Respects `HTTPS_PROXY`. |
| `update_check_timeout_seconds` | `YADGAR_UPDATE_CHECK_TIMEOUT_SECONDS` | int | `5` | HTTP timeout (seconds) for the PyPI version probe. |
| `update_pypi_url` | `YADGAR_UPDATE_PYPI_URL` | str | `https://pypi.org/pypi/yadgar/json` | PyPI JSON API endpoint for version probes. Override for air-gapped mirrors. |
| `update_user_agent_template` | `YADGAR_UPDATE_USER_AGENT_TEMPLATE` | str | `yadgar/{version}` | User-Agent template for version-probe requests; `{version}` is replaced at runtime. |
| `update_debug_apis_enabled` | `YADGAR_UPDATE_DEBUG_APIS_ENABLED` | str | `off` | Enable the `/api/control/update` endpoint. Set `on` for Control-tab integration (v5.50) or power-user CLI use. Also requires bearer-token auth. |
| `debug_apis_enabled` | `YADGAR_DEBUG_APIS_ENABLED` | bool | `false` | Umbrella gate for `/api/control/{config,action,restart}/*` endpoints. Bearer token alone is insufficient — this gate must also be true. |
| `update_snapshot_retention` | `YADGAR_UPDATE_SNAPSHOT_RETENTION` | int | `3` | Number of upgrade snapshots to retain after each upgrade. `0` = keep all (no pruning). |
| `update_install_enabled` | `YADGAR_UPDATE_INSTALL_ENABLED` | bool | `false` | Enable the `yadgar update --install` orchestrator. When false, `run_install()` refuses immediately. Read `docs/plans/archive/PLAN_V5_49_0.md` § Rollout before enabling. |
| `update_lock_max_age_seconds` | `YADGAR_UPDATE_LOCK_MAX_AGE_SECONDS` | int | `3600` | Maximum age (seconds) for an upgrade lock before it is treated as stale. Recovers from a killed upgrader process. |

---

## Memory Archive Retention (v5.49.0)

Auto-purge of expired `memory_archive` rows. Anchored memories (`_anchor` tag or `is_protected=True`) are never purged regardless of these settings.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `memory_archive_retention_days` | `YADGAR_MEMORY_ARCHIVE_RETENTION_DAYS` | int | `90` | Purge `memory_archive` rows whose `archived_at` exceeds this age (days). Set `0` to disable permanent deletion entirely. |
| `memory_archive_retention_circuit_breaker` | `YADGAR_MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER` | int | `500` | Maximum rows deleted in a single `purge_expired_archives()` call. Fires a CRITICAL log when the cap is hit. |
| `memory_archive_retention_thrash_guard_days` | `YADGAR_MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS` | int | `7` | Skip archives whose `created_at` is more recent than this many days ago. Prevents thrash-purging recently-created archives carrying an old `archived_at`. |

---

## Cold-Memory Retention (#29)

DRY-RUN visibility for cold immortal user memories. **By default this only reports — it deletes nothing.** A real delete requires BOTH `cold_memory_purge_enabled=true` AND `cold_memory_purge_dry_run=false`.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `cold_memory_retention_days` | `YADGAR_COLD_MEMORY_RETENTION_DAYS` | int | `90` | Age threshold (days) for cold user-memory retention candidates. Memories older than this with `heat<cold_threshold` and `access_count=0` are surfaced in the nightly report. `0` = disable candidate detection. |
| `cold_memory_purge_enabled` | `YADGAR_COLD_MEMORY_PURGE_ENABLED` | bool | `false` | Master gate for cold-memory hard deletes. When false the pass only logs candidates + emits a metric. |
| `cold_memory_purge_dry_run` | `YADGAR_COLD_MEMORY_PURGE_DRY_RUN` | bool | `true` | Dry-run gate. When true no memory is deleted even if `cold_memory_purge_enabled=true`. |

---

## Table Retention Windows

Rows older than these thresholds are pruned each consolidation cycle. Set to `0` to disable for a specific table. *(none have FIELD_META — defaults and descriptions from config.py comments)*

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `action_log_retention_days` | `YADGAR_ACTION_LOG_RETENTION_DAYS` | int | `7` | Prune processed `action_log` rows older than this each consolidation cycle. |
| `episode_retention_days` | `YADGAR_EPISODE_RETENTION_DAYS` | int | `14` | Prune `episode` rows older than this each consolidation cycle. |
| `action_stream_max_age_days` | `YADGAR_ACTION_STREAM_MAX_AGE_DAYS` | int | `14` | `_memify_prune` Pass 5 deletes unaccessed `_action_stream` memories older than this. `0` disables. |
| `auto_generated_memory_max_age_days` | `YADGAR_AUTO_GENERATED_MEMORY_MAX_AGE_DAYS` | int | `30` | Delete cold unaccessed `auto-generated` memories older than this. `0` disables. |
| `auto_abstracted_memory_max_age_days` | `YADGAR_AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS` | int | `30` | Delete cold unaccessed `auto-abstracted` memories (CLS promotions, action-stream pattern noise) older than this. `0` disables. |
| `dream_insight_max_age_days` | `YADGAR_DREAM_INSIGHT_MAX_AGE_DAYS` | int | `21` | Delete unaccessed dream memories older than this regardless of heat. `0` disables. |
| `narrative_entry_retention_days` | `YADGAR_NARRATIVE_ENTRY_RETENTION_DAYS` | int | `90` | Prune `narrative_entry` rows older than this. |
| `astrocyte_process_retention_days` | `YADGAR_ASTROCYTE_PROCESS_RETENTION_DAYS` | int | `7` | Prune `astrocyte_process` rows older than this. |
| `memory_cluster_retention_days` | `YADGAR_MEMORY_CLUSTER_RETENTION_DAYS` | int | `30` | Prune `memory_cluster` rows older than this. |
| `derived_belief_retention_days` | `YADGAR_DERIVED_BELIEF_RETENTION_DAYS` | int | `30` | Prune `derived_belief` rows older than this. |
| `prospective_memory_retention_days` | `YADGAR_PROSPECTIVE_MEMORY_RETENTION_DAYS` | int | `30` | Prune `prospective_memory` rows older than this. |
| `max_caused_by_rows` | `YADGAR_MAX_CAUSED_BY_ROWS` | int | `100000` | `caused_by` relationship ceiling — older rows pruned when this limit is exceeded. `0` disables the ceiling check. |
| `similarity_matrix_max_candidates` | `YADGAR_SIMILARITY_MATRIX_MAX_CANDIDATES` | int | `4000` | Cap on memories included in the N×N similarity matrix (`_link_similar_memories`/`_merge_duplicates`). Prevents OOM at scale. |
| `cls_pattern_max_candidates` | `YADGAR_CLS_PATTERN_MAX_CANDIDATES` | int | `2000` | Cap on the episodic-memory scan in `find_recurring_patterns`. |

---

## Async Write Queue / DLQ

*(none have FIELD_META — defaults and descriptions from config.py comments)*

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `data_dir` | `YADGAR_DATA_DIR` | str | `~/.local/share/yadgar` | Base directory for the async write queue and wiki archive files. |
| `wiki_slug_prefix` | `YADGAR_WIKI_SLUG_PREFIX` | str | `""` | Optional prefix for wiki `.md` archive filenames (e.g. `myproject` → `myproject-overview.md`). |
| `queue_drain_interval` | `YADGAR_QUEUE_DRAIN_INTERVAL` | int | `30` | Seconds queue entries stay visible before being flushed to the DB. |
| `queue_max_permanent_attempts` | `YADGAR_QUEUE_MAX_PERMANENT_ATTEMPTS` | int | `3` | 4xx failures → DLQ after this many tries. |
| `queue_max_transient_attempts` | `YADGAR_QUEUE_MAX_TRANSIENT_ATTEMPTS` | int | `20` | 5xx / network failures → DLQ after this many tries. |
| `queue_backoff_base_s` | `YADGAR_QUEUE_BACKOFF_BASE_S` | int | `30` | Initial retry delay in seconds. |
| `queue_backoff_max_s` | `YADGAR_QUEUE_BACKOFF_MAX_S` | int | `3600` | Maximum retry-delay cap. |
| `queue_dlq_retention_days` | `YADGAR_QUEUE_DLQ_RETENTION_DAYS` | int | `90` | Prune DLQ entries older than this. |
| `max_batch_statements` | `YADGAR_MAX_BATCH_STATEMENTS` | int | `500` | Per-transaction cap on SQL statements (avoids SurrealDB recursive-serialiser stack blow-ups). |
| `max_batch_bytes` | `YADGAR_MAX_BATCH_BYTES` | int | `1000000` | Per-transaction serialised-byte cap (prevents HTTP 413). Whichever limit fires first starts a new chunk. |

---

## Backend HTTP Timeouts & Circuit Breaker

*(none have FIELD_META — defaults and descriptions from config.py comments)*

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `backend_http_timeout_sec` | `YADGAR_BACKEND_HTTP_TIMEOUT_SEC` | int | `5` | Short timeout for all non-import backend calls (health, `/sql`, `/admin/dbsize`). |
| `rerank_backend_timeout_sec` | `YADGAR_RERANK_BACKEND_TIMEOUT_SEC` | int | `90` | Dedicated timeout for `/rerank` calls (CE inference can take 8–46 s on CPU). `0` falls back to `backend_http_timeout_sec`. |
| `backend_import_timeout_sec` | `YADGAR_BACKEND_IMPORT_TIMEOUT_SEC` | int | `300` | Long timeout for vacuum `/import` POST and `/export` GET (bulk data ops). |
| `migration_http_timeout_sec` | `YADGAR_MIGRATION_HTTP_TIMEOUT_SEC` | int | `30` | Timeout for schema-migration HTTP calls during `StorageEngine.__init__`. |
| `check_invariants_query_timeout_seconds` | `YADGAR_CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS` | int | `60` | Per-table query timeout for `check_invariants`. On timeout the table is skipped (WARN) and the rest still run. |
| `db_size_warning_bytes` | `YADGAR_DB_SIZE_WARNING_BYTES` | int | `1073741824` | Warn when total `surreal_db/` size exceeds this (default 1 GiB; fires at most once per hour). |
| `circuit_breaker_enabled` | `YADGAR_CIRCUIT_BREAKER_ENABLED` | bool | `true` | N4 backend ML circuit breaker — opens when `/rerank` repeatedly times out/errors. |
| `circuit_breaker_failure_threshold` | `YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | int | `3` | Open the OPEN state after this many consecutive per-endpoint failures. |
| `circuit_breaker_open_duration_sec` | `YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC` | int | `60` | Stay OPEN this many seconds before allowing a single probe attempt. |
| `circuit_breaker_probe_timeout_sec` | `YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC` | float | `2.0` | Short HTTP timeout for HALF_OPEN probe calls. |
| `circuit_breaker_max_open_duration_sec` | `YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC` | float | `600.0` | Maximum cooldown ceiling for exponential backoff on repeated probe failures. |
| `circuit_breaker_backoff_factor` | `YADGAR_CIRCUIT_BREAKER_BACKOFF_FACTOR` | float | `2.0` | Backoff multiplier — each failed probe multiplies cooldown by this factor. |
| `rerank_max_concurrency` | `YADGAR_RERANK_MAX_CONCURRENCY` | int | `8` | **BACKEND cross-encoder cap**: max concurrent `/rerank` inference threads the backend serves at once (#74 backend-saturation guard). Independent of the core pool — read by the **backend** container; needs a backend restart/env change to take effect. Raised from 1 in lockstep with `tool_pool_workers` (Fix A O7) so N-parallel core offload does not cause rerank 503-storms. Part of the three-way gate: effective recall concurrency = min(`tool_pool_workers`, `recall_heavy_concurrency`, `rerank_max_concurrency`). |
| `rerank_semaphore_acquire_timeout_sec` | `YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` | float | `2.0` | Seconds to wait for the semaphore before returning 503. Should be ≤ `circuit_breaker_probe_timeout_sec`. |
| `asgi_shutdown_timeout_sec` | `YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC` | int | `5` | Caps uvicorn's wait for in-flight requests to drain on SIGTERM. `0` = unlimited. |

---

## Vacuum

*(none have FIELD_META — defaults and descriptions from config.py comments)*

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `vacuum_old_max_age_days` | `VACUUM_OLD_MAX_AGE_DAYS` | int | `7` | Age backstop for surreal_db.old-* rollback dirs (ADR-0076 D1): reap any .old dir older than this many days on each vacuum finalize. The current-run .old is always exempted. |
| `vacuum_snapshot_retention` | `YADGAR_VACUUM_SNAPSHOT_RETENTION` | int | `3` | Number of pre-vacuum DB snapshots to retain. |
| `vacuum_auto_enabled` | `YADGAR_VACUUM_AUTO_ENABLED` | bool | `true` | Enable the backstop threshold trigger (emergency backstop only from v5.7.0; nightly cron is primary). |
| `vacuum_auto_threshold_bytes` | `YADGAR_VACUUM_AUTO_THRESHOLD_BYTES` | int | `2147483648` | Backstop fires when the DB exceeds this size (default 2 GiB). |
| `vacuum_auto_window_start` | `YADGAR_VACUUM_AUTO_WINDOW_START` | str (HH:MM) | `19:00` | Local-time start of the backstop trigger window (24-hour, validated `HH:MM`). |
| `vacuum_auto_window_end` | `YADGAR_VACUUM_AUTO_WINDOW_END` | str (HH:MM) | `23:00` | Local-time end of the backstop trigger window (exclusive, validated `HH:MM`). |
| `vacuum_side_launcher` | `YADGAR_VACUUM_SIDE_LAUNCHER` | str | `auto` | Which side-build launcher Phase 3 uses for its throwaway SurrealDB (task 0107): `auto` (host binary first, container second, SKIP third), `host` (host binary only, fails loud rather than falling through), or `container` (container only, ignoring any resolvable host binary). |

---

## CPU Burst Detection (v5.15.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `phase_duration_warn_ms` | `YADGAR_PHASE_DURATION_WARN_MS` | int | `60000` | Consolidation-phase duration warn threshold (ms). When any `_consolidation_cycle()` phase exceeds this, a CRITICAL log is emitted. `0` disables. |

---

## Visualization Knobs (v5.11.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `viz_health_refresh_sec` | `YADGAR_VIZ_HEALTH_REFRESH_SEC` | float | `5.0` | How often the `viz_daemon_health` background scraper refreshes daemon metrics. *(no FIELD_META)* |
| `viz_node_size_3d` | `YADGAR_VIZ_NODE_SIZE_3D` | float | `8.0` | 3D node sphere radius (`nodeRelSize`). |
| `viz_node_size_2d` | `YADGAR_VIZ_NODE_SIZE_2D` | float | `4.0` | 2D canvas node base radius. |
| `viz_heat_hue_start` | `YADGAR_VIZ_HEAT_HUE_START` | int | `240` | Heat colour hue at h=0 (cool end, blue). |
| `viz_heat_hue_end` | `YADGAR_VIZ_HEAT_HUE_END` | int | `0` | Heat colour hue at h=1 (hot end, red). |
| `viz_heat_sat_base` | `YADGAR_VIZ_HEAT_SAT_BASE` | int | `60` | Heat colour saturation base %. |
| `viz_heat_sat_gain` | `YADGAR_VIZ_HEAT_SAT_GAIN` | int | `30` | Heat colour saturation gain %. |
| `viz_heat_light_base` | `YADGAR_VIZ_HEAT_LIGHT_BASE` | int | `40` | Heat colour lightness base %. |
| `viz_heat_light_gain` | `YADGAR_VIZ_HEAT_LIGHT_GAIN` | int | `20` | Heat colour lightness gain %. |
| `viz_cat_color_architecture` | `YADGAR_VIZ_CAT_COLOR_ARCHITECTURE` | str | `#58a6ff` | Wiki category colour: architecture. |
| `viz_cat_color_decision` | `YADGAR_VIZ_CAT_COLOR_DECISION` | str | `#ffa657` | Wiki category colour: decision. |
| `viz_cat_color_pattern` | `YADGAR_VIZ_CAT_COLOR_PATTERN` | str | `#3fb950` | Wiki category colour: pattern. |
| `viz_cat_color_debugging` | `YADGAR_VIZ_CAT_COLOR_DEBUGGING` | str | `#f85149` | Wiki category colour: debugging. |
| `viz_cat_color_reference` | `YADGAR_VIZ_CAT_COLOR_REFERENCE` | str | `#8b949e` | Wiki category colour: reference. |
| `viz_cat_color_convention` | `YADGAR_VIZ_CAT_COLOR_CONVENTION` | str | `#d2a8ff` | Wiki category colour: convention. |
| `viz_cat_color_fact` | `YADGAR_VIZ_CAT_COLOR_FACT` | str | `#a5d6ff` | Wiki category colour: fact. |
| `viz_cat_color_analysis` | `YADGAR_VIZ_CAT_COLOR_ANALYSIS` | str | `#d29922` | Wiki category colour: analysis. |
| `viz_edge_color_semantic` | `YADGAR_VIZ_EDGE_COLOR_SEMANTIC` | str | `#1f6feb` | Edge colour: semantic. |
| `viz_edge_color_temporal` | `YADGAR_VIZ_EDGE_COLOR_TEMPORAL` | str | `#6e40c9` | Edge colour: temporal. |
| `viz_edge_color_transition` | `YADGAR_VIZ_EDGE_COLOR_TRANSITION` | str | `#3fb950` | Edge colour: transition. |
| `viz_edge_color_wiki_crossref` | `YADGAR_VIZ_EDGE_COLOR_WIKI_CROSSREF` | str | `#d2a8ff` | Edge colour: wiki_crossref. |
| `viz_edge_color_memory_wiki` | `YADGAR_VIZ_EDGE_COLOR_MEMORY_WIKI` | str | `#ffa657` | Edge colour: memory_wiki. |
| `viz_edge_width_3d_multiplier` | `YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER` | float | `1.8` | 3D edge width multiplier over the 2D base (Variant C). |
| `viz_edge_arrow_len` | `YADGAR_VIZ_EDGE_ARROW_LEN` | int | `5` | Arrow length for directional edge types. |
| `viz_edge_opacity` | `YADGAR_VIZ_EDGE_OPACITY` | float | `0.9` | Link opacity for all edges (Variant C). |
| `viz_edge_variant` | `YADGAR_VIZ_EDGE_VARIANT` | str | `C` | Informational: edge style variant in use. |
| `viz_wiki_shape` | `YADGAR_VIZ_WIKI_SHAPE` | str | `octahedron` | Desired shape for wiki nodes (config only; renderer not wired pending v5.10.7.3). |
| `viz_physics_charge_strength` | `YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH` | float | `-18.0` | D3 charge (repulsion) strength. |
| `viz_physics_link_distance_2d` | `YADGAR_VIZ_PHYSICS_LINK_DISTANCE_2D` | float | `30` | D3 link distance in 2D mode. |
| `viz_physics_link_distance_3d` | `YADGAR_VIZ_PHYSICS_LINK_DISTANCE_3D` | float | `36` | D3 link distance in 3D mode. |
| `viz_layout_zoom_fit_tick` | `YADGAR_VIZ_LAYOUT_ZOOM_FIT_TICK` | int | `80` | Engine tick threshold to trigger auto-zoom-fit. |
| `viz_layout_zoom_fit_padding` | `YADGAR_VIZ_LAYOUT_ZOOM_FIT_PADDING` | int | `50` | Padding (px) passed to `zoomToFit()`. |
| `viz_layout_zoom_fit_transition_ms` | `YADGAR_VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS` | int | `800` | Transition duration (ms) for `zoomToFit()`. |
| `viz_search_match_color` | `YADGAR_VIZ_SEARCH_MATCH_COLOR` | str | `#ffffff` | Stroke colour for search-matched nodes. |
| `viz_search_pinned_color` | `YADGAR_VIZ_SEARCH_PINNED_COLOR` | str | `#ffd700` | Stroke colour for pinned nodes. |
| `viz_search_dim_opacity` | `YADGAR_VIZ_SEARCH_DIM_OPACITY` | float | `0.18` | Opacity for non-matched dimmed nodes. |
| `viz_max_memories` | `YADGAR_VIZ_MAX_MEMORIES` | int | `0` | Max memory nodes in the `/api/graph` payload (`0` = unlimited default; any positive N caps to N). |
| `viz_max_wiki` | `YADGAR_VIZ_MAX_WIKI` | int | `0` | Max wiki nodes in the `/api/graph` payload (`0` = unlimited default; any positive N caps to N). |
| `viz_max_entities` | `YADGAR_VIZ_MAX_ENTITIES` | int | `0` | Max entity nodes in the `/api/graph` payload (`0` = unlimited default; any positive N caps to N). |
| `viz_layout_iterations` | `YADGAR_VIZ_LAYOUT_ITERATIONS` | int | `50` | `spring_layout` iteration cap for the (unconditional) precomputed layout (lower = faster/looser). |
| `viz_max_transitions` | `YADGAR_VIZ_MAX_TRANSITIONS` | int | `0` | Max transition (co-recall) edges in `/api/graph` (`0`/`-1` = unlimited; ordered by count). |
| `viz_max_wiki_crossrefs` | `YADGAR_VIZ_MAX_WIKI_CROSSREFS` | int | `0` | Max wiki cross-reference edges in `/api/graph` (`0`/`-1` = unlimited). |
| `viz_max_causal_edges` | `YADGAR_VIZ_MAX_CAUSAL_EDGES` | int | `0` | Max PC-algorithm causal edges in `/api/graph` (`0`/`-1` = unlimited; ordered by confidence). |
| `viz_max_relationships` | `YADGAR_VIZ_MAX_RELATIONSHIPS` | int | `0` | Max entity typed-relation edges in `/api/graph` (`0`/`-1` = unlimited; ordered by weight). |
| `viz_max_similarity_links` | `YADGAR_VIZ_MAX_SIMILARITY_LINKS` | int | `0` | Max memory_similarity_link edges in `/api/graph` (`0`/`-1` = unlimited; ordered by weight). |

---

## Security (v5.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `require_auth` | `YADGAR_REQUIRE_AUTH` | bool | `true` | Enforce bearer-token auth on `/api/*` and `/hooks/*` routes. When false, the middleware is a no-op (logs WARN at startup). |
| `mcp_auth_token` | `YADGAR_MCP_AUTH_TOKEN` | str | `""` | Bearer token clients must present. Must be set when `require_auth=true`. Generate via `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `allowed_origins` | `YADGAR_ALLOWED_ORIGINS` | str | `http://127.0.0.1:8765,http://localhost:8765` | Comma-separated CORS allowed origins. Loopback only by default; wildcard (`*`) is never allowed. |
| `max_hash_bytes` | `YADGAR_MAX_HASH_BYTES` | int | `10485760` | Maximum file size (bytes) for path-based `memorize` hashing. Files exceeding this are skipped. Default 10 MiB. |
| `auto_capture_rate_limit` | `YADGAR_AUTO_CAPTURE_RATE_LIMIT` | int | `30` | Max requests per directory key per minute to `/hooks/auto-capture`. Prevents log-flooding from misbehaving hooks. |

**Database credentials** (set in `~/.config/yadgar/secrets.env`, loaded via `EnvironmentFile=` in systemd):

| Env var | Required | Description |
|---------|----------|-------------|
| `YADGAR_DB_USER` | Yes (server mode) | SurrealDB username for the core container. |
| `YADGAR_DB_PASS` | Yes (server mode) | SurrealDB password. Raises `KeyError` at startup if unset (unless `YADGAR_ALLOW_ROOT=1`). |
| `YADGAR_ALLOW_ROOT` | No | Set to `1` in test/CI environments to bypass the DB credential requirement. **Never set in production.** |

See `MIGRATION_NOTES.md` for the step-by-step deployment procedure.

---

## Database Schema

These fields exist on the SurrealDB tables but have no corresponding env var or config key — they are written by storage helpers and read by queries.

### `memory` table

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `branch` | `option<string>` | `NONE` | Git branch captured at write time. `NONE` for pre-v5 rows before backfill or non-git contexts. After migration 004 all pre-v5 rows are set to `'master'`. |

### `wiki_page` table

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `branch` | `option<string>` | `NONE` | Git branch captured at write time. Same semantics as `memory.branch`. |

---

## §25 Branch tagging (v5.0 Stage 10)

### Write-path auto-capture

Every write call (`memorize`, `anchor`, `checkpoint`, `wiki_add`) auto-captures the current git branch via `_detect_branch(directory)` before enqueueing. The branch is stored in the `branch` field on `memory` and `wiki_page` rows. Non-git directories and detached HEAD states result in `branch = NONE`.

`_detect_branch` is LRU-cached with a **30-second TTL** (time-bucket trick via `functools.lru_cache`). The 30-second window is hardcoded — there is no env-var to tune it.

`_get_default_branch` is LRU-cached with a **5-minute TTL** (same mechanism). Also hardcoded.

Auto-capture failure is non-fatal: if git detection raises for any reason, the memory is still stored with `branch = NONE`.

### Retrieval filter

`recall()` and `wiki_query()` apply a branch filter post-retrieval:

```
branch IN (current_branch, default_branch, NONE)
```

Memories/pages on unrelated branches are excluded. When `current_branch` is `None` (non-git working directory), the filter degenerates to `branch IN (default_branch, NONE)`.

**Branch score boost**: results where `branch == current_branch` receive a convex-combination boost (`branch_boost_weight`, default `0.2`), then the result list is re-sorted. This surfaces feature-branch context ahead of default-branch context.

### `wiki_read` resolution order

`wiki_read(slug)` resolves the slug in order:

1. `branch = current_branch` (exact match)
2. `branch = default_branch` (exact match)
3. `branch IS NONE` (legacy/canonical)
4. Not found → error dict

Default branch is detected via `git symbolic-ref refs/remotes/origin/HEAD`, falling back to `"master"`.

---

## Docker configuration

When running in Docker, inject settings via environment variables — no image rebuild needed:

```yaml
# docker-compose.yml
services:
  yadgar:
    image: yadgar
    environment:
      - YADGAR_RETRIEVAL_PROFILE=fast
      - YADGAR_DECAY_FACTOR=0.999
      - YADGAR_COLD_THRESHOLD=0.05
      # Container installs typically set YADGAR_DATA_DIR=/data and
      # YADGAR_CONFIG_FILE=/data/config.yaml so all state lives on one volume.
      - YADGAR_DATA_DIR=/data
      - YADGAR_CONFIG_FILE=/data/config.yaml
    volumes:
      - yadgar-data:/data
      # OR: mount your own config.yaml at the path YADGAR_CONFIG_FILE points to
      - ./my-config.yaml:/data/config.yaml:ro
```

Environment variables override the YAML file. `YADGAR_CONFIG_FILE` lets the
container read the config from the bind-mounted `/data` volume rather than the
container's home directory.

---

## §22 project_brief — layered bootstrap (v5.0)

### Config values

See the **Project Brief** section above for `project_init_cap_chars` and `brief_mode_default`.

### MCP tools

#### `project_brief(directory, mode="catalog") → dict`

Returns a structured project context snapshot.

**mode="catalog"** (default, ~500 tokens):

| Field | Type | Description |
|-------|------|-------------|
| `_resolved_directory` | str | Git-resolved project root (walks up via `git rev-parse --show-toplevel`). |
| `_mode` | str | `"catalog"` |
| `project` | str | Project name (last path component of resolved root). |
| `tech` | list[str] | Detected tech stack (stub: `[]` until scan is wired). |
| `branch` | str\|None | Current git branch (`git rev-parse --abbrev-ref HEAD`). |
| `init_memory_present` | bool | Whether a `_project_init` memory exists for this directory. |
| `active_work_present` | bool | Whether an `_active_work` memory exists for this directory. |
| `top_anchors` | list[dict] | Up to 5 most-accessed `_anchor` memories: `{id, title, tags, access_count}`. |
| `recent_episode_count` | int | Count of episodic memories created in last 24h for this directory. |
| `stale_wiki_count` | int | Count of wiki pages with hash drift. |

**mode="full"** (opt-in, ~1050 tokens): everything from catalog, plus:

| Field | Type | Description |
|-------|------|-------------|
| `init_memory` | str\|None | Full `_project_init` content if present. |
| `active_work` | str\|None | Full `_active_work` content if present. |
| `hot_memories` | list[dict] | Top 10 memories by heat: `{id, content[:200], heat, tags}`. |
| `key_wiki_pages` | list[dict] | 5 most recently updated wiki pages: `{slug, title, access_count}`. |

#### `bootstrap_project(directory, content) → dict` — `power=True`

Create or replace the `_project_init` memory for a directory atomically.

- **Hard cap**: content must be ≤ `project_init_cap_chars` (2000) characters. Raises `ValueError` on overflow.
- **Idempotent**: deletes all existing `_project_init` memories for this directory before inserting the new one.
- **Tag set**: `["_project_init", "_anchor"]`, `store_type=semantic`, `is_protected=True`.
- Returns the new memory dict.

#### `update_active_work(directory, content) → dict` — `power=True`

Replace the `_active_work` memory for a directory atomically (delete-then-insert in one TX).

- **No char cap** — markdown of any size is accepted.
- **Tag set**: `["_active_work"]`, `store_type=episodic`, `is_protected=True`.
- Returns `{previous_content: str | None, new_memory: dict}`.

---

## §26 wiki_refresh_stale + wiki_cleanup_merged_branches (v5.0)

### MCP tools

#### `wiki_refresh_stale(directory, slugs=None, force_branch=False) → dict` — `power=True`

Detect stale repo-wiki pages (`.local-review/wiki/*.md`) and signal for regeneration.

**Stale detection**: reads YAML frontmatter `hash` field and `source_files` list. Computes fresh SHA256 over all listed source files; if the hash differs, the page is stale.

**Master-only enforcement**: refuses on non-default branches unless `force_branch=True`. Detects default branch via `git symbolic-ref refs/remotes/origin/HEAD`.

**Queue file**: when drift is found, writes a JSON file to `.local-review/wiki/refresh-queue/<timestamp>.json` listing the stale slugs. Actual regeneration is done by a background Agent running `/repo-wiki update`.

**Never raises** — all errors are caught; returns `{"stale": [], "error": "..."}` on internal failure.

Returns:
```json
{
  "stale": ["mod-server", "mod-storage"],
  "dispatched_agent_id": null,
  "branch": "master",
  "skipped_reason": null
}
```

`skipped_reason` is `"not_default_branch"` when enforcement kicks in.

#### `wiki_cleanup_merged_branches(directory, dry_run=True) → dict` — `power=True`

List (and optionally delete) `wiki_page` rows whose `branch` is no longer in `git branch -a`.

Pages with `branch IN (master, main, NULL)` are never candidates.

`dry_run=True` (default): returns candidates without deleting.
`dry_run=False`: deletes the listed pages and returns `deleted_count`.

Returns:
```json
{
  "candidates": [{"id": 42, "slug": "old-feat-page", "branch": "feat/merged-long-ago"}],
  "deleted_count": 0,
  "dry_run": true
}
```

### Queue drainer validation (Option Z)

The queue drainer applies the following checks to every `wiki_add` operation before inserting it into the DB:

| Check | Failure action |
|-------|---------------|
| `wiki_schema_version >= 2` | DLQ with `reason="schema_version_too_old"` |
| Required fields: `slug`, `title`, `content`, `category` | DLQ with `reason="missing_required_field: <field>"` |
| Content passes v4.9 degenerate filter | DLQ with `reason="degenerate_content"` |
| `branch` field absent | Filled with `"master"` |
| `confidence` field absent | Filled with `"medium"` |

Frontmatter shape expected by `wiki_refresh_stale`:
```yaml
---
wiki_schema_version: 2
slug: mod-server
title: Server module
hash: <sha256-hex>
source_files:
  - yadgar/server.py
---
```

---

## §27 Stop-hook expansion (v5.0)

`yadgar/hooks/stop-memory-checkpoint.py` fires a signal-evaluation prompt instead of a simple checkpoint prompt every 25 human messages.

**State file**: `~/.local/state/yadgar/stop-hook-state.json` — keyed by `session_id`, written atomically via `tmp + os.replace`.

**Prompt contents**: instructs the running session to call `project_brief()`, evaluate `signals.stale_wiki_count`, `active_work_present`, `init_memory` presence, and take action (repo-wiki regen, `update_active_work`, `bootstrap_project`, or `memorize/wiki_add`).

The hook is a **dumb pipe** — no Python signal detection, no Anthropic API calls. All evaluation happens via tool calls in the Claude session.

---

## §28 SessionStart hook pipe (v5.0)

### Hook: `yadgar/hooks/session-start-context.py`

Calls `GET /hooks/session-context?directory=<cwd>` with bearer token from `YADGAR_MCP_AUTH_TOKEN` env var. Prints the `text` field (rendered markdown) to stdout for injection into the session context.

Silently skips on daemon-down or any error.

### Endpoint: `GET /hooks/session-context`

**Auth-required** (bearer token via `YADGAR_MCP_AUTH_TOKEN`).

| Query param | Description |
|-------------|-------------|
| `directory` | Project directory (defaults to cwd) |
| `mode` | Brief mode: `"catalog"` (default) or `"full"` |

Calls `project_brief(directory, mode=mode)` and returns the `_render` markdown field:

```json
{"text": "# yadgar — catalog\n\n**Branch:** master\n..."}
```

Returns `{"text": ""}` gracefully on any error (storage down, DB unavailable).

The `_render` field is a markdown string suitable for direct injection into a Claude session. Contents:
- Project name, branch, mode
- Signals (stale_wiki_count, init_memory, active_work presence)
- Top anchors list
- In `full` mode: init_memory content, active_work content
