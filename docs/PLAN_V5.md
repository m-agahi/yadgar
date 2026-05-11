# Yadgar v5 Plan — Security, Bugs, Observability, Refactor

## Context

Local review on 2026-05-09 (`.local-review/review-20260509-215742.md`, 25 tickets in `.local-review/tickets/`) found 5 critical, 24 high, 17 medium, and 9 low/info findings. Headline risk: any web page the user visits can read or mutate the full memory graph because the MCP API has wildcard CORS, no auth, and SurrealDB ships with hardcoded `root:root` across every install path. Several non-atomic operations and race conditions can corrupt or silently drop data. 26 None-dereference sites will crash at runtime.

v4.x shipped through 4.4.10 (perf, async memorize, DLQ, three-tier DB users with 1Password). v5 is one breaking release that closes the review backlog end-to-end and ships the observability we never had. Version bump: `4.4.10 → 5.0.0`.

Single release because the security work is breaking (auth model, default-deny CORS, env-var-required credentials) and the rest doesn't merit a second tag.

---

## Security

### 1. Credentials: no defaults, fail closed

**Relationship to v4.4.7 three-tier DB user model:** v4.4.7 already plumbs `YADGAR_RW_USER/PASS` + `YADGAR_RO_USER/PASS` (`entrypoint-backend.sh:33-44`, `scripts/setup.sh:199-202`) from 1Password. That layer is the DB user model and stays. v5 closes the remaining gaps: (a) hard-coded `root:root` literals that are still the *fallback* path when those env vars are unset, and (b) a new MCP API bearer token, separate from DB users — needed because no DB credential protects the MCP HTTP surface today. The 1Password integration extends to mint the new bearer token (`op://Private/yadgar/MCP_AUTH_TOKEN`).

Remove every `root:root` literal. Locations from review (C1):

- `Dockerfile.backend:19-20`
- `entrypoint-backend.sh:13`
- `scripts/setup.sh:25-26`
- `scripts/migrate_to_server.py:78-82`
- `scripts/migrate_v235_to_v204.py:50-54,73`
- `scripts/test_server_compat.py:23-27,45`
- `yadgar/storage.py:243-249` (soft warning → hard error)

Mechanism:
- Bash: `${SURREAL_PASS:?SURREAL_PASS is required}` — startup fails if unset.
- Python: `os.environ["YADGAR_DB_PASS"]` with `KeyError` propagated.
- Escape hatch: `YADGAR_ALLOW_ROOT=1` for tests only.
- Install path: generate random password at first run, write to `~/.yadgar/credentials` (chmod 600), read from there.

`scripts/setup.sh:215-260` currently interpolates passwords into a mode-644 systemd unit — switch to `EnvironmentFile=/etc/yadgar/secrets.env` (chmod 600 root-owned).

`entrypoint-backend.sh:43-44,66-68` interpolates `YADGAR_RW_PASS` directly into SurrealQL string literals — switch to parameterized statements (same fix pattern as v4.3 wiki_add escape).

### 2. MCP API: auth + default-deny CORS

`yadgar/server.py:156-169` currently sets `allow_origins=["*"]`. Replace:

