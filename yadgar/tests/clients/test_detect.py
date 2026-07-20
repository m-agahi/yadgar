"""Car 3 — detect.py tests.

Contracts under test:

  1. ``detect_installed_clients`` — returns only clients whose config dir exists.
  2. ``detect_installed_clients`` — returns empty list when no client dirs exist.
  3. ``is_client_present`` — True when global config dir exists.
  4. ``is_client_present`` — False when global config dir absent.
  5. ``detect_installed_clients`` — ordering is consistent (deterministic).
  6. ``is_client_present`` — binary probe (PATH) used as fallback when config dir is None.
  7. ``detect_installed_clients`` — returns all clients when all present.
  8. ``detect_installed_clients`` — full 9-client registry round-trip.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
)
from yadgar.core.install.clients.registry import CLIENT_REGISTRY


def _make_descriptor_with_dir(
    name: str, global_path_fn=None, project_path_fn=None
) -> ClientDescriptor:
    """Minimal descriptor for detect tests."""
    return ClientDescriptor(
        name=name,
        mcp_config_path=PathSpec(
            global_factory=global_path_fn,
            project_factory=project_path_fn,
        ),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )


# ── 1. detect_installed_clients — only returns present ───────────────────────


def test_detect_returns_only_present(tmp_path: Path):
    """Clients with existing config dirs appear; absent ones do not."""
    from yadgar.core.install.clients import detect

    present_dir = tmp_path / "present_client"
    present_dir.mkdir()
    absent_dir = tmp_path / "absent_client"
    # absent_dir NOT created

    d_present = _make_descriptor_with_dir(
        "present", global_path_fn=lambda: present_dir / "cfg.json"
    )
    d_absent = _make_descriptor_with_dir("absent", global_path_fn=lambda: absent_dir / "cfg.json")

    registry = {"present": d_present, "absent": d_absent}
    result = detect.detect_installed_clients(registry=registry)
    names = [d.name for d in result]
    assert "present" in names
    assert "absent" not in names


# ── 2. detect_installed_clients — empty list when nothing present ─────────────


def test_detect_returns_empty_when_none_present(tmp_path: Path):
    from yadgar.core.install.clients import detect

    d = _make_descriptor_with_dir("x", global_path_fn=lambda: tmp_path / "no" / "cfg.json")
    result = detect.detect_installed_clients(registry={"x": d})
    assert result == []


# ── 3. is_client_present — True when dir exists ──────────────────────────────


def test_is_client_present_dir_exists(tmp_path: Path):
    from yadgar.core.install.clients import detect

    config_file = tmp_path / "cfg.json"
    config_file.touch()  # parent dir = tmp_path (exists)
    d = _make_descriptor_with_dir("x", global_path_fn=lambda: config_file)
    assert detect.is_client_present(d) is True


# ── 4. is_client_present — False when dir absent ─────────────────────────────


def test_is_client_present_dir_absent(tmp_path: Path):
    from yadgar.core.install.clients import detect

    d = _make_descriptor_with_dir("x", global_path_fn=lambda: tmp_path / "no" / "cfg.json")
    assert detect.is_client_present(d) is False


# ── 5. deterministic ordering ─────────────────────────────────────────────────


def test_detect_order_is_deterministic(tmp_path: Path):
    from yadgar.core.install.clients import detect

    # Three clients with dirs present
    dirs = {}
    for name in ("aaa", "bbb", "ccc"):
        d = tmp_path / name
        d.mkdir()
        dirs[name] = d

    registry = {
        name: _make_descriptor_with_dir(name, global_path_fn=lambda n=name: dirs[n] / "cfg.json")
        for name in ("aaa", "bbb", "ccc")
    }

    r1 = [d.name for d in detect.detect_installed_clients(registry=registry)]
    r2 = [d.name for d in detect.detect_installed_clients(registry=registry)]
    assert r1 == r2


# ── 6. is_client_present — binary fallback when config dir is None ────────────


def test_is_client_present_binary_fallback():
    from yadgar.core.install.clients import detect

    d = _make_descriptor_with_dir("x", global_path_fn=None)  # no config path

    with patch("shutil.which", return_value="/usr/bin/x"):
        assert detect.is_client_present(d, binary_name="x") is True

    with patch("shutil.which", return_value=None):
        assert detect.is_client_present(d, binary_name="x") is False


# ── 7. detect — returns all when all dirs present ────────────────────────────


def test_detect_all_present(tmp_path: Path):
    from yadgar.core.install.clients import detect

    names = ["a", "b", "c", "d"]
    registry = {}
    for name in names:
        d = tmp_path / name
        d.mkdir()
        registry[name] = _make_descriptor_with_dir(
            name, global_path_fn=lambda n=name: tmp_path / n / "cfg.json"
        )

    result = detect.detect_installed_clients(registry=registry)
    assert sorted(d.name for d in result) == sorted(names)


# ── 8. Full 9-client registry round-trip (smoke — just no crash) ─────────────


def test_full_registry_detect_no_crash():
    """detect_installed_clients over the real registry completes without error."""
    from yadgar.core.install.clients import detect

    # On a CI box with no client dirs, result will be [].
    result = detect.detect_installed_clients(registry=CLIENT_REGISTRY)
    assert isinstance(result, list)
    # All returned descriptors must be valid ClientDescriptor objects
    for d in result:
        assert hasattr(d, "name")
