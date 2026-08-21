"""Tests for yadgar/cli/context.py — lightweight context query subcommand.

Wave 3 coverage: yadgar/cli/context.py (~40 stmts, 0% pre-wave).
Strategy: mock StorageEngine._q at boundary. Test register() parser wiring
and cmd_context branches (hot+anchored, empty, exception).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yadgar.core.cli.context import cmd_context, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> SimpleNamespace:
    defaults = {"directory": "/home/user/project", "db_path": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_storage_mock(hot_rows=None, anchored_rows=None, raise_on_q=False):
    """Return a mock StorageEngine whose _q dispatches to hot or anchored list."""
    mock_storage = MagicMock()
    if raise_on_q:
        mock_storage._q.side_effect = Exception("db error")
    else:
        hot = hot_rows if hot_rows is not None else []
        anchored = anchored_rows if anchored_rows is not None else []

        call_count = [0]

        def _q_side_effect(query, params=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return hot
            return anchored

        mock_storage._q.side_effect = _q_side_effect
    return mock_storage


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_creates_context_subparser(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["context", "/proj"])
        assert args.directory == "/proj"

    def test_db_path_flag(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["context", "/proj", "--db-path", "/tmp/db"])
        assert args.db_path == "/tmp/db"


# ---------------------------------------------------------------------------
# cmd_context
# ---------------------------------------------------------------------------


class TestCmdContext:
    def test_prints_hot_memories(self, capsys):
        hot = [{"content": "important note about project", "heat": 9.5}]
        mock_storage = _make_storage_mock(hot_rows=hot, anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        out = capsys.readouterr().out
        assert "important note about project" in out

    def test_prints_anchored_memories(self, capsys):
        anchored = [{"content": "critical fact about system"}]
        mock_storage = _make_storage_mock(hot_rows=[], anchored_rows=anchored)
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        out = capsys.readouterr().out
        assert "critical fact about system" in out

    def test_empty_results_no_output(self, capsys):
        mock_storage = _make_storage_mock(hot_rows=[], anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_exception_returns_silently(self, capsys):
        mock_storage = _make_storage_mock(raise_on_q=True)
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        # No output on stdout — stdout stays the clean machine-readable
        # payload the hook parses. No exception either.
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_exception_notes_reason_on_stderr(self, capsys):
        """Car 5 item 2 (second sub-defect): before this, a DB-access
        failure (e.g. the host CLI unable to reach the database — the same
        root cause as item 3's `yadgar stats` failure) made `context`
        return silently with ZERO explanation anywhere, which is what "yadgar
        context <dir> returns NO context" meant in practice — an operator
        had nothing to go on. stdout must stay clean (still asserted above),
        but stderr should carry a one-line reason so the failure isn't
        completely invisible."""
        mock_storage = _make_storage_mock(raise_on_q=True)
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        err = capsys.readouterr().err
        assert "db error" in err  # the underlying exception message surfaces

    def test_storage_closed_after_query(self, capsys):
        mock_storage = _make_storage_mock(hot_rows=[], anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        mock_storage.close.assert_called_once()

    def test_storage_closed_even_on_exception(self, capsys):
        mock_storage = _make_storage_mock(raise_on_q=True)
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        mock_storage.close.assert_called_once()

    def test_long_content_truncated(self, capsys):
        long_content = "x" * 300
        hot = [{"content": long_content, "heat": 5.0}]
        mock_storage = _make_storage_mock(hot_rows=hot, anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        out = capsys.readouterr().out
        # Should truncate at 200 chars + "..."
        assert "..." in out

    def test_directory_shown_in_footer(self, capsys):
        hot = [{"content": "note", "heat": 1.0}]
        mock_storage = _make_storage_mock(hot_rows=hot, anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args(directory="/my/project"))
        out = capsys.readouterr().out
        assert "/my/project" in out

    def test_critical_facts_header_printed(self, capsys):
        anchored = [{"content": "fact"}]
        mock_storage = _make_storage_mock(hot_rows=[], anchored_rows=anchored)
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
        ):
            cmd_context(_make_args())
        out = capsys.readouterr().out
        assert "Critical Facts" in out


# ---------------------------------------------------------------------------
# task 310 (E2) — hot-memories query scopes on project_id, not
# directory_context. The column was re-stamped from the resolved project_id
# (C10f/C10g) so the old ``directory_context = $dir`` predicate bound a raw
# filesystem path against a column legacy rows still hold paths in but new
# rows hold identity strings in — silently missing every post-re-key row.
# ---------------------------------------------------------------------------


class TestProjectIdScoping:
    def test_hot_query_selects_on_project_id_column(self, capsys):
        """The SQL text must scope on project_id, not the retired directory_context."""
        mock_storage = _make_storage_mock(hot_rows=[], anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
            patch(
                "yadgar.core.cli._shared.resolve_cli_project",
                return_value="m-agahi/yadgar",
            ),
        ):
            cmd_context(_make_args())
        hot_query = mock_storage._q.call_args_list[0].args[0]
        assert "project_id = $dir" in hot_query
        assert "directory_context" not in hot_query

    def test_hot_query_binds_resolved_project_id_not_raw_directory(self, capsys):
        """The bound $dir value is the RESOLVED project_id, not args.directory."""
        mock_storage = _make_storage_mock(hot_rows=[], anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
            patch(
                "yadgar.core.cli._shared.resolve_cli_project",
                return_value="m-agahi/yadgar",
            ),
        ):
            cmd_context(_make_args(directory="/home/user/project"))
        hot_params = mock_storage._q.call_args_list[0].args[1]
        assert hot_params == {"dir": "m-agahi/yadgar"}

    def test_unresolvable_project_binds_none_not_directory_fallback(self, capsys):
        """required=False degrades to None on an unresolvable tree — the query
        must bind that None (matching no rows) rather than silently falling
        back to the raw directory path."""
        mock_storage = _make_storage_mock(hot_rows=[], anchored_rows=[])
        with (
            patch("yadgar._shared.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar._shared.config.Settings"),
            patch("yadgar.core.cli._shared.resolve_cli_project", return_value=None),
        ):
            cmd_context(_make_args(directory="/home/user/project"))
        hot_params = mock_storage._q.call_args_list[0].args[1]
        assert hot_params == {"dir": None}
