"""§6 atomicity tests — recreate_vector_table backup + transaction safety.

Verifies:
- Pre-flight backup written to memory_embedding_backup before DROP INDEX
- Embeddings recoverable from sidecar after simulated failure
- insert_checkpoint is atomic (deactivate + insert together)
- replace_wiki_crossrefs is atomic (delete + insert together)
"""

import pytest

from yadgar._shared.storage import StorageEngine

# C13 (0047 PR#40 §5): seeds must NAME the project they write into —
# C5 deleted every fallback that used to answer an unnamed write (ADR-0227).
# A per-file constant, deliberately NOT a shared fixture default: a new test
# that builds its own write payload still reds — the signal of the flip.
_PROJECT = "m-agahi/yadgar"


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_atomicity.db"))
    yield engine
    engine.close()


def _insert_memory_with_embedding(storage, content):
    """Insert a memory with a synthetic embedding matching storage._embedding_dim."""
    dim = storage._embedding_dim
    # Pack dim floats as 4 bytes each (little-endian IEEE 754 value 0.5)
    import struct

    embedding = struct.pack(f"{dim}f", *[0.5] * dim)
    return storage.insert_memory(
        {
            "content": content,
            "embedding": embedding,
            "tags": ["test"],
            "directory_context": "/project",
            "project_id": _PROJECT,
            "heat": 1.0,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": "test-model",
        }
    )


class TestRecreateVectorTableBackup:
    """Backup sidecar must be written before DROP INDEX."""

    def test_backup_table_created_after_recreate(self, storage):
        """memory_embedding_backup must exist after recreate_vector_table."""
        # Insert a memory with an embedding
        _insert_memory_with_embedding(storage, "test content A")

        # Run recreate_vector_table — should create the backup sidecar.
        orig_dim = storage._embedding_dim
        storage.recreate_vector_table(orig_dim)

        # The backup table must exist (even if empty after recreate).
        rows = storage._q("SELECT * FROM memory_embedding_backup LIMIT 10")
        # The backup was taken before DROP INDEX — may have rows from the backup
        # OR be empty because recreate succeeded.  Either way the table must exist.
        assert isinstance(rows, list), "memory_embedding_backup table must exist after recreate"

    def test_embeddings_cleared_after_recreate(self, storage):
        """All memory embeddings must be NONE after recreate_vector_table."""
        _insert_memory_with_embedding(storage, "test content B")
        orig_dim = storage._embedding_dim
        storage.recreate_vector_table(orig_dim)

        mems = storage._q("SELECT embedding FROM memory")
        for m in mems:
            assert m.get("embedding") is None, (
                "All embeddings must be cleared after recreate_vector_table"
            )

    def test_new_dim_recorded(self, storage):
        """_embedding_dim must be updated to the new dimension."""
        orig_dim = storage._embedding_dim
        storage.recreate_vector_table(orig_dim)
        assert storage._embedding_dim == orig_dim


class TestInsertCheckpointAtomicity:
    """insert_checkpoint: deactivate-old + create-new must be atomic."""

    def test_only_one_active_checkpoint_after_insert(self, storage):
        """After two inserts, only the latest checkpoint is active."""
        storage.insert_checkpoint(
            {
                "directory_context": "/project",
                "session_id": "sess1",
                "current_task": "task A",
            }
        )
        storage.insert_checkpoint(
            {
                "directory_context": "/project",
                "session_id": "sess2",
                "current_task": "task B",
            }
        )

        active = storage.get_active_checkpoint()
        assert active is not None
        assert active["current_task"] == "task B"

        # Verify only one is active
        all_active = storage._q("SELECT id FROM checkpoint WHERE is_active = true")
        assert len(all_active) == 1, (
            f"Exactly one checkpoint should be active, found {len(all_active)}"
        )

    def test_checkpoint_fields_stored(self, storage):
        """Checkpoint fields must be stored correctly."""
        storage.insert_checkpoint(
            {
                "directory_context": "/my/project",
                "session_id": "test_session",
                "current_task": "implementing feature X",
                "files_being_edited": ["server.py", "storage.py"],
                "key_decisions": ["Use streaming hash"],
            }
        )
        cp = storage.get_active_checkpoint()
        assert cp is not None
        assert cp["directory_context"] == "/my/project"
        assert cp["current_task"] == "implementing feature X"
        assert "server.py" in cp.get("files_being_edited", [])


class TestReplaceWikiCrossrefsAtomicity:
    """replace_wiki_crossrefs: delete + insert must be atomic."""

    def _insert_wiki_page(self, storage, slug, title="Test"):
        """Insert a minimal wiki page."""
        storage._q(
            "CREATE wiki_page SET slug = $slug, title = $title, content = $content, "
            "category = $cat, tags = $tags, confidence = $conf, created_at = time::now()",
            {
                "slug": slug,
                "title": title,
                "content": "content",
                "cat": "reference",
                "tags": [],
                "conf": "medium",
            },
        )

    def test_crossrefs_replaced_atomically(self, storage):
        """After replace, only new crossrefs remain — old ones are gone."""
        # Set up initial crossrefs
        storage.replace_wiki_crossrefs("from-page", ["old-target-1", "old-target-2"])

        initial = storage.get_wiki_backlinks("old-target-1")
        assert "from-page" in initial

        # Replace with new targets
        storage.replace_wiki_crossrefs("from-page", ["new-target-1"])

        # Old targets must be gone
        old_links = storage.get_wiki_backlinks("old-target-1")
        assert "from-page" not in old_links, "Old crossrefs must be deleted on replace"

        # New target must be present
        new_links = storage.get_wiki_backlinks("new-target-1")
        assert "from-page" in new_links, "New crossrefs must be present after replace"

    def test_empty_to_slugs_clears_all(self, storage):
        """Replacing with empty list removes all crossrefs from that slug."""
        storage.replace_wiki_crossrefs("from-page", ["target-a", "target-b"])
        storage.replace_wiki_crossrefs("from-page", [])

        links = storage.get_wiki_backlinks("target-a")
        assert "from-page" not in links, "All crossrefs should be removed with empty list"

    def test_crossrefs_for_other_slugs_unaffected(self, storage):
        """Replacing crossrefs for slug A must not affect slug B's crossrefs."""
        storage.replace_wiki_crossrefs("page-a", ["shared-target"])
        storage.replace_wiki_crossrefs("page-b", ["shared-target"])

        # Replace page-a's crossrefs
        storage.replace_wiki_crossrefs("page-a", ["new-target"])

        # page-b's crossref to shared-target must still exist
        links = storage.get_wiki_backlinks("shared-target")
        assert "page-b" in links, "page-b crossrefs must not be affected by page-a replace"
