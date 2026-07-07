"""Tests for scripts/check_dead_capability.py (I29 — edge dead-capability lint).

TDD: tests define required behaviour of the lint. Run with:
  uv run pytest yadgar/tests/test_check_dead_capability.py

Test plan:
  1. Real-codebase passthrough — lint exits 0 on the actual repo.
  2. Orphan fixture — edge type produced in code but absent from contract → exit 1,
     names the type.
  3. Drop-still-produced fixture — contract marks type `drop` but still produced
     → exit 1, names the type.
  4. Stale fixture — contract row for a type that no longer exists in code → exit 1,
     names the type.
  5. Clean fixture — produced == contracted, no drop violations → exit 0.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
SCRIPT = _REPO_ROOT / "scripts" / "check_dead_capability.py"


def run_script(*args: str) -> subprocess.CompletedProcess:
    """Run the lint script as a subprocess and return the result."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_GRAPH_API_EDGE_TMPL = """\
def build_edges():
    return [
        {{"source": "mem:1", "target": "mem:2", "type": "{edge_type}", "role": "display"}},
    ]
"""

_GRAPH_API_DYNAMIC_TMPL = """\
def build_entity_edges(rel_type):
    # rel_type comes from DB — not a literal
    return [{{"source": "e:1", "target": "e:2", "type": rel_type, "role": "retrieval"}}]
"""

_VIZ_META_TMPL = """\
EDGE_TYPES: dict[str, dict] = {{
{entries}
}}
"""

_CONTRACT_HEADER = """\
# Edge Contract

## Contract table

