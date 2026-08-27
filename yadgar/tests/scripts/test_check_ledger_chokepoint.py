"""Tests for scripts/check_ledger_chokepoint.py (Car A — D20 ledger chokepoint).

The guard AST-scans ``yadgar/**/*.py`` and ``scripts/**/*.py`` (its ``SCAN_DIRS``;
tests excluded) and FAILS if any code
outside ``MariaStorageEngine`` and outside the explicit allowlist references
a ledger table name (``task``, ``adr``, ``agent_pattern``, ``agent_discipline``,
``task_blocked_by``, ``adr_supersedes``, ``agent_pattern_composes``) via
raw SQL string literals.

Run:
  uv run pytest yadgar/tests/scripts/test_check_ledger_chokepoint.py
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent.parent / "scripts" / "check_ledger_chokepoint.py"


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _make_root(tmp_path: Path, src: str, name: str = "mod.py") -> Path:
    """Write ``src`` into a scannable root dir and return that root."""
    root = tmp_path / "pkg"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(textwrap.dedent(src))
    return root


# ---------------------------------------------------------------------------
# Violations → exit 1
# ---------------------------------------------------------------------------


def test_select_from_task_outside_engine_fails(tmp_path):
    """A raw ``text("SELECT ... FROM task")`` outside MariaStorageEngine → exit 1."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def read_things():
            return text("SELECT id, title FROM task WHERE id = 1")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "task" in res.stdout


def test_insert_into_task_outside_engine_fails(tmp_path):
    """A ``text("INSERT INTO task ...")`` outside MariaStorageEngine → exit 1."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def write_things():
            return text("INSERT INTO task (id, title) VALUES (1, 'x')")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "task" in res.stdout


def test_select_from_adr_outside_engine_fails(tmp_path):
    """``text("FROM adr")`` outside the engine is a chokepoint violation."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def fetch_adr():
            return text("SELECT id, title FROM adr ORDER BY id DESC")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "adr" in res.stdout


def test_join_table_reference_outside_engine_fails(tmp_path):
    """``text("FROM task_blocked_by")`` outside the engine is a chokepoint violation."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def fetch_blocks():
            return text("SELECT * FROM task_blocked_by")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "task_blocked_by" in res.stdout


def test_agent_pattern_outside_engine_fails(tmp_path):
    """``agent_pattern`` is a ledger table — same rule."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def fetch_patterns():
            return text("SELECT name FROM agent_pattern WHERE status='active'")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout


# ---------------------------------------------------------------------------
# Aliased imports — the guard must resolve the alias, not the surface name
# ---------------------------------------------------------------------------


def test_aliased_name_import_outside_engine_fails(tmp_path):
    """``from sqlalchemy import text as sql`` then ``sql(...)`` must still be caught.

    The checker matches on the literal call name (``ast.Name.id`` /
    ``ast.Attribute.attr``). An aliased import binds the SQL-exec function to
    a different surface name, which — without alias resolution — slips past
    the ``_SQL_EXEC_FUNCS`` frozenset entirely.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text as sql

        def read_things():
            return sql("SELECT id, title FROM task WHERE id = 1")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "task" in res.stdout


def test_aliased_module_import_attribute_outside_engine_fails(tmp_path):
    """``import sqlalchemy as sa`` then ``sa.text(...)`` — must not regress.

    This form was already caught pre-fix (the Attribute's ``.attr`` is
    literally ``text`` regardless of the module alias). Kept as an explicit
    regression guard so alias resolution for ``ast.Import`` doesn't
    accidentally break the already-working Attribute path.
    """
    root = _make_root(
        tmp_path,
        """\
        import sqlalchemy as sa

        def read_things():
            return sa.text("SELECT id, title FROM task WHERE id = 1")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "task" in res.stdout


