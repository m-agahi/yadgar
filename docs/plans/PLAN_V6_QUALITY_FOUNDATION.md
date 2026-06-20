# PLAN — v6 Quality Foundation ("brain, not query-spitter")

**Status:** DRAFT (2026-06-18). North star, not yet scheduled into releases.
**Thesis:** v6 is the LLM release. But an LLM on top of unmeasured, half-wired
retrieval just launders garbage faster. **Before the LLM part of v6, we make
quality measurable and bring the already-built Tier-2 "brain" superstructure to
life — proven, not believed.** Every step is gated by a measurement harness so we
keep what earns its keep and cut what doesn't.

The repeated failure mode this codebase has hit (enrichment silently off, dream
no-op, domain-consolidation never firing, surprise-gating computed-then-ignored,
null-embedding corruption) all share one root cause: **features shipped without a
dial that proves they work.** v6 fixes that root cause first.

---

## 0. Where we are — ML/brain inventory (verified 2026-06-18)

**HOT recall path (every query):** all-MiniLM-L6-v2 embeddings (384-d, *2021 — the
oldest piece*) → weighted-RRF fusion → GTE-reranker-modernbert (2024, strong) +
FlashRank fallback + multi-passage aggregation + graph/cofire priors (precomputed
nightly, O(1) additive boost).

**LIVE write-time:** engram competitive temporal-clustering (Josselyn-Frankland,
not Hopfield), successor-representation transition recording, index-time
enrichment paths (COMET / doc2query / ConceptNet — *enabled but models absent in
prod → return `[]`* until the model-bundled CI/runtime image ships).

**LIVE nightly:** CLS greedy clustering (sim≥0.7) + pattern promotion, PC causal
discovery, astrocyte domain consolidation (keyword/regex), graph/cofire prior
precompute, the sleep/dream cycle (proven in v5.72: co_occurrence links + dream
insights + reembed_stale + auto_narrate).

**Computed-but-DISABLED (the gold):** predictive-coding **surprise-gating**
(`WRITE_GATE_THRESHOLD=0.0` → stores everything despite computing novelty).

**Wired-but-off (don't chase without proof):** NLI rerank (deberta-v3, ~55s CPU,
"minimal gains"), query-time COMET expansion, Personalized PageRank (implemented,
never called in prod), cognitive-load (Cowan 4±1), Hopfield energy (config-only).

**Untested:** `sr_retrieve` (successor-representation associative retrieval).

---

## Phase 0 — MEASUREMENT (the keystone; nothing ships without it)

Goal: turn "I believe recall is good" into a number. This is the gate for every
later phase. **We are NOT starting from scratch** — see 0.0.

> **This is THE most important work of v6** (user, 2026-06-19). The eval harness is
> to *quality* what the `make e2e` gate is to *behavior*. The e2e gate already
> earned its keep this cycle — it caught the BC-D1 regression and the stale
> nightly tests that unit tests missed. The eval harness does the same for quality:
> it converts "recall is good / this feature helps / the brain works" from belief
> into an enforced number. Target end-state: a **quality gate** (recall@k must not
> regress vs baseline) that blocks merges, exactly like e2e blocks pre-push. Build
> non-gating first (report the delta), then graduate to gating once the golden set
> is trusted. Every other v6 phase is downstream of this dial existing.

### 0.0 What we already have (build on it, don't reinvent)
- **LongMemEval** (`benchmarks/run_longmemeval.py`) — a *published* memory benchmark
  (Wu et al., ICLR 2025, arXiv:2410.10813; **MIT-licensed**, 500 questions, 6 types).
  Already RUN at **v5.26.0**: **MRR 0.928 · Recall@10 0.906 · nDCG@10 0.863 · QA 69.4%**
  (vs Zep/mem0). Emits the exact IR metrics Phase 0 needs. Two phases: Phase-1
  retrieval-only (reader-independent) + Phase-2 QA (LLM answer + LLM judge).
- **Ablation harness** (`bench/v5.54-graph-prior-ablation`, `run_graph_prior_ablation.py`)
  — isolated-SurrealDB A/B with **paired-delta bootstrap CI + sign test**. Already
  proved `graph_prior` delta = 0.0 (vector+FTS already ranked correctly) — i.e. the
  measure-or-cut discipline is *already operating*. This is the reusable ablation pattern.
- **LoCoMo** (`run_locomo_jscore.py`) — memory-level labels, but **CC BY-NC**
  (non-commercial) + dataset gated + no yadgar baseline. Treat as a *template* only;
  do NOT depend on it for a shippable/commercial harness.
- **Reusable primitives**: `compute_recall()` / `compute_ndcg()` / MRR loop,
  `isolated_surreal()` scaffold, SHA-256 dataset pinning, reproducibility dict,
  `make_benchmark_settings()` override factory.

**So Phase 0 = adapt these, not greenfield.** Two tracks:
1. **Regression baseline (free, immediate):** run LongMemEval each release; alert on
   regression vs the published 0.928 MRR. We already have a number to defend.
