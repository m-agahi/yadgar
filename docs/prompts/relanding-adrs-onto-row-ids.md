# Procedure — re-land a project's ADR corpus so every slug equals its row id

> **EXECUTED for `m-agahi/yadgar` on 2026-08-19 — 230/230, zero failures.**
> Old 0001–0230 → ledger ids 23–252. Audit: `~/.yadgar/adr-migration/RESULTS-2026-08-19.md`.
> Recorded as ADR-0253. The corrections below are from that run; they are not
> speculative. Anything still phrased as a prediction has been marked.

**Human-invoked, one project per run. Nothing here runs automatically. It DELETES pages.**

This REPLACES `backfill-adrs-to-ledger.md` for `m-agahi/yadgar`. That procedure preserved the
historical ADR number in the slug and pointed `body_slug` at the existing page. This one does the
opposite: the ledger row is written first, its **id is authoritative**, the body page is re-created
at a slug derived from that id, and the old page is deleted.

    row -> id N -> new page m-agahi_yadgar_adr-{N:04d} -> map line -> delete old page

Historical ADR numbers do not survive. The owner's words: *"i never needed the id in the title"*,
and *"the old pages must be deleted and new slugs created corresponding to the row number in sql
table for that adr"*.

## Why this shape, and what it deletes from the old one

`adr.id` is ONE GLOBAL `AUTO_INCREMENT` shared by every project, while `adr_list` is project-scoped
(wiki `m-agahi_yadgar_task-177`). `quinyx/flux` holds ids 2-4 and 10-21 and is actively writing.
Every previous plan tried to *predict* the id and failed:

- "skip one spent id" became six, then nine, and grows during deliberation
- a clean dry run predicted a successful apply, and did not (task 176)
- "a skip set restores id == number" was never achievable

Reading the id **back from the INSERT** makes all three unreachable. There is no `next_id`, no
`information_schema` read, no `--skip-adr`, no ADR-0006 arithmetic, and flux can burn a hundred ids
mid-run without affecting anything.

## Decisions already made — do not re-open

| | |
|---|---|
| Unlock | `wiki_set_mutability(slug, "free", reason=...)` on the 231 legacy pages. No code. The pages are `page_type='adr'` → `MUTABILITY_LOCKED`, so `wiki_delete` and `update_wiki_page` reject them today. New pages come out `locked` by default from the canonical writer, so the corpus ends correct. |
| References | **Inline `[[yadgar-adr-NNNN]]` links only — 3 pages.** Prose mentions of `ADR-NNNN` keep their historical numbers and are treated as narrative, not pointers. No ADR body is semantically edited. |
| Tier | Stamp `tier="binding"` on every INSERT. Non-optional — see the trap below. |
| Map + export | `~/.yadgar/adr-migration/`, outside the repo. ADR-0005 says git-tracked; PR #47 removed exactly that class of artifact from this PUBLIC repo. The owner chose outside. |
| `yadgar-adr-index` | Delete it with the rest. The ledger is the index now, and that page already lies (it skips ADR-0124 while calling itself "the ID source of truth"). |

## THE TRAP THAT WOULD MAKE A PERFECT RUN LOOK LIKE A TOTAL FAILURE

`adr_list` defaults `tier="binding"` and forwards it verbatim. A NULL `tier` matches neither
`"binding"` nor `"historical"`, so **NULL-tier rows are unreachable through any argument**.
Observed live 2026-08-18: 6 rows exist, default `adr_list` returns 2.

`seed_adr_rows` sets no tier. So a fully successful 230-row run through the old op would return an
empty `adr_list` and read as "the migration did nothing". **Stamp `tier="binding"` on every insert.**
Filed as ledger task 191.

## Pre-flight — every item is a gate, not a checklist

1. **Census.** Enumerate `yadgar-adr-*`. Expect **231** pages (230 ADRs + `yadgar-adr-index`), all
   `page_type='adr'`, all `mutability_override` NULL. Any other number → STOP.
