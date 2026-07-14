> ARCHIVED 2026-07-14 — SHIPPED as Car 4 of `feat/stophook-tasklist-train` (v5.138.0,
> merge `40020c0d`). Commit 1 (the I33 v2 lint machinery) already shipped earlier in
> obs-quickwins #195 / v5.133.0; this car delivered the residual sweep (`_extract_id` +
> `_row_to_dict` → `@observe(span=False)`, both added to `_span_budget`). Deploy-verification
> (per-op recall span count tens-of-thousands → tens in Tempo) is the one open follow-up —
> needs a live deploy, cannot run in a worktree.

# P-SB — I33 v2 span-budget refinement + hot-loop sweep

**Status:** ARCHIVED — SHIPPED (Car 4, v5.138.0) (Car 4 of `feat/stophook-tasklist-train`) — 2026-07-14. RECONCILIATION: Commit 1 (the entire I33 v2 lint — `_span_budget` section, `scan_file_span_v2`, ADR-0041 logging-handler hard rule, advisory loop-heuristic, docstring widening, and its full test file) plus the FIRST sweep offender (`server_helpers:_cosine_similarity` flipped to `@observe(span=False)` + listed) already SHIPPED in the prior obs-quickwins train (#195 / e30eeba8 / core 5.133.0), which is an ancestor of this car's base. Observed-state-wins: this car does NOT redo that work. This car delivers the RESIDUAL sweep — the two remaining recall per-row storm offenders `_ClientMixin._extract_id` + `_ClientMixin._row_to_dict` (`client.py`) flipped to `span=False` and added to `_span_budget`, a method-key-form unit test, version 5.138.0 + CHANGELOG. The broad 120-hit advisory loop-heuristic set is intentionally NOT blanket-flipped (each needs individual A/B judgement; the plan names only these offenders). Extracted from `full-observability-standard-2026-07-03.md` §5b (ADR-0074 ACCEPTED 2026-07-09); the tri-signal STANDARD is COMPLETE (that plan archived 2026-07-14).
**Date:** 2026-07-14. **Depends:** recall T3 (Ettin swap) — SHIPPED (core 5.132.0) → **UNBLOCKED**. **Scope:** core only (lint + allowlist + hot helpers + docstrings).

## Why
The I33 coverage ratchet over-applied spans to hot-loop micro-helpers — `audit_anchors` emitted ~42k `_cosine_similarity` spans; recall 27–35k per-row `_row_to_dict`/`_extract_id` spans per op → OTLP queue saturation → **boundary spans DROPPED** (`tool.audit_anchors` unfindable in Tempo). ADR-0074 sets the policy; this phase makes the lint enforce it. **Order is load-bearing: refine I33 FIRST, then sweep** (a sweep without the lint counterpart rots).

## Commit 1 — I33 v2 (lint refinement)
1. `.observe-allowlist.json` gains a `_span_budget` section: `fq → {rationale}` = "this fn must NOT open a per-call span". Lint **HARD-FAILS** if a listed fn carries a span-opening decorator without `span=False`. Same governance as existing sections: ≥40-char rationale, stale-entry hard-fail.
2. Advisory channel (non-failing, like the ADR-0040 glob-audit report): a span-decorated fn called inside a `For`/`While` body in the same module → stdout warning. Catches NEW hot-loop spans before they storm.
3. ADR-0041 hard rule: span-opening decorators forbidden in the logging-handler module set (small explicit list — `log_config.py`, LogSpanProcessor module).
4. Widen `span=False` / `tier="hot"` semantics + docstrings (`observe.py:240-246` currently scopes `span=False` to the explicit-inner-span nesting case only; the hot-loop budget case is a second legitimate reason; `tier="hot"` wording "span only" at `observe.py:13` is muddled → fix to "attributes on enclosing span, NO per-call span").

## Commit 2 — sweep (under the refined lint)
- Populate `_span_budget` with the storm offenders: `_cosine_similarity`, `_row_to_dict`, `_extract_id`, plus grep `tier="stage"` inside loops for others.
- Flip them to `@observe(span=False)` (metrics only — the `_ring_append`/ADR-0041 treatment, precedent #173) or decorator-level aggregation (one span with count+total) where an aggregate is genuinely useful.
- **Verify:** re-run the trace sweep on a deploy; `tool.audit_anchors` and recall boundary spans present in Tempo; per-op span count for `audit_anchors`/recall drops from tens-of-thousands to tens.

## Overhead gate (from obs-standard §6)
Any overhead gate for P-SB must A/B on the **same deploy** (not vs a historical floor). Method = the recall-perf warm-floor checklist: ≥6 warm runs, median, same box, backend fixed.

## TDD
- lint: a `_span_budget` fn carrying a per-call span decorator → hard-fail; same fn with `span=False` → pass; stale `_span_budget` entry → hard-fail.
- advisory: a span-decorated fn inside a loop body → stdout warning (non-failing).
- ADR-0041: a span-opening decorator in a logging-handler module → hard-fail.
- span-count: (integration/manual) the three offenders emit metrics but no per-call span after the flip.

## Rollout
Core version bump. Register in ROADMAP + the `yadgar-roadmap-future-improvements` wiki. On completion, this plan archives (full-obs already archived; this closes the residual).
