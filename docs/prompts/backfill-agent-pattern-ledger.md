# Prompt — backfill a wiki agent library into the `agent_pattern` / `agent_discipline` ledger

**Human-invoked. Nothing here runs automatically. There is deliberately no CLI flag.**

ADR-0005 (binding): corpus backfills are human-run *procedures* under `docs/prompts/`, not
automation. Its rationale names the exact risk this file exists to avoid — "a command that
can duplicate the corpus in one invocation". A `yadgar backfill --backfill-agent-pattern`
flag was written for this backfill and then removed unrun; the flag's `_CliStorage` raised
`RuntimeError` unconditionally, so it never worked and nothing was lost. This procedure is
its replacement.

The mechanism is `scripts/backfill_agent_pattern_from_wiki.py`. This file is the judgement
around it: what to check first, when to stop, and how to verify afterwards. Copy it into an
instance that can reach the yadgar backend, fill in the parameters, and let it work. **Every
STOP is a hard stop: report and wait.**

Nothing is deleted by this procedure. Wiki bodies stay wiki bodies (plan D4); this creates
the ledger *rows* that point at them.

## Parameters

| Name | Example | How to get it |
|---|---|---|
| `PROJECT_DIR` | `/home/max/git/yadgar` | repo root |
| `PROJECT_ID` | `m-agahi/yadgar` | the SessionStart banner prints it |
| `PAGE_TYPE` | `both` | `agent_pattern`, `agent_discipline`, or `both` |

---

## STOP 0 — read this before anything else: the ledger is install-wide

`agent_pattern` and `agent_discipline` have **no `project_id` column**
(`yadgar/_shared/storage/sql/migrations/versions/002_ledger_tables.py`), and both carry
`UNIQUE(name)` across the entire install. Two consequences you must accept before running:

1. **This is not a per-project write.** Backfilling `PROJECT_ID`'s pages changes the pattern
   discovery surface (`agent_prompt_list`, `agent_dispatch_prelude`) for *every* project on
   this install.
2. **The script offers no project scope, on purpose.** A knob that cannot narrow the write is
   a knob that lies, and the wiki reader's own directory filter could not narrow it either:
   it matches `directory_context IN (D, 'global')` and every page `agent_prompt_save` /
   `discipline_save` writes declares `directory="global"`, so it cannot exclude a single
   agent-library page. (ADR-0225 separately retires `directory` as a scoping concept; the
   residue sweep enforces that.) `PROJECT_ID` above is for the census reads and the commit
   message — it does not scope the backfill.

If a per-project agent library is what you want, **STOP** — that needs a schema change
(a `project_id` column plus a composite unique), not a backfill.

## Step 1 — record the pre-run census, and COMMIT it

ADR-0005 requires the source backed up to a git-tracked file, committed before any write.
Nothing is deleted here, so the adaptation is: **capture the pre-run state of the
destination**, which is what a recovery would need. A dump of the wiki pages would be
pointless — they are not touched.

```
agent_prompt_list(status=None, limit=0, project="PROJECT_ID")
```

Write the full row list — `name`, `body_slug`, `content_hash`, `purpose`, `status`, `uses` —
to `docs/backfills/agent-pattern-ledger-pre-<YYYY-MM-DD>.json`, `git add` it, and commit
with a message naming this procedure. **Do not proceed until that commit exists.** Recovery
from a wrong run is manual and this file is the only thing that makes it possible: the write
is an UPSERT keyed on `UNIQUE(name)`, so a wrong run does not duplicate rows — it silently
**overwrites** `purpose` and `status` on rows it did not create.

Record the row count. Call it `N_BEFORE`.

## Step 2 — dry run

```
uv run scripts/backfill_agent_pattern_from_wiki.py --page-type PAGE_TYPE
```

Read the stderr summary. It prints, per run:

- `scanned` — wiki pages the prefix scan returned
- `would_insert` — rows the apply would INSERT
- `already_present` — pages whose `name` already has a ledger row
- `skipped_unknown_page_type`, `skipped_page_type_filtered`,
  `skipped_non_string_content`, `skipped_empty_slug` — every dropped row, attributed
- `content_hash_mismatches` — already-present rows pinned to different bytes than their page
- `gate` — `{"applicable": false, "would_reconcile": <bool>, ...}`

**STOP if `would_reconcile` is `false`.** It means
`would_insert + already_present + every skip bucket != scanned`, i.e. a row went somewhere
the report cannot name. Do not apply against an unattributable census.

**STOP if `content_hash_mismatches` is non-empty.** A ledger row already exists whose
`content_hash` disagrees with its wiki body. The backfill will not touch those rows — it
never rewrites an already-present row, because the row may legitimately be newer than the
page. But it is a live `check_page_row_desync` violation and you should report it and get a
decision before adding more rows next to it.

**STOP if `would_insert` is 0.** Already backfilled. Nothing to do.

## Step 3 — judge the candidate list

The dry-run JSON on stdout carries the full candidate set. This is the judgement the script
cannot make:

- **Retired patterns.** A wiki page for a pattern nobody should dispatch any more should not
  become a discovery-surface row. Skip it — delete or retype the page first, then re-run.
- **Slugs outside the convention.** `_slug_to_name` strips `agent-prompt-` /
  `agent-discipline-` and otherwise passes the slug through literally, so a page at
  `agent-prompt-foo-v2` seeds `name="foo-v2"`. Decide whether that is the name you want in
  the discovery surface *before* it is written; `UNIQUE(name)` means renaming later is a
  delete plus an insert.
- **Cross-project collisions.** Two projects with a same-named pattern page get ONE row.
  Whichever runs first wins and the second reads as `already_present`. If the census shows
  a name you did not expect, that is why — **STOP** and decide.

## Step 4 — apply

```
uv run scripts/backfill_agent_pattern_from_wiki.py --page-type PAGE_TYPE --apply
```

Exit code 1 means at least one insert failed; the failures print as `FLAGGED` lines and are
listed in the JSON `flagged` array. The run is partial, not corrupt: the script is
idempotent, so fix the cause and re-run — already-written rows come back as
`already_present`.

Read the gate: `{"exact_match": true, "accounted": N, "scanned": N}`. `exact_match: false`
means the run did not account for every scanned row and should be investigated before the
next step.

## Step 5 — verify

```
agent_prompt_list(status=None, limit=0, project="PROJECT_ID")
```

- Row count should be `N_BEFORE + rows_inserted`. Any other number: **STOP** and report.
- Spot-check three inserted rows: `agent_prompt_get(pattern="<name>")` returns both the row
  and the wiki body, and its `content_hash` matches the body.
- Re-run the DRY RUN from Step 2. It must now report `would_insert: 0`. That is the
  idempotency proof; if it is non-zero, the names being derived do not match the names being
  written and you should **STOP**.
- `check_invariants()` — the cross-engine `check_page_row_desync` arm compares every
  `agent_pattern` / `agent_discipline` row's `content_hash` against its wiki body. It must
  not report new violations.

## What this procedure does NOT do

- It does not write `agent_pattern_composes` edges (which disciplines a pattern composes).
- It does not derive `baseline_hash`; rows land with it NULL and the first mutation sets it.
- It does not repair a `content_hash` disagreement on an existing row — only reports it.
- It does not regenerate `agent-prompt-toc` (task 90). That page is separately frozen and
  `agent_prompt_list` treats it as retired; the two surfaces still disagree.
