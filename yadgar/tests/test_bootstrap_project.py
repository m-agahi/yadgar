"""Tests for §23 bootstrap_project — _project_init memory pattern."""

import pytest

from yadgar.core import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("bootstrap_project")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


@pytest.fixture
def _flush(flush_queue):
    return flush_queue


# ── 2000-char cap enforcement ─────────────────────────────────────────────────


def test_bootstrap_project_raises_on_overflow():
    with pytest.raises(ValueError, match="2000"):
        server.bootstrap_project(directory="/tmp/proj", content="x" * 2001)


def test_bootstrap_project_rejects_exactly_one_over():
    with pytest.raises(ValueError):
        server.bootstrap_project(directory="/tmp/proj", content="a" * 2001)


def test_bootstrap_project_accepts_exactly_at_cap():
    # Should NOT raise for exactly 2000 chars
    result = server.bootstrap_project(directory="/tmp/proj", content="x" * 2000)
    assert result is not None


def test_bootstrap_project_accepts_content_under_cap():
    result = server.bootstrap_project(directory="/tmp/proj", content="# TOC\n- item1\n- item2")
    assert result is not None


# ── idempotent replace ────────────────────────────────────────────────────────


def test_bootstrap_project_idempotent_replace(flush_queue):
    directory = "/tmp/idempotent_bootstrap"
    server.bootstrap_project(directory=directory, content="# Version 1")
    flush_queue()
    server.bootstrap_project(directory=directory, content="# Version 2")
    flush_queue()

    # Exactly one _project_init memory should exist
    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_project_init' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1, f"Expected 1 _project_init memory, found {len(rows)}"
    assert "Version 2" in rows[0]["content"]


def test_bootstrap_project_deletes_all_existing(flush_queue):
    """If multiple _project_init rows exist (corruption), all are replaced."""
    directory = "/tmp/multi_bootstrap"
    # Create two _project_init memories manually
    storage = server._get_storage()
    storage.insert_memory(
        {
            "content": "# Old 1",
            "tags": ["_project_init", "_anchor"],
            "directory_context": directory,
            "heat": 1.0,
            "is_stale": False,
            "is_protected": True,
            "store_type": "semantic",
        }
    )
    storage.insert_memory(
        {
            "content": "# Old 2",
            "tags": ["_project_init", "_anchor"],
            "directory_context": directory,
            "heat": 1.0,
            "is_stale": False,
            "is_protected": True,
            "store_type": "semantic",
        }
    )
    flush_queue()

    # Now bootstrap — should remove both and insert one new
    server.bootstrap_project(directory=directory, content="# Fresh")
    flush_queue()

    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_project_init' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    assert "Fresh" in rows[0]["content"]


# ── return value ─────────────────────────────────────────────────────────────


def test_bootstrap_project_returns_memory_dict():
    result = server.bootstrap_project(directory="/tmp/ret_test", content="# Hello")
    assert isinstance(result, dict)
    assert "content" in result or "id" in result or "status" in result


# ── tag set ──────────────────────────────────────────────────────────────────


def test_bootstrap_project_tags(flush_queue):
    directory = "/tmp/tag_test"
    server.bootstrap_project(directory=directory, content="# Tagged")
    flush_queue()

    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_project_init' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    tags = rows[0].get("tags", [])
    assert "_project_init" in tags
    assert "_anchor" in tags


def test_bootstrap_project_store_type_semantic(flush_queue):
    directory = "/tmp/store_type_test"
    server.bootstrap_project(directory=directory, content="# Store type test")
    flush_queue()

    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_project_init' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    assert rows[0].get("store_type") == "semantic"


def test_bootstrap_project_is_protected(flush_queue):
    directory = "/tmp/protected_test"
    server.bootstrap_project(directory=directory, content="# Protected")
    flush_queue()

    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_project_init' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    assert rows[0].get("is_protected") is True
