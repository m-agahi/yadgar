# PLAN — MCP tool-surface audit (dedupe, deprecate, clarify)

Status: **PLANNED 2026-06-15.** User-initiated review of the 72 MCP tools surfaced
cruft + confusable names + one live bug. Small cleanup pass; runs after / alongside
the recall-scoping train. Same "make it actually usable" theme.

theme: tooling / cleanup
priority: medium (one real bug in here; rest is hygiene)

## Findings (2026-06-15 user audit)

### 1. `remember` — deprecated alias, REMOVE
Its description literally says *"Renamed to memorize. Update your MCP config."*
It's a dead alias of `memorize` still exposed in the 72-tool list. Remove it (or,
if external callers might exist, keep one release as an explicit deprecation
shim then drop). Confirm no internal caller, then delete the registration.

### 2. `bootstrap_project` vs `seed_project` — confusable; reconcile
- `bootstrap_project(directory, content)` — caller supplies a curated ≤2000-char
  `_project_init` memory + seeds default memory blocks. **Manual.**
- `seed_project(directory)` — scans the repo (configs/README/CI/entrypoints) and
  auto-creates `_seed`-tagged memories. **Automatic.**
- User: *"won't use manual bootstrap_project; seed_project makes more sense (automatic)."*
- BUT `_project_init` (what bootstrap writes) IS surfaced to the agent via
  `project_brief`/SessionStart — and it goes STALE: observed `init_memory_age_hours
  = 656` (~27 days) on yadgar itself. So the curated init memory has value to the
  agent but the manual-refresh path rots.
- **Decide:** (a) deprecate `bootstrap_project`, have `seed_project` (or a new
  auto-refresh) also produce/refresh `_project_init` from the scan; or (b) keep
  bootstrap but auto-refresh `_project_init` on a staleness signal; or (c) rename
  for clarity (`set_project_init` vs `seed_project`). Lean: fold the curated-init
  role into the automatic path so it can't go stale.

### 3. `reembed_all` — LIVE BUG (found while testing)
Running `reembed_all` on the live store returned:
`{status: ok, reembedded: 0, total_missing: 32, model: all-MiniLM-L6-v2}`.
It **found 32 memories missing embeddings but generated 0** — does nothing despite
work to do, on a healthy backend (embed service up). Possible causes: silent
embed failure swallowed; the 32 are un-embeddable (empty/NULL content, archived,
or a filter excludes them) yet still counted `missing`; or the embed path used by
reembed_all differs from the live write path. **Investigate + fix** — a backfill
tool that backfills nothing is worse than none (false "ok"). 32 memories with no
embedding are invisible to similarity search. Cross-ref `[[db-audit-fix]]` (this
is a data-integrity gap too).

### 4. General surface sweep
Review all 72 tools for other deprecated/duplicate/confusable entries (e.g. is
`wiki_get` redundant with `wiki_read`? `validate_memory` usage? `archive_purge`
vs `vacuum_*` overlap?). Produce a keep/deprecate/rename verdict per tool.

## Scope
- Remove `remember` (item 1).
- Reconcile bootstrap/seed (item 2) — decision needed before code.
- Investigate + fix `reembed_all` no-op (item 3) — likely the highest-value item.
- Surface sweep (item 4) — read-only audit → verdicts → follow-up cleanups.

## Acceptance
- `remember` gone from the tool list (or explicit shim).
- `reembed_all` actually embeds the 32 (or documents why they're legitimately
  un-embeddable + stops counting them as `missing`).
- bootstrap/seed reconciled (decision applied or doc-clarified).
- A per-tool keep/deprecate/rename table for the 72-tool surface.

## Related
- `[[db-audit-fix]]` — reembed_all's 32-missing overlaps live-store integrity
- `[[wiki-kb-usefulness-snr]]` + `[[recall-scoping-restamp]]` — same usability theme
- `[[unified-scoped-recall]]` — will itself reshape the recall/wiki_query surface
