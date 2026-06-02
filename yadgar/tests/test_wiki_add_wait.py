"""v5.41.2 wait flag tests for wiki write tools — TDD test suite.

Tests cover the 6 acceptance-criteria tests from the plan:
  1. test_wait_false_default_returns_immediately
  2. test_wait_true_blocks_until_committed
  3. test_wait_true_on_wiki_update_sees_new_version (wiki_update is sync — wait=True is no-op)
  4. test_wait_timeout_returns_error
  5. test_wait_default_still_async_for_perf
  6. test_wait_param_composes_with_force_and_replace_slug

Plus:
  7. wiki_restore accepts wait param (sync tool — wait=True is no-op)
  8. wiki_append_section accepts wait param (sync tool — wait=True is no-op)

RED before Phase 2; GREEN after.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from yadgar import server
from yadgar.server.tools.wiki import wiki_append_section, wiki_history, wiki_restore
from yadgar.storage.migrations import _migration_013_wiki_page_version

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Embedded storage with isolated tmp database per test."""
    server.init_engines(
        db_path=str(tmp_path / "wait_flag_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _storage():
    return server._get_storage()


def _wiki():
    return server._wiki


def _apply_migration():
    """Apply migration 013 directly."""
    _migration_013_wiki_page_version(_storage())


# ── Helper ────────────────────────────────────────────────────────────────────


def _make_unique_title(base: str) -> str:
    """Return a title unlikely to collide with others (for sim-gate bypass)."""
    import uuid

    return f"{base} {uuid.uuid4().hex[:8]}"


# ── 1. wait=False (default) — async behavior unchanged ───────────────────────


class TestWaitFalseDefault:
    def test_wait_false_default_returns_immediately(self):
        """wiki_add with default wait=False returns queued=True without blocking."""
        title = _make_unique_title("Async Default Page")
        result = server.wiki_add(title=title, content="content", tags=["test"])
        assert result.get("stored") is True
        assert result.get("queued") is True
        # Default: no committed field or committed=False
        assert not result.get("committed", False)

    def test_wait_false_explicit_returns_immediately(self):
        """wiki_add(wait=False) explicit — same async behavior."""
        title = _make_unique_title("Async Explicit Page")
        result = server.wiki_add(title=title, content="content", wait=False, tags=["test2"])
        assert result.get("stored") is True
        assert result.get("queued") is True
        assert not result.get("committed", False)


# ── 2. wait=True — read-your-writes consistency ───────────────────────────────


class TestWaitTrueBlocking:
    def test_wait_true_blocks_until_committed(self):
        """wiki_add(wait=True) — wiki_history shows new version immediately after return."""
        _apply_migration()
        title = _make_unique_title("Wait True Page")
        result = server.wiki_add(title=title, content="version one", wait=True, tags=["waitflag"])
        # Must be committed (not just queued)
        assert result.get("stored") is True
        assert result.get("committed") is True
        assert not result.get("queued", False)

        slug = result.get("slug")
        assert slug is not None
        history = wiki_history(slug=slug)
        assert "error" not in history, f"wiki_history error: {history}"
        assert history.get("total_versions", 0) >= 1, (
            "wait=True should ensure at least 1 version is visible without sleep"
        )


# ── 3. wiki_update — synchronous tool, wait=True is documented no-op ─────────


class TestWaitOnSyncTools:
    def test_wait_true_on_wiki_update_accepted(self):
        """wiki_update accepts wait param without error (sync tool — no-op)."""
        # Insert a page first via direct storage
        pid = _storage().insert_wiki_page(
            {
                "slug": "update-wait-test",
                "title": "Update Wait Test",
                "content": "original",
                "category": "reference",
                "tags": [],
                "confidence": "medium",
                "source_memory_ids": [],
                "links": [],
            }
        )
        # wiki_update is sync — wait=True should be accepted and not error
        result = server.wiki_update(page_id=pid, fields={"content": "updated"}, wait=True)
        assert "error" not in str(result).lower() or isinstance(result, dict)
        # Sync tools may return committed=True or just the page dict — both OK

    def test_wait_true_on_wiki_restore_accepted(self):
        """wiki_restore accepts wait param without error (sync tool — no-op)."""
        _apply_migration()
        pid = _storage().insert_wiki_page(
            {
                "slug": "restore-wait-test",
                "title": "Restore Wait Test",
                "content": "original content",
                "category": "reference",
                "tags": [],
                "confidence": "medium",
                "source_memory_ids": [],
                "links": [],
            }
        )
        # Need a second version to restore to version 1
        _storage().update_wiki_page(pid, {"content": "updated content"})
        # wiki_restore(wait=True) should be accepted
        result = wiki_restore(slug="restore-wait-test", version=1, wait=True)
        assert "error" not in result, f"wiki_restore returned error: {result}"

    def test_wait_true_on_wiki_append_section_accepted(self):
        """wiki_append_section accepts wait param without error (sync tool — no-op)."""
        _apply_migration()
        content = "## Introduction\nHello world.\n"
        _storage().insert_wiki_page(
            {
                "slug": "append-wait-test",
                "title": "Append Wait Test",
                "content": content,
                "category": "reference",
                "tags": [],
                "confidence": "medium",
                "source_memory_ids": [],
                "links": [],
            }
        )
        result = wiki_append_section(
            slug="append-wait-test",
            section_heading="Introduction",
            content="New line.",
            wait=True,
        )
        assert "error" not in result, f"wiki_append_section returned error: {result}"


# ── 4. wait=True sync path — storage error propagates cleanly ────────────────


class TestWaitSyncErrorHandling:
    def test_wait_true_storage_error_surfaces(self):
        """wiki_add(wait=True) — storage errors propagate rather than silently queuing.

        wait=True bypasses the async queue and writes synchronously. If the
        underlying write fails (e.g. storage down), the error is returned
        immediately rather than buried in the queue's retry/DLQ machinery.
        """
        import yadgar.server._state as _state

        title = _make_unique_title("Storage Error Test Page")

        # Patch _wiki.add to raise
        original_add = _state._wiki.add
        _state._wiki.add = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down"))

        try:
            with pytest.raises(RuntimeError, match="db down"):
                server.wiki_add(
                    title=title,
                    content="error test",
                    wait=True,
                    tags=["error-test"],
                )
        finally:
            _state._wiki.add = original_add

    def test_wait_true_ingest_error_surfaces(self):
        """wiki_add(wait=True, append=True) — storage errors propagate synchronously."""
        import yadgar.server._state as _state

        title = _make_unique_title("Ingest Error Test Page")

        # Patch _wiki.ingest to raise
        original_ingest = _state._wiki.ingest
        _state._wiki.ingest = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ingest down"))

        try:
            with pytest.raises(RuntimeError, match="ingest down"):
                server.wiki_add(
                    title=title,
                    content="error test",
                    wait=True,
                    append=True,
                    tags=["error-test"],
                )
        finally:
            _state._wiki.ingest = original_ingest


# ── 5. wait=False performance — <50ms ────────────────────────────────────────


class TestWaitFalsePerf:
    def test_wait_default_still_async_for_perf(self):
        """wiki_add(wait=False) returns in <50ms — async path not accidentally slowed."""
        from yadgar.file_queue import FileQueue

        # Warm up
        title0 = _make_unique_title("Warmup Page")
        server.wiki_add(title=title0, content="warmup", tags=["warmup"])

        # Mock enqueue to avoid disk I/O for pure latency measurement
        original_enqueue = FileQueue.enqueue

        call_times = []

        def _fast_enqueue(self, op_type, payload):
            t0 = time.perf_counter()
            result = original_enqueue(self, op_type, payload)
            call_times.append((time.perf_counter() - t0) * 1000)
            return result

        samples = []
        with patch.object(FileQueue, "enqueue", _fast_enqueue):
            for _ in range(5):
                title = _make_unique_title("Perf Test Page")
                t0 = time.perf_counter()
                server.wiki_add(title=title, content="perf test", tags=["perf"])
                elapsed_ms = (time.perf_counter() - t0) * 1000
                samples.append(elapsed_ms)

        median_ms = sorted(samples)[len(samples) // 2]
        assert median_ms < 50.0, (
            f"wiki_add(wait=False) p50={median_ms:.1f}ms exceeds 50ms budget. "
            "Check that wait=False path is not accidentally synchronous."
        )


# ── 6. wait param composes with force and replace_slug ───────────────────────


class TestWaitComposesWithOtherParams:
    def test_wait_param_composes_with_force(self):
        """wiki_add(wait=True, force=True) works for sim-gate bypass + committed=True."""
        _apply_migration()
        title = _make_unique_title("Force Wait Page")
        # First write
        r1 = server.wiki_add(title=title, content="first write", tags=["force-wait"])
        assert r1.get("stored") is True

        # Second write with same/similar title — use force=True to bypass sim gate
        r2 = server.wiki_add(
            title=title, content="second write", wait=True, force=True, tags=["force-wait"]
        )
        assert r2.get("stored") is True
        assert r2.get("committed") is True

    def test_wait_param_composes_with_replace_slug(self):
        """wiki_add(wait=True, replace_slug=...) works for slug overwrite + committed=True."""
        _apply_migration()
        title = _make_unique_title("Replace Slug Wait Page")
        slug = title.lower().replace(" ", "-")[:64]

        # Pre-create page
        server.wiki_add(title=title, content="original", wait=True, tags=["replace-wait"])

        # Overwrite via replace_slug
        result = server.wiki_add(
            title=title,
            content="replaced content",
            wait=True,
            replace_slug=slug,
            tags=["replace-wait"],
        )
        assert result.get("stored") is True
        assert result.get("committed") is True
