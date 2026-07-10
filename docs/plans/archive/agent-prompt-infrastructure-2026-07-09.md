> ARCHIVED 2026-07-10 — executing on feat/prelude-contract-wiki, ships with the train PR (stages 1+2+3)

# Plan: Agent-prompt infrastructure — stages 2–3 (discipline extraction + schema)

**Status:** AGREED (design final 2026-07-09 after 3 iterations — do NOT re-litigate the split).
**Date:** 2026-07-09
**Principle:** file = law, wiki = practice. Anything that must survive a fresh install and be testable ships as a packaged file (YAML seed materials, schema files). Anything that evolves at runtime lives as a versioned wiki page, seeded from the packaged genesis.

## Context — Stage 1 (in flight, NOT this plan)

Contract → seeded wiki page. Branch `feat/prelude-contract-wiki` (core 5.121.0):
`_YADGAR_CONTRACT` hardcoded constant deleted; contract genesis in
`yadgar/core/seed/materials/agent_prompts.yaml`; `agent_dispatch_prelude` reads the
`agent-prompt-contract` wiki page (epoch-keyed cache) with seed-on-miss; contract rule 4
points at plan lifecycle (ADR-0081/0082); `agent-prompt-plan-executing-build` seeded as
5th starter. This plan starts AFTER that PR merges and deploys.

## Stage 2 — Discipline-page extraction (0.5–1d)

Cross-cutting discipline rules are duplicated across the ~26 pattern pages today
(recall-first, observed-state-wins, findings footer, monitor cleanup before final report,
pgrep bracket form, branch-state checks). Duplication means drift: fixing a rule in one
pattern leaves stale copies in the rest.

1. Inventory: sweep all `page_type=agent_prompt` pages; cluster repeated rule text into
   candidate discipline pages (expect 4–6: e.g. `agent-discipline-recall-first`,
   `agent-discipline-process-hygiene`, `agent-discipline-branch-state`,
   `agent-discipline-plan-lifecycle`).
2. Create discipline pages as seeded wiki pages — same genesis mechanism as the contract:
   text in `agent_prompts.yaml` seed materials, seed-on-miss, versioned in wiki.
3. Rewrite pattern pages to REFERENCE disciplines (`[[slug]]` under a `## Composes`
   section) instead of inlining the text. Pattern pages shrink to what is genuinely
   pattern-specific.
4. No prelude change yet — resolution of references is Stage 3. Until then the contract
   (always included) carries the universal rules, so nothing is lost in assembled preludes.

## Stage 3 — Agent-prompt schema + composition (task 33; 2–3d)

Half-exists already: `page_type=agent_prompt` in PAGE_TYPES (`_shared/wiki_meta.py`,
requires [Purpose, Prompt]), `agent_prompt_save` stamps it, `wiki_lint` format-checks it.
Delta:

1. **Externalize PAGE_TYPES → packaged schema file** `yadgar/_shared/schemas/wiki_page_types.yaml`.
   `wiki_meta.py` loads it at import (packaged resource, not $HOME copy — no chicken-and-egg,
   ships tested with each version). Code body keeps zero schema literals.
2. **Richer agent_prompt schema** in that file: required [Purpose, Prompt]; optional
   [Preconditions, Failure modes, Verification, Composes]; frontmatter-style metadata
   (composes-with slugs, applies-to task shapes). `wiki_lint` stays advisory (never rejects
   writes — existing wiki_add contract unchanged).
3. **Composition resolution in prelude**: `agent_dispatch_prelude` resolves the pattern's
   `## Composes` references and assembles contract + disciplines + pattern within the
   2000-char budget (4000 with context). Dedup: a discipline already covered by the
   contract is not re-included. Deterministic order: contract → disciplines → pattern →
   recall hint. Budget overflow drops disciplines last-listed-first, logs a warning.
4. **Usage counter per pattern**: increment on each prelude assembly (which patterns
   actually get dispatched). Surfaces in `agent_prompt_toc` — dead patterns become visible,
   quality feedback becomes natural.
5. Tests: schema-file load + validation, composition assembly (budget, dedup, order,
   overflow), counter increment, seed-on-miss for discipline pages, lint of new optional
   sections.

## Sequencing

After: Stage 1 PR merge + deploy; test-suite hardening train (clean-code-first directive).
Stage 2 and Stage 3 item 1 (schema externalization) are independent — can run as parallel
cars if slots free. Stage 3 items 2–4 depend on both.

## References

Task 33 (schema), ADR-0081/0082 (plan lifecycle — first commit of implementing branch
archives THIS file), dispatch_helper.py (prelude assembly), `_shared/wiki_meta.py`
(PAGE_TYPES), `core/seed/materials/agent_prompts.yaml` (genesis corpus),
wiki `agent-prompt-toc`.
