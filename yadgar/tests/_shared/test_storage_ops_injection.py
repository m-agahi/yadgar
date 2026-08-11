"""SurrealQL injection regression tests for storage/ops.py (S1 — v5.2.0 security cluster).

Covers:
    1. extra_where injection attempt (H-5): must be rejected at validation.
    2. Memory content containing '$id' token — stored and recalled literally.
    3. Memory content containing closing-quote + new statement — stored literally.
    4. Surrogate-pair attack (see wiki: surrealdb-v3-surrogate-pair-gotcha) — emoji/non-BMP
       content stored and recalled without HTTP 400.
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
    db_path = str(tmp_path / "test_injection.db")
    engine = StorageEngine(db_path)
    yield engine
    engine.close()


def _sentinel_memory(storage, tag="sentinel"):
    """Insert a canary memory that must survive injection attempts."""
    return storage.insert_memory(
        {
            "content": "canary — must not be deleted",
            "directory_context": "/test",
            "project_id": _PROJECT,
            "tags": [tag],
        }
    )


# ── S1a: extra_where injection (H-5) ──────────────────────────────────────────


class TestExtraWhereInjection:
    """extra_where must be validated; injection attempts must raise ValueError."""

    def test_extra_where_injection_rejected(self, storage):
        """Passing SQL injection payload via extra_where raises ValueError.

        Before the fix, this would interpolate raw SQL into the DELETE query,
        potentially corrupting or wiping the table.
        """
        with pytest.raises(ValueError, match="extra_where"):
            storage.prune_old_rows(
                "narrative_entry",
                older_than_days=1,
                extra_where="OR 1=1; DELETE FROM memory; --",
            )

    def test_extra_where_semicolon_injection_rejected(self, storage):
        """extra_where containing a semicolon (statement separator) is rejected."""
        with pytest.raises(ValueError, match="extra_where"):
            storage.prune_old_rows(
                "narrative_entry",
                older_than_days=1,
                extra_where="is_active = true; DROP TABLE memory",
            )

    def test_extra_where_comment_injection_rejected(self, storage):
        """extra_where containing SQL comment marker is rejected."""
        with pytest.raises(ValueError, match="extra_where"):
            storage.prune_old_rows(
                "narrative_entry",
                older_than_days=1,
                extra_where="is_active = false -- bypass",
            )

    def test_extra_where_valid_passes(self, storage):
        """Allowlisted simple column = literal patterns are accepted and do not raise."""
        # Must not raise — only tables matter for injection here; 0 rows pruned is fine
        result = storage.prune_old_rows(
            "narrative_entry",
            older_than_days=1,
            extra_where="is_active = false",
        )
        assert isinstance(result, int)

    def test_extra_where_injection_does_not_wipe_canary(self, storage):
        """Canary memory survives an injection attempt (the attempt must be blocked before DB)."""
        canary_id = _sentinel_memory(storage)

        with pytest.raises(ValueError):
            storage.prune_old_rows(
                "narrative_entry",
                older_than_days=1,
                extra_where="OR 1=1; DELETE FROM memory; --",
            )

        # Canary must still exist
        mem = storage.get_memory(canary_id)
        assert mem is not None, "canary memory was wiped — injection may have succeeded"
        assert "canary" in mem["content"]


# ── S1b: engram_slot INSERT bind (H-4) ───────────────────────────────────────


class TestEngramSlotInsertBind:
    """init_engram_slots must survive content containing SurrealQL-special tokens."""

    def test_dollar_id_token_in_slot_does_not_corrupt(self, storage):
        """Engram slot init works when surrounding context contains '$id' text.

        The fix uses bind parameters so '$id' in data is never interpreted as SQL.
        (The slot data itself doesn't contain '$id', but we verify the INSERT path
        doesn't corrupt other memories that do contain dollar tokens.)
        """
        # Insert a memory whose content contains the dangerous token
        mid = storage.insert_memory(
            {
                "content": "Memory with $id and $content tokens — must not corrupt",
                "directory_context": "/test",
                "project_id": _PROJECT,
                "tags": ["dollar-token-test"],
            }
        )

        # Trigger the engram slot INSERT path
        storage.init_engram_slots(5)

        # Memory must be retrievable with content intact
        mem = storage.get_memory(mid)
        assert mem is not None
        assert "$id" in mem["content"]
        assert "$content" in mem["content"]

        # Engram slots must also be created correctly
        slot = storage.get_engram_slot(0)
        assert slot is not None
        assert slot["slot_index"] == 0

    def test_engram_slots_init_idempotent(self, storage):
        """init_engram_slots called twice does not duplicate or corrupt rows."""
        storage.init_engram_slots(3)
        storage.init_engram_slots(3)
        slots = storage.get_all_engram_slots()
        indices = [s["slot_index"] for s in slots]
        assert len(indices) == len(set(indices)), "duplicate slot indices — bind failure"
        assert set(indices) == {0, 1, 2}


# ── S1c: memory content injection via insert path ────────────────────────────


class TestMemoryContentInjection:
    """Memory content containing SurrealQL-special tokens is stored and recalled literally."""

    def test_dollar_id_in_content_stored_literally(self, storage):
        """Content containing '$id' is stored verbatim, not interpreted as SurrealQL var."""
        content = "Dangerous: $id and $content should not be evaluated"
        mid = storage.insert_memory(
            {
                "content": content,
                "directory_context": "/test",
                "project_id": _PROJECT,
                "tags": ["injection-test"],
            }
        )
        mem = storage.get_memory(mid)
        assert mem is not None
        assert mem["content"] == content, f"content mutated: {mem['content']!r}"

    def test_closing_quote_in_content_stored_literally(self, storage):
        """Content with closing-quote + new statement attempt is stored verbatim."""
        content = 'quote-escape attempt: "; DELETE FROM memory; SELECT "'
        mid = storage.insert_memory(
            {
                "content": content,
                "directory_context": "/test",
                "project_id": _PROJECT,
                "tags": ["injection-test"],
            }
        )
        mem = storage.get_memory(mid)
        assert mem is not None
        assert mem["content"] == content, f"content mutated: {mem['content']!r}"

    def test_closing_quote_canary_survives(self, storage):
        """A canary memory is not wiped when a closing-quote injection is attempted."""
        canary_id = _sentinel_memory(storage, tag="closing-quote-canary")

        evil_content = 'evil"; DELETE FROM memory; LET $x = "'
        storage.insert_memory(
            {
                "content": evil_content,
                "directory_context": "/test",
                "project_id": _PROJECT,
                "tags": ["injection-test"],
            }
        )

        canary = storage.get_memory(canary_id)
        assert canary is not None, "canary wiped — closing-quote injection may have succeeded"

    def test_surrogate_pair_attack_in_content(self, storage):
        """Non-BMP / emoji content is stored and recalled without parse error.

        See wiki: surrealdb-v3-surrogate-pair-gotcha — ensure_ascii=True emits \\uD800-\\uDFFF
        surrogates that SurrealDB v3 rejects with HTTP 400. The fix is ensure_ascii=False.
        """
        content = "Surrogate-pair attack vector: \U0001f6a8 emoji plus U+10000 \U00010000"
        mid = storage.insert_memory(
            {
                "content": content,
                "directory_context": "/test",
                "project_id": _PROJECT,
                "tags": ["surrogate-pair-test"],
            }
        )
        mem = storage.get_memory(mid)
        assert mem is not None
        assert mem["content"] == content, f"emoji content mangled: {mem['content']!r}"

    def test_all_memories_undisturbed_after_injection_attempts(self, storage):
        """After multiple injection attempts no memories are accidentally deleted."""
        # Plant several canaries
        ids = [
            storage.insert_memory(
                {
                    "content": f"canary {i}",
                    "directory_context": "/test",
                    "project_id": _PROJECT,
                    "tags": ["canary"],
                }
            )
            for i in range(5)
        ]

        # Fire multiple injection payloads
        payloads = [
            "Memory with $id token",
            'quote"; DELETE FROM memory; --',
            "\U0001f525 fire emoji \U0001f4a5",
            "$content $id $result",
        ]
        for p in payloads:
            storage.insert_memory({"content": p, "directory_context": "/test", "tags": ["payload"]})

        # All canaries must still exist
        for cid in ids:
            assert storage.get_memory(cid) is not None, f"canary {cid} was wiped"
