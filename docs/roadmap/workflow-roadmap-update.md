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

## Section-by-section guide

**Use `wiki_append_section` for bullet-list sections:**

| Section | Position | Notes |
|---|---|---|
| `Recently shipped` | `start_of_section` | newest entry at top |
| `Follow-ups logged` | `end_of_section` | append new items |
| `Open architectural questions` | `end_of_section` | append new numbered item |
| `Workflow rules (anchored)` | `end_of_section` | append new rule |

**Use `wiki_append_section` with `position="replace_section"` for structural sections (tables / state):**

| Section | Why replace_section |
|---|---|
| `Pipeline (in dispatch order)` | Markdown table — row status changes need full body rewrite |
| `Deferred decisions` | Markdown table — same reason |
| `Branches` | Bullet list of current branches; ship → branch deleted |

```python
wiki_append_section(
    slug="yadgar-roadmap-future-improvements",
    section_heading="Pipeline (in dispatch order)",
    content="<new full markdown table body>",
    position="replace_section",
)
```

`replace_section` replaces the section BODY (heading preserved). Cheaper than full RMW
because the rest of the page is untouched.

**Use full RMW only for:**
- Editing the page preamble (e.g., `Currently deployed (LIVE)` lines that sit BEFORE the first `##` heading)
- Cross-section restructuring
- Removing entire sections

```python
page = wiki_read("yadgar-roadmap-future-improvements")
new_content = edit_in_memory(page["content"])
wiki_update(page_id=page["id"], fields={"content": new_content})
```

## Common mistake (2026-06-02 lesson)

If the user reports "pipeline table looks stale" after you appended ship entries to
`Recently shipped`: you wrote to the wrong section. **Pipeline table rows are NOT
auto-updated by appending to `Recently shipped`.** Use `replace_section` on the
Pipeline section to mark shipped items struck-through and add new in-flight rows.

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
