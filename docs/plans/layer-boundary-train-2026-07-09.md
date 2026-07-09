# Plan: T2 — Layer-boundary train (placement law + no-lone-files + DB-path forwarding)

**Status:** DRAFT — awaiting user sign-off (user 2026-07-09: "do not do anything without a saved file plan"). ADR pending capture (yadgar maintenance window).
**Date:** 2026-07-09. **Position:** T2 of the recall program (after test-suite hardening train; before T3 restructure, T4 Ettin — see recall-3-train plan re-numbering, commit dc285e5e).
**Evidence base:** read-only opus sweep 2026-07-09 (transitive import-graph over all 116 `_shared` modules + per-symbol split analysis; analyzer script preserved in session scratchpad). Two agents disagreed on import-linter mode; resolved against source: contracts ARE enforcing via the `lint-imports` pre-commit hook (`.pre-commit-config.yaml:166`) — the `pyproject.toml:229-239` REPORT-ONLY comment is STALE (Car 0 leftover) and gets fixed in this train.

## The two placement laws (user 2026-07-09, verbatim intent)

1. **Dual-import law:** `_shared/` membership ONLY for code directly or INDIRECTLY imported by BOTH `core/` and `backend/`. Single-layer consumers move to their layer.
2. **Semantic law (wins on conflict):** anything needing COMPUTE (numpy/matrix/scoring), heavy transformation, or stateless-over-DB-data → BACKEND (7 CPUs, next to DB, ADR-0078) even if all current importers are core. Core keeps ONLY routing/orchestration, MCP tool surface, session state, response caches, sanctioned debugging APIs. Where the laws disagree, the verdict must name the forwarding shape.
3. **No lone files:** every module is a package directory, small or big. Zero single-file modules at any layer root.
4. Contract/impl splits encouraged: other layer needs only a dataclass/protocol → contract stays `_shared`, impl moves.

## Sweep verdicts (the move set)

`_shared` is healthier than feared: of 116 modules, all but a handful are genuinely dual. Moves:

| Module | LOC | Verdict | Notes |
|---|--:|---|---|
| `config_sync.py` | 169 | MOVE→core | config plumbing, core-only importers, no compute |
| `platform_paths.py` | 61 | MOVE→core | fs helpers, core-only |
| `cognitive_map.py` | 247 | **MOVE→backend + FORWARD** | semantic law: numpy SR/transition-matrix compute over DB rows; core-only importers irrelevant. Forward: core `restore` tool → backend endpoint (`POST /restore` or `/sr/predict`); session-side transition buffer stays core, passes `(from_id, to_id)` via existing write seam |
| `restoration.py` | 527 | **SPLIT** | `CheckpointContext` dataclass → `_shared` contract; `CheckpointRestore` (restore compute over DB) → backend, behind the same forward as cognitive_map |
| `wiki.py` | 2314 | **SPLIT** (placement part) | `WikiAddOptions` contract → `_shared`; `WikiStore` (DB r/w) → backend; core viz reads via forward. Internal I13 splitting stays task #18 |
| `models.py` | 341 | STAY | core-only importer today, but pure pydantic contracts — contracts belong `_shared` |
| `observability/timing.py` | 174 | **prod-DEAD** | imported only by its own tests — delete (owner decision), not a move |

Everything else: STAY(dual) — including `storage/` (genuinely dual), `retrieval/*` (compute, but BOTH layers execute it: core recall path + `backend/predictive_coding.py`), `runtime/lifecycle.py` (composition root, 3 permanent ADR-0056 waivers), `runtime/offload.py`, `cache_epoch`, secrets/enforcement.

## Core DB-touch surface (ADR-0078 ledger)

True surface ≈ 15 core modules (11 direct `_shared.storage` imports + 9 `_get_storage()` users; earlier "13" undercounted). Only **5 do raw WRITES** — the actual ADR-0078 blockers:

| Core module | Write | Disposition |
|---|---|---|
| `core/cli/capture.py` | `insert_action_log` | (c) CLI/ops — sanction or relocate to drain seam |
| `core/seed/_generate.py` | `insert_memory`, `update_memory_scores` | (b) FORWARD → backend seed endpoint or write-drainer |
| `core/server/http.py` | `delete_memory`, `insert_action_log` | (b) FORWARD — action-log has queue seam; delete_memory needs backend write endpoint |
| `core/server/tools/admin_other.py` | `update_memory_staleness` | (a) sanctioned debug/ops API — keep or forward |
| `core/staleness.py` | `update_memory_heat/staleness` | (b) heat-decay compute = backend consolidation territory → relocate |

File-queue users (`tools/wiki.py`, `memorize.py`, `misc.py`, `project.py`, `admin_dlq.py`) are the SANCTIONED forward seam — not violations. **`storage/` sink into `backend/` is OUT OF SCOPE this train**: it needs all 5 write paths forwarded PLUS core reads routed through a backend read endpoint (9 tool modules read directly today). Post-T2 follow-up.

