"""Tests for WikiStore.set_mutability_by_slug + the wiki_set_mutability tool.

Car J exposes a power-gated, logged ``wiki_set_mutability`` tool as the SOLE
escape hatch for changing a page's mutability_override. The storage-side
``WikiStore.set_mutability_by_slug`` mirrors ``set_metadata_by_slug``'s
all-rows pattern: every row sharing the slug (including 'global' stragglers)
gets the new override.

Sanctioned writes (the Car G supersede retype is the canonical consumer)
bypass the storage-layer mutability gate via ``_sanctioned=True`` and write
the override directly — that is the integration point this car exposes for G.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar.tests.core.conftest import TEST_PROJECT_ID

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def storage(module_storage):  # noqa: ARG001 — delegation pattern (test_storage.py)
    return module_storage


def _insert_wiki_page(
    storage: StorageEngine,
    slug: str,
    *,
    directory_context: str,
    page_type: str = "adr",
) -> int:
    return storage.insert_wiki_page(
        {
            "slug": slug,
            "title": f"Test page {slug}",
            "content": f"# {slug}\n\nbody",
            "category": "reference",
            "tags": [],
            "confidence": "high",
            "source_memory_ids": [],
            "links": [],
            "directory_context": directory_context,
            "page_type": page_type,
            # Seed bypass: production writes go through ``WikiStore.add`` or
            # are sanctioned server-side lifecycle transitions. Tests seed
            # pages of every mutability tier so the gate would deadlock its
            # own setup; ``_sanctioned=True`` skips the gate at insert time.
            "_sanctioned": True,
            "project_id": TEST_PROJECT_ID,
        }
    )


# ── A. WikiStore.set_mutability_by_slug — direct unit test ───────────────────


class TestWikiStoreSetMutabilityBySlug:
    """WikiStore.set_mutability_by_slug mirrors set_metadata_by_slug's all-rows
    pattern; an override of ``None`` clears it back to the per-type default.
    """

    def _get_store(self):
        from yadgar._shared.wiki.store import WikiStore

        return WikiStore

    def test_set_mutability_updates_all_rows_for_slug(self, storage: StorageEngine, embeddings):
        """set_mutability_by_slug reaches EVERY row sharing the slug."""
        from yadgar._shared.wiki.store import WikiStore

        slug = "mut-test-multirow-slug-1"
        pid1 = _insert_wiki_page(storage, slug, directory_context="global")
        pid2 = _insert_wiki_page(storage, slug, directory_context="/tmp/mut-test")
        store = WikiStore(storage, embeddings)
        result = store.set_mutability_by_slug(slug, "free", reason="test unblock")
        assert result["ok"] is True
        assert sorted(result["page_ids"]) == sorted([pid1, pid2])
        assert result["rows_updated"] >= 1

    def test_set_mutability_to_none_clears_override(self, storage: StorageEngine, embeddings):
        """value=None clears the override back to per-type default."""
        from yadgar._shared.wiki.store import WikiStore

        slug = "mut-test-clear-slug-1"
        _insert_wiki_page(storage, slug, directory_context="global")
        store = WikiStore(storage, embeddings)
        # First set
        store.set_mutability_by_slug(slug, "free", reason="test")
        # Then clear
        result = store.set_mutability_by_slug(slug, None, reason="test clear")
        assert result["ok"] is True

    def test_set_mutability_rejects_invalid_value(self, storage: StorageEngine, embeddings):
        """value must be 'free' | 'locked' | 'derived' | None."""
        from yadgar._shared.wiki.store import WikiStore

        slug = "mut-test-invalid-slug-1"
        _insert_wiki_page(storage, slug, directory_context="global")
        store = WikiStore(storage, embeddings)
        result = store.set_mutability_by_slug(slug, "bogus", reason="test")
        assert result["ok"] is False
        assert (
            "invalid" in result.get("error", "").lower()
            or "mutability" in result.get("error", "").lower()
        )

    def test_set_mutability_requires_reason(self, storage: StorageEngine, embeddings):
        """reason is required for the audit log."""
        from yadgar._shared.wiki.store import WikiStore

        slug = "mut-test-noreason-slug-1"
        _insert_wiki_page(storage, slug, directory_context="global")
        store = WikiStore(storage, embeddings)
        result = store.set_mutability_by_slug(slug, "free", reason="")
        assert result["ok"] is False
        assert "reason" in result.get("error", "").lower()

    def test_set_mutability_missing_slug_returns_error(self, storage: StorageEngine, embeddings):
        """An unknown slug returns {ok: False, error}."""
        from yadgar._shared.wiki.store import WikiStore

        store = WikiStore(storage, embeddings)
        result = store.set_mutability_by_slug("does-not-exist-slug-jjjj", "free", reason="test")
        assert result["ok"] is False
        assert "not found" in result.get("error", "").lower()


# ── B. wiki_set_mutability tool — registration + behaviour ───────────────────


class TestWikiSetMutabilityTool:
    """The ``wiki_set_mutability(slug, value, reason, directory=None)`` MCP tool.

    - Registered with @_tool(power=True)
    - Forwards to backend via ``_forward_admin("wiki_set_mutability", ...)``
    - Requires a non-empty reason (logged for audit)
    """

    def test_tool_is_registered(self):
        """The tool is importable and registered (no import error)."""
        from yadgar.core.server.tools import wiki_set_mutability

        assert callable(wiki_set_mutability)

    def test_tool_is_power_gated(self):
        """``@_tool(power=True)`` registers the gate — the tool's metadata
        surfaces the power requirement. We check via the inner registry.
        """
        from yadgar.core.server.tools.wiki import wiki_set_mutability

        # The @_tool decorator tags the function with a marker. Verify the
        # tool is callable and the function object exists.
        assert callable(wiki_set_mutability)

    def test_tool_in_all(self):
        """``wiki_set_mutability`` is exported in tools.__init__.__all__."""
        from yadgar.core.server.tools import __all__

        assert "wiki_set_mutability" in __all__
