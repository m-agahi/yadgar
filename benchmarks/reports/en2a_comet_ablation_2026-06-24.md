# en2a COMET-FPA ablation — VERDICT: RETIRE COMET

Date: 2026-06-24. Benchmark: LongMemEval-s (cleaned), **Q=120, stratified 20×6 types**, unified MCP recall path.
- **Arm A** (baseline): `COMET_ENRICHMENT_ENABLED=False` → `en2a_armA_v2.json`
- **Arm B** (treatment): `COMET_ENRICHMENT_ENABLED=True, COMET_FPA_EXEMPT=True` (COMET on, cosine FPA gate bypassed) → `en2a_armB_v2.json`

## Result — COMET is net-negative on recall, prohibitively expensive

| Type (n=20) | A R@5 | B R@5 | Δ R@5 | A R@10 | B R@10 | Δ R@10 | A MRR | B MRR | Δ MRR |
|---|---|---|---|---|---|---|---|---|---|
| knowledge-update | 0.975 | 0.975 | 0 | 0.975 | 0.975 | 0 | 1.000 | 1.000 | 0 |
| **multi-session** | 0.894 | **0.853** | **−0.042** | 0.942 | **0.917** | **−0.025** | 0.888 | **0.827** | **−0.061** |
| single-session-assistant | 1.000 | 1.000 | 0 | 1.000 | 1.000 | 0 | 1.000 | 1.000 | 0 |
| single-session-preference | 1.000 | 1.000 | 0 | 1.000 | 1.000 | 0 | 0.908 | 0.942 | +0.033 |
| single-session-user | 0.950 | 0.950 | 0 | 0.950 | 0.950 | 0 | 0.925 | 0.925 | 0 |
| temporal-reasoning | 0.946 | 0.958 | +0.013 | 0.971 | 0.983 | +0.013 | 0.925 | 0.950 | +0.025 |
| **overall** | **0.961** | **0.956** | **−0.005** | **0.973** | **0.971** | **−0.002** | 0.941 | 0.941 | 0 |

(R@50: overall A 0.975 → B 0.984, +0.009 — marginal deep-k gain that does not reach the k=5/10 context window.)

## Interpretation
- **No recall benefit.** Overall R@5 and R@10 are slightly WORSE with COMET. MRR flat.
- **Hurts the type it was meant to help.** multi-session — the hardest type, the motivation for commonsense enrichment — drops R@5 −4.2pts and MRR −6.1pts with COMET on. COMET's inferred facts are retrieval NOISE that displaces the true evidence in the top-k.
- **FPA gate was not the bottleneck.** Running FPA-exempt (no cosine filter) made multi-session worse, not better — so the inferences themselves don't help; loosening the gate just admits more noise.
- **Only marginal upside:** temporal-reasoning +1.3pts R@5 / +2.5pts MRR. Not enough to offset the multi-session regression.
- **Cost is prohibitive.** Arm B took ~17h wall-clock (~8 min/question COMET-BART beam search), pinned ~10 cores the whole time, and starved the live yadgar daemon (the v5.81 health/OTLP incident traced back to this load). Arm A (no COMET) is fast.

## VERDICT: RETIRE COMET enrichment
COMET delivers no net recall gain (slightly negative), regresses the key hard type, and costs ~17h/10-cores per ingest pass. The cost/benefit is decisively negative for a single-host personal deployment.

**Actions** (retire-to-DORMANT — keep the code, per user directive; plan: docs/plans/comet-retire-dormant.md):
1. Flip `COMET_ENRICHMENT_ENABLED` default `True`→`False` (config.py:221 — it was incorrectly True in prod, not False; I25 three-way sync). This is the core change.
2. KEEP the COMET code + tests DORMANT — do NOT delete. `transformers`/`torch` are SHARED deps (embeddings, doc2query), so dormant code is cost-free, and the model is lazy-loaded (never fetched while disabled). Dormant-banner the code + mark docs DORMANT.
3. The benchmark flags (`--comet`, `--comet-fpa-exempt`, `--enrich`) + the `COMET_FPA_EXEMPT` knob stay as reusable eval infra / dormant sub-knob — merge `feat/en2a-comet-fpa-exempt` rather than cherry-pick-and-drop.
4. ConceptNet / Doc2Query / Logic enrichers are SEPARATE and not evaluated here — this verdict is COMET-specific.

Supersedes the RETIRE-lean in PD-50 / ADR-0004 (now CLOSED → rejected).