## Lone-file inventory → packaging targets

- **`_shared` (29 lone files):** `config*` quartet → `config/` pkg (config_sync exits to core); `embeddings.py`+`remote_embeddings.py` → `embeddings/`; `wiki.py`+`wiki_meta.py` → `wiki/`; `tracing.py`/`log_config.py`/`metrics.py`/`exception_telemetry.py` → merge into existing `observability/`; `secrets.py`+`enforcement.py` → `security/`; `models.py`/`protocols.py`/`engram.py` → `contracts/`; `paths.py` → `paths/`; remaining singles (`astrocyte_pool`, `sensory_buffer`, `thermodynamics`, `knowledge_graph`, `rules_engine`, `rate_limit`, `blocks_render`, `server_helpers`) → own small pkg dirs.
- **`backend` (8):** `embed_service.py` 1363, `cache.py` 976, `ml_client.py` 807, `predictive_coding.py` 641, `embed_service_metrics.py` 415, `narrative.py`, `conflict_resolver.py`, `prospective.py` → each its own pkg dir.
- **`core` root (21):** `viz_*` → `core/viz/`; `install_*_lib` → `core/install/`; `daemon.py`/`daemons.py`/`sd_notify.py`/`drain.py` → `core/daemon/`; rest → own pkg dirs.
- I13 hard-cap violators (`wiki.py` 2314, `config_yaml.py` 2093, `metrics.py` 1198, `config.py` 1019, `log_config.py` 1002): this train does PLACEMENT/packaging only; internal splitting = task #18 (module-standardization), which now runs as follow-up cars on the packaged layout.

## Cars (order: C → A → B → D → E; F = verification)

| Car | Content | Blast | Depends |
|---|---|---|---|
| **C — contract/impl splits** | `restoration.py` split (context stays, CheckpointRestore→backend); `wiki.py` placement split (WikiAddOptions contract, WikiStore→backend, viz forward) | `checkpoint_impl.py`, `viz_meta.py`, `admin_exec/wiki.py`, `write_exec/wiki_add_impl.py` + tests | #178 merged ✅ |
| **A — `_shared`→core moves** | `config_sync.py`, `platform_paths.py` → core pkgs, PEP-562 shims | ~2 modules + 2 test files | none |
| **B — `_shared`→backend moves** | `cognitive_map.py` → backend pkg + `POST /restore` (or `/sr/predict`) forward; core restore tool becomes thin forwarder (recall Train-1 pattern) | `cli/_shared.py`, `admin_other.py`, new backend endpoint, cognitive tests; BACKEND_VERSION bump | Car C (restoration split) |
| **D — lone-file→package conversions** | D1 `_shared` (29), D2 backend (8), D3 core (21); mechanical, PEP-562 shims, test-mirror moves | biggest churn, low risk | rebase AFTER hardening Car 1 merges (conftest churn) |
| **E — core DB write-path forwarding** | the 5 raw-write paths → backend endpoints / drain seams; staleness/heat-decay relocated to backend consolidation | seed, http, staleness, capture, admin_other + backend endpoints | flag: touches `_get_storage()` seam shared with prelude-contract branch — land after it merges |
| **F — verification (no-op)** | import-linter ALREADY enforcing via pre-commit hook; fix STALE `pyproject.toml:229-239` REPORT-ONLY comment; assert contracts still pass post-moves; update contracts when future storage-sink lands | comment + CI assert | all cars |

Every car: one PR, behavior-neutral, tests green, plan-archival rules per ADR-0081/0082 (first commit of the COMPLETING car moves this file to archive).

## Exit criteria

1. `_shared` contains ONLY dual-imported modules (verified by the sweep analyzer re-run).
2. Zero lone-file modules in all three layers.
3. Core raw DB WRITES = 0 outside sanctioned ops APIs (reads remain until post-T2 storage sink).
4. import-linter green with no new waivers; stale comment fixed.
5. `observability/timing.py` deleted or consciously kept (user call at Car D1).

## Collisions / sequencing flags

- PR #178 (cognitive_map, storage/rules) MERGED 2026-07-09 → Car B unblocked.
- Hardening Car 1 branch (conftest 57KB churn) → Car D rebases after it merges.
- `feat/prelude-contract-wiki` (agent_prompts/dispatch_helper `_get_storage()` seam) → Car E lands after its single PR merges.

## References

ADR-0078 (DB isolation), ADR-0056 (composition-root waivers), ADR-0066/task #18 (module standardization — internal splitting, follow-up on this layout), Car 0 #167 (PEP-562 shim precedent), recall-3-train plan (T-numbering), sweep report 2026-07-09 (session artifact).
