"""Task 332 — ``validate_memory``'s no-detector fallback must not invent staleness.

``core/server/tools/admin_other.py``'s ``validate_memory`` short-circuits on
``_st._staleness`` (built by ``core/bootstrap/bootstrap.py:60`` on every full
core path).  When that slot is empty the tool used to fall through to a
hand-rolled hash check that read ``memory["directory_context"]`` **as a
filesystem path**.

Since C10f, ``write_exec/_memorize_phases/_phase_store.py:168`` stamps that
column with ``ctx.project_id`` — an ``owner/repo`` string.  Hashing it as a
path can only return ``None``, so the fallback declared every memory with a
``file_hash`` stale, with the reason ``"file no longer exists"``, **and wrote
that verdict back** via ``update_memory_staleness``.

The genuine detector (``core/staleness/staleness.py:122``) resolves the path
from ``file_hash`` via ``get_filepath_by_hash`` and never reads
``directory_context`` at all — so the fallback was not a degraded copy of it,
it was a different and wrong algorithm.

These tests pin the property that matters: **a call made with no detector must
not forward a staleness write**.  A test asserting only ``is_valid``/``reason``
would still pass against a "fix" that keeps writing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import yadgar._shared.runtime.state as _st
from yadgar.core.server.tools import admin_other

_PROJECT_ID = "m-agahi/yadgar"


class _FakeStorage:
    """Minimal storage double: one memory whose directory_context is an identity."""

    def __init__(self, memory: dict | None):
        self._memory = memory

    def get_memory(self, memory_id: int) -> dict | None:
        return self._memory


@pytest.fixture
def _no_detector():
    """Empty the ``_staleness`` slot for the duration of a test."""
    previous = _st._staleness
    _st._staleness = None
    try:
        yield
    finally:
        _st._staleness = previous


def _run(memory: dict | None) -> tuple[dict, list]:
    """Call ``validate_memory`` with a fake storage; capture admin forwards."""
    forwards: list = []

    def _capture(op, payload=None, *a, **kw):
        forwards.append((op, payload))
        return {}

    with (
        patch.object(admin_other, "_get_storage", return_value=_FakeStorage(memory)),
        patch.object(admin_other, "_forward_admin", side_effect=_capture),
    ):
        result = admin_other.validate_memory(1)
    return result, forwards


@pytest.mark.usefixtures("_no_detector")
def test_no_detector_does_not_forward_a_staleness_write():
    """The property that discriminates: no write when nothing was actually checked."""
    memory = {
        "id": 1,
        "directory_context": _PROJECT_ID,
        "project_id": _PROJECT_ID,
        "file_hash": "a" * 64,
    }
    result, forwards = _run(memory)

    staleness_writes = [op for op, _ in forwards if op == "update_memory_staleness"]
    assert staleness_writes == [], (
        "validate_memory flagged a memory stale without ever reading a real file — "
        f"forwards were {forwards!r}"
    )
    assert result["reason"] != "file no longer exists"


@pytest.mark.usefixtures("_no_detector")
def test_no_detector_reports_why_it_could_not_validate():
    """The MCP contract keys are unchanged, and the reason names the real cause."""
    memory = {
        "id": 1,
        "directory_context": _PROJECT_ID,
        "project_id": _PROJECT_ID,
        "file_hash": "a" * 64,
    }
    result, _ = _run(memory)

    assert set(result) == {"memory_id", "is_valid", "reason"}
    assert result["memory_id"] == 1
    assert result["is_valid"] is False
    assert "staleness detector" in result["reason"]


@pytest.mark.usefixtures("_no_detector")
def test_no_detector_never_hashes_the_scoping_column():
    """``directory_context`` must not reach ``_file_hash`` — it is an identity."""
    from yadgar._shared import server_helpers

    with patch.object(server_helpers, "_file_hash", side_effect=AssertionError) as spy:
        memory = {
            "id": 1,
            "directory_context": _PROJECT_ID,
            "project_id": _PROJECT_ID,
            "file_hash": "a" * 64,
        }
        _run(memory)
    assert spy.call_count == 0


def test_detector_present_still_delegates():
    """The short-circuit onto the genuine detector is untouched."""

    class _Detector:
        def validate_memory(self, memory_id: int) -> dict:
            return {"valid": True, "reason": "file unchanged"}

    previous = _st._staleness
    _st._staleness = _Detector()
    try:
        result = admin_other.validate_memory(7)
    finally:
        _st._staleness = previous

    assert result == {"memory_id": 7, "is_valid": True, "reason": "file unchanged"}
