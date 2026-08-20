"""Task 94 — memory_update leaves enriched_content stale (TDD).

DEFECT: memory_update(fields={"content": ...}) patches the `content` column
and (since Car 2 Part B) re-encodes the raw `embedding`, but never touches
`enriched_content` / `enrichment_queries` / `enrichment_concepts` /
`enrichment_comet` / `enrichment_logic` / `enrichment_model_versions`. Those
columns are the enrichment pipeline's output at insert time
(`_enrich_memory_if_enabled`, storage/memory.py) and `enriched_content` feeds
the actual stored embedding via `encode_document_enriched(content,
enriched_content)`. After a content correction the row's embedding and its
`enriched_content` column DISAGREE about what the memory says: the embedding
reflects the NEW raw content (Car 2's guard re-embeds from `fields["content"]`
only), while `enriched_content` still holds enrichment DERIVED FROM THE
SUPERSEDED text — including any "[enrichment] ..." synthetic-query block
built from words that no longer appear anywhere else on the row.

FIX (this file exercises it): on a real content change, memory_update must
either re-derive enrichment via the SAME producer insert_memory uses
(`_get_enrichment_pipeline`) or explicitly NULL the stale columns when
re-derivation is not possible (disabled / short content / pipeline failure)
— stale-but-honest beats stale-and-wrong.

RED before the fix; GREEN after. No sentence-transformers / network required:
the "re-derives" path drives the REAL EnrichmentPipeline in logic-only mode
(mirrors test_enrichment_wiring.py's _settings_logic_only helper) so the test
proves the SAME producer is reused, not a reimplementation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.storage import StorageEngine

#: C13 — every write in this file names a project explicitly (ADR-0227).
_TEST_PROJECT = "m-agahi/yadgar"

_OLD_CONTENT = "went camping at Yellowstone last summer"
_NEW_CONTENT = "went hiking at Yosemite last winter"


def _settings_logic_only(**overrides) -> Settings:
    """Settings with INDEX+LOGIC enrichment on; all model-download paths off."""
    defaults = {
        "INDEX_ENRICHMENT_ENABLED": True,
        "LOGIC_ENRICHMENT_ENABLED": True,
        "CONCEPTNET_ENRICHMENT_ENABLED": False,
        "COMET_ENRICHMENT_ENABLED": False,
        "DOC2QUERY_ENRICHMENT_ENABLED": False,
        "ENRICHMENT_MIN_CONTENT_LENGTH": 10,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _settings_disabled(**overrides) -> Settings:
    defaults = {"INDEX_ENRICHMENT_ENABLED": False}
    defaults.update(overrides)
    return Settings(**defaults)


def _vec(*xs) -> list[float]:
    return [float(x) for x in xs]


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "task94.db"), embedding_dim=4)
    yield engine
    engine.close()


def _mock_embeddings() -> MagicMock:
    """Deterministic embeddings engine — no real model, mirrors Car2's test style."""
    eng = MagicMock()
    eng.model_name = "crafted"
    # memory_update's raw re-embed step (Car 2 Part B guard)
    eng.encode_batch.return_value = [None]  # overridden per-test via side_effect
    # the enrichment-derived embed step (insert_memory AND the resync path)
    eng.encode_document_enriched.return_value = None  # overridden per-test
    return eng


def _insert_with_enrichment(storage, embeddings, settings, content, base_vec) -> int:
    return storage.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": content,
            "tags": ["t"],
            "store_type": "episodic",
            "heat": 0.7,
            "directory_context": "/tmp/task94",
            "embedding": storage._floats_to_bytes(base_vec),
            "embedding_model": "crafted",
        },
        embeddings_engine=embeddings,
        settings=settings,
    )