2. **Per-component ablation (the build):** the existing harnesses score at *session*
   granularity over an *external* dataset; Phase 0 needs *memory-id* granularity over
   *yadgar's own* data. The one genuinely-missing piece is the yadgar-native golden
   set below (0.1) + a thin memory-id scoring adapter reusing the metric primitives.

### 0.1 Yadgar-native golden set + `make eval` adapter
- **Golden set** (the real missing piece): curated `(query, relevant_memory_ids[])`
  pairs across realistic scenarios (lookup, multi-hop, cross-domain, recency-biased,
  paraphrase). Start ~50–100, grow over time. Stored as a fixture; versioned.
  *Confirmed absent today* — no `relevant_memory_ids` set exists; this is Phase-0.1's
  deliverable.
- **Metrics**: `recall@k` (k=1,5,10,20), `MRR`, `nDCG@k`, plus **latency p50/p95**
  per query (so quality gains are always weighed against the 2s hook budget).
- **Output**: a committed baseline report + a `make eval` that prints the delta
  vs baseline. Wire into CI as a non-gating report first, gating later.
- **Ablation switches**: reuse the `bench/v5.54-graph-prior-ablation` pattern
  (isolated DB + paired-delta bootstrap CI + sign test) to run each component
  on/off (reranker, priors, enrichment, MMR, SR) → per-component contribution
  with confidence intervals. This is how every Tier-2 feature earns or loses its
  place — and it already caught `graph_prior` ≈ 0.

### 0.2 Data-quality metrics (dashboard + invariants)
- `%-memories-with-valid-embedding` (today's null-embedding bug would have lit
  this up) — promote to a hard invariant (a write that stores `embedding=None`
  is a defect, not a warning).
- `duplicate-rate` (near-dup density), `zombie/dead-rate` (orphaned/never-recalled
  derived memories), `domain-coverage` (% memories assigned a domain),
  `surprise-distribution` (histogram of the novelty score we already compute).
- Export as Prometheus metrics + a `yadgar stats` section.

**Exit criteria for Phase 0:** baseline recall@k/MRR/nDCG + data-quality numbers
committed; `make eval` runs locally and in CI; ablation harness works.

---

## Phase 1 — DATA QUALITY (garbage-in prevention)

"Best data quality" = stop storing junk + clean what's there. Measured against 0.2.

### 1.1 Surprise-gating ON — the headline brain lever
- Raise `WRITE_GATE_THRESHOLD` above 0 in **staged** steps (e.g. 0.1 → 0.2 → …).
- At each step measure: store-volume ↓, duplicate-rate ↓, **recall@k unchanged**
  (the safety constraint — we must not gate out memories we later need).
- Ship the threshold that maximizes quality without measurable recall loss.
- This is THE "brain not query-spitter" change: selective storage by salience.

