# Configuration Reference

Yadgar is configured via three mechanisms, applied in priority order:

1. **Environment variables** — prefix `YADGAR_`, e.g. `YADGAR_DECAY_FACTOR=0.999`
2. **YAML config file** — `~/.yadgar/config.yaml`
3. **Built-in defaults** — shown in this document

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
| `db_path` | `YADGAR_DB_PATH` | str | `~/.yadgar/surreal_db` | SurrealDB storage path |
| `port` | `YADGAR_PORT` | int | `8765` | HTTP server port (daemon mode) |
| `embedding_model` | `YADGAR_EMBEDDING_MODEL` | str | `all-MiniLM-L6-v2` | Sentence-transformer model name |
| `max_episode_tokens` | `YADGAR_MAX_EPISODE_TOKENS` | int | `50000` | Maximum tokens per episode chunk |
| `overlap_tokens` | `YADGAR_OVERLAP_TOKENS` | int | `2000` | Token overlap between episode chunks |

---

## Daemon / Background Processing

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `idle_threshold_seconds` | `YADGAR_IDLE_THRESHOLD_SECONDS` | int | `300` | Idle seconds before triggering consolidation |
| `daemon_check_interval` | `YADGAR_DAEMON_CHECK_INTERVAL` | int | `30` | Seconds between background loop wakeups |
| `num_astrocyte_processes` | `YADGAR_NUM_ASTROCYTE_PROCESSES` | int | `4` | Domain-aware background worker count |
| `narrative_interval_hours` | `YADGAR_NARRATIVE_INTERVAL_HOURS` | int | `24` | Hours between autobiographical narrative updates |

---

## Memory Lifecycle

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `write_gate_threshold` | `YADGAR_WRITE_GATE_THRESHOLD` | float | `0.0` | Minimum novelty score to store a memory (0.0 = store everything) |
| `write_gate_continuity_discount` | `YADGAR_WRITE_GATE_CONTINUITY_DISCOUNT` | float | `0.15` | Threshold reduction for task-continuous content |
| `write_gate_continuity_window` | `YADGAR_WRITE_GATE_CONTINUITY_WINDOW` | int | `10` | Recent store window size for continuity detection |
| `compression_gist_age_hours` | `YADGAR_COMPRESSION_GIST_AGE_HOURS` | float | `168.0` | Hours before gist-compressing old memories (default = 7 days) |
| `compression_tag_age_hours` | `YADGAR_COMPRESSION_TAG_AGE_HOURS` | float | `720.0` | Hours before tag-compressing very old memories (default = 30 days) |
| `decision_auto_protect` | `YADGAR_DECISION_AUTO_PROTECT` | bool | `true` | Auto-protect detected decisions from decay |
| `action_stream_enabled` | `YADGAR_ACTION_STREAM_ENABLED` | bool | `true` | Capture tool actions in sensory buffer |
| `micro_checkpoint_enabled` | `YADGAR_MICRO_CHECKPOINT_ENABLED` | bool | `true` | Auto-checkpoint on significant events |
| `micro_checkpoint_cooldown` | `YADGAR_MICRO_CHECKPOINT_COOLDOWN` | int | `5` | Min tool calls between micro-checkpoints |
| `session_coherence_bonus` | `YADGAR_SESSION_COHERENCE_BONUS` | float | `0.2` | Heat bonus for memories from the current session |
| `session_coherence_window_hours` | `YADGAR_SESSION_COHERENCE_WINDOW_HOURS` | float | `4.0` | How long the session coherence bonus lasts |
| `reinjection_enabled` | `YADGAR_REINJECTION_ENABLED` | bool | `true` | Auto-surface related context when storing a memory |
| `reinjection_max_results` | `YADGAR_REINJECTION_MAX_RESULTS` | int | `3` | Max related memories to reinject on store |

---

