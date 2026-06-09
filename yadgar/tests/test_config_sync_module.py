"""Tests for yadgar/config_sync.py — incremental YAML config sync.

Wave 2 coverage: yadgar/config_sync.py (100 stmts, 0% pre-wave).
Strategy: patch yadgar.config.Settings (class-level model_fields used by lazy imports),
yadgar.config_yaml.FIELD_META, and yadgar.config_yaml.get_config_path at boundary.
Test each helper + cmd_config_sync in check, dry-run, and apply modes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from yadgar.config_sync import (
    _apply_missing,
    _atomic_yaml_write,
    _handle_check,
    _handle_dry_run,
    cmd_config_sync,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings_class(fields: dict):
    """Return a mock Settings class (not instance) with model_fields."""
    cls = MagicMock()
    cls.model_fields = {k.upper(): MagicMock() for k in fields}
    # Instance used in cmd_config_sync calls settings = Settings()
    instance = MagicMock()
    for k, v in fields.items():
        setattr(instance, k.upper(), v)
    cls.return_value = instance
    return cls


def _yaml_with_keys(*keys) -> CommentedMap:
    data = CommentedMap()
    for k in keys:
        data[k] = "existing"
    return data


def _write_yaml_file(path: Path, keys: list[str]):
    y = YAML()
    y.default_flow_style = False
    data = CommentedMap({k: "existing" for k in keys})
    with open(path, "w") as f:
        y.dump(data, f)


# ---------------------------------------------------------------------------
# _compute_missing (via cmd_config_sync, since lazy import makes direct test
#  complex — test through integration path)
# ---------------------------------------------------------------------------


class TestComputeMissingViaSync:
    def test_no_missing_when_all_present(self, tmp_path):
        config = tmp_path / "config.yaml"
        _write_yaml_file(config, ["foo", "bar"])
        mock_cls = _mock_settings_class({"foo": "f", "bar": "b"})
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=config),
            patch("yadgar.config_yaml.FIELD_META", {}),
        ):
            # No exception / no file modification expected
            from yadgar.config_sync import _compute_missing

            data = _yaml_with_keys("foo", "bar")
            result = _compute_missing(data, None)
        assert result == []

    def test_missing_field_detected(self):
        mock_cls = _mock_settings_class({"foo": "f", "bar": "b"})
        with patch("yadgar.config.Settings", mock_cls):
            from yadgar.config_sync import _compute_missing

            data = _yaml_with_keys("foo")
            result = _compute_missing(data, None)
        assert "bar" in result

    def test_returns_lowercase_names(self):
        mock_cls = _mock_settings_class({"my_field": "v"})
        with patch("yadgar.config.Settings", mock_cls):
            from yadgar.config_sync import _compute_missing

            data = CommentedMap()
            result = _compute_missing(data, None)
        assert "my_field" in result


class TestComputeUnknownViaModule:
    def test_returns_empty_when_disabled(self):
        from yadgar.config_sync import _compute_unknown

        data = _yaml_with_keys("extra_key")
        result = _compute_unknown(data, remove_unknown=False)
        assert result == []

    def test_returns_unknown_keys_when_enabled(self):
        mock_cls = _mock_settings_class({"foo": "f"})
        with patch("yadgar.config.Settings", mock_cls):
            from yadgar.config_sync import _compute_unknown

            data = _yaml_with_keys("foo", "unknown_key")
            result = _compute_unknown(data, remove_unknown=True)
        assert "unknown_key" in result

    def test_known_keys_not_returned(self):
        mock_cls = _mock_settings_class({"foo": "f"})
        with patch("yadgar.config.Settings", mock_cls):
            from yadgar.config_sync import _compute_unknown

            data = _yaml_with_keys("foo")
            result = _compute_unknown(data, remove_unknown=True)
        assert "foo" not in result


# ---------------------------------------------------------------------------
# _handle_check
# ---------------------------------------------------------------------------


class TestHandleCheck:
    def test_no_missing_prints_synced(self, capsys):
        settings = MagicMock()
        with pytest.raises(SystemExit) as exc_info:
            _handle_check([], settings)
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "synced" in out.lower() or "no missing" in out.lower()

    def test_missing_exits_1(self):
        settings = MagicMock()
        settings.NEW_FIELD = "default"
        with pytest.raises(SystemExit) as exc_info:
            _handle_check(["new_field"], settings)
        assert exc_info.value.code == 1

    def test_missing_key_printed(self, capsys):
        settings = MagicMock()
        settings.NEW_FIELD = "val"
        with pytest.raises(SystemExit):
            _handle_check(["new_field"], settings)
        out = capsys.readouterr().out
        assert "new_field" in out


# ---------------------------------------------------------------------------
# _handle_dry_run
# ---------------------------------------------------------------------------


class TestHandleDryRun:
    def test_no_missing_prints_synced(self, capsys):
        settings = MagicMock()
        with patch("yadgar.config_yaml.FIELD_META", {}):
            _handle_dry_run([], [], settings, remove_unknown=False)
        out = capsys.readouterr().out
        assert "synced" in out.lower() or "no changes" in out.lower()

    def test_missing_printed_with_default(self, capsys):
        settings = MagicMock()
        settings.NEW_FIELD = "my_default"
        with patch("yadgar.config_yaml.FIELD_META", {"new_field": {"desc": "some desc"}}):
            _handle_dry_run(["new_field"], [], settings, remove_unknown=False)
        out = capsys.readouterr().out
        assert "new_field" in out

    def test_unknown_keys_printed_when_enabled(self, capsys):
        settings = MagicMock()
        with patch("yadgar.config_yaml.FIELD_META", {}):
            _handle_dry_run([], ["stale_key"], settings, remove_unknown=True)
        out = capsys.readouterr().out
        assert "stale_key" in out

    def test_unknown_keys_not_printed_when_disabled(self, capsys):
        settings = MagicMock()
        with patch("yadgar.config_yaml.FIELD_META", {}):
            _handle_dry_run([], ["stale_key"], settings, remove_unknown=False)
        out = capsys.readouterr().out
        assert "stale_key" not in out


# ---------------------------------------------------------------------------
# _apply_missing
# ---------------------------------------------------------------------------


class TestApplyMissing:
    def test_adds_missing_key(self):
        data = CommentedMap()
        settings = MagicMock()
        settings.NEW_FIELD = "default_value"
        with patch("yadgar.config_yaml.FIELD_META", {"new_field": {"desc": "test desc"}}):
            _apply_missing(data, ["new_field"], settings)
        assert "new_field" in data
        assert data["new_field"] == "default_value"

    def test_preserves_existing_keys(self):
        data = CommentedMap({"existing": "value"})
        settings = MagicMock()
        settings.NEW = "n"
        with patch("yadgar.config_yaml.FIELD_META", {}):
            _apply_missing(data, ["new"], settings)
        assert data["existing"] == "value"

    def test_empty_missing_list_noop(self):
        data = CommentedMap({"key": "v"})
        settings = MagicMock()
        with patch("yadgar.config_yaml.FIELD_META", {}):
            _apply_missing(data, [], settings)
        assert len(data) == 1

    def test_no_desc_in_field_meta_still_adds(self):
        data = CommentedMap()
        settings = MagicMock()
        settings.MY_KEY = "val"
        with patch("yadgar.config_yaml.FIELD_META", {}):  # no entry for my_key
            _apply_missing(data, ["my_key"], settings)
        assert data["my_key"] == "val"


# ---------------------------------------------------------------------------
# _atomic_yaml_write
# ---------------------------------------------------------------------------


class TestAtomicYamlWrite:
    def test_writes_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        y = YAML()
        y.default_flow_style = False
        data = CommentedMap({"key": "value"})
        _atomic_yaml_write(path, y, data)
        assert path.exists()
        assert "key: value" in path.read_text()

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "config.yaml"
        y = YAML()
        y.default_flow_style = False
        data = CommentedMap({"x": 1})
        _atomic_yaml_write(path, y, data)
        assert path.exists()

    def test_file_permissions_600(self, tmp_path):
        path = tmp_path / "config.yaml"
        y = YAML()
        y.default_flow_style = False
        data = CommentedMap({"k": "v"})
        _atomic_yaml_write(path, y, data)
        mode = oct(path.stat().st_mode)
        assert mode.endswith("600")

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("old: content")
        y = YAML()
        y.default_flow_style = False
        data = CommentedMap({"new": "content"})
        _atomic_yaml_write(path, y, data)
        assert "new: content" in path.read_text()
        assert "old: content" not in path.read_text()


# ---------------------------------------------------------------------------
# cmd_config_sync — integration
# ---------------------------------------------------------------------------


def _args(**kw):
    defaults = {"check": False, "dry_run": False, "remove_unknown": False}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestCmdConfigSync:
    def test_missing_config_file_exits(self, tmp_path, capsys):
        args = _args()
        mock_cls = _mock_settings_class({"foo": "f"})
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=tmp_path / "missing.yaml"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_config_sync(args)
        assert exc_info.value.code == 1

    def test_fully_synced_prints_no_changes(self, tmp_path, capsys):
        config = tmp_path / "config.yaml"
        _write_yaml_file(config, ["foo", "bar"])
        mock_cls = _mock_settings_class({"foo": "f", "bar": "b"})
        args = _args()
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=config),
            patch("yadgar.config_yaml.FIELD_META", {}),
        ):
            cmd_config_sync(args)
        out = capsys.readouterr().out
        assert "no changes" in out.lower() or "synced" in out.lower()

    def test_check_mode_exits_1_with_missing(self, tmp_path):
        config = tmp_path / "config.yaml"
        _write_yaml_file(config, ["foo"])
        mock_cls = _mock_settings_class({"foo": "f", "new_key": "default"})
        args = _args(check=True)
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=config),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_config_sync(args)
        assert exc_info.value.code == 1

    def test_dry_run_does_not_modify_file(self, tmp_path):
        config = tmp_path / "config.yaml"
        _write_yaml_file(config, ["foo"])
        original_content = config.read_text()
        mock_cls = _mock_settings_class({"foo": "f", "new_key": "default"})
        args = _args(dry_run=True)
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=config),
            patch("yadgar.config_yaml.FIELD_META", {}),
            patch("yadgar.config_yaml.FIELD_META", {}),
        ):
            cmd_config_sync(args)
        assert config.read_text() == original_content

    def test_apply_adds_missing_key(self, tmp_path):
        config = tmp_path / "config.yaml"
        _write_yaml_file(config, ["foo"])
        mock_cls = _mock_settings_class({"foo": "f", "new_key": "new_default"})
        args = _args()
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=config),
            patch("yadgar.config_yaml.FIELD_META", {}),
            patch("yadgar.config_yaml.FIELD_META", {}),
        ):
            cmd_config_sync(args)
        content = config.read_text()
        assert "new_key" in content

    def test_remove_unknown_deletes_key(self, tmp_path, capsys):
        config = tmp_path / "config.yaml"
        _write_yaml_file(config, ["foo", "stale_key"])
        mock_cls = _mock_settings_class({"foo": "f"})
        args = _args(remove_unknown=True)
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=config),
            patch("yadgar.config_yaml.FIELD_META", {}),
            patch("yadgar.config_yaml.FIELD_META", {}),
        ):
            cmd_config_sync(args)
        content = config.read_text()
        assert "stale_key" not in content

    def test_yaml_parse_error_exits(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text("{ invalid yaml }")
        # ruamel.yaml may or may not raise on this — use a truly invalid format
        # that forces an exception
        config.write_text("key: [unclosed bracket\n")
        mock_cls = _mock_settings_class({"foo": "f"})
        args = _args()
        with (
            patch("yadgar.config.Settings", mock_cls),
            patch("yadgar.config_yaml.get_config_path", return_value=config),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_config_sync(args)
        assert exc_info.value.code == 1
