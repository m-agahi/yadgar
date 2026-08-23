"""Targeted tests for task 247 / C1 — adr_get omits empty-string prose keys.

The bug: ``adr_get`` returned ``rationale``, ``alternatives``, ``revisit_trigger``
as the empty string ``""``. The ``adr`` table has no such columns
(``yadgar/_shared/storage/sql/ledger_columns.py:74-78``); the prose lives on
the body page as flat bullets (``ADR.to_markdown_body`` +
``_build_adr_body``). A caller reading ``""`` would conclude "this ADR has no
rationale" when in fact the prose is on the body page — see task 247, C1.

These tests pin:
  1. adr_get does NOT emit ``rationale`` / ``alternatives`` / ``revisit_trigger``
     in its top-level response dict (the unambiguous signal: key-absent).
  2. adr_get STILL emits the row-side metadata that the caller does need:
     ``date``, ``supersedes``, ``superseded_by``, ``subsystem``, ``tier``,
     ``baseline_hash``, ``content_hash`` (D5 additive-only contract).
"""

from __future__ import annotations

from unittest.mock import patch

from yadgar.tests.core.conftest import TEST_PROJECT_ID


def _body_fixture() -> dict:
    return {
        "content": "## Purpose\n\nTest ADR body.",
        "slug": "myproj-adr-0001",
        "directory_context": "/tmp/getmerge",
        "tags": ["adr", "decisions", "adr-status:accepted", "adr-0001"],
    }


def _row_fixture() -> dict:
    return {
        "id": 1,
        "project_id": "myproj",
        "title": "Use ledger-backed ADR rows",
        "status": "accepted",
        "decided_on": "2026-08-09",
        "subsystem": None,
        "tier": None,
        "body_slug": "myproj_adr-0001",
        "created_at": "2026-08-09T00:00:00",
        "updated_at": "2026-08-09T00:00:00",
    }


def _adr_get_with_row(project_dir: str) -> dict:
    from yadgar.core.server.tools.adr import adr_get

    with (
        patch(
            "yadgar.core.server.tools.adr._resolve_project_root",
            return_value=project_dir,
        ),
        patch(
            "yadgar.core.server.tools.adr.wiki_read",
            return_value=_body_fixture(),
        ),
        patch(
            "yadgar.core.server.tools.adr._forward_admin",
            return_value={"row": _row_fixture()},
        ),
    ):
        return adr_get(directory=project_dir, adr_id="ADR-0001", project=TEST_PROJECT_ID)


def test_adr_get_omits_empty_prose_keys(tmp_path):
    """The fix: adr_get must not emit rationale/alternatives/revisit_trigger.

    Pre-fix, all three keys were present with value ``""`` (a caller reading
    ``""`` as "this ADR has no rationale" was wrong — prose lives on the body).
    Post-fix, the keys are absent entirely; the caller reads the body page.
    """
    project_dir = str(tmp_path / "getmerge")
    import os as _os

    _os.makedirs(project_dir, exist_ok=True)

    result = _adr_get_with_row(project_dir)

    for absent in ("rationale", "alternatives", "revisit_trigger"):
        assert absent not in result, (
            f"adr_get must not emit {absent!r} — the adr table has no such "
            f'column and emitting "" misleads callers (task 247, C1).\n'
            f"Result keys: {sorted(result)}"
        )


def test_adr_get_still_emits_real_metadata(tmp_path):
    """D5 additive-only: the merge path must keep emitting the row-side keys
    that the caller DOES need (date, baseline_hash, content_hash, supersedes,
    superseded_by, subsystem, tier). Pre-fix and post-fix this stays green —
    it guards against the merge path being accidentally broken by the removal.
    """
    project_dir = str(tmp_path / "getmerge")
    import os as _os

    _os.makedirs(project_dir, exist_ok=True)

    result = _adr_get_with_row(project_dir)

    for present in (
        "date",
        "supersedes",
        "superseded_by",
        "subsystem",
        "tier",
        "baseline_hash",
        "content_hash",
    ):
        assert present in result, (
            f"adr_get must keep emitting {present!r} — D5 additive-only "
            f"contract (task 247, C1 regression guard).\n"
            f"Result keys: {sorted(result)}"
        )

    # Specific value check on the date, since we built the row fixture.
    assert result["date"] == "2026-08-09"