2. **Purpose-made export.** The nightly `wiki/*.jsonl` export **has stopped** — newest is
   2026-08-12, six days stale, and predates the canonical pages. `~/yadgar-private-backups/` holds
   three markdown files, not snapshots. Neither is a gate. Take a fresh export of exactly the 231
   pages (slug, content, page_type, tags, category, directory_context, created_at) plus a sha256
   manifest. Verify it parses, count == 231, per-page hash matches. THEN proceed.
3. **Queue drained to verified-empty.** A stale queued write for `yadgar-adr-NNNN` draining after
   that page is deleted re-creates it.
4. **DLQ inspected** for ADR-slug payloads. (2,340 files; the 10 matching "adr-" are prose mentions.
   Re-check immediately before the run.)
5. **Exclusive lock.** There is NO concurrency lock. Two instances interleave ids and both append to
   the map, and nothing detects it until the post-condition. Take one.
6. **ADR-writing freeze in force** for the whole run, on every project that shares the sequence.

## The per-ADR state machine

The owner's order was row → page → rewrite → delete → map. Two changes, both for recoverability:

- **The map line is written BEFORE the delete.** Map-last creates the one unrecoverable window —
  old page gone, map line unwritten, nothing anywhere linking old number to new id.
- **Reference rewriting moves to pass 2.** Pass 1 copies the body VERBATIM.

```
S0  nothing done
 |  1. create_adr_row(project_id, title, status, decided_on,
 |                    tier="binding", body_slug=None)   -> id N
 v
S1  row exists, body_slug NULL                     <- crash window
 |  2. create page m-agahi_yadgar_adr-{N:04d}, content = legacy body VERBATIM
 v
S2  row + page, body_slug still NULL               <- crash window
 |  3. set_adr_body_slug(N, new_slug)
 v
S3  row fully linked, old page present             <- crash window
 |  4. append map line, fsync
 v
S4  map durable, old page present                  <- crash window
 |  5. delete old page
 v
S5  done
```

`body_slug` cannot be supplied at INSERT because the id does not exist yet. `adr_add` already uses
exactly this row → page → `set_adr_body_slug` shape.

### CORRECTION (2026-08-19) — call `adr_add`, do NOT hand-build S0→S3

The run did **not** drive `create_adr_row` / page-create / `set_adr_body_slug` separately.
It called **`adr_add` once per ADR**, which performs all three internally and returns
`{"adr_id": "ADR-NNNN", "slug": ...}` — the new id and the new slug in one response. The
five-state machine above is then the *internal* shape, and S0→S3 collapse into one call
with no crash window between them. Only S3→S4→S5 (map line, then delete) are driven here.

**`wiki_add` cannot be used for the page, and this is not a preference.** ADR pages are
`page_type='adr'` → effective mutability `locked`, and `wiki_add` carries no `_sanctioned`
token, so `insert_wiki_page` rejects it. Observed live 2026-08-18: `wiki_add(page_type='adr')`
returns `wait_timeout` and the drainer logs
`wiki page mutability='locked' forbids insert_wiki_page (page_id=None slug=None page_type='adr')`
— `page_id=None` proving a fresh INSERT, not an update. The identical call *without*
`page_type` commits instantly. `adr_add` works because it writes through
`_write_adr_body_page`'s `_sanctioned=True` canonical path (`adr.py:429-458`).

Consequence for the resume table: the S1 and S2 rows cannot occur when driving via
`adr_add`. S3 (linked, no map line) and S4 (map line, old page alive) remain live and their
repairs are unchanged.

**Leave the `# ADR-0042:` H1 alone in pass 1.** This is the keystone of the resume design: while
pass 2 has not run, old-number → new-id is independently recoverable from the database as
`(slug ⇒ id) × (H1 ⇒ old number)`. The map is therefore *verifiable against the DB* rather than
being a lone unverifiable artifact. Pass 2 rewrites the H1 along with the 3 inline links.

## Resume — run the repair sweep BEFORE continuing the loop

