# PLAN — Config Knob Backfill (v5.7.10 core + backend v5.3.1)

> **STATUS: SHIPPED v5.7.10 (2026-05-27)** — I25 invariant shipped with this PR. Backlog drain (Tier-2 gaps) ongoing.

**Status:** drafted 2026-05-27 from live audit. Pending implementation.

## Why

Yadgar has THREE config surfaces and they drifted:

1. **`yadgar/config.py`** — pydantic `Settings`. Source of truth at runtime. Env-overridable.
2. **`yadgar/config_yaml.py`** — declarative registry → `config.yaml` durable storage + CLI/UI rendering.
3. **`yadgar/config_registry.py`** — runtime registry → `/admin/config` endpoint + `yadgar_config_value{name}` Prometheus gauge.

Audit 2026-05-27 inventory:

| State | Count | Notes |
|---|---|---|
| Triple-registered (code + yaml + registry) | 54 | Gold standard. v5.4-v5.5 era. |
| Code + registry, missing yaml | 18 | v5.6.x / v5.7.x additions. Env-only knobs that aren't yaml-discoverable. |
| Code + yaml, missing registry | 127 | Older knobs. Invisible to `/admin/config` + Prometheus gauge. |
| Registry-only (no code field) | 45 | Intentional env-only infra knobs (container names, secrets, paths). Not orphaned. |

**Gap is real and large.** Most non-trivial code+yaml-missing-registry knobs are operational tunables that should be on the dashboard but aren't.

---

## Critical prereq discovered 2026-05-27 — container yaml loading is broken

`yadgar/config_yaml.py:547` hardcodes config path to `Path("~/.yadgar/config.yaml").expanduser()`. In container, `~` = `/root` (since `--user root`), but the host bind-mount is `/data`. So container looks at `/root/.yadgar/config.yaml` → doesn't exist → yaml silently ignored, ALL config comes from env.

Yaml works for host pipx install (CLI/MCP local mode). Yaml does nothing in container today.

This makes "minimize nix env knobs" a 3-step refactor:

### Step A — Container yaml loading (load-bearing prereq)

- Add `YADGAR_CONFIG_FILE` env override OR honor `YADGAR_DATA_DIR` for config-path resolution.
- Nix `yadgar.nix` ExecStart sets `-e YADGAR_CONFIG_FILE=/data/config.yaml`.
- Container now reads `/data/config.yaml` = host `~/.yadgar/config.yaml` via the existing `${homeDir}/.yadgar:/data` bind mount.
- Tests: host loading still works (default path), container env override resolves correctly.
- ~50 LOC + tests.

### Step B — Backfill missing yaml + registry registrations

- 18 env-only knobs (v5.6.x / v5.7.x): see table in v5.7.10 train section.
- ~20 of the 127 code+yaml-missing-registry knobs (operational subset).
- Add `tests/test_config_three_way_sync.py` enforcing **I25**.
- Same pattern as `scripts/check_metric_writers.py` (I23) + `scripts/check_trace_spans.py` (I24).

### Step C — Migrate nix `-e` flags to yaml entries

- Touches `~/git/nix/modules/home/yadgar.nix` ExecStart for both yadgar + yadgar-backend.
- Remove yaml-mirrored knobs from `-e` flags.
- Generate / update host `~/.yadgar/config.yaml` with all moved knobs at their previous defaults.
- Add **I26 invariant**: "Nix systemd ExecStart `-e` flags MUST NOT mirror a yaml-registered knob; secrets + infra-wiring only." Lint via regex pass on `yadgar.nix`.

## What moves vs what stays (Step C migration map)

### Core yadgar `-e` flags

| Flag | Fate | Why |
|---|---|---|
| `YADGAR_HOST=0.0.0.0` | → yaml `server.host` | operational |
| `YADGAR_PORT=8765` | → yaml `server.port` | operational |
| `YADGAR_WIKI_SLUG_PREFIX=yadgar` | → yaml `wiki.slug_prefix` | operational |
| `YADGAR_CORE_LOG_LEVEL=INFO` | → yaml `logging.core_level` | operational |
| `YADGAR_LOG_LEVEL=INFO` | → yaml `logging.level` | operational |
| `YADGAR_OTLP_ENDPOINT=...` | → yaml `observability.otlp_endpoint` | operational |
| `YADGAR_DB_URL` | **stay env** | container-internal URL |
| `YADGAR_EMBED_URL` | **stay env** | container-internal URL |
| `YADGAR_DATA_DIR=/data` | **stay env** | container-specific path |
| `YADGAR_DB_USER` / `PASS` | **stay env** | secret |
| `YADGAR_MCP_AUTH_TOKEN` | **stay env** | secret |
| `YADGAR_IN_CONTAINER=1` | **stay env** | deployment flag |
| `YADGAR_CONFIG_FILE=/data/config.yaml` | **NEW env** | Step A enabler — points app at the yaml |

