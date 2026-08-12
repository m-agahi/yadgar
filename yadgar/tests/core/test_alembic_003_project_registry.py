"""alembic regression: ``003_project_registry`` revision file shape.

§16.5 of the master plan. The revision file is created by Car A0 of the
0047 spine train; this test pins its STRUCTURAL contract so a
refactor cannot silently desync the file from the table schema or
from the §16.5 FK chain.

The revision chains ``down_revision = "002_ledger_tables"`` (Car A's
revision, not yet at HEAD when this car merges — alembic only
verifies chain consistency, does not run on a fresh DB without 002).

Tests pin four properties separately so a regression localises:

  * Revision ID is exactly ``003_project_registry``.
  * Down-revision chains to ``002_ledger_tables``.
  * ``upgrade()`` calls ``create_table("project", ...)`` AND
    ``create_foreign_key(...)`` for ``task`` AND ``adr``.
  * ``downgrade()`` reverses cleanly — drops FKs then drops the table.

NO alembic runtime: the test parses the source AST so it can run on
the yadgar-ci image (which does not bake the ``sql`` extra) and on
any host where the migration file would not be importable directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "_shared" / "storage" / "sql" / "migrations" / "versions"
)


# ── helpers ───────────────────────────────────────────────────────────────


def _load_module_source(slug: str) -> str:
    """Read the source of an alembic revision file by slug.

    The path is constructed from VERSIONS_DIR; no alembic import is
    needed because the module is parsed as plain text.
    """
    path = VERSIONS_DIR / f"{slug}.py"
    if not path.is_file():
        pytest.fail(f"alembic revision file missing: {path}")
    return path.read_text(encoding="utf-8")


def _parse_module(slug: str) -> ast.Module:
    return ast.parse(_load_module_source(slug))


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _call_string_args(call: ast.Call) -> list[str]:
    """Collect positional string arguments of an ``ast.Call`` as plain strings.

    Handles both bare string literals (``create_table("project")``) and
    Name references (``create_table(TABLE_NAME)``). Anything else
    (f-strings, calls, etc.) is dropped — those tests should not need
    them.
    """
    out: list[str] = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value)
        elif isinstance(arg, ast.Name):
            out.append(arg.id)
    return out


def _resolve_string_constants(tree: ast.Module) -> dict[str, str]:
    """Return a ``{Name: string_value}`` map of module-level string constants.

    Alembic revisions frequently bind the table name to a module-level
    constant (``TABLE_NAME = "project"``) so a refactor cannot drift
    between the upgrade and downgrade halves. Tests that need to match
    the resolved value rather than the constant name call this first.

    Handles both annotated (``TABLE_NAME: str = "project"``) and bare
    (``TABLE_NAME = "project"``) assignments.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name):
                target = tgt.id
                value = node.value
        if target is not None and value is not None and isinstance(value, ast.Constant):
            if isinstance(value.value, str):
                out[target] = value.value
    return out


# ── revision metadata ─────────────────────────────────────────────────────


def test_revision_id_is_003_project_registry():
    """The module-level ``revision`` literal must be exactly ``003_project_registry``."""
    tree = _parse_module("003_project_registry")
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "revision":
                assert node.value is not None
                assert isinstance(node.value, ast.Constant)
                assert node.value.value == "003_project_registry"
                return
    pytest.fail("`revision = '...'` assignment not found")


def test_down_revision_chains_to_002_ledger_tables():
    """``down_revision`` must chain to Car A's ``002_ledger_tables`` (§16.5).

    A ``down_revision`` of ``None`` would put 003 at the head of an
    independent chain — and ``upgrade head`` on the production DB
    would run only this revision, skipping 002, and the FK references
    in 003 would fail (no ``task.project_id`` column yet).
    """
    tree = _parse_module("003_project_registry")
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "down_revision":
                assert node.value is not None
                # Str-annotated value: ``"002_ledger_tables"``
                if isinstance(node.value, ast.Constant):
                    assert node.value.value == "002_ledger_tables"
                    return
                pytest.fail(
                    f"down_revision must be the literal '002_ledger_tables'; got {ast.dump(node.value)}"
                )
    pytest.fail("`down_revision = '...'` assignment not found")


# ── upgrade() shape ───────────────────────────────────────────────────────


