# T4 — Ettin CE-model swap: train plan (AUDITED)

**Status:** AUDITED — decisions resolved, Car 0 in build. Opus + advisor adversarial audit
complete 2026-07-12 (working-tree draft; no code changed, no branch). Every
load-bearing claim re-verified against master (core 5.129.0 / backend 5.40.0) via
four parallel investigators; per-claim verification table embedded below. Line-number
and mechanism errors fixed in place. **One prod correctness bug uncovered** (`_ckpt`
does not track the reranker model — see NEW Car 0(d)) and **one scope expansion**
(backend-container self-sufficiency — now USER-CONFIRMED, see Car 4).

**USER DECISIONS — RESOLVED (2026-07-12, user-confirmed):**
1. **Eval-gate Q/type = 20/type.** All 6 types, 3 arms (GTE / Ettin-32M / Ettin-68M),
   fresh-process-per-arm (~4.6h floor per the cost table; budget above it). The
   determinism-check precondition stands.
2. **Car 4 backend self-sufficiency: USER-CONFIRMED** (no longer relay-pending) — bake
   ALL default-ON backend model weights into `Dockerfile.backend` + offline
   `--network none` smoke test. The §Self-sufficiency quarantine is lifted; scope is
   in the train.
3. **Bake target CONFIRMED: BACKEND image only** (`Dockerfile.backend`) — all 4
   default-ON runtime models, GTE CE kept one cycle for rollback; **yadgar-ci NOT
   baked** (relocation from the original CI-image framing confirmed).

**NOT a user decision — mandated correctness fix:** the `_ckpt` bug (Car 0(d)) is a
pre-existing prod bug that any CE swap would trip; it is a required Car 0 fix, not a choice.

**Date:** 2026-07-12. **Audited against:** master (core 5.129.0 / backend 5.40.0,
`5b9c8ca1`-descended; T3 recall-restructure train COMPLETE — Car 1 #184, Car 2, Car 3
shipped; Car 0 re-measure + CPU-scaling series recorded in
`docs/testing/recall-perf-checklist.md`).

**North-star research input:** `docs/plans/ce-rerank-alternatives-research-2026-07-04.md`
(CONCLUDED — Ettin winner; 3-train mapping). This file is the T4 *build spec* derived
from that research after a full re-audit of the current tree.

---

## BLUF — what T4 actually is