| Edge / relation | Producer | Stored? | Consumer TODAY | TARGET role | Action |
|---|---|---|---|---|---|
"""

_CONTRACT_ROW_TMPL = (
    "| `{edge_type}` | some_producer | yes | some_consumer | **{role}** | some action |\n"
)


def _make_fixture(
    tmp_path: Path,
    *,
    graph_api_edge_types: list[str],
    registry_edge_types: list[str],
    contract_rows: dict[str, str],  # type → role
    dynamic_types: list[str] | None = None,
) -> Path:
    """Build a minimal fake repo under tmp_path.

    Structure:
      tmp_path/
        yadgar/
          graph_api.py     — produces edge literal types
          viz_meta.py      — EDGE_TYPES registry
        docs/
          EDGE_CONTRACT.md — contract table

    Returns tmp_path (the fake repo root).
    """
    yadgar_dir = tmp_path / "yadgar" / "core"
    yadgar_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    # graph_api.py — literal edge type dicts
    graph_api_lines = ["def build_edges():\n    return [\n"]
    for et in graph_api_edge_types:
        graph_api_lines.append(
            f'        {{"source": "mem:1", "target": "mem:2", "type": "{et}", "role": "display"}},\n'
        )
    if dynamic_types:
        # Dynamic types have no string literal — only in registry
        graph_api_lines.append("    ]\n\ndef dyn(rel_type):\n    return []\n")
    else:
        graph_api_lines.append("    ]\n")
    (yadgar_dir / "graph_api.py").write_text("".join(graph_api_lines))

    # viz_meta.py — EDGE_TYPES registry
    entries = ""
    for rt in registry_edge_types:
        entries += f'    "{rt}": {{"label": "{rt}", "role": "display", "default_on": True}},\n'
    (yadgar_dir / "viz_meta.py").write_text(f"EDGE_TYPES: dict[str, dict] = {{\n{entries}}}\n")

    # docs/EDGE_CONTRACT.md
    contract_text = _CONTRACT_HEADER
    for et, role in contract_rows.items():
        contract_text += _CONTRACT_ROW_TMPL.format(edge_type=et, role=role)
    (docs_dir / "EDGE_CONTRACT.md").write_text(contract_text)

    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: real codebase passes
# ---------------------------------------------------------------------------


def test_real_codebase_passes():
    """The lint must exit 0 on the actual repository (post-train, all edges contracted)."""
    result = run_script()
    assert result.returncode == 0, (
        f"check_dead_capability.py failed on real codebase:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout == "", f"Unexpected violations:\n{result.stdout}"


# ---------------------------------------------------------------------------
# Test 2: orphan capability → lint fails, names the type
# ---------------------------------------------------------------------------


def test_orphan_edge_fails(tmp_path):
    """Edge type produced in code but absent from EDGE_CONTRACT → exit 1, names type."""
    _make_fixture(
        tmp_path,
        graph_api_edge_types=["orphan_edge"],
        registry_edge_types=["orphan_edge"],
        contract_rows={},  # nothing in contract
    )
    result = run_script("--repo-root", str(tmp_path))
    assert result.returncode == 1
    assert "orphan_edge" in result.stdout
    assert "ORPHAN" in result.stdout


def test_orphan_registry_only_fails(tmp_path):
    """Type in EDGE_TYPES registry but not in contract → exit 1, named as ORPHAN."""
    _make_fixture(
        tmp_path,
        graph_api_edge_types=[],  # no literal in graph_api.py
        registry_edge_types=["ghost_type"],  # but in registry
        contract_rows={},
    )
    result = run_script("--repo-root", str(tmp_path))
    assert result.returncode == 1
    assert "ghost_type" in result.stdout
    assert "ORPHAN" in result.stdout


# ---------------------------------------------------------------------------
# Test 3: drop-still-produced → lint fails, names the type
# ---------------------------------------------------------------------------


def test_drop_still_produced_fails(tmp_path):
    """Type marked `drop` in contract but still produced in code → exit 1."""
    _make_fixture(
        tmp_path,
        graph_api_edge_types=["dead_edge"],
        registry_edge_types=["dead_edge"],
        contract_rows={"dead_edge": "drop"},
    )
    result = run_script("--repo-root", str(tmp_path))
    assert result.returncode == 1
    assert "dead_edge" in result.stdout
    assert "DROP" in result.stdout


# ---------------------------------------------------------------------------
# Test 4: stale contract row → lint fails, names the type
# ---------------------------------------------------------------------------


def test_stale_contract_row_fails(tmp_path):
    """Contract row for a type no longer produced → exit 1, named as STALE."""
    _make_fixture(
        tmp_path,
        graph_api_edge_types=[],  # not produced
        registry_edge_types=[],  # not in registry
        contract_rows={"ghost_edge": "display"},  # but in contract
    )
    result = run_script("--repo-root", str(tmp_path))
    assert result.returncode == 1
    assert "ghost_edge" in result.stdout
    assert "STALE" in result.stdout


# ---------------------------------------------------------------------------
# Test 5: clean fixture → passes
# ---------------------------------------------------------------------------


def test_clean_fixture_passes(tmp_path):
    """Produced == contracted, no drop violations → exit 0."""
    _make_fixture(
        tmp_path,
        graph_api_edge_types=["alpha_edge", "beta_edge"],
        registry_edge_types=["alpha_edge", "beta_edge"],
        contract_rows={"alpha_edge": "retrieval", "beta_edge": "display"},
    )
    result = run_script("--repo-root", str(tmp_path))
    assert result.returncode == 0, (
        f"Expected clean exit but got violations:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 6: --list-all exits 0 even with violations
# ---------------------------------------------------------------------------


def test_list_all_exits_zero_on_violations(tmp_path):
    """--list-all is informational; exits 0 even when violations exist."""
    _make_fixture(
        tmp_path,
        graph_api_edge_types=["orphan_type"],
        registry_edge_types=["orphan_type"],
        contract_rows={},
    )
    result = run_script("--repo-root", str(tmp_path), "--list-all")
    assert result.returncode == 0
    assert "orphan_type" in result.stdout


# ---------------------------------------------------------------------------
# Test 7: multi-type combined contract row (entity typed-relations pattern)
# ---------------------------------------------------------------------------


def test_multi_type_contract_row(tmp_path):
    """A contract row listing multiple backtick types covers all of them."""
    yadgar_dir = tmp_path / "yadgar" / "core"
    yadgar_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    # graph_api.py with literal types for co_a and co_b
    (yadgar_dir / "graph_api.py").write_text(
        "def f():\n"
        '    return [{"source":"a","target":"b","type":"co_a"},\n'
        '            {"source":"a","target":"b","type":"co_b"}]\n'
    )
    # registry has both
    (yadgar_dir / "viz_meta.py").write_text(
        "EDGE_TYPES: dict[str, dict] = {\n"
        '    "co_a": {"label": "Co A", "role": "retrieval", "default_on": True},\n'
        '    "co_b": {"label": "Co B", "role": "retrieval", "default_on": True},\n'
        "}\n"
    )
    # contract has ONE row for both types
    (docs_dir / "EDGE_CONTRACT.md").write_text(
        "# Edge Contract\n\n## Contract table\n\n"
        "| Edge / relation | Producer | Stored? | Consumer TODAY | TARGET role | Action |\n"
        "|---|---|---|---|---|---|\n"
        "| **entity typed-relations** (`co_a`, `co_b`) | code | yes | recall | **retrieval** | use it |\n"
    )
    result = run_script("--repo-root", str(tmp_path))
    assert result.returncode == 0, (
        f"Multi-type contract row should cover both types:\n{result.stdout}\n{result.stderr}"
    )
