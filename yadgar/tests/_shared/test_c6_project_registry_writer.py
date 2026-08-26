"""C6 — the ``project`` registry WRITER, and where its error classes live.

``003_project_registry`` creates the ``project`` table; ``002_ledger_tables``
ships zero rows; and before this car there was no ``INSERT INTO project``
anywhere in the tree, so the first ``create_task_row`` died on
``fk_task_project`` with an opaque SQL error.

The writer is deliberately NOT ``INSERT OR IGNORE`` (ADR-0202 / ADR-0223): a
free-string ``project=`` is exactly how a phantom namespace gets minted, and
auto-creating a row on a typo is the failure the registry exists to prevent.
A duplicate key is an ERROR the operator sees, not a silent success.

The error classes live in a STDLIB-ONLY module under ``_shared`` for a reason
the ``sql``-extra venv cannot catch by running tests: the backend guard's own
docstring promises it stays importable on hosts without engine #2, and a
top-level import of a sqlalchemy-touching module would quietly break that
while every test here still passes. ``test_errors_module_is_stdlib_only``
asserts it at the source level instead.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

# ``sqlalchemy`` is gated by the ``sql`` extra — the yadgar-ci image skips it.
pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402, I001
from yadgar._shared.storage.sql.errors import (  # noqa: E402
    DuplicateProjectError,
    ProjectRegistryUnavailableError,
    UnknownProjectError,
)

_ERRORS_PATH = Path(__file__).resolve().parents[2] / "_shared" / "storage" / "sql" / "errors.py"


def _body_source_without_docstring(fn) -> str:
    """Return *fn*'s source with its docstring removed.

    Needed by the ``INSERT OR IGNORE`` prohibition below: the docstring names
    the banned SQL forms in order to forbid them, so a grep over raw source
    would flag the prohibition rather than a violation. Parsing and dropping
    the docstring node leaves the executable body — the only place the
    banned text would actually matter.
    """
    tree = ast.parse(inspect.cleandoc(inspect.getsource(fn)))
    fn_node = tree.body[0]
    assert isinstance(fn_node, ast.AsyncFunctionDef | ast.FunctionDef)
    body = fn_node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        fn_node.body = body[1:]
    return ast.unparse(fn_node)


# ── the error classes ───────────────────────────────────────────────────────


def test_errors_module_is_stdlib_only():
    """``sql/errors.py`` must import nothing third-party.

    The module is the seam every raiser and catcher shares: ``mariadb.py`` /
    ``sql/registry.py`` (which need sqlalchemy) and the core-side callers that
    must stay off the ``sql`` extra. A third-party import here would drag
    sqlalchemy in at import time for all of them — invisible in a venv that
    HAS the extra, which is every venv this suite runs in.

    Car-J relaxes the rule for ``yadgar._shared.refusal``: that module does
    NOT reach engine #2 (it pulls opentelemetry lazily, not sqlalchemy), and
    re-using its ``AdminRefusal`` is the only way to keep class identity with
    the rest of the codebase. The structural invariant — "importable on a
    host that never installs the ``sql`` extra" — still holds, because
    ``yadgar._shared.refusal`` is importable without sqlalchemy.
    """
    tree = ast.parse(_ERRORS_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    allowed_yadgar = frozenset({"yadgar._shared.refusal"})
    forbidden = [
        m for m in imported if m.split(".")[0] not in {"__future__"} and m not in allowed_yadgar
    ]
    assert not forbidden, f"sql/errors.py must be engine-#2-free; imports: {forbidden}"


def test_duplicate_project_error_carries_the_key():
    """The structured error carries the offending key verbatim.

    The operator running the seed needs to know WHICH key collided, not that
    "a" key did — the seed writes many rows in one pass.
    """
    exc = DuplicateProjectError("m-agahi/yadgar")
    assert exc.project_id == "m-agahi/yadgar"
    assert "m-agahi/yadgar" in str(exc)


def test_registry_unavailable_error_is_not_an_unknown_project_error():
    """ "Registry cannot be consulted" and "project is not in it" are different.

    Collapsing them would let an absent engine read as a rejected project_id
    (or vice versa) at the call site, which is the ambiguity the guard's
    engine-absent branch exists to remove.
    """
    assert not issubclass(ProjectRegistryUnavailableError, UnknownProjectError)
    assert not issubclass(UnknownProjectError, ProjectRegistryUnavailableError)


@pytest.mark.parametrize(
    "cls", [UnknownProjectError, DuplicateProjectError, ProjectRegistryUnavailableError]
)
def test_registry_errors_are_runtime_errors(cls):
    """Callers expect ``except RuntimeError`` to catch every registry failure.

    Matches the existing backend structured-error pattern
    (``RestoreVerificationError(RuntimeError)``).
    """
    assert issubclass(cls, RuntimeError)


# ── the writer on MariaStorageEngine ────────────────────────────────────────


def test_create_project_row_exists_and_is_async():
    """The writer lives on the engine — the sanctioned surface for row access."""
    method = getattr(MariaStorageEngine, "create_project_row", None)
    assert method is not None, "MariaStorageEngine missing create_project_row"
    assert inspect.iscoroutinefunction(method)


def test_list_project_rows_exists_and_is_async():
    """The registry READ the backfill uses to validate a host-supplied mapping."""
    method = getattr(MariaStorageEngine, "list_project_rows", None)
    assert method is not None, "MariaStorageEngine missing list_project_rows"
    assert inspect.iscoroutinefunction(method)


def test_create_project_row_takes_keyword_only_registry_fields():
    """Signature pins the §16.5 column set, keyword-only.

    Positional args on a five-column INSERT is how ``kind`` and
    ``remote_url`` get transposed.
    """
    sig = inspect.signature(MariaStorageEngine.create_project_row)
    params = sig.parameters
    for name in ("key", "kind", "display_name", "remote_url"):
        assert name in params, f"create_project_row missing kwarg: {name}"
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"


def test_create_project_row_is_not_insert_or_ignore():
    """THE assertion of this commit — a plain INSERT, never a swallow.

    ADR-0202/0223: the registry is load-bearing precisely because a free
    string is how a phantom namespace is minted. The swallowing forms would
    each turn a typo into a silently-created project. Read from the method
    SOURCE because no MariaDB is available here and the SQL text is the
    artefact.

    The DOCSTRING is stripped before matching — it names the banned forms in
    order to forbid them, and a grep over raw source would flag the
    prohibition itself.
    """
    src = _body_source_without_docstring(MariaStorageEngine.create_project_row).upper()
    for banned in ("INSERT IGNORE", "INSERT OR IGNORE", "ON DUPLICATE KEY"):
        assert banned not in src, (
            f"create_project_row must not use {banned!r} — a duplicate key is an "
            "error the operator sees (ADR-0202/0223)"
        )
    assert "INSERT INTO PROJECT" in src, "create_project_row must INSERT INTO project"


def test_assert_project_registered_exists_and_is_async():
    """The in-engine guard — sits INSIDE the chokepoint so no caller bypasses it."""
    method = getattr(MariaStorageEngine, "assert_project_registered", None)
    assert method is not None, "MariaStorageEngine missing assert_project_registered"
    assert inspect.iscoroutinefunction(method)


# ── the admin-op surface ────────────────────────────────────────────────────


def test_registry_ops_are_registered_on_the_admin_dispatch():
    """Both registry ops are reachable over ``/admin``.

    Without a registered op the operator has no way to put the first row in
    a table every ledger write FKs to — the deployment is bricked with a
    correct schema.
    """
    from yadgar.backend.admin_exec import admin_ops

    ops = admin_ops()
    assert "create_project_row" in ops
    assert "list_project_rows" in ops


def test_registry_seed_op_is_not_registry_guarded():
    """The seed must NOT call the registry guard — that is a bootstrap deadlock.

    If seeding a project required a registered project, nothing could ever
    be registered. Pinned as a source assertion because the deadlock only
    shows up on a genuinely empty deployment, which no test fixture is.
    """
    from yadgar.backend.admin_exec import ledger

    src = inspect.getsource(ledger.create_project_row)
    assert "assert_project_registered" not in src


# ── writer behaviour (no DB — a fake engine stands in for the connection) ────


class _FakeConn:
    """Minimal async connection: records ``execute`` calls, or raises."""

    def __init__(self, raise_on_execute: BaseException | None = None) -> None:
        self.raise_on_execute = raise_on_execute
        self.executed: list[tuple[str, dict | None]] = []

    async def execute(self, sql, params=None):
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.executed.append((str(sql), params))
        return _FakeResult()


class _FakeResult:
    """The one attribute the ledger INSERT paths read off a result."""

    lastrowid = 1


class _FakeEngine:
    """Stands in for the sqlalchemy ``AsyncEngine`` handle on the storage object.

    Only ``begin()`` is exercised by the writer; it is an async context
    manager yielding the connection.
    """

    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def begin(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _engine_with(conn: _FakeConn) -> Any:
    """Build a ``MariaStorageEngine`` WITHOUT connecting.

    ``__init__`` creates a real sqlalchemy engine against a unix socket that
    does not exist here, so the object is constructed unbound and given a
    fake ``_engine``. The methods under test touch nothing else.
    """
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)
    return engine


@pytest.mark.parametrize("bad_kind", ["", "GIT", "svn", "remote", "local/foo"])
@pytest.mark.asyncio
async def test_create_project_row_rejects_an_unknown_kind(bad_kind):
    """``kind`` is validated before the INSERT, naming the legal values.

    003 declares ENUM('git','local'); MySQL's own rejection names neither
    the legal values nor the caller, and on a non-strict server would
    coerce rather than reject.
    """
    conn = _FakeConn()
    engine = _engine_with(conn)
    with pytest.raises(ValueError, match="kind"):
        await engine.create_project_row(key="m-agahi/yadgar", kind=bad_kind)
    assert conn.executed == [], "no statement may be issued for an invalid kind"


@pytest.mark.asyncio
async def test_create_project_row_rejects_an_empty_key():
    """An empty key is the degenerate phantom namespace — rejected up front."""
    conn = _FakeConn()
    engine = _engine_with(conn)
    with pytest.raises(ValueError, match="key"):
        await engine.create_project_row(key="", kind="local")
    assert conn.executed == []


@pytest.mark.asyncio
async def test_create_project_row_issues_one_insert_and_returns_the_row():
    """The happy path: one INSERT, and the written values come back."""
    conn = _FakeConn()
    engine = _engine_with(conn)
    row = await engine.create_project_row(
        key="m-agahi/yadgar", kind="git", remote_url="git@github.com:m-agahi/yadgar.git"
    )
    assert len(conn.executed) == 1
    stmt, params = conn.executed[0]
    assert "INSERT INTO project" in stmt
    assert params["key"] == "m-agahi/yadgar"
    assert params["kind"] == "git"
    assert row["key"] == "m-agahi/yadgar"


@pytest.mark.asyncio
async def test_create_project_row_raises_duplicate_project_error_on_collision():
    """A duplicate key is an ERROR carrying the key — never a silent skip.

    This is the ``INSERT OR IGNORE`` prohibition expressed as behaviour
    rather than as a source grep: re-seeding an existing project must tell
    the operator which key already existed.
    """
    from sqlalchemy.exc import IntegrityError

    integrity = IntegrityError("INSERT INTO project", {}, Exception("duplicate"))
    engine = _engine_with(_FakeConn(raise_on_execute=integrity))
    with pytest.raises(DuplicateProjectError) as caught:
        await engine.create_project_row(key="m-agahi/yadgar", kind="git")
    assert caught.value.project_id == "m-agahi/yadgar"


@pytest.mark.asyncio
async def test_assert_project_registered_raises_for_an_unknown_key():
    """The in-engine guard rejects an unregistered project_id."""
    engine: Any = object.__new__(MariaStorageEngine)
    calls: list[dict] = []

    async def _row_exists(*, table, key_column, key_value, limit=1):
        calls.append({"table": table, "key_column": key_column, "key_value": key_value})
        return False

    engine.row_exists = _row_exists
    with pytest.raises(UnknownProjectError) as caught:
        await engine.assert_project_registered("quinyx/typo")
    assert caught.value.project_id == "quinyx/typo"
    assert calls == [{"table": "project", "key_column": "key", "key_value": "quinyx/typo"}]


@pytest.mark.asyncio
async def test_assert_project_registered_passes_for_a_known_key():
    """A registered project passes silently — no write, no error."""
    engine: Any = object.__new__(MariaStorageEngine)

    async def _row_exists(*, table, key_column, key_value, limit=1):
        return True

    engine.row_exists = _row_exists
    await engine.assert_project_registered("m-agahi/yadgar")


# ── the guard is wired into every project_id-stamping write (C6) ─────────────


@pytest.mark.parametrize("method", ["create_task_row", "create_adr_row"])
@pytest.mark.asyncio
async def test_stamping_writes_check_the_registry_before_inserting(method):
    """An unregistered project_id is REJECTED before the INSERT is issued.

    The FK alone is not the check: it fires as an opaque constraint error
    naming neither the offending value nor the call site, and by then the
    caller has lost the context in which the typo was made.

    Guarding inside the engine method rather than in the admin-op wrapper is
    what covers the two callers that reach the engine directly — ``adr_seed``
    and ``seed`` — rather than through the ``/admin`` dispatch.
    """
    conn = _FakeConn()
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)

    async def _row_exists(*, table, key_column, key_value, limit=1):
        return False

    engine.row_exists = _row_exists

    with pytest.raises(UnknownProjectError) as caught:
        await getattr(engine, method)(project_id="m-agahi/typo", title="t")
    assert caught.value.project_id == "m-agahi/typo"
    assert conn.executed == [], "the INSERT was issued despite an unknown project_id"


@pytest.mark.parametrize("method", ["create_task_row", "create_adr_row"])
@pytest.mark.asyncio
async def test_stamping_writes_proceed_for_a_registered_project(method):
    """A registered project_id passes the guard and the INSERT runs.

    The other half of "the guard cannot brick writes": once the registry is
    seeded — which the runbook orders BEFORE any agent use — the guard is
    invisible.

    Two statements are issued, not one (task 384): the guard bumps
    ``project.last_validated_at`` on a confirmed-present row before the row
    write runs. Pinned by SHAPE rather than by count so the assertion still
    says which statement is which.
    """
    conn = _FakeConn()
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)

    async def _row_exists(*, table, key_column, key_value, limit=1):
        return True

    engine.row_exists = _row_exists

    await getattr(engine, method)(project_id="m-agahi/yadgar", title="t")
    statements = [stmt for stmt, _ in conn.executed]
    inserts = [s for s in statements if s.startswith("INSERT INTO")]
    refreshes = [s for s in statements if "last_validated_at = CURRENT_TIMESTAMP" in s]
    assert len(inserts) == 1, statements
    assert len(refreshes) == 1, statements
    assert len(statements) == 2, statements


# ── task 384: the staleness refresh lives on the guard that actually runs ────


@pytest.mark.asyncio
async def test_assert_project_registered_bumps_last_validated_at():
    """The reachable guard refreshes the row's staleness clock.

    Task #88 put this bump on ``admin_exec/project_registry._ensure_project_exists_async``,
    which had no call site (and task 384 deleted). Nothing would ever have
    bumped the column: migration 005 backfills every row to CURRENT_TIMESTAMP
    at deploy and that would have been the LAST write, so on day
    ``PROJECT_STALENESS_DAYS + 1`` ``yadgar project list --stale`` would report
    EVERY project stale, permanently — the surface reporting the exact inverse
    of the truth for the rest of the deployment's life.
    """
    conn = _FakeConn()
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)

    async def _row_exists(*, table, key_column, key_value, limit=1):
        return True

    engine.row_exists = _row_exists

    await MariaStorageEngine.assert_project_registered(engine, "m-agahi/yadgar")

    assert len(conn.executed) == 1
    stmt, params = conn.executed[0]
    assert "UPDATE project" in stmt
    assert "last_validated_at = CURRENT_TIMESTAMP" in stmt
    assert params == {"key": "m-agahi/yadgar"}


@pytest.mark.asyncio
async def test_refresh_failure_cannot_break_the_registry_check():
    """A failing bump is swallowed — the guard's contract is the raise.

    The refresh is observability bolted onto a gate that ledger INSERTs depend
    on. If a transient failure on the UPDATE could propagate, adding the bump
    would have turned every ``task`` / ``adr`` write into a second thing that
    can fail for a reason unrelated to the caller.
    """
    conn = _FakeConn(raise_on_execute=RuntimeError("simulated: refresh blew up"))
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)

    async def _row_exists(*, table, key_column, key_value, limit=1):
        return True

    engine.row_exists = _row_exists

    # No raise: the check passed, so the caller must see the project as
    # registered regardless of what the refresh did.
    await MariaStorageEngine.assert_project_registered(engine, "m-agahi/yadgar")


@pytest.mark.asyncio
async def test_unregistered_project_is_not_refreshed():
    """A rejected project_id must issue no UPDATE — it has no row to stamp."""
    conn = _FakeConn()
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)

    async def _row_exists(*, table, key_column, key_value, limit=1):
        return False

    engine.row_exists = _row_exists

    with pytest.raises(UnknownProjectError):
        await MariaStorageEngine.assert_project_registered(engine, "m-agahi/typo")
    assert conn.executed == []


def test_list_project_rows_projection_omits_last_validated_at():
    """The forwarded create-gate SELECT must not name an optional column.

    ``core/server/tools/_project_registry`` forwards ``list_project_rows`` to
    answer ``assert_project_registered_for_create`` on every ``memorize`` /
    ``wiki_add``. With ``last_validated_at`` in the projection, 005's
    ``downgrade()`` makes the statement fail with MySQL 1054 and the create
    gate silently degrades to a shape check.
    """
    src = _body_source_without_docstring(MariaStorageEngine.list_project_rows)
    assert "last_validated_at" not in src


@pytest.mark.asyncio
async def test_refresh_false_checks_without_stamping():
    """``refresh=False`` runs the check and issues no UPDATE.

    The dry-run preflights (``admin_exec/identity_stamp``,
    ``admin_exec/adr_seed``) pass this so a preview reaches the apply's verdict
    without the apply's side effect — a ``--dry-run`` that writes is the defect
    ledger task 385 fixed in ``verify-hooks``, and the guard's bump (task 384)
    put a second instance of it on the preview path.
    """
    conn = _FakeConn()
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)
    seen: list[str] = []

    async def _row_exists(*, table, key_column, key_value, limit=1):
        seen.append(key_value)
        return True

    engine.row_exists = _row_exists

    await MariaStorageEngine.assert_project_registered(engine, "m-agahi/yadgar", refresh=False)

    assert seen == ["m-agahi/yadgar"], "the check itself must still run"
    assert conn.executed == [], "a preview stamped last_validated_at"


@pytest.mark.asyncio
async def test_refresh_false_still_refuses_an_unknown_project():
    """Withholding the stamp must not withhold the refusal.

    Preview/apply parity is owed on the VERDICT. If ``refresh=False`` also
    softened the check, the dry run would stop predicting the apply, which is
    the whole reason the preflight calls the guard at all (Car 19 / task 176).
    """
    conn = _FakeConn()
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)

    async def _row_exists(*, table, key_column, key_value, limit=1):
        return False

    engine.row_exists = _row_exists

    with pytest.raises(UnknownProjectError):
        await MariaStorageEngine.assert_project_registered(engine, "m-agahi/typo", refresh=False)
    assert conn.executed == []