def test_aliased_submodule_import_from_outside_engine_fails(tmp_path):
    """``from sqlalchemy.sql import text as t`` then ``t(...)`` must be caught."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy.sql import text as t

        def read_things():
            return t("SELECT id, title FROM task WHERE id = 1")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "task" in res.stdout


def test_aliased_submodule_import_attribute_outside_engine_fails(tmp_path):
    """``import sqlalchemy.sql as s`` then ``s.text(...)`` must be caught."""
    root = _make_root(
        tmp_path,
        """\
        import sqlalchemy.sql as s

        def read_things():
            return s.text("SELECT id, title FROM task WHERE id = 1")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "task" in res.stdout


def test_aliased_import_inside_engine_still_passes(tmp_path):
    """Alias resolution must not start flagging the sanctioned surface.

    ``sql(...)`` resolves to ``text`` and mentions a ledger table, but it
    sits inside ``MariaStorageEngine`` — still exempt.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text as sql

        class MariaStorageEngine:
            def list_task_rows(self):
                return sql("SELECT id, title FROM task WHERE project_id = 'p'")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_unrelated_aliased_import_not_flagged(tmp_path):
    """Aliasing a non-SQL function must not become a violation.

    ``from os import getenv as env_text`` resolves ``env_text`` to its
    origin symbol ``getenv``, which is not in ``_SQL_EXEC_FUNCS`` — so the
    call is skipped even though its string arg looks like SQL.
    """
    root = _make_root(
        tmp_path,
        """\
        from os import getenv as env_text

        def read_things():
            return env_text("SELECT id, title FROM task WHERE id = 1")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


# ---------------------------------------------------------------------------
# Anti-vacuity: the checker must still resolve real, non-trivial call sites
# ---------------------------------------------------------------------------