### 1.2 Embedding-completeness guard
- Hard invariant + startup/repair pass: no memory persists without an embedding;
  a background sweep re-embeds any `embedding IS NULL` rows (generalizes the v5.66
  zombie purge + today's corruption fix).

### 1.3 Enrichment, measured (folds in v5.72.1 / #64)
- With models bundled, run the harness with enrichment on vs off. Keep COMET /
  doc2query / ConceptNet **only** where they raise recall@k. Decide ConceptNet's
  path (HTTP-API at index-time vs drop) on the numbers.

---

## Phase 2 — RETRIEVAL QUALITY (Tier-2, measured)

### 2.1 Unified-scoped-recall rebuild (#30) + consensus-as-signal
- Flat exhaustive candidate-gen (fast, complete) → **domain-aware MMR** re-rank
  (the consensus principle: reward distinct-domain coverage, penalize same-domain
  stacking) → existing graph/cofire priors + GTE reranker.
- Expose `consensus_retrieve` as an explicit **broad/landscape mode** (resolves
  BC-AC3a = expose, not retire). Not the per-prompt default.
- Gate on harness: diversity ↑ without completeness ↓.

### 2.2 Prove or cut SR retrieval
- `sr_retrieve` (successor-representation associative recall) vs flat baseline on
  the harness. It's genuinely brain-like (hippocampal "what's near what"); prove
  it adds recall on multi-hop queries or cut it from the hot path.

### 2.3 Embedding-model decision (measured, not assumed)
- Benchmark all-MiniLM-L6-v2 vs modern (bge-large / gte / e5) on the harness +
  latency. **Only swap if the embedding (not the reranker) is the bottleneck**
  and latency stays within budget. The strong GTE reranker may make a bigger
  embedding redundant — the numbers decide.

### 2.4 Reranker + fusion tuning
- Sweep candidate-count, fusion weights (WRRF graph/cofire), multi-passage params
  on the harness. Retire the wired-but-off losers (NLI, PPR, query-COMET) unless
  an ablation surprises us.

---

## Phase 3 — BRAIN DYNAMICS (consolidation/association efficacy)

The claim "it learns while it sleeps" must become a measurement.

### 3.1 Consolidation-efficacy test
- Measure recall@k on the golden set **before vs after** a nightly consolidation:
  does CLS promotion + dream linking + priors actually improve future retrieval?
  If yes, quantify; if not, the consolidation is cost without benefit — fix or cut.

### 3.2 Engram + SR transition value
- Do engram temporal clusters / SR transitions improve multi-hop recall? Ablate.

### 3.3 Causal-discovery precision
- Sample inferred causal edges (PC algorithm); measure precision (are they real
  cause→effect or spurious co-occurrence?). Gate the `detect_causality` phase on
  a minimum precision or scope it down.

---

## Phase 4 — THEN the LLM: generative consolidation (the v6 headline)

**The vision (user, 2026-06-19):** yadgar should *generate* new, better memories
from what it already holds — given A and B, synthesize a new memory / wiki / insight,
like a brain forming semantic knowledge from episodes. Not a query-spitter — a
system that *thinks while it consolidates*.

**Key realization: the architecture already has the slots — they're placeholders
waiting for an LLM.** Three generative points already fire nightly, today as
mechanical templates:
- **Dream insight** (`dream.py`) — given a co-activated pair, writes
  `"Dream connection: X relates to Y"` (template). LLM → infer + write the *real*
  implication of A+B. (Literally the "given A and B produce a new memory" dream,
  already shipped at v0 in v5.72.)
- **CLS promotion** (`cls_store`/`consolidation/cls.py`) — clusters episodics, picks
  a representative (mechanical). LLM → read the cluster, *write a new semantic
  generalization*.
- **auto_narrate** (`narrative.py`) — project story as a template. LLM → an actual
  narrative synthesis.

### The ML / LLM division of labor (efficient + correct)
- **ML / existing pipeline = the SELECTOR (cheap, runs on everything):** clustering
  (which memories belong together), graph + cofire (which are linked), surprise
  (what's novel), decay/heat (what matters). Already built — it decides *which A,B
  are worth combining*, nightly, over the whole store.
- **LLM = the SYNTHESIZER (expensive, runs only on selector-flagged clusters):**
  read the selected memories → write the new memory/wiki/insight. **Never LLM every
  memory — only the few high-value clusters the cheap pipeline surfaces.** Brain
  analog: sleep consolidates the *salient* patterns, not every input.

### Other LLM roles (same measured base)
- **LLM as surprise/salience judge** — Phase 1's write-gate graduates from an
  embedding-novelty score to an LLM "is this worth remembering?" call.
- **LLM-drafted wiki, human-approved** — LLM reads related memories → *drafts* a
  wiki page → human approves (draft→approve already exists). The SAFE entry point:
  human-in-loop kills hallucination risk for the curated layer. Start here.
- **Query understanding** — LLM rewrites/decomposes queries pre-retrieval.

### Non-negotiable guardrails (a memory engine that hallucinates is worse than useless)
1. **Provenance.** Every synthesized memory links to its sources ("derived from
   X+Y"). Build on existing derived-memory tracking (+ #28 zombie purge).
2. **No unbounded derived-of-derived.** Keep originals; cap derivation depth so
   errors don't compound (telephone game).
3. **Measure it.** A synthesized memory must *improve* recall/answers vs raw A+B on
   the Phase-0 harness, or it's just more text. The harness gates synthesis.
4. **Cost-bounded.** The ML selector keeps LLM calls to high-value clusters only.

### Why this is Phase 4, not Phase 1
Generative synthesis only works on **clean, measured retrieval** (good A,B selection)
with a **harness to prove the synthesis helps**. Let an LLM generate on unmeasured
junk → confident nonsense, fast. Phases 0–3 are the precondition. Detailed LLM scope
(models, prompts, cost budget) → its own plan; this doc ensures that plan lands on
something measurable + grounded.

---

## Cross-cutting discipline (non-negotiable)
1. **Measure-or-cut.** Every Tier-2 feature either improves a harness metric or
   gets cut/disabled. No feature survives on belief.
2. **The harness gates everything.** No quality change merges without a measured
   delta (recall@k and latency).
3. **Completeness is sacred.** No change may drop a previously-retrievable
   relevant memory (the explicit safety check on surprise-gating + recall rebuild).
4. **Contract-honest.** New behaviors get real SHALL-asserting e2e (the v5.72
   discipline), not toothless tests.

## Folds-in / supersedes
- #30 (unified-scoped-recall) → Phase 2.1
- #59 (heat-decay single-writer / intents) → enables Phase 1 gating cleanly
- #64 (enrichment models) → Phase 1.3
- #41 (dead-config/code) → informed by ablation (cut the proven-dead)
- BC-AC3a (consensus_retrieve) → Phase 2.1 (expose as mode)
- BC-D3 (clean-shutdown test) → Phase 0 harness adds the missing e2e

## Sequencing
Phase 0 first (keystone) → Phase 1 (data quality, headline = surprise-gating) →
Phase 2 (retrieval) → Phase 3 (dynamics) → Phase 4 (LLM). Phases 1–3 can
interleave per-feature, but each is gated by Phase 0's harness.
