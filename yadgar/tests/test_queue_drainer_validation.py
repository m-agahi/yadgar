"""Tests for §26 Queue Drainer Validation (Option Z).

TDD — written BEFORE the implementation.
Covers:
- branch fill (placeholder "master" when absent)
- v4.9 degenerate filter applied to wiki_add ops
- required fields validated (slug, title, content, category)
- schema_version < 2 → DLQ
- DLQ on format drift
"""

from __future__ import annotations

import pytest

from yadgar import server

MIN_WIKI_SCHEMA = 2


# ── helpers ──────────────────────────────────────────────────────────────────


# Default directory_context injected by _make_wiki_op (v5.42.5 NOT NULL constraint).
_DEFAULT_DIR_CTX = "/test/sandbox"


def _make_wiki_op(
    *,
    slug: str = "test-slug",
    title: str | None = None,
    content: str = "Meaningful content about the architecture.",
    category: str = "reference",
    tags: list | None = None,
    branch: str | None = None,
    schema_version: int | None = 2,
    extra: dict | None = None,
) -> dict:
    """Build a wiki_add queue operation dict.

    NOTE: wiki_add derives slug from title via slugify(title). To get a
    predictable slug, pass a title that slugifies to the desired value:
    e.g. title="test-slug" → slug="test-slug".

    directory_context defaults to _DEFAULT_DIR_CTX to satisfy the schema NOT NULL
    constraint (migration 018). Override via extra={"directory_context": "..."}.
    """
    if title is None:
        # Build a title that slugifies to the given slug
        title = slug  # slugify("test-slug") = "test-slug" since dashes are kept
    payload: dict = {
        "slug": slug,
        "title": title,
        "content": content,
        "category": category,
        "tags": tags or [],
        "directory_context": _DEFAULT_DIR_CTX,
    }
    if branch is not None:
        payload["branch"] = branch
    if schema_version is not None:
        payload["wiki_schema_version"] = schema_version
    if extra:
        payload.update(extra)
    return {"op": "wiki_add", "payload": payload}


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("queue_drainer_validation")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


@pytest.fixture
def queue_and_drainer(tmp_path):
    """Return the server's FileQueue and QueueDrainer for tests.

    The autouse _isolate_file_queue already set YADGAR_DATA_DIR to a
    per-test tmp path, so accessing _get_file_queue() + _queue_drainer
    gives isolated instances.
    """
    fq = server._get_file_queue()
    drainer = server._queue_drainer
    yield fq, drainer


# ── branch fill ───────────────────────────────────────────────────────────────


def test_branch_left_as_none_when_absent(queue_and_drainer, flush_queue):
    """wiki_add ops with _internal=True + no branch go to canonical slot (branch=None).

    v5.42.2: drainer no longer injects branch='master'. Absent branch = None (canonical slot).
    v5.42.3: external payloads without branch are rejected. _internal=True is the carve-out
    for system/migration writes that legitimately target the canonical NULL-branch slot.
    """
    fq, drainer = queue_and_drainer

    # v5.42.3: use _internal=True for canonical-slot writes without branch context
    op = _make_wiki_op(branch=None)
    assert "branch" not in op["payload"]
    op["payload"]["_internal"] = True  # explicit canonical-slot write carve-out

    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug, branch FROM wiki_page WHERE slug = 'test-slug'")
    assert rows, "wiki page should have been inserted"
    assert rows[0].get("branch") is None, (
        f"expected branch=None (canonical slot), got {rows[0].get('branch')!r}"
    )


def test_branch_not_overwritten_when_present(queue_and_drainer, flush_queue):
    """Explicit branch field is preserved, not overwritten with 'master'."""
    fq, drainer = queue_and_drainer

    op = _make_wiki_op(branch="feat/something", slug="slug-with-branch")
    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug, branch FROM wiki_page WHERE slug = 'slug-with-branch'")
    assert rows, "wiki page should have been inserted"
    assert rows[0].get("branch") == "feat/something"


# ── degenerate filter ─────────────────────────────────────────────────────────


