# Prompt — backfill a project's ADR wiki pages into the `adr` ledger

**Human-invoked, one project per run. Nothing here runs automatically.**

Unlike the task backfill, ADR **bodies stay exactly where they are** — they are wiki
pages and remain wiki pages (plan D4). This procedure creates the ledger *rows* that
point at them, and re-slugs the pages onto the project-scoped naming scheme. No page is
deleted. No ADR content is edited. ADRs are immutable; nothing here mutates one.

Copy this file into an instance that has the project's ADR pages, fill in the
parameters, and let it work. Every STOP is a hard stop: report and wait.

## Parameters

| Name | Example | How to get it |
|---|---|---|
| `PROJECT_ID` | `m-agahi/yadgar` | the SessionStart banner prints it |
| `PROJECT_DIR` | `/home/max/git/yadgar` | repo root |
| `LEGACY_PREFIX` | `yadgar-adr-` | `{repo-basename}-adr-` |

`SLUG_PREFIX` is `PROJECT_ID` with `/` → `_` — e.g. `m-agahi_yadgar`. Canonical page slugs
are `{SLUG_PREFIX}_adr-NNNN` (plan D32 ③).

Every yadgar MCP call passes `project="PROJECT_ID"`.

---

## The numbering rule — read this before anything else

`adr.id` is `AUTO_INCREMENT` and **is** the ADR number (ADR-0197). There is no separate
`number` column and no way to supply an id on insert. So an ADR lands on its own number
only if two things hold:

1. rows are inserted in **ascending ADR-number order**, and
2. the AUTO_INCREMENT counter is sitting at exactly the number about to be inserted.

MariaDB 11.4 does **not** reset that counter when rows are deleted, not even across a
restart — verified empirically 2026-08-15. Only `TRUNCATE` resets it, and `adr_supersedes`
has an FK onto `adr.id` which blocks `TRUNCATE`. **So the counter only ever moves
forward. There is no do-over.** Get step 3 right the first time.

## Step 0 — read the current ledger state

```
adr_list(directory=PROJECT_DIR, project=PROJECT_ID, tier=None, limit=0)
```

Record every row: its `id` and its `slug`. **This tells you where the counter is** — the
next insert gets `MAX(id) + 1`, always.

Three cases:

- **Empty ledger** → the first insert gets id 1. Backfill every ADR from 0001 ascending
  and each lands on its own number.
- **Rows present, and they already are the historical ADRs** → this project is already
  backfilled. **STOP.**
- **Rows present that are NOT historical ADRs** (a new ADR was written before the
  backfill ran — this is the `m-agahi/yadgar` case, row id=1 is an identity ADR from
  2026-08-15) → those ids are permanently spent. Skip the historical ADRs that would
  have wanted them, so the rest still align. See Step 3.

## Step 1 — enumerate the pages and prove the range is contiguous

```
wiki_list(directory=PROJECT_DIR, project=PROJECT_ID, slug_prefix=LEGACY_PREFIX, limit=300)
```

Metadata only — do **not** read page bodies, you do not need them and they are large.
Page if the result is truncated. Repeat with `slug_prefix="{SLUG_PREFIX}_adr-"` to catch
anything already reslugged.

Parse `NNNN` from each slug. Report:

- total count, min, max
- **the complete list of missing numbers in [min..max]**
- any slug under the prefix that does not parse as `…-adr-<digits>` (e.g. `…-adr-index`,
  `…-adr-log` — these are index pages, not ADRs, and are correctly excluded by the op)
- any duplicate number

**STOP if there are gaps.** Insertion is sequential, so a missing number shifts every ADR
after it down by one, silently, for the rest of the range. A gap needs an explicit
decision (leave the tail misaligned, or insert a placeholder row to consume the number) —
it is not something to reason past on your own.

## Step 2 — re-slug the pages

Dry run first. This is the default; do not pass `--apply` yet.

```
yadgar backfill --reslug-adr-pages
```

It rewrites `{LEGACY_PREFIX}NNNN` → `{SLUG_PREFIX}_adr-NNNN`, and with it the
`[[crossref]]` rows in both directions, inline `[[old-slug]]` links inside page bodies,
and `adr.body_slug` on any row that already exists.