def test_checker_resolves_nontrivial_violation_count_on_repo_tree():
    """Guard against a future regex/AST change silently going no-op.

    Runs the checker against a small planted tree with a mix of aliased and
    unaliased violations, several of which are only caught by alias
    resolution. If a future change to the matching logic breaks resolution
    (e.g. reverting to surface-name-only matching, or breaking on some AST
    shape), this count drops and the test fails — it is not enough for the
    checker to merely exit non-zero once.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "pkg"
        root.mkdir()
        (root / "a.py").write_text(
            textwrap.dedent(
                """\
                from sqlalchemy import text

                def unaliased():
                    return text("SELECT id FROM task")
                """
            )
        )
        (root / "b.py").write_text(
            textwrap.dedent(
                """\
                from sqlalchemy import text as sql

                def aliased_name():
                    return sql("SELECT id FROM adr")
                """
            )
        )
        (root / "c.py").write_text(
            textwrap.dedent(
                """\
                import sqlalchemy.sql as s

                def aliased_module():
                    return s.text("SELECT id FROM task_blocked_by")
                """
            )
        )
        (root / "d.py").write_text(
            textwrap.dedent(
                """\
                from sqlalchemy.sql import text as t

                def aliased_submodule():
                    return t("SELECT id FROM agent_pattern")
                """
            )
        )
        res = run_script("--root", str(root), "--list-all")
        assert res.returncode == 1, res.stdout
        lines = [line for line in res.stdout.splitlines() if "ledger table" in line]
        assert len(lines) >= 4, res.stdout


# ---------------------------------------------------------------------------
# Clean forms → exit 0
# ---------------------------------------------------------------------------


def test_select_from_task_inside_engine_passes(tmp_path):
    """``text("SELECT ... FROM task")`` INSIDE ``MariaStorageEngine`` → exit 0.

    The guard's INTENT is one chokepoint, and the VEHICLE is
    ``MariaStorageEngine``'s ledger methods. SQL inside the engine is the
    sanctioned surface.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        class MariaStorageEngine:
            def list_task_rows(self):
                return text("SELECT id, title FROM task WHERE project_id = 'p'")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_insert_inside_engine_passes(tmp_path):
    """``text("INSERT INTO task")`` INSIDE the engine is the sanctioned surface."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        class MariaStorageEngine:
            def create_task_row(self):
                return text(
                    "INSERT INTO task (project_id, title) VALUES ('p', 'x')"
                )
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_no_table_reference_passes(tmp_path):
    """Code with no ledger table mentions → exit 0."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def unrelated():
            return text("SELECT 1 FROM config WHERE `key` = 'k'")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_string_literal_in_docstring_not_flagged(tmp_path):
    """Table-name inside a docstring is a Constant str (NOT a SQL statement).

    The guard checks whether the literal is wrapped in ``text(...)`` /
    ``execute(...)`` — a docstring is neither. Strings that look like
    SQL but are NOT passed to an SQL-execution function are out of scope.
    """
    root = _make_root(
        tmp_path,
        '''\
        def documented():
            """Ledger tables are: task, adr, agent_pattern, agent_discipline."""
            return None
        ''',
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def _import_checker_module():
    """Load check_ledger_chokepoint.py as an in-process module (not subprocess).

    Needed to monkeypatch ``_REPO_ROOT`` for the portability test below —
    a subprocess invocation can't have its module globals patched from the
    test process.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_ledger_chokepoint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: the module defines a
    # `@dataclass(frozen=True)` (Violation), and dataclass's own forward-ref
    # resolution looks the module up via `sys.modules.get(cls.__module__)` —
    # an unregistered module makes that lookup return None and crash.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_allowlist_key_relative_path_survives_different_repo_root(tmp_path, monkeypatch):
    """Repo-relative allowlist entries must match regardless of ``_REPO_ROOT``.

    This is what makes a committed entry like
    ``yadgar/backend/admin_exec/adr_seed.py:364:...`` keep matching across a
    checkout at a different absolute location — including a git worktree of
    this same repo, whose absolute path prefix differs from the main
    checkout. Simulated here by monkeypatching ``_REPO_ROOT`` to a fake repo
    root under ``tmp_path`` and confirming a *repo-relative* allowlist entry
    still suppresses the violation found under that fake root.
    """
    module = _import_checker_module()
    fake_repo_root = tmp_path / "fake_repo"
    pkg_dir = fake_repo_root / "yadgar" / "backend" / "admin_exec"
    pkg_dir.mkdir(parents=True)
    target = pkg_dir / "mod.py"
    target.write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import text

            def read_things():
                return text("SELECT id FROM adr")
            """
        )
    )
    monkeypatch.setattr(module, "_REPO_ROOT", fake_repo_root)

    # Without the allowlist: the violation is found (proves the fixture is real).
    unfiltered = module.scan(fake_repo_root / "yadgar", allowed=set())
    assert len(unfiltered) == 1

    # A repo-relative key (no fake_repo_root prefix) suppresses it.
    allowed = {("yadgar/backend/admin_exec/mod.py", 4)}
    filtered = module.scan(fake_repo_root / "yadgar", allowed)
    assert filtered == []


def test_allowlisted_file_passes(tmp_path):
    """An entry on the allowlist suppresses the violation for that exact line.

    The allowlist is sourced from a file of ``path:lineno:reason`` lines; a
    planted allowlist can grant clemency to a documented pre-existing
    violation.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def legitimate_violation():
            # pre-existing — covered by allowlist
            return text("SELECT id FROM task")
        """,
    )
    allowlist = root / "ledger_chokepoint.allowlist"
    allowlist.write_text(
        textwrap.dedent(
            f"""\
            {root}/mod.py:5: pre-existing read in audit arm — covered by car H cross-engine check
            """
        )
    )
    res = run_script("--root", str(root), "--allowlist", str(allowlist))
    assert res.returncode == 0, res.stdout


# ---------------------------------------------------------------------------
# Scanning scope
# ---------------------------------------------------------------------------