## Memory Thermodynamics (Decay)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `decay_factor` | `YADGAR_DECAY_FACTOR` | float | `0.9995` | Per-hour heat decay multiplier (0.9995 ≈ 34% heat after 3 months no access) |
| `importance_decay_factor` | `YADGAR_IMPORTANCE_DECAY_FACTOR` | float | `0.9999` | Per-cycle decay for important memories (slower) |
| `cold_threshold` | `YADGAR_COLD_THRESHOLD` | float | `0.02` | Heat below which a memory is archived (~6 months no-access floor) |
| `action_stream_cold_threshold` | `YADGAR_ACTION_STREAM_COLD_THRESHOLD` | float | `0.1` | Heat below which an action-stream memory is archived (higher = expires faster) |
| `hot_threshold` | `YADGAR_HOT_THRESHOLD` | float | `0.0` | Minimum heat for hot-memory retrieval (0.0 = include all) |
| `project_context_min_heat` | `YADGAR_PROJECT_CONTEXT_MIN_HEAT` | float | `0.01` | Minimum heat for project context injection |
| `surprise_boost` | `YADGAR_SURPRISE_BOOST` | float | `0.3` | Heat boost applied to surprising/novel memories |
| `emotional_decay_resistance` | `YADGAR_EMOTIONAL_DECAY_RESISTANCE` | float | `0.5` | How much emotional salience slows decay (0–1) |
| `synaptic_window_minutes` | `YADGAR_SYNAPTIC_WINDOW_MINUTES` | int | `30` | Time window for synaptic boost propagation |
| `synaptic_boost` | `YADGAR_SYNAPTIC_BOOST` | float | `0.2` | Heat boost propagated from nearby high-importance memories |

---

## Retrieval & Fusion (WRRF)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `retrieval_profile` | `YADGAR_RETRIEVAL_PROFILE` | str | `balanced` | Preset: `fast`, `balanced`, or `full` |
| `wrrf_k` | `YADGAR_WRRF_K` | int | `60` | RRF constant k (higher = smoother rank blending) |
| `wrrf_candidate_multiplier` | `YADGAR_WRRF_CANDIDATE_MULTIPLIER` | int | `10` | Candidate pool = max_results × this |
| `wrrf_vector_weight` | `YADGAR_WRRF_VECTOR_WEIGHT` | float | `1.0` | Weight of vector similarity signal |
| `wrrf_fts_weight` | `YADGAR_WRRF_FTS_WEIGHT` | float | `0.5` | Weight of full-text search signal |
| `wrrf_ppr_weight` | `YADGAR_WRRF_PPR_WEIGHT` | float | `0.5` | Weight of personalized PageRank signal |
| `wrrf_spreading_weight` | `YADGAR_WRRF_SPREADING_WEIGHT` | float | `0.3` | Weight of spreading activation signal |
| `fusion_method` | `YADGAR_FUSION_METHOD` | str | `convex` | Fusion method: `convex` or other |
| `fusion_norm` | `YADGAR_FUSION_NORM` | str | `zscore` | Score normalisation: `zscore`, `minmax`, or `raw` |
| `combmnz_enabled` | `YADGAR_COMBMNZ_ENABLED` | bool | `false` | Multiply fused score by contributing signal count |

---

