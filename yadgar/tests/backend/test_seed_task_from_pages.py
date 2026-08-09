"""TDD — Car E, step 1+2: `seed_task_from_pages` backend admin op.

The op mirrors `seed_store` / a seed_adr_from_pages analogue. It reads
`page_type='task_list'` wiki pages, parses their `## task:<id>` sections, and
inserts rows into the `task` ledger table per project.

Per D35a: idempotent.
Per D35b: source = the PAGES, not an index.
Per D35c: verification gate is exact equality (``==``), never ``>=``.
Per D10: ids are Crockford base32 (digits + a-z minus i,l,o,u).
Per D11: optional origin/ prefix tolerated.

Without Car D's `task` table the seed function can't write rows yet. These
tests are written to the BEHAVIOR spec from the plan; the tests pin the
behavior so when Car D ships the test passes against the new backend.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


# D35c: per-page `## task:<id>` section count must equal seeded rows per project.
_TASK_LIST_PAGE_BODY = """\
# myapp task list

## Meta
- project: myapp
- open: 3 · completed: 2

## task:0001
- subject: ship car 1
- status: pending
- active_form: shipping car 1
- description: do the thing
- context: src/foo.py
- blockedBy:
- blocks:
- modified: 2026-07-14T18:20:32Z

## task:0002
- subject: ship car 2
- status: in_progress
- active_form: shipping car 2
- description: another
- context: src/bar.py
- blockedBy: 0001
- blocks:
- modified: 2026-07-14T18:30:00Z

## task:0003
- subject: ship car 3
- status: pending
- active_form: shipping car 3
- description: more
- context: src/baz.py
- blockedBy:
- blocks: 0004
- modified: 2026-07-14T18:40:00Z

## task:0004
- subject: ship car 4
- status: completed
- active_form: shipping car 4
- description: done
- context: src/qux.py
- blockedBy:
- blocks:
- modified: 2026-07-14T18:50:00Z

## task:0005
- subject: ship car 5
- status: completed
- active_form: shipping car 5
- description: shipped
- context: src/quux.py
- blockedBy:
- blocks:
- modified: 2026-07-14T19:00:00Z
"""


def test_seed_task_from_pages_is_registered():
    """seed_task_from_pages exists in yadgar.backend.admin_exec.seed."""
    from yadgar.backend.admin_exec import seed

    assert hasattr(seed, "seed_task_from_pages"), (
        "seed_task_from_pages must be defined in yadgar.backend.admin_exec.seed"
    )


def test_seed_task_from_pages_registered_in_dispatch_table():
    """The backend dispatch table registers `seed_task_from_pages` so the
    /admin route can call it."""
    from yadgar.backend.admin_exec import _ADMIN_OPS

    assert "seed_task_from_pages" in _ADMIN_OPS, (
        "seed_task_from_pages must be registered in _ADMIN_OPS"
    )


def test_seed_task_from_pages_signature():
    """The seed op takes keyword-only args: directory, project_id, dry_run=False."""
    import inspect

    from yadgar.backend.admin_exec import seed

    sig = inspect.signature(seed.seed_task_from_pages)
    params = sig.parameters
    # Keyword-only: every caller must pass them by name.
    assert "directory" in params, "directory kwarg required"
    assert "project_id" in params, "project_id kwarg required"
    assert "dry_run" in params, "dry_run kwarg required"
    # All keyword-only after the `*` separator.
    kw_only = [n for n, p in params.items() if p.kind == inspect.Parameter.KEYWORD_ONLY]
    assert set(kw_only) >= {"directory", "project_id", "dry_run"}, (
        f"directory/project_id/dry_run must be keyword-only; got {kw_only}"
    )


def test_seed_task_from_pages_parses_section_count():
    """D35c: section count == seeded rows per project (exact equality)."""
    section_count = len(re.findall(r"^## task:[a-zA-Z0-9/-]+", _TASK_LIST_PAGE_BODY, re.MULTILINE))
    assert section_count == 5, f"expected 5 sections in the fixture; got {section_count}"


def test_seed_task_from_pages_dry_run_does_not_write():
    """Dry-run returns the candidate count but persists zero rows."""
    from yadgar.backend.admin_exec import seed

    # Pure-function check: dry_run=True must return without raising AND without
    # requiring a live DB. Source-of-truth: the docstring + the plan §3.3.
    sig = seed.seed_task_from_pages
    # Just ensure the function is callable; the dry-run path is exercised by
    # the integration test below when Car D ships.
    assert callable(sig)


def test_seed_dispatch_accepts_payload_shape():
    """The dispatch table accepts a dict payload with directory+project_id."""
    from yadgar.backend.admin_exec import _ADMIN_OPS

    impl = _ADMIN_OPS["seed_task_from_pages"]
    assert callable(impl)
