"""I32 — capability-registry coverage lint, as a plain (non-e2e) pytest.

Mirrors test_contract_coverage.py: loads scripts/check_capability_coverage.py by
path and asserts the shipped docs/contracts/CAPABILITY_REGISTRY.md is fully catalogued, plus
a few unit checks on the enumerators and the orphan/stale/malformed detection.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_capability_coverage.py"

_spec = importlib.util.spec_from_file_location("check_capability_coverage", _SCRIPT)
assert _spec and _spec.loader
ccc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccc)


def test_real_registry_is_fully_catalogued() -> None:
    """The shipped registry covers every surface item, no stale/malformed rows."""
    assert ccc.check(_REPO_ROOT) == []


def test_enumerators_find_the_surfaces() -> None:
    """Sanity floors: the AST enumerators must find the known surfaces."""
    # T2 Car D packaged config: config.py lives inside the _shared/config/ package.
    assert (
        len(ccc.enumerate_settings(_REPO_ROOT / "yadgar" / "_shared" / "config" / "config.py"))
        >= 280
    )
    assert len(ccc.enumerate_tools(_REPO_ROOT / "yadgar" / "core" / "server" / "tools")) >= 70
    assert (
        len(
            ccc.enumerate_migrations(
                _REPO_ROOT / "yadgar" / "_shared" / "storage" / "migrations.py"
            )
        )
        >= 20
    )
    assert len(ccc.enumerate_bc(_REPO_ROOT / "docs" / "contracts" / "BEHAVIOR_CONTRACT.md")) >= 200


def test_parse_registry_extracts_fields() -> None:
    block = (
        "### CAP-RETR-001 — Example\n"
        "- **status:** LIVE\n"
        "- **settings:** `FOO_BAR`, `BAZ_QUX`\n"
        "- **tools:** `recall`\n"
        "- **bc:** `BC-RR1`\n"
        "- **refs:** `yadgar/config.py`\n"
    )
    entries = ccc.parse_registry(block)
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == "CAP-RETR-001"
    assert e["status"] == "LIVE"
    assert e["settings"] == {"FOO_BAR", "BAZ_QUX"}
    assert e["tools"] == {"recall"}
    assert e["bc"] == {"BC-RR1"}


def test_invalid_status_is_malformed(tmp_path) -> None:
    _write_min_registry(tmp_path, status="BOGUS")
    errs = ccc.check(tmp_path)
    assert any("MALFORMED" in e and "BOGUS" in e for e in errs), errs


def test_unresolved_ref_is_malformed(tmp_path) -> None:
    _write_min_registry(tmp_path, ref="yadgar/does_not_exist.py")
    errs = ccc.check(tmp_path)
    assert any("MALFORMED" in e and "does_not_exist" in e for e in errs), errs


def _write_min_registry(root: Path, *, status: str = "LIVE", ref: str = "yadgar/config.py") -> None:
    """Create a tiny tree the lint can run against (config/migrations/contract empty)."""
    (root / "yadgar" / "server" / "tools").mkdir(parents=True, exist_ok=True)
    (root / "yadgar" / "storage").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "contracts").mkdir(parents=True, exist_ok=True)
    # An empty config so enumerate_settings() returns no fields → no orphans to chase.
    (root / "yadgar" / "config.py").write_text("class Settings:\n    pass\n", encoding="utf-8")
    (root / "yadgar" / "storage" / "migrations.py").write_text("x = 1\n", encoding="utf-8")
    (root / "docs" / "contracts" / "BEHAVIOR_CONTRACT.md").write_text(
        "no rows here\n", encoding="utf-8"
    )
    (root / "docs" / "contracts" / "CAPABILITY_REGISTRY.md").write_text(
        f"### CAP-CFG-001 — t\n- **status:** {status}\n- **refs:** `{ref}`\n",
        encoding="utf-8",
    )