## Reranking

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `reranker_enabled` | `YADGAR_RERANKER_ENABLED` | bool | `true` | Enable cross-encoder reranking stage |
| `reranker_top_k` | `YADGAR_RERANKER_TOP_K` | int | `50` | Candidates passed to reranker |
| `cross_encoder_enabled` | `YADGAR_CROSS_ENCODER_ENABLED` | bool | `true` | Enable FlashRank ONNX cross-encoder |
| `cross_encoder_model` | `YADGAR_CROSS_ENCODER_MODEL` | str | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model name |
| `cross_encoder_top_k` | `YADGAR_CROSS_ENCODER_TOP_K` | int | `20` | Top-k passed to cross-encoder |
| `cross_encoder_weight` | `YADGAR_CROSS_ENCODER_WEIGHT` | float | `0.6` | Cross-encoder score weight (retrieval gets 1-this) |
| `gte_reranker_enabled` | `YADGAR_GTE_RERANKER_ENABLED` | bool | `true` | Enable GTE-Reranker (ModernBERT-based) |
| `gte_reranker_model` | `YADGAR_GTE_RERANKER_MODEL` | str | `Alibaba-NLP/gte-reranker-modernbert-base` | GTE reranker model name |
| `gte_reranker_max_length` | `YADGAR_GTE_RERANKER_MAX_LENGTH` | int | `512` | Max token length for GTE reranker |
| `gte_reranker_fallback_to_flashrank` | `YADGAR_GTE_RERANKER_FALLBACK_TO_FLASHRANK` | bool | `true` | Fall back to FlashRank if GTE fails |
| `nli_reranking_enabled` | `YADGAR_NLI_RERANKING_ENABLED` | bool | `true` | Enable NLI entailment scoring stage |
| `nli_model` | `YADGAR_NLI_MODEL` | str | `cross-encoder/nli-deberta-v3-base` | NLI model name |
| `nli_weight` | `YADGAR_NLI_WEIGHT` | float | `0.3` | NLI signal weight in final blend |
| `nli_only_for_open_domain` | `YADGAR_NLI_ONLY_FOR_OPEN_DOMAIN` | bool | `true` | Only apply NLI reranking for open-domain queries |
| `multi_passage_reranking_enabled` | `YADGAR_MULTI_PASSAGE_RERANKING_ENABLED` | bool | `true` | Enable multi-passage evidence aggregation |
| `multi_passage_cluster_overlap_threshold` | `YADGAR_MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD` | float | `0.3` | Overlap threshold for passage clustering |
| `multi_passage_max_cluster_size` | `YADGAR_MULTI_PASSAGE_MAX_CLUSTER_SIZE` | int | `3` | Maximum passages per evidence cluster |

---

## Query Routing

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `query_routing_enabled` | `YADGAR_QUERY_ROUTING_ENABLED` | bool | `true` | Enable automatic query routing to specialised retrievers |
| `query_expansion_enabled` | `YADGAR_QUERY_EXPANSION_ENABLED` | bool | `true` | Enable query expansion (pseudo-HyDE) |
| `comparison_dual_search_enabled` | `YADGAR_COMPARISON_DUAL_SEARCH_ENABLED` | bool | `true` | Run dual search for comparison queries |
| `comparison_top_k_per_option` | `YADGAR_COMPARISON_TOP_K_PER_OPTION` | int | `10` | Top-k results per option in comparison search |
| `temporal_keywords` | `YADGAR_TEMPORAL_KEYWORDS` | str | *(see config)* | Comma-separated keywords that trigger temporal routing |
| `code_keywords` | `YADGAR_CODE_KEYWORDS` | str | *(see config)* | Comma-separated keywords that trigger code-aware routing |
| `relational_keywords` | `YADGAR_RELATIONAL_KEYWORDS` | str | *(see config)* | Comma-separated keywords that trigger relational routing |

---

## Confidence Gating

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `confidence_gating_enabled` | `YADGAR_CONFIDENCE_GATING_ENABLED` | bool | `true` | Reject low-confidence result sets and trigger fallback |
| `confidence_min_results` | `YADGAR_CONFIDENCE_MIN_RESULTS` | int | `3` | Minimum results before gating is applied |
| `confidence_score_spread_threshold` | `YADGAR_CONFIDENCE_SCORE_SPREAD_THRESHOLD` | float | `0.15` | Min spread between top scores to be confident |
| `confidence_top_score_threshold` | `YADGAR_CONFIDENCE_TOP_SCORE_THRESHOLD` | float | `0.5` | Minimum top score to pass confidence gate |
| `confidence_fallback_strategy` | `YADGAR_CONFIDENCE_FALLBACK_STRATEGY` | str | `expand` | Strategy when gate fails: `expand` or `relax` |

---

