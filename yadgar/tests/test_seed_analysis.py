"""Tests for yadgar/seed/_analysis.py — pure config-summarisation functions.

Coverage targets:
- _detect_stack: happy path + edge cases
- _summarize_structure: directory tree truncation
- _summarize_package_json: valid + malformed JSON
- _summarize_pyproject: valid + malformed TOML
- _summarize_cargo_toml: valid + malformed
- _summarize_go_mod: basic extraction
- _summarize_config: dispatcher
- _find_subproject_boundaries: monorepo detection
"""

from __future__ import annotations

import json

from yadgar.seed._analysis import (
    _detect_stack,
    _find_subproject_boundaries,
    _summarize_cargo_toml,
    _summarize_config,
    _summarize_go_mod,
    _summarize_package_json,
    _summarize_pyproject,
    _summarize_structure,
)

# ── _detect_stack ────────────────────────────────────────────────────────────


def test_detect_stack_from_configs():
    configs = [{"language": "Python"}, {"language": "TypeScript"}]
    stats = {"top_extensions": []}
    result = _detect_stack(configs, stats)
    assert "Python" in result
    assert "TypeScript" in result


def test_detect_stack_from_extensions():
    configs = []
    stats = {"top_extensions": [(".go", 10), (".rs", 5)]}
    result = _detect_stack(configs, stats)
    assert "Go" in result
    assert "Rust" in result


def test_detect_stack_empty():
    result = _detect_stack([], {"top_extensions": []})
    assert result == "Unknown"


def test_detect_stack_deduplicates():
    # Same language from both configs and extensions
    configs = [{"language": "Python"}]
    stats = {"top_extensions": [(".py", 50)]}
    result = _detect_stack(configs, stats)
    assert result.count("Python") == 1


# ── _summarize_structure ─────────────────────────────────────────────────────


def test_summarize_structure_basic():
    structure = {
        ".": ["README.md", "pyproject.toml"],
        "src": ["main.py", "utils.py"],
    }
    result = _summarize_structure(structure)
    assert "src" in result
    assert "main.py" in result


def test_summarize_structure_deep_dirs_truncated():
    # dirs at depth >= max_depth (3) are excluded
    structure = {
        "a/b/c/d": ["deep.py"],
        "a": ["shallow.py"],
    }
    result = _summarize_structure(structure, max_depth=2)
    assert "deep.py" not in result


def test_summarize_structure_many_files_truncated():
    structure = {".": ["f1.py", "f2.py", "f3.py", "f4.py", "f5.py", "f6.py", "f7.py"]}
    result = _summarize_structure(structure)
    # When >5 files, shows 4 + "+N more"
    assert "more" in result


def test_summarize_structure_empty():
    result = _summarize_structure({})
    assert result == ""


# ── _summarize_package_json ──────────────────────────────────────────────────


def test_summarize_package_json_happy():
    pkg = {
        "name": "my-app",
        "description": "A cool app",
        "version": "1.0.0",
        "scripts": {"start": "node index.js", "test": "jest"},
        "dependencies": {"react": "^18", "lodash": "^4"},
    }
    result = _summarize_package_json(json.dumps(pkg))
    assert "my-app" in result
    assert "A cool app" in result
    assert "react" in result


def test_summarize_package_json_with_workspaces_list():
    pkg = {"name": "mono", "workspaces": ["packages/a", "packages/b"]}
    result = _summarize_package_json(json.dumps(pkg))
    assert "packages/a" in result


def test_summarize_package_json_with_workspaces_dict():
    pkg = {"name": "mono", "workspaces": {"packages": ["pkg/x"]}}
    result = _summarize_package_json(json.dumps(pkg))
    assert "pkg/x" in result


def test_summarize_package_json_malformed():
    # Non-JSON falls back to truncation
    result = _summarize_package_json("not valid json {{{")
    assert len(result) <= 500


def test_summarize_package_json_dev_deps():
    pkg = {"devDependencies": {"jest": "^29", "typescript": "^5"}}
    result = _summarize_package_json(json.dumps(pkg))
    assert "jest" in result


# ── _summarize_pyproject ─────────────────────────────────────────────────────


_PYPROJECT_CONTENT = """
[project]
name = "myproject"
description = "A test project"
version = "2.0.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27", "fastapi>=0.109"]

[project.scripts]
myapp = "myapp.__main__:cli"

[build-system]
build-backend = "hatchling.build"
"""


def test_summarize_pyproject_happy():
    result = _summarize_pyproject(_PYPROJECT_CONTENT)
    assert "myproject" in result
    assert "A test project" in result
    assert "httpx" in result
    assert "hatchling" in result


