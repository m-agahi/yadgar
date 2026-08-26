"""Car C11-#88 (task #88): project table staleness surface.

Project rows are created once and never re-validated. After 81 days of silent
drift on the canonical repo, this car makes the staleness computable:

  1. ``project`` carries a ``last_validated_at`` column (migration 005).
  2. New INSERTs stamp it via ``create_project_row``.
  3. The runtime guard ``MariaStorageEngine.assert_project_registered`` bumps
     it on every successful registry check. Task 384 re-homed the bump here
     from a standalone ``admin_exec`` guard that had no call site — where it
     would never have fired, leaving 005's backfill as the last write to the
     column and every project permanently stale from day 91.
  4. A new admin op ``list_stale_projects`` returns rows older than
     ``YADGAR_PROJECT_STALENESS_DAYS`` (default 90), plus rows whose
     ``last_validated_at`` is NULL.
  5. ``yadgar project list [--stale]`` surfaces the same set.

The migration runs on a populated table — backfilling ``CURRENT_TIMESTAMP``
into every existing row is deliberate, so the threshold does NOT trip on
day-zero after deploy. Stale drift was the SYMPTOM; the threshold is what
kills the *next* cycle of drift.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402

VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "_shared" / "storage" / "sql" / "migrations" / "versions"
)


# ── migration 005 — structural contract ─────────────────────────────────────


def _load_module_source(slug: str) -> str:
    path = VERSIONS_DIR / f"{slug}.py"
    if not path.is_file():
        pytest.fail(f"alembic revision file missing: {path}")
    return path.read_text(encoding="utf-8")


def test_migration_005_exists():
    """The migration file must exist with a 005-prefixed slug."""
    matches = list(VERSIONS_DIR.glob("005_*.py"))
    assert matches, "migration 005_*.py file missing"


def test_migration_005_chains_off_004():
    """``down_revision`` must chain to ``004_agent_pattern_model_client``.

    A 005 chained to anything else would put it at the head of a separate
    branch — ``alembic upgrade head`` would then skip it.
    """
    slugs = sorted(p.stem for p in VERSIONS_DIR.glob("005_*.py"))
    src = _load_module_source(slugs[0])
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "down_revision":
                assert node.value is not None
                assert isinstance(node.value, ast.Constant)
                assert node.value.value == "004_agent_pattern_model_client", (
                    f"005 must chain to 004_agent_pattern_model_client; got {node.value.value!r}"
                )
                return
    pytest.fail("`down_revision = '...'` assignment not found")


def test_migration_005_alters_project_table():
    """``upgrade()`` must ALTER ``project`` and backfill existing rows."""
    slugs = sorted(p.stem for p in VERSIONS_DIR.glob("005_*.py"))
    src = _load_module_source(slugs[0])
    assert "ALTER TABLE project" in src, "005 must ALTER TABLE project"
    assert "last_validated_at" in src, "005 must add last_validated_at column"
    # Backfill: existing rows treated as freshly validated NOW.
    assert "UPDATE project SET last_validated_at" in src, (
        "005 must backfill existing rows with CURRENT_TIMESTAMP "
        "so the threshold doesn't trip on day-zero"
    )


def test_migration_005_downgrade_drops_column():
    """``downgrade()`` must reverse cleanly: drop the column."""
    slugs = sorted(p.stem for p in VERSIONS_DIR.glob("005_*.py"))
    src = _load_module_source(slugs[0])
    assert "DROP COLUMN last_validated_at" in src, (
        "005 downgrade must drop last_validated_at column"
    )


# ── write path stamps last_validated_at ─────────────────────────────────────


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict | None]] = []

    async def execute(self, sql, params=None):
        self.executed.append((str(sql), params))
        return _FakeResult()


class _FakeResult:
    lastrowid = 1


class _FakeEngine:
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
    engine: Any = object.__new__(MariaStorageEngine)
    engine._engine = _FakeEngine(conn)
    return engine


@pytest.mark.asyncio
async def test_create_project_row_stamps_last_validated_at():
    """The INSERT must stamp ``last_validated_at`` so new rows are fresh by default."""
    conn = _FakeConn()
    engine = _engine_with(conn)
    await engine.create_project_row(key="m-agahi/yadgar", kind="git")
    stmt, params = conn.executed[0]
    assert "last_validated_at" in stmt, (
        f"create_project_row must stamp last_validated_at; got SQL: {stmt}"
    )
    assert "CURRENT_TIMESTAMP" in stmt, (
        f"create_project_row must stamp CURRENT_TIMESTAMP for last_validated_at; got: {stmt}"
    )


def _strip_docstring(src: str) -> str:
    """Return *src* with the leading function docstring removed.

    The assertion below is about the SQL the method ISSUES, not the prose
    explaining why the column is absent — which necessarily names it.
    """
    node = ast.parse(textwrap.dedent(src)).body[0]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:]
    return ast.unparse(node)


def test_list_project_rows_does_not_select_last_validated_at():
    """The generic READ must NOT name the column (task 384).

    ``list_project_rows`` is what ``core/server/tools/_project_registry``
    forwards to answer the create gate on every ``memorize`` / ``wiki_add``.
    Naming an optional column there couples the gate to it: after 005's
    ``downgrade()`` the SELECT fails with MySQL 1054 and the gate silently
    degrades to a shape check. ``list_stale_projects`` is the surface that
    ages rows and selects the column itself.
    """
    src = inspect.getsource(MariaStorageEngine.list_project_rows)
    assert "last_validated_at" not in _strip_docstring(src), (
        "list_project_rows must not SELECT last_validated_at — the create gate "
        "forwards this statement and must survive the column being absent"
    )


def test_list_stale_projects_selects_last_validated_at():
    """The staleness surface selects the column it filters on."""
    src = inspect.getsource(MariaStorageEngine.list_stale_projects)
    assert "last_validated_at" in src


# ── runtime guard bumps last_validated_at ───────────────────────────────────


def test_assert_project_registered_bumps_last_validated_at():
    """After the registry check passes, the staleness column must be bumped.

    Task 384: the bump lives on the guard that is actually REACHED — the
    in-engine one inside ``create_task_row`` / ``create_adr_row``. It used to
    sit on a standalone ``admin_exec`` function with zero call sites, which
    means it would never have fired: 005's day-zero backfill would have been
    the last write to the column, and ``yadgar project list --stale`` would
    have reported EVERY project stale from day 91 onward, forever.
    """
    src = inspect.getsource(MariaStorageEngine.assert_project_registered)
    assert "last_validated_at" in src, (
        "assert_project_registered must bump last_validated_at after the row_exists check"
    )
    # The bump must NEVER break the guard — wrapped so a failed UPDATE is
    # logged, not propagated. The guard's contract to its callers is the raise.
    assert "try" in src and "except" in src, (
        "the last_validated_at bump must be wrapped in try/except so it cannot "
        "break the registry check callers depend on"
    )


# ── list_stale_projects admin op ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_stale_projects_method_exists():
    """Storage method must exist; admin op dispatches to it."""
    assert hasattr(MariaStorageEngine, "list_stale_projects"), (
        "MariaStorageEngine missing list_stale_projects"
    )
    assert inspect.iscoroutinefunction(MariaStorageEngine.list_stale_projects)


def test_list_stale_projects_op_is_registered():
    """``list_stale_projects`` must be reachable over ``/admin``.

    Without a registered op the CLI has no way to read the stale set.
    """
    from yadgar.backend.admin_exec import admin_ops

    ops = admin_ops()
    assert "list_stale_projects" in ops, "list_stale_projects missing from admin_ops"


def test_list_stale_projects_threshold_filter():
    """SQL filter must use the threshold (NULL OR older-than-days)."""
    src = inspect.getsource(MariaStorageEngine.list_stale_projects)
    assert "last_validated_at" in src, "list_stale_projects must reference last_validated_at"
    assert "INTERVAL" in src, "list_stale_projects must filter by an INTERVAL on the column"
    assert "IS NULL" in src, (
        "list_stale_projects must include NULL last_validated_at rows (never-validated)"
    )


# ── settings: PROJECT_STALENESS_DAYS ────────────────────────────────────────


def test_project_staleness_days_setting_exists_with_90_default(monkeypatch):
    """Default 90 days; env override via YADGAR_PROJECT_STALENESS_DAYS."""
    monkeypatch.delenv("YADGAR_PROJECT_STALENESS_DAYS", raising=False)
    from yadgar._shared.config import get_settings
    from yadgar._shared.config.config import Settings

    get_settings.cache_clear()
    settings = Settings()
    assert settings.PROJECT_STALENESS_DAYS == 90, (
        f"PROJECT_STALENESS_DAYS default must be 90; got {settings.PROJECT_STALENESS_DAYS}"
    )


def test_project_staleness_days_setting_env_override(monkeypatch):
    """The env var YADGAR_PROJECT_STALENESS_DAYS overrides the default."""
    monkeypatch.setenv("YADGAR_PROJECT_STALENESS_DAYS", "30")
    from yadgar._shared.config.config import Settings

    settings = Settings()
    assert settings.PROJECT_STALENESS_DAYS == 30, (
        f"YADGAR_PROJECT_STALENESS_DAYS env override must be honored; got "
        f"{settings.PROJECT_STALENESS_DAYS}"
    )


# ── CLI surface ─────────────────────────────────────────────────────────────


def test_cli_project_list_subparser_registered():
    """``yadgar project list [--stale]`` must be wired up."""
    from yadgar.core.cli.project import register

    parser, sub = _make_parser()
    register(sub)
    args = parser.parse_args(["project", "list"])
    assert args.project_command == "list"
    assert hasattr(args, "stale")


def test_cli_project_list_stale_flag():
    """``yadgar project list --stale`` parses the flag."""
    from yadgar.core.cli.project import register

    parser, sub = _make_parser()
    register(sub)
    args = parser.parse_args(["project", "list", "--stale"])
    assert args.stale is True


def test_cli_project_list_no_stale_default_false():
    """Without ``--stale`` the flag is False — show ALL rows."""
    from yadgar.core.cli.project import register

    parser, sub = _make_parser()
    register(sub)
    args = parser.parse_args(["project", "list"])
    assert args.stale is False


def _make_parser():
    """Build a top-level parser matching how the CLI invokes ``register``.

    Returns the parent parser so ``parse_args`` resolves ``project`` ->
    ``list`` -- ``register`` itself expects to be handed the parent's
    ``add_subparsers(...)`` action (a ``_SubParsersAction``), so the
    helper threads both back.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="yadgar")
    sub = parser.add_subparsers(dest="top_command")
    return parser, sub


