"""Car 0 — format-preserving, atomic merge helper tests.

merge.py is format-generic (Car 0 boundary): given an existing config file, a
root-key path, and a value dict, it merges the value under that path preserving
every sibling key/comment, and writes atomically. No per-client serializers
here — those are Car 1.

Contracts under test:
- JSON key-merge preserves sibling MCP servers + unrelated top-level keys.
- TOML merge (tomlkit) preserves comments + other tables + key order.
- Nested root-key paths (e.g. ("amp","mcpServers")) are created/preserved.
- Idempotent: re-running yields byte-identical file content.
- Atomic: no partial file survives a crash mid-write; no temp files leak.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from yadgar.core.install.clients import merge

# ── JSON merge ───────────────────────────────────────────────────────────────


def test_json_merge_preserves_sibling_servers(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other": {"url": "http://other/mcp", "type": "streamable-http"},
                },
                "unrelatedTopLevel": {"keep": "me"},
            },
            indent=2,
        )
    )
    merge.merge_json(
        cfg,
        root_key=("mcpServers",),
        entry_key="yadgar",
        value={"type": "streamable-http", "url": "http://127.0.0.1:8765/mcp"},
    )
    out = json.loads(cfg.read_text())
    assert out["mcpServers"]["other"]["url"] == "http://other/mcp"
    assert out["mcpServers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"
    assert out["unrelatedTopLevel"] == {"keep": "me"}


def test_json_merge_creates_missing_file(tmp_path: Path):
    # Two missing levels — the writer must create parents recursively.
    cfg = tmp_path / "a" / "b" / "mcp.json"
    merge.merge_json(
        cfg,
        root_key=("mcpServers",),
        entry_key="yadgar",
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    out = json.loads(cfg.read_text())
    assert out["mcpServers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"


def test_json_merge_nested_root_key(tmp_path: Path):
    # Amp nests under amp.mcpServers.
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"amp": {"other": 1}}))
    merge.merge_json(
        cfg,
        root_key=("amp", "mcpServers"),
        entry_key="yadgar",
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    out = json.loads(cfg.read_text())
    assert out["amp"]["other"] == 1
    assert out["amp"]["mcpServers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"


def test_json_merge_malformed_existing_starts_fresh(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{ this is not json")
    merge.merge_json(
        cfg,
        root_key=("mcpServers",),
        entry_key="yadgar",
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    out = json.loads(cfg.read_text())
    assert out["mcpServers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"


def test_json_merge_toplevel_non_dict_starts_fresh(tmp_path: Path):
    # Valid JSON, but a list at the top level is not a config object — the loader
    # must discard it (dict guard) rather than crash or write into a list.
    cfg = tmp_path / "mcp.json"
    cfg.write_text("[1, 2, 3]")
    merge.merge_json(
        cfg,
        root_key=("mcpServers",),
        entry_key="yadgar",
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    out = json.loads(cfg.read_text())
    assert isinstance(out, dict)
    assert out["mcpServers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"


def test_json_merge_replaces_scalar_at_intermediate_key(tmp_path: Path):
    # A pre-existing scalar sitting where a nested table must go must be replaced
    # with a fresh mapping — the descent must treat a non-mapping as non-mapping.
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"amp": "i am a string, not a table"}))
    merge.merge_json(
        cfg,
        root_key=("amp", "mcpServers"),
        entry_key="yadgar",
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    out = json.loads(cfg.read_text())
    assert out["amp"]["mcpServers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"


def test_json_merge_idempotent(tmp_path: Path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"url": "http://other"}}}, indent=2))
    kwargs = dict(
        root_key=("mcpServers",),
        entry_key="yadgar",
        value={"type": "streamable-http", "url": "http://127.0.0.1:8765/mcp"},
    )
    merge.merge_json(cfg, **kwargs)
    first = cfg.read_text()
    merge.merge_json(cfg, **kwargs)
    second = cfg.read_text()
    assert first == second


# ── TOML merge ───────────────────────────────────────────────────────────────


def test_toml_merge_preserves_comments_and_tables(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "# top-level user comment\n"
        'model = "gpt-5"  # inline comment\n'
        "\n"
        "[mcp_servers.other]\n"
        'url = "http://other/mcp"\n'
    )
    merge.merge_toml(
        cfg,
        root_key=("mcp_servers", "yadgar"),
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    text = cfg.read_text()
    assert "# top-level user comment" in text
    assert "# inline comment" in text
    assert "[mcp_servers.other]" in text
    # tomlkit round-trip; parse to confirm the merged table exists.
    import tomlkit

    doc = tomlkit.parse(text)
    assert doc["mcp_servers"]["other"]["url"] == "http://other/mcp"
    assert doc["mcp_servers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"
    assert doc["model"] == "gpt-5"


def test_toml_merge_creates_missing_file(tmp_path: Path):
    cfg = tmp_path / "x" / "y" / "config.toml"
    merge.merge_toml(
        cfg,
        root_key=("mcp_servers", "yadgar"),
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    import tomlkit

    doc = tomlkit.parse(cfg.read_text())
    assert doc["mcp_servers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"


def test_toml_merge_idempotent(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('# keep me\nmodel = "gpt-5"\n')
    kwargs = dict(
        root_key=("mcp_servers", "yadgar"),
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    merge.merge_toml(cfg, **kwargs)
    first = cfg.read_text()
    merge.merge_toml(cfg, **kwargs)
    second = cfg.read_text()
    assert first == second


def test_toml_merge_replaces_existing_table_value(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.yadgar]\nurl = "http://stale/mcp"\n')
    merge.merge_toml(
        cfg,
        root_key=("mcp_servers", "yadgar"),
        value={"url": "http://127.0.0.1:8765/mcp"},
    )
    import tomlkit

    doc = tomlkit.parse(cfg.read_text())
    assert doc["mcp_servers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"


# ── atomic write ─────────────────────────────────────────────────────────────


def test_atomic_write_text_no_temp_leak(tmp_path: Path):
    target = tmp_path / "out.txt"
    merge._atomic_write_text(target, "hello world\n")
    assert target.read_text() == "hello world\n"
    leaked = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leaked == []


def test_atomic_write_text_replaces_existing(tmp_path: Path):
    target = tmp_path / "out.txt"
    target.write_text("old")
    merge._atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_text_cleans_temp_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "out.txt"

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        merge._atomic_write_text(target, "data")
    # target never created; no temp files left behind.
    assert not target.exists()
    leaked = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leaked == []