def test_tests_dir_excluded(tmp_path):
    """A violating file under a tests/ dir is excluded from the scan."""
    tests_dir = tmp_path / "pkg" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_x.py").write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import text

            def fixture():
                return text("SELECT id FROM task")
            """
        )
    )
    res = run_script("--root", str(tmp_path / "pkg"))
    assert res.returncode == 0, res.stdout


def test_list_all_flag_outputs_all_violations(tmp_path):
    """``--list-all`` is the same output as default failure output."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def a():
            return text("SELECT id FROM task")

        def b():
            return text("SELECT id FROM adr")
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 1
    assert "task" in res.stdout
    assert "adr" in res.stdout


def test_information_schema_metadata_read_not_flagged(tmp_path):
    """Car B (task #201): ``FROM information_schema.<ledger_table>`` is metadata, not a row access.

    ``adr_seed._read_next_adr_id`` reads
    ``SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_NAME = 'adr'``
    — that is a schema-metadata lookup, not a row access on the ``adr`` ledger
    table. The chokepoint's intent is to catch row reads on the ledger; the
    bare-substring matcher over-matches and would block this legitimate read.
    The fix excludes matches where the table is referenced via the
    ``information_schema.`` schema prefix — same query, no row data touched.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def read_next_adr_id():
            return text(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'adr'"
            )
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 0, res.stdout
    assert "information_schema" not in res.stdout or "adr" not in res.stdout


def test_information_schema_other_table_not_flagged(tmp_path):
    """Same exclusion must apply to every ledger table, not just ``adr``."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def read_metadata():
            return text("SELECT * FROM information_schema.COLUMNS WHERE TABLE_NAME = 'task'")
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 0, res.stdout


def test_real_adr_row_read_still_flagged(tmp_path):
    """After excluding information_schema reads, a real ``FROM adr`` must still trip.

    The exclusion is targeted at the schema prefix — a row read on ``adr`` is
    unchanged and must still be caught.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def read_adr():
            return text("SELECT id, title FROM adr ORDER BY id DESC")
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "adr" in res.stdout


# ---------------------------------------------------------------------------
# Car Q: positional matching replaces Car B's whole-string exemption
#
# Car B (task #201) fixed a real false positive by returning [] for the WHOLE
# literal whenever ``FROM information_schema.`` appeared anywhere in it. That
# made any literal MIXING a metadata read with a real ledger row access
# invisible to the guard. The matcher now scores each TABLE POSITION on its
# own qualifier, so the metadata read is skipped and the row access is not.
# ---------------------------------------------------------------------------


def _violation_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if "ledger table" in line]


def test_metadata_read_plus_ledger_write_in_one_literal_is_flagged(tmp_path):
    """THE case Car B's whole-string exemption created.

    One literal, two statements: a legitimate ``information_schema`` catalog
    read AND a real ``UPDATE adr SET ...`` row write. Under the exemption the
    presence of the first bought clemency for the second and the guard scored
    ZERO violations. Positional matching skips only the metadata POSITION.

    Asserts the exact violation count and the table named — a bare
    ``returncode == 1`` would also pass if the guard regressed to flagging the
    ``'adr'`` string VALUE in the WHERE clause, which is the very false
    positive Car B was right to remove.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def sneaky():
            return text(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                "WHERE TABLE_NAME='adr'; "
                "UPDATE adr SET tier='binding' WHERE id=:id"
            )
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 1, res.stdout
    lines = _violation_lines(res.stdout)
    assert len(lines) == 1, res.stdout
    assert "`adr`" in lines[0], res.stdout


