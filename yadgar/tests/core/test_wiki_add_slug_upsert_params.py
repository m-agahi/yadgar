"""Car C (#83): wiki_add MCP tool exposes slug + upsert params.

TDD RED-first suite.

Seam: wiki_add tool function (core/server/tools/wiki.py) must accept
``slug`` and ``upsert`` and thread them into the queued payload so the
drainer (wiki_add_impl.py) stores the page at the caller-supplied slug
(not title-derived).

Acceptance criteria:
  C1  slug param flows through to payload / returned slug.
  C2  wait=True + slug stores at caller slug, not title slug.
  C3  upsert=True second call overwrites (one page, version bumps).
  C4  upsert=False collision → rejected with reason="slug_exists".
  C5  No slug → backward-compat title-derived.
  C6  Docstring describes slug + upsert.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from yadgar.core import server

_TEST_DIR = "/home/max/git/yadgar"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Module-scoped engine fixture (real WikiStore + embeddings)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_add_slug_upsert")
    server.init_engines(
        db_path=str(tmp_path / "wiki_add_slug_upsert.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# Per-test drainer fixture (mirrors test_wiki_add_wait.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wire_drainer(tmp_path, _isolate_file_queue):
    """Wire a real FileQueue + QueueDrainer for wait=True path."""
    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.lifecycle.lifecycle as _cl
    from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

    real_fq = FileQueue(tmp_path / "slug_upsert_queue")
    drainer = QueueDrainer(
        queue=real_fq,
        storage_factory=lambda: _state_mod._storage,
        drain_interval=9999,
    )

    def _get_fq():
        return real_fq

    with (
        patch.object(_cl, "_get_file_queue", _get_fq),
        patch("yadgar.core.server.tools.wiki._get_file_queue", _get_fq),
        patch.object(_state_mod, "_queue_drainer", drainer),
        patch.object(_state_mod, "_file_queue", real_fq),
    ):
        yield


def _storage():
    return server._wiki._storage


# ---------------------------------------------------------------------------
# C1 — async (wait=False): slug param in returned dict
# ---------------------------------------------------------------------------


class TestSlugParamAsync:
    def test_c1_slug_in_returned_dict(self):
        """C1: wiki_add(slug='proj-mod-c1-async') wait=False returns caller slug."""
        caller_slug = f"proj-mod-c1-{_uid()}"
        result = server.wiki_add(
            title=f"Some Title {_uid()}",
            content="## Purpose\nX\n## Exports\nY\n## Design\nZ.",
            category="reference",
            slug=caller_slug,
            upsert=True,
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=False,
        )
        assert result.get("stored") is True
        # Async path returns the effective slug immediately.
        assert result.get("slug") == caller_slug, (
            f"Expected caller slug {caller_slug!r}, got {result.get('slug')!r}"
        )


# ---------------------------------------------------------------------------
# C2 — wait=True: page stored at caller slug (not title-derived)
# ---------------------------------------------------------------------------


class TestSlugParamWait:
    def test_c2_wait_true_stores_at_caller_slug(self):
        """C2: wiki_add(slug=…, wait=True) page lands at caller slug."""
        caller_slug = f"proj-mod-c2-{_uid()}"
        result = server.wiki_add(
            title=f"Completely Different Title {_uid()}",
            content="## Purpose\nA.\n## Exports\nB.\n## Design\nC.",
            category="reference",
            slug=caller_slug,
            upsert=True,
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=True,
        )
        assert result.get("stored") is not False, f"Write failed: {result}"
        # Returned slug must be the caller slug.
        assert result.get("slug") == caller_slug, (
            f"wait=True returned wrong slug: {result.get('slug')!r}, want {caller_slug!r}"
        )
        # Page must exist at caller slug.
        page = _storage().get_wiki_page_by_slug(caller_slug)
        assert page is not None, "page not stored at caller-supplied slug"
        assert page.get("slug") == caller_slug

    def test_c2_title_derived_slug_absent(self):
        """C2: title-derived slug must not be created when explicit slug given."""
        unique_title = f"Totally Unique Title For CarC Test {_uid()}"
        caller_slug = f"proj-mod-c2b-{_uid()}"
        server.wiki_add(
            title=unique_title,
            content="## Purpose\nP.\n## Exports\nE.\n## Design\nD.",
            category="reference",
            slug=caller_slug,
            upsert=True,
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=True,
        )
        # Title-derived slug must not exist.
        import re

        title_slug = re.sub(r"[^a-z0-9]+", "-", unique_title.lower()).strip("-")[:64]
        title_page = _storage().get_wiki_page_by_slug(title_slug)
        assert title_page is None, f"page leaked to title-derived slug {title_slug!r}"


# ---------------------------------------------------------------------------
# C3 — upsert=True second write overwrites
# ---------------------------------------------------------------------------


class TestUpsertOverwrite:
    def test_c3_second_write_overwrites(self):
        """C3: two wait=True writes at same slug → one page, updated content."""
        caller_slug = f"proj-mod-c3-{_uid()}"
        server.wiki_add(
            title=f"Gen One {_uid()}",
            content="## Purpose\nversion one.\n## Exports\nG.\n## Design\nA.",
            category="reference",
            slug=caller_slug,
            upsert=True,
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=True,
        )
        server.wiki_add(
            title=f"Gen Two {_uid()}",
            content="## Purpose\nversion two edited.\n## Exports\nG.\n## Design\nB.",
            category="reference",
            slug=caller_slug,
            upsert=True,
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=True,
        )
        page = _storage().get_wiki_page_by_slug(caller_slug)
        assert page is not None
        assert "version two edited" in page.get("content", "")
        assert "version one" not in page.get("content", "")


# ---------------------------------------------------------------------------
# C4 — upsert=False collision → rejected
# ---------------------------------------------------------------------------


class TestUpsertFalseRejects:
    def test_c4_upsert_false_collision_rejected_via_wait_path(self):
        """C4: wiki_add(upsert=False, wait=True) on existing slug → stored=False, reason=slug_exists.

        Car C adds a upsert=False slug-collision check in _sim_gate_for_drainer, BEFORE
        the policy dispatch, so the rejection surfaces through _handle_sim_rejection → DLQ
        sidecar → _wiki_add_wait_path returns {stored: False, reason: "slug_exists"}.
        End-to-end: tool enqueues payload → drainer fires gate → rejection surfaces.
        """
        caller_slug = f"proj-mod-c4-{_uid()}"
        # First write creates the page.
        first = server.wiki_add(
            title=f"Original C4 {_uid()}",
            content="## Purpose\noriginal.\n## Exports\nH.\n## Design\nC.",
            category="reference",
            slug=caller_slug,
            upsert=True,
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=True,
        )
        assert first.get("stored") is not False, f"Setup write failed: {first}"

        # Second write with upsert=False should be rejected via gate.
        result = server.wiki_add(
            title=f"Collision C4 {_uid()}",
            content="## Purpose\nSHOULD NOT LAND.\n## Exports\nH.\n## Design\nD.",
            category="reference",
            slug=caller_slug,
            upsert=False,
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=True,
        )
        assert result.get("stored") is False, f"Expected rejection, got: {result}"
        assert result.get("reason") == "slug_exists", f"Wrong reason: {result.get('reason')!r}"
        # Original content preserved.
        page = _storage().get_wiki_page_by_slug(caller_slug)
        assert page is not None
        assert "original" in page.get("content", "")
        assert "SHOULD NOT LAND" not in page.get("content", "")

    def test_c4_payload_carries_upsert_false(self):
        """C4b: wiki_add MCP tool includes upsert=False in the enqueued payload."""
        import yadgar._shared.runtime.state as _state_mod

        captured = []
        real_enqueue = _state_mod._file_queue.enqueue if _state_mod._file_queue else None

        def _spy_enqueue(op, payload):
            if op == "wiki_add":
                captured.append(payload.copy())
            if real_enqueue:
                return real_enqueue(op, payload)

        _state_mod._file_queue.enqueue = _spy_enqueue
        try:
            server.wiki_add(
                title=f"Payload Test {_uid()}",
                content="test content",
                category="reference",
                slug=f"proj-mod-c4b-{_uid()}",
                upsert=False,
                directory=_TEST_DIR,
                branch_hint="test-branch",
                wait=False,  # async; just check payload
            )
        finally:
            if real_enqueue:
                _state_mod._file_queue.enqueue = real_enqueue

        assert len(captured) == 1, "Expected exactly one wiki_add enqueue call"
        payload = captured[0]
        assert payload.get("upsert") is False, (
            f"Expected upsert=False in payload, got: {payload.get('upsert')!r}"
        )
        assert payload.get("slug", "").startswith("proj-mod-c4b-"), (
            f"Expected caller slug in payload, got: {payload.get('slug')!r}"
        )


# ---------------------------------------------------------------------------
# C5 — backward compat: no slug → title-derived
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_c5_no_slug_title_derived(self):
        """C5: no slug param → title-derived slug unchanged."""
        title = f"Legacy Title Page Car C {_uid()}"
        result = server.wiki_add(
            title=title,
            content="plain content",
            category="reference",
            directory=_TEST_DIR,
            branch_hint="test-branch",
            wait=True,
        )
        import re

        expected_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]
        assert (
            result.get("slug") == expected_slug
            or _storage().get_wiki_page_by_slug(expected_slug) is not None
        )


# ---------------------------------------------------------------------------
# C6 — docstring mentions slug + upsert
# ---------------------------------------------------------------------------


class TestDocstring:
    def test_c6_docstring_mentions_slug_and_upsert(self):
        """C6: wiki_add docstring must mention slug and upsert params."""
        doc = server.wiki_add.__doc__ or ""
        assert "slug" in doc, "wiki_add docstring missing 'slug'"
        assert "upsert" in doc, "wiki_add docstring missing 'upsert'"