## Temporal Retrieval

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `temporal_retrieval_enabled` | `YADGAR_TEMPORAL_RETRIEVAL_ENABLED` | bool | `true` | Boost memories matching temporal expressions in query |
| `temporal_boost_weight` | `YADGAR_TEMPORAL_BOOST_WEIGHT` | float | `0.4` | Weight of temporal boost signal |
| `temporal_decay_days` | `YADGAR_TEMPORAL_DECAY_DAYS` | int | `30` | Days over which temporal relevance decays |
| `temporal_exact_match_boost` | `YADGAR_TEMPORAL_EXACT_MATCH_BOOST` | float | `2.0` | Extra boost multiplier for exact date matches |

---

## Embedding Enhancement

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `candidate_pool_multiplier` | `YADGAR_CANDIDATE_POOL_MULTIPLIER` | int | `20` | Candidate pool = max_results × this before reranking |
| `embedding_cache_size` | `YADGAR_EMBEDDING_CACHE_SIZE` | int | `128` | LRU cache size for embeddings |
| `query_prefix` | `YADGAR_QUERY_PREFIX` | str | `""` | Optional prefix prepended to all queries before embedding |
| `dual_vectors_enabled` | `YADGAR_DUAL_VECTORS_ENABLED` | bool | `false` | Enable dual-vector architecture (explicit + implicit) |
| `implicit_embedding_model` | `YADGAR_IMPLICIT_EMBEDDING_MODEL` | str | `""` | Model for implicit/latent embedding channel |

---

## Graph & Knowledge

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `graph_max_hops` | `YADGAR_GRAPH_MAX_HOPS` | int | `2` | Maximum graph traversal hops |
| `graph_min_edge_weight` | `YADGAR_GRAPH_MIN_EDGE_WEIGHT` | float | `0.1` | Minimum edge weight to traverse |
| `graph_spreading_decay` | `YADGAR_GRAPH_SPREADING_DECAY` | float | `0.5` | Activation decay factor per hop |
| `graph_spreading_max_depth` | `YADGAR_GRAPH_SPREADING_MAX_DEPTH` | int | `2` | Maximum depth for spreading activation |
| `graph_entity_min_length` | `YADGAR_GRAPH_ENTITY_MIN_LENGTH` | int | `3` | Minimum character length for extracted entities |
| `causal_threshold` | `YADGAR_CAUSAL_THRESHOLD` | int | `3` | Min co-occurrence count before inferring causality |
| `ppr_damping` | `YADGAR_PPR_DAMPING` | float | `0.85` | Personalized PageRank damping factor |
| `ppr_iterations` | `YADGAR_PPR_ITERATIONS` | int | `50` | Number of PageRank iterations |
| `cluster_similarity_threshold` | `YADGAR_CLUSTER_SIMILARITY_THRESHOLD` | float | `0.7` | Min similarity to assign a memory to a cluster |

---

## Neuromorphic (Hopfield / HDC / SR)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `cognitive_load_limit` | `YADGAR_COGNITIVE_LOAD_LIMIT` | int | `4` | Max chunks in active context (Cowan's 4±1 rule) |
| `reconsolidation_low_threshold` | `YADGAR_RECONSOLIDATION_LOW_THRESHOLD` | float | `0.3` | Below this heat: no modification on recall |
| `reconsolidation_high_threshold` | `YADGAR_RECONSOLIDATION_HIGH_THRESHOLD` | float | `0.7` | Above this heat: archive old + create updated memory |
| `plasticity_spike` | `YADGAR_PLASTICITY_SPIKE` | float | `0.3` | Plasticity increase on each memory access |
| `plasticity_half_life_hours` | `YADGAR_PLASTICITY_HALF_LIFE_HOURS` | float | `6.0` | Plasticity decay half-life in hours |
| `stability_increment` | `YADGAR_STABILITY_INCREMENT` | float | `0.1` | Stability increase per successful retrieval |
| `excitability_half_life_hours` | `YADGAR_EXCITABILITY_HALF_LIFE_HOURS` | float | `6.0` | Engram excitability decay half-life in hours |
| `excitability_boost` | `YADGAR_EXCITABILITY_BOOST` | float | `0.5` | Excitability increase on engram slot activation |
| `dream_replay_pairs` | `YADGAR_DREAM_REPLAY_PAIRS` | int | `20` | Random memory pairs examined per dream replay cycle |
| `hopfield_beta` | `YADGAR_HOPFIELD_BETA` | float | `8.0` | Hopfield sharpness (low = blended recall, high = precise) |
| `hopfield_max_patterns` | `YADGAR_HOPFIELD_MAX_PATTERNS` | int | `5000` | Max patterns in Hopfield energy store |
| `sr_discount` | `YADGAR_SR_DISCOUNT` | float | `0.9` | Successor representation discount factor γ |
| `sr_update_rate` | `YADGAR_SR_UPDATE_RATE` | float | `0.1` | Incremental SR update learning rate |
| `fractal_levels` | `YADGAR_FRACTAL_LEVELS` | int | `3` | Fractal memory compression levels |