# ── CLI smoke: list projects path is wired to admin ops ─────────────────────


def test_cmd_project_list_dispatches_to_list_project_rows(monkeypatch):
    """Without ``--stale`` the CLI calls ``list_project_rows`` (the existing op)."""
    import argparse

    from yadgar.core import forward as forward_mod
    from yadgar.core.cli import project as cli_project

    captured: dict = {}

    def _fake_forward(op, payload, timeout_s):
        captured["op"] = op
        captured["payload"] = payload
        return {"rows": []}

    monkeypatch.setattr(forward_mod, "_forward_admin", _fake_forward)

    args = argparse.Namespace(stale=False)
    rc = cli_project.cmd_project_list(args)
    assert rc == 0
    assert captured["op"] == "list_project_rows"


def test_cmd_project_list_stale_dispatches_to_list_stale_projects(monkeypatch):
    """With ``--stale`` the CLI calls ``list_stale_projects`` (the new op)."""
    import argparse

    from yadgar.core import forward as forward_mod
    from yadgar.core.cli import project as cli_project

    captured: dict = {}

    def _fake_forward(op, payload, timeout_s):
        captured["op"] = op
        captured["payload"] = payload
        return {"projects": [], "threshold_days": 90, "count": 0}

    monkeypatch.setattr(forward_mod, "_forward_admin", _fake_forward)

    args = argparse.Namespace(stale=True)
    rc = cli_project.cmd_project_list(args)
    assert rc == 0
    assert captured["op"] == "list_stale_projects"
