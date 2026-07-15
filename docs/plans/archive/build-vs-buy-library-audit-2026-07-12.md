> Archived 2026-07-16 — decided = ADR-0103 (keep custom); stamina adoption = task #33.

# Build-vs-Buy Library Audit — hand-rolled infrastructure vs battle-hardened libraries (2026-07-12)

**Status: REVIEW — user decision pending, no build without approval.**

**Motivation (user, verbatim):** "im afraid our usage is not thorough enough to make sure we have
fixed all the edge cases. same thing goes with other modules we are maintaining. if there are good
and well maintained battle hardened libraries, we should investigate the pros/cons of switching
some or all parts of the said module."

**Method:** 5 parallel deep audits (cache; queue+DLQ+retry; config/knob; observability;
migrations/backup/scheduler/vacuum), each applying the same rubric: (a) our LOC + test coverage,
(b) bug-history mining with LIBRARY-PREVENTABLE vs DOMAIN-SEMANTICS classification per bug class,
(c) candidate library health from primary sources (PyPI `/json` metadata: latest version, release
date, `requires_python`; license), (d) what stays custom regardless, (e) migration cost S/M/L +
risk, (f) verdict. Audited against **master** (`33a2f3f4`-descended, core 5.130.0 / backend 5.41.0).
Concurrent `feat/deps-modernization-train` (transformers 5.x) is orthogonal — zero overlap; nothing
here touches that train's scope.

**Binding constraints applied to every verdict:**
- **2-container self-sufficiency** (ADR-0101 spirit; redis already rejected): no new daemons,
  services, brokers, or external binaries. Disqualifies huey/dramatiq (broker) and restic/borg
  (binary) on arrival.
- **py3.14** (`requires-python = ">=3.14"`): candidate must credibly support 3.14. Several
  otherwise-plausible libs fail here (diskcache, litequeue, APScheduler — see sections).
- **Zero-warning gate:** every new dep's deprecation surface joins the CI gate; under the
  blanket-lock policy each dep is also an upgrade-treadmill commitment.
- **ADR-0048:** unified Cache = ONE class, N namespaces, policy bound at construction — any cache
  switch must preserve that shape.
- **ADR-0078:** backend owns all DB I/O; core = HTTP forwarding + response caches.

---

## BLUF — verdict table

| # | Subsystem | Ours (src LOC) | Test LOC (ratio) | Best candidate | Candidate health | Verdict | Size | Payoff |
|---|---|---|---|---|---|---|---|---|
| 1 | Cache (backend+core+epoch bus) | 1,508 | ~5,400 (3.6:1) | diskcache / cachetools | diskcache: **stale 2023-08**, no py3.14 CI. cachetools: healthy 2026-05, MIT | **KEEP-CUSTOM** | — | Lib saves <10% LOC; diskcache kills RAM hot path |
| 2 | File queue + DLQ + drainer | 1,509 | ~3,162 (2:1) | persist-queue / litequeue | persist-queue 1.1.0 OK; litequeue stale 2024-08, py3.14 unverified | **KEEP-CUSTOM** | — | Lib replaces ~150 LOC primitives, breaks wait_for_job contract |
| 3 | Retry/backoff (6 diffuse sites) | ~255 diffuse | **0 dedicated** | tenacity 9.1.4 / stamina 26.1.0 | Both active 2026, Apache-2.0/MIT | **SWITCH-PARTIAL** (2 of 6 sites) | S | −50 LOC + library-correct timing; **real fix = write the missing tests** |
| 4 | Config/knob (Settings+registry+CLI) | 3,815 (≈1,719 logic) | ~2,831 (18 files) | pydantic-settings (ALREADY a dep) | 2.14.2, healthy, py3.14 OK | **KEEP-CUSTOM** | — | Only ~50 LOC absorbable (costs ruamel comments). Real cleanup: KnobSpec unification, no lib |
| 5 | Observability (@observe/tracing/metrics) | 3,359 | ~6,587 (2:1) | pure OTel (ALREADY a dep) | opentelemetry 1.30+, healthy | **KEEP-CUSTOM** | — | Tier system + I33 allowlist has NO OTel equivalent; custom pieces fix real OTel SDK gaps |
| 6 | Migrations runner (SurrealDB) | 1,367 (runner ≈30) | ~35 test fns | none exists | alembic/yoyo = SQL-dialect only | **KEEP-CUSTOM** | — | No SurrealQL migration lib exists; runner is 30 LOC |
| 7 | Backup/rotation | 434 | ~51 test fns | restic/borg | external binaries | **KEEP-CUSTOM** | — | Violates self-sufficiency; libs don't cover surql-export/quiesce modes |
| 8 | Scheduler/daemon loops + vacuum | ~1,700 | ~100 test fns | APScheduler | 3.x maintenance-only (2023), 4.x still **alpha**, no py3.14 cert | **KEEP-CUSTOM** | — | Zero historical bugs were scheduling bugs; loops already externalized to systemd |

