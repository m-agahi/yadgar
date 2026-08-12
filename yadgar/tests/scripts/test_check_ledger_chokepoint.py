"""Tests for scripts/check_ledger_chokepoint.py (Car A — D20 ledger chokepoint).

The guard AST-scans ``yadgar/**/*.py`` (tests excluded) and FAILS if any code
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