---

## Index-Time Enrichment

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `index_enrichment_enabled` | `YADGAR_INDEX_ENRICHMENT_ENABLED` | bool | `true` | Enable index-time memory enrichment pipeline |
| `enrichment_min_content_length` | `YADGAR_ENRICHMENT_MIN_CONTENT_LENGTH` | int | `20` | Minimum content length to run enrichment |
| `conceptnet_enrichment_enabled` | `YADGAR_CONCEPTNET_ENRICHMENT_ENABLED` | bool | `true` | Expand memories with ConceptNet relations |
| `conceptnet_min_edge_weight` | `YADGAR_CONCEPTNET_MIN_EDGE_WEIGHT` | float | `1.0` | Minimum ConceptNet edge weight to include |
| `conceptnet_max_terms` | `YADGAR_CONCEPTNET_MAX_TERMS` | int | `10` | Maximum ConceptNet terms to add per memory |
| `conceptnet_relations` | `YADGAR_CONCEPTNET_RELATIONS` | str | `IsA,UsedFor,HasProperty,...` | Comma-separated ConceptNet relations to use |
| `comet_enrichment_enabled` | `YADGAR_COMET_ENRICHMENT_ENABLED` | bool | `true` | Expand memories with COMET commonsense inference |
| `comet_model` | `YADGAR_COMET_MODEL` | str | `mismayil/comet-bart-ai2` | COMET model name |
| `comet_num_beams` | `YADGAR_COMET_NUM_BEAMS` | int | `5` | Beam search width for COMET generation |
| `comet_top_k_per_relation` | `YADGAR_COMET_TOP_K_PER_RELATION` | int | `3` | Top-k inferences per COMET relation |
| `comet_min_confidence` | `YADGAR_COMET_MIN_CONFIDENCE` | float | `0.3` | Minimum COMET inference confidence to include |
| `comet_relations` | `YADGAR_COMET_RELATIONS` | str | `xAttr,xIntent,xWant` | Comma-separated COMET relations to use |
| `comet_query_expansion_enabled` | `YADGAR_COMET_QUERY_EXPANSION_ENABLED` | bool | `false` | Apply COMET expansion at query time too |
| `doc2query_enrichment_enabled` | `YADGAR_DOC2QUERY_ENRICHMENT_ENABLED` | bool | `true` | Generate synthetic queries per memory (doc2query) |
| `doc2query_model` | `YADGAR_DOC2QUERY_MODEL` | str | `doc2query/msmarco-t5-small-v1` | Doc2query model name |
| `doc2query_num_queries` | `YADGAR_DOC2QUERY_NUM_QUERIES` | int | `5` | Synthetic queries to generate per memory |
| `logic_enrichment_enabled` | `YADGAR_LOGIC_ENRICHMENT_ENABLED` | bool | `true` | Enable formal logic pattern enrichment |
| `fpa_similarity_threshold` | `YADGAR_FPA_SIMILARITY_THRESHOLD` | float | `0.25` | Similarity threshold for first-principles analysis |

