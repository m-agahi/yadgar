# Core / Backend Monorepo Folder Split (task #17)

- **Date:** 2026-07-06
- **Status:** PLAN (read-only; build-to-it per user direction 2026-07-06)
- **Scope:** Organizational folder split ONLY — `yadgar/core/` + `yadgar/backend/` + `yadgar/_shared/`, **ONE package / one wheel**.
- **Driver:** code-org + DI boundary enforcement. Formalize the modular-monolith Protocol+DI standard (MLClient Protocol → LocalMLClient/RemoteMLClient is the exemplar; ADR-0051/0053; CacheProtocol already exists at `yadgar/backend/cache.py:297`).
- **Out of scope:** recall Train-3 (bounded-parallel recall, gated on Ettin #32), #21 db-audit, two-package / two-repo split. See §7.

---

## 0. TL;DR / BLUF

The monorepo already *works* — imports resolve in both the core and backend containers — but the core↔backend boundary is **convention, not enforced**. Grep proves it is already crossed in both directions:

- **core → backend (10 sites, all lazy):** `retrieval/reranking.py`, `storage/ops.py` (×3), `storage/memory.py`, `server/lifecycle.py` (×2), `server/tools/admin_other.py`, `log_config.py` → `yadgar.backend.{cache,ml_client,embed_service_metrics}`.
- **backend → core (many, mostly lazy + a few module-level):** `yadgar/backend/*` imports `config`, `observability.observe`, `metrics`, `paths`, `embeddings`, `log_config`, `tracing`, `exception_telemetry`, **`server.lifecycle.init_engines`/`_get_storage`**, **`server._state`**, **`server._offload`**, **`server.tools._recall_pipeline`**, `storage.directory`.

The single load-bearing finding: **`server/` is not core-only.** The backend `/recall` endpoint (`yadgar/backend/embed_service.py:960,1011,1052-1053`) reuses core's engine bootstrap (`server.lifecycle.init_engines` + `_get_storage`), the global engine registry (`server._state`), and the recall pipeline (`server.tools._recall_pipeline`). `server._state` (`yadgar/server/_state.py:16-39`) imports the **entire 23-engine constellation**. So the retrieval / storage / graph / engram / embeddings runtime is genuinely **`_shared`**, not backend-only and not core-only.

**Consequence (state it honestly):** `_shared/` is large; `core/` is thin (MCP-tool shell + hooks + consolidation-scheduler wiring + daemon + cli + install). Trying to keep the constellation in `core/` would REQUIRE `backend → core` — the exact violation we are eliminating.

**Two kinds of cross-edges** (this split turns on the distinction):

1. **Relocation-only (no DI escape).** `backend → server.lifecycle/_state/_recall_pipeline`. The process that *runs* the recall pipeline must import it. You cannot DI your way out of "the runner imports the runnable." The only legalization is to move that runtime into `_shared/` (where backend is allowed to import). **Non-optional for zero violations.**
2. **DI-solvable.** `core storage/{ops,memory}.py → backend.cache` data caches, and `log_config → backend.embed_service_metrics`. Here the importer *uses* a handle it does not run → inject it via a Protocol. `CacheProtocol` already exists; this is exemplar #2 alongside MLClient. Storage depends on `CacheProtocol` in `_shared`; concrete cache instances injected at construction. **Preserves the "keep two cache.py split" decision** — `backend/cache.py` keeps ce/embed (ModelCkpt-keyed); storage never imports it.

The recall-runtime extraction **is the point** of this train. Half-enforcing (waiving the `backend→server` seam) leaves the driver goal unmet — it would waive precisely the seam that motivated the work. This is **NOT Train-3-gated**: moving `_state`/`lifecycle`/`_recall_pipeline` is pure relocation (where code lives), independent of Train-3 (how recall runs). If anything it should land *before* Train-3 so Train-3 rewrites the file in its final home.

---

## 1. MODULE CLASSIFICATION (the foundation)

**Classification rule** (one wheel → folder = Docker-image partition + the two no-cross-import rules):

- `core/` iff backend never needs it AND the core MCP/stdio server needs it.
- `backend/` iff core never imports it (only reached via the HTTP `/recall`+`/rerank` boundary or a Protocol).
- `_shared/` otherwise — imported by both, OR needed by the backend's in-process recall runtime.

**Process legend:** `core` = MCP server (stdio + Docker core image) + hooks + consolidation + daemon + cli. `backend` = embed_service uvicorn app (`/embed`, `/rerank`, `/recall`) + LocalMLClient inference. `stdio` = Claude-Code-spawned subprocess, no network, no backend — but recall is now **forward-only** (`server/tools/recall.py:86` "no in-core fallback"), so stdio recall requires a backend; the constellation still loads in-process for *memorize/consolidation/wiki/restore*, which are NOT forwarded (`server/tools/_memorize_phases/*`).

### 1a. Definitive table

| Module / subpackage | Class | Evidence (importers / runner) | Target path |
|---|---|---|---|
| `config.py`, `config_yaml.py`, `config_registry.py`, `config_sync.py` | **_shared** | imported by both; `backend/*` imports `config.resolve_knob`/`get_settings` (ml_client.py:19, cache.py:390, embed_service.py:87). Three-way-sync guard (I25/I32) pins these. | `yadgar/_shared/config*` |
| `observability/` (`observe`) | **_shared** | `backend/cache.py:45`, `ml_client.py:20`, `embed_service.py:88`; also every core module. | `yadgar/_shared/observability/` |
| `metrics.py` | **_shared** | `backend/cache.py:44`, `backend/ml_client.py:200`. server imports are **lazy** (metrics.py:1067,1167). | `yadgar/_shared/metrics.py` |
| `tracing.py` | **_shared** | `backend/embed_service.py:489`; core `__main__`/lifecycle. | `yadgar/_shared/tracing.py` |
| `paths.py`, `platform_paths.py` | **_shared** | `backend/embed_service.py:26` (`import yadgar.paths`); hooks import `paths`. | `yadgar/_shared/paths.py` |
| `exception_telemetry.py` | **_shared** | `backend/ml_client.py:444` (×4). | `yadgar/_shared/exception_telemetry.py` |
| `models.py` | **_shared** | pydantic DTOs; imported broadly incl. recall pipeline. | `yadgar/_shared/models.py` |
| `log_config.py` | **_shared** (with a DI seam) | `backend/embed_service.py:477` imports `configure_logging`; but log_config.py:771 lazily imports `backend.embed_service_metrics` → **DI-solvable edge** (§3). | `yadgar/_shared/log_config.py` |
| `embeddings.py` (`EmbeddingEngine`) | **_shared** | `backend/embed_service.py:92,405`; core `_state.py:23`. Runs in both. | `yadgar/_shared/embeddings.py` |
| `remote_embeddings.py` | **_shared** | core→backend embed client; both sides. | `yadgar/_shared/remote_embeddings.py` |
| `storage/` (`StorageEngine`, ops, memory, migrations, directory) | **_shared** | backend `/recall` holds a `StorageEngine` via `_get_storage` (embed_service.py:1052); core holds one too. `storage/ops.py:507/524/555` + `memory.py:421` import `backend.cache` → **DI-solvable** (§3). `storage/directory` imported by backend (embed_service.py:1012). | `yadgar/_shared/storage/` |
| `retrieval/` | **_shared** | `_recall_pipeline.py:56-59` imports `retrieval.providers.{base,fusion,memory,wiki}`; `_state.py:32` imports `Retriever`; **`retrieval/reranking.py:71,80` imports `backend.ml_client.LocalMLClient` + `backend.cache.get_ce_cache`** → DI-solvable (§3). Runs in backend recall. | `yadgar/_shared/retrieval/` |
| `knowledge_graph.py` | **_shared** | `_state.py:25`, backend recall PPR/spreading-activation uses the entity graph (mem 531710). | `yadgar/_shared/knowledge_graph.py` |
| `engram.py` (`EngramAllocator`) | **_shared** | `_state.py:24`; engram-slot occupancy is a recall input (backend). | `yadgar/_shared/engram.py` |
| `thermodynamics.py` | **_shared** | imports `storage`+`embeddings`; heat/decay applied by backend recall DB side-effects (`_apply_recall_db_side_effects`). | `yadgar/_shared/thermodynamics.py` |
| `astrocyte_pool.py` | **_shared** | `_state.py:16`; landscape recall in backend (`_run_landscape_backend`, embed_service.py). | `yadgar/_shared/astrocyte_pool.py` |
| `cls_store/`, `cognitive_map.py`, `predictive_coding.py`, `causal_discovery/`, `metacognition/` | **_shared** | all in `_state.py:17-28` engine constellation; loaded wherever engines init (core + backend recall). | `yadgar/_shared/...` |
| `wiki.py`, `wiki_meta.py`, `narrative.py`, `restoration.py`, `prospective.py`, `sensory_buffer.py`, `staleness.py`, `rules_engine.py`, `rate_limit.py`, `sleep_compute/`, `curation/` | **_shared** | engine constellation (`_state.py:29-39`). NB: some (staleness, prospective, sensory_buffer) are core-behaviour-only but are pulled by `_state` construction → keep in `_shared` to avoid backend→core. **See OPEN QUESTION Q3.** | `yadgar/_shared/...` |
| `server/_state.py`, `server/lifecycle.py`, `server/tools/_recall_pipeline.py`, `server/_offload.py`, `server/_helpers.py` (recall bits) | **_shared** (relocation-only edge) | backend `/recall` imports `lifecycle.init_engines`/`_get_storage` (embed_service.py:960,1052), `_recall_pipeline` (:1053), `_offload` (ml_client.py:766), `_state`. THE core↔backend seam. | `yadgar/_shared/runtime/` (new) |
| `server/` (rest: `_app.py`, `http*.py`, `routes/`, `tools/*` except recall pipeline, `auth_middleware.py`) | **core** | MCP tool registration + HTTP shell; backend never imports these (only `_recall_pipeline` + `lifecycle`). | `yadgar/core/server/` |
| `hooks/` | **core** | importers: `scripts/hook_runner.py`, `server/http.py`. Module-level imports only `observability`,`tracing`,`paths` (→ `_shared`). #166 moved hook-recall *forwarding* logic but hooks host = core. | `yadgar/core/hooks/` |
| `consolidation/` | **core** | ConsolidationScheduler runs in core/daemon; imports the constellation (now `_shared`). Not imported by backend. | `yadgar/core/consolidation/` |
| `daemon.py` | **core** | nightly/consolidation orchestrator; module-level import only `observability`. | `yadgar/core/daemon.py` |
| `cli/` | **core** | operator CLI; not imported by backend. | `yadgar/core/cli/` |
| `enrichment/`, `export/`, `file_queue/`, `repo_wiki/`, `seed/`, `update/`, `vacuum/`, `curation/` (scheduler side), `drain.py`, `backup.py`, `ops.py`, `conflict_resolver.py`, `blocks_render.py`, `graph_api.py`, `graph_layout.py`, `viz_*.py`, `narrative.py` (writer path) | **core** | core-only tools/maintenance; verify none imported by `backend/*` (grep: none). **`conflict_resolver.py:97` lazily imports `server.lifecycle._get_storage`** → becomes `_shared/runtime` import (legal from core). | `yadgar/core/...` |
| `install_hooks_lib.py`, `install_subagents_lib.py`, `install_assets/`, `scripts/`, `systemd/`, `static/`, `seed/`, `security/`, `secrets.py`, `sensitive_lock.py`, `sanitize.py`, `sd_notify.py`, `_surreal_runner.py`, `drain.py` | **core** | install/runtime-ops, core image. `_surreal_runner.py` starts SurrealDB — arguably backend, but launched by core entrypoint; keep core unless backend imports it (grep: no). **Q4.** | `yadgar/core/...` |
| `cache.py` (core) | **core** | 4 read-tool namespaces (project_brief/wiki_read/wiki_query/agent_prompt_prelude), byte-bounded to core RAM. By-design split. | `yadgar/core/cache.py` |
| `backend/cache.py` | **backend** | ce/embed (ModelCkpt) + memory_doc/engram_slot/graph (ScopeVersions). Keep. Exposes `CacheProtocol` → move Protocol to `_shared` (§3). | `yadgar/backend/cache.py` |
| `backend/embed_service.py`, `backend/ml_client.py`, `backend/embed_service_metrics.py` | **backend** | uvicorn app + inference + backend metrics. | `yadgar/backend/...` |
| `__main__.py`, `__init__.py`, `py.typed` | **package root** | entry dispatch + version. Stay at `yadgar/`. | `yadgar/` |

### 1b. Hard cases — resolved

- **`retrieval/` — does core still import it?** Yes indirectly: `_state.py:32` (`Retriever`) is constructed in core lifecycle, and `_recall_pipeline` (now `_shared/runtime`) imports `retrieval.providers.*`. Recall *executes* in backend but retrieval is loaded in both → **_shared**. Its edge to `backend.ml_client`/`backend.cache` (reranking.py:71,80) is **DI-solvable** (§3, MLClient/CacheProtocol).
- **`storage/` — truly shared?** Yes. Both processes hold a `StorageEngine`. Backend `/recall` calls `_get_storage()` (embed_service.py:1052). Its `backend.cache` imports (ops/memory) are **DI-solvable** → **_shared**.
- **`knowledge_graph`/`engram` — backend + core?** Both. Constellation members + recall inputs → **_shared**.
- **`config` — shared?** Yes, unambiguous → **_shared** (keep three-way-sync guard files together).
- **the two `cache.py`** — stay split (core=read-tool, backend=ce/embed/data). Only `CacheProtocol` migrates to `_shared`.
- **`server/`** — SPLIT: recall runtime (`_state`,`lifecycle`,`_recall_pipeline`,`_offload`) → `_shared/runtime/`; MCP/HTTP shell (`tools/*`,`http*`,`routes/`,`_app`) → `core/server/`.

---

## 2. TARGET LAYOUT

One wheel, three subpackages, all shipped (`[tool.hatch.build.targets.wheel] packages = ["yadgar"]` already globs subpackages — `core/`, `_shared/`, `backend/` come for free).

```
yadgar/
  __init__.py            # version, __version__ (stays)
  __main__.py            # cli() dispatch → core.server / backend (stays root)
  py.typed

  _shared/               # imported by BOTH core and backend
    __init__.py
    config.py  config_yaml.py  config_registry.py  config_sync.py
    observability/  metrics.py  tracing.py  exception_telemetry.py  log_config.py
    paths.py  platform_paths.py  models.py
    embeddings.py  remote_embeddings.py
    storage/           # StorageEngine + ops + memory + migrations + directory
    retrieval/
    knowledge_graph.py  engram.py  thermodynamics.py  astrocyte_pool.py
    cls_store/  cognitive_map.py  predictive_coding.py  causal_discovery/  metacognition/
    wiki.py  narrative.py  restoration.py  prospective.py  sensory_buffer.py
    staleness.py  rules_engine.py  rate_limit.py  sleep_compute/  curation/
    protocols.py       # NEW: MLClientProtocol, CacheProtocol, StorageProtocol (§3)
    runtime/           # NEW: the shared recall/engine runtime lifted out of server/
      __init__.py
      state.py         # ex server/_state.py (engine registry)
      lifecycle.py     # ex server/lifecycle.py (init_engines, _get_storage, getters)
      recall_pipeline.py  # ex server/tools/_recall_pipeline.py
      offload.py       # ex server/_offload.py
      helpers.py       # recall-relevant bits of server/_helpers.py

  core/                  # core image + stdio + hooks + daemon + cli + tools
    __init__.py
    server/
      __init__.py  _app.py  http.py  http_*.py  routes/
      tools/           # recall.py (forwarder), memorize.py, wiki.py, admin_*, project.py ...
      auth_middleware.py
    hooks/
    consolidation/
    daemon.py
    cli/
    cache.py           # core read-tool cache (byte-bounded, 4 namespaces)
    enrichment/ export/ file_queue/ repo_wiki/ seed/ update/ vacuum/
    graph_api.py graph_layout.py viz_*.py blocks_render.py conflict_resolver.py
    drain.py backup.py ops.py narrative-writer bits ...
    install_hooks_lib.py install_subagents_lib.py install_assets/
    security/ secrets.py sensitive_lock.py sanitize.py sd_notify.py
    _surreal_runner.py systemd/ static/ scripts/

  backend/               # backend image ONLY (unchanged location)
    __init__.py
    embed_service.py  ml_client.py  embed_service_metrics.py  cache.py
```

### 2a. Entry points / packaging

- **`pyproject.toml [project.scripts]`** — paths change:
  - `yadgar = "yadgar.__main__:cli"` — unchanged (root stays).
  - `yadgar-nightly-cycle = "yadgar.core.scripts.nightly_cycle:main"` (was `yadgar.scripts...`).
  - `yadgar-setup = "yadgar.core.scripts.yadgar_setup:main"`.
- **`__main__.py`** dispatch: `cli()` routes to `yadgar.core.server...` (stdio/http) and `--backend` / `entrypoint-backend.sh` runs `uvicorn yadgar.backend.embed_service:app`.
- **`server.json`** — `console-scripts`/version untouched (identifier `yadgar`, one pypi package). Bump `version` + `backend_version` (§5.7).
- **Wheel** — `packages = ["yadgar"]` already ships all three subpkgs. `shared-data` (install_assets, seed materials, scripts/install) mappings unchanged if those dirs move under `core/` — **update the two `install_assets`/`scripts/install`/`seed/materials` source paths in `[tool.hatch.build.targets.wheel.shared-data]` to the new `yadgar/core/...` locations** (§5.7, packaging risk).
- **Docker** — `Dockerfile` (core), `Dockerfile.backend`, `entrypoint.sh`, `entrypoint-backend.sh`: still one wheel, two images differing by entrypoint + `--memory`. No per-image wheel. Verify `entrypoint-backend.sh` invokes `yadgar.backend.embed_service` (already true).
- **Nix (`flake.nix`)** — one wheel (`pname = "yadgar"`), `coreImage`/`backendImage` differ by tag+entrypoint only. `pythonImportsCheck` (if any) updates to `yadgar.core`/`yadgar.backend`.

---

## 3. DI / PROTOCOL BOUNDARIES (formalize the modular-monolith standard)

**Rule:** core MUST NOT import backend internals — only via a `_shared` Protocol or the HTTP `/recall`+`/rerank` boundary. backend MUST NOT import core. Both may import `_shared`.

Create **`yadgar/_shared/protocols.py`** holding the seam Protocols (single obvious home; both sides depend on it):

1. **`MLClientProtocol`** (exemplar #1 — already effectively exists). `runtime_checkable` Protocol with `rerank/score/...` matching `LocalMLClient`/`RemoteMLClient` (`backend/ml_client.py`). Move the Protocol declaration to `_shared/protocols.py`; `LocalMLClient`/`RemoteMLClient` stay in `backend/ml_client.py` and `import ...protocols.MLClientProtocol`. `retrieval/reranking.py:71` stops importing `backend.ml_client.LocalMLClient` directly — instead receives an injected `MLClientProtocol` (constructed in `_shared/runtime/lifecycle.py` `_init_embedding_client`/reranker selector: Local in stdio/backend, Remote in core Docker). This kills the `retrieval → backend` module edge.

2. **`CacheProtocol`** (exemplar #2 — **already exists** at `backend/cache.py:297`). Move to `_shared/protocols.py`. `storage/ops.py` (`get_engram_slot_cache`/`get_scope_versions`/`get_graph_cache`, lines 507/524/555) and `storage/memory.py:421` (`get_memory_doc_cache`) stop importing `backend.cache` directly. Instead the backend caches are **injected into StorageEngine at construction** (in `_shared/runtime/lifecycle.init_engines`, which already runs in the backend process where the data caches live). In core (forward-only), the injected instance is a no-op/null cache — verified harmless because memorize writes run in core but recall reads run in backend (`_apply_recall_db_side_effects` runs in backend). This kills the `storage → backend` module edge **without merging the two cache.py** (backend/cache.py keeps ce/embed/data namespaces; storage only sees the `CacheProtocol` handle).

3. **`StorageProtocol`** (optional, LOWER priority). `StorageEngine` lives in `_shared/storage/` and both sides import it directly — no cross-boundary violation, so a Protocol is not strictly required. Introduce a narrow `StorageProtocol` only if we want to decouple `_shared/runtime` from the concrete engine for testability; recommend deferring (avoid churn). **Q2.**

4. **`log_config → backend.embed_service_metrics`** (log_config.py:771, lazy). Legalize by injecting a metrics sink (or gate the import behind a `_shared` registry lookup). Minor — a single lazy edge. Fold into Car 2.

**Constructor-DI wiring point:** `_shared/runtime/lifecycle.init_engines(local_engines: bool)` is the single composition root (already the case — ADR-0046 §backend selects `local_engines=True`). It selects `LocalMLClient` vs `RemoteMLClient` and the real-vs-null backend caches, and injects them into `Retriever`/`StorageEngine`. This is the formalized modular-monolith standard: **Protocol in `_shared`, concrete in the owning subpackage, injected at the one composition root.**

---

## 4. ENFORCEMENT (import-lint)

`import-linter` is **NOT currently installed** (verified: absent from `.venv/bin`, `pyproject.toml`, `.pre-commit-config.yaml`). Add it.

### 4a. Add dependency + config

`pyproject.toml`:
```toml
[dependency-groups]         # or [tool.uv] dev deps
dev = [ ..., "import-linter>=2.0" ]

[tool.importlinter]
root_package = "yadgar"

[[tool.importlinter.contracts]]
name = "core and backend must not import each other's internals"
type = "forbidden"
source_modules = ["yadgar.core"]
forbidden_modules = ["yadgar.backend"]
# allow the HTTP + Protocol seam explicitly during migration (tighten per car):
ignore_imports = [
  # Car-scoped waiver list — SHRINKS to empty by the final car.
  "yadgar.core.server.tools.recall -> yadgar.backend.*",   # example; remove once forwarder is HTTP-only
]

[[tool.importlinter.contracts]]
name = "backend must not import core"
type = "forbidden"
source_modules = ["yadgar.backend"]
forbidden_modules = ["yadgar.core"]

[[tool.importlinter.contracts]]
name = "shared must not import core or backend"
type = "forbidden"
source_modules = ["yadgar._shared"]
forbidden_modules = ["yadgar.core", "yadgar.backend"]

[[tool.importlinter.contracts]]
name = "layered: core->_shared->(none); backend->_shared->(none)"
type = "layers"
layers = ["yadgar.core | yadgar.backend", "yadgar._shared"]
containers = ["yadgar"]
```

### 4b. Pre-commit hook (mirror existing local-hook style, `.pre-commit-config.yaml`)

```yaml
      - id: check-core-backend-boundary
        name: Check core<->backend import boundary (import-linter)
        language: system
        entry: uv run lint-imports
        pass_filenames: false
        files: ^yadgar/(core|backend|_shared)/.*\.py$
```

Also add a CI job (Dockerfile.ci) running `uv run lint-imports` on push.

### 4c. Incremental-enforcement story (reviewability)

Backend's **module-level** core imports today are only `paths`, `config`, `observe` — all `_shared` after Car 0. Every *remaining* backend→core edge is **lazy** (`noqa: PLC0415`). So: introduce import-linter in **report-only / allowlist mode at Car 0**, tighten the `ignore_imports` allowlist per car, reach **zero-waiver at the final car**. The allowlist IS the migration burn-down.

---

## 5. MIGRATION MECHANICS (staged cars, behavior-neutral)

**Churn baseline:** v5.60 moved 4 files → touched ~41 files. Measured churn for this split: **140 import sites** touch the `_shared` leaf libs (config/metrics/observe/tracing/paths/embeddings/models/exception_telemetry); **22 sites** import `server.lifecycle`. `git mv` + a codemod (`ruff`-safe sed or `libcst`) rewrites `from yadgar.X import` → `from yadgar._shared.X import`. **Prefer re-export shims** (old module path re-exports from new) to stage churn: `yadgar/config.py` becomes `from yadgar._shared.config import *  # noqa` (or a `__getattr__` module shim) so old imports keep working while call sites migrate incrementally, then shims are deleted in a final cleanup car.

**Reviewability decision:** **one PR per car**, stacked. Each car keeps the full suite green (behavior-neutral) and is independently revertible. See OPEN QUESTION Q1 (shims-all-cars vs folders-now + runtime-extraction-follow-up).

### Car 0 — create `_shared/` + move the truly-shared LEAF libs
- **Moves:** `config*`, `observability/`, `metrics.py`, `tracing.py`, `exception_telemetry.py`, `paths.py`, `platform_paths.py`, `models.py`, `log_config.py`, `embeddings.py`, `remote_embeddings.py` → `yadgar/_shared/`.
- **Method:** `git mv` each; add re-export shim at old path (`yadgar/config.py` → `from yadgar._shared.config import *`); codemod the ~140 import sites in a follow-up or leave shims.
- **Import sites:** ~140 (mostly mechanical).
- **Behavior-neutral guarantee:** shims preserve every old import path; no runtime code changes, only file location.
- **Legalizes immediately:** all backend **module-level** core imports (`config`,`paths`,`observe`) become `backend → _shared` = legal.
- **Test gate:** full suite green; `test_config_three_way_sync.py` (I25) green (move the 3 config files + allowlist txt together, update its `files:` glob in pre-commit); import-linter added in **report-only** mode.

### Car 1 — create `_shared/runtime/` (relocation-only seam) + move the constellation
- **Moves:** `server/_state.py`→`_shared/runtime/state.py`, `server/lifecycle.py`→`_shared/runtime/lifecycle.py`, `server/tools/_recall_pipeline.py`→`_shared/runtime/recall_pipeline.py`, `server/_offload.py`→`_shared/runtime/offload.py`; the engine constellation (`storage/`, `retrieval/`, `knowledge_graph.py`, `engram.py`, `thermodynamics.py`, `astrocyte_pool.py`, `cls_store/`, `cognitive_map.py`, `predictive_coding.py`, `causal_discovery/`, `metacognition/`, `wiki.py`, `narrative.py`, `restoration.py`, `prospective.py`, `sensory_buffer.py`, `staleness.py`, `rules_engine.py`, `rate_limit.py`, `sleep_compute/`, `curation/`) → `_shared/`.
- **Method:** `git mv` + shims at old paths (esp. the 22 `server.lifecycle` sites → shim `yadgar/server/lifecycle.py` re-exports from `_shared/runtime/lifecycle`).
- **Import sites:** 22 lifecycle + ~30 `_state`/constellation sites.
- **Behavior-neutral guarantee:** pure relocation; the composition root `init_engines` unchanged.
- **Legalizes:** `backend → server.lifecycle/_state/_recall_pipeline/_offload` becomes `backend → _shared/runtime` = **legal** — this closes THE seam.
- **Test gate:** full suite; recall integration tests; backend `/recall` smoke; import-linter allowlist SHRINKS (remove the `backend→server` waivers).

### Car 2 — formalize Protocol boundaries + DI wiring
- **Adds:** `_shared/protocols.py` (`MLClientProtocol`, `CacheProtocol` moved from `backend/cache.py:297`).
- **Changes:** `retrieval/reranking.py` stops importing `backend.ml_client` — receives injected `MLClientProtocol`; `storage/{ops,memory}.py` stop importing `backend.cache` — receive injected `CacheProtocol` from `init_engines`; `log_config` metrics-sink DI.
- **Import sites:** ~6 core→backend lazy edges removed.
- **Behavior-neutral guarantee:** DI selects the same concrete objects the lazy imports selected (Local vs Remote MLClient; real vs null cache) at the same composition root. Characterization tests pin recall output pre/post.
- **Test gate:** recall parity (LongMemEval smoke or content-integrity test); import-linter allowlist → **empty** for core→backend; `runtime_checkable` isinstance tests for both Protocols.

### Car 3 — move remaining CORE-only modules into `core/` + enforcement ON
- **Moves:** everything not `_shared`/`backend` → `yadgar/core/` (`server/` shell, `hooks/`, `consolidation/`, `daemon.py`, `cli/`, `cache.py`, `enrichment/`, `export/`, `file_queue/`, `repo_wiki/`, `seed/`, `update/`, `vacuum/`, `security/`, install/ops libs, viz, graph_api, etc.).
- **Method:** `git mv` + update `[project.scripts]` paths + `shared-data` source paths + `__main__` dispatch. Delete the Car 0/1 re-export shims (final churn car).
- **Import sites:** the remaining bulk; but by now all cross-edges are legal, so this is mechanical.
- **Behavior-neutral guarantee:** file moves + path updates only.
- **Enforcement:** flip import-linter from report-only → **hard-fail** in pre-commit + CI; `ignore_imports` empty.
- **Test gate:** full suite; wheel-build test (`shared-data` paths); entry-point smoke (`yadgar`, `yadgar-nightly-cycle`, `yadgar-setup`); editable-install dev loop; Docker core + backend image build; nix `pythonImportsCheck`.

### 5.7 Version bump / packaging sync
- Core `pyproject.toml version` + `server.json version` + `backend_version` bump (minor — organizational). Update `[project.scripts]` (3 entries) + `[tool.hatch.build.targets.wheel.shared-data]` source paths (install_assets, seed/materials/anchors.yaml, scripts/install) to `yadgar/core/...`. Keep `packages = ["yadgar"]` (globs all three).

---

## 6. TESTING + RISKS + ROLLBACK

### 6a. Testing
- **Import-structure tests:** new `test_core_backend_boundary.py` asserting `import-linter` contracts pass (or invoke `lint-imports` as a test); assert `yadgar.core` importable without importing `yadgar.backend` heavy deps, and vice versa.
- **Full suite behavior-neutral** per car (green gate). Characterization: recall output parity across Car 2 (content-integrity test already exists — `recall-content-integrity-flake.md`).
- **Guards to keep green:** I25 three-way config sync (`test_config_three_way_sync.py` — update its `files:` glob + move the 4 config files together), I28 allowlist-audit, I29 dead-capability, I32 capability-registry coverage (`check_capability_coverage.py` globs `yadgar/server/tools/.*` — **update glob to `yadgar/core/server/tools/`**), I30 complexity. Wheel-bundle test (shared-data).
- **CI:** add `lint-imports` job; keep e2e behavior-contract (make e2e) green.

### 6b. Risks
1. **Circular imports exposed by the split.** Highest risk. `metrics → yadgar.server` (lazy, safe), `paths → observability → metrics → config → observability` — the `_shared` leaf cluster is mutually referential. Moving them together (Car 0) preserves current (working) cycles; splitting the cluster across `core`/`_shared` would break it. Mitigation: move the whole leaf cluster atomically; run `python -c "import yadgar._shared"` in CI.
2. **Entry-point breakage.** `[project.scripts]` + `__main__` dispatch + `shared-data` paths. Mitigation: entry-point smoke test per car; keep shims until Car 3.
3. **Wheel / nix packaging.** `shared-data` source-path drift; nix `pythonImportsCheck`. Mitigation: wheel-build + `pipx`-install smoke; nix `nix build .#yadgar` in CI-viz.
4. **Editable-install dev loop.** `uv pip install -e .` — hatchling editable must still resolve subpackages; shims prevent stale-path breakage during migration.
5. **The forward-only null-cache DI (Car 2)** — if a core code path unexpectedly reads through the injected cache, a null cache could change behavior. Mitigation: characterization/parity test; verify memorize write path (core) does not read recall data caches (confirmed: `_apply_recall_db_side_effects` runs backend-side).
6. **Big mechanical diff reviewability.** Mitigation: one PR per car, `git mv` (preserves blame), codemod diffs isolated from logic diffs.

### 6c. Rollback (per car)
- Each car = one stacked PR, `git revert`-able. Car 0/1/2 leave shims so reverting a later car does not strand imports. Car 3 removes shims last — revert Car 3 restores shims + old paths. Because every car is behavior-neutral, rollback is a pure `git revert` with no data/migration implications (no DB schema, no config semantics changed).

---

## 7. OUT OF SCOPE (explicit)
- **recall Train-3** (bounded-parallel recall perf restructure) — SEPARATE, gated on Ettin (#32). This split is *relocation*; Train-3 is *how recall runs*. Note: the runtime extraction (Car 1) should land BEFORE Train-3 so Train-3 rewrites `recall_pipeline.py` in its final `_shared/runtime/` home.
- **#21 db-audit.**
- **two-package / two-repo split** — explicitly rejected by user; ONE wheel, three subpackages.
- **Merging the two `cache.py`** — by-design dual (ADR-0048/0053); keep. Only `CacheProtocol` moves to `_shared`.

---

## 8. OPEN QUESTIONS (need user)
- **Q1 (reviewability / the real one):** recall-runtime extraction (Car 1) is a large diff (~22 lifecycle + ~30 constellation sites). Option (a): all cars in this train, stacked with re-export shims, ending **zero-violation**. Option (b): folders + leaf-libs now (Car 0), recall-runtime extraction as an immediate follow-up train, shipping the `backend→server` edge as a **documented temporary import-linter waiver**. **Recommend (a)** — (b) launches the boundary UNENFORCED at exactly the seam that motivated the work.
- **Q2:** introduce `StorageProtocol` now, or defer? `StorageEngine` lives in `_shared` so no cross-boundary violation forces it. Recommend defer (avoid churn) unless testability wants it.
- **Q3:** core-behaviour-only engines pulled into `_shared` by `_state` construction (`staleness`, `prospective`, `sensory_buffer`, `narrative` writer path). Keep in `_shared` (simplest, avoids backend→core), or split `_state` so backend recall constructs a *slim* engine set (no staleness/prospective)? Slim `_state` is cleaner long-term but is a real refactor — recommend defer to a follow-up, keep whole constellation in `_shared` for this train.
- **Q4:** `_surreal_runner.py` — core or backend? Launched by core entrypoint, not imported by `backend/*`. Classified core; confirm.
- **Q5:** naming — `_shared/runtime/` for the extracted engine/recall runtime; acceptable, or prefer `_shared/engines/` + `_shared/recall/`?

---

## 9. DECISIONS (user, 2026-07-06)

- **Q1 → ALL 4 cars this train.** End state = zero cross-boundary violations, enforcement fully ON. Runtime extraction (Car 1) included.
- **Q3 → SLIM `_state` NOW** (the harder path, chosen deliberately). Backend recall must construct only the engines the recall path needs; core-only engines (staleness / prospective / sensory_buffer / narrative-writer + any others) are NOT built by the backend. This is a REAL refactor, not pure relocation → it needs the actual backend-`/recall` engine-dependency map (which of the ~23 `_state` engines the recall pipeline touches vs core-only). Design sub-task dispatched; folds into Car 1/2 as a `_state` split (full engine set for core/memorize/consolidation; slim recall set for backend `/recall`). RISK: a missing engine in the slim set → backend recall runtime error → characterization/parity + a backend-recall smoke test per the slim change.
- **Q5 → `_shared/runtime/`** (engine-agnostic name; recall is one member, not the only engine — do NOT name a dir `recall`). `recall_pipeline.py` is one file inside `_shared/runtime/`.
- **Q2 → defer `StorageProtocol`** (StorageEngine is `_shared`, nothing forces it).
- **Q4 → `_surreal_runner.py` = core** (launched by core entrypoint, not imported by backend).
