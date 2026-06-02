# Workflow: Roadmap Update After Each Ship

**Convention since v5.41.4.** Replaces full read-modify-write (RMW) for routine ship entries.

## TL;DR

After each ship, call `wiki_append_section` targeting the `Recently shipped` section.
Reserve full RMW for structural restructures only.

## Why

Full RMW on the `yadgar-roadmap-future-improvements` wiki page costs ~9k tokens per call
(read the whole page, edit, write back). The 2026-05-31 corruption was caused by
short-snippet overwrites via `wiki_update`; `wiki_append_section` is section-atomic
and sidesteps that failure mode while also being far cheaper.

## Template

```python
wiki_append_section(
    slug="yadgar-roadmap-future-improvements",
    section="Recently shipped",
    content="- **vX.Y.Z (YYYY-MM-DD):** one-line summary. N/N tests. Key change.",
    position="start_of_section",   # newest entry at top
)
```

Replace `vX.Y.Z`, the date, the summary, and the test count. One line per ship.

### Full example (v5.41.4)

```python
wiki_append_section(
    slug="yadgar-roadmap-future-improvements",
    section="Recently shipped",
    content=(
        "- **v5.41.4 (2026-06-02):** roadmap-update-lag signal + `update_roadmap` "
        "recommended action in `project_brief(mode='signals')`. 7 tests."
    ),
    position="start_of_section",
)
```

## When to use full RMW instead

- Restructuring section headers or table rows in Pipeline / Deferred decisions
- Closing a previously open item (needs an edit to a non-`Recently shipped` line)
- Removing stale entries (full page read required to locate them)

For those cases: read full page → edit in memory → `wiki_update(slug=..., content=<full>)`.

## Pipeline-table updates

`wiki_append_section` cannot edit table rows. After shipping a Pipeline item, do a
targeted RMW of just the Pipeline table row:

```python
# read
page = wiki_read("yadgar-roadmap-future-improvements")
# edit the relevant row in page["content"]
wiki_update(slug="yadgar-roadmap-future-improvements", content=<edited content>)
```

## Signal: `roadmap_update_lag_hours`

`project_brief(mode="signals")` now returns `roadmap_update_lag_hours` (float).

- `> 0` and a ship detected → `recommended_actions` includes `update_roadmap` action
- `-1.0` → roadmap wiki page not found (sentinel; no action emitted)
- `0` → roadmap updated after last master commit (no action)

The `update_roadmap` recommended action includes a `suggested_call` field with the
`wiki_append_section` invocation pre-filled. Copy-paste it, fill in the summary.

## Ship-detection heuristic

PRIMARY: `pyproject.toml` `version` field differs between roadmap's `updated_at`
timestamp and current master HEAD.

FALLBACK: commit message matches `^merge: v\d+\.\d+\.\d+` or `chore: bump version`
(handles squash/rebase merges where the "merge:" prefix is absent but version changed).
