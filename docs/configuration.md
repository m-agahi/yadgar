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
| `predictive_coding_entity_ttl_seconds` | `YADGAR_PREDICTIVE_CODING_ENTITY_TTL_SECONDS` | int | `300` | TTL (seconds) for the entity-set cache inside WriteGate. Avoids a `get_all_entities()` DB call on every write-gate evaluation. Set to `0` to disable caching. Invalidated immediately on entity add/delete via `invalidate_entity_cache()`. |
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

## Observability (v5.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `metrics_enabled` | `YADGAR_METRICS_ENABLED` | bool | `true` | Expose `/metrics` Prometheus endpoint. Set to `false` / `0` to return 404 on `/metrics`. The endpoint is always unauthenticated (exempt from bearer-auth) — bind Yadgar to loopback (default) so only local scrapers can reach it. |
| `log_format` | `YADGAR_LOG_FORMAT` | str | `"human"` | Log format. `"human"` = human-readable `%(asctime)s %(name)s %(levelname)s %(message)s`. `"json"` = one JSON object per line with `timestamp`, `level`, `logger`, `message`, and any `extra=` fields (`request_id`, `tool_name`, `duration_ms`, `status`, `trace_id`). |

---

## Security (v5.0)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `require_auth` | `YADGAR_REQUIRE_AUTH` | bool | `false` | Enforce bearer-token auth on `/api/*` and `/hooks/*` routes. When false, middleware is a no-op (logs WARN on startup). Flip to `true` after minting `YADGAR_MCP_AUTH_TOKEN`. |
| `mcp_auth_token` | `YADGAR_MCP_AUTH_TOKEN` | str | `""` | Bearer token for authenticated routes. Must be set when `REQUIRE_AUTH=true`. Generate via `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` or 1Password. |
| `allowed_origins` | `YADGAR_ALLOWED_ORIGINS` | str | `"http://127.0.0.1:8765,http://localhost:8765"` | Comma-separated CORS allowed origins. Default: loopback only. Wildcard (`*`) is never allowed. |
| `host` | `YADGAR_HOST` | str | `127.0.0.1` | Bind address for the MCP HTTP server. Default is loopback-only; set to `0.0.0.0` explicitly if you need LAN exposure (not recommended without auth + TLS). |
| `max_hash_bytes` | `YADGAR_MAX_HASH_BYTES` | int | `10485760` | Maximum file size (bytes) for path-based memorize hashing. Files exceeding this threshold are skipped. Default: 10 MiB. |
| `auto_capture_rate_limit` | `YADGAR_AUTO_CAPTURE_RATE_LIMIT` | int | `30` | Max requests per directory per minute to `/hooks/auto-capture`. Prevents log-flooding from misbehaving hooks. |

**Database credentials** (set in `/etc/yadgar/secrets.env`, loaded via `EnvironmentFile=` in systemd):

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
| `branch` | `option<string>` | `NONE` | Git branch captured at write time. `NONE` for pre-v5 rows before backfill or non-git contexts. After migration 004 all pre-v5 rows are set to `'master'`. Write path active from Stage 10 onwards. |

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

**1.5× score boost**: results where `branch == current_branch` have their `_retrieval_score` multiplied by 1.5, then the result list is re-sorted. This surfaces feature-branch context ahead of default-branch context.

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
    volumes:
      - yadgar-data:/data
      # OR: mount your own config.yaml
      - ./my-config.yaml:/root/.yadgar/config.yaml:ro
```

The config file at `/root/.yadgar/config.yaml` inside the container is read at startup. Environment variables override it.

---

## §22 project_brief — layered bootstrap (v5.0)

### New config values (`yadgar/config.py`)

| Key | Env var | Default | Description |
|-----|---------|---------|-------------|
| `PROJECT_INIT_CAP_CHARS` | `YADGAR_PROJECT_INIT_CAP_CHARS` | `2000` | Hard character cap for `_project_init` memory content. Server raises `ValueError` on overflow — no silent truncation. |
| `BRIEF_MODE_DEFAULT` | `YADGAR_BRIEF_MODE_DEFAULT` | `"catalog"` | Default mode for `project_brief`. Options: `"catalog"` or `"full"`. |

### New MCP tools

#### `project_brief(directory, mode="catalog") → dict`

Replaces `get_project_context`. Returns a structured project context snapshot.

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
| `stale_wiki_count` | int | Count of wiki pages with hash drift (Stage 9 detail — `0` for now). |

**mode="full"** (opt-in, ~1050 tokens): everything from catalog, plus:

| Field | Type | Description |
|-------|------|-------------|
| `init_memory` | str\|None | Full `_project_init` content if present. |
| `active_work` | str\|None | Full `_active_work` content if present. |
| `hot_memories` | list[dict] | Top 10 memories by heat: `{id, content[:200], heat, tags}`. |
| `key_wiki_pages` | list[dict] | 5 most recently updated wiki pages: `{slug, title, access_count}`. |

#### `bootstrap_project(directory, content) → dict` — `power=True`

Create or replace the `_project_init` memory for a directory atomically.

- **Hard cap**: content must be ≤ `PROJECT_INIT_CAP_CHARS` (2000) characters. Raises `ValueError` on overflow.
- **Idempotent**: deletes all existing `_project_init` memories for this directory before inserting the new one.
- **Tag set**: `["_project_init", "_anchor"]`, `store_type=semantic`, `is_protected=True`.
- Returns the new memory dict.

#### `update_active_work(directory, content) → dict` — `power=True`

Replace the `_active_work` memory for a directory atomically (delete-then-insert in one TX).

- **No char cap** — markdown of any size is accepted.
- **Tag set**: `["_active_work"]`, `store_type=episodic`, `is_protected=True`.
- Returns `{previous_content: str | None, new_memory: dict}`.

### Deprecated alias

`get_project_context(directory)` is retained for one release as a backward-compatible alias for `project_brief(directory, mode="catalog")`. It emits `DeprecationWarning` on every call and will be removed in a future version.

---

## §26 wiki_refresh_stale + wiki_cleanup_merged_branches (v5.0)

### New MCP tools

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
| `branch` field absent | Filled with `"master"` (Stage 10 will source from git) |
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

`yadgar/hooks/stop-memory-checkpoint.py` now fires a signal-evaluation prompt instead of a simple checkpoint prompt every 25 human messages.

**State file**: `~/.yadgar/stop-hook-state.json` — keyed by `session_id`, written atomically via `tmp + os.replace`.

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