---

## Profiles & Beliefs

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `profile_extraction_enabled` | `YADGAR_PROFILE_EXTRACTION_ENABLED` | bool | `true` | Extract and maintain structured user profiles |
| `profile_confidence_direct` | `YADGAR_PROFILE_CONFIDENCE_DIRECT` | float | `0.7` | Confidence for directly stated profile attributes |
| `profile_confidence_inferred` | `YADGAR_PROFILE_CONFIDENCE_INFERRED` | float | `0.4` | Confidence for inferred profile attributes |
| `profile_summary_enabled` | `YADGAR_PROFILE_SUMMARY_ENABLED` | bool | `true` | Generate profile summaries |
| `derived_beliefs_enabled` | `YADGAR_DERIVED_BELIEFS_ENABLED` | bool | `true` | Derive higher-order beliefs from episodic memories |
| `belief_min_confidence` | `YADGAR_BELIEF_MIN_CONFIDENCE` | float | `0.3` | Minimum confidence to store a derived belief |
| `belief_high_confidence_boost` | `YADGAR_BELIEF_HIGH_CONFIDENCE_BOOST` | float | `1.2` | Score multiplier for high-confidence beliefs |
| `belief_search_priority_for_open_domain` | `YADGAR_BELIEF_SEARCH_PRIORITY_FOR_OPEN_DOMAIN` | bool | `true` | Prioritise beliefs for open-domain queries |

---

## Adversarial Protection

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `adversarial_detection_enabled` | `YADGAR_ADVERSARIAL_DETECTION_ENABLED` | bool | `true` | Detect and suppress adversarially-crafted memory injection |
| `adversarial_score_gap_threshold` | `YADGAR_ADVERSARIAL_SCORE_GAP_THRESHOLD` | float | `0.05` | Max acceptable score gap between top results |
| `adversarial_diversity_enforcement` | `YADGAR_ADVERSARIAL_DIVERSITY_ENFORCEMENT` | bool | `true` | Enforce result diversity to prevent manipulation |
| `adversarial_min_confidence` | `YADGAR_ADVERSARIAL_MIN_CONFIDENCE` | float | `0.3` | Minimum confidence required to surface a memory |

---

## Miscellaneous

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `contextual_prefix_enabled` | `YADGAR_CONTEXTUAL_PREFIX_ENABLED` | bool | `true` | Prepend contextual prefix to improve embedding quality |
| `curation_similarity_threshold` | `YADGAR_CURATION_SIMILARITY_THRESHOLD` | float | `0.95` | Minimum similarity to trigger memory merging (near-duplicates only) |
| `crdt_agent_id` | `YADGAR_CRDT_AGENT_ID` | str | `default` | Agent identifier for multi-agent CRDT sync |
| `replay_max_restore_memories` | `YADGAR_REPLAY_MAX_RESTORE_MEMORIES` | int | `8` | Max memories included in context restoration |
| `replay_anchor_heat` | `YADGAR_REPLAY_ANCHOR_HEAT` | float | `1.0` | Heat assigned to anchored (protected) memories |
| `replay_checkpoint_auto_interval` | `YADGAR_REPLAY_CHECKPOINT_AUTO_INTERVAL` | int | `50` | Auto-checkpoint every N tool calls |
| `data_dir` | `YADGAR_DATA_DIR` | str | `~/.yadgar` | Base directory for queue and archive files |
| `wiki_slug_prefix` | `YADGAR_WIKI_SLUG_PREFIX` | str | `""` | Optional prefix for wiki archive filenames |
| `queue_drain_interval` | `YADGAR_QUEUE_DRAIN_INTERVAL` | int | `30` | Drain interval in seconds for async write queue |

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
    volumes:
      - yadgar-data:/data
      # OR: mount your own config.yaml
      - ./my-config.yaml:/root/.yadgar/config.yaml:ro
```

The config file at `/root/.yadgar/config.yaml` inside the container is read at startup. Environment variables override it.
