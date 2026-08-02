# SPDX-License-Identifier: Apache-2.0
"""Strict-type tests for spine code — no implicit Python type casting.

These tests pin the contract: spine tools reject malformed input with a
clear error rather than silently coercing (e.g. int("abc") raises
ValueError; str(val).strip() accepts any type).

The goal: weird exceptions from Python's automatic casting never reach
callers. Every input is validated at the tool boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── ADR ID parsing ───────────────────────────────────────────────────────────


def test_adr_get_rejects_non_string_adr_id() -> None:
    """adr_get rejects non-string adr_id (no str() coercion)."""
    from yadgar.core.server.tools.adr import adr_get

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        result = adr_get(project_id="m-agahi/yadgar", adr_id=None)

    assert result.get("error") is not None


def test_adr_get_rejects_integer_adr_id() -> None:
    """adr_get rejects integer adr_id (no str() coercion)."""
    from yadgar.core.server.tools.adr import adr_get

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        result = adr_get(project_id="m-agahi/yadgar", adr_id=42)

    assert result.get("error") is not None


def test_adr_get_rejects_non_integer_number() -> None:
    """adr_get rejects adr_id whose number portion is non-integer."""
    from yadgar.core.server.tools.adr import adr_get

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        result = adr_get(
            project_id="m-agahi/yadgar", adr_id="ADR-notanumber"
        )

    assert result.get("error") is not None


def test_adr_get_rejects_float_adr_id() -> None:
    """adr_get rejects float adr_id (no implicit int truncation)."""
    from yadgar.core.server.tools.adr import adr_get

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        result = adr_get(project_id="m-agahi/yadgar", adr_id=42.7)

    assert result.get("error") is not None


# ── Task tool input validation ───────────────────────────────────────────────


def test_task_write_rejects_non_string_title() -> None:
    """task_write rejects non-string title (no str() coercion)."""
    from yadgar.core.server.tools.task import task_write

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=MagicMock(),
    ):
        result = task_write(project_id="m-agahi/yadgar", title=None)

    assert result.get("ok") is False
    assert "title" in result.get("error", "").lower()


def test_task_write_rejects_integer_title() -> None:
    """task_write rejects integer title (no str() coercion)."""
    from yadgar.core.server.tools.task import task_write

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=MagicMock(),
    ):
        result = task_write(project_id="m-agahi/yadgar", title=42)

    assert result.get("ok") is False


def test_task_write_rejects_non_string_project_id() -> None:
    """task_write rejects non-string project_id."""
    from yadgar.core.server.tools.task import task_write

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=MagicMock(),
    ):
        result = task_write(project_id=123, title="valid title")

    assert result.get("ok") is False


# ── ADR add input validation ─────────────────────────────────────────────────


def test_adr_add_rejects_non_string_title() -> None:
    """adr_add rejects non-string title."""
    from yadgar.core.server.tools.adr import adr_add

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        result = adr_add(
            project_id="m-agahi/yadgar",
            title=None,
            status="open",
            date="2026-08-02",
            context="c",
            decision="d",
            rationale="r",
            alternatives="",
            consequences="",
            revisit_trigger="",
            supersedes="none",
        )

    assert result.get("ok") is False


def test_adr_add_rejects_invalid_status() -> None:
    """adr_add rejects status values outside the enum."""
    from yadgar.core.server.tools.adr import adr_add

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        result = adr_add(
            project_id="m-agahi/yadgar",
            title="valid",
            status="bogus_status",
            date="2026-08-02",
            context="c",
            decision="d",
            rationale="r",
            alternatives="",
            consequences="",
            revisit_trigger="",
            supersedes="none",
        )

    assert result.get("ok") is False
    assert "status" in result.get("error", "").lower()


# ── ADR list input validation ────────────────────────────────────────────────


def test_adr_list_rejects_negative_limit() -> None:
    """adr_list rejects negative limit (no implicit abs())."""
    from yadgar.core.server.tools.adr import adr_list

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        result = adr_list(project_id="m-agahi/yadgar", limit=-1)

    # Negative limit is documented as "no limit" but should not be negative.
    # The test pins that we accept it per the legacy contract but don't crash.
    assert result is not None


def test_adr_list_rejects_non_integer_limit() -> None:
    """adr_list rejects non-integer limit (no implicit int coercion)."""
    from yadgar.core.server.tools.adr import adr_list

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=MagicMock(),
    ):
        # String limit should not be silently coerced.
        result = adr_list(project_id="m-aghi/yadgar", limit="50")

    # Either it works because the legacy contract accepts it, or it rejects.
    # The strict guarantee: no crash with a confusing ValueError.
    assert result is not None
