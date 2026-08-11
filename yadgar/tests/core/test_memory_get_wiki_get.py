"""§17 memory_get / wiki_get — fetch by integer ID.

Tests:
- memory_get returns dict with correct fields for existing ID
- memory_get returns None for missing ID
- memory_get response has no 'embedding' bytes field
- wiki_get returns dict with correct fields for existing ID
- wiki_get returns None for missing ID
- wiki_get response has no 'embedding' bytes field
"""

import pytest

from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("memory_get_wiki_get")
    server.init_engines(
        db_path=str(tmp_path / "test_get.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _mem(content: str, **kwargs) -> dict:
    base = {
        "content": content,
        "tags": [],
        "store_type": "episodic",
        "heat": 0.5,
        "directory_context": "/tmp/test",
        "project_id": TEST_PROJECT_ID,
    }
    base.update(kwargs)
    return base


class TestMemoryGet:
    def test_memory_get_returns_dict_by_id(self):
        """memory_get returns a dict when ID exists."""
        mem_id = server._get_storage().insert_memory(
            _mem("hello from memory_get test", tags=["test"])
        )
        result = server.memory_get(mem_id)
        assert result is not None
        assert isinstance(result, dict)
        assert result["content"] == "hello from memory_get test"

    def test_memory_get_returns_none_for_missing_id(self):
        """memory_get returns None when ID does not exist."""
        result = server.memory_get(999999999)
        assert result is None

    def test_memory_get_strips_embedding_bytes(self):
        """memory_get must not return raw embedding bytes in the response."""
        mem_id = server._get_storage().insert_memory(_mem("embedding strip test"))
        result = server.memory_get(mem_id)
        assert result is not None
        # embedding should be absent or not be bytes
        emb = result.get("embedding")
        assert not isinstance(emb, (bytes, bytearray)), (
            f"embedding must not be raw bytes, got {type(emb)}"
        )

    def test_memory_get_returns_correct_id(self):
        """memory_get returns the right record by ID."""
        id_a = server._get_storage().insert_memory(_mem("memory A"))
        id_b = server._get_storage().insert_memory(_mem("memory B"))
        result_a = server.memory_get(id_a)
        result_b = server.memory_get(id_b)
        assert result_a is not None
        assert result_b is not None
        assert result_a["content"] == "memory A"
        assert result_b["content"] == "memory B"


class TestWikiGet:
    def test_wiki_get_returns_dict_by_id(self):
        """wiki_get returns a dict when page ID exists."""
        page_id = server._get_storage().insert_wiki_page(
            {
                "slug": "test-wiki-get",
                "title": "Test Wiki Get",
                "content": "wiki content here",
                "tags": ["test"],
                "category": "test",
                "status": "approved",
                "confidence": 0.9,
                "project_id": TEST_PROJECT_ID,
            }
        )
        result = server.wiki_get(page_id)
        assert result is not None
        assert isinstance(result, dict)
        assert result["slug"] == "test-wiki-get"
        assert result["content"] == "wiki content here"

    def test_wiki_get_returns_none_for_missing_id(self):
        """wiki_get returns None when page ID does not exist."""
        result = server.wiki_get(999999999)
        assert result is None

    def test_wiki_get_strips_embedding_bytes(self):
        """wiki_get must not return raw embedding bytes in the response."""
        page_id = server._get_storage().insert_wiki_page(
            {
                "slug": "test-embed-strip",
                "title": "Embed Strip Test",
                "content": "content",
                "tags": [],
                "category": "test",
                "status": "approved",
                "confidence": 0.8,
                "project_id": TEST_PROJECT_ID,
            }
        )
        result = server.wiki_get(page_id)
        assert result is not None
        emb = result.get("embedding")
        assert not isinstance(emb, (bytes, bytearray)), (
            f"embedding must not be raw bytes, got {type(emb)}"
        )

    def test_wiki_get_returns_correct_id(self):
        """wiki_get returns the right page by ID."""
        id_a = server._get_storage().insert_wiki_page(
            {
                "slug": "page-a",
                "title": "Page A",
                "content": "content a",
                "tags": [],
                "category": "test",
                "status": "approved",
                "confidence": 0.8,
                "project_id": TEST_PROJECT_ID,
            }
        )
        id_b = server._get_storage().insert_wiki_page(
            {
                "slug": "page-b",
                "title": "Page B",
                "content": "content b",
                "tags": [],
                "category": "test",
                "status": "approved",
                "confidence": 0.8,
                "project_id": TEST_PROJECT_ID,
            }
        )
        result_a = server.wiki_get(id_a)
        result_b = server.wiki_get(id_b)
        assert result_a is not None
        assert result_b is not None
        assert result_a["slug"] == "page-a"
        assert result_b["slug"] == "page-b"
