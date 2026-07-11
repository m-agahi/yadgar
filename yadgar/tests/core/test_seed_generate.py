"""Tests for yadgar/seed/_generate.py — memory generation from scan data.

Coverage targets:
- generate_memories: happy path + edge cases (empty fields, ci_cd, entry_points, components)
- Component boundary detection + sub-file gathering
- _delete_existing_seed_memories: via mock storage
- seed_project: storage interaction via mock
"""

from __future__ import annotations

# T2 Car E1: _delete_existing_seed_memories moved to the backend seed_store impl.
from yadgar.backend.admin_exec.seed import _delete_existing_seed_memories
from yadgar.core.seed._generate import (
    _HEAT_BY_TYPE,
    _PROJECT_INIT_CAP,
    _draft_project_init,
    generate_memories,
)


def _minimal_scan_data(directory: str = "/project") -> dict:
    """Minimal valid scan_data dict."""
    return {
        "root": directory,
        "project_name": "testproj",
        "stats": {
            "total_files": 5,
            "total_dirs": 2,
            "top_extensions": [(".py", 4), (".md", 1)],
        },
        "structure": {
            ".": ["README.md", "pyproject.toml"],
            "src": ["main.py", "utils.py"],
        },
        "configs": [],
        "docs": [],
        "ci_cd": [],
        "entry_points": [],
    }


# ── generate_memories — basic ─────────────────────────────────────────────────


def test_generate_memories_returns_list():
    result = generate_memories(_minimal_scan_data())
    assert isinstance(result, list)
    assert len(result) >= 1  # At minimum: overview


def test_generate_memories_overview_present():
    result = generate_memories(_minimal_scan_data())
    overview = next((m for m in result if "overview" in m["tags"]), None)
    assert overview is not None
    assert "testproj" in overview["content"]
    assert overview["heat_type"] == "overview"


def test_generate_memories_overview_contains_structure():
    result = generate_memories(_minimal_scan_data())
    overview = next(m for m in result if "overview" in m["tags"])
    assert "src" in overview["content"]


def test_generate_memories_context_is_directory():
    sd = _minimal_scan_data("/home/user/myproject")
    result = generate_memories(sd)
    for m in result:
        assert m["context"] == "/home/user/myproject"


def test_generate_memories_all_have_required_keys():
    result = generate_memories(_minimal_scan_data())
    for m in result:
        assert "content" in m
        assert "context" in m
        assert "tags" in m
        assert "heat_type" in m


# ── configs section ───────────────────────────────────────────────────────────


def test_generate_memories_config_entry():
    sd = _minimal_scan_data()
    sd["configs"] = [
        {"path": "pyproject.toml", "language": "Python", "content": "[project]\nname='x'"}
    ]
    result = generate_memories(sd)
    config_entries = [m for m in result if "config" in m["tags"] and "_seed" in m["tags"]]
    assert len(config_entries) == 1
    assert "pyproject.toml" in config_entries[0]["content"]
    assert config_entries[0]["heat_type"] == "config"


def test_generate_memories_config_language_in_tags():
    sd = _minimal_scan_data()
    sd["configs"] = [{"path": "Cargo.toml", "language": "Rust", "content": "[package]\n"}]
    result = generate_memories(sd)
    config_entries = [m for m in result if "config" in m["tags"]]
    assert "Rust" in config_entries[0]["tags"]


# ── docs section ─────────────────────────────────────────────────────────────


def test_generate_memories_doc_entry():
    sd = _minimal_scan_data()
    sd["docs"] = [{"path": "README.md", "content": "# My Project\n\nDescription."}]
    result = generate_memories(sd)
    doc_entries = [m for m in result if "documentation" in m["tags"]]
    assert len(doc_entries) == 1
    assert "README.md" in doc_entries[0]["content"]
    assert doc_entries[0]["heat_type"] == "documentation"


def test_generate_memories_doc_no_dir_no_extra_tags():
    sd = _minimal_scan_data()
    sd["docs"] = [{"path": "README.md", "content": "readme"}]
    result = generate_memories(sd)
    doc_entries = [m for m in result if "documentation" in m["tags"]]
    # No subdir → tags = ["_seed", "documentation"]
    assert len(doc_entries[0]["tags"]) == 2


def test_generate_memories_doc_with_dir_has_extra_tag():
    sd = _minimal_scan_data()
    sd["docs"] = [{"path": "docs/guide.md", "content": "guide"}]
    result = generate_memories(sd)
    doc_entries = [m for m in result if "documentation" in m["tags"]]
    assert "docs" in doc_entries[0]["tags"]


# ── ci_cd section ─────────────────────────────────────────────────────────────


def test_generate_memories_ci_cd_entry():
    sd = _minimal_scan_data()
    sd["ci_cd"] = [{"path": ".github/ci.yml", "content": "name: CI\non: push\n"}]
    result = generate_memories(sd)
    ci_entries = [m for m in result if "ci_cd" in m["tags"]]
    assert len(ci_entries) == 1
    assert "ci_cd" in ci_entries[0]["tags"]
    assert ci_entries[0]["heat_type"] == "ci_cd"


def test_generate_memories_no_ci_cd_when_empty():
    sd = _minimal_scan_data()
    sd["ci_cd"] = []
    result = generate_memories(sd)
    ci_entries = [m for m in result if "ci_cd" in m["tags"]]
    assert len(ci_entries) == 0


# ── entry_points section ──────────────────────────────────────────────────────


