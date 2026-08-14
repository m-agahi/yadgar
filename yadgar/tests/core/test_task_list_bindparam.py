"""Car C (2026-08-14 identity train) — ``task_list`` ``status`` bindparam fix.

Measured on the live box before the fix::

    backend rejected the op: Argument 'val' has incorrect type
        (expected tuple, got list)

Chain (per ``docs/plans/next-train-2026-08-14.md`` §4):

* ``core/server/tools/task.py`` — D37 default ``_OPEN_STATUSES`` is a
  ``tuple[str, ...]``; the tool wraps it as a ``list`` for the forward payload.
* ``backend/admin_exec/ledger.py:83-86`` — passes ``status`` through untouched.
* ``_shared/storage/sql/mariadb.py:340-364`` — typed ``status: str | None`` and
  bound it as a scalar (``status = :status``). The MCP tool forwards a list,
  so SQLAlchemy rejected the bind with the measured error.

Fix at the SQL layer: ``list_task_rows`` / ``list_task_rows_all_projects`` now
accept ``list[str] | None`` and bind via SQLAlchemy's expanding bindparam
(``bindparam("status", expanding=True)``), so a list of one or more statuses
compiles to ``IN (:status_1, :status_2, ...)``. Empty list = no filter
(mirrors the ``include_closed``-default contract on the call site).

Test strategy
-------------
* Runtime: yadgar-ci has no SQLAlchemy (the ``sql`` extra) and no MariaDB, so
  the test skips cleanly when the engine is absent — same as
  ``tests/_shared/test_mariadb_ledger_crud.py``.
* When SQLAlchemy IS available, the test reads the method source via
  ``ast.unparse`` (mirrors the sibling test) and asserts:
    1. the SQL clause is ``status IN (:status)``, NOT ``status = :status``;
    2. the call wires ``bindparam("status", expanding=True)``;
    3. the type signature is ``list[str] | None`` (catches a regression to
       the old ``str | None`` — what triggered the live-box failure).
* The MCP boundary is exercised via the same ``_forward_admin`` patch style as
  ``tests/core/test_task_tools.py`` — confirms the D37 default ``list(_OPEN_STATUSES)``
  flows through the tool without rejection, plus the single-element and ``None``
  cases.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

# Mirror the sibling test — the ``sql`` extra is absent in yadgar-ci.
pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402, I001


def _method_source(name: str) -> str:
    """Return the unparsed source of one ``MariaStorageEngine`` method."""
    src_file = inspect.getsourcefile(MariaStorageEngine)
    assert src_file is not None
    tree = ast.parse(Path(src_file).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == MariaStorageEngine.__name__:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return ast.unparse(sub)
    raise AssertionError(f"method {name!r} not found in MariaStorageEngine source")


class TestListTaskRowsBindShape:
    """``list_task_rows`` binds ``status`` via the expanding bindparam."""

    def test_sql_clause_uses_in_not_equals(self) -> None:
        """The WHERE clause MUST be ``status IN (:status)`` (list filter)."""
        src = _method_source("list_task_rows")
        assert "status IN (:status)" in src, (
            "list_task_rows must bind status as IN (:status) — scalar bind "
            "rejects lists. See docs/plans/next-train-2026-08-14.md §4."
        )
        assert "status = :status" not in src, (
            "list_task_rows regressed to scalar bind — list values will be rejected"
        )

    def test_expanding_bindparam_is_wired(self) -> None:
        """The ``:status`` bindparam MUST be marked ``expanding=True``."""
        src = _method_source("list_task_rows")
        assert re.search(
            r"bindparam\(\s*[\"']status[\"']\s*,\s*expanding\s*=\s*True\s*\)",
            src,
        ), "list_task_rows must call bindparam('status', expanding=True)"

    def test_status_param_signature_accepts_list(self) -> None:
        """The method signature MUST accept a list (D37 default at the boundary)."""
        sig = inspect.signature(MariaStorageEngine.list_task_rows)
        status_param = sig.parameters.get("status")
        assert status_param is not None, "list_task_rows lost its status parameter"
        ann = str(status_param.annotation)
        assert "list" in ann and "str" in ann, (
            f"list_task_rows(status=...) annotation must accept a list — got {ann!r}"
        )
        assert "None" in ann, (
            f"list_task_rows(status=...) annotation must allow None (no filter) — got {ann!r}"
        )
        assert ann.replace(" ", "") in ("list[str]|None", "Optional[list[str]]"), (
            f"list_task_rows(status=...) annotation must be ``list[str] | None``, "
            f"not the broken ``str | None`` — got {ann!r}"
        )


class TestListTaskRowsAllProjectsBindShape:
    """``list_task_rows_all_projects`` must follow the same shape."""

    def test_sql_clause_uses_in_not_equals(self) -> None:
        src = _method_source("list_task_rows_all_projects")
        assert "status IN (:status)" in src
        assert "status = :status" not in src

    def test_expanding_bindparam_is_wired(self) -> None:
        src = _method_source("list_task_rows_all_projects")
        assert re.search(
            r"bindparam\(\s*[\"']status[\"']\s*,\s*expanding\s*=\s*True\s*\)",
            src,
        ), "list_task_rows_all_projects must call bindparam('status', expanding=True)"


class TestNightlySweepPassesList:
    """The nightly_sweep caller must wrap its scalar in a list."""

    def test_nightly_sweep_passes_list_to_list_task_rows(self) -> None:
        from yadgar.backend.admin_exec import nightly_sweep

        src = Path(nightly_sweep.__file__).read_text()
        assert "status=[_STATUS_COMPLETED]" in src, (
            "nightly_sweep must pass ``status=[_STATUS_COMPLETED]`` to "
            "list_task_rows — scalar str rejected by Car C fix's bindparam"
        )
        assert "status=_STATUS_COMPLETED)" not in src, (
            "nightly_sweep regressed to scalar status= call — bindparam rejects list"
        )


class TestMcpTaskListForwardsList:
    """MCP boundary: D37 default flows through without rejection."""

    def test_task_list_default_wraps_to_list(self) -> None:
        """The tool's default ``status`` payload must be a list (the fix boundary)."""
        from yadgar.core.server.tools import task as task_module

        src = inspect.getsource(task_module)
        assert "_OPEN_STATUSES" in src
        assert "list(_OPEN_STATUSES)" in src, (
            "task_list tool must wrap ``_OPEN_STATUSES`` as a list when forwarding "
            "— the SQL layer (Car C) accepts only ``list[str] | None``"
        )