- Default `allow_origins=["http://127.0.0.1:*", "http://localhost:*"]`; configurable via `YADGAR_ALLOWED_ORIGINS`.
- Add HTTP bearer-token middleware to every `/hooks/*` and `/api/*` route. Server reads the token from `YADGAR_MCP_AUTH_TOKEN` env var (sourced from 1Password at install time, written into the systemd `EnvironmentFile`).
- **Client-side mechanism:** Claude Code hook config (`~/.claude/settings.json`) gains a per-hook `env` block with `YADGAR_MCP_AUTH_TOKEN`. `install_hooks` injects the current token value when writing settings.json. Hook scripts read the env var and add `Authorization: Bearer $YADGAR_MCP_AUTH_TOKEN`. MCP transport: same env-var lookup. Token rotation = rerun `install_hooks` after refreshing the 1Password entry.
- `/health` and `/metrics` stay unauthenticated **on loopback only** (Prometheus scrapers don't carry per-request tokens). Bind both to 127.0.0.1; external scrapes need a reverse proxy.

Bind defaults: `YADGAR_HOST=127.0.0.1` everywhere — `scripts/setup.sh:243`, `yadgar/viz_server.py:35`, `entrypoint-backend.sh:11-13`. Loopback unless user opts into LAN exposure with explicit env.

### 3. install_hooks: no shell injection

`yadgar/server.py:1719-1825` interpolates `project_directory` into the `command` field of `settings.json`. A path containing `$(...)` runs as code (C3).

Fix: ship the hook logic as a real script (`yadgar/scripts/hook_runner.py`), reference it by absolute path in `settings.json`, and pass the directory as `argv[1]`. No shell interpolation, no multiline `python3 -c` (which also fixes Q4 — JSON newlines breaking the lockdown hook).

### 4. Path-traversal & file-hash oracle

`yadgar/server.py:774-782`: `memorize(context=<path>)` resolves and hashes any file the daemon can read; hash is then queryable via `/api/graph`. Combined with wildcard CORS, a visited web page can fingerprint `~/.ssh/id_rsa` (C4). Also: `p.read_bytes()` on unbounded file OOMs the daemon.

Fix:
- Whitelist: only hash files under directories registered as project roots via `seed_project`.
- Cap: skip files larger than `YADGAR_MAX_HASH_BYTES` (default 10 MB); stream-hash in 64 KB chunks (no `read_bytes`).
- Strip the hash from the public `/api/graph` payload — keep it server-side only.

### 5. SurrealQL record-ID & input safety

`yadgar/storage.py:2489-2515` (`update_memory_fields`, `update_memory_last_accessed`, +4 others): builds `f"UPDATE memory:{memory_id}"` without casting. Replicate the `int(memory_id)` pattern from `get_memory:931` across all six call sites. Also covers `cls_store.py`, `curation.py`, `sleep_compute.py` per review.

`yadgar/storage.py:471-479` (`_q` retry, Q3): retry policy currently re-runs CREATE/UPDATE/DELETE on any exception, including successful-write-then-read-error → double-inserts. Restrict retry to read-only statements (`SELECT`, `INFO FOR`, `SHOW`).

`yadgar/storage.py:516-526` (`batch_writes`, Q7): regex param-name rewrite over raw SQL corrupts user content containing `$id`/`$content`. Rewrite as a proper tokenizer or use SurrealDB's native param binding per statement.

### 6. Atomicity for destructive operations

- `yadgar/storage.py:1242-1256` (`recreate_vector_table`, C5): DROP INDEX → `UPDATE memory SET embedding = NONE` → REDEFINE INDEX, not in a transaction. Wrap in `BEGIN TRANSACTION; … COMMIT TRANSACTION;`. Add a pre-flight backup of `embedding` to a sidecar table before the DROP so recovery is possible even if the transaction fails.
- `yadgar/storage.py:2225-2249, 2287-2342` (Q15): `insert_checkpoint`, `insert_profile`, `upsert_file_hash`, `replace_wiki_crossrefs` do read-modify-write outside a TX → duplicate active checkpoints + empty crossref tables under concurrency. Wrap each in a single TX.
- `yadgar/seed.py:911-953` (Q17): re-seed deletes existing memories then partially inserts new ones; mid-loop exception leaves DB worse than before. Build new memories first; delete only after successful bulk insert.

### 7. Auto-capture log poisoning

`yadgar/server.py:281-345` (`/hooks/auto-capture`): no auth, no rate limit, `summary` + `directory` go straight into the action log surfaced into Claude's context — direct prompt-injection vector.

Fix: bearer-token auth (point 2 covers this), per-source rate limit (token-bucket on `directory` key), sanitize control chars / strip ANSI escapes / cap field length before storage.

### 8. ConceptNet & urllib hardening

- `yadgar/enrichment.py:322`: `term` interpolated into ConceptNet URL without `urllib.parse.quote`. Switch to `httpx` (HTTPS-only) and quote terms.
- `yadgar/__main__.py:198` + 9 other locations: `urllib.request.urlopen` accepts `file://`. Replace with `httpx` or validate scheme `in {"http","https"}` before each call.

---

## Bugs & correctness

### 9. Async-loop correctness

- `yadgar/server.py:281-345` (Q1): wrap blocking SurrealDB calls in `asyncio.to_thread`.
- `yadgar/server.py:209-221` (Q5): swap `httpx.get` for `httpx.AsyncClient.get` with `await`.
- `yadgar/server.py:321-329` (Q2): protect `_action_batch` with `asyncio.Lock`; flush+swap under the lock.
- `yadgar/server.py:587, 2422-2432` (Q6): snapshot `_system_metrics_cache` under a `threading.Lock` before serializing.

### 10. Logic bugs + dead code

- `yadgar/thermodynamics.py:140` (Q12): `hours_elapsed = max(0.0, hours_elapsed)` — Hypothesis-confirmed.
- `yadgar/thermodynamics.py:185` (Q18): same `max(0.0, hours)` clamp in `apply_session_coherence` — fuzz edge case.
- `yadgar/thermodynamics.py:84`: `compute_importance` can never reach exactly 1.0 (IEEE 754 ordering). `return round(min(score, 1.0), 10)`.
- `yadgar/causal_discovery.py:112-116` (Q13): coerce `datetime.fromisoformat` to tz-aware via `.replace(tzinfo=timezone.utc)` if naive.
- `yadgar/causal_discovery.py:336-338` (Q11): add the missing non-adjacency precondition to Meek's R3 (`not adjacency[z1][z2]`).
- `yadgar/rules_engine.py:220` (Q19, ticket 0021): `evaluate_condition` returns `True` on parse error → hard filter silently disabled. For hard rules return `False` on parse failure; log at WARN.
- `yadgar/storage.py:369-374` (Q20): `_bytes_to_floats` no length validation. Assert `len(data) % 4 == 0` and `n == expected_dim`; raise `ValueError` otherwise.
- `yadgar/retrieval/reranking.py:619` (Q8): dead z-score computation. Read the original PR; either wire it into ranking confidence or delete.
- `yadgar/knowledge_graph.py:197` (Q9): always-false branch (`fname == m.group(1)` then `!=` comparison). Remove.
- `yadgar/predictive_coding.py:331` (Q10): `content.lower()` discarded. `content_lower = content.lower()` and use it.

### 11. Atomic settings.json / CLAUDE.md writes

`yadgar/server.py:1807, 1831, 1911, 1933` (Q14): non-atomic writes to `~/.claude/settings.json` and `CLAUDE.md`. Crash mid-write truncates the user's Claude Code config. Use `tmp + os.replace` (same pattern as `file_queue.py`).

### 12. Resource hygiene

- **Bare `except Exception: pass`** (ticket 0018). Replace silent swallow with `log.warning("...", exc_info=True)` at minimum so corruption is visible. Sites:
  - `yadgar/config.py:27-28` (YAML config load)
  - `yadgar/storage.py:343-344, 975-976, 1236-1240, 1318-1322` (backup restore, delete_memory, update_memory_embedding, update_memory_compression)
  - `yadgar/server.py:391-393, 460-462, 2105-2107` (wiki blending, file-queue archive, DLQ alerts)
- **Unbounded module-level dicts** `server.py:107, 138, 1240`. Wrap with `functools.lru_cache` or bounded `OrderedDict` (max 1000 entries).
- **`_run_migrations` no lock** `storage.py:580-604`. Filesystem `flock` on `~/.yadgar/.migration.lock` for the duration; two daemons starting concurrently must serialize.
- **`init_engram_slots` int/float mismatch** `storage.py:2153-2167`. Cast `r` to `int` before set membership; otherwise every restart re-inserts all 5000 slots.
- **Double shutdown** Q16, `server.py:2526-2530, 2589-2592`. `_signal_handler` + `main()` finally both call `shutdown()`. Add `_shutdown_done = False` guard; close paths must be idempotent.
- **`cmd_context` StorageEngine leak** Q21, `__main__.py:111-159`. Wrap in `try/finally` with `storage.close()`; otherwise SurrealKV lock file leaks and blocks reopen.
- **Dead `_db_locked` TOCTOU helper** `yadgar/hooks/post-tool-capture.py:21-32`. flock released before the HTTP call, no protection. Delete.
- **`server.py:752-770`** double-checked `_file_queue` / `_queue_drainer` init: if `_queue_drainer.start()` raises, `_file_queue` is assigned with a dead drainer. Restructure so assignment happens after `start()` succeeds.

### 13. None-dereference runtime crash sites (ticket 0025)

26 sites flagged by mypy/pyright as guaranteed runtime crashes when the optional field is unset. Each site classified raise-vs-skip up front:

| Site | Dep | Action |
|------|-----|--------|
| `yadgar/embeddings.py:194, 257, 271` | embedding model | **Raise** — required for any retrieval; fail fast at startup, not at use. Eager-load in `__init__`. |
| `yadgar/server.py:2465` | embedding engine | **Raise** — required. |
| `yadgar/enrichment.py:299, 393, 433, 438, 454, 489, 512, 517, 529` | ConceptNet / COMET / doc2query / logic | **Skip** — each is optional. Gate the call site behind the corresponding `_enabled` flag; log debug if disabled. No raise. |
| `yadgar/retrieval/reranking.py:242, 304, 367, 403` | cross-encoder + monoT5 | **Skip** — optional. Gate behind `RERANK_CROSS_ENCODER_ENABLED` / `RERANK_MONOT5_ENABLED`. |
| `yadgar/retrieval/core.py:112` | NLI inferencer | **Skip** — optional. Gate behind `RETRIEVAL_NLI_ENABLED`. |
| `yadgar/cognitive_map.py:141, 144, 148` | embedding matrix | **Raise** — internal invariant, not optional; means cognitive map was used before populated. |
| `yadgar/sensory_buffer.py:35-37, 100-104` | buffer dict | **Raise** — set in `__init__`; None means programming error. |
| `yadgar/causal_discovery.py:281-294` | adjacency matrix | **Raise** — internal invariant. |
| `yadgar/metacognition.py:457-461` | datetime field | **Raise** — set at memory creation time; None means corrupt record. |
| `yadgar/storage.py:2500` | type bug (`bool` → `list[float]`) | **Type fix** — not a None-guard; review original PR, return correct type. |
| `yadgar/narrative.py:115` | type bug (`int` → `Sequence[str]`) | **Type fix** — same. |

Pattern for raise sites: `if x is None: raise RuntimeError("embedding engine not initialized")` at method entry. Pattern for skip sites: wrap call in `if self._enabled:` block; return empty/identity result otherwise.

The other 99 mypy errors stay in the backlog hygiene project.

---

## Performance

### 14. Hot-path perf fixes (D3)

- **`yadgar/predictive_coding.py:233-237, 295-298`** — `get_all_entities()` + full `all_memories` iteration on every write-gate evaluation. O(N·M); unusable at 10k+ memories. Cache entity set under a TTL (5 min) and pre-filter memories by directory_context.
- **`yadgar/consolidation.py:678-692`** — `_merge_duplicates` is O(N²) Python pairwise. Use the same numpy matmul pattern as `_link_similar_memories`. (Note: a prior pure-numpy attempt did not improve `process_episodes`; this is a different function and the bottleneck is the Python-level cross product, not embedding I/O.)

---

## Observability

### 15. Metrics, logs, phase markers

Three concrete additions, all opt-in via `YADGAR_METRICS_ENABLED`:

- **Prometheus endpoint** at `/metrics` (gated by the same bearer-token auth). Expose: consolidation cycle duration broken down by phase, queue depth (`queue/`, `archive/`, `dlq/`), DB query p50/p95, embedding-cache hit ratio, request count by route, action-batch size.
- **Structured JSON logs** behind `YADGAR_LOG_FORMAT=json` (default stays human). Per request: `request_id`, `tool_name`, `duration_ms`, `status`. Trace ID propagates from MCP client header if present.
- **Consolidation phase markers** (extends 4.4.6 work): explicit `phase_start` / `phase_end` log lines with duration so the daily 18:30 UTC cycle is auditable.

### 16. Wiki backup automation

v4.1.3 added wiki durability but there's no scheduled export. Add a parallel of the SurrealDB backup loop in `entrypoint-backend.sh`:

```bash
# wiki snapshot: every 6 hours, alongside backup_*.surql.
# SurrealDB /sql endpoint takes raw SurrealQL in body with Content-Type: text/plain
# (same shape entrypoint-backend.sh:45 already uses for the bootstrap SQL).
curl -sf -u "${SURREAL_USER}:${SURREAL_PASS}" \
  -H "Surreal-NS: yadgar" -H "Surreal-DB: main" \
  -H "Content-Type: text/plain" \
  -X POST --data "SELECT * FROM wiki_page;" \
  -o "/data/wiki_$(date +%Y%m%d_%H%M%S).jsonl" \
  http://127.0.0.1:8000/sql
find /data -name 'wiki_*.jsonl' -mtime +14 -delete
```

---

## Features

### 17. Fetch by integer ID — new MCP tools

`storage.get_memory(memory_id: int)` and `storage.get_wiki_page(page_id: int)` already exist but no MCP tool wraps them. `wiki_read(slug)` is slug-only.

Add two tools in `yadgar/server.py`:

- **`memory_get(memory_id: int) -> dict | None`** — strip embedding bytes from response, otherwise pass-through.
- **`wiki_get(page_id: int) -> dict | None`** — strip embedding bytes, include `slug`, `title`, `category`, `tags`, `content`, `confidence`, `source_memory_ids`, `created_at`, `updated_at`.

Both `power=True` (matches existing `wiki_read` classification).

ID sources the model will chase: `wiki_crossref.from_id`/`to_id` rows surfaced by graph queries, `source_memory_ids` arrays inside wiki pages, DLQ `.error.json` sidecars, action-batch log entries, consolidation phase markers, and `_retrieval_score` debug output from `recall`.

---

## Refactor

### 18. Duplication hotspots (D6)

- **`yadgar/enrichment.py:375-396` vs `:471-492`** — 21-line duplicate. Extract `_apply_enrichment_source(src, term, ...)` helper.
- **`yadgar/causal_discovery.py:236-249` vs `:258-271`** — 13-line duplicate traversal. Extract `_traverse_oriented_edges(...)` helper.
- `yadgar/seed.py:347-356` — two `if/elif` branches do identical dict-append. Merge with `or` (SonarQube S1871).

**Mega-function decomposition deferred to v5.1.** `retrieval/core.py:305` (cognitive complexity **330**) and `causal_discovery.py:187` (complexity **146**) need characterization tests written first — current coverage is 0%, and a pipeline-stage rewrite bundled with breaking auth changes makes the PR un-reviewable and rollback ugly. v5.1 ships: (1) characterization test suite snapshotting current outputs, (2) decomposition, (3) parity check against snapshots. The other 72 functions over cognitive complexity 15 stay opportunistic — fix when touching the code.

---

## Dependencies & CVEs

### 19. CVE bumps (D1)

- `python-multipart 0.0.26 → 0.0.27` — CVE-2026-42561 (HIGH, prod runtime via FastAPI/Starlette transitive).
- `pytest 9.0.2 → 9.0.3` — CVE-2025-71176 (MEDIUM, dev/CI only).

```bash
# python-multipart is transitive — add a direct floor in pyproject.toml so the bump sticks:
#   dependencies = [..., "python-multipart>=0.0.27"]
uv lock --upgrade-package python-multipart
# pyproject.toml: "pytest>=9.0.3" in dev extras
uv lock
```

Verify `uv pip list | grep python-multipart` shows 0.0.27 after lock.

---

## Roadmap leftovers (from wiki `yadgar-roadmap-future-improvements`)

After audit on 2026-05-11, six items remain open. Fold into v5:

- **`test_memorize_latency.py`** (v4.4 plan called for it, never written). Use `time.perf_counter` around in-process `server.memorize()`. Assert < 5 ms.
- **`_engines` `scope="module"` fixture leak audit.** `test_memory_behavior.py:28` + `test_frontier_integration.py:22` mutate `server.*` module globals (`_write_gate._threshold`, `_curator`, `_settings`). Walk both files, add `try/finally` teardown matching `test_frontier_integration.py:157-168` pattern.
- **`surreal start` subprocess leak.** `surreal_server` session fixture in `yadgar/tests/conftest.py:32` races with pytest-xdist worker cleanup. Add explicit-kill pattern + track PID file.
- **`wiki_list` push filter to DB.** `yadgar/server.py:2173` currently slices in Python (`pages[:limit]` + Python `startswith` on `slug_prefix`). Push `LIMIT`, `WHERE category =`, `WHERE string::starts_with(slug, $prefix)` into the SurrealQL.
- **`cache-to: mode=max` size prune.** `.forgejo/workflows/build.yml` caches every layer indefinitely. Add a periodic prune step or switch to `mode=min`.
- **HF model cache key includes model name.** Current key `hf-models-v1-${{ hashFiles('pyproject.toml') }}` doesn't invalidate when `EMBEDDING_MODEL` changes without a deps bump. Include the model name literal.

Dropped after 2026-05-11 review: memorize/wiki_add < 50 ms (wire-bound), `~` operator audit (done), `.github/workflows/ci.yaml` dead code (PR #41), Build Cloud detour split (current setup correct), per-op-type retry policy (no DLQ data), DLQ web UI, `_has_unpaired_surrogate` typed wrapper, auto-requeue on schema migration, CRDT `provenance_agent` per-host default.

---

## Container & misc

### 20. Dockerfile HEALTHCHECK + trivial cleanup (D4)

- Add `HEALTHCHECK CMD curl -f http://localhost:8765/health || exit 1` to `Dockerfile` and `HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1` to `Dockerfile.backend`. Both images already install `curl` (Dockerfile:5, Dockerfile.backend:6).
- (Skip base-image digest pinning per user decision 2026-05-11.)
- Delete `requirements.txt` — repo uses `uv`, file is stale (missing `scipy`, `httpx`, `ruamel.yaml`) and used by nothing in build or CI.
- Bump `ruff target-version "py311" → "py314"` in `pyproject.toml` (yadgar requires Python ≥ 3.14).

---

## Execution order

Single version bump, but ship as five sequential commits on the same branch so each is independently reviewable and any can be reverted without unwinding the rest:

1. **Credentials + auth** (§1, §2, §3, §7). Breaking — ship first behind feature flag `YADGAR_REQUIRE_AUTH=1` initially, flip to default-on at end of commit. This includes the install_hooks rewrite (§3) and the systemd EnvironmentFile + 1Password integration for `YADGAR_MCP_AUTH_TOKEN`.
2. **Bugs + None-dereference + atomicity** (§4-§8, §9-§13, plus §6 RMW TX wraps). No public API change; pure correctness.
3. **Resource hygiene + perf** (§12, §14). Bare-except logging, LRU bounds, migration lock, engram cast, shutdown idempotency, predictive_coding cache, _merge_duplicates numpy.
4. **Observability + features** (§15, §16, §17). Additive only — `/metrics`, JSON logs, wiki backup loop, `memory_get` / `wiki_get`.
5. **Refactor + CVE + container + roadmap leftovers** (§18, §19, §20, Roadmap leftovers, plus `pyproject.toml` version bump to 5.0.0).

CI must be green after each commit. The breaking commit (1) is the only one users need to read `MIGRATION_NOTES.md` for.

## Files touched

| File | Change |
|------|--------|
| `Dockerfile.backend`, `entrypoint-backend.sh`, `scripts/setup.sh`, `scripts/migrate_*.py`, `scripts/test_server_compat.py` | Remove `root:root` defaults; fail-closed env-var pattern; EnvironmentFile pattern |
| `yadgar/storage.py` | Hard error on missing creds; int-cast record IDs; restrict `_q` retry; tokenize `batch_writes`; TX for `recreate_vector_table` + Q15 RMW sites; `_bytes_to_floats` validation; migration lock; `init_engram_slots` cast |
| `yadgar/server.py` | Bearer auth middleware; default-deny CORS; `install_hooks` → real script; path-hash whitelist + size cap; auto-capture sanitization; `asyncio.to_thread`, `AsyncClient`, locks (Q1/Q2/Q5/Q6); atomic settings.json (Q14); shutdown idempotency (Q16); double-checked init fix; LRU module dicts; new `memory_get` + `wiki_get` MCP tools; `wiki_list` DB-side filter |
| `yadgar/scripts/hook_runner.py` (new) | install_hooks payload as real script |
| `yadgar/viz_server.py` | Default `host=127.0.0.1` |
| `yadgar/enrichment.py` | HTTPS + `urllib.parse.quote`; 9 None-dereference guards; extract duplicate enrichment-source helper |
| `yadgar/__main__.py` (+ 9 other call sites) | `urllib.request` → `httpx`; `cmd_context` try/finally (Q21) |
| `yadgar/embeddings.py`, `retrieval/reranking.py`, `retrieval/core.py`, `cognitive_map.py`, `sensory_buffer.py`, `metacognition.py`, `narrative.py` | None-dereference guards |
| `yadgar/thermodynamics.py` | `max(0.0, hours)` (Q12/Q18); `compute_importance` clamp |
| `yadgar/causal_discovery.py` | tz-aware datetime (Q13); Meek R3 precondition (Q11); decompose 146-complexity function into R1–R4 methods; extract duplicate traversal helper |
| `yadgar/rules_engine.py` | Hard-rule fail-closed (Q19) |
| `yadgar/knowledge_graph.py`, `yadgar/predictive_coding.py`, `yadgar/retrieval/reranking.py` | Dead-code fixes (Q9/Q10/Q8) |
| `yadgar/predictive_coding.py`, `yadgar/consolidation.py` | Perf hotspots (D3) |
| `yadgar/retrieval/core.py` | Decompose complexity-330 function into pipeline stages |
| `yadgar/seed.py` | Re-seed atomicity (Q17); merge identical if/elif (S1871) |
| `yadgar/config.py` | Log YAML config errors instead of swallowing |
| `yadgar/hooks/post-tool-capture.py` | Remove dead `_db_locked` TOCTOU helper |
| `yadgar/metrics.py` (new) | Prometheus collectors |
| `yadgar/log_config.py` (or extend existing) | JSON formatter, request-id middleware |
| `entrypoint-backend.sh` | Wiki snapshot loop |
| `Dockerfile`, `Dockerfile.backend` | HEALTHCHECK |
| `docker-compose.yml` | `read_only: true`, `tmpfs: [/tmp]`, `security_opt: [no-new-privileges:true]` |
| `yadgar/tests/conftest.py` | `surreal_server` fixture explicit-kill + PID tracking |
| `yadgar/tests/test_memory_behavior.py`, `test_frontier_integration.py` | `_engines` teardown audit |
| `yadgar/tests/test_memorize_latency.py` (new) | `< 5 ms` in-process assertion |
| `yadgar/tests/test_security_headers.py`, `test_credentials_required.py`, `test_install_hooks_injection.py`, `test_path_traversal.py`, `test_recreate_vector_table_atomicity.py`, `test_thermodynamics_negative_time.py`, `test_async_handlers_no_block.py`, `test_metrics_endpoint.py`, `test_wiki_backup.py`, `test_memory_get_wiki_get.py`, `test_rules_engine_hard_fail_closed.py`, `test_bytes_to_floats_validation.py`, `test_none_dereference_guards.py` (new) | Coverage for every security/correctness item |
| `.forgejo/workflows/build.yml` | Cache prune step; HF cache key includes model name |
| `pyproject.toml` | Bump `4.4.10 → 5.0.0`; add `prometheus-client`; `pytest>=9.0.3`; `ruff target-version = "py314"` |
| `uv.lock` | `python-multipart 0.0.26 → 0.0.27` |
| `requirements.txt` | Remove or regenerate (out of date) |
| `docs/configuration.md` | Document `YADGAR_DB_PASS`, `YADGAR_AUTH_TOKEN`, `YADGAR_ALLOWED_ORIGINS`, `YADGAR_HOST`, `YADGAR_METRICS_ENABLED`, `YADGAR_LOG_FORMAT`, `YADGAR_MAX_HASH_BYTES` |
| `MIGRATION_NOTES.md` | Operator steps: generate creds, point hooks at new auth token, update systemd unit, bump container images |

---

## Verification

### Security
1. `pytest tests/test_credentials_required.py` — start with no env var, assert process exits.
2. Manual: visit `http://attacker.example` with fetch to `http://localhost:<port>/api/graph` — assert browser blocks (CORS) AND server rejects 401 even with `Origin` spoofed (no token = no access).
3. `pytest tests/test_install_hooks_injection.py` — call `install_hooks(project_directory="/tmp/$(touch /tmp/pwned)")`, assert no file created.
4. `pytest tests/test_path_traversal.py` — `memorize(context="/etc/passwd")` outside project roots returns refusal; file too large is skipped.
5. `semgrep --config=auto yadgar/` — assert 0 critical findings.

### Correctness
6. `pytest tests/test_recreate_vector_table_atomicity.py` — inject failure between DROP and REDEFINE, assert embeddings recoverable from sidecar.
7. Hypothesis property test: `compute_decay(t<0)` and `apply_session_coherence(t<0)` never return > input.
8. `pytest tests/test_rules_engine_hard_fail_closed.py` — parse error on hard rule yields `False`.
9. `pytest tests/test_bytes_to_floats_validation.py` — buffer not divisible by 4 raises `ValueError`.
10. Load test: 50 concurrent `/hooks/auto-capture` POSTs — no lost entries (Q2), no `RuntimeError` (Q6), p95 latency below threshold (Q1/Q5).

### Observability
11. `curl /metrics` returns Prometheus text format; `consolidation_phase_duration_seconds` histogram populated after one daily cycle.
12. `YADGAR_LOG_FORMAT=json` produces JSON with `request_id`, `tool_name`, `duration_ms`, `status`.

### Data
13. Force a wiki write, confirm `wiki_*.jsonl` appears in `/data` within the next interval, confirm 14-day retention.
14. `pytest tests/test_memory_get_wiki_get.py` — both tools return expected shape; embedding bytes stripped.

### Perf
15. `pytest tests/test_predictive_coding_perf.py` — write-gate eval at N=10000 memories completes in < 100 ms.
16. `pytest tests/test_merge_duplicates_perf.py` — numpy path matches Python output and runs ≥ 10× faster at N=500.

### Type safety
17. `mypy yadgar/embeddings.py yadgar/enrichment.py yadgar/retrieval/` — 0 runtime-crash class errors (None.method).

### CI
18. Workflow re-runs green; HF model cache invalidates when `EMBEDDING_MODEL` literal changes.

---

## Out of scope (deferred to v5.1 or never)

- **server.py / storage.py module split** — 2596 + 2954 LoC; real debt but no behaviour change. Decompose alongside touching code; full split deferred until maintenance pain forces it.
- **Remaining 72 cognitive-complexity-15+ functions** — fix opportunistically.
- **Remaining 99 mypy errors** (the 26 None-dereference fixes are in v5; the rest is a hygiene project).
- **Per-tool granular auth** — single bearer token is sufficient for laptop deployment; revisit if multi-user appears.
- **GHA security hardening** (ticket 0022) — most rules referenced GitHub-only constructs; Forgejo migration moot.
- **`llm.nix` backup DST fix** — already in `TODO.md`; nix-repo concern.
- **CRDT multi-agent sync, dual-vectors, prospective-memory wiring** — speculative, no consumer.
- **DLQ web UI, per-op retry policy, auto-requeue on schema migration** — speculative until concrete data appears.
