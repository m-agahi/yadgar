# PLAN v5.50.12 — Viz detail-panel stale-state + SSE node-type fix

Status: PLANNED 2026-06-12. Core release (frontend `index.html` + one backend SSE emit). No backend image version change unless the SSE emit lands (then core only).

## Symptom (reported 2026-06-12)

Clicking a node in the 3D viz showed the detail panel with header **"WIKI"** + title **"quinyx-aws-org-migration-decision-register"** while the BODY showed a **memory**: `HEAT 1.0000`, the "YADGAR DEV WORKFLOW…" content, memory tags (`_anchor`, `yadgar`…), `PROJECT /home/max/git/yadgar`, `CREATED 2026-05-30`, `CONNECTIONS 6 semantic · 0 temporal · 0 transition`, `NODE ID mem:519714`. A Frankenstein panel — wiki header/title over a memory body.

## Confirmed via investigation (NOT guesses)

1. **Backend is correct.** Live `/api/graph` serves node `mem:519714` as `{"type":"memory","label":"YADGAR DEV WORKFLOW…","content":"YADGAR DEV WORKFLOW…", tags, directory, created_at}`. No wiki fields. Whole-payload scan: **0** nodes with a `mem:` id and a wiki-ish type (2128 nodes). `memory_get(519714)` confirms `store_type="semantic"`, `is_protected=true`, `_anchor` tag. → **The record is a clean memory. Bug is frontend-only.**
2. The title shown ("quinyx-aws-org-migration-decision-register") is **absent from `519714`'s data** — it's a *different* (wiki) record's slug/title. → proves **cross-selection leakage**, not this node's data.
3. `showDetail(node)` (`index.html:1744`) branches: `if (node.type === 'wiki')` (`:1748`) sets `det-type='WIKI'`, `det-title=node.label||node.slug`, async `_fetchWikiContent(node.slug)`; else (`:1817`) sets `det-type=(node.type||'unknown').toUpperCase()`, `det-title=content.slice(0,100)`, the heat/content/project/created/connections rows. The two branches render DIFFERENT field sets — the observed body is unambiguously the **memory (else) branch**, the observed header/title is the **wiki branch**. They cannot both be the current node → the header/title are **stale from a previously-selected wiki node**.

## Latent defects found (fix as defense-in-depth)

- **A. SSE `memory_added` omits `type`.** Backend `_phase_post_write.py:199-206` emits node `{id, heat, content, tags, directory}` — no `type`. Frontend `index.html:2070-2079` pushes `msg.node` as-is → SSE-added memories have `type===undefined` → render header **"UNKNOWN"** (`:1818` `(node.type||'unknown')`) and miss the wiki/memory branch logic.
- **B. No SSE handler for wiki events.** `sse.onmessage` (`index.html:2064-2081`) handles only `system_metrics`, `daemon_health`, `memory_added`. No `wiki_added`/`wiki_updated`/`wiki_deleted` → wiki changes never stream into the viz (only appear on full reload). Backend wiki emits (`wiki.py:134-140`) also omit `type`.
- **C. Split-brain type check.** Header label path uses strict `node.type === 'wiki'` for the wiki branch but `node.type.toUpperCase()` for the fallback. A type with odd casing/whitespace would take the else branch yet print "WIKI" — header and body could disagree by construction.

## Root cause

**Primary (the reported symptom): `showDetail` does not fully reset the panel between selections.** `det-type` and `det-title` (and the in-flight `_fetchWikiContent`) persist from a prior WIKI selection; a subsequent MEMORY selection renders the memory body but the header/title are not reliably overwritten in all paths (and/or a late async wiki-content fetch writes back). Result = wiki header/title over memory body.

**Contributing:** defects A–C make node `type` unreliable and the two render branches structurally divergent, widening the blast radius.

## Fix

### Step 0 — Repro first (pin the exact stale element)
Reproduce: select a wiki node, then a memory node (and the reverse). Capture which DOM elements (`det-type`, `det-title`, `det-body`, `det-heat-fill`, `#wiki-content-body`) retain prior-selection values, and whether `_fetchWikiContent` resolves after the new selection. This confirms the precise leak before patching. (Repro is the test.)

### Step 1 — Full panel reset + selection guard (the real fix)
- At the TOP of `showDetail`, reset every shared element unconditionally (`det-type`, `det-title`, `det-body`, `det-heat-fill` width/background) BEFORE branching — no element can carry prior state.
- Add a monotonic `selectionId` (increment per `showDetail` call). `_fetchWikiContent` captures the id at call time and only writes to the DOM if `selectionId` still matches → late wiki-content fetches can't bleed into a newer selection.
- Ensure BOTH branches set every shared element (no partial leakage).

### Step 2 — Unify the type check (defect C)
- One helper `nodeType(node)` returning a normalized (`String(node.type||'').toLowerCase().trim()`) type. Header label + branch selection + color + shape ALL derive from it, so header and body can never disagree. Header text from a single map (`{wiki:'WIKI', memory:'MEMORY', entity:'ENTITY'}`, fallback uppercased).

### Step 3 — SSE node `type` + wiki handlers (defects A, B)
- Backend: include `"type"` in the SSE node payloads — `memory_added` (`_phase_post_write.py`) → `"memory"`; `wiki_added`/`wiki_updated` (`wiki.py`) → `"wiki"`.
- Frontend: when ingesting ANY SSE node, set `node.type` explicitly from the event name (don't trust the payload) — `memory_added→'memory'`. Add handlers for `wiki_added`/`wiki_updated` (upsert with `type='wiki'`) and `wiki_deleted` (remove). Mirror the memory dedup-by-id pattern.

### Step 4 — Tests
- Frontend (vitest/jsdom): `showDetail(wikiNode)` then `showDetail(memoryNode)` → `det-type`==='MEMORY', `det-title`===memory content, NO wiki slug/title anywhere in the panel; reverse order symmetric.
- `_fetchWikiContent` resolving AFTER a newer selection does NOT mutate the panel (selectionId guard).
- `nodeType()` normalization: `'Wiki'`/`' wiki '`/`'WIKI'` all classify as wiki (header + branch agree).
- SSE: `memory_added` ingested node has `type==='memory'`; a `wiki_added` event upserts a node with `type==='wiki'`.

### Step 5 — Ship
Bump core 5.50.11 → 5.50.12; CHANGELOG; tag `v5.50.12`; build+push core image; nix core bump; PyPI. (Backend unchanged — 5.5.0.)

## Out of scope
- Reworking the detail panel's visual design.
- The wiki-no-heat / category-color findings (separate; documented in the ideas page).

## Effort
S–M. Mostly `index.html` (panel reset + selectionId guard + `nodeType` helper + 2 SSE handlers) + a 2-line backend SSE-emit `type` add + ~6 frontend tests. ~0.5–1 day. Step 0 repro pins the exact stale element before patching.

## Risk
- The exact stale mechanism is inferred from static analysis + backend ground-truth (which is confirmed clean); Step 0 repro must confirm the precise element before the Step 1 patch — do not skip it.
- Full-reset + both-branches-set-everything is robust regardless of which element leaked, so the fix holds even if the precise leak differs slightly from the hypothesis.
