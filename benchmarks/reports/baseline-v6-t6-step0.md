# Baseline Report — v6 T6 Step 0 (Unified Scoped Recall)

Generated: 2026-06-20 (v6-T6 Step 0 harness)

## Golden Set

- **Source:** `benchmarks/golden/golden_set.jsonl`
- **Schema version:** v6-T6 (adds `relevant_wiki_slugs[]` + `type` annotation)
- **Status:** BOOTSTRAP — auto-drafted, REQUIRES HUMAN CURATION
- **Total pairs:** 42 (40 original bootstrap + 2 new mixed-type cases mx-0001, mx-0002)
- **Curated pairs:** 0 (all bootstrap until human curation)

### Mixed-type cases added (Step 0)

| query_id | type | derivation | note |
|----------|------|------------|------|
| mx-0001  | wiki | wiki_primary | Wiki-primary: `unified-scoped-recall` slug is the better answer; high-heat memory about RRF is plausible noise |
| mx-0002  | memory | paraphrase | Memory-primary: `WRITE_GATE_THRESHOLD` config; a matching wiki page might also score but memory is canonical |

## Retrieval Metrics (flag-False / legacy path)

> These numbers reflect the **current legacy recall path** (no unified recall).
> `UNIFIED_RECALL_ENABLED` defaults to False; flag-True path not yet wired.
> Baseline locked here for regression gating when flag-True ships in Steps 3–5.

```
recall@1  = 0.0595    nDCG@1  = 0.0714
recall@5  = 0.0595    nDCG@5  = 0.0622
recall@10 = 0.0595    nDCG@10 = 0.0622
recall@20 = 0.0595    nDCG@20 = 0.0622
MRR       = 0.0714
```

**Note:** Low MRR on bootstrap set is expected — the golden set was machine-generated
and uses sequential ID assignment that doesn't align with retrieval ranking in the
isolated eval DB. Human curation will fix this.

## Latency

```
p50  = 348.6 ms
p95  = 720.0 ms
mean = 354.6 ms
```

## By Derivation Type

```
paraphrase          MRR=0.0357  R@10=0.0357
tag_lookup          MRR=0.0000  R@10=0.0000
recency_anchor      MRR=0.1667  R@10=0.1667
wiki_primary        MRR=1.0000  R@10=0.5000  ← wiki cases scoring via legacy wiki-blend
```

## By Query Type (v6-T6 unified recall segmentation)

```
type=memory         MRR=0.0488  R@10=0.0488
type=wiki           MRR=1.0000  R@10=0.5000
```

The `type=wiki` case scores well via legacy wiki-blend in recall. After Step 2
(fan-out orchestrator behind flag-True), the same query should score >= this
baseline when unified recall is enabled.

## Machine-Readable Report

See `benchmarks/reports/baseline-v6-t6-step0.json` for full per-query data.

## What Changed from baseline-v5.74.md

1. Golden set schema extended with `relevant_wiki_slugs[]` + `type` fields
2. Two mixed-type cases added (mx-0001, mx-0002) — bootstrap, flagged needs_curation
3. `load_golden_set` now keeps pairs with `relevant_wiki_slugs` (not just `relevant_memory_ids`)
4. `evaluate_pair` uses unified namespace keys (`mem:<id>`, `wiki:<slug>`) — same primitives
5. Aggregation + summary table report by derivation type and query type
6. `wiki_primary` derivation bucket added to segmentation

## Pending (Steps 3–5)

- Add `type=all` cases (a query answerable by both memory AND wiki)
- Seed real wiki pages in isolated eval DB for wiki-slug scoring
- Gate the harness on `UNIFIED_RECALL_ENABLED=True` path once Step 2 ships
- Human curation of golden_set.jsonl before using as regression gate