def test_upgrade_creates_project_table():
    """``upgrade()`` must call ``op.create_table(..., 'project', ...)``.

    A typo or rename (e.g. ``op.create_table("projects")``) would leave
    the registry hidden from the runtime FK on ``task.project_id``,
    silently breaking the typo guard the registry exists to provide.

    Accepts both ``op.create_table("project", ...)`` and
    ``op.create_table(TABLE_NAME, ...)`` — many car-A revisions use a
    module-level constant rather than a literal.
    """
    tree = _parse_module("003_project_registry")
    fn = _find_function(tree, "upgrade")
    assert fn is not None, "upgrade() not defined"

    # Resolve any module-level string constants that could be passed in
    # place of a literal (e.g. ``TABLE_NAME = "project"``).
    name_constants = _resolve_string_constants(tree)

    create_table_calls: list[list[str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            # Match ``op.create_table`` or ``create_table`` (depending on import shape).
            is_op_create = isinstance(func, ast.Attribute) and func.attr == "create_table"
            if is_op_create:
                args = _call_string_args(node)
                # Substitute resolved Name constants for their literal value.
                resolved = [name_constants.get(a, a) for a in args]
                if resolved:
                    create_table_calls.append(resolved)
    assert "project" in {c[0] for c in create_table_calls}, (
        f"upgrade() must call op.create_table(...,'project',...); "
        f"saw first-arg values: {[c[0] for c in create_table_calls]}"
    )


def test_upgrade_adds_fk_on_task_project_id():
    """``upgrade()`` must call ``op.create_foreign_key(..., 'task', 'project', ...)``.

    The FK is what makes the typo guard structural (§16.5) — a
    missing ``fk_task_project`` would leave the registry unwired.
    """
    tree = _parse_module("003_project_registry")
    fn = _find_function(tree, "upgrade")
    assert fn is not None, "upgrade() not defined"

    fk_calls: list[list[str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "create_foreign_key":
                args = _call_string_args(node)
                fk_calls.append(args)
    # At least one FK referencing ``task`` AND one referencing ``adr``.
    assert any("task" in c for c in fk_calls), (
        f"upgrade() must call op.create_foreign_key(...,'task',...); saw FK calls: {fk_calls}"
    )
    assert any("adr" in c for c in fk_calls), (
        f"upgrade() must call op.create_foreign_key(...,'adr',...); saw FK calls: {fk_calls}"
    )


def test_project_table_has_required_columns():
    """The ``project`` table must carry the §16.5 columns.

    Columns (per the master plan):
      key            VARCHAR(256) PRIMARY KEY
      display_name   VARCHAR(64)  NULL
      kind           ENUM('git','local') NOT NULL
      remote_url     VARCHAR(512) NULL
      created_at     DATETIME NOT NULL
    """
    tree = _parse_module("003_project_registry")
    fn = _find_function(tree, "upgrade")
    assert fn is not None, "upgrade() not defined"

    # Find the call ``op.create_table("project", ...)`` and collect its
    # column names from the ``sa.Column("name", ...)`` positional args.
    required = {"key", "display_name", "kind", "remote_url", "created_at"}
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "create_table":
                args = _call_string_args(node)
                if args and args[0] in {"project", "TABLE_NAME"}:
                    # Walk all Column() calls nested under this create_table.
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            sub_func = sub.func
                            is_column = (
                                isinstance(sub_func, ast.Attribute) and sub_func.attr == "Column"
                            )
                            if is_column:
                                col_args = _call_string_args(sub)
                                if col_args:
                                    found.add(col_args[0])
    missing = required - found
    assert not missing, (
        f"project table missing required columns: {sorted(missing)}; found: {sorted(found)}"
    )


# ── downgrade() shape ─────────────────────────────────────────────────────


def test_downgrade_drops_fk_then_table():
    """``downgrade()`` must reverse cleanly: drop FKs before the table.

    Dropping the table while FKs reference it would be a DDL error on
    a populated DB; ordering matters.
    """
    tree = _parse_module("003_project_registry")
    fn = _find_function(tree, "downgrade")
    assert fn is not None, "downgrade() not defined"

    drop_order: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {
                "drop_constraint",
                "drop_table",
            }:
                drop_order.append(func.attr)

    # Index of drop_table must come AFTER any drop_constraints.
    if "drop_table" in drop_order:
        drop_table_idx = drop_order.index("drop_table")
        last_constraint_idx = max(
            (i for i, kind in enumerate(drop_order) if kind == "drop_constraint"),
            default=-1,
        )
        assert last_constraint_idx < drop_table_idx, (
            f"downgrade() must drop constraints BEFORE the table; order: {drop_order}"
        )


# ── C6 (#17): the FK's two sides must agree on width ──────────────────────


def test_project_key_width_matches_the_referencing_columns():
    """``project.key`` and ``task/adr.project_id`` must be the SAME width.

    A FK across mismatched VARCHAR widths is a latent truncation bug that no
    other test catches: MySQL accepts the constraint, and the disagreement
    only surfaces when a key long enough to be truncated on one side is
    written. C6 (#17) set both to 256 — ADR-0202's slug cap, reused for the
    identity columns so the schema carries one number rather than two
    adjacent ones a later reader would try to reconcile.

    Read from the SOURCE of both revisions rather than from a rendered DDL,
    because the assertion is about the two files agreeing with each other.
    """
    registry_src = _load_module_source("003_project_registry")
    ledger_src = _load_module_source("002_ledger_tables")

    assert 'sa.Column("key", sa.String(length=256)' in registry_src, (
        "003 must size project.key at VARCHAR(256)"
    )
    assert "_PROJECT_ID = sa.String(length=256)" in ledger_src, (
        "002 must size task/adr.project_id at VARCHAR(256) to match project.key"
    )
    assert "_SLUG = sa.String(length=256)" in ledger_src, (
        "002 must size the body_slug columns at ADR-0202's 256-char slug cap"
    )