def test_metadata_read_plus_ledger_join_in_one_literal_is_flagged(tmp_path):
    """A catalog read JOINed to a ledger table is still a ledger row access.

    Same shape as above in a single statement rather than two — the
    ``JOIN adr`` position carries no ``information_schema.`` qualifier.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def joined():
            return text(
                "SELECT c.COLUMN_NAME, a.title "
                "FROM information_schema.COLUMNS c "
                "JOIN adr a ON a.id = c.ORDINAL_POSITION"
            )
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 1, res.stdout
    lines = _violation_lines(res.stdout)
    assert len(lines) == 1, res.stdout
    assert "`adr`" in lines[0], res.stdout


def test_metadata_only_read_scores_zero_violations(tmp_path):
    """The Car B false positive stays fixed — and stays fixed for the right reason.

    Stronger than ``test_information_schema_metadata_read_not_flagged``'s
    ``"information_schema" not in stdout or "adr" not in stdout``, which is
    satisfied by any stdout that merely omits one of the two words. This
    pins the violation COUNT at zero.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def read_next_adr_id():
            return text(
                "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'adr'"
            )
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 0, res.stdout
    assert _violation_lines(res.stdout) == [], res.stdout


def test_ledger_table_as_string_value_not_flagged(tmp_path):
    """A ledger table name appearing only as a VALUE is not a row access.

    This is the general form of the Car B false positive, with no
    ``information_schema`` involved at all: the old bare-token matcher
    flagged ``'adr'`` anywhere in the string, including inside quotes.
    """
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def label():
            return text("SELECT 'adr' AS kind, id FROM config WHERE id = 1")
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 0, res.stdout
    assert _violation_lines(res.stdout) == [], res.stdout