### Backend `-e` flags

| Flag | Fate | Why |
|---|---|---|
| `SURREAL_USER` / `PASS` | **stay env** | secret |
| `YADGAR_RW_USER` / `PASS` | **stay env** | secret |
| `YADGAR_RO_USER` / `PASS` | **stay env** | secret |
| `YADGAR_MCP_AUTH_TOKEN` | **stay env** | secret |
| `SURREAL_RUNTIME_STACK_SIZE` | → yaml `backend.surreal_stack_size` | operational |
| `YADGAR_DBSIZE_CACHE_TTL_SEC=600` | → yaml `backend_cache.dbsize_ttl_sec` | operational |
| `YADGAR_CONFIG_FILE=/data/config.yaml` | **NEW env** | Step A enabler |

End state: ~5-6 `-e` flags per container (secrets + URLs + paths + flag), down from current 12-13.

---

## Scope: TWO trains

This plan splits along the natural code-ownership boundary.

### v5.7.10 (yadgar-core) — add yaml registration for env-only knobs introduced in v5.6.x / v5.7.x

Knobs to add to `yadgar/config_yaml.py`:

| Knob | Introduced | Section |
|---|---|---|
| `HEAVY_RERANK_ENABLED` | v5.6.6 | `reranking` |
| `RERANK_BACKEND_TIMEOUT_SEC` | v5.6.6 | `reranking` |
| `NLI_RERANKING_ENABLED` | v5.6.6 (default flipped to False) | `reranking` |
| `YADGAR_OTLP_ENDPOINT` | v5.7.6 | `observability` (new section) |
| `YADGAR_OTLP_HEADERS` | v5.7.6 | `observability` |
| `YADGAR_OTLP_TIMEOUT_SEC` | v5.7.6 | `observability` |
| `YADGAR_OTLP_INSECURE` | v5.7.6 | `observability` |
| `YADGAR_VIZ_HEALTH_REFRESH_SEC` | v5.7.7 | `viz` (or existing `daemon` section) |
| `YADGAR_BACKUP_RETENTION` | v5.7.0 PR-6 | `operational` |
| `YADGAR_LOG_DIR` | v5.6.7 PR-M | `logging` |
| `CROSS_ENCODER_TOP_K` | v5.4 P11 (already yaml-registered? verify) | `reranking` |
| `MODEL_IDLE_EVICTION_SECONDS` | v5.6.7 PR-G | `ml` (new section?) |

Plus retroactive registry coverage for code+yaml knobs that should ALSO surface in `/admin/config`. Targeted list (high-value operational knobs from the 127):

- consolidation_cooldown_seconds
- check_invariants_query_timeout_seconds
- db_size_warning_bytes
- vacuum_auto_threshold_bytes / window_start / window_end / enabled
- queue_retention_days
- circuit_breaker_* (open_threshold, recovery_seconds, half_open_max_requests)

NOT every code+yaml knob needs registry — pick the operational ones operators tune at runtime. Internal math knobs (similarity thresholds, retrieval weights) stay code+yaml only.

**No env-only-to-keep changes here** — `YADGAR_VACUUM_TRIGGER_PATH` and similar infra knobs stay env-only by design.

### Backend v5.3.1 — same backfill for backend-only knobs

Knobs to add to `yadgar/config_yaml.py` (these live in code + registry today):

| Knob | Introduced | Section |
|---|---|---|
| `YADGAR_DBSIZE_CACHE_TTL_SEC` | backend v5.3.0 | `backend_cache` (new section) |
| `YADGAR_SHUTDOWN_MARKER_PATH` | backend v5.3.0 | `backend_ops` (new section) |

