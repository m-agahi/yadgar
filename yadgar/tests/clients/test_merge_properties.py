"""Car 0 — property-based tests for merge idempotence + sibling preservation.

Hypothesis drives arbitrary sibling structures through the JSON and TOML merge
helpers and asserts two invariants:
  (P1) sibling preservation — every pre-existing sibling under the root key, and
       every unrelated top-level key, survives the merge unchanged.
  (P2) idempotence — a second identical merge yields byte-identical file content.
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from yadgar.core.install.clients import merge

# JSON-safe scalar values for sibling payloads.
_scalars = st.one_of(
    st.text(max_size=20),
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
)

# Sibling server names that are NOT "yadgar" and are valid identifiers-ish.
_sibling_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz_",
    min_size=1,
    max_size=8,
).filter(lambda s: s != "yadgar")

_sibling_map = st.dictionaries(
    keys=_sibling_names,
    values=st.dictionaries(keys=st.text(min_size=1, max_size=6), values=_scalars, max_size=3),
    max_size=4,
)


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(siblings=_sibling_map, top=st.dictionaries(_sibling_names, _scalars, max_size=3))
def test_json_merge_preserves_siblings_property(tmp_path_factory, siblings, top):
    d = tmp_path_factory.mktemp("json_prop")
    cfg = d / "mcp.json"
    base = {"mcpServers": dict(siblings)}
    base.update(top)
    cfg.write_text(json.dumps(base, indent=2))

    value = {"type": "streamable-http", "url": "http://127.0.0.1:8765/mcp"}
    merge.merge_json(cfg, root_key=("mcpServers",), entry_key="yadgar", value=value)

    out = json.loads(cfg.read_text())
    # P1: every sibling survives unchanged.
    for k, v in siblings.items():
        assert out["mcpServers"][k] == v
    for k, v in top.items():
        assert out[k] == v
    assert out["mcpServers"]["yadgar"] == value

    # P2: idempotent.
    first = cfg.read_text()
    merge.merge_json(cfg, root_key=("mcpServers",), entry_key="yadgar", value=value)
    assert cfg.read_text() == first


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(siblings=_sibling_map)
def test_toml_merge_preserves_siblings_property(tmp_path_factory, siblings):
    import tomlkit

    d = tmp_path_factory.mktemp("toml_prop")
    cfg = d / "config.toml"
    doc = tomlkit.document()
    doc.add(tomlkit.comment("user comment must survive"))
    servers = tomlkit.table()
    for name, payload in siblings.items():
        sub = tomlkit.table()
        for kk, vv in payload.items():
            sub[kk] = vv
        servers[name] = sub
    doc["mcp_servers"] = servers
    cfg.write_text(tomlkit.dumps(doc))

    value = {"url": "http://127.0.0.1:8765/mcp"}
    merge.merge_toml(cfg, root_key=("mcp_servers", "yadgar"), value=value)

    text = cfg.read_text()
    assert "user comment must survive" in text
    parsed = tomlkit.parse(text)
    for name, payload in siblings.items():
        for kk, vv in payload.items():
            assert parsed["mcp_servers"][name][kk] == vv
    assert parsed["mcp_servers"]["yadgar"]["url"] == "http://127.0.0.1:8765/mcp"

    # P2: idempotent.
    first = cfg.read_text()
    merge.merge_toml(cfg, root_key=("mcp_servers", "yadgar"), value=value)
    assert cfg.read_text() == first