def test_ledger_table_as_column_name_not_flagged(tmp_path):
    """A COLUMN named after a ledger table is not a table reference."""
    root = _make_root(
        tmp_path,
        """\
        from sqlalchemy import text

        def insert_elsewhere():
            return text("INSERT INTO audit_log (id, task) VALUES (1, 2)")
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 0, res.stdout
    assert _violation_lines(res.stdout) == [], res.stdout


@pytest.mark.parametrize(
    ("sql", "table"),
    [
        ("SELECT id FROM task WHERE id = 1", "task"),
        ("SELECT * FROM t JOIN task_blocked_by tb ON tb.task_id = t.id", "task_blocked_by"),
        ("INSERT INTO adr (id) VALUES (1)", "adr"),
        ("REPLACE INTO agent_pattern (name) VALUES ('x')", "agent_pattern"),
        ("UPDATE adr SET tier = 'binding' WHERE id = 1", "adr"),
        ("DELETE FROM adr_supersedes WHERE adr_id = 1", "adr_supersedes"),
        ("TRUNCATE TABLE agent_discipline", "agent_discipline"),
        ("TRUNCATE task", "task"),
        ("SELECT id FROM `task` WHERE id = 1", "task"),
        ("SELECT id FROM ledgerdb.task WHERE id = 1", "task"),
        ("SELECT id FROM `ledgerdb`.`adr` WHERE id = 1", "adr"),
    ],
)
def test_every_table_introducer_shape_is_caught(tmp_path, sql, table):
    """Positional matching under-detects where bare-token over-detected.

    A guard that silently stops matching is worse than one that over-matches,
    so every SQL shape that puts a name in a table position gets a case here:
    ``FROM``, ``JOIN``, ``INSERT INTO``, ``REPLACE INTO``, ``UPDATE ... SET``,
    ``DELETE FROM``, ``TRUNCATE TABLE``, bare ``TRUNCATE``, back-quoted, and
    schema-qualified (which must NOT be confused with the
    ``information_schema.`` metadata skip).
    """
    root = _make_root(
        tmp_path,
        f"""\
        from sqlalchemy import text

        def q():
            return text({sql!r})
        """,
    )
    res = run_script("--root", str(root), "--list-all")
    assert res.returncode == 1, res.stdout
    assert f"`{table}`" in res.stdout, res.stdout


# ---------------------------------------------------------------------------
# Default root set — ledger task 394 widened it from ``yadgar/`` to
# ``yadgar/`` + ``scripts/``.
#
# These tests deliberately do NOT pass ``--root``. A test that plants a
# violation under ``tmp_path/scripts`` and then points ``--root`` at it proves
# only that the scanner accepts an arbitrary root, which it always did — it
# stays green on the pre-394 ``yadgar/``-only default and so proves nothing
# about the widening. Monkeypatching ``_REPO_ROOT`` onto a fixture repo and
# calling ``scan()`` with no root is what exercises the DEFAULT set.
# ---------------------------------------------------------------------------


def _fixture_repo(tmp_path: Path, *, where: str, body: str) -> Path:
    """Build a fake repo root with ``yadgar/`` + ``scripts/`` and plant ``body``.

    ``where`` is the repo-relative directory the violating module goes into.
    Both scanned dirs are always created so ``default_roots()`` resolves the
    same shape it does in the real checkout.
    """
    repo = tmp_path / "fake_repo"
    (repo / "yadgar").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    target_dir = repo / where
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "mod.py").write_text(textwrap.dedent(body))
    return repo


_RAW_LEDGER_WRITE = """\
from sqlalchemy import text

def seed_rows(conn):
    return conn.execute(text("UPDATE adr SET tier = 'binding' WHERE id = 1"))
"""


def test_default_scan_catches_raw_ledger_write_under_scripts(tmp_path, monkeypatch):
    """A raw ``UPDATE adr`` in an operator script is caught with NO ``--root``.

    This is the regression that ledger task 394 closes: ``scripts/`` holds
    seed and migration helpers that write against the live store, and the
    scan skipped the directory entirely, so a D20 bypass there was invisible
    to the gate whose whole job is to prevent one.
    """
    module = _import_checker_module()
    repo = _fixture_repo(tmp_path, where="scripts", body=_RAW_LEDGER_WRITE)
    monkeypatch.setattr(module, "_REPO_ROOT", repo)

    violations = module.scan()  # no root → default set

    assert [v.table for v in violations] == ["adr"], violations
    assert violations[0].source_file.parent.name == "scripts"


def test_default_scan_still_catches_violations_under_yadgar(tmp_path, monkeypatch):
    """Widening the default set must not drop the original ``yadgar/`` root."""
    module = _import_checker_module()
    repo = _fixture_repo(tmp_path, where="yadgar/backend", body=_RAW_LEDGER_WRITE)
    monkeypatch.setattr(module, "_REPO_ROOT", repo)

    violations = module.scan()

    assert [v.table for v in violations] == ["adr"], violations
    assert violations[0].source_file.parent.name == "backend"


def test_explicit_root_replaces_the_default_set(tmp_path, monkeypatch):
    """``--root``/``scan(root=...)`` scans THAT dir only — never additively.

    Every pre-394 test passes a single root and asserts on exactly that tree.
    If the override became additive, a fixture root would suddenly be scanned
    alongside the real repo and those assertions would break for a reason
    having nothing to do with the fixture.
    """
    module = _import_checker_module()
    repo = _fixture_repo(tmp_path, where="scripts", body=_RAW_LEDGER_WRITE)
    monkeypatch.setattr(module, "_REPO_ROOT", repo)

    # Point at the OTHER default dir: the scripts/ violation must not appear.
    assert module.scan(repo / "yadgar") == []
    # ...and it is genuinely there when that root is named.
    assert len(module.scan(repo / "scripts")) == 1


def test_scan_dirs_names_scripts(tmp_path, monkeypatch):
    """``SCAN_DIRS`` is the contract; a missing dir is skipped, not a crash."""
    module = _import_checker_module()
    assert "scripts" in module.SCAN_DIRS
    assert "yadgar" in module.SCAN_DIRS

    partial = tmp_path / "partial_repo"
    (partial / "yadgar").mkdir(parents=True)  # no scripts/ dir at all
    monkeypatch.setattr(module, "_REPO_ROOT", partial)
    assert [p.name for p in module.default_roots()] == ["yadgar"]
