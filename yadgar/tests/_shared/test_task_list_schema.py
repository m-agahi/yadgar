"""Task-list page-type schema + section-boundary robustness (task-list mirror).

TDD — written BEFORE the schema data-edit + prompt discipline landed.

Covers:
1. wiki_page_types.yaml gains a `task_list` entry (required: [Meta]).
2. check_page_type_format: zero issues on a well-formed task-list page;
   a `missing_section` warning when `## Meta` is absent.
3. Section-boundary robustness: a task `description` containing a column-0-looking
   `##` line and a ``` fence, kept off column 0 by the mandatory 2-space
   continuation-indent, does NOT create spurious section headings — and a
   replace_section on a DIFFERENT task leaves the poisoned task byte-identical.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Schema registry
# ---------------------------------------------------------------------------


def test_task_list_page_type_registered():
    """PAGE_TYPES gains a `task_list` entry requiring the `## Meta` section."""
    from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_SCHEMAS, PAGE_TYPES

    assert "task_list" in PAGE_TYPES, "task_list page_type not registered"
    assert PAGE_TYPES["task_list"] == ["Meta"], (
        f"task_list required sections must be [Meta]; got {PAGE_TYPES['task_list']}"
    )
    # Optional Tasks section documented (advisory shape only).
    assert "Tasks" in PAGE_TYPE_SCHEMAS["task_list"].get("optional", [])


# ---------------------------------------------------------------------------
# 2. check_page_type_format — advisory Meta lint
# ---------------------------------------------------------------------------

_WELL_FORMED = """\
<!-- yadgar task-list page — schema v1. -->

# myapp task list

## Meta
- project: myapp
- open: 1 · completed: 0

## task:0001
- subject: do the thing
- status: pending
- description: a short description
- context: src/foo.py
- blockedBy:
- blocks:
- modified: 2026-07-14T18:20:32Z
"""

_MISSING_META = """\
# myapp task list

## task:0001
- subject: do the thing
- status: pending
"""


def test_check_page_type_format_zero_issues_on_well_formed():
    """A well-formed task-list page (has `## Meta`) yields no lint issues."""
    from yadgar._shared.wiki.wiki_meta import check_page_type_format

    issues = check_page_type_format("myapp-task-list", "task_list", _WELL_FORMED)
    assert issues == [], f"well-formed task-list page should lint clean; got {issues}"


def test_check_page_type_format_flags_missing_meta():
    """A task-list page missing `## Meta` yields a `missing_section` warning."""
    from yadgar._shared.wiki.wiki_meta import check_page_type_format

    issues = check_page_type_format("myapp-task-list", "task_list", _MISSING_META)
    assert len(issues) == 1, f"expected exactly one missing-section issue; got {issues}"
    issue = issues[0]
    assert issue["type"] == "missing_section"
    assert issue["severity"] == "warning"
    assert "Meta" in issue["message"]


# ---------------------------------------------------------------------------
# 3. Section-boundary robustness (the 2-space continuation-indent discipline)
# ---------------------------------------------------------------------------

# A task whose description contains a "##"-looking line and a ``` fence, kept off
# column 0 by the mandatory 2-space continuation-indent (same discipline as the
# ADR to_markdown_body renderer). Two tasks so we can prove a replace_section on
# one leaves the poisoned one byte-identical.
_POISONED_PAGE = """\
# myapp task list

## Meta
- project: myapp
- open: 2 · completed: 0

## task:0001
- subject: poisoned task
- status: in_progress
- description: multi-line value with a fake heading and a fence
  ## Not A Heading — indented so the section parser ignores it
  ```python
  ## still not a heading inside a fence
  ```
- context: src/foo.py
- blockedBy:
- blocks:
- modified: 2026-07-14T18:20:32Z

## task:0002
- subject: clean task
- status: pending
- description: nothing tricky here
- context: src/bar.py
- blockedBy:
- blocks:
- modified: 2026-07-14T17:55:10Z
"""


def test_section_parser_ignores_indented_heading_and_fence():
    """_find_section_headings sees ONLY the real column-0 headings — the indented
    `##` line and the fenced `##` inside task:0001's description are not detected
    as section boundaries."""
    from yadgar._shared.wiki.store import _find_section_headings

    headings = [h["text"] for h in _find_section_headings(_POISONED_PAGE)]
    # Exactly the three real sections — no spurious "Not A Heading" / fenced lines.
    assert headings == ["Meta", "task:0001", "task:0002"], (
        f"section parser picked up spurious headings: {headings}"
    )


def test_zero_padded_ids_are_exact_match_not_prefix():
    """task:0001 and task:0012 are distinct sections — the matcher is exact-line,
    so a prefix collision (task:1 vs task:12) cannot occur with zero-padded ids."""
    from yadgar._shared.wiki.store import _find_section_headings

    page = "## Meta\n- project: x\n\n## task:0001\n- subject: a\n\n## task:0012\n- subject: b\n"
    headings = [h["text"] for h in _find_section_headings(page)]
    assert "task:0001" in headings
    assert "task:0012" in headings
    # The two ids are distinct sections, not one prefix-matched section.
    assert headings.count("task:0001") == 1
    assert headings.count("task:0012") == 1


def test_replace_section_leaves_sibling_task_byte_identical():
    """A replace_section on task:0002 leaves task:0001 (the poisoned one) byte
    identical — the embedded `##`/fence never corrupts the neighbour's span."""
    from yadgar._shared.wiki.store import _find_section_headings, _patch_section

    headings = _find_section_headings(_POISONED_PAGE)
    target = next(h for h in headings if h["text"] == "task:0002")
    new_body = (
        "- subject: clean task\n"
        "- status: completed\n"
        "- description: now done\n"
        "- context: src/bar.py\n"
        "- blockedBy:\n"
        "- blocks:\n"
        "- modified: 2026-07-14T19:00:00Z\n"
    )
    updated = _patch_section(_POISONED_PAGE, target, new_body, position="replace_section")

    # Extract task:0001's section span from both original and updated by locating
    # the heading and the next column-0 heading. It must be byte-identical.
    def _section_span(content: str, heading_text: str) -> str:
        hs = _find_section_headings(content)
        idx = next(i for i, h in enumerate(hs) if h["text"] == heading_text)
        lines = content.splitlines(keepends=True)
        start = hs[idx]["line_idx"]
        end = hs[idx + 1]["line_idx"] if idx + 1 < len(hs) else len(lines)
        return "".join(lines[start:end])

    assert _section_span(_POISONED_PAGE, "task:0001") == _section_span(updated, "task:0001"), (
        "replace_section on task:0002 must not alter the poisoned task:0001 section"
    )
    # And task:0002 actually changed.
    assert "status: completed" in _section_span(updated, "task:0002")
