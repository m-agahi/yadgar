"""v5.169.1 fix: PyYAML imported by shipped modules but never declared in pyproject.

`pyproject.toml` declares `ruamel.yaml>=0.18.0` as the only YAML dependency.
PyYAML (`yaml`) shows up in uv.lock ONLY as a transitive dependency of the
optional `ml` extra (huggingface-hub / transformers) — a base install (no
`[ml]` extra) genuinely has no PyYAML installed. Three loader functions
nonetheless did `import yaml` first and only fell back to ruamel.yaml on
ImportError, meaning their primary code path silently depended on whichever
packages happened to be installed in a given environment — the same
undeclared-dependency shape that shipped the "No module named surrealdb"
class of bug before.

Fix: those loaders now use ruamel.yaml (the always-present, declared hard
dependency) exclusively; the incidental PyYAML preference is removed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_YADGAR_ROOT = Path(__file__).resolve().parent.parent.parent

# The three loader modules that had the "prefer PyYAML, except ImportError:
# fall back to ruamel.yaml" shape (found by repo-wide grep for `import yaml`
# outside yadgar/tests/). NOTE: yadgar/core/server/tools/project.py's
# `_scan_stale_wiki_slugs` also does an optional `import yaml` but is a
# structurally different, already-safe pattern (sets `_yaml = None` on
# ImportError and degrades further inside `_parse_frontmatter`, which has its
# own independent ruamel fallback) — out of scope for this fix.
_TARGET_MODULES = [
    "core/server/tools/agent_prompts.py",
    "core/cli/seed.py",
    "_shared/wiki/wiki_meta.py",
]

_BARE_PYYAML_IMPORT_RE = re.compile(r"^\s*import yaml\b", re.MULTILINE)


def test_yaml_loaders_do_not_import_undeclared_pyyaml():
    """No shipped loader may `import yaml` (PyYAML) — it is not a declared
    dependency (only ruamel.yaml is, per pyproject.toml). These loaders must
    use ruamel.yaml directly, not prefer an incidental transitive package."""
    offenders = []
    for rel in _TARGET_MODULES:
        src = (_YADGAR_ROOT / rel).read_text()
        if _BARE_PYYAML_IMPORT_RE.search(src):
            offenders.append(rel)
    assert not offenders, (
        f"modules still `import yaml` (undeclared PyYAML dependency): {offenders}. "
        "Use ruamel.yaml (yadgar's declared hard YAML dependency) instead."
    )


def test_pyproject_declares_no_pyyaml_dependency():
    """Guard the premise: pyproject.toml must not declare PyYAML as a base
    dependency. If this ever flips, the loaders above may legitimately prefer
    it again — but today ruamel.yaml is the only declared YAML dependency."""
    pyproject_src = (_YADGAR_ROOT.parent / "pyproject.toml").read_text()
    assert "ruamel.yaml" in pyproject_src
    assert re.search(r'"pyyaml', pyproject_src, re.IGNORECASE) is None


@pytest.fixture
def pyyaml_blocked(monkeypatch):
    """Force `import yaml` to raise ImportError regardless of whether PyYAML
    is actually installed in the environment running this test (e.g. a
    dev venv with the `ml` extra installed would otherwise mask the gap)."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    yield


def test_genesis_yaml_loads_with_pyyaml_absent(pyyaml_blocked):
    """_load_genesis_yaml must parse materials/agent_prompts.yaml correctly
    via ruamel.yaml alone when PyYAML is absent."""
    from yadgar.core.server.tools.agent_prompts import _load_genesis_yaml

    data = _load_genesis_yaml()
    assert isinstance(data, dict)
    assert "prompts" in data and len(data["prompts"]) >= 1
    assert "contract" in data
    assert "disciplines" in data
    for entry in data["prompts"]:
        assert {"pattern", "purpose", "content"} <= entry.keys()


def test_anchors_yaml_loads_with_pyyaml_absent(pyyaml_blocked):
    """_load_anchors_yaml must parse materials/anchors.yaml correctly via
    ruamel.yaml alone when PyYAML is absent."""
    from importlib.resources import files

    from yadgar.core.cli.seed import _load_anchors_yaml

    anchors_path = str(files("yadgar.core.seed").joinpath("materials").joinpath("anchors.yaml"))
    entries = _load_anchors_yaml(anchors_path)
    assert isinstance(entries, list)
    assert len(entries) >= 1
    for e in entries:
        assert "content" in e
        assert "tags" in e


def test_page_type_schemas_loads_with_pyyaml_absent(pyyaml_blocked):
    """_load_page_type_schemas must parse schemas/wiki_page_types.yaml
    correctly via ruamel.yaml alone when PyYAML is absent."""
    from yadgar._shared.wiki.wiki_meta import _load_page_type_schemas

    data = _load_page_type_schemas()
    assert isinstance(data, dict)
    assert "schema_version" in data
    assert "page_types" in data
    assert isinstance(data["page_types"], dict)
    assert len(data["page_types"]) >= 1