**Bottom line:** one SWITCH-PARTIAL (retry, small), seven KEEP-CUSTOM. The user's fear is
legitimate but the bug-history evidence points the other way than "adopt libraries": of ~12
significant bug classes mined from git history, **only 2 were library-preventable** (v4.3 retry
storm without a cap; #53 drainer file-theft race) — **and both are already fixed**. The other 10
(CE `_ckpt` wrong-model keying #188, torn surrealkv vacuum segment, drainer branch defaults,
DLQ taxonomy gaps, phantom knobs, log-path span flood, OTel blocking shutdown, prune-mtime,
drainer-never-started wiring, migration numbering collisions) are domain-semantics bugs that no
general-purpose library models. The highest-value actions this audit surfaces are NOT switches —
they are three **test/hardening gaps** in our own code (§Sequencing).

---

## 1. Cache engine — KEEP-CUSTOM

**Scope:** `yadgar/backend/cache/cache.py` (980), `yadgar/core/cache/cache.py` (378),
`yadgar/_shared/runtime/cache_epoch.py` (150). Byte-bounded LRU, TTL/KeyFn/Manual invalidation,
version-in-key (ModelCkpt / DataEpoch / ScopeVersions), msgpack disk snapshots, cross-process
epoch file bus.

**(a) Coverage:** 19 test files, ~5,424 LOC (3.6:1). Every build car has a characterization suite;
the 766-LOC cross-container invalidation e2e is the strongest signal. Gaps: no power-fail/chaos
test on snapshot corruption, no snapshot-parser fuzz.

**(b) Bug history:** exactly ONE cache-correctness bug shipped — #188 (`33a2f3f4`): `_ckpt` hashed
`YADGAR_CE_MODEL` (embed model) instead of `GTE_RERANKER_MODEL` — reranker swap served stale scores
across restarts. **DOMAIN-SEMANTICS**: no library knows which env var feeds which cache. Notably:
the feared "torn snapshot" class has NO git evidence — snapshot writes were already atomic
(`tmp.write_bytes` → `tmp.replace`) in both write paths, and the loader treats any parse failure as
a safe miss (magic bytes, version, ckpt-len validation → `return None`).

**(c) Candidates:**
- **diskcache 5.6.3** — last release **2023-08-31** (~3 years stale), `requires_python=">=3"`
  (blanket floor, zero py3.14 CI evidence), Apache-2.0. CRUX: SQLite-backed — every get/set is a
  disk round-trip. Our design is RAM-hot LRU (CE cache = `OrderedDict.get` on every recall) with
  periodic whole-cache snapshots. Adopting diskcache **inverts the performance model**: SQLite on
  the recall hot path. As a persistence-tier-only bolt-on it doesn't compose — you'd run two stores
  or abandon the RAM path. **Reject.**
- **cachetools 7.1.4** — active (2026-05-21), MIT, `>=3.10`, py3.14 fine. Pure in-RAM eviction
  policies, NO persistence. Would replace ~80 LOC (`OrderedDict` + `_evict_to_budget_locked`),
  needs custom `getsizeof` to stay byte-bounded (keeping our `_estimate_bytes`), leaves snapshot
  machinery, epoch bus, protocol, metrics all hand-rolled. v6→v7 already broke `keys()` semantics —
  a live upgrade-treadmill data point under blanket-lock.

**(d) Keep custom regardless:** version-in-key invalidation, epoch bus (cross-container without a
daemon = precisely what ADR-0101 forbids buying), `CacheProtocol` + `NullCache` DI, Prometheus
emission, cgroup-aware RAM-% byte budget, namespace factory/registry (ADR-0048 shape), snapshot
format. That's ~600 of 980 backend LOC — the library-replaceable slice is <10% of the subsystem.

**(e) Cost:** cachetools partial S (~80 LOC swap, ~50 tests rewritten); diskcache L + high risk
(perf inversion + stale dep).

**(f) VERDICT: KEEP-CUSTOM.** The evidence contradicts the fear here: 3.6:1 test ratio, one shipped
bug (domain), atomic writes already in place. Optional hardening if belt-and-suspenders wanted:
**CRC32 checksum on snapshot payload** (~15 LOC, validated on load) + a power-fail/fuzz test for
the loader. No dep needed.

---

## 2. File queue + DLQ + drainer — KEEP-CUSTOM

**Scope:** `_shared/file_queue/queue.py` (281), `backend/queue_drainer/` (~890: drain machinery,
DLQ mixin, apply dispatch), `core/server/tools/admin_dlq.py` (~220), `_shared/storage/queue.py`
(122). JSON-file-per-item queue, drainer with retry/backoff + error classification, DLQ with
`failure_reason` taxonomy, `.error.json` sidecars, `.events.log` audit trail, `wait_for_job`
filesystem-signal contract.

**(a) Coverage:** 14 test files, ~3,162 LOC (2:1). Covers enqueue/archive atomicity, taxonomy,
sim gate, branch/directory enforcement, concurrency lock, requeue/dismiss blocking.

**(b) Bug history (5 classes):**
- v4.3 retry storm (`b3157e92`) — surrogate-pair JSON rejected by SurrealDB, no retry cap →
  drainer hammered DB 24h at 60–95% CPU. **Partially LIBRARY-PREVENTABLE** (a lib would have
  capped+DLQ'd); the encoding bug itself was domain. Fixed — this incident *created* the DLQ.
- #53 file-theft race (`7c7d1dc7`) — `run()` + `drain_now()` concurrent `_drain_once`.
  **LIBRARY-PREVENTABLE** (SQLite locking gives this free). Fixed with `threading.Lock`.
- Drainer hardcoded `branch="master"` (`cece7c82`) — gate-scope mismatch. **DOMAIN.**
- Taxonomy gap (`dd01b6da`, `19d0ab90`) — rejections archived as success. **DOMAIN** (the taxonomy
  IS the yadgar layer).
- Drainer-never-started P0 (`bd280093`) — lifespan wiring. **DOMAIN.**

**(c) Candidates:**
- **persist-queue 1.1.0** (2025-10, BSD, py3.14 tested) — SQLite-WAL FIFO, solid. Replaces ~150 LOC
  of enqueue/archive/pending primitives. Does NOT provide: taxonomy, sidecars, audit log, sim-gate
  routing, `wait_for_job`. CRUX: `wait_for_job` polls archive/ vs dlq/ by filename as its terminal
  signal — the load-bearing contract for `wait=True` callers. Swapping storage breaks it directly.
- **litequeue 0.9** (2024-08, MIT) — `locked`/`failed` states are conceptually the better fit, but
  stale (11+ months no release), py3.14 unverified. Not production-grade for a memory daemon.
- **huey/dramatiq** — broker required. **Disqualified** (self-sufficiency).

**(d) Keep custom:** ~1,350 of 1,509 LOC is the semantic layer (taxonomy, validation, sim gate,
sidecars, wait_for_job, `is_draining()` re-enqueue guard, MCP operator tools, op dispatch).

**(e) Cost:** M either lib; primary risk = wait_for_job contract redesign.

**(f) VERDICT: KEEP-CUSTOM.** Both library-preventable bugs are fixed; the lib saves 150 LOC of
primitives and forces re-architecting the terminal-signal contract. Net negative. Revisit only if
the file-theft class recurs or wait_for_job is redesigned anyway.

---

## 3. Retry/backoff — SWITCH-PARTIAL (the one approved-if-you-agree switch)

**Scope:** NO dedicated module — 6 diffuse sites, ~255 LOC total, **zero dedicated test files**:

| Site | Mechanism | Fit for a library? |
|---|---|---|
| 1. `queue_drainer` `_Attempt`/`_record_failure` | per-file backoff-window skip, DLQ escalation | NO — poll-per-file model; tenacity wraps single calls |
| 2. `ml_client` `_CircuitBreaker` | CLOSED/OPEN/HALF_OPEN state machine | NO — tenacity has no CB semantics |
| 3. `_surreal_runner.allocate_port_with_retry` | linear loop, EADDRINUSE only | **YES** — textbook decorator case |
| 4. `nightly_cycle._run_systemctl_with_retry` | linear loop | **YES** — textbook decorator case |
| 5. `tracing._CircuitBreakerSpanExporter` | OTLP CB state machine | NO — CB, and OTLP-specific |
| 6. config knobs (backoff constants) | config only | N/A |

**(b) Bug history:** the v4.3 retry storm is the retry bug — **LIBRARY-PREVENTABLE** (missing cap).
Now capped via drainer thresholds. No other retry-labelled fixes; absence of evidence here is weak
evidence given zero direct tests.

**(c) Candidates (both healthy):** **tenacity 9.1.4** (2026-02, Apache-2.0, `>=3.10`, active);
**stamina 26.1.0** (2026-04, MIT, Hynek — tracks CPython early, structured logging +
instrumentation hooks built in). stamina preferred: opinionated, jitter-by-default, observability
out of the box.

**(d) Keep custom:** drainer poll model, both circuit breakers, yadgar error taxonomy
(4xx=permanent, SecretLeakBlocked=permanent).

**(e) Cost:** S — sites 3+4 only, ~1–2h, −50 LOC, +1 dep (its deprecation surface joins the gate).

**(f) VERDICT: SWITCH-PARTIAL** — stamina for sites 3+4; KEEP-CUSTOM for 1/2/5.
**The bigger finding:** zero dedicated retry-timing or CB-state-transition tests exist at ANY site.
That test gap — not the implementation style — is what let the v4.3 storm run 24h undetected. Write
those tests FIRST regardless of the stamina decision (they also become the safety net for the swap).

---

## 4. Config/knob system — KEEP-CUSTOM (already lib-backed; cleanup is internal)

**Scope:** `_shared/config/config.py` (1,048), `config_registry.py` (630), `config_yaml.py`
(2,137). **Reframe: this was never build-vs-buy** — `Settings(BaseSettings)` with
`env_prefix="YADGAR_"` + `YamlConfigSource(PydanticBaseSettingsSource)` already IS the full
pydantic-settings integration. The question was whether the 3,815 LOC on top can shrink toward it.

**LOC decomposition (the honest breakdown):** 2,096 LOC is pure schema DATA (`FIELD_META` 329
entries ≈1,766 LOC + `_REGISTRY` 277 entries) — documentation arrays for 323 knobs, not logic.
Actual behavior logic ≈1,719 LOC.

**(a) Coverage:** 18 test files, ~2,831 LOC, ratcheted: I25 three-way-sync invariant (every field in
yaml+registry or allowlisted), phantom-knob regression tests asserting no `os.getenv()` bypasses.

**(b) Bug history:** the phantom-knob class (v5.95, `a07bee6f` — config.yaml wrote values the code
never read; caused a daemon freeze via un-armed offload) was **plumbing** — and was fixed by the
custom `resolve_knob` + ratchet, which is exactly the discipline pydantic-settings alone doesn't
enforce. Stale `_yaml_layer` cache (v5.89), chmod-600 gap — plumbing, fixed. Remaining bug surface
is schema-completeness drift (domain).

**(c) Absorption estimate:** pydantic-settings 2.14.2 (healthy, py3.14 OK) could absorb **~50 LOC**
(built-in `YamlSettingsSource`) at the cost of ruamel comment preservation — which `set_config_value`
and `yadgar config init` (fully-commented operator YAML) require. Everything else — admin/Prometheus
source-attribution registry, FIELD_META docs, CLI (`init/list/get/set/edit`), `resolve_knob`
(needed for lru_cache-vs-monkeypatch test isolation) — has no library equivalent. dynaconf = second
config lib on top of pydantic; rejected without deep-dive.

**(f) VERDICT: KEEP-CUSTOM.** The 3,815 LOC is mostly essential: 323 knobs × (env, default, type,
desc, section, gauge, YAML, CLI) — no library bundles those dimensions. **Genuine accidental
complexity worth fixing (no lib involved):** `FIELD_META` + `_REGISTRY` duplicate near-identical key
sets → unify into a single declarative `KnobSpec` list (~200 LOC saved, kills the triple-edit
discipline); collapse the `_yaml_layer`/`get_settings` dual cache; drain the Tier-2 allowlist
backlog (already planned, `PLAN_V5_7_X_CONFIG_KNOB_BACKFILL.md`).

---

## 5. Observability — KEEP-CUSTOM (custom layer is a product OTel lacks)

**Scope:** `_shared/observability/`: observe.py (308), tracing.py (787), metrics.py (1,202),
log_config.py (1,002), exception_telemetry.py (52). **Reframe:** OTel api/sdk/instrumentation +
prometheus-client already deps — question is whether the layer on top is reinvention.

**(a) Coverage:** 25 test files, ~6,587 LOC (2:1), including a live-codebase coverage lint
(`test_check_observe_coverage.py`: 1,626 in-scope functions, 0 unclassified, hard-fail).

**(b) Bug history:** 4 of 5 mined bugs were DOMAIN and each *justifies* a custom piece: log-path
span flood (`2109365a`, crashed prod — auto-instrumentation would have made it WORSE; only the
allowlist glob enforces "the log path is the span sink, never span it"), OTel SDK blocking shutdown
→ SIGKILL exit-137 (`4da2b413` hard-timeout workaround), OTLP-collector-down retry flood
(`2785d9c6` → `_CircuitBreakerSpanExporter`; the SDK has no CB), 42k-span storm (P-SB span-budget —
**still open**). 1 was OTel-version churn (`OTEL_SDK_DISABLED` no-op semantics broke 36 tests).

**(c) What pure OTel does NOT give:** tier-based cardinality enforcement (boundary/stage/hot/exempt),
the I33 coverage ratchet (observability gaps = build failure — the load-bearing idea), allowlist
governance (167 exemptions with rationales + stale-entry detection), shared RED metric families
(vs ~19,500-series cardinality bomb), double-instrumentation guard over `@_tool` spans, I14
span→log bridging. LOC split: ~55–60% irreducibly domain, ~17% Prometheus metric declarations
(needed regardless), ~15–20% thin wrapper glue whose removal is blocked by the sentinel coupling
(`trace_span` sets the flag `@observe`'s double-span guard reads). Only genuine reinvention found:
`_parse_otlp_headers`, ~20 LOC.

**(f) VERDICT: KEEP-CUSTOM.** Also a warning the other direction: OTel is the dep whose version
churn already bit us (`_on_ending` vs `on_end` shim, `OTEL_SDK_DISABLED`) — MORE direct SDK
surface means MORE gate exposure, not less. Actionable follow-up: **implement P-SB** (span-budget
allowlist section; the 42k-span storm is a live bug), cost S.

---

## 6. Migrations runner — KEEP-CUSTOM (no lib exists)

`_shared/storage/migrations.py` (1,367 — but the *runner* is ~30 LOC: flock + `schema_version`
table + ordered list; the rest is 25 SurrealQL migration bodies). ~35 test fns; each migration gets
its own suite. Bug history: numbering collisions across concurrent branches (coordination, not
runner), migration-body design bugs — zero runner-level bugs (no double-apply, no lost state).
Alembic = SQLAlchemy-only; yoyo/Flyway = SQL dialects. **No SurrealQL migration framework exists in
Python**; writing an adapter costs more than the 30-LOC runner. N/A cost. Domain complexity lives
in the SurrealQL bodies where it belongs.

## 7. Backup/rotation — KEEP-CUSTOM

`core/backup/backup.py` (304) + `cleanup-backups.sh` (130). ~51 test fns. Bug history all
domain-operational: torn-segment backup of live lock-held surrealkv dir (3,622 memories lost
2026-06-16 → v5.69.0 stop-then-copy fix), `copytree copy2` mtime propagation making fresh snapshots
prune-eligible (v5.10.5 → `touch()` fix). restic/borg: external binaries (violate self-sufficiency +
image budget), and they wrap only the copy step — the surql `GET /export` mode and the quiesce
ordering (the parts that actually broke) stay custom regardless. N/A.

## 8. Scheduler/daemon loops + vacuum — KEEP-CUSTOM

Loops: 3 `while True + sleep` daemon threads (metrics 5s, reranker-idle, viz); consolidation
already externalized to a systemd timer (v5.7.0 deliberate removal of the in-process loop); vacuum
triggered by trigger-file + systemd path-watch. Vacuum orchestration (~1,700 LOC incl. tests'
subject) is stop/snapshot/side-build/verify/atomic-swap — pure domain. ~100 test fns
(test_vacuum.py alone: 2,235 LOC, 61 tests).

**The decisive check — was the 2026-06-30 daemon hang a scheduling bug?** No: sync git subprocesses
inline on the asyncio event loop (FastMCP runs sync tool bodies inline; time-bucketed lru_cache
expiry → synchronized miss wave → serial 2s git calls on the loop thread). Fixed via
`asyncio.to_thread` (v5.90.0) + healthcheck-kill. APScheduler operates on scheduled-task
invocation and never touches MCP dispatch — it prevents **zero** historical bugs here (same for
drainer-not-started = lifespan wiring, file-theft = async race, vacuum loss = quiesce ordering).
APScheduler health seals it: 3.10.4 stable is 2023/maintenance-only; 4.x is still **alpha**;
neither has py3.14 certification. Adding it converts 3 trivial loops for zero bug-class coverage.

---

## Suggested sequencing (IF approved — none of this builds without a go)

The audit's honest output is that the payoff ranking inverts the original hypothesis: the wins are
tests-and-hardening in our code, plus one small lib adoption. Cache was the user's named priority —
it lands as hardening, not a switch, because the persistence layer was already atomic and the only
shipped bug was un-buyable domain logic.

1. **Retry test backfill** (S, no dep, do first regardless): timing tests for drainer backoff
   windows (assert skip while `next_retry_at > now`) + CB state-transition tests (ml_client, OTLP).
   Closes the gap that hid the v4.3 storm; becomes the safety net for step 2.
2. **stamina for sites 3+4** (S, +1 dep — the only switch on the table): port-allocation +
   systemctl retry loops. −50 LOC, library-correct jitter/backoff, structured logs. Explicitly
   NOT the drainer or circuit breakers.
3. **Cache snapshot hardening** (S, no dep — the cache answer): CRC32 payload checksum on
   snapshot write/load + a power-fail/truncation fuzz test for the loader. Addresses the residual
   torn-snapshot anxiety with ~15 LOC instead of a dependency.
4. **P-SB span-budget** (S, no dep): the `_span_budget` allowlist section + `span=False` on the
   42k-storm hot-loop helpers — the one live observability bug.
5. **KnobSpec unification** (M, no dep, lowest urgency): collapse `FIELD_META` + `_REGISTRY` into
   one declarative structure (~200 LOC, kills triple-edit); optionally single config cache object.

Steps 1–4 are each independently shippable; 5 is a refactor train of its own.

## Non-goals (explicit)

- **No domain-layer replacement anywhere:** version-keys/epoch bus/DI (cache), DLQ taxonomy +
  wait_for_job contract (queue), knob registry/CLI (config), tier system/I33 allowlist
  (observability), SurrealQL bodies (migrations), quiesce orchestration (backup/vacuum) all stay.
- **No diskcache, no persist-queue/litequeue, no APScheduler, no dynaconf, no restic/borg** — each
  rejected above with reasons (hot-path inversion; contract breakage; staleness/alpha + zero
  bug-class coverage; second-config-lib; binary deps).
- **No broker-based task systems** (huey/dramatiq) — self-sufficiency.
- Nothing in this plan touches the deps-modernization train's scope (transformers/hub/hf-xet).

## Risks

- **Verdict-inversion risk (honesty check):** the KEEP verdicts lean on test suites (2:1–3.6:1
  ratios) as evidence of edge-case coverage. Test LOC is not proof of edge-case *selection* —
  the identified gaps (no snapshot power-fail test, zero retry-timing tests, no P-SB tests) are
  precisely where the ratios are misleading. Steps 1/3/4 exist to close that.
- **stamina under blanket-lock:** +1 dep = its releases join the upgrade treadmill and its
  deprecations join the zero-warning gate. Hynek projects track CPython aggressively (good for
  py3.14/3.15) but also deprecate briskly. If that trade reads as unfavorable, KEEP-CUSTOM for
  sites 3+4 is defensible — they are 25–30 LOC loops; the tests (step 1) matter more than the swap.
- **Future re-audit trigger:** if a NEW library-preventable bug class appears (e.g. a second
  file-theft-style race, or snapshot corruption despite atomic writes), the corresponding KEEP
  verdict should be re-opened — the verdicts are evidence-based on today's history, not identity.