T4 swaps the cross-encoder reranker model from the incumbent
**`Alibaba-NLP/gte-reranker-modernbert-base` (150M)** to **`cross-encoder/ettin-reranker-32m-v1`
(32.8M, ModernBERT-lineage, Apache-2.0)**, gated on a LongMemEval memory-domain
recall@k parity-or-better A/B. Ettin-68M (`cross-encoder/ettin-reranker-68m-v1`,
MTEB +0.007 above GTE) is the safety fallback if 32M regresses. Expected CE speedup:
**6.3× per pass** for 32M, 2.1× for 68M (Ettin single-harness blog benchmark, general-domain
MTEB — the *real* quality risk is measured on yadgar's own LongMemEval, not MTEB).

**The swap itself is one line** — `GTE_RERANKER_MODEL` at
`yadgar/_shared/config/config.py:282` (env `YADGAR_GTE_RERANKER_MODEL`). The CE loader
(`ml_client.py:363`) reads it. **AUDIT CORRECTION:** the CE score cache does NOT auto-invalidate on
the swap — its `_ckpt` derives from the *embedding* model, not the reranker (`_get_ce_checkpoint_hash`,
`embed_service.py:202-210`), and the cache is disk-persistent → stale GTE scores survive the swap and
a restart. This is a pre-existing prod bug fixed in **Car 0(d)** (mandated, revert-safe). Aside from
that fix, no new export, no recall code-path change: Ettin loads as a drop-in
`sentence-transformers` `CrossEncoder`, torch path, 8K context matches GTE's 8192.

**The work around that one line is the whole plan:** an eval gate to authorise the
flip, a perf re-measure to book the win, an image/CI reconciliation (the "bake Ettin,
remove GTE" directive collides with the observed architecture — CE is baked *nowhere*
today; see Car 4 OPEN item), version discipline, and a rollback story that keeps the
GTE fallback reachable.

**Structure (binding, per user):** Car 0 ships as its OWN SEPARATE PR (Ettin-independent
fixes, so Ettin is revertible without redoing them). Cars 1–4 ship as ONE train PR,
one version (ADR-0088). No per-car PRs for the train body — that was the T3 anti-pattern
(T3's own exemplar says "per-car PRs — NOT one PR"; T4 inverts it per ADR-0088).

---

## Per-claim verification table (audit 2026-07-12)

Every load-bearing `file:line`/mechanism claim, re-verified against master. **WRONG-LINE**
= fact true but line ref stale (fixed in the body). **WRONG** = claim false. **VERIFIED** =
holds exactly.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `GTE_RERANKER_MODEL` at `config.py:282`, env `YADGAR_GTE_RERANKER_MODEL`, GTE default | **VERIFIED** | `config.py:282` exact; env_prefix `YADGAR_` at `config.py:951` |
| 2 | `GTE_RERANKER_BACKEND` `torch` at `config.py:285` | **VERIFIED** | exact |
| 3 | `GTE_RERANKER_MAX_LENGTH=512` at `config.py:283` | **VERIFIED** | exact |
| 4 | `CROSS_ENCODER_TOP_K=10` at `config.py:186` | **VERIFIED** | exact |
| 5 | CE instantiation `STCrossEncoder(settings.GTE_RERANKER_MODEL,…)` at `ml_client.py:363` (torch) / `:372` (onnx) | **VERIFIED** | `:363` loads `settings.GTE_RERANKER_MODEL` + `settings.GTE_RERANKER_MAX_LENGTH` — **the A/B lever genuinely loads the swapped model** |
| 6 | CE score-cache key `f"{query_sha}:{text_sha}:{ckpt}"` at `_reranking_cross_encoder.py:227`, `ckpt=getattr(cache,"_ckpt","")` at `:222` — "model swap auto-busts cache" | **PARTLY WRONG — see Car 0(d)** | Key format + getattr VERIFIED. **BUT `_ckpt` derives from `_get_ce_checkpoint_hash()` (`embed_service.py:202-210`) which hashes `YADGAR_CE_MODEL`→fallback EMBEDDING model, NOT the reranker (`YADGAR_CE_MODEL` VERIFIED unset in prod). Swapping `GTE_RERANKER_MODEL` leaves `_ckpt` UNCHANGED → cache does NOT auto-bust.** Prod correctness bug (cache is disk-persistent, survives restart). |
| 7 | Idle-unload daemon `_reranker_idle_loop()` `daemons.py:108-129`, interval 60 (`config.py:810`), evict 600 (`config.py:808`) — LIVE | **VERIFIED** | Thread started at `daemons.py:171` inside `init_engines()`. Live, not dead. `RERANKER_IDLE_UNLOAD_SEC` = `600.0` float. |
| 8 | Startup preload `_run_model_warmup()` gated on `MODEL_PRELOAD` | **WRONG-LINE** | Function `embed_service.py:324–374` (not `:352`); early-return `if not settings.MODEL_PRELOAD` at `:335`; task scheduled at `:732`. Gating logic correct. |
| 9 | `MODEL_IDLE_EVICTION_SECONDS=0` `config.py:71` separate subsystem from reranker idle | **VERIFIED** | Distinct: this is the `RemoteMLClient` eviction path, not `RERANKER_IDLE_UNLOAD_SEC`. |
| 10 | Lazy CE HF download "at `embed_service.py:559–570`" | **WRONG** | `:559–571` is the `_get_reranker()` double-checked-lock guard; the actual HF download is in `ml_client._load_gte_reranker()` (`ml_client.py:347–394`). Download site misattributed. |
| 11 | CI image tag == pyproject version; content change → new tag (dind cache miss BY DESIGN) | **WRONG (major)** | `Dockerfile.ci:15` LABEL version is **hardcoded `5.121.1`**; `ci-pr.yaml:44` references `yadgar-ci:5.121.1` hardcoded. Pyproject is `5.129.0` — **8-version drift, no auto-sync pipeline for yadgar-ci**. The "new tag automatically" premise is false. yadgar-ci is manually rebuilt/pushed. (Latent CI bug — noted, NOT this train's job. See Car 4.) |
| 12 | CE baked nowhere in `Dockerfile.ci` (only MiniLM `:74-77`, comet+doc2query `:86-92`) | **VERIFIED** | Zero CrossEncoder/gte/ettin/nli in the file. |
| 13 | CI tests: `HF_HUB_OFFLINE=1` + CE mocked | **VERIFIED (nuance)** | `HF_HUB_OFFLINE=1` in every test leg (`ci-pr.yaml:46-47,96-97,149-150,212-213`). CE mocked **per-test** (not a global conftest fixture) + `YADGAR_MODEL_PRELOAD=false` (`conftest.py:53`). No real CE ever loads in yadgar-ci. |
| 14 | `Dockerfile.backend` bakes zero model weights (runtime HF download) | **VERIFIED** | 37-line Dockerfile, no bake step. Prod gets models via host bind-mount `~/.cache/huggingface:/root/.cache/huggingface` (`flake.nix:278`, `docker-compose.yml:55`). No `HF_HOME`/`HF_HUB_OFFLINE` set for backend. |
| 15 | Backend runtime model set (default-ON) for `/embed`+`/rerank` | **VERIFIED** | embed `all-MiniLM-L6-v2`; CE primary `gte-reranker-modernbert-base` (`GTE_RERANKER_ENABLED=True`); flashrank fallback `ms-marco-MiniLM-L-12-v2` (`~/.cache/flashrank`, separate); `doc2query/msmarco-t5-small-v1` (enrichment ON). NLI `nli-deberta-v3-base`, multi-passage, comet all **default-OFF**. |
| 16 | `make longmemeval` target `Makefile:307–315` shape | **VERIFIED** | exact; `Q ?= 30`, `--unified --retrieval-only --variant s --stratify-per-type`. |
| 17 | `--settings-override` str-coercion `:338`; merged over defaults, overrides win | **VERIFIED** | coercion `:347-363`, `defaults.update(overrides)` `:411`. String HF-id passes as str. |
| 18 | NullMLClient fix (#184): `LocalMLClient(settings)` `:946` → `Retriever(…, ml_client=)` `:1050` | **VERIFIED (present on tree)** | injection at `:944-946`, passed at `:1050`. |
| 19 | `--unified` in-process, own surreal subprocess if `YADGAR_DB_URL` unset | **VERIFIED** | `:591-613` in-process recall; surreal spawn `:964-988`. |
| 20 | Harness supports `--types` + repeat | **PARTLY WRONG** | `--types` exists (`:1307-1311`, comma-sep, default `""`); `--max-questions` (`:1301`); `--stratify-per-type` (`:1331`). **No `--repeat` flag** — repeating = re-invoke binary. 6 recognised types (`QUESTION_TYPES` `:160-167`). `--stratify-per-type` WITHOUT `--types` collapses to single-session-user head-slice (confirms T3 collapse). |
| 21 | CE-miss validity gate (ADR-0098, Δmiss≥5/q via `:8001/metrics`) in harness | **WRONG — absent + path-incoherent** | No CE-miss assertion in the harness. Worse: the eval runs in-process (`LocalMLClient`), so **there is no backend `:8001` to scrape during the Car 1 A/B** — the `:8001` gate only applies to Car 3 perf. Car 1 needs an *in-process* CE-ran proof (see Eval gate). |
| 22 | Version sync sites (8) at core 5.129.0 / backend 5.40.0 | **VERIFIED** | all 8 sites confirmed; `check_versions.py` + `.pre-commit-config.yaml:33-44` always_run. |
| 23 | `check_backend_bump.py`: config.py under `_shared/` does NOT trip it; `embed_service_metrics.py` DOES | **VERIFIED** | `BACKEND_BUILD_DIRS=("backend",)` matches `backend` in path parts. `_shared/config/config.py` misses; `backend/embed_service/embed_service_metrics.py` (Car 0(a)) fires. Plan's bump-mandate is correct. |
| 24 | No colliding open version claims | **VERIFIED** | Only remote branch = docs-only `docs/cpu-scaling-series-2026-07-12`. 5.130.0 / 5.131.0 / backend 5.41.0 clear. |
| 25 | `_get_retriever` NOT dead (imported `core/server/__init__.py:84`, def `_shared/runtime/lifecycle.py:82`) | **VERIFIED** | repo-wide grep = 2 hits (def + import). Live. **Do NOT remove.** |
| 26 | `_fanout_recall` stale docstring at `recall_pipeline.py:40,198-199` | **WRONG-LINE** | `:40` + `:196-200` are *mentions*. Real stale docstring at **`recall_pipeline.py:459` (def) / ~471-474**: claims `UNIFIED_RECALL_ENABLED`-gated + "legacy body below" — but `embed_service.py:1332` calls it unconditionally, flag default-on since v5.80. Docstring-only, safe to fix. |
| 27 | "7 zero-importer shims" (#186) | **WRONG (stale count)** | Only **`_shared/enforcement.py` = 0 importers (dead)**. The other named shims are LIVE: config_sync(13), platform_paths(14), exception_telemetry(7), cognitive_map(5), tracing(4), config_registry(3), secrets(2), models(1), engram(1). Sweep narrows to 1 shim. |
| 28 | Perf baseline WARM CE-miss 10,955/7,916/6,807ms at checklist `~345-410` | **VERIFIED** | `recall-perf-checklist.md:356` exact. |
| 29 | `flake.nix:285` `--cpus 2`; no image-size CI gate | **VERIFIED (nuance)** | `:285` = `"--memory 4g --cpus 2 --stop-timeout 30"`. `DEFAULT_BACKEND_CAP_GB=2.0` EXISTS at `scripts/check_image_size.py:38` but is **not CI-wired** — plan's "no gate" holds with that nuance (baking models will exceed 2.0GB → if that script is ever wired, it must be raised). |
| 30 | ADR-0043/0078/0088/0097/0098 exist | **VERIFIED** | all in `yadgar-adr-log` wiki + referenced across docs. |

**Net:** claims fixed = line-refs on #6/#8/#10/#26, mechanism on #11 (CI tag), #20 (no repeat flag), #21 (gate absent + path-incoherent), #27 (shim count 7→1); one NEW prod bug (#6 → Car 0(d)); one scope expansion (backend self-sufficiency).

---

## User directives (verbatim intent — binding)

1. **Car 0 = SEPARATE PR (fixes only).** Rationale: the user wants Ettin revertible
   *without redoing the fixes*. Car 0 contains only Ettin-independent pre-fixes. The
   user will **MERGE Car 0 but NOT build/deploy it separately** — it rides into
   production with the train deploy. Reverting the train PR must leave Car 0's fixes
   intact.

2. **Rest of the train = ONE PR at the end** (ADR-0088: train = one PR, one version).
   No per-car PRs. This rule was violated once in T3; never again.

3. **CI image must include the Ettin model; REMOVE the old CE model from the image if
   nothing uses it post-swap.** Research how models get into CI (Dockerfile.ci /
   yadgar-ci image / HF cache bake — find the actual mechanism). **NEW TAG rule:** any
   `yadgar-ci` content change = new tag (dind caches by tag). *(See Car 4 — the observed
   architecture partly contradicts this directive; surfaced for audit, not silently
   resolved.)*

4. **Clean revert = TWO mechanisms, both wanted:** (a) Car 0 separation (fixes survive a
   train revert), AND (b) a config-key rollback (`GTE_RERANKER_MODEL` back to the GTE
   checkpoint). The plan must reconcile these against the CI-bake directive: if Ettin is
   baked and GTE removed from the image, a config-revert to GTE only works offline if GTE
   is still present. See Rollback plan.

---

## Ettin variant — IDENTIFIED (not unverified)

The model is chosen and sourced from primary research
(`ce-rerank-alternatives-research-2026-07-04.md`, Ettin blog single-harness benchmark
that includes the GTE incumbent at 14.7 pairs/s — apples-to-apples):

| Model | HF ID | Params | MTEB NDCG@10 | vs GTE | CPU pairs/s | vs GTE | Role |
|---|---|---|---|---|---|---|---|
| **Ettin-32M** | `cross-encoder/ettin-reranker-32m-v1` | 32.8M | 0.5779 | −0.006 | 92.5 | **6.3×** | **Primary swap target** |
| **Ettin-68M** | `cross-encoder/ettin-reranker-68m-v1` | 68.6M | 0.5915 | +0.007 | 31.2 | **2.1×** | **Safety fallback** (quality above GTE) |
| Ettin-17M | `cross-encoder/ettin-reranker-17m-v1` | 17.6M | 0.5576 | −0.027 | 267.4 | 18× | Aggressive-latency stretch (bench only) |
| Ettin-150M | `cross-encoder/ettin-reranker-150m-v1` | 150.9M | 0.5994 | +0.015 | 14.0 | ≈1× | Quality-ceiling control (bench only) |
| GTE-ModernBERT (incumbent) | `Alibaba-NLP/gte-reranker-modernbert-base` | 150M | 0.5843 | — | 14.7 | — | Current production |

**Must be verified in Car 1 (model-card / load check — do NOT invent):**
- Ettin loads as `CrossEncoder("cross-encoder/ettin-reranker-32m-v1")` on the current
  `sentence-transformers` pin, torch path, no ONNX export needed for the bench.
- Native ONNX exports exist (research claim) — confirm only if/when T5 does the ONNX
  fusion lever; NOT needed for T4.
- Ettin max seq length ≥ `GTE_RERANKER_MAX_LENGTH=512` (`config.py:283`); Ettin family
  advertises 8K context. Confirm the tokenizer/config on the card.
- The CPU pairs/s numbers above are **general-benchmark**; T4 books its own latency from
  the Car 3 re-measure under `--cpus 4` on the real backend, NOT from the blog table.

---

## Current CE wiring (verified against tree)

| Fact | file:line | Value |
|---|---|---|
| **CE model config field** | `yadgar/_shared/config/config.py:282` | `GTE_RERANKER_MODEL` (env `YADGAR_GTE_RERANKER_MODEL`), default `"Alibaba-NLP/gte-reranker-modernbert-base"` |
| CE backend switch (torch/onnx) | `config.py:285` | `GTE_RERANKER_BACKEND` default `"torch"` (`onnx-int8` dormant — NO-GO per ADR-0043, thread-thrash) |
| CE max length | `config.py:283` | `GTE_RERANKER_MAX_LENGTH=512` |
| CE cascade top-K | `config.py:186` | `CROSS_ENCODER_TOP_K=10` — CE sees only top-10 fusion candidates (cascade already taken v5.7.2) |
| CE model instantiation | `yadgar/backend/ml_client/ml_client.py:363` | `STCrossEncoder(settings.GTE_RERANKER_MODEL, …)` (torch) / `:372` (onnx branch) |
| **CE score cache key** | `yadgar/backend/retrieval/_reranking_cross_encoder.py:227` | `f"{query_sha}:{text_sha}:{ckpt}"`, `ckpt = getattr(cache, "_ckpt", "")` (`:222`). **AUDIT CORRECTION: does NOT auto-bust on a reranker swap.** `_ckpt` derives from `_get_ce_checkpoint_hash()` (`embed_service.py:202-210`) which hashes `YADGAR_CE_MODEL`→fallback EMBEDDING model — **the reranker model id feeds `_ckpt` NOWHERE**. Cache is disk-persistent (msgpack `.snap`, `cache.py:128/160/243`, snapshot interval 600s, checkpoint-mismatch-discards on load `cache.py:196`) → survives restart → swapping `GTE_RERANKER_MODEL` serves stale GTE scores as Ettin. **Prod correctness bug → Car 0(d) fix.** |
| Idle-unload daemon | `yadgar/core/daemon/daemons.py:108–129` | `_reranker_idle_loop()` runs every `RERANKER_IDLE_CHECK_INTERVAL_SEC=60` (`config.py:810`), evicts after `RERANKER_IDLE_UNLOAD_SEC=600` (`config.py:808`) |
| Startup preload | `yadgar/backend/embed_service/embed_service.py:324–374` | `_run_model_warmup()` async task (scheduled `:732`), early-returns unless `MODEL_PRELOAD=True` (`:335`); else lazy-load on first `/rerank`. HF download itself is in `ml_client._load_gte_reranker()` (`ml_client.py:347–394`), not in embed_service `:559-571` (that is the `_get_reranker()` lock guard). |
| Legacy idle knob (separate subsystem) | `config.py:71` | `MODEL_IDLE_EVICTION_SECONDS=0` (never) — NOT the reranker path; do not confuse with `RERANKER_IDLE_UNLOAD_SEC` |

**Keep-warm re-scope note (corrects the research doc's Lever 3 premise):** the research
doc and the T3 audit (C10) both said "idle-unload never fires, 7s = post-restart load."
That is now **stale** — `_reranker_idle_loop()` at `daemons.py:108–129` IS a live
background daemon that evicts the reranker after 600s idle. So idle-unload *can* fire in
steady state. This changes keep-warm's justification: it is no longer purely a
post-restart cost. **Audit action:** decide whether T4 touches keep-warm at all (raise
`RERANKER_IDLE_UNLOAD_SEC` / eager-preload via `MODEL_PRELOAD`) or defers it to T5. The
recommendation below (Car 3) is to leave keep-warm alone in T4 and book only the
model-swap win — but the re-scoped fact must be recorded either way.

---

## Eval gate — LongMemEval memory-domain recall@k parity (the authorising gate)

**This is the gate that authorises flipping `GTE_RERANKER_MODEL` default to Ettin.**
Reuses the exact T3 Car 1 A/B pattern (#184).

- **Harness:** `benchmarks/run_longmemeval.py` (1416 lines). Invoked via
  `make longmemeval Q=N` (`Makefile:307–315`), which runs
  `uv run --extra test --extra ml python benchmarks/run_longmemeval.py --unified
  --retrieval-only --variant s --stratify-per-type --max-questions $(Q)`.
- **A/B lever (confirmed):** `--settings-override GTE_RERANKER_MODEL=<hf-id>` (repeatable;
  parser coerces bool→int→float→**str** at `run_longmemeval.py:338`, so a string HF-id is
  accepted; merged over defaults into `Settings(...)` at `:367`, overrides win). One arm
  per run:
  - Arm A (baseline): `--settings-override GTE_RERANKER_MODEL=Alibaba-NLP/gte-reranker-modernbert-base`
  - Arm B (Ettin-32M): `--settings-override GTE_RERANKER_MODEL=cross-encoder/ettin-reranker-32m-v1`
  - Arm C (fallback, run in parallel): `…=cross-encoder/ettin-reranker-68m-v1`
- **NullMLClient fix state (#184):** PRESENT. `run_longmemeval.py:940–946` injects a real
  `LocalMLClient(settings)` and passes it to `Retriever(…, ml_client=ml_client)` (`:1050`).
  Without this the rerank chain is silently dead (every CE score `None`). Car 1 MUST
  re-confirm this holds on the current tree before trusting any A/B number (the T3 lesson:
  static-green is a false oracle; the A/B is only valid if CE actually ran — assert CE-miss
  Δ per the perf protocol).
- **`--unified` does NOT require a live backend.** It runs
  `yadgar.core.server.tools.recall.recall` in-process (`run_longmemeval.py:591–613`, own
  `surreal` subprocess if `YADGAR_DB_URL` unset). So the A/B is a standalone local run.
- **Reports:** default `benchmarks/results/…`; pass `--output benchmarks/reports/lme_t4_arm_{a,b,c}.json`
  explicitly (T3 Car 1 convention).
- **Gate criteria:** on the **memory domain** (the LongMemEval question types yadgar cares
  about), Arm B (Ettin-32M) recall@{5,10,50} + MRR + nDCG@10 **parity-or-better** vs Arm A
  (GTE). If 32M holds → ship 32M. If 32M regresses on any metric beyond noise → fall back
  to Arm C (Ettin-68M, which is MTEB-above-GTE and expected to hold). If neither holds →
  NO swap; T4 reduces to Car 0 only + a documented negative result.
### Statistical-power spec — PINNED by audit (was OPEN)

This gate *authorises a production model swap whose entire premise is a quality risk*
(MTEB −0.006 is general-domain). The T3 `--stratify-per-type Q=30` shape collapsed to
**all `single-session-user`, n=30, one run** — the thin-evidence trap. Audit pins the
gate as follows.

**Gate execution rule (mandatory — the eval-validity guarantee):** run **each arm as a
separate fresh `run_longmemeval` invocation**. This is what makes the gate valid despite the
`_ckpt` prod bug: the harness builds `Retriever(...)` at `run_longmemeval.py:1050` **omitting
`ce_cache`** → defaults `None` → `Reranker` uses a NullCache (`_reranking_cross_encoder.py:213`,
`core.py:47/60`) → **every CE score computed live per arm, no disk snapshot, no cross-arm bleed**.
(The disk-persistent `ce` cache is only wired by `compose.py:53` `get_ce_cache()`, which the
harness path does NOT use.) A shared-process run would still be safe here, but fresh-per-arm is
the belt-and-suspenders rule.

**In-process CE-ran proof (replaces the incoherent `:8001` scrape):** the eval is in-process, so
there is **no backend `:8001` to scrape** — the `Δmiss≥5/query` gate (ADR-0098) is a *Car 3 perf*
mechanism, not a Car 1 eval mechanism. Car 1 must instead prove CE actually ran **in-process**:
assert non-degenerate rerank behaviour (e.g. the retrieved ranking is not identical to the
pre-rerank fusion order across the sample, or log a per-query CE-invocation count from the
`LocalMLClient`). Without this, the A/B repeats the #184 false-oracle (CE silently dead → measures
fusion-only). **This is a Car 1 build requirement, not an existing harness feature (it is absent —
claim #21).**

**Pinned `--types` (operationalises "memory domain"):** all six recognised types
(`QUESTION_TYPES`, `run_longmemeval.py:160-167`), passed explicitly so the run does NOT collapse:
`--types single-session-user,single-session-assistant,single-session-preference,multi-session,temporal-reasoning,knowledge-update`.
Cap Q/type at the min available per type (the harness prints `type_counts` at `:328`; LongMemEval-s
type distribution is uneven — do not request more than a type holds).

**Q/type = the user cost-ceiling decision.** The run is **ingest-bound and CE-model-insensitive**:
ingest is per-question (`run_longmemeval.py:442`, inside the question loop — no reuse possible,
confirmed) at ~42s/q (~75% of wall time); retrieve is ~4-6s/q; the CE swap only touches the CE
sub-part of retrieve. So Ettin and GTE arms cost ~the same. From the measured T3 baseline (~46-48s/q):

| Q/type | total q (6 types × 3 arms A/B/C) | ~wall (ingest-bound) |
|---|---|---|
| 10 | 180 | **~2.3h** |
| 20 | 360 | ~4.6h |
| 30 | 540 | ~6.9h |

**These are FLOOR estimates.** The ~46-48s/q comes from T3's *collapsed single-session-user* run; a
real 6-type run includes multi-session / temporal-reasoning, whose haystacks likely ingest heavier
(ingest dominates wall time). Budget ABOVE the table, not at it — an underestimate burns the
box-time decision. (No re-measure needed; direction of error is up.)

Plus a one-time determinism check (~+48min, one arm run twice). **Recommendation: 10-20 q/type**
(2.3-4.6h); at n=10/type recall@k moves in 0.1 steps per type (coarse for small regressions), so
20/type buys finer resolution if the box time is available. **USER DECIDED: Q/type = 20**
(all 6 types, 3 arms, fresh-process-per-arm; ~4.6h floor — budget above it).

**Repeats — audit overrides the task's "≥2 repeats" default (audit trail):** the task cited "≥2
runs given documented variance," but that variance is *timing* (perf checklist wall-clock),
irrelevant to a **recall@k** gate. Question selection is a deterministic head-slice
(`run_longmemeval.py:881`, no RNG/seed/shuffle) and recall@k is deterministic given (store, query
set, model). So repeats add ~nothing to a quality gate → **spend the budget on higher Q instead**.
**Precondition:** run one arm twice, diff recall@k; if stable (expected), the "parity-or-better"
delta is clean; if it drifts, the gate delta must exceed the observed noise band. The determinism
result is a gate precondition, not optional.

- **Method rigor (ADR-0098):** fresh/distinct queries per the type set; quiesced box, one arm at a
  time. Caveat from T3 Car 1: the harness's `make_benchmark_settings` hardcodes some fields via
  `os.environ.update` (`run_longmemeval.py:414`) — the ONLY reliable A/B lever is
  `--settings-override GTE_RERANKER_MODEL=<id>`, not env vars. The lever genuinely loads the swapped
  model (`ml_client.py:363` reads `settings.GTE_RERANKER_MODEL` — verified). Log the effective
  setting in the report's `settings_overrides`.

---

## Perf protocol — re-measure under ADR-0097 / ADR-0098

**Baseline to A/B against (GTE, from the 2026-07-12 CPU-scaling series,
`recall-perf-checklist.md:345–410`):**

| Regime | 2-CPU | 3-CPU | 4-CPU |
|---|---|---|---|
| **WARM CE-miss** (6 distinct, valid) | 10,955ms | 7,916ms | **6,807ms** |
| HOT (exact-repeat, CE-cache hit) | 1,126ms | 875ms | 3,452ms (INVALID — cold graph) |
| restore() | 4,348ms | 4,264ms | 4,142ms (CPU-invariant, DB-IO bound) |

- **Restore `--cpus 4` (ADR-0097).** The backend is currently `--cpus 2` as a *deliberate
  temporary posture during T4 planning*. ADR-0097's owner verdict: **4 CPUs = sweet spot**
  (gather_budget=2 unlocks parallel CE, −28% at 2→3; torch intra-op=2 adds −14% at 3→4;
  5th CPU adds nothing — both knobs saturate at ≤4). Car 3 restores `flake.nix:285`
  `--cpus 2 → --cpus 4` (and `--memory` per ADR-0097 / MIGRATION_NOTES `--cpus 4 --memory 6g`)
  and re-runs the scaling curve on Ettin.
- **Protocol (ADR-0098):** fresh queries (topics absent from the reused baseline set — CE
  cache persists across daemon restarts, so a reused query set silently measures HOT), CE-miss
  validity gate ≥ ~5/query, histogram deltas on `yadgar_recall_duration_ms`, full Tempo trace
  by traceID for stage attribution. **Method rule (2026-07-12 correction block):** never
  compute `core = total − grep-of-logs`; read the `POST /recall` backend span duration
  directly (retrieval is fully sunk to backend, `_st._retriever is None` in core — ADR-0078;
  the 13.6s warm-common-case is ~99% backend-CE-bound, not core-bound).
- **HOT caveat (from RCA Anomaly 2, 2026-07-12).** Do NOT compare single-query HOT across
  runs. Recall has **no output cache (#88)** → a HOT repeat re-runs the full KNN+FTS+PPR+fusion
  compute; the true HOT floor is ~4.3s, compute-bound. The 3-CPU 875ms HOT was a
  graph-subgraph-residency outlier (a hook-recall pre-warmed that exact query's neighbourhood),
  NOT a per-CPU speedup — discard it as an artifact. HOT is only meaningful as a within-session,
  same-graph-state delta. (This caveat is also codified into the checklist by Car 0 — see below.)

**Expected T4 result:** Ettin-32M at 6.3×/pass should drop the CE-bound warm floor
substantially at `--cpus 4`. But CE is ~70–90% of pipeline cost and speedups do not multiply
cleanly (Amdahl + shared I/O); book the *measured* number, not the arithmetic product.

---

## Car breakdown

### Car 0 — Ettin-independent pre-fixes (SEPARATE PR)

**Ships as its own PR, merged but not deployed standalone; rides into prod with the train.**
Content is strictly Ettin-independent so a train revert never touches it.

**(a) Surface `EmbeddingEngine._query_cache` counters in the backend `CacheStatsCollector`
(~20 LOC).** RCA (`scratchpad/anomaly-rca-2026-07-12.md`, Anomaly 1): the recall query-embed
cache (`EmbeddingEngine._query_cache`, text-keyed OrderedDict) is functionally live but its
hit/miss counters are invisible at both scrape endpoints. Its counters
(`record_cache_miss("embedding")` + `yadgar_embedding_cache_misses_total`) land in the SHARED
`_registry` (`_shared/observability/metrics.py:47`) which the backend `/metrics` does NOT
serialize; the backend serializes an isolated `CollectorRegistry` (`embed_service_metrics.py:48`,
served at `:480`). Fix: register the retriever's engine `_query_cache` as a named `Cache` in
`yadgar.backend.cache._REGISTRY` (or add a scrape-time collector reading its len/hit/miss) so it
emits `{cache="query_embedding"}` on `:8001` alongside `ce`/`graph`. **Cosmetic, no behavior
change, no perf stake** (query-embed is ~10ms vs a 4–8s recall). Explains the recurring "embed
cache 0/0" open question in the checklist. *Ettin-independent — pure observability.*

**(b) `docs/testing/recall-perf-checklist.md` HOT caveat.** Add the RCA Anomaly-2 conclusion as
a standing caveat: HOT regime is unreliable cross-run (no recall output cache #88; hot floor ≈4.3s
@4cpu; the 3-CPU 875ms was a subgraph-residency outlier, not a per-CPU speedup). Prevents the next
measurer from repeating the cross-run HOT-comparison mistake. *Docs-only.*

**(c) Deferred dead-code sweep — CONSERVATIVE (judge each; include only clearly-safe).** The #186
RCA deferred a dead-code list. Car 0 rides to **production** via the train deploy, so the bar is
"genuinely safe," not "plausibly dead." Verdict table (audit re-confirms each before inclusion):

| Candidate | Source claim | Verdict for Car 0 (audit-resolved) | Evidence |
|---|---|---|---|
| `_get_retriever` (alleged zero-caller) | #186 "zero-caller" | **DEFER — NOT dead (confirmed)** | `core/server/__init__.py:84` imports it (def `_shared/runtime/lifecycle.py:82`); repo-wide grep = 2 hits only. Do not remove. |
| stale `_fanout_recall` docstring | #186 deferred | **INCLUDE — confirmed stale; line refs corrected** | Real target is `recall_pipeline.py:459` (def) / ~471-474, NOT `:40,198-199` (those are mentions). Docstring claims `UNIFIED_RECALL_ENABLED`-gated + "legacy body below" — but `embed_service.py:1332` calls it unconditionally, flag default-on since v5.80. Docstring-only, safe. |
| reranker-idle branch + 2 knobs | #186 deferred | **DEFER** — the idle daemon IS live (`daemons.py:108–129`, thread at `:171`); NOT dead. Leave for T5 keep-warm decision. |
| 7 zero-importer shims | #186 deferred | **NARROWS TO 1 — count was stale** | Only `yadgar/_shared/enforcement.py` = **0 importers (dead → INCLUDE)**. The rest are LIVE: config_sync(13), platform_paths(14), exception_telemetry(7), cognitive_map(5), tracing(4), config_registry(3), secrets(2), models(1), engram(1) — all DEFER. |

**Confirmed Car 0(c) sweep = exactly two items:** `_shared/enforcement.py` (dead shim) +
`_fanout_recall` docstring (`recall_pipeline.py:459/471-474`). Nothing else.

**Rule: any candidate whose dead-status cannot be confirmed from a repo-wide grep gets DEFERRED,
not included.** Car 0 must be boring and safe.

**(d) `_ckpt` CE-cache-key correctness fix (NEW — mandated, uncovered by audit; ~10 LOC + test).**
`_get_ce_checkpoint_hash()` (`embed_service.py:202-210`) hashes `YADGAR_CE_MODEL`→fallback the
EMBEDDING model. **`YADGAR_CE_MODEL` is VERIFIED UNSET in prod** (grep of flake.nix /
docker-compose.yml / entrypoints / workflows / scripts = zero; not a Settings field), so `_ckpt`
always resolves to the EMBEDDING model. The **reranker** model id (`GTE_RERANKER_MODEL`, what
`ml_client.py:363` actually loads) feeds `_ckpt` **nowhere**. The `ce` score cache is disk-persistent (msgpack `.snap`,
`cache.py:128/160`, checkpoint-mismatch-discards-on-load `cache.py:196`, single process-global
instance via `get_ce_cache()` `cache.py:314`). Consequence: swapping the reranker model does NOT
change `_ckpt` → the snapshot is NOT discarded on load → **stale GTE scores are served under the
Ettin config, and survive restart.** This is a **pre-existing bug that ANY CE swap ever would trip**
— strictly Ettin-independent, which is exactly why it belongs in Car 0 (revert-safe; the train can
revert without un-fixing it). **Must precede or accompany the swap.**
- **Fix:** make `_get_ce_checkpoint_hash()` hash the actual reranker model id
  (`settings.GTE_RERANKER_MODEL` / whatever `_load_gte_reranker` reads). Single fix site — one
  process-global `ce` instance, so no second path to patch (relay claim #4 resolved: one source).
- **Salt convention (coordinator-relayed scope addition, 2026-07-12):** the hash input is
  `f"{reranker_model_id}:{CE_SCORING_VERSION}"` where `CE_SCORING_VERSION` is a module-level
  constant next to `_get_ce_checkpoint_hash()`. Bump the salt whenever CE *scoring semantics*
  change (preprocessing, truncation, score transform) — salt bump → ckpt mismatch at snapshot
  load → whole old snapshot discarded via the existing discard-on-mismatch path. Model-id change
  alone already busts the cache, so the train (Car 1 Ettin swap) does NOT need to touch the salt.
- **TDD (hard rule):** failing-test-first — assert that changing `GTE_RERANKER_MODEL` changes
  `get_ce_cache()._ckpt` (and thus that a swap discards the old snapshot). Fails today; passes after.
- **Ordering:** because Car 0 merges FIRST and the eval (Car 1) runs against a Car-0-inclusive tree,
  the prod cache is correct before any prod swap. The eval itself is unaffected either way (NullCache
  path), so this fix does not gate the A/B — but it MUST land before the prod default flip (Car 2).

**Car 0 acceptance:**
- `{cache="query_embedding"}` appears on backend `:8001/metrics` and moves on a recall (hit or
  miss counter increments).
- Checklist HOT caveat present.
- Only grep-confirmed-dead items removed (`enforcement.py` shim + `_fanout_recall` docstring); all
  tests green; no signature changes (T3 false-oracle lesson: any signature change needs a real
  bounded test pass, not static-green).
- **(d) `_ckpt` fix:** the failing-test-first passes — changing `GTE_RERANKER_MODEL` changes
  `get_ce_cache()._ckpt` (CE snapshot discards on a reranker swap); backend test leg green.
- Version bump = its own patch (core 5.130.0; note Car 0 touches `backend/embed_service/
  embed_service_metrics.py` + `embed_service.py` → `check_backend_bump.py` FIRES → backend_version
  bumps too).

**Car 0 test plan:** unit test asserting the new `query_embedding` cache collector emits; **`_ckpt`
failing-test-first (reranker-swap busts CE cache);** run the backend test leg; re-run the
fast+shared+core legs to confirm no regression from the shim removal.

---

### Car 1 — Ettin model swap + LongMemEval gate (train PR)

**Scope:** flip `GTE_RERANKER_MODEL` default at `config.py:282` from GTE to
`cross-encoder/ettin-reranker-32m-v1` (or `-68m-v1` if the gate selects the fallback). One
config line. **The CE cache does NOT auto-bust on this swap (audit-confirmed) — Car 0(d) must have
landed first** so `_ckpt` tracks the reranker; the prod flip (Car 2) depends on that fix. The Car 1
A/B itself uses the harness NullCache path and is unaffected.

**Model-card / load verification (do first, before the A/B):** confirm Ettin-32M loads as
`CrossEncoder(...)` on the current pin, torch path, and reports a max seq length ≥
`GTE_RERANKER_MAX_LENGTH`. Record the actual params/context from the card. If the load fails on
the pin → that is the blocker to resolve before any bench.

**Gate:** the LongMemEval memory-domain recall@k A/B above. Arm A (GTE) vs Arm B (Ettin-32M) vs
Arm C (Ettin-68M). Ship the winner that holds parity-or-better; 32M preferred (6.3×), 68M fallback
(2.1×, quality-above-GTE). Paste the A/B result table into this plan (T3 Car 1 convention).

**Model label:** sonnet to build + run the A/B; **opus for the recall@k go/no-go decision** (a
quality-authorising judgment, not a mechanical pass/fail).

**Acceptance:**
- Ettin loads on the pin; **`_ckpt` fix (Car 0(d)) landed** so the prod CE cache tracks the reranker
  (the eval itself uses NullCache, so this is a prod-path precondition, not an A/B gate).
- A/B run with the **in-process CE-ran proof PASS** (ranking ≠ pre-rerank fusion order / per-query
  CE-invocation count > 0 from `LocalMLClient`) — proves CE actually ran; #184/T3 false-oracle guard.
  NOTE: the `:8001/metrics` Δmiss gate does NOT apply here (in-process, no backend) — see Eval gate.
- Chosen model holds memory-domain recall@{5,10,50} + MRR + nDCG@10 parity-or-better vs GTE, across
  all six pinned `--types`, at the user-chosen Q/type, determinism-check passed.
- Effective `GTE_RERANKER_MODEL` logged in each report's `settings_overrides`.

**Test plan:** the A/B itself is the gate. Plus a guard test that `ml_client.py:363` instantiates
from `settings.GTE_RERANKER_MODEL` (config↔loader drift guard). **32M/68M leak guard:** the test
must assert the loader reads *the config default dynamically*, NOT a literal `"32m"`/param-count
string — so a later 32M→68M fallback is a config-only edit with ZERO test change (audit confirmed no
code/test hardcodes the Ettin variant).

---

### Car 2 — config default + rollback wiring (train PR)

**Scope:** make the swap the *default* (not just an A/B override) once Car 1's gate passes, and
wire the clean config-key rollback. This is the "flip default ON after the gate" step, kept
distinct from Car 1's *authorisation* so the plan reads clearly (both are code in the same train
PR — logical sections, not separate PRs).

- Default `GTE_RERANKER_MODEL` = chosen Ettin id (the actual line edit).
- Document the rollback lever: `YADGAR_GTE_RERANKER_MODEL=Alibaba-NLP/gte-reranker-modernbert-base`
  (env override) or revert the config default. This works at runtime **iff GTE is still reachable**
  (HF Hub or baked) — see Car 4 + Rollback plan for the image tension.
- Any config-registry / config_yaml FIELD_META `desc` text that names the old model gets updated.

**Config strategy recommendation (justified):** flip the default to Ettin **only after** the
LongMemEval memory-domain gate passes (Car 1). Do NOT stage behind a separate feature flag — the
existing `GTE_RERANKER_MODEL` field IS the flag (any value swaps the model; env-overridable;
cache busts **only after Car 0(d)** — the `_ckpt` fix is a hard precondition for this default flip,
else stale GTE scores are served under the Ettin config). A second boolean flag would be redundant
surface. The user's two rollback
mechanisms are both satisfied: (1) Car 0 separation = fixes survive a train revert; (2)
`GTE_RERANKER_MODEL` config-key = single-line model rollback. Clean and minimal.

**Acceptance:** default is the chosen Ettin id across the config; env-override back to GTE
verified to reload the GTE model (integration check); rollback documented in MIGRATION_NOTES.

**Test plan:** integration test asserting an env-override of `GTE_RERANKER_MODEL` changes the
loaded checkpoint (both directions).

---

### Car 3 — perf re-measure + `--cpus 4` restore (train PR)

**Scope:** restore `flake.nix:285` `--cpus 2 → --cpus 4` (+ `--memory` per ADR-0097 /
MIGRATION_NOTES `--cpus 4 --memory 6g`), then re-run the ADR-0098 perf protocol on Ettin and
book the win.

- Run the WARM CE-miss regime (6 distinct fresh queries, Δmiss≥5/q validity gate via backend
  `:8001/metrics` — valid HERE because Car 3 hits the REAL backend, unlike the in-process Car 1 eval)
  on Ettin at `--cpus 4`, compare to the GTE 4-CPU baseline **6,807ms**.
- **Re-run the 2/3/4-CPU scaling curve on Ettin (NOT optional — rollback-safety):** the
  `--cpus 4` + torch intra-op tuning (ADR-0097) was derived on GTE. Confirm the knob attribution
  (gather_budget dominant, torch intra-op secondary) still holds on the smaller model. **Rollback
  note:** a model-only revert to GTE keeps GTE's original tuning (GTE was the tuning baseline), so
  the revert is safe — BUT if Car 3 *re-tunes* any knob for Ettin, record that reverting the model
  alone would leave GTE on the Ettin-tuned knobs. Recommendation: do NOT re-tune knobs for Ettin in
  T4 — keep the ADR-0097 GTE-derived `--cpus 4`/intra-op settings, just re-measure. That keeps
  model-revert clean (config-key only, tuning untouched).
- Record HOT with the #88 caveat (do not compare single-query HOT cross-run); record restore()
  (CPU-invariant, ~4.2s, should be unchanged by the CE swap).
- Append the Ettin perf table to `docs/testing/recall-perf-checklist.md` as a new run log.

**Keep-warm decision (audit):** recommend leaving `RERANKER_IDLE_UNLOAD_SEC`/`MODEL_PRELOAD`
untouched in T4 (the idle daemon is live but the swap already shrinks the cold-load cost — a 32M
model loads far faster than 150M GTE). Defer any keep-warm tuning to T5. Record the re-scoped fact
(idle-unload IS wired, corrects the research doc) regardless.

**Model label:** sonnet (scripted; the recall-perf-check pattern exists).

**Acceptance:** Ettin 4-CPU WARM CE-miss measured with validity gate PASS; delta vs GTE 6,807ms
booked; checklist updated; `flake.nix` `--cpus 4` committed.

**Test plan:** measurement only (no new tests); the perf protocol *is* the check. Verify byte-identity
of recall results is unaffected by the model swap on a smoke query (results differ in *ranking* by
model, which is expected and gated by Car 1 — not a correctness regression).

---

### Car 4 — image bake: Ettin into the BACKEND image (train PR) — **A/B analysis retained; Option A relayed-chosen; audit RELOCATES the target**

The user's task mandated an A-vs-B argument with the advisor taking the opposite side, recording
both positions + what the user must decide. That analysis is retained below **even though the
coordinator relayed a "chose Option A, argue execution only" instruction** — the user's own written
task governs, and the A/B is the audit trail for *why* A. The coordinator relay carries no user
authority; treat Option A as relayed-pending-confirmation.

**The directive (verbatim intent):** "CI image must include Ettin; REMOVE old CE from image; new
tag on content change (dind caches by tag)." **Plus (relayed 2026-07-12):** "the containers need to
be self sufficient" — bake ALL required weights in the BACKEND image.

**Observed architecture (verified — the directive's "CI image" framing rests on a misconception):**

| Fact | file:line | Implication |
|---|---|---|
| `yadgar-ci` tag | **HARDCODED `5.121.1`** at `Dockerfile.ci:15`; `ci-pr.yaml:44` refs `yadgar-ci:5.121.1` | **NOT pyproject-derived** (pyproject = 5.129.0, 8-version drift). No auto-sync pipeline for yadgar-ci. The "new tag automatically on content change" premise is **FALSE** — yadgar-ci is manually rebuilt/pushed. (Latent CI bug; recorded, not this train's job.) |
| Models in `Dockerfile.ci` | `:74–77` MiniLM, `:86–92` comet+doc2query | **CE baked nowhere** (not GTE, not anything). |
| CE at CI test time | `ci-pr.yaml` legs: `HF_HUB_OFFLINE=1` + CE mocked (per-test `MagicMock`) | CI tests **never load a real CE** → baking Ettin into `yadgar-ci` **helps NOTHING**. |
| Eval-gate CE | `run_longmemeval --unified` in-process, local | The eval is NOT `yadgar-ci` either — it's a local in-process run. |
| CE at prod runtime | `ml_client._load_gte_reranker` lazy HF download; `Dockerfile.backend` bakes zero weights; models via host bind-mount `~/.cache/huggingface` (`flake.nix:278`) | **Prod is where offline-safety actually matters.** |
| Old-model strings outside python config | grep: **zero** in Dockerfiles/workflows/Makefile/flake.nix/scripts | Nothing to edit outside `config.py:282` for the swap itself. |

**A-vs-B (retained per task; advisor argued the opposite side):**
- **Option A (bake) — argued FOR:** proactive bake removes HF-download flakiness at prod runtime;
  a fresh/offline container serves `/rerank` without network; matches "self-sufficient containers."
- **Option B (no bake) — advisor's opposite side:** the swap needs no image edit (config-only; CI
  mocks CE; prod downloads at runtime via the existing host cache mount, the GTE status quo). Baking
  adds image size + build time + a rebuild for every model change; the download path already works.
- **Audit verdict → Option A, but RELOCATED:** if offline-safety is the goal, baking into
  **`yadgar-ci` is the wrong target** (CI never loads a real CE). The bake belongs in
  **`Dockerfile.backend`** — which is version-synced (pyproject-driven, `check_backend_bump.py`
  fires), rides the normal release build, and is where prod actually loads models. **The entire
  yadgar-ci tag-discipline / dind-cache concern is MOOT for this train** — yadgar-ci content does
  not change. **Coordinator relays user chose Option A (pending direct confirmation).**

**Coordinator-claim contradiction (surfaced, not absorbed):** the relay said "backend images build
LOCALLY (amd64, workflow rule)." The tree says `ci-release.yaml:156` `runs-on: ubuntu-latest` +
Docker Build Cloud (`driver: cloud`, endpoint `openfantasy/builder`, `:179-182`), building
**both `linux/amd64,linux/arm64`** (`:241`) and pushing to DockerHub. So image size affects **Build
Cloud time + DockerHub push**, not "local build minutes." The build-minutes reasoning rests on a
false premise — flagged for the user, not silently adopted.

#### Self-sufficiency — USER-CONFIRMED (2026-07-12; quarantine lifted)

> Confirmed in the user's own words 2026-07-12: bake ALL default-ON backend model
> weights + offline smoke test. This sub-section is now in-scope for the train (Car 4).
> Still: do NOT interleave it into Cars 1-3.

**Goal:** a fresh `Dockerfile.backend` container serves `/embed` + `/rerank` with **no network and
no HF mount**. The host `~/.cache/huggingface` bind-mount stays as an override/cache layer, but is
no longer *required*.

**Models a fresh offline backend needs (default-ON only — audit-enumerated, claim #15):**

| Model | Purpose | Default | ~HF size | Notes |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` | `/embed` | ON | ~90 MB | loads on first `/embed` |
| chosen Ettin CE (`ettin-reranker-32m-v1` or `-68m-v1`) | `/rerank` primary | ON | ~65 MB (32M) | the swap target |
| `Alibaba-NLP/gte-reranker-modernbert-base` | rollback (one cycle) | — | ~570 MB | bake for offline config-revert safety; drop in T5 |
| `ms-marco-MiniLM-L-12-v2` (flashrank) | `/rerank` fallback | ON | ~40 MB | uses `~/.cache/flashrank` (separate dir, NOT HF cache) — bake mechanism differs |
| `doc2query/msmarco-t5-small-v1` | enrichment | ON | ~230 MB | already baked in `Dockerfile.ci`; add to backend |

Excluded (default-OFF, won't load in prod): NLI `nli-deberta-v3-base`, multi-passage (reuses CE),
comet (ADR-0004 dormant). Total added weight ≈ 1.0 GB with GTE-for-a-cycle, ≈ 0.45 GB without.

**Build mechanism:** mirror the existing `Dockerfile.ci` pattern (Python library calls, NOT
`huggingface-cli download`): `SentenceTransformer('all-MiniLM-L6-v2')`,
`CrossEncoder('<ettin-id>')`, `CrossEncoder('Alibaba-NLP/gte-reranker-modernbert-base')`,
`AutoModelForSeq2SeqLM.from_pretrained('doc2query/msmarco-t5-small-v1')` at a build stage (populates
`/root/.cache/huggingface/hub`). Flashrank's `ms-marco-MiniLM-L-12-v2` uses `~/.cache/flashrank` —
bake via a `Ranker(...)` call at build or accept it as the one still-networked fallback (document).
**Env:** do NOT hardcode `HF_HUB_OFFLINE=1` in the image (it must still work WITH a mount / online);
set it only in the offline smoke test. Keep the host mount as an override.

**Image-size / build note:** ≈ +0.45–1.0 GB. No CI size gate is wired (`DEFAULT_BACKEND_CAP_GB=2.0`
exists at `scripts/check_image_size.py:38` but is NOT enforced — if ever wired, the baked backend
will exceed 2.0 GB and the cap must be raised). Cost lands on Build Cloud time + DockerHub push (see
contradiction above), not local minutes.

**Self-sufficiency acceptance:** **offline-container smoke test** — start the backend image with
`--network none` and NO HF mount; assert `/embed` and `/rerank` both return valid results (no
network error, no HF download attempt). This is the concrete gate for the self-sufficiency scope.

**Car 4 acceptance (bake; self-sufficiency USER-CONFIRMED):**
- Ettin (+ GTE for one cycle) baked in `Dockerfile.backend`; offline smoke test passes.
- `backend_version` bumped (Dockerfile.backend staged → `check_backend_bump.py` fires).
- `yadgar-ci` UNTOUCHED (no tag/dind concern for this train).
- If self-sufficiency NOT confirmed: Car 4 = bake only the chosen Ettin (+ GTE one cycle) into
  `Dockerfile.backend` for the swap+rollback path; runtime download remains for the rest.

**Test plan:** offline `--network none` smoke test loads baked Ettin + embed + serves both endpoints;
`HF_HUB_OFFLINE=1` set only in the test, not the image.

---

## Version discipline

**Sync sites (verified — `scripts/check_versions.py` enforces; `.pre-commit-config.yaml`
`check-versions` always_run):**

| Field | Files | Current |
|---|---|---|
| Core version | `pyproject.toml:7`, `server.json:10` + `:16` (packages[]), `flake.nix:46`, `docker-compose.yml:76` (`CORE_VERSION` default), `uv.lock:2507` | 5.129.0 |
| Backend version | `server.json:11` (`backend_version`), `docker-compose.yml:39` (`BACKEND_VERSION` default) | 5.40.0 |

- `scripts/check_backend_bump.py` (VERIFIED) forces a `backend_version` bump when a path with
  `backend` in its parts (any depth, `tests` excluded), or `Dockerfile.backend` /
  `entrypoint-backend.sh`, is staged. **Confirmed hook gap:** `GTE_RERANKER_MODEL` lives in
  `yadgar/_shared/config/config.py` — NOT under `backend/` → the hook does **NOT** auto-force a
  backend bump for the config-only swap (Car 2). **Mandate the `backend_version` bump explicitly**
  for the train (the CE model is a backend behavior change). NOTE: **Car 0 and Car 4 DO trip the
  hook** — Car 0 touches `backend/embed_service/embed_service_metrics.py` + `embed_service.py`; Car 4
  touches `Dockerfile.backend`. Only Car 2's config edit needs the manual mandate.
- **Car 0 build-on-merge note (VERIFIED):** the `build-images` job fires when pyproject differs from
  the latest `v*` tag (`ci-release.yaml:51-76,153-155`); backend image builds+pushes when
  `backend/`-changed (`:112-116,235-246`). Merging Car 0 (version-bumped + backend-touching) triggers
  a core + backend DockerHub push *for Car 0's version*. Building ≠ deploying — "merged but not
  deployed, rides in with the train" holds — but expect the Car 0 image to be built at merge.
- **Version pre-claim (namespace VERIFIED clear):** only open remote branch is docs-only
  `docs/cpu-scaling-series-2026-07-12`; 5.130.0 / 5.131.0 / backend 5.41.0 are all unobstructed.
  Car 0 (separate PR, first) claims **core 5.130.0 + backend bump** (hook fires). Train (Cars 1–4,
  one PR, one version per ADR-0088) claims **core 5.131.0 + backend 5.41.0**. Pre-claim both tuples
  up front (parallel branches → avoid torn-manifest / tag collision, the T3 pattern). Rule: Car 0's
  version < train's; train bumps `backend_version` regardless of the hook.

---

## Rollback plan

**Train revert (Cars 1–4):** revert the single train PR. Because Car 0 is a SEPARATE PR merged
first, its fixes (query-cache metrics, HOT caveat, dead-code sweep, **`_ckpt` fix**) **survive the
train revert** — exactly the user's requirement. `GTE_RERANKER_MODEL` returns to the GTE default;
**the CE cache correctly busts back to GTE keys BECAUSE Car 0(d) landed** (pre-`_ckpt`-fix, a revert
would have served stale Ettin scores under GTE — another reason the fix belongs in Car 0, revert-safe).

**Config-key rollback (no revert):** set `YADGAR_GTE_RERANKER_MODEL=Alibaba-NLP/gte-reranker-modernbert-base`
(env) or edit the config default. Single line. Busts the cache correctly (post-Car-0(d)).

**Offline-reachability (resolved — Car 4 bakes into `Dockerfile.backend`):** a config-revert to GTE
loads GTE *iff GTE is reachable*. Car 4 bakes the chosen Ettin **and GTE for one release cycle**
into `Dockerfile.backend`, so an offline config-revert to GTE works from the image alone; the host
`~/.cache/huggingface` mount remains as an additional source. GTE drops from the backend image in T5
once Ettin is proven in prod. (If the self-sufficiency scope is NOT confirmed and Car 4 bakes
nothing, the revert relies on the runtime HF-download status quo — reachable whenever HF is up.)

**Fallback model path:** if Ettin-32M ships and later underperforms in prod, the config-key
rollback target is Ettin-68M (quality-above-GTE) or GTE — both selectable by the same
`GTE_RERANKER_MODEL` lever.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Model quality regression** (Ettin worse on conversational-memory recall than general MTEB suggests) | Medium — MTEB −0.006 is general-domain, not LongMemEval | The gate: no default flip until memory-domain recall@k parity-or-better. 68M fallback (MTEB +0.007) if 32M regresses. |
| **CPU-latency surprise** (Ettin not as fast as the blog table on the real backend) | Low-med — blog is single-harness, not yadgar's `--cpus 4` | Car 3 books the *measured* number; the gate authorises on quality, latency is upside not a gate. |
| **Image size** (backend bake, +0.45–1.0 GB) | Low | No backend size cap is CI-enforced today (`DEFAULT_BACKEND_CAP_GB=2.0` exists at `scripts/check_image_size.py:38` but is NOT wired). Cost lands on Build Cloud time + DockerHub push, not local minutes. If the cap is ever wired, raise it — the baked backend exceeds 2.0 GB. |
| **HF download flakiness at prod runtime** | Medium — status quo for GTE; the directive's motive | Car 4 bake into `Dockerfile.backend` + offline smoke test removes it (relayed-pending-confirm). |
| **`_ckpt` does NOT track the reranker → stale scores on swap** (MATERIALIZED — confirmed prod bug) | **High if unfixed** — `_get_ce_checkpoint_hash` keys off embedding model, cache is disk-persistent | **Car 0(d) MANDATED fix** (failing-test-first): hash the reranker id. Must land before the Car 2 prod flip. |
| **Backend-bump hook misses the config-only change** | Medium — `config.py` is under `_shared/`, not `backend/` (VERIFIED) | Mandate the explicit `backend_version` bump for Car 2 (Version discipline). Car 0/Car 4 trip the hook naturally. |
| **False-oracle A/B** (CE silently dead → A/B measures fusion-only) | Medium — this exact bug hit #184 | **In-process CE-ran proof** on every A/B arm (ranking ≠ fusion order / CE-invocation count — the `:8001` Δmiss gate does NOT apply to the in-process eval); re-confirm the `LocalMLClient` injection (`run_longmemeval.py:946`, VERIFIED present). |
| **Eval sampling collapses to one type** (T3 trap) | Medium — `--stratify-per-type` alone is a no-op | Pin explicit `--types` (all six); cap Q/type at min-available (`type_counts` `:328`). |

---

## Car structure summary

| Car | PR | Scope | Model label | Gate |
|---|---|---|---|---|
| **0** | **SEPARATE PR** (core 5.130.0) | query-cache metrics (~20 LOC) + HOT caveat doc + dead-code sweep (2 items: `enforcement.py` shim + `_fanout_recall` docstring) + **(d) `_ckpt` reranker-cache fix (mandated)** | sonnet | tests green; `_ckpt` failing-test passes; grep-confirmed-dead only |
| **1** | train PR | Ettin swap + LongMemEval typed A/B (all 6 types; 32M primary, 68M fallback) | sonnet build/run; **opus go/no-go** | recall@k parity-or-better across types; **in-process** CE-ran proof PASS |
| **2** | train PR | config default flip + rollback wiring (**needs Car 0(d) landed first**) | sonnet | env-override reloads either model + busts cache |
| **3** | train PR | perf re-measure + `--cpus 4` restore (ADR-0097/0098) | sonnet | Ettin 4-CPU WARM CE-miss booked vs 6,807ms GTE |
| **4** | train PR | **Option A (relayed): bake Ettin (+GTE 1 cycle) into `Dockerfile.backend`** — NOT yadgar-ci; + self-sufficiency scope (PENDING confirm) | sonnet | offline `--network none` smoke: `/embed`+`/rerank` serve |

---

## Audit resolution of the open questions

Each original open question, with the audit's disposition. **RESOLVED** = audit settled it;
**PENDING USER** = needs the user's own decision (see Status header).

1. **Car 4 target — RESOLVED (audit) + USER-CONFIRMED (relocation + self-sufficiency scope,
   2026-07-12).** CE is baked nowhere; yadgar-ci mocks CE + `HF_HUB_OFFLINE=1`, so baking into
   yadgar-ci helps nothing, and yadgar-ci's tag is hardcoded (no auto-sync). Bake target confirmed:
   `Dockerfile.backend` ONLY — version-synced, where prod loads models; the dind/tag concern is moot;
   yadgar-ci NOT baked. Self-sufficiency (bake ALL default-ON weights + offline smoke test)
   **USER-CONFIRMED**. Coordinator's "builds locally" claim is contradicted by
   the tree (Build Cloud, both arches) — surfaced.
2. **Config strategy — RESOLVED.** Flip `GTE_RERANKER_MODEL` default post-gate; no second feature
   flag (the field IS the flag). **Caveat added:** the cache only busts correctly after Car 0(d).
3. **Keep-warm — RESOLVED.** Idle-unload daemon IS live (`daemons.py:108-129`, thread `:171`).
   Leave keep-warm untouched in T4 (32M loads fast); defer tuning to T5. Re-scoped fact recorded.
4. **Dead-code sweep — RESOLVED.** `_get_retriever` NOT dead (2 grep hits). The "7 shims" was stale:
   **only `enforcement.py` is dead**; sweep = that shim + the `_fanout_recall` docstring (`:459/471-474`,
   not `:40/198-199`). Conservative grep-confirmed-only bar confirmed.
5. **68M vs 32M — RESOLVED (rule) / gate-dependent (outcome).** Prefer 32M (6.3×) if it holds
   memory-domain parity; else 68M (2.1×, MTEB +0.007). The swap is config-only either way — the Car 1
   test asserts the config default *dynamically* (no `"32m"` hardcode), so 32M→68M is a one-line edit
   with zero test change (audit confirmed no code/test hardcodes the variant).
6. **Backend-bump hook gap — RESOLVED (VERIFIED).** `config.py` under `_shared/` does NOT trip
   `check_backend_bump.py`; Car 2 needs the explicit `backend_version` bump. Car 0/Car 4 trip it.
7. **Version pre-claim — RESOLVED (VERIFIED).** Namespace clear. Car 0 = core 5.130.0 (+backend bump,
   hook fires); train = core 5.131.0 / backend 5.41.0. Pre-claim both on parallel branches.
8. **Eval-gate statistical power — RESOLVED (spec + user Q/type decision).** Pinned: all 6
   `--types` explicit; NullCache-per-arm validity guarantee; **in-process** CE-ran proof (the `:8001`
   Δmiss gate is Car-3-perf-only, incoherent for the in-process eval); repeats→higher-Q (recall@k
   deterministic, head-slice — task's "≥2 repeats" was timing-variance, overridden with audit trail);
   determinism-check precondition. **User picked Q/type = 20** (all 6 types, 3 arms,
   fresh-process-per-arm; ~4.6h floor, budget above it). See Eval gate.

### New findings the original plan did not surface (audit-added)
- **`_ckpt` prod correctness bug** (Car 0(d)): CE score cache keys off the embedding model, not the
  reranker → any CE swap serves stale scores; disk-persistent, survives restart. Pre-existing.
- **yadgar-ci tag hardcoded `5.121.1`** (8-version drift, no auto-sync): latent CI bug, recorded, not
  this train's job.
- **CE-ran gate path-incoherence:** the plan's `:8001` Δmiss gate cannot run against the in-process
  eval — Car 1 needs an in-process proof instead.
- **Coordinator "builds locally amd64" contradicted** by tree (ubuntu-latest + Build Cloud, both arches).

---

## Sources

- Research north-star: `docs/plans/ce-rerank-alternatives-research-2026-07-04.md` (Ettin winner,
  3-train mapping, dead-ends).
- Style/structure exemplar: `docs/plans/archive/t3-recall-restructure-2026-07-11.md` (T3 — note:
  its "per-car PRs" rule is the ANTI-pattern T4 inverts per ADR-0088).
- RCA (Car 0 content): `scratchpad/anomaly-rca-2026-07-12.md` (query-cache visibility gap; HOT #88
  caveat; #186 warm-attribution correction).
- Perf baselines + protocol: `docs/testing/recall-perf-checklist.md` (CPU-scaling series
  10,955/7,916/6,807ms; 2026-07-12 correction block; ADR-0098 method rule).
- ADRs (wiki `yadgar-adr-log`, sole source past ADR-0094): ADR-0043 (onnx-int8 REJECTED), ADR-0044
  (recall→backend), ADR-0078 (retrieval sunk to backend), ADR-0088 (train=one PR/version), ADR-0097
  (4-CPU sweet spot), ADR-0098 (perf protocol: fresh queries + CE-miss gate — NOTE: the Δmiss/`:8001`
  gate is a *backend perf* mechanism; it is NOT in the LongMemEval harness and does NOT apply to the
  in-process Car 1 eval). The CE-swap ADR is uncodified — T4 should write it (include the `_ckpt`
  fix). **A new CE-swap-caused-stale-cache ADR is warranted for Car 0(d).**
- Config/wiring/CI file:line facts: **re-verified 2026-07-12 by four parallel investigators** (see
  the Per-claim verification table). Notable corrections: yadgar-ci tag is hardcoded not
  pyproject-derived; `_ckpt` keys off the embedding model; CE-miss gate absent from the harness;
  "7 shims" was 1; several line numbers stale (fixed in body).
