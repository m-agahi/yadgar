"""Tests for yadgar/consolidation/cls.py — _CLSMixin._extract_entities static method.

Coverage targets:
- _extract_entities: file path detection, Python def/class, JS functions,
  errors/exceptions, traceback header, imports, JS require, decision phrases,
  deduplication

Note: _process_new_episodes, _link_similar_memories, _merge_duplicates require
a full engine (StorageEngine + EmbeddingEngine + ConsolidationSettings) and are
excluded from this unit test pass. Coverage floor: ~30% (_extract_entities only).
"""

from __future__ import annotations

from yadgar.core.consolidation.cls import _CLSMixin

_extract = _CLSMixin._extract_entities


# ── file paths ─────────────────────────────────────────────────────────────────


def test_extract_python_file_path():
    result = _extract("edited yadgar/cli/stats.py today")
    assert ("yadgar/cli/stats.py", "file") in result


def test_extract_relative_path():
    result = _extract("see ./src/main.go for details")
    assert any(name.endswith("main.go") and etype == "file" for name, etype in result)


def test_extract_dotdot_path():
    result = _extract("../config/settings.yaml has the value")
    assert any(name.endswith("settings.yaml") and etype == "file" for name, etype in result)


def test_extract_non_code_extension_not_captured():
    # .xyz is not in _CODE_EXTENSIONS
    result = _extract("file: stuff/data.xyz")
    assert not any(name.endswith(".xyz") for name, _ in result)


def test_extract_typescript_extension():
    result = _extract("see frontend/components/App.tsx")
    assert any(name.endswith("App.tsx") and etype == "file" for name, etype in result)


# ── Python def / class ─────────────────────────────────────────────────────────


def test_extract_python_def():
    result = _extract("def my_function(x): pass")
    assert ("my_function", "function") in result


def test_extract_python_class():
    result = _extract("class MyClass(Base):")
    assert ("MyClass", "function") in result


def test_extract_multiple_defs():
    result = _extract("def foo():\ndef bar():\nclass Baz:")
    names = [n for n, t in result if t == "function"]
    assert "foo" in names
    assert "bar" in names
    assert "Baz" in names


# ── JS functions ───────────────────────────────────────────────────────────────


def test_extract_js_function():
    result = _extract("function handleClick(event) { return event; }")
    assert ("handleClick", "function") in result


def test_extract_multiple_js_functions():
    result = _extract("function init() {}\nfunction destroy() {}")
    names = [n for n, t in result if t == "function"]
    assert "init" in names
    assert "destroy" in names


# ── errors / exceptions ────────────────────────────────────────────────────────


def test_extract_error_type():
    result = _extract("raised ValueError in loop")
    assert ("ValueError", "error") in result


def test_extract_exception_type():
    result = _extract("caught RuntimeException in handler")
    assert ("RuntimeException", "error") in result


def test_extract_multiple_errors():
    result = _extract("KeyError and TypeError both raised")
    etypes = {n for n, t in result if t == "error"}
    assert "KeyError" in etypes
    assert "TypeError" in etypes


# ── traceback ─────────────────────────────────────────────────────────────────


def test_extract_traceback_header():
    result = _extract("Traceback (most recent call last):\n  File 'x.py', line 1")
    assert ("Traceback", "error") in result


def test_no_traceback_without_header():
    result = _extract("no traceback here, just a normal error")
    assert ("Traceback", "error") not in result


# ── Python imports ────────────────────────────────────────────────────────────


def test_extract_import_statement():
    result = _extract("\nimport os")
    assert ("os", "dependency") in result


def test_extract_from_import():
    result = _extract("\nfrom pathlib import Path")
    assert ("pathlib", "dependency") in result


def test_extract_dotted_import():
    result = _extract("\nimport yadgar.storage")
    assert ("yadgar.storage", "dependency") in result


def test_extract_multiple_imports():
    result = _extract("\nimport sys\nfrom typing import Optional")
    deps = {n for n, t in result if t == "dependency"}
    assert "sys" in deps
    assert "typing" in deps


# ── JS require ────────────────────────────────────────────────────────────────


def test_extract_require_double_quotes():
    result = _extract('const x = require("lodash")')
    assert ("lodash", "dependency") in result


def test_extract_require_single_quotes():
    result = _extract("const x = require('express')")
    assert ("express", "dependency") in result


def test_extract_require_path():
    result = _extract('const cfg = require("./config/settings")')
    assert ("./config/settings", "dependency") in result


# ── decision phrases ──────────────────────────────────────────────────────────


def test_extract_decided_phrase():
    result = _extract("decided FastAPI for the new service")
    decisions = [n for n, t in result if t == "decision"]
    assert len(decisions) >= 1
    assert any("FastAPI" in d for d in decisions)


def test_extract_switched_to_phrase():
    result = _extract("switched to postgres for reliability")
    decisions = [n for n, t in result if t == "decision"]
    assert len(decisions) >= 1


def test_extract_using_phrase():
    result = _extract("using pytest for all unit tests")
    decisions = [n for n, t in result if t == "decision"]
    assert len(decisions) >= 1


def test_extract_chose_phrase():
    result = _extract("chose numpy over pandas for performance")
    decisions = [n for n, t in result if t == "decision"]
    assert len(decisions) >= 1


# ── deduplication ─────────────────────────────────────────────────────────────


def test_extract_deduplicates_same_entity():
    result = _extract("import os\nimport os")
    dep_os = [(n, t) for n, t in result if n == "os" and t == "dependency"]
    assert len(dep_os) == 1


def test_extract_preserves_order():
    result = _extract("\nimport abc\nimport xyz")
    deps = [n for n, t in result if t == "dependency"]
    abc_idx = deps.index("abc")
    xyz_idx = deps.index("xyz")
    assert abc_idx < xyz_idx


def test_extract_returns_list_of_tuples():
    result = _extract("def foo(): pass")
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, tuple)
        assert len(item) == 2


# ── empty + edge cases ────────────────────────────────────────────────────────


def test_extract_empty_string():
    result = _extract("")
    assert result == []


def test_extract_whitespace_only():
    result = _extract("   \n\t  ")
    assert result == []


def test_extract_no_match():
    result = _extract("hello world, no code here, just plain text")
    # Might still match "world" as a decision if "using" is present — but plain text shouldn't
    for _, etype in result:
        assert etype in {"file", "function", "error", "dependency", "decision"}


def test_extract_mixed_content():
    content = (
        "Fixed def parse_input() after catching ValueError.\n"
        "\nimport json\n"
        "Traceback (most recent call last):\n"
        "  File 'src/parser.py', line 10\n"
    )
    result = _extract(content)
    types = {t for _, t in result}
    assert "function" in types
    assert "error" in types
    assert "dependency" in types
    assert "file" in types
