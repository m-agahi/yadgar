"""Tests for §22 deprecated alias: get_project_context → project_brief.

Ensures backward compatibility for one release while emitting DeprecationWarning.
"""

import warnings

import pytest

from yadgar import server

pytestmark = pytest.mark.xdist_group("server_globals")


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def test_get_project_context_still_works():
    """Deprecated alias must still return a dict."""
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        result = server.get_project_context("/tmp/deprecated_test")
    assert isinstance(result, dict)


def test_get_project_context_emits_deprecation_warning():
    """Must emit DeprecationWarning when called."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        server.get_project_context("/tmp/deprecation_warn_test")

    deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
    assert len(deprecation_warnings) >= 1


def test_get_project_context_same_shape_as_project_brief():
    """Deprecated alias returns same payload shape as project_brief(mode='catalog')."""
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        deprecated_result = server.get_project_context("/tmp/shape_test")

    fresh_result = server.project_brief("/tmp/shape_test", mode="catalog")

    # Both should have the same top-level keys
    catalog_keys = {
        "_resolved_directory",
        "_mode",
        "project",
        "tech",
        "branch",
        "init_memory_present",
        "active_work_present",
        "top_anchors",
        "recent_episode_count",
        "stale_wiki_count",
    }
    assert catalog_keys.issubset(deprecated_result.keys()), (
        f"Deprecated alias missing keys: {catalog_keys - deprecated_result.keys()}"
    )
    assert catalog_keys.issubset(fresh_result.keys())


def test_get_project_context_mode_is_catalog():
    """Deprecated alias always returns catalog mode, never full."""
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        result = server.get_project_context("/tmp/mode_test")
    assert result["_mode"] == "catalog"


def test_get_project_context_no_full_fields():
    """Full-mode-only fields must NOT appear in deprecated alias result."""
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        result = server.get_project_context("/tmp/no_full_test")
    assert "init_memory" not in result
    assert "active_work" not in result
    assert "hot_memories" not in result
    assert "key_wiki_pages" not in result
