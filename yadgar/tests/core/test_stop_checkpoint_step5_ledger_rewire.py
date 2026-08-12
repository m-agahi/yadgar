"""TDD — Car E, step 7: stop_checkpoint_prompt.md step 5 rewire.

Per the Car E plan §3.4: step 5 (TASK-LIST MIRROR) of the stop-hook
checkpoint protocol must use the new task tools (`task_list`, `task_write`)
instead of `wiki_write_task_list`. The page-format SCHEMA block at lines
156-181 must be deleted.

After Car E:
- step 5b uses `task_list(project_id=...)` (D37 open-only default)
- step 5c uses `task_write(project_id=..., title=..., status=..., state=...,
  active_form=...)` per task
- No `wiki_write_task_list` references
- No SCHEMA block at the bottom of step 5
- `project_id` is a caller parameter, not re-derived per write
"""

from __future__ import annotations

import os


def _read_source() -> str:
    """Resolve the on-disk path of stop_checkpoint_prompt.md without importing."""
    # yadgar/tests/core/test_foo.py — yadgar tests live at top of repo.
    here = os.path.abspath(__file__)
    repo_root = here
    while repo_root and not os.path.exists(os.path.join(repo_root, "pyproject.toml")):
        parent = os.path.dirname(repo_root)
        if parent == repo_root:
            break
        repo_root = parent
    with open(
        os.path.join(
            repo_root,
            "yadgar",
            "core",
            "hooks",
            "templates",
            "stop_checkpoint_prompt.md",
        )
    ) as fh:
        return fh.read()


def _step5_body(src: str) -> str:
    """Return the body of step 5 (TASK-LIST MIRROR) from the prompt."""
    # Step 5 is the heading "5. TASK-LIST MIRROR" up to step 6.
    match = _step5_match(src)
    assert match is not None, "step 5 of the stop_checkpoint_prompt must exist"
    return match.group(0)


def _step5_match(src: str):
    import re

    return re.search(
        r"5\. TASK-LIST MIRROR.*?(?=\n6\.)",
        src,
        re.DOTALL,
    )


def test_step5_no_longer_uses_wiki_write_task_list():
    """step 5 must NOT reference wiki_write_task_list after Car E."""
    body = _step5_body(_read_source())
    assert "wiki_write_task_list" not in body, (
        "step 5 must use task_list / task_write, not wiki_write_task_list"
    )


def test_step5_uses_task_list_tool():
    """step 5 must reference the task_list tool (D37 open-only default)."""
    body = _step5_body(_read_source())
    assert "task_list" in body, "step 5b must reference task_list to read open tasks"


def test_step5_uses_task_write_tool():
    """step 5 must reference the task_write tool for persistence."""
    body = _step5_body(_read_source())
    assert "task_write" in body, "step 5c must reference task_write to persist new/updated tasks"


def test_step5_no_longer_has_page_schema_block():
    """The page-format SCHEMA block at lines 156-181 must be deleted."""
    body = _step5_body(_read_source())
    assert "SCHEMA (page body)" not in body, (
        "the page-format SCHEMA block must be deleted — tasks now live in SQL"
    )


def test_step5_no_longer_reads_wiki_page_for_task_list():
    """step 5b must NOT instruct the model to wiki_read the task-list page."""
    body = _step5_body(_read_source())
    assert 'wiki_read("{project}-task-list"' not in body, (
        "step 5b must not read the wiki task-list page — it reads the task ledger"
    )


def test_step5_uses_project_id_caller_param():
    """step 5 must treat project_id as a caller parameter, not re-derive."""
    body = _step5_body(_read_source())
    assert "project_id=" in body, "step 5 must pass project_id= to the task tools (ADR-0202)"


def test_step5_does_not_instruct_wiki_write():
    """step 5 must not instruct the model to write the wiki page."""
    body = _step5_body(_read_source())
    assert "wiki_write" not in body, (
        "step 5 must not instruct the model to call any wiki_write* tool"
    )
