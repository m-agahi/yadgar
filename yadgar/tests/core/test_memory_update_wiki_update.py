"""§17 memory_update / wiki_update — patch by integer ID.

Tests:
- memory_update patches allowed fields (content, tags, is_protected, is_stale)
- memory_update rejects unknown/disallowed keys (heat, embedding, id, created_at)
- memory_update preserves heat, access_count, created_at
- wiki_update patches allowed fields (content, tags, category, confidence)
- wiki_update rejects unknown/disallowed keys (slug, id, created_at)
- Updates persist (re-fetch confirms change)
"""

import pytest

from yadgar.core import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("memory_update_wiki_updat")
    server.init_engines(
        db_path=str(tmp_path / "test_update.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.mark.usefixtures("admin_backend_bypass")
class TestMemoryUpdate:
    def _insert_memory(self, content="initial content"):
        return server._get_storage().insert_memory(
            {
                "content": content,
                "tags": ["original-tag"],
                "store_type": "episodic",
                "heat": 0.7,
                "directory_context": "/tmp/test",
            }
        )

    def test_memory_update_content(self):
        """memory_update patches content and persists the change."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"content": "updated content"})
        assert result["content"] == "updated content"
        refetched = server.memory_get(mid)
        assert refetched is not None
        assert refetched["content"] == "updated content"

    def test_memory_update_tags(self):
        """memory_update patches tags."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"tags": ["new-tag", "another"]})
        assert "new-tag" in result["tags"]
        refetched = server.memory_get(mid)
        assert refetched is not None
        assert "new-tag" in refetched["tags"]

    def test_memory_update_is_protected(self):
        """memory_update patches is_protected flag."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"is_protected": True})
        assert result["is_protected"] is True

    def test_memory_update_is_stale(self):
        """memory_update patches is_stale flag."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"is_stale": True})
        assert result["is_stale"] is True

    def test_memory_update_rejects_heat(self):
        """memory_update must reject 'heat' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="heat"):
            server.memory_update(mid, {"heat": 999.0})

    def test_memory_update_rejects_embedding(self):
        """memory_update must reject 'embedding' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="embedding"):
            server.memory_update(mid, {"embedding": b"\x00" * 16})

    def test_memory_update_rejects_id(self):
        """memory_update must reject 'id' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="id"):
            server.memory_update(mid, {"id": 42})

    def test_memory_update_rejects_created_at(self):
        """memory_update must reject 'created_at' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="created_at"):
            server.memory_update(mid, {"created_at": "2020-01-01"})

    def test_memory_update_rejects_unknown_key(self):
        """memory_update must reject completely unknown keys."""
        mid = self._insert_memory()
        with pytest.raises(ValueError):
            server.memory_update(mid, {"totally_unknown_field": "bad"})

    def test_memory_update_preserves_heat(self):
        """memory_update must not change heat."""
        mid = self._insert_memory()
        before = server.memory_get(mid)
        assert before is not None
        original_heat = before.get("heat")
        server.memory_update(mid, {"content": "changed content"})
        after = server.memory_get(mid)
        assert after is not None
        assert after.get("heat") == original_heat

    def test_memory_update_preserves_created_at(self):
        """memory_update must not change created_at."""
        mid = self._insert_memory()
        before = server.memory_get(mid)
        assert before is not None
        original_ca = before.get("created_at")
        server.memory_update(mid, {"content": "changed"})
        after = server.memory_get(mid)
        assert after is not None
        assert after.get("created_at") == original_ca

    def test_memory_update_returns_updated_record(self):
        """memory_update returns the updated dict."""
        mid = self._insert_memory("old")
        result = server.memory_update(mid, {"content": "new"})
        assert isinstance(result, dict)
        assert result["content"] == "new"


@pytest.mark.usefixtures("admin_backend_bypass")
class TestWikiUpdate:
    def _insert_wiki(self, slug="test-wiki-update", content="initial content"):
        return server._get_storage().insert_wiki_page(
            {
                "slug": slug,
                "title": "Test Wiki Update",
                "content": content,
                "tags": ["original"],
                "category": "test",
                "status": "approved",
                "confidence": 0.8,
            }
        )

    def test_wiki_update_content(self):
        """wiki_update patches content and persists the change."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"content": "updated wiki content"})
        assert result["content"] == "updated wiki content"
        refetched = server.wiki_get(pid)
        assert refetched is not None
        assert refetched["content"] == "updated wiki content"

    def test_wiki_update_tags(self):
        """wiki_update patches tags."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"tags": ["new-tag"]})
        assert "new-tag" in result["tags"]

    def test_wiki_update_category(self):
        """wiki_update patches category."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"category": "architecture"})
        assert result["category"] == "architecture"

    def test_wiki_update_confidence(self):
        """wiki_update patches confidence."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"confidence": 0.99})
        assert abs(result["confidence"] - 0.99) < 0.001

    def test_wiki_update_rejects_slug(self):
        """wiki_update must reject 'slug' key."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError, match="slug"):
            server.wiki_update(pid, {"slug": "new-slug"})

    def test_wiki_update_rejects_id(self):
        """wiki_update must reject 'id' key."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError, match="id"):
            server.wiki_update(pid, {"id": 99})

    def test_wiki_update_rejects_created_at(self):
        """wiki_update must reject 'created_at' key."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError, match="created_at"):
            server.wiki_update(pid, {"created_at": "2020-01-01"})

    def test_wiki_update_rejects_unknown_key(self):
        """wiki_update must reject completely unknown keys."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError):
            server.wiki_update(pid, {"not_a_real_field": "bad"})

    def test_wiki_update_preserves_created_at(self):
        """wiki_update must not change created_at."""
        pid = self._insert_wiki()
        before = server.wiki_get(pid)
        assert before is not None
        original_ca = before.get("created_at")
        server.wiki_update(pid, {"content": "changed"})
        after = server.wiki_get(pid)
        assert after is not None
        assert after.get("created_at") == original_ca

    def test_wiki_update_returns_updated_record(self):
        """wiki_update returns the updated dict."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"content": "new content"})
        assert isinstance(result, dict)
        assert result["content"] == "new content"

    # v5.41.0: version regression guards
    def test_wiki_update_produces_version_row(self):
        """Every wiki_update call produces a new wiki_page_version row (v5.41.0)."""
        from yadgar._shared.storage.migrations import (
            _migration_013_wiki_page_version,  # noqa: PLC0415
        )

        storage = server._get_storage()
        _migration_013_wiki_page_version(storage)  # DDL + seed for existing pages
        pid = self._insert_wiki("ver-update-test", "version update check")
        # pid already has version=1 from insert_wiki_page hook
        server.wiki_update(pid, {"content": "updated once"})
        server.wiki_update(pid, {"content": "updated twice"})

        rows = storage._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version ASC",
            {"p": pid},
        )
        # insert: version=1; update 1: version=2; update 2: version=3
        assert len(rows) >= 3, f"Expected ≥3 version rows, got {len(rows)}"
        versions = [r["version"] for r in rows]
        assert 1 in versions
        assert 2 in versions
        assert 3 in versions
