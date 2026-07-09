# PLAN — CE Lever 2: `CROSS_ENCODER_TOP_K` trim (improvement-train task #28)

> **DEFERRED PERMANENTLY (2026-07-06) — superseded by Ettin (#32). ARCHIVED.**
> Swapping the CE model to Ettin (~8× faster) removes CE as the bottleneck, so
> trimming `CROSS_ENCODER_TOP_K` becomes marginal — and the trim is quality-risky
> (fewer candidates reranked). Never implemented. Revisit ONLY if Ettin stalls and
> CE is still the binding constraint. The plan below is retained for reference.

**Status:** DEFERRED PERMANENTLY (was DRAFT) — plan only, never implemented. Audited 2026-07-03.
**Scope:** ONE config-default change + sweep-results doc. Config-only, no backend image bump.
**Go/no-go lean:** CONDITIONAL — viable ONLY if the quality gate measures **recall@5 / MRR@5** (not @10). Predicted knee **K≈6–7**. See §6 audit — a recall@10 gate makes this lever a mechanical no-op.

---

## CORRECTION (2026-07-04): CE runs THREE passes, not one

**Source:** real cold recall trace 242cc546 (`docs/diagrams/archive/2026-07-04/recall-cold-trace-2026-07-04.svg`, task #41).

The plan above was written assuming CE is a single span ("87% of recall"). That is **incomplete**. The trace reveals **three separate serial CE passes** totalling ~20s = 77% of the 26.1s cold recall:

| Pass | Span name | Timestamp in trace | Duration | Arm |
|---|---|---|---|---|
| 1 | `cross_encoder` | @3146ms | **~11.3s** | memory arm |
| 2 | `multi_passage` | @14398ms | **~2.4s** | memory arm (2nd CE pass) |
| 3 | `crossfuse.score_candidates_ce` | @19505ms | **~6.4s** | WIKI arm |

### What this means for the plan

**1. `CROSS_ENCODER_TOP_K` almost certainly gates ONLY pass 1 (~11.3s).**
Passes 2 (`multi_passage`) and 3 (`crossfuse.score_candidates_ce`) likely have distinct knobs or none at all. The K-sweep's latency win is **bounded to pass 1**, not "~40% of all CE." The original framing ("CE span = one span = 87%") must be re-read as: pass 1 alone = ~43% of 26.1s cold recall, not 87%. Actual savings from a K 10→6 trim: ~40% of 11.3s ≈ **4.5s**, not ~40% of 20s.

**2. The K-sweep protocol (§3/§5) must measure ALL THREE CE spans.**
Measuring only `retrieval.rerank.cross_encoder` (pass 1) misses passes 2 and 3. The sweep must also capture `multi_passage` and `crossfuse.score_candidates_ce` spans to understand total CE budget motion and identify what (if any) knobs gate passes 2 and 3.

**3. Open questions now blocking the K-sweep (add to §3 pre-requisites):**
- What gates pass 2 (`multi_passage`)? Its own top-k? Implicit from pass 1 output size? Fixed pool?
- What feeds pass 3 (`crossfuse.score_candidates_ce`)? Is it wiki-arm-only? Does it share any config with pass 1?
- Are passes 2 and 3 parallelisable, or do they have data dependencies on each other / on pass 1?

**4. Bigger lever is task #41 (parallelism), not K-trim.**
The three passes are serial. Parallelizing them could cut **~9s independent of any K-trim** (passes 2+3 concurrently with or after pass 1, if data dependencies allow). Task #85 (backend move) is the natural place to batch/parallelize CE passes.

**5. Status of shipped work (PR #161, v5.107).**
The DECOUPLE + configurable knobs (`CROSS_ENCODER_TOP_K` env var, `RERANKER_TOP_K` knob, `CE_TAIL_FILL_ENABLED`, `max_results`) are **already shipped** in PR #161. Only the K-sweep (choosing the default-K knee) remains — and it is now **gated on understanding the 3-pass structure** before results can be correctly interpreted against the right budget.

### Corrected expected win

| Scope | Original claim | Corrected |
|---|---|---|
| CE total budget | ~20s (3 passes) | ~20s — unchanged |
| Pass 1 budget (`CROSS_ENCODER_TOP_K` gates) | assumed all CE | ~11.3s |
| K 10→6 trim saves | ~40% of all CE ≈ 8s | ~40% of 11.3s ≈ **4.5s** |
| Passes 2+3 savings from K-trim | assumed included | **unknown / likely 0** |
| Parallelism opportunity (task #41) | not identified | **~9s** (passes 2+3 serial today) |

---

## 1. Context

- CE is the wall. Real trace: `retrieval.rerank.cross_encoder` span = 8.8s = **87% of recall** latency.
- Backend pinned `--cpus 2`. **STAYS.** This lever tunes for that constraint; it does not relax it.
- CE cost scales with the number of passages reranked. That count is gated by `CROSS_ENCODER_TOP_K` (config.py:177, currently **10**).
- CE model on current master is **GTE-reranker-ModernBERT** (`Alibaba-NLP/gte-reranker-modernbert-base`), primary; FlashRank ONNX + ST-MiniLM are fallbacks (`ml_client.py`). The `~400ms/pair` figure in `_reranking_cross_encoder.py:117` is a **stale MiniLM** comment — do not treat as GTE ground truth (see §5).
- This is a candidate-count lever, distinct from the CE-inference-cost menu in `docs/plans/ce-perf-options.md` (options A–G: quantization, GPU, skip-on-confidence, etc.). Those cut cost-per-pair; this cuts pair-count.

---

## 2. Hypothesis — CE latency linear in K

**Claim:** CE latency ≈ linear in K → K 10→6 ≈ 40% CE-span cut.

**Confirmed from code — CE cost IS ∝ pairs (∝ K):**

| Fact | Evidence |
|---|---|
| top_k slices candidates **before** scoring | `_reranking_cross_encoder.py:119` `memories_to_score = memories[:top_k]` |
| One batched forward pass, not per-passage loop | `_reranking_cross_encoder.py:126` `all_scores = self._ml.score_cross_encoder(query, expanded_texts)` → `ml_client.py:428` GTE `predict(pairs)` (one batch) |
| No GPU fixed-batch flattening | CPU forward pass, `--cpus 2`; each pair ≈ one transformer forward → cost grows with pair count, not a flat 32-slot batch |

**Refinement — cost is ∝ PAIRS, and pairs = K × variant-factor, not K:**
- `_reranking_cross_encoder.py:121` `_build_expanded_pairs(memories_to_score, open_domain_mode)`. In `open_domain_mode` each memory expands to **2 variants** (base + query-variant); otherwise 1.
- So pairs = **2K** (open-domain) or **K** (non-open-domain). The 8.8s÷~440ms/pair ≈ 20 pairs = 2×10 lines up with an **open-domain** query at K=10.
- **Consequence:** the linear-in-K story holds, but the multiplier depends on query-type mix. A K 10→6 trim cuts pairs 20→12 (open-domain) — a **40% pair cut → ~40% CE-span cut**, consistent with the hypothesis. State the 2× variant factor explicitly in the results doc so the extrapolation is not misread.

**Verdict: hypothesis CONFIRMED at the mechanism level.** CE cost is genuinely ∝ K. Whether the trim is *worth it* hinges entirely on §4/§6 (quality gate + the dual-knob trap), not on the latency mechanism.

---

## 3. Design — sweep to find the knee

> **See CORRECTION above.** Before running the sweep, identify what gates passes 2 (`multi_passage`) and 3 (`crossfuse.score_candidates_ce`). `CROSS_ENCODER_TOP_K` gates only pass 1 (~11.3s). The sweep results must report ALL THREE CE spans (see §5).

Sweep K, measure BOTH axes at each point, find the **knee** = minimum K with no quality regression.

**Sweep set:** K ∈ **{10, 8, 7, 6, 5}**.
- **K=4 DROPPED.** See §4/§6: K<5 mechanically under-fills recall@5, corrupting even the valid gate. Measuring K=4 produces a counting artifact, not a quality signal.
- Denser near the expected knee (8,7,6) than the tails.

**Per-K measurement (both axes, same run):**
1. Set `CROSS_ENCODER_TOP_K=K` (config.yaml, no restart-of-backend needed — see §7).
2. Quality: `make longmemeval Q=<N>` → recall@5, MRR@5 (see §4 for N and metric choice).
3. Latency: MCP-tool `recall` wall-time + Tempo `retrieval.rerank.cross_encoder` span p50/p95, `--cpus 2`, quiet host (see §5).

**Knee rule:** the knee = the **smallest K** whose recall@5 and MRR@5 are within the no-regression band of the K=10 baseline (§4). That K = max latency saving at flat quality. If every K<10 regresses → knee is 10 → **no free lunch, do not ship** (report that plainly).

---

## 4. QUALITY GATE — the crux (dual-knob trap)

**This is the make-or-break constraint the task asked to find. `CROSS_ENCODER_TOP_K` is a DUAL knob: it is BOTH the CE input window AND a cap on the returned count.**

Evidence:
- `cross_encoder_rerank` returns `memories_to_score[:top_k]` — **outputs at most K items** (`_reranking_cross_encoder.py:147`, sliced input at :119).
- The pipeline **reassigns** `result_memories` to that ≤K return (`reranking.py:141`).
- Downstream stages only reorder/filter (nli :162, mmr :206) — they do **not** re-append the dropped fusion tail. `profile_belief_merge` (:193) adds profile/belief rows from a *different* source, not the discarded fusion candidates.
- Final trim `result_memories[: ctx.max_results]` (`reranking.py:299`) operates on that ≤K set.
- Fusion built a pool of `max(max_results, RERANKER_TOP_K=50, CROSS_ENCODER_TOP_K)` (`fusion.py:294`) = **50** candidates; CE discards `50−K` of them with **no backfill** before the trim.

**Mechanical consequence:** if `CROSS_ENCODER_TOP_K < max_results`, recall returns **fewer than max_results** items — regardless of CE quality. Positions K+1..max_results are simply empty.

**Impact on the gate:** LongMemEval computes recall@5 AND recall@10 (requests max_results=10).

| Gate metric | Valid down to | Below that |
|---|---|---|
| recall@10 | K=10 only | K<10 under-fills positions 7–10 → recall@10 drops for a **counting** reason, not CE quality. **Knee analysis corrupted.** |
| recall@5 | K=5 | K<5 under-fills positions 5–? → corrupts even @5. |

**Therefore the quality gate for THIS lever MUST be `recall@5` + `MRR@5`, NOT recall@10.** Any @10 comparison across K is invalid unless a code change decouples the window from the return count (out of this config-only PR's scope — see §6 follow-up).

**Gate protocol:**
- **Q size:** `make longmemeval Q=500` (full, stratified) for the go/no-go run. `Q=50` for a quick smoke while iterating. Prior full-run baseline (v5.26): recall@5 0.87, MRR 0.93 — regenerate the baseline at **current** master + K=10 (model is now GTE, not the v5.26 stack).
- **Baseline:** K=10 recall@5 + MRR@5 on current master. Every K compared to THIS, not the historical number.
- **No-regression band:** recall@5 within **−0.01 absolute** (≈ −1 point) of K=10 baseline AND MRR@5 within −0.01. Rationale: matches the <2% offline-A/B gate used for CE-perf option B; recall@5 near 0.87 means −0.01 ≈ 1.1% relative. Per-query variance on Q=500 stratified is low enough that a >0.01 drop is signal, not noise. **Tighten to −0.005 if the K=10 vs K=8 delta is within noise** (i.e. don't accept a knee whose "flat" is just measurement jitter).
- **Report both axes per K** in the results doc: recall@5, MRR@5, CE-span p50, recall wall-time p50. The knee is visible as the last K before recall@5 breaks the band.

---

## 5. LATENCY measurement

> **See CORRECTION above.** Cold recall trace 242cc546 identified THREE serial CE spans. Measure ALL of them per K, not just pass 1.

- **Harness:** MCP-tool `recall` (routes through prod unified recall — the same path LongMemEval `--unified` uses). Not the legacy `retriever.recall()` (recurring benchmark-harness bug — must call `init_engines()` first per the LongMemEval memory).
- **Spans to capture (all three):**
  - Pass 1: `retrieval.rerank.cross_encoder` → `yadgar_recall_stage_ms{stage="cross_encoder"}`. p50 + p95.
  - Pass 2: `multi_passage` span (memory arm, 2nd CE pass). p50 + p95.
  - Pass 3: `crossfuse.score_candidates_ce` (wiki arm). p50 + p95.
  - **Total CE budget** = pass 1 + pass 2 + pass 3. Report this alongside the per-pass split.
- **Conditions:** backend `--cpus 2` (unchanged), quiet host (no concurrent recalls, no consolidation cycle running — pause the nightly cycle). Warm the model first (discard the cold-load first call).
- **Query-type control:** run a fixed query set spanning open-domain (2 variants → 2K pairs) and non-open-domain (K pairs). Report CE-span per query-type bucket — a K-trim helps the open-domain bucket ~2× more in absolute ms. Do NOT average across a shifting query mix.
- **N per K:** ≥30 recalls per K per bucket for a stable p50/p95.

---

## 6. AUDIT (brutal)

**Does trimming actually help, or is this a no-op / quality-loss?**

> **See CORRECTION above.** The "~40% CE cut" claim applies only to pass 1 (~11.3s of ~20s total CE). Passes 2+3 (~8.8s combined) are currently unaffected by `CROSS_ENCODER_TOP_K`. The corrected K 10→6 savings estimate is **~4.5s** (not ~8s).

- **Latency mechanism is sound — for pass 1.** CE cost is genuinely ∝ pairs ∝ K (§2, batched single forward pass, CPU, no fixed-batch flattening). A K 10→6 trim really does cut ~40% of pass 1 CE compute. This part is not a mirage. But it's ~4.5s, not 8s — passes 2+3 are not gated by this knob.
- **BUT the win is gated by the dual-knob trap (§4), and the gate-metric choice decides everything:**
  - **If the gate is recall@5, no backfill (confirmed):** slack plausibly exists down to K≈5–6, because fusion's top-6 usually contains the gold passage for @5 purposes. Predicted knee **K≈6–7**, ~30–40% CE-span cut. **Lever viable.**
  - **If the gate is recall@10, no backfill (confirmed):** the knee is **already at 10** — any trim under-fills positions 7–10 → mechanical quality loss. **Lever is a no-op** for a @10 gate without a code change out of this PR's scope. This is the brutal-honesty branch: if the team cares about recall@10, do not ship a config trim; ship the decouple (below) instead.

**Risks:**
- **Gold-passage drop below the CE window (real).** Candidates feeding CE are **fusion(WRRF)-ordered** (`fusion.py:192–248, 310–319`); CE reranks only `memories[:top_k]` of that order (`_reranking_cross_encoder.py:119`). A gold passage ranked #8 by fusion survives at K=10, dropped at K=6. Silent quality loss on hard queries where fusion mis-ranks the gold low. The recall@5 gate catches this **only if** those queries are in the Q=500 stratified set — hard/adversarial queries are exactly where fusion under-ranks gold, so weight the stratification toward multi-session / reasoning queries.
- **MMR interaction.** MMR runs AFTER CE (`reranking.py:296` after :292) on `top_k=max_results`. Trimming K shrinks MMR's input pool → less diversity headroom. Minor at K≥5 (MMR pool = max_results anyway), but note it.
- **Knee may already be at 10 (no free lunch).** Fully possible K=10 is not over-provisioned for @5 either — if every K<10 regresses recall@5, this lever has **zero slack** and must NOT ship. The sweep is designed to detect exactly this; a null result is a valid, reportable outcome (do not force a trim).
- **Per-query variance.** Small-Q runs (Q=20/50) will show noise > 0.01. Only the Q=500 run gates go/no-go.
- **Design smell (flag, don't absorb).** `CROSS_ENCODER_TOP_K` conflating "how many to CE-score" with "how many to return" is the root problem. The clean lever = **decouple**: CE-score the top_k, then return the CE-reranked top_k **followed by the fusion-ordered tail** up to max_results (so the return count is preserved while the CE window narrows). That makes a trim safe for BOTH @5 and @10. **But that is CODE, not config** — out of this config-only PR. Log it as a follow-up (`ce-topk-decouple`), do not silently expand scope.

**Rollback:** revert the one config default. `CROSS_ENCODER_TOP_K` is YAML-driven (`config_yaml.py` FIELD_META, section `reranking`) and the slice is **core-side** (`_reranking_cross_encoder.py:119`), before any RPC — so the backend never sees the value. Cheap, instant, no image rebuild. (Note: it is NOT in `config_registry.py` — no `YADGAR_CROSS_ENCODER_TOP_K` env var; change it via config.yaml / the Settings default. See §7.)

**Go/no-go:**
- **Note:** Decouple + env-var knobs already shipped (PR #161, v5.107). Only the K-sweep (choosing the actual knee) remains.
- **GO** if gate = recall@5 and the Q=500 sweep shows a K<10 within the −0.01 band → ship that K. Expected max savings now ~4.5s from pass 1; task #41 (serial→parallel passes) is the path to recovering the remaining ~9s.
- **NO-GO** if gate = recall@10 (config trim is mechanically lossy → ship the decouple follow-up instead), OR if every K<10 regresses recall@5 (no slack).
- **BLOCKED** pending: understanding what gates passes 2 (`multi_passage`) + 3 (`crossfuse.score_candidates_ce`) before the sweep is run — otherwise savings figures cannot be correctly attributed.

---

## 7. PR scope (1 PR)

**Contents:**
1. Change the default `CROSS_ENCODER_TOP_K` in `config.py:177` (and the matching `config_yaml.py` FIELD_META default, section `reranking`, if it carries its own default) from `10` → the chosen knee K.
2. Commit the sweep-results doc (recall@5 / MRR@5 / CE-span p50 per K, the baseline, the chosen knee, the query-type latency split).

**Why config-only = cheap, no backend bump:**
- The `top_k` slice is **core-side** (`_reranking_cross_encoder.py:119`), before the RPC to the backend. The backend `/rerank` (`ml_client.py`) never receives TOP_K — it scores whatever pairs core sends. So the core config change is sufficient; **no `yadgar-backend` image rebuild/bump.** Cite line 119 as the reason in the PR.

**Explicitly NOT in this PR:**
- The window/return-count **decouple** (code change, follow-up `ce-topk-decouple`).
- Adding a `config_registry.py` row / `YADGAR_*` env var for TOP_K (not needed; changing the default is enough — but if the team wants runtime env override, that's a separate I25 three-way change).
- Any CE-inference-cost work (quantization etc. — that's `ce-perf-options.md`).

**Tests / contracts:**
- No BEHAVIOR_CONTRACT row changes (rerank is a scoring component; recall BCs are outcome-level — run the recall e2e suite to confirm still green).
- If touching config_yaml FIELD_META default, `test_config_three_way_sync` must stay green.

---

## Appendix — evidence index (file:line)

| Claim | Location |
|---|---|
| `CROSS_ENCODER_TOP_K: int = 10` | `config.py:177` |
| CE model = GTE-reranker-ModernBERT primary | `ml_client.py` `_try_st_cross_encoder` / GTE path ~:428 |
| top_k slices input BEFORE scoring | `_reranking_cross_encoder.py:119` |
| top_k defaults from settings | `_reranking_cross_encoder.py:103–104` |
| one batched CE call (not per-passage) | `_reranking_cross_encoder.py:126` → `ml_client.py:428/514` |
| 2-variant expansion (open-domain) | `_reranking_cross_encoder.py:121` `_build_expanded_pairs(..., open_domain_mode)` |
| CE returns ≤ top_k | `_reranking_cross_encoder.py:147` |
| pipeline reassigns to CE return (no backfill) | `reranking.py:141` |
| fusion pool = max(max_results, 50, K) | `fusion.py:294` |
| no fusion-tail re-append before final trim | `reranking.py:292→299` (only reorder/profile-belief between) |
| final trim to max_results | `reranking.py:299` |
| MMR after CE | `reranking.py:296` after `:292` |
| CE span instrumented | `reranking.py:135,142` → `yadgar_recall_stage_ms{stage="cross_encoder"}` |
| TOP_K in YAML FIELD_META, NOT in config_registry | `config_yaml.py` FIELD_META (section `reranking`); absent from `config_registry.py` |
| slice is core-side (no backend bump) | `_reranking_cross_encoder.py:119` (pre-RPC) |
| LongMemEval gate harness | `make longmemeval Q=N`, unified path, `benchmarks/run_longmemeval.py` |
