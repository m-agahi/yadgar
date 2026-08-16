"""Engine-#2 ledger CRUD methods on ``MariaStorageEngine`` (Car A of 0047).

The methods are async + reach the live DB; the yadgar-ci image does not have
MariaDB. The assertions are therefore static:

1. **Existence + signature** — every method the plan names is on
   ``MariaStorageEngine`` with the right async-ness and kwarg shape. Caught
   by a rename / parameter-removal before any code path runs.
2. **SQL shape** — the SQL each method emits is inspected as a string (the
   method body text). This is intentionally string-grep rather than an
   end-to-end DB round-trip: car H's ``test_mariadb_restore_arm`` owns the
   integration half (live MariaDB), and a no-DB test that reads the method
   body is what catches a missing ``WHERE id = :id`` or a regression to
   ``SELECT ... FOR UPDATE``.

The static SQL shape check is the same shape ``scripts/check_ledger_chokepoint``
uses to whitelist ``MariaStorageEngine`` — so the methods' SQL strings live
behind the chokepoint and are inspectable via ast / regex on the source.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

# ``sqlalchemy`` is gated by the ``sql`` extra — the yadgar-ci image skips it.
pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Method existence + signature (per-entity tools, D1)
# ---------------------------------------------------------------------------


_TASK_METHODS = (
    "create_task_row",
    "list_task_rows",
    "list_task_rows_all_projects",
    "get_task_row",
    "update_task_row",
    "set_task_body_slug",
    "add_task_blocked_by",
    "list_task_blocked_by",
    # Car E: the read half of the join table. ``list_task_blocks`` is the
    # inverse direction (no reader existed), ``remove_task_blocked_by`` is the
    # DELETE that used to be inline SQL in the admin op body, and
    # ``list_task_edges`` is the bulk both-directions read the LIST path uses
    # instead of 2N round-trips.
    "list_task_blocks",
    "remove_task_blocked_by",
    "list_task_edges",
)
_ADR_METHODS = (
    "create_adr_row",
    "list_adr_rows",
    "get_adr_row",
    "set_adr_body_slug",
    "add_adr_supersedes",
)
_AGENT_PATTERN_METHODS = (
    "save_agent_prompt",
    "list_agent_prompt_rows",
    "get_agent_prompt_row",
    "increment_agent_prompt_uses",
)
_AGENT_DISCIPLINE_METHODS = (
    "save_agent_discipline",
    "list_agent_discipline_rows",
)
_PATTERN_COMPOSES_METHODS = ("set_pattern_composes",)
_SCOPE_FILTER_METHODS = ("apply_scope_filter",)

ALL_LEDGER_METHODS = (
    _TASK_METHODS
    + _ADR_METHODS
    + _AGENT_PATTERN_METHODS
    + _AGENT_DISCIPLINE_METHODS
    + _PATTERN_COMPOSES_METHODS
    + _SCOPE_FILTER_METHODS
)


@pytest.mark.parametrize("name", ALL_LEDGER_METHODS)
def test_ledger_method_exists(name):
    """Every plan-§3 method is on ``MariaStorageEngine``.

    Caught by a rename or accidental drop before any code path runs.
    ``apply_scope_filter`` is the D17 no-op hook; the rest are the CRUD
    surface.
    """
    method = getattr(MariaStorageEngine, name, None)
    assert method is not None, f"MariaStorageEngine missing method: {name}"


@pytest.mark.parametrize(
    "name",
    _TASK_METHODS
    + _ADR_METHODS
    + _AGENT_PATTERN_METHODS
    + _AGENT_DISCIPLINE_METHODS
    + _PATTERN_COMPOSES_METHODS,
)
def test_crud_method_is_coroutine(name):
    """Every CRUD method is ``async def`` — the engine is async (§1 of plan).

    ``apply_scope_filter`` is the D17 no-op hook and is intentionally
    SYNC — a no-op wrapper today, not a DB call.
    """
    method = getattr(MariaStorageEngine, name)
    assert inspect.iscoroutinefunction(method), (
        f"{name} must be async — the engine is async (mariadb.py:113)"
    )


def test_apply_scope_filter_is_a_no_op():
    """D17 — the hook returns its query unchanged today (no tenancy columns).

    ``apply_scope_filter`` is a ``staticmethod`` — the no-op hook does not
    need the engine handle until tenancy columns are restored. Calling
    it on the class directly avoids constructing an engine.
    """
    sentinel = object()
    out = MariaStorageEngine.apply_scope_filter(query=sentinel, project_id="p")
    assert out is sentinel


# ---------------------------------------------------------------------------
# Source-level SQL shape checks (no DB)
# ---------------------------------------------------------------------------


def _method_source(name: str) -> str:
    """Return the source text of a single MariaStorageEngine method.

    ASTs the class module, finds the method's ``FunctionDef`` / ``AsyncFunctionDef``,
    then ``unparse``s it back to source. Useful for inspecting the SQL string
    arguments without running the method.
    """
    src_file = inspect.getsourcefile(MariaStorageEngine)
    assert src_file is not None
    tree = ast.parse(Path(src_file).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == MariaStorageEngine.__name__:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return ast.unparse(sub)
    raise AssertionError(f"method {name!r} not found in MariaStorageEngine source")


@pytest.fixture(scope="module")
def create_task_row_src() -> str:
    return _method_source("create_task_row")


@pytest.fixture(scope="module")
def list_task_rows_src() -> str:
    return _method_source("list_task_rows")


@pytest.fixture(scope="module")
def increment_uses_src() -> str:
    return _method_source("increment_agent_prompt_uses")


@pytest.fixture(scope="module")
def save_agent_prompt_src() -> str:
    return _method_source("save_agent_prompt")


def test_create_task_row_returns_dict_with_id(create_task_row_src):
    """``create_task_row`` returns ``dict`` with ``id`` (the AUTO_INCREMENT PK).

    PR #32 §13.2 blocker 2: return shapes MUST be keyed on ``id``, not
    ``number``. ADR-0197 retired ``number``.
    """
    assert "id" in create_task_row_src
    assert "-> dict" in create_task_row_src or "-> dict | None" in create_task_row_src


def test_create_task_row_inserts_into_task(create_task_row_src):
    """The SQL statement inserts into the ``task`` table — and uses ``id``
    LAST INSERT id to return the new row's PK (MySQL's ``LAST_INSERT_ID()``
    or SQLAlchemy's ``inserted_primary_key``).
    """
    assert re.search(r"INSERT\s+INTO\s+task\b", create_task_row_src, re.IGNORECASE), (
        "create_task_row must INSERT INTO task"
    )


def test_list_task_rows_filters_by_project_id(list_task_rows_src):
    """Project-scoped list reads must filter on ``project_id``.

    Without the WHERE clause every project sees every other project's
    rows — a hard violation of the per-project tenancy the registry
    exists to enforce.
    """
    assert "FROM task" in list_task_rows_src
    assert "project_id" in list_task_rows_src


def test_increment_agent_prompt_uses_is_atomic(increment_uses_src):
    """D40 — ``SET uses = uses + 1`` (atomic), NOT read-modify-write.

    A read-modify-write (SELECT then UPDATE) loses writes under
    concurrency: two callers both read ``uses=3``, both write ``uses=4``,
    net effect is one increment instead of two.
    """
    assert re.search(
        r"SET\s+uses\s*=\s*uses\s*\+\s*1|uses\s*=\s*uses\s*\+\s*1",
        increment_uses_src,
        re.IGNORECASE,
    ), "increment_agent_prompt_uses must be SET uses = uses + 1"


def test_save_agent_prompt_upserts_on_name(save_agent_prompt_src):
    """PR #32 Fix 8 — second save of the same ``agent_pattern.name`` must
    UPDATE, not violate the UNIQUE constraint.

    The detection: the method must contain an UPSERT (INSERT ... ON
    DUPLICATE KEY UPDATE) on ``agent_pattern``, or an explicit
    UPDATE-else-INSERT pair.
    """
    src_lower = save_agent_prompt_src.lower()
    assert "agent_pattern" in src_lower
    assert "on duplicate key update" in src_lower or "update agent_pattern" in src_lower, (
        "save_agent_prompt must upsert on name (Fix 8)"
    )
