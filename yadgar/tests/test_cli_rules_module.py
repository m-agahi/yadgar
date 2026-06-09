"""Tests for yadgar/cli/rules.py — rules management subcommand.

Wave 3 coverage: yadgar/cli/rules.py (~55 stmts, 0% pre-wave).
Strategy: mock StorageEngine and RulesEngine at yadgar.storage / yadgar.rules_engine
(lazy imports inside function bodies — do NOT patch at yadgar.cli.rules.*).
Test register(), cmd_rules_export, cmd_rules_import, cmd_rules dispatch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yadgar.cli.rules import cmd_rules, cmd_rules_export, cmd_rules_import, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_export_args(db_path=None):
    return SimpleNamespace(db_path=db_path)


def _make_import_args(file, db_path=None):
    return SimpleNamespace(file=str(file), db_path=db_path)


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_creates_rules_subparser(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["rules", "export"])
        assert args.rules_command == "export"

    def test_register_import_subcommand(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["rules", "import", "/tmp/rules.json"])
        assert args.rules_command == "import"
        assert args.file == "/tmp/rules.json"


# ---------------------------------------------------------------------------
# cmd_rules_export
# ---------------------------------------------------------------------------


class TestCmdRulesExport:
    def test_exports_rules_as_json_when_no_ruamel(self, capsys):
        rules_data = [{"id": "r1", "pattern": "*.py", "action": "block"}]
        mock_storage = MagicMock()
        mock_engine = MagicMock()
        mock_engine.export_rules.return_value = rules_data

        with (
            patch("yadgar.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar.rules_engine.RulesEngine", return_value=mock_engine),
            patch("yadgar.config.Settings"),
            patch.dict("sys.modules", {"ruamel.yaml": None}),
        ):
            cmd_rules_export(_make_export_args())

        out = capsys.readouterr().out
        # Should fall back to JSON when ruamel.yaml not available
        payload = json.loads(out)
        assert payload[0]["id"] == "r1"

    def test_export_calls_engine_export(self, capsys):
        rules_data = []
        mock_storage = MagicMock()
        mock_engine = MagicMock()
        mock_engine.export_rules.return_value = rules_data

        with (
            patch("yadgar.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar.rules_engine.RulesEngine", return_value=mock_engine),
            patch("yadgar.config.Settings"),
            patch.dict("sys.modules", {"ruamel.yaml": None}),
        ):
            cmd_rules_export(_make_export_args())

        mock_engine.export_rules.assert_called_once()

    def test_storage_closed_after_export(self, capsys):
        mock_storage = MagicMock()
        mock_engine = MagicMock()
        mock_engine.export_rules.return_value = []

        with (
            patch("yadgar.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar.rules_engine.RulesEngine", return_value=mock_engine),
            patch("yadgar.config.Settings"),
            patch.dict("sys.modules", {"ruamel.yaml": None}),
        ):
            cmd_rules_export(_make_export_args())

        mock_storage.close.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_rules_import
# ---------------------------------------------------------------------------


class TestCmdRulesImport:
    def test_import_from_json_file(self, tmp_path, capsys):
        rules_data = [{"id": "r1"}]
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(json.dumps(rules_data))

        mock_storage = MagicMock()
        mock_engine = MagicMock()
        mock_engine.import_rules.return_value = 1

        with (
            patch("yadgar.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar.rules_engine.RulesEngine", return_value=mock_engine),
            patch("yadgar.config.Settings"),
            patch.dict("sys.modules", {"ruamel.yaml": None}),
        ):
            cmd_rules_import(_make_import_args(rules_file))

        out = capsys.readouterr().out
        assert "1" in out

    def test_missing_file_exits_one(self, tmp_path, capsys):
        missing = tmp_path / "no_such.json"
        with pytest.raises(SystemExit) as exc_info:
            cmd_rules_import(_make_import_args(missing))
        assert exc_info.value.code == 1

    def test_non_list_rules_exits_one(self, tmp_path, capsys):
        rules_file = tmp_path / "bad.json"
        rules_file.write_text('{"key": "value"}')

        with (
            patch.dict("sys.modules", {"ruamel.yaml": None}),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_rules_import(_make_import_args(rules_file))
        assert exc_info.value.code == 1

    def test_storage_closed_after_import(self, tmp_path, capsys):
        rules_file = tmp_path / "rules.json"
        rules_file.write_text("[]")

        mock_storage = MagicMock()
        mock_engine = MagicMock()
        mock_engine.import_rules.return_value = 0

        with (
            patch("yadgar.storage.StorageEngine", return_value=mock_storage),
            patch("yadgar.rules_engine.RulesEngine", return_value=mock_engine),
            patch("yadgar.config.Settings"),
            patch.dict("sys.modules", {"ruamel.yaml": None}),
        ):
            cmd_rules_import(_make_import_args(rules_file))

        mock_storage.close.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_rules dispatch
# ---------------------------------------------------------------------------


class TestCmdRulesDispatch:
    def test_no_subcommand_prints_help(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        rules_parser = register(subs)
        root.parse_args(["rules"])

        with patch.object(rules_parser, "print_help") as mock_help:
            # Call via the lambda that was set as func
            args_with_none = SimpleNamespace(rules_command=None)
            cmd_rules(args_with_none, rules_parser)
        mock_help.assert_called_once()

    def test_export_subcommand_dispatches(self):
        args = SimpleNamespace(rules_command="export", db_path=None)
        mock_parser = MagicMock()
        with patch("yadgar.cli.rules.cmd_rules_export") as mock_export:
            cmd_rules(args, mock_parser)
        mock_export.assert_called_once_with(args)

    def test_import_subcommand_dispatches(self, tmp_path):
        rules_file = tmp_path / "r.json"
        rules_file.write_text("[]")
        args = SimpleNamespace(rules_command="import", file=str(rules_file), db_path=None)
        mock_parser = MagicMock()
        with patch("yadgar.cli.rules.cmd_rules_import") as mock_import:
            cmd_rules(args, mock_parser)
        mock_import.assert_called_once_with(args)