| State | Detection | Repair |
|---|---|---|
| S1 row, no page | Row with NULL `body_slug` whose id-derived slug resolves to nothing | Re-derive the slug (pure function of the id), re-create the page |
| S2 row + page, NULL slug | Row with NULL `body_slug` whose id-derived slug DOES resolve | `set_adr_body_slug(N, slug)` — idempotent |
| S3 linked, no map line | Canonical page whose H1 old-number is not a map key | Append the map line; the old number is readable from the copied H1 |
| S4 map line, old page alive | Map line exists and `wiki_read(old_slug)` resolves | Delete — a no-op on an absent slug |
| Torn map line | Trailing line fails to parse | Discard it and re-derive from the DB. **This is why the map is append-only** |
| Old page gone, no row, no map line | In the census, absent everywhere | **STOP.** Unrecoverable. Cannot occur if map-append precedes delete |

Two rules that make the table hold:

- **NEVER read "which ADRs already have rows" through `adr_list`.** It under-counts by the tier
  filter and you will re-insert rows that already exist — permanently, with no `adr_delete`.
  Use the map, or `adr_get` (id-keyed, no tier predicate), or the backend `list_adr_rows`.
- **`wait_timeout` is UNKNOWN, not FAILED.** `_wiki_write_canonical(wait=True)` can time out and the
  write still lands. Re-read the target slug before concluding step 2 failed, or the retry
  double-creates.

## Ordering — supersede-bearing ADRs go LAST (added 2026-08-19)

The procedure above says nothing about ordering, and the reference section treats the 14
`- supersedes:` bullets as narrative. **That is wrong for `adr_add`**, which parses the
`supersedes` argument into a real FK and flips the target row's status to `superseded`
(D23). The target row must therefore already exist.

Split the corpus in two and run it in this order:

1. **Plain ADRs** (no `- supersedes:` bullet) — 216 on `m-agahi/yadgar`.
2. **Supersede-bearing ADRs** — 14, migrated LAST.

Every one of the 14 edges points at a **lower old number**, verified before the run, so
last-place ordering guarantees each FK target already has a known new id. Resolve the
target through the map and pass the **new** id as `supersedes="ADR-00NN"` — correct under
ADR-0197, where the id IS the number. Never pass the old number from the prose.

This means ids are NOT monotonic in old-number order: on `m-agahi/yadgar` old 0030 landed
at id 239 while old 0031 landed at 55. That is expected and harmless — the map is the only
link that matters.

## Pass 2 — references

Walks the completed map. Rewrites the H1 on all 230 and the inline `[[yadgar-adr-NNNN]]` links on
the 3 pages that have them. Reuses `reslug.py`'s `_INLINE_LINK_RE` (`:190`) and its substitution
(`:374-377`) — the transform is exactly right, only its driver is unusable (it calls
`update_wiki_page` with no `_sanctioned`).

Measured reference surface: **3** pages with inline links, 14 with a `- supersedes:` bullet, 7
crossref rows. Per the decision above, only the inline links and the H1 are rewritten.

### CORRECTION (2026-08-19) — pass 2 is NOT optional, and the H1 half is already done

Two things the run changed:

- **The H1 needs no pass-2 rewrite.** `adr_add` generates the body from fields, so the H1
  comes out as `# ADR-{new_id}: {title}` immediately. The "leave the old H1 for
  recoverability" design above does not apply when driving via `adr_add` — but that also
  means the `(slug ⇒ id) × (H1 ⇒ old number)` cross-check is **unavailable**, so the map is
  a lone artifact and its fsync-before-delete ordering is the only protection. Do not skip it.
- **The inline links DO need rewriting, and skipping it breaks live pages.** This run
  skipped pass 2 and left 6 dangling `[[yadgar-adr-NNNN]]` links pointing at deleted slugs.
  Repaired same day via `wiki_replace_text` (surgical, one call per link, no page rewritten
  from memory) and verified to zero.

The 3 pages were **not ADR bodies** — they were ordinary wiki pages citing ADRs:

| Page | Links |
|---|---|
| `yadgar-install-surface-generators` | 0185→0196, 0186→0197, 0187→0198 |
| `storage-layer-map-shared-storage-mixin-composition-execution-ent` | 0195→0205, 0196→0206, 0183→0194 |
| `yadgar-roadmap-future-improvements` | `[[yadgar-adr-log]]` — the pre-2026-07-15 monolith slug, dangling since then. **Predates this migration; not repaired here.** |

So the reference sweep must query the WHOLE wiki for `[[{LEGACY_PREFIX}` — not just the ADR
corpus — and it must run AFTER the map is complete, since the new id is only known then.

## Post-condition — exact equality, no `>=`

1. `len(map) == 230`; its `old_number` set equals the pre-flight census exactly.
2. Every map line: row exists at `new_id`, `body_slug == new_slug`, **`tier == "binding"`**.
3. Every map line: page exists at `new_slug`, `page_type == "adr"`.
4. Every map line: `wiki_read(old_slug)` returns not-found.
5. `wiki_page` count with slug prefix `yadgar-adr-` == **0** (the index page is deleted too).
6. After pass 1: `sha256(new content) == content_sha256` from the map — the nothing-was-mangled proof.
7. Zero rows with NULL `body_slug` for this project.
8. `adr_list(project_id)` with DEFAULT arguments returns 230. If it returns fewer, the tier stamp
   was missed.

### CORRECTIONS to the post-conditions (2026-08-19, from the executed run)

**8 is wrong — the count is 232, not 230.** `adr_list` returns migrated rows PLUS any rows
that already existed for the project. On `m-agahi/yadgar` two canonical rows predate the run
(ids 5 and 6), so a complete run ends at **230 + 2 = 232**. Reconcile explicitly rather than
asserting a bare number:

| Set | Expected | Composition |
|---|---|---|
| pages at `{SLUG_PREFIX}_adr-*` | 236 | 230 migrated + 6 canonical pre-existing |
| ledger rows, all tiers | 232 | 230 migrated + 2 canonical (ids 5, 6) |
| rows with NO page | **0** | — |
| pages with NO row | 4 | 0001/0007/0008/0009 — pre-existing, NOT created by the run |

**Add post-condition 6b, and treat it as the one that matters most.** Clause 3 as written
checks the row's `body_slug` field against a computed pattern — that proves the field is
internally consistent, **not that a page exists at that slug**. Because the write is
row-first and the legacy page is then deleted, a silent page-write failure loses the body
to everything except the export. Diff `wiki_list(slug_prefix=...)` against every `new_slug`
in the map and require zero missing. Measured 0/230 on this run.

**6 does not hold byte-exactly if any field text was escaped.** Angle brackets passed as
`&lt;`/`&gt;` are stored as those entities, so 64 of 230 bodies differ from the legacy text
and their `content_sha256` will not validate. Cosmetic (`->` arrows are unaffected), and the
legacy text survives in the export — but either escape nothing, or state the deviation
rather than reporting clause 6 as passed. Filed as ledger task 196.

**Also assert tier explicitly.** Clause 2 requires `tier == "binding"`, and the run honoured
it — but D27 says superseded/rejected/deprecated ADRs are `historical`, so stamping every
row `binding` leaves the 14 superseded + 6 rejected polluting the default `adr_list`. On the
next run, stamp `tier` from `status`. Filed as ledger task 197.