def test_summarize_pyproject_scripts():
    result = _summarize_pyproject(_PYPROJECT_CONTENT)
    assert "myapp" in result


def test_summarize_pyproject_malformed():
    result = _summarize_pyproject("this is not toml !!!")
    # Falls back to truncation
    assert len(result) <= 800


def test_summarize_pyproject_empty():
    result = _summarize_pyproject("[tool.ruff]\nignore = []")
    # No project section — falls back to content truncation
    assert isinstance(result, str)


# ── _summarize_cargo_toml ─────────────────────────────────────────────────────


_CARGO_CONTENT = """
[package]
name = "mylib"
description = "A Rust library"
edition = "2021"

[dependencies]
serde = { version = "1" }
tokio = { version = "1" }

[dev-dependencies]
criterion = "0.5"

[workspace]
members = ["crates/a", "crates/b"]
"""


def test_summarize_cargo_toml_happy():
    result = _summarize_cargo_toml(_CARGO_CONTENT)
    assert "mylib" in result
    assert "serde" in result
    assert "criterion" in result


def test_summarize_cargo_toml_workspace():
    result = _summarize_cargo_toml(_CARGO_CONTENT)
    assert "crates/a" in result


def test_summarize_cargo_toml_malformed():
    result = _summarize_cargo_toml("[[[ not valid toml")
    assert len(result) <= 800


# ── _summarize_go_mod ─────────────────────────────────────────────────────────


_GO_MOD_CONTENT = """
module github.com/myorg/myapp

go 1.21

require (
    github.com/gin-gonic/gin v1.9.1
    golang.org/x/sync v0.6.0
)
"""


def test_summarize_go_mod_happy():
    result = _summarize_go_mod(_GO_MOD_CONTENT)
    assert "github.com/myorg/myapp" in result
    assert "1.21" in result
    assert "gin" in result


def test_summarize_go_mod_empty():
    result = _summarize_go_mod("# empty go.mod\n")
    # Falls back to truncation
    assert isinstance(result, str)


# ── _summarize_config dispatcher ─────────────────────────────────────────────


def test_summarize_config_package_json():
    cfg = {"path": "package.json", "content": '{"name":"x"}'}
    result = _summarize_config(cfg)
    assert "x" in result


def test_summarize_config_pyproject():
    cfg = {"path": "pyproject.toml", "content": _PYPROJECT_CONTENT}
    result = _summarize_config(cfg)
    assert "myproject" in result


def test_summarize_config_cargo():
    cfg = {"path": "Cargo.toml", "content": _CARGO_CONTENT}
    result = _summarize_config(cfg)
    assert "mylib" in result


def test_summarize_config_go_mod():
    cfg = {"path": "go.mod", "content": _GO_MOD_CONTENT}
    result = _summarize_config(cfg)
    assert "myapp" in result


def test_summarize_config_requirements_txt():
    cfg = {"path": "requirements.txt", "content": "requests>=2.28\nhttpx>=0.27\n# comment\n"}
    result = _summarize_config(cfg)
    assert "requests" in result
    assert "comment" not in result


def test_summarize_config_dockerfile():
    cfg = {"path": "Dockerfile", "content": "FROM python:3.14\nCOPY . /app\n"}
    result = _summarize_config(cfg)
    assert "FROM" in result


def test_summarize_config_unknown():
    # Generic fallback: truncate content
    cfg = {"path": "some.cfg", "content": "key=value\n" * 100}
    result = _summarize_config(cfg)
    assert len(result) <= 800


# ── _find_subproject_boundaries ──────────────────────────────────────────────


def test_find_subproject_boundaries_monorepo():
    structure = {"packages/a": ["index.js"], "packages/b": ["index.ts"], ".": ["package.json"]}
    configs = [
        {"path": "packages/a/package.json"},
        {"path": "packages/b/package.json"},
    ]
    result = _find_subproject_boundaries(structure, configs)
    assert "packages/a" in result
    assert "packages/b" in result


def test_find_subproject_boundaries_no_subprojects():
    structure = {"src": ["main.py"], ".": ["pyproject.toml"]}
    configs = []
    result = _find_subproject_boundaries(structure, configs)
    # Top-level dirs without config children are included
    assert isinstance(result, list)


def test_find_subproject_boundaries_top_dir_without_config():
    # Top-level dirs with no config-bearing children → included as boundary
    structure = {"docs": ["index.md"], "src": ["main.py"]}
    configs = []
    result = _find_subproject_boundaries(structure, configs)
    assert "docs" in result or "src" in result


def test_find_subproject_boundaries_empty():
    result = _find_subproject_boundaries({}, [])
    assert result == []
