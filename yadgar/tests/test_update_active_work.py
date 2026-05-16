"""Tests for §24 update_active_work — _active_work memory pattern."""

import pytest

from yadgar import server

pytestmark = pytest.mark.xdist_group("server_globals")


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── basic creation ────────────────────────────────────────────────────────────


def test_update_active_work_returns_dict():
    result = server.update_active_work(directory="/tmp/aw_test", content="## Working on X")
    assert isinstance(result, dict)
    assert "new_memory" in result


def test_update_active_work_previous_content_none_on_first_call():
    result = server.update_active_work(directory="/tmp/aw_fresh", content="## Fresh start")
    assert result["previous_content"] is None


def test_update_active_work_new_memory_has_content():
    result = server.update_active_work(directory="/tmp/aw_content", content="## Task alpha")
    new_mem = result["new_memory"]
    assert isinstance(new_mem, dict)
    # content should be present in new_memory
    assert "content" in new_mem or "id" in new_mem


# ── atomic replace ────────────────────────────────────────────────────────────


def test_update_active_work_atomic_replace(flush_queue):
    directory = "/tmp/aw_atomic"

    server.update_active_work(directory=directory, content="## Step 1")
    flush_queue()
    server.update_active_work(directory=directory, content="## Step 2")
    flush_queue()

    # Only one _active_work memory should exist
    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_active_work' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    assert "Step 2" in rows[0]["content"]


def test_update_active_work_previous_content_returned(flush_queue):
    directory = "/tmp/aw_prev"
    server.update_active_work(directory=directory, content="## First")
    flush_queue()

    result = server.update_active_work(directory=directory, content="## Second")
    flush_queue()

    assert result["previous_content"] is not None
    assert "First" in result["previous_content"]


def test_update_active_work_idempotent_three_calls(flush_queue):
    """Multiple updates — always exactly one _active_work row."""
    directory = "/tmp/aw_idem"
    for i in range(3):
        server.update_active_work(directory=directory, content=f"## Iteration {i}")
        flush_queue()

    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_active_work' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    assert "Iteration 2" in rows[0]["content"]


# ── tag set ──────────────────────────────────────────────────────────────────


def test_update_active_work_tags(flush_queue):
    directory = "/tmp/aw_tags"
    server.update_active_work(directory=directory, content="## Tagged work")
    flush_queue()

    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_active_work' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    tags = rows[0].get("tags", [])
    assert "_active_work" in tags


def test_update_active_work_store_type_episodic(flush_queue):
    directory = "/tmp/aw_store"
    server.update_active_work(directory=directory, content="## Episodic")
    flush_queue()

    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_active_work' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    assert rows[0].get("store_type") == "episodic"


def test_update_active_work_is_protected(flush_queue):
    directory = "/tmp/aw_prot"
    server.update_active_work(directory=directory, content="## Protected")
    flush_queue()

    storage = server._get_storage()
    rows = storage._q(
        "SELECT * FROM memory WHERE directory_context = $dir AND '_active_work' INSIDE tags",
        {"dir": directory},
    )
    assert len(rows) == 1
    assert rows[0].get("is_protected") is True


# ── no char cap (unlike bootstrap_project) ───────────────────────────────────


def test_update_active_work_no_char_cap():
    """update_active_work has no character cap — large content is accepted."""
    big_content = "## Work\n" + ("- item\n" * 500)
    # Should not raise
    result = server.update_active_work(directory="/tmp/aw_big", content=big_content)
    assert result is not None
