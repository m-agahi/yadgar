# Wiki/KB usefulness + signal-to-noise — discussion + decisions

Status: **DISCUSSION OPEN** (2026-06-15). Investigation + design needed before
implementation. Edit-primitives (v5.61) grinding now; this SNR work comes after,
discussed with the user. This doc records the discussion + decisions so far so the
conversation resumes from facts.

## Goal (user framing)

Make the memory/wiki system **truly useful**, not just populated. Usefulness =
the right knowledge surfaces at the right time with low noise — not metrics like
page-count or edge-count.

## Decisions so far

### D1 — DROP wiki↔memory linkage (2026-06-15)
`source_count=0` on ~all wiki pages is **not a defect** — it's an unused field.
Wiki retrieval is content-based (FTS + vector over page text); `source_memory_ids`
/ `wiki_refs` feeds **nothing** in the retrieval path. The v5.58 audit mislabeled
"disconnected corpora" as a problem; nothing consumes the connection.

- Auto-link + similarity-backfill would populate a provenance edge with **no
  reader** → effort for a metric, not usefulness. Backfill of ~2000 pages = real
  risk, zero functional gain.
- Linkage earns its keep ONLY if a **consumer is built first**:
  1. Recall augmentation — surface derived wiki when a memory is recalled.
  2. Provenance staleness — but `wiki_lint` already does hash-drift staleness.
  3. Viz memory_wiki edges — pure decoration.
- **Parked** unless/until we design a consumer. Do not wire the field before
  anything reads it.
- Root cause for the record: `_link_memories` (`yadgar/wiki.py:1090`) only fires
  when a caller passes `source_memory_ids` to `wiki_add`; ~no caller ever does.

### D2 — Edit-primitives (v5.61) FIRST
Can't clean/reclassify the corpus without edit/maintenance tools. `wiki_update`
allowlist is only `{content,tags,category,confidence}` — can't fix
`directory_context`/`branch`; preamble edits need a full 40k-char resend
(corruption-class risk). Build the tools, THEN cleanup is possible. See
`wiki-edit-primitives.md`.

## SNR — the real usefulness problem (OPEN, needs investigation + discussion)

Symptom observed 2026-06-15: `recall("wiki audit findings …")` returned mostly
**auto-generated AWS co-occurrence memories** ("X and Y are frequently modified
together", tags `derived`/`auto-generated`) — NOT the actual knowledge. Noise
crowds signal in retrieval.

### Hypotheses to investigate (not yet confirmed)
- **Auto-capture bloat.** The PostToolUse action-stream + co-occurrence derivation
  generate high-volume low-value `derived`/`auto-generated` memories that rank in
  recall. How many? What fraction of the corpus + of top-K recall results?
- **Heat doesn't separate signal from noise.** Derived co-fire memories get
  heat/access that floats them into results. Is heat the wrong ranking signal for
  usefulness?
- **Retrieval surfaces by similarity, not value.** No notion of "is this worth
  showing." Cross-encoder rerank exists — does it down-rank derived noise?
- **Corpus tiers.** AWS-inventory pages (~1547 flagged v5.58) may be a whole tier
  that should be archived/excluded from default recall.

### Investigation plan (to run, then DISCUSS before building)
1. **Measure SNR.** For a sample of real queries: what % of top-K recall is
   `derived`/`auto-generated`/co-occurrence vs human/agent-authored knowledge?
   Break down memory corpus by tag/store_type/provenance.
2. **Source of bloat.** Which pipeline produces the co-occurrence "frequently
   modified together" memories? Volume/day. Are they ever useful in recall?
3. **Ranking audit.** Do these noise memories win on heat, similarity, or rerank?
   Where in the pipeline do they float up?
4. **Options to weigh (discuss):** suppress derived memories from default recall;
   separate tier/store for auto-derived; stop generating low-value co-fire
   memories; rerank/penalize `derived`; archive AWS-inventory tier; a "usefulness"
   signal distinct from heat.

### Open questions for the user
- Is the goal better RECALL (less noise in results) or a smaller/cleaner CORPUS,
  or both?
- Are the auto-generated co-occurrence memories ever wanted? (If never surfaced
  usefully → stop generating them.)
- Acceptable to exclude whole tiers (AWS-inventory, derived) from default recall?

## Related
- `[[wiki-edit-primitives]]` — the enabling tooling (grinding v5.61)
- `[[db-audit-fix]]` — live-store integrity (separate; some overlap on derived-memory volume)
- v5.58 wiki audit (memory): 2128 pages, AWS-inventory bloat, source_count=0