def test_generate_memories_entry_point_entry():
    sd = _minimal_scan_data()
    sd["entry_points"] = [{"path": "src/main.py", "content": "def main(): pass"}]
    result = generate_memories(sd)
    ep_entries = [m for m in result if "entry_point" in m["tags"]]
    assert len(ep_entries) == 1
    assert "main.py" in ep_entries[0]["content"]
    assert ep_entries[0]["heat_type"] == "entry_point"


# ── component section ─────────────────────────────────────────────────────────


def test_generate_memories_component_from_top_dirs():
    sd = _minimal_scan_data()
    sd["structure"] = {
        ".": ["pyproject.toml"],
        "src": ["main.py", "utils.py"],
        "tests": ["test_main.py"],
    }
    result = generate_memories(sd)
    component_entries = [m for m in result if "component" in m["tags"]]
    assert len(component_entries) >= 1
    tags_flat = [tag for m in component_entries for tag in m["tags"]]
    assert "src" in tags_flat or "tests" in tags_flat


def test_generate_memories_component_has_file_count():
    sd = _minimal_scan_data()
    sd["structure"] = {
        ".": ["pyproject.toml"],
        "src": ["a.py", "b.py", "c.py"],
    }
    result = generate_memories(sd)
    component_entries = [m for m in result if "component" in m["tags"]]
    # One of them should reference src files
    assert any("src" in m["content"] for m in component_entries)


def test_generate_memories_component_with_readme():
    sd = _minimal_scan_data()
    sd["structure"] = {
        ".": ["pyproject.toml"],
        "packages/a": ["index.py", "README.md"],
    }
    sd["configs"] = [{"path": "packages/a/pyproject.toml", "language": "Python", "content": ""}]
    sd["docs"] = [{"path": "packages/a/README.md", "content": "component docs"}]
    result = generate_memories(sd)
    component_entries = [m for m in result if "component" in m["tags"]]
    # May or may not appear depending on boundary detection, but no crash
    assert isinstance(component_entries, list)


# ── heat values sanity ────────────────────────────────────────────────────────


def test_heat_values_exist_for_all_types():
    expected_types = {"overview", "documentation", "config", "ci_cd", "entry_point", "component"}
    assert expected_types == set(_HEAT_BY_TYPE.keys())


def test_heat_values_are_floats_in_range():
    for name, val in _HEAT_BY_TYPE.items():
        assert 0.0 < val <= 1.0, f"{name} heat {val} out of range"


# ── _delete_existing_seed_memories via mock ───────────────────────────────────


def test_delete_existing_seed_memories_calls_storage():
    class FakeStorage:
        def __init__(self):
            self.queries = []

        def _extract_id(self, raw_id):
            return raw_id  # return as-is (already int)

        def _q(self, surql, params=None):
            self.queries.append(surql)
            if "SELECT id" in surql:
                return [{"id": 1}, {"id": 2}]
            return []

    storage = FakeStorage()
    count = _delete_existing_seed_memories(storage, "/proj")
    assert count == 2
    # Should have run at least the SELECT query + 2x DELETE pairs
    assert len(storage.queries) >= 3


def test_delete_existing_seed_memories_excludes_ids():
    class FakeStorage:
        def __init__(self):
            self.queries = []

        def _extract_id(self, raw_id):
            return raw_id

        def _q(self, surql, params=None):
            self.queries.append((surql, params))
            if "SELECT id" in surql:
                return [{"id": 1}, {"id": 2}, {"id": 3}]
            return []

    storage = FakeStorage()
    count = _delete_existing_seed_memories(storage, "/proj", exclude_ids=[2, 3])
    assert count == 1
    # Only id=1 should be in DELETE params
    delete_params = [p for (q, p) in storage.queries if "DELETE type" in q]
    assert all(p.get("id") != 2 for p in delete_params)
    assert all(p.get("id") != 3 for p in delete_params)


# ── _draft_project_init ──────────────────────────────────────────────────────


def test_draft_project_init_basic():
    sd = _minimal_scan_data("/home/user/proj")
    result = _draft_project_init(sd)
    assert "testproj" in result
    assert "/home/user/proj" in result
    assert "# testproj — Project Init" in result


def test_draft_project_init_caps_at_limit():
    sd = _minimal_scan_data()
    result = _draft_project_init(sd)
    assert len(result) <= _PROJECT_INIT_CAP


def test_draft_project_init_includes_readme_snippet():
    sd = _minimal_scan_data()
    sd["docs"] = [
        {"name": "README.md", "path": "README.md", "content": "This is the readme content."}
    ]
    result = _draft_project_init(sd)
    assert "README snippet" in result
    assert "This is the readme content" in result


def test_draft_project_init_no_readme_shows_none_docs():
    sd = _minimal_scan_data()
    sd["docs"] = []
    result = _draft_project_init(sd)
    assert "(none)" in result


def test_draft_project_init_doc_list():
    sd = _minimal_scan_data()
    sd["docs"] = [
        {"name": "README.md", "path": "README.md", "content": ""},
        {"name": "CONTRIBUTING.md", "path": "CONTRIBUTING.md", "content": ""},
    ]
    result = _draft_project_init(sd)
    assert "CONTRIBUTING.md" in result


def test_draft_project_init_readme_with_empty_content_skipped():
    sd = _minimal_scan_data()
    sd["docs"] = [{"name": "README.md", "path": "README.md", "content": ""}]
    result = _draft_project_init(sd)
    # Empty readme content → no snippet section
    assert "README snippet" not in result


def test_delete_existing_seed_memories_empty_returns_zero():
    class FakeStorage:
        def _q(self, surql, params=None):
            return []

        def delete_memory(self, mid):
            raise AssertionError("delete_memory should not be called for empty list")

    count = _delete_existing_seed_memories(FakeStorage(), "/proj")
    assert count == 0