def test_degenerate_content_routed_to_dlq(queue_and_drainer, tmp_path):
    """wiki_add ops with degenerate content go to DLQ, not DB."""
    fq, drainer = queue_and_drainer

    # Produce degenerate content that triggers the v4.9 filter.
    # The filter matches: Recurring pattern prefix + "frequently modified together" body.
    degenerate = "Recurring pattern across 42 observations: frequently modified together"

    op = _make_wiki_op(content=degenerate, slug="degen-slug")
    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug FROM wiki_page WHERE slug = 'degen-slug'")
    assert rows == [], "degenerate page must NOT be inserted"

    dlq_files = list(fq.dlq_dir.glob("*.json"))
    # At least one DLQ entry (either the main file or its sidecar)
    assert len(dlq_files) >= 1, "degenerate op must be in DLQ"


# ── required fields validation ────────────────────────────────────────────────


@pytest.mark.parametrize("missing_field", ["slug", "title", "content", "category"])
def test_missing_required_field_goes_to_dlq(queue_and_drainer, missing_field, tmp_path):
    """wiki_add ops missing required fields go to DLQ."""
    fq, drainer = queue_and_drainer

    payload = {
        "slug": "req-slug",
        "title": "Req Title",
        "content": "Some content",
        "category": "reference",
        "wiki_schema_version": 2,
        "directory_context": "/test/sandbox",
        "branch": "feat/test",
    }
    del payload[missing_field]
    # Use a unique slug (or no slug) so we can check absence
    unique_slug = "req-slug" if missing_field != "slug" else "__no_slug__"

    fq.enqueue("wiki_add", payload)
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q(f"SELECT slug FROM wiki_page WHERE slug = '{unique_slug}'")
    assert rows == [], f"op missing '{missing_field}' must not be inserted"

    dlq_files = list(fq.dlq_dir.glob("*"))
    assert len(dlq_files) >= 1, f"op missing '{missing_field}' must be in DLQ"


# ── schema_version gate ───────────────────────────────────────────────────────


def test_schema_version_less_than_2_goes_to_dlq(queue_and_drainer):
    """wiki_add ops with wiki_schema_version < 2 go to DLQ."""
    fq, drainer = queue_and_drainer

    op = _make_wiki_op(schema_version=1, slug="old-schema-slug")
    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug FROM wiki_page WHERE slug = 'old-schema-slug'")
    assert rows == [], "schema_version=1 op must NOT be inserted"

    dlq_files = list(fq.dlq_dir.glob("*"))
    assert len(dlq_files) >= 1, "schema_version=1 op must be in DLQ"


def test_schema_version_0_goes_to_dlq(queue_and_drainer):
    """wiki_schema_version=0 → DLQ."""
    fq, drainer = queue_and_drainer

    op = _make_wiki_op(schema_version=0, slug="v0-slug")
    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug FROM wiki_page WHERE slug = 'v0-slug'")
    assert rows == []


def test_schema_version_2_accepted(queue_and_drainer, flush_queue):
    """wiki_schema_version=2 is accepted and inserted (with branch, v5.42.3)."""
    fq, drainer = queue_and_drainer

    op = _make_wiki_op(schema_version=2, slug="v2-slug", branch="feat/schema-test")
    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug FROM wiki_page WHERE slug = 'v2-slug'")
    assert rows, "schema_version=2 op must be inserted"


def test_schema_version_3_accepted(queue_and_drainer, flush_queue):
    """wiki_schema_version >= 2 (e.g. 3) is accepted (with branch, v5.42.3)."""
    fq, drainer = queue_and_drainer

    op = _make_wiki_op(schema_version=3, slug="v3-slug", branch="feat/schema-test")
    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug FROM wiki_page WHERE slug = 'v3-slug'")
    assert rows, "schema_version=3 op must be inserted"


def test_no_schema_version_field_uses_default(queue_and_drainer, flush_queue):
    """wiki_add op without wiki_schema_version field uses schema_version=None → treated as old, DLQ."""
    fq, drainer = queue_and_drainer

    op = _make_wiki_op(schema_version=None, slug="no-schema-slug")
    # Remove schema_version key entirely
    op["payload"].pop("wiki_schema_version", None)
    fq.enqueue("wiki_add", op["payload"])
    drainer.drain_now()

    storage = server._get_storage()
    rows = storage._q("SELECT slug FROM wiki_page WHERE slug = 'no-schema-slug'")
    # Missing schema_version is treated as 0 → DLQ
    assert rows == [], "op without wiki_schema_version must be rejected"
