# SPDX-License-Identifier: Apache-2.0
"""Characterization tests for Car F — ADR tools re-pointed.

Spine task-table-refactor-2026-07-29, Car F: adr_list, adr_get must keep
their existing return shapes after the spine migration. These tests pin
the contract against the new ledger-backed implementation.

id is the AUTO_INCREMENT PK and also the semantic number — no separate
allocation step. adr_add creates the ledger row, writes the wiki body,
then sets body_slug on the row.

Return shape contract (pinned here):
  adr_list() → list of {adr_id, status, date, title, ...}
  adr_get(adr_id) → {adr_id, status, date, title, body_slug, ...}
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_storage():
    s = MagicMock()
    return s


def test_adr_list_returns_expected_shape(mock_storage) -> None:
    """adr_list returns a list of dicts with adr_id, status, date, title."""
    from yadgar.core.server.tools.adr import adr_list

    mock_storage.list_adr_rows.return_value = [
        {
            "id": 194,
            "project_id": "m-agahi/yadgar",
            "status": "accepted",
            "date": "2026-08-02",
            "title": "spine plan",
            "tier": "binding",
            "subsystem": "storage",
            "supersedes": None,
            "superseded_by": None,
        }
    ]

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=mock_storage,
    ):
        result = adr_list(project_id="m-agahi/yadgar")

    assert isinstance(result, list)
    assert len(result) == 1
    row = result[0]
    assert "adr_id" in row
    assert row["adr_id"] == "ADR-0194"
    assert row["status"] == "accepted"
    assert row["title"] == "spine plan"


def test_adr_get_returns_full_record(mock_storage) -> None:
    """adr_get returns the full ADR record by (project_id, number)."""
    from yadgar.core.server.tools.adr import adr_get

    mock_storage.get_adr_row.return_value = {
        "id": 194,
        "project_id": "m-agahi/yadgar",
        "status": "accepted",
        "date": "2026-08-02",
        "title": "spine plan",
        "body_slug": "m-agahi_yadgar_adr-194",
    }

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=mock_storage,
    ):
        result = adr_get(adr_id="ADR-0194", project_id="m-agahi/yadgar")

    assert result["adr_id"] == "ADR-0194"
    assert result["status"] == "accepted"
    assert result["title"] == "spine plan"
    assert result["body_slug"] == "m-agahi_yadgar_adr-194"


def test_adr_get_parses_adr_id(mock_storage) -> None:
    """adr_get parses ADR-NNNN format to extract the number."""
    from yadgar.core.server.tools.adr import adr_get

    mock_storage.get_adr_row.return_value = {"id": 194}

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=mock_storage,
    ):
        adr_get(adr_id="ADR-0194", project_id="m-agahi/yadgar")

    mock_storage.get_adr_row.assert_called_once()
    call_kwargs = mock_storage.get_adr_row.call_args.kwargs
    assert call_kwargs["number"] == 194


def test_adr_get_handles_invalid_id_format(mock_storage) -> None:
    """adr_get returns an error for malformed ADR IDs."""
    from yadgar.core.server.tools.adr import adr_get

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=mock_storage,
    ):
        result = adr_get(adr_id="garbage", project_id="m-agahi/yadgar")

    assert result.get("ok") is False or "error" in result
    mock_storage.get_adr_row.assert_not_called()


def test_adr_add_creates_row_and_writes_body(mock_storage) -> None:
    """adr_add creates the ledger row, writes wiki body, sets body_slug."""
    from yadgar.core.server.tools.adr import adr_add

    mock_storage.create_adr_row.return_value = {
        "id": 195,
        "project_id": "m-agahi/yadgar",
        "status": "open",
        "title": "new ADR",
    }

    with (
        patch(
            "yadgar.core.server.tools.adr._get_storage",
            return_value=mock_storage,
        ),
        patch(
            "yadgar.core.server.tools.adr._wiki_write_canonical",
            return_value={"stored": True},
        ),
    ):
        result = adr_add(
            project_id="m-agahi/yadgar",
            title="new ADR",
            status="open",
            date="2026-08-02",
            context="context text",
            decision="decision text",
            rationale="rationale text",
            alternatives="",
            consequences="",
            revisit_trigger="",
            supersedes="none",
        )

    assert result["number"] == 195
    assert result["adr_id"] == "ADR-0195"
    assert result["body_slug"] == "m-agahi_yadgar_adr-195"
    mock_storage.create_adr_row.assert_called()
    mock_storage.set_adr_body_slug.assert_called_once_with(
        project_id="m-agahi/yadgar", number=195, body_slug="m-agahi_yadgar_adr-195"
    )