**Do not expect the supersedes edge to be readable.** All 14 target statuses flipped, but
the superseder's own `supersedes` column reads `none` and `adr_list` shows `superseded_by: -`.
Unverifiable locally (`adr_supersedes` is engine #2; `db_inspect` is SurrealQL-only). Filed
as ledger task 195. The edges are rebuildable from the map.

Do **not** route through `cmd_backfill` or the D35c gate. `_exact_equality_gate` reconciles
`index_rows == pages_seen == page_type_adr_rows`; `pages_seen` changes *during* this run as pages are
created and deleted, and `index_rows` counts a page being deleted. Structurally unsatisfiable here.

## Map file

Append-only JSONL, one object per completed ADR, written after step 3 and before step 4, fsync per
line. `~/.yadgar/adr-migration/map-pass1.jsonl`:

```json
{"old_number":42,"old_slug":"yadgar-adr-0042","new_id":251,
 "new_slug":"m-agahi_yadgar_adr-0251","title":"...",
 "content_sha256":"...","ts":"2026-08-18T21:04:11Z","pass":1}
```

Validation on resume: parse and discard a torn trailing line; assert row+page exist for each line;
**cross-check every untouched canonical page's H1 old-number against the map's claim for that
`new_id`** — a disagreement means corruption or interleaved runs, STOP; assert `old_number` and
`new_id` are each unique.

## What deletion actually costs

`wiki_page_version` keys on `page_id`, not slug, and `delete_wiki_page` removes only the crossrefs
and the page row. Deleting 230 pages **strands ~460 version rows rather than destroying them** —
the history survives in the DB but becomes unreachable through `wiki_history`, which resolves
slug→page first. The irreversibility is about reachability. It does not remove the export gate.

## Reuse — file:line

Directly reusable, pure: `_parse_adr_id_from_slug` (`adr_seed.py:122`), `_adr_page_sort_key` (`:138`),
`_extract_title_and_status` (`:153`), `_is_per_adr_page_slug` (`:58`), `_present_adr_number_to_id`
(`:312`, no tier filter); `_INLINE_LINK_RE` (`reslug.py:190`) and its substitution (`:374-377`);
`cap_slug` / `NEW_SLUG_TEMPLATE` / `_project_id_to_slug` (`reslug.py:72`, `:178`, `:103`) — mint the
new slug through these, never by hand; `_write_adr_body_page`'s sanctioned canonical write
(`adr.py:429-458`).

NOT reusable: `_seed_one_page` / `_insert_one_row` (page-driven, inverted); `_read_next_adr_id`
(prediction is gone); `_exact_equality_gate`; **`reslug`'s write driver** (`reslug.py:353-388`) —
`update_wiki_page` with no `_sanctioned` cannot touch a locked page.

Verify the page-create path **errors** rather than silently upserting if a target slug is occupied —
`wiki_add` defaults `upsert=True`.

## Known defects this procedure must work around

- **191** `adr_list` hides NULL-tier rows; `seed_adr_rows` writes NULL tier.
- **192** ADR pages are locked; `wiki_delete` / `update_wiki_page` reject them; `yadgar backfill
  --reslug-adr-pages --apply` has therefore never been runnable, and its tests use a fake storage
  that never reaches the gate.
  **ROOT CAUSE FOUND AND FIXED 2026-08-19 (PR #54, ledger task 193).** The blocker was wider
  than 192 recorded: `wiki_set_mutability` — the escape hatch the gate names in its own error
  text — was never registered in `_ADMIN_OPS`, so `run_admin_op` raised `KeyError` and the
  route returned a bare HTTP 400 with no logged reason. That made `page_type='adr'` pages
  uncreatable, uneditable AND undeletable by every unsanctioned caller. One line registered
  it; proven by ~230 successful calls during the run. A bare 400 reads as "broken", not
  "unwired" — which is why it survived four PRs.
- **188** `adr_get` returns a cross-project chimera — `get_adr_row` discards the `project_id`.
- **190** `adr_add` uses the prose number as an FK, on every new ADR. Harmless for THIS
  procedure — under ADR-0197 the id IS the number, so passing the resolved new id as
  `ADR-00NN` is correct under both the buggy and the intended reading.
- **195** (new, from this run) `adr_add`'s supersedes edge does not surface on the row.
- **196** (new) 64 migrated bodies carry `&lt;`/`&gt;` where the legacy text had angle brackets.
- **197** (new) all 230 rows stamped `tier=binding`; 20 of them should be `historical` per D27.
- `admin_exec/wiki.py:61` `wiki_delete` has no try/except, contradicting the never-raise model; a
  locked-page rejection likely surfaces as **"not found"** for a page that exists. Confirm on one
  throwaway page before trusting the loop's error handling.