Likely also a v5.4.0 dependency: cache knobs from `PLAN_BACKEND_V5_4_CACHING.md`:

- `YADGAR_CE_CACHE_MAX_ENTRIES`
- `YADGAR_EMBED_CACHE_MAX_ENTRIES`
- `YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC`
- `YADGAR_CACHE_SNAPSHOT_DIR`
- `YADGAR_CE_CACHE_ENABLED`
- `YADGAR_EMBED_CACHE_ENABLED`

These get added during v5.4.0 directly (the plan calls for yaml registration). v5.3.1 covers only the v5.3.0-era backfill.

---

## Implementation order

Both trains share the same skeleton:

1. **TDD test** — add a `tests/test_config_three_way_sync.py` that asserts EVERY knob in `config.py` is either:
   - In both yaml + registry, OR
   - Documented in an allowlist file (`config_env_only_allowlist.txt`) as intentional env-only.
2. **Run new test** — collect the gap (should match audit's 18 missing-yaml from §2).
3. **Fill yaml entries** — one entry per knob with `desc` + `section` strings.
4. **Fill registry entries** — same names, `ConfigEntry(env, default_str, kind)` rows.
5. **Re-run test → green.**
6. **`scripts/check_metric_writers.py` + `scripts/check_trace_spans.py`** — exit 0.
7. **Document in `MIGRATION_NOTES.md`** — entry per train.

The TDD test is the load-bearing piece. It turns drift into a CI-blockable invariant going forward. Equivalent to I23/I24 invariants for config-surface coverage.

**Proposed new invariant: I25 — Config knob MUST be triple-registered OR allowlisted env-only.** Add to `docs/ARCHITECTURE_INVARIANTS.md` after backfill lands clean.

---

## What does NOT ship in either train

- Migration of registry-only knobs (45 items, mostly secrets + container infra) to `Settings` class. Out of scope; intentional separation.
- UI changes to expose newly-registered knobs (Grafana / config-edit UI is downstream consumer work, not yadgar).
- Backfill of the 127 code+yaml-missing-registry knobs in one go. Pick the operational subset (~20 listed above). Long-tail can be a v5.7.11 followup if dashboard ergonomics motivate it.

---

## Acceptance criteria (per train)

- New `test_config_three_way_sync.py` exits green.
- I25 invariant added to `docs/ARCHITECTURE_INVARIANTS.md`.
- `python scripts/check_metric_writers.py` exit 0.
- `python scripts/check_trace_spans.py` exit 0.
- Live backend `/metrics` shows `yadgar_config_value{name="<new_knob>"} ...` for each newly-registered knob.
- Live `/admin/config` returns each newly-registered knob.

---

## Estimate

Per train: ~200 LOC + ~100 LOC tests. Single agent dispatch each. v5.7.10 core image rebuild. v5.3.1 backend image rebuild.

---

## Train re-split (after Step A discovery)

| Train | Scope | Prereq |
|---|---|---|
| **v5.7.10 core** | Step A (container yaml loading) + Step B core (yaml backfill for v5.6.x/v5.7.x knobs) + I25 invariant + TDD three-way-sync test | none |
| **backend v5.3.1** | Step A backend (yaml loading for embed_service.py) + Step B backend (yaml backfill for v5.3.0 knobs) | v5.7.10 lands (shares loader code) |
| **v5.7.11 core + backend v5.3.2** | Step C (remove yaml-mirrored `-e` flags from nix ExecStart) + I26 invariant + lint script | both above |
| **backend v5.4.0 (caching)** | CE + embed cache from `PLAN_BACKEND_V5_4_CACHING.md`. Cache knobs land yaml-only from day one. | optional after Step C; soft-prefers post-cleanup discipline |

## Sequencing vs v5.4.0 caching

**Recommended order:**
1. v5.7.10 core (yaml backfill for v5.6.x/v5.7.x knobs) — no image-runtime risk, all-or-nothing yaml edit.
2. Backend v5.3.1 (yaml backfill for v5.3.0 knobs) — same.
3. Backend v5.4.0 (CE + embed caches) — ships with cache knobs already yaml-registered by step 2's pattern.

Step 3 builds on the discipline established in 1+2. If you'd rather ship the caching impact first, swap order: v5.4.0 first with yaml registration baked in, then v5.7.10 + v5.3.1 backfill the older knobs.

User call.