Read the dry-run output and check two things:

- **Collisions.** A target slug already occupied by a different page is reported and
  skipped, never overwritten. Expect exactly one on `m-agahi/yadgar`:
  `yadgar-adr-0001` → `m-agahi_yadgar_adr-0001`, occupied by the 2026-08-15 identity ADR.
  That is correct and expected — that page keeps its legacy slug.
- **Count.** Pages rewritten + collisions skipped should equal the total from Step 1.

**STOP if any collision is unexplained.** Then apply:

```
yadgar backfill --reslug-adr-pages --apply
```

## Step 3 — decide which ADRs to skip, and why

Skip exactly the historical ADRs whose numbers are already spent by the rows found in
Step 0.

For `m-agahi/yadgar`: row id=1 is taken, so **historical ADR-0001 is skipped**. Historical
0002–0230 then land on ids 2–230 — each on its own number. One ADR displaced, 229 exact.

A skipped ADR keeps its page and stays readable by slug. It will **not** appear in
`adr_list`, will not get `tier`/`subsystem` rollups, and `adr_get("ADR-0001")` returns
whatever actually holds id 1. Say so in your report; it is a real cost, not a footnote.

Before skipping, check what the skipped ADR is and whether anything points at it:

- `wiki_read("{LEGACY_PREFIX}0001")` — status, and whether it was superseded
- grep the other ADR bodies for `supersedes: ADR-0001`

**STOP if another ADR supersedes the one you are about to skip.** Supersede links are
written from the number parsed out of the prose and passed straight through as a row id,
so a link to a skipped ADR points at whatever row occupies that id — a wrong link, not a
missing one.

## Step 4 — run the backfill

```
yadgar backfill --adr-rows
```

The op inserts in ascending ADR-number order, enumerates both slug schemes, and is
idempotent — it dedups on `body_slug`, so a page that already has a row is counted in
`rows_skipped` and never re-inserted. Re-running is safe. (This is the opposite of the
deleted task seed, which claimed idempotency while writing `body_slug=NULL`; ADR pages
always have a slug, so here the constraint genuinely binds.)

## Step 5 — check the gate, which does not check itself

The result dict carries:

```
project_id · directory · pages_seen · rows_inserted · rows_skipped
flagged · supersedes_links · gate{index_rows, pages_seen, page_type_adr_rows, exact_match}
```

**`exact_match: false` does not raise. Nothing stops on it. You must look.**

Assert all of:

- `gate["exact_match"]` is true
- `rows_inserted` == pages enumerated in Step 1, minus the skips from Step 3
- `flagged` is empty (anything in it is a page whose slug would not parse)

**STOP on any failure.** Report the whole dict; do not summarise it away.

## Step 6 — verify the numbering actually landed

This is the check that matters, and it is not the same as the gate.

```
adr_list(directory=PROJECT_DIR, project=PROJECT_ID, tier=None, limit=0)
```

For a sample spanning the range — lowest backfilled, a few in the middle, highest —
confirm the row's `id` equals the number in its own `body_slug`:

```
id 2   ↔ …_adr-0002
id 115 ↔ …_adr-0115
id 230 ↔ …_adr-0230
```

**STOP on the first mismatch.** A mismatch means insertion order was wrong, and because
the counter never goes backwards it cannot be fixed by re-running — it needs a decision
from the user before anything else is written to the table.

## Step 7 — report

- rows inserted, skipped, flagged
- which ADRs were skipped and why
- re-slug collisions
- the id ↔ slug spot-checks from Step 6
- `gate` verbatim
- crossrefs that changed

## What this procedure does NOT do

- **Delete or edit any ADR page.** Bodies stay in the wiki, versioned (D4). ADRs are
  immutable; there is no `update_adr_row` and no `adr_delete` in the surface, deliberately.
- **Touch `retype_page_type`.** That is D23's supersede-lifecycle writer and is out of
  scope here. Note: it has never run through the `/admin` route in production.
- **Renumber anything.** The counter only moves forward. If the numbers do not line up,
  that is a decision for the user, not a repair to attempt.
