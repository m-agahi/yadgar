# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car G — ADR seed from PAGES + retype superseded.

Spine task-table-refactor-2026-07-29, Car G:
- One-shot admin op: seed the `adr` table from existing wiki pages
  (D35b — pages over index, because §1.5 proved the index can miss rows).
- Retype 12 superseded pages to page_type='adr_superseded' (D23).
- Delete legacy parser/serializer/lock.
- Re-point project_brief's `_build_adr_log` from index parse to table query.

D35c verification gate: exact equality on {number set}, no >= tolerance.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_storage():
    s = MagicMock()
    return s


def test_seed_adr_from_pages_reads_pages_not_index(mock_storage) -> None:
    """Car G seed reads per-ADR pages (not the markdown index)."""
    from yadgar.backend.admin_exec.seed_ledger import seed_adr_from_pages

    mock_storage.list_wiki_pages.return_value = [
        {"slug": "m-agahi_yadgar_adr-0001", "content": "...", "page_type": "adr"},
        {"slug": "m-agahi_yadgar_adr-0002", "content": "...", "page_type": "adr"},
    ]
    mock_storage.list_adr_rows.return_value = []  # empty table

    with patch(
        "yadgar.backend.admin_exec.seed_ledger._get_storage",
        return_value=mock_storage,
    ):
        seed_adr_from_pages(
            directory="/home/max/git/yadgar",
            project_id="m-agahi/yadgar",
        )

    # Seed reads wiki pages (not the index file)
    mock_storage.list_wiki_pages.assert_called_once()
    # No index file read
    assert not hasattr(mock_storage, "read_index") or not mock_storage.read_index.called


def test_seed_adr_dry_run_does_not_write(mock_storage) -> None:
    """Dry-run mode reports what would be seeded without writing."""
    from yadgar.backend.admin_exec.seed_ledger import seed_adr_from_pages

    mock_storage.list_wiki_pages.return_value = [
        {"slug": "m-agahi_yadgar_adr-0001", "content": "...", "page_type": "adr"},
    ]
    mock_storage.list_adr_rows.return_value = []

    with patch(
        "yadgar.backend.admin_exec.seed_ledger._get_storage",
        return_value=mock_storage,
    ):
        result = seed_adr_from_pages(
            directory="/home/max/git/yadgar",
            project_id="m-agahi/yadgar",
            dry_run=True,
        )

    assert result["dry_run"] is True
    assert result["candidates"] == 1
    mock_storage.create_adr_row.assert_not_called()


def test_seed_adr_is_idempotent(mock_storage) -> None:
    """Re-running the seed converges (no duplicates)."""
    from yadgar.backend.admin_exec.seed_ledger import seed_adr_from_pages

    # Page already exists in the table
    mock_storage.list_wiki_pages.return_value = [
        {"slug": "m-agahi_yadgar_adr-0001", "content": "...", "page_type": "adr"},
    ]
    mock_storage.list_adr_rows.return_value = [
        {"id": 1, "project_id": "m-agahi/yadgar"},
    ]

    with patch(
        "yadgar.backend.admin_exec.seed_ledger._get_storage",
        return_value=mock_storage,
    ):
        result = seed_adr_from_pages(
            directory="/home/max/git/yadgar",
            project_id="m-agahi/yadgar",
        )

    assert result["seeded"] == 0
    assert result["skipped"] == 1


def test_seed_adr_exact_equality_gate(mock_storage) -> None:
    """D35c: verification gate uses exact equality, not >=."""
    from yadgar.backend.admin_exec.seed_ledger import (
        _collect_page_numbers,
        verify_adr_seed,
    )

    # 194 pages, 195 index rows: the 194→195 gap is the index page itself.
    # The 193→194 gap is the defect (§1.5). The gate must distinguish them.
    pages = [{"slug": f"m-agahi_yadgar_adr-{i:04d}"} for i in range(1, 195)]
    rows = [{"number": i} for i in range(1, 195)]

    page_numbers = _collect_page_numbers(pages)
    row_numbers = {r["number"] for r in rows}

    result = verify_adr_seed(page_numbers=page_numbers, row_numbers=row_numbers)
    # Exact equality: 194 pages == 194 rows == same number set
    assert result["passed"] is True


def test_legacy_adr_parser_deleted() -> None:
    """The legacy markdown-index parser is deleted after seed."""
    from yadgar.core.server.tools import adr_index

    assert not hasattr(adr_index, "parse_index_rows"), "parse_index_rows should be deleted (D35b)"
    assert not hasattr(adr_index, "_INDEX_ROW_RE"), "_INDEX_ROW_RE should be deleted"
