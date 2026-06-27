"""Shared config writer tests (v5.86 car #8 — P2).

set_config_value(key, raw) is the ONE sanctioned writer shared by the CLI
(cmd_config_set) and the Control-tab API (POST /api/control/config). It:

  - coerces `raw` to the right Python type from Settings.model_fields[KEY].annotation
    (NOT from a registry `kind` string — the annotation is authoritative and
    handles Optional[...] / list[...] uniformly),
  - raises ValueError/TypeError on a non-coercible value (callers map to 422/exit),
  - load/mutate/dump ~/.config/yadgar/config.yaml via ruamel (comment-preserving),
  - chmod 0o600.

Before this, the API had its own divergent coercion path (_coerce_json_value,
keyed off registry `kind`, no list branch) while the CLI used coerce_value off
the annotation — the classic two-write-paths divergence trap. This pins the
single shared path.
"""

from __future__ import annotations

import os

import pytest

from yadgar.config_yaml import set_config_value


@pytest.fixture
def _cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr("yadgar.config_yaml.get_config_path", lambda: path)
    return path


def test_set_config_value_writes_and_returns_coerced_float(_cfg_path):
    coerced = set_config_value("viz_node_size_3d", "12.5")
    assert coerced == 12.5
    assert isinstance(coerced, float)
    assert _cfg_path.exists()


def test_set_config_value_coerces_from_annotation_not_string(_cfg_path):
    """A bool knob given 'true' returns a real bool (annotation-driven)."""
    coerced = set_config_value("require_auth", "true")
    assert coerced is True


def test_set_config_value_accepts_already_typed_value(_cfg_path):
    """API passes JSON-decoded values (int/float/bool), not just strings."""
    coerced = set_config_value("viz_node_size_3d", 9)
    assert coerced == 9.0
    assert isinstance(coerced, float)


def test_set_config_value_raises_on_bad_coercion(_cfg_path):
    """Non-coercible value raises (callers map to 422 / CLI exit)."""
    with pytest.raises((ValueError, TypeError)):
        set_config_value("viz_node_size_3d", "not-a-number")


def test_set_config_value_chmod_0o600(_cfg_path):
    set_config_value("db_path", "/tmp/x.db")
    mode = os.stat(_cfg_path).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_cmd_config_set_delegates_to_shared_writer(tmp_path, monkeypatch):
    """The CLI path must route through set_config_value (single validation path)."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr("yadgar.config_yaml.get_config_path", lambda: path)

    calls = []
    real = set_config_value

    def _spy(key, raw):
        calls.append((key, raw))
        return real(key, raw)

    monkeypatch.setattr("yadgar.config_yaml.set_config_value", _spy)

    from yadgar.config_yaml import cmd_config_set

    class _Args:
        key = "viz_node_size_3d"
        value = "7.5"

    cmd_config_set(_Args())
    assert ("viz_node_size_3d", "7.5") in calls, (
        f"cmd_config_set did not delegate to set_config_value; calls={calls}"
    )