class TestEnrichedContentResyncOnUpdate:
    def _patch(self, monkeypatch, storage, embeddings, settings):
        import yadgar._shared.runtime.state as _st
        import yadgar.backend.admin_exec.memory as mem

        monkeypatch.setattr(_st, "_storage", storage, raising=False)
        monkeypatch.setattr(mem, "_get_embeddings", lambda: embeddings)
        monkeypatch.setattr(mem, "get_settings", lambda: settings)

    def test_content_change_reenriches_from_new_text_not_old(self, monkeypatch, storage):
        """Fix path: enrichment eligible → enriched_content is re-derived from
        NEW content via the real pipeline, and no longer mentions the OLD
        content's distinguishing term."""
        from yadgar.backend.admin_exec.memory import memory_update

        logic_settings = _settings_logic_only()
        embeddings = _mock_embeddings()
        # raw re-embed step: distinguishable crafted vector for the new content
        embeddings.encode_batch.side_effect = lambda texts: [
            storage._floats_to_bytes(_vec(0, 1, 0, 0))
        ]
        # enrichment-derived embed step: a THIRD distinguishable vector, proving
        # the final embedding is the enriched one, not the raw one (matches the
        # insert-time contract: encode_document_enriched overrides the raw embed).
        enriched_vec = _vec(0, 0, 1, 0)
        embeddings.encode_document_enriched.return_value = storage._floats_to_bytes(enriched_vec)

        mid = _insert_with_enrichment(
            storage, embeddings, logic_settings, _OLD_CONTENT, _vec(1, 0, 0, 0)
        )
        before = storage.get_memory(mid)
        assert "yellowstone" in (before.get("enriched_content") or "").lower(), (
            "test setup sanity: insert-time enrichment must have run"
        )

        self._patch(monkeypatch, storage, embeddings, logic_settings)
        memory_update({"memory_id": mid, "fields": {"content": _NEW_CONTENT}})

        after = storage.get_memory(mid)  # re-read storage, never trust the tool's echo
        assert after["content"] == _NEW_CONTENT
        enriched = (after.get("enriched_content") or "").lower()
        assert "yosemite" in enriched, "enriched_content must reflect the NEW content"
        assert "yellowstone" not in enriched, (
            "enriched_content must NOT keep describing the SUPERSEDED content (task 94)"
        )

        # embedding/enriched_content must agree post-fix: the stored embedding
        # is the enriched one (mirrors insert_memory's own contract), not the
        # bare raw-content vector from the Car 2 guard alone.
        stored_emb = after.get("embedding")
        stored_floats = (
            storage._bytes_to_floats(bytes(stored_emb)) if stored_emb is not None else None
        )
        assert stored_floats == pytest.approx(enriched_vec), (
            "post-update embedding must be the enrichment-derived vector, "
            "matching what enriched_content now says (no embedding/text disagreement)"
        )

    def test_content_change_nulls_stale_enrichment_when_unreachable(self, monkeypatch, storage):
        """Fallback path: enrichment disabled at update time → previously-written
        enrichment columns are explicitly NULLed, never left describing the
        superseded content (stale-but-honest beats stale-and-wrong)."""
        from yadgar.backend.admin_exec.memory import memory_update

        logic_settings = _settings_logic_only()
        embeddings = _mock_embeddings()
        embeddings.encode_batch.side_effect = lambda texts: [
            storage._floats_to_bytes(_vec(0, 1, 0, 0))
        ]
        embeddings.encode_document_enriched.return_value = None

        mid = _insert_with_enrichment(
            storage, embeddings, logic_settings, _OLD_CONTENT, _vec(1, 0, 0, 0)
        )
        before = storage.get_memory(mid)
        assert before.get("enriched_content"), "test setup sanity: row must start enriched"

        disabled_settings = _settings_disabled()
        self._patch(monkeypatch, storage, embeddings, disabled_settings)
        memory_update({"memory_id": mid, "fields": {"content": _NEW_CONTENT}})

        after = storage.get_memory(mid)
        assert after["content"] == _NEW_CONTENT
        assert not after.get("enriched_content"), (
            "enrichment unreachable at update time — enriched_content must be "
            "nulled, not left holding text derived from superseded content"
        )
        assert not after.get("enrichment_queries")
        assert not after.get("enrichment_logic")
        assert not after.get("enrichment_model_versions")

    def test_content_change_nulls_stale_enrichment_when_raw_reembed_fails(
        self, monkeypatch, storage
    ):
        """Raw re-embed itself fails (encode_batch raises) → resync still runs
        and nulls the stale enrichment columns rather than skipping entirely.
        """
        from yadgar.backend.admin_exec.memory import memory_update

        logic_settings = _settings_logic_only()
        embeddings = _mock_embeddings()

        def _boom(_texts):
            raise RuntimeError("embed service unavailable")

        mid = _insert_with_enrichment(
            storage, embeddings, logic_settings, _OLD_CONTENT, _vec(1, 0, 0, 0)
        )
        before = storage.get_memory(mid)
        assert before.get("enriched_content"), "test setup sanity: row must start enriched"

        embeddings.encode_batch.side_effect = _boom
        self._patch(monkeypatch, storage, embeddings, logic_settings)
        memory_update({"memory_id": mid, "fields": {"content": _NEW_CONTENT}})

        after = storage.get_memory(mid)
        assert after["content"] == _NEW_CONTENT
        assert not after.get("enriched_content"), (
            "raw re-embed failure must not leave enriched_content describing "
            "the superseded content — the resync's null fallback must still run"
        )

    def test_tags_only_patch_leaves_enrichment_untouched(self, monkeypatch, storage):
        """No content change → no resync attempt; existing enrichment survives."""
        from yadgar.backend.admin_exec.memory import memory_update

        logic_settings = _settings_logic_only()
        embeddings = _mock_embeddings()
        embeddings.encode_document_enriched.return_value = None

        mid = _insert_with_enrichment(
            storage, embeddings, logic_settings, _OLD_CONTENT, _vec(1, 0, 0, 0)
        )
        before = storage.get_memory(mid)

        self._patch(monkeypatch, storage, embeddings, logic_settings)
        memory_update({"memory_id": mid, "fields": {"tags": ["a", "b"]}})

        after = storage.get_memory(mid)
        assert after.get("enriched_content") == before.get("enriched_content")
