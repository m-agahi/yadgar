"""Task 296 — the content-change enrichment resync must not be memory_update's alone (TDD).

Car 7 (task 94) fixed ONE caller: ``memory_update`` re-derives (or nulls) the six
enrichment columns when it patches ``content``.  Every OTHER writer of
``memory.content`` still leaves them describing the SUPERSEDED text:

  * ``sleep_compute/embed_compress.py`` — ``compress_old_memories`` rewrites
    content nightly, unattended, corpus-wide.
  * ``write_exec/_memorize_phases/_phase_contradiction.py`` — the conflict
    resolver's UPDATE op.
  * ``curation/ingestion.py`` — ``merge_memory`` concatenates new content onto
    an existing row (the "third caller" that proves this is a class, not a pair).

``enriched_content`` is NOT a retrieval input (FULLTEXT indexes ``content``
alone) — the harm is that it is echoed verbatim in raw memory dicts, so a
reading session is handed a claim the row no longer makes.

THE SEAM, in two halves (see the commit message for why it splits):

  1. THE FLOOR lives in ``update_memory_fields`` — the one funnel every content
     writer already goes through.  A content change nulls the six enrichment
     columns in the SAME UPDATE (no extra round-trip, no embeddings engine, no
     model inference, no ordering hazard: it never touches ``embedding``).
     Structurally unskippable, so caller #4 inherits it for free.
  2. THE RE-DERIVATION stays caller-side, because it must run AFTER the caller
     writes its own embedding — otherwise the caller's raw re-embed clobbers the
     enrichment-derived vector the resync just stored.  This car wires the
     nightly path (the worst one); the others get the honest floor.

RED before the fix; GREEN after.  No sentence-transformers / network required:
the pipeline runs in logic-only mode with a MagicMock embeddings engine, the
same harness Car 7 used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.storage import StorageEngine
from yadgar.backend.sleep_compute.embed_compress import _EmbedCompressMixin

#: C13 — every write in this file names a project explicitly (ADR-0227).
_TEST_PROJECT = "m-agahi/yadgar"

#: Sentences WITHOUT any `_ENTITY_PATTERN_RE` hit — compression drops these.
#: Deliberately free of "def ", "class ", "import ", "from ", `a/b.c` paths and
#: `*Error`/`*Exception` words, or compress_old_memories would keep them.
_FILLER = (
    "The camping trip to Yellowstone was long and uneventful with rain most "
    "days and very little to report about weather or food or anything else. "
)

#: The ONE sentence compression keeps (matches `\bdef\s+\w+`).
_KEY = "def hike_trail runs the Yosemite loop each morning."

#: >1000 chars so compress_old_memories' length gate opens; compressed output
#: is _KEY alone, which is strictly shorter (its other gate).
_LONG_CONTENT = _KEY + " " + _FILLER * 8


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


def _vec(*xs) -> list[float]:
    return [float(x) for x in xs]


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "task296.db"), embedding_dim=4)
    yield engine
    engine.close()


def _mock_embeddings(storage) -> MagicMock:
    """Deterministic embeddings engine — no real model (mirrors Car 7's harness)."""
    eng = MagicMock()
    eng.model_name = "crafted"
    eng.get_model_name.return_value = "crafted"
    eng.encode.return_value = storage._floats_to_bytes(_vec(0, 1, 0, 0))
    eng.encode_batch.return_value = [storage._floats_to_bytes(_vec(0, 1, 0, 0))]
    eng.encode_document_enriched.return_value = storage._floats_to_bytes(_vec(0, 0, 1, 0))
    return eng


def _insert_enriched(storage, embeddings, settings, content, base_vec=None) -> int:
    return storage.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": content,
            "tags": ["t"],
            "store_type": "episodic",
            "heat": 0.7,
            "directory_context": "/tmp/task296",
            "embedding": storage._floats_to_bytes(base_vec or _vec(1, 0, 0, 0)),
            "embedding_model": "crafted",
        },
        embeddings_engine=embeddings,
        settings=settings,
    )


class _SleepHost(_EmbedCompressMixin):
    """Minimal SleepComputeEngine stand-in carrying only what the mixin reads."""

    def __init__(self, storage, embeddings, settings) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._settings = settings


class TestNightlyCompressionResyncsEnrichment:
    """The unattended, corpus-wide path — compress_old_memories."""

    def _age_row(self, storage, mid: int, days: int = 60) -> None:
        # Direct UPDATE, not update_memory_fields: `created_at` is deliberately
        # absent from _MEMORY_UPDATABLE_FIELDS, so that call would silently
        # filter the field out and leave the row too young to compress.
        past = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        storage._q(f"UPDATE memory:{int(mid)} SET created_at = $ts", {"ts": past})

    def test_compression_reenriches_from_compressed_text(self, storage):
        """The nightly path re-derives enrichment from the COMPRESSED content —
        it must not leave enriched_content describing the sentences compression
        just threw away."""
        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)

        mid = _insert_enriched(storage, embeddings, settings, _LONG_CONTENT)
        before = storage.get_memory(mid)
        assert "yellowstone" in (before.get("enriched_content") or "").lower(), (
            "setup sanity: insert-time enrichment must have run over the filler text"
        )
        self._age_row(storage, mid)

        host = _SleepHost(storage, embeddings, settings)
        count = host.compress_old_memories(days_threshold=30)

        # Guard against a vacuous pass: every one of compress_old_memories'
        # four gates (age / >1000 chars / not compressed / strictly shorter)
        # must actually have opened, or the assertions below prove nothing.
        assert count == 1, "setup sanity: the row must actually have been compressed"

        after = storage.get_memory(mid)  # re-read storage, never trust a return value
        assert after["content"] == _KEY
        enriched = (after.get("enriched_content") or "").lower()
        assert "yosemite" in enriched, (
            "enriched_content must be re-derived from the COMPRESSED content (task 296)"
        )
        assert "yellowstone" not in enriched, (
            "enriched_content must NOT keep describing sentences compression "
            "removed from the row — nightly, unattended, corpus-wide (task 296)"
        )

    def test_compression_nulls_enrichment_when_pipeline_unreachable(self, storage):
        """Enrichment disabled by the time the nightly cycle runs → the stale
        columns are nulled, never left describing the pre-compression text."""
        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)

        mid = _insert_enriched(storage, embeddings, settings, _LONG_CONTENT)
        assert storage.get_memory(mid).get("enriched_content"), (
            "setup sanity: row must start enriched"
        )
        self._age_row(storage, mid)

        disabled = Settings(INDEX_ENRICHMENT_ENABLED=False)
        host = _SleepHost(storage, embeddings, disabled)
        assert host.compress_old_memories(days_threshold=30) == 1

        after = storage.get_memory(mid)
        assert after["content"] == _KEY
        assert not after.get("enriched_content"), (
            "enrichment unreachable → nulled, not left holding pre-compression text"
        )
        assert not after.get("enrichment_logic")
        assert not after.get("enrichment_model_versions")

    def test_compression_keeps_the_enrichment_derived_embedding(self, storage):
        """The resync runs AFTER the raw re-embed, so the stored vector is the
        enrichment-derived one — same contract insert_memory and memory_update
        already hold. (This is why the re-derivation cannot move into
        update_memory_fields: the caller's own embedding write would clobber it.)
        """
        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)
        enriched_vec = _vec(0, 0, 1, 0)

        mid = _insert_enriched(storage, embeddings, settings, _LONG_CONTENT)
        self._age_row(storage, mid)

        host = _SleepHost(storage, embeddings, settings)
        assert host.compress_old_memories(days_threshold=30) == 1

        stored = storage.get_memory(mid).get("embedding")
        floats = storage._bytes_to_floats(bytes(stored)) if stored is not None else None
        assert floats == pytest.approx(enriched_vec), (
            "post-compression embedding must be the enrichment-derived vector, "
            "matching what enriched_content now says"
        )

    def test_enrichment_failure_does_not_abort_the_remaining_rows(self, storage, monkeypatch):
        """A resync blow-up on one row must not kill the whole nightly pass —
        the loop keeps going and the returned count stays honest."""
        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)

        first = _insert_enriched(storage, embeddings, settings, _LONG_CONTENT)
        second = _insert_enriched(storage, embeddings, settings, _LONG_CONTENT + " tail here.")
        self._age_row(storage, first)
        self._age_row(storage, second)

        calls: list[int] = []

        def _boom(_self, mem_id, *_a, **_kw):
            calls.append(mem_id)
            raise RuntimeError("enrichment backend down")

        monkeypatch.setattr(
            type(storage), "resync_enrichment_on_content_change", _boom, raising=True
        )

        host = _SleepHost(storage, embeddings, settings)
        assert host.compress_old_memories(days_threshold=30) == 2, (
            "a best-effort resync failure must not abort compression of later rows"
        )
        # Non-vacuous: the nightly path must actually have reached the resync
        # for BOTH rows, not skipped it and passed by doing nothing.
        assert calls == [first, second], (
            f"compression must attempt the resync once per compressed row, got {calls}"
        )
        assert storage.get_memory(first)["content"] == _KEY
        assert storage.get_memory(second)["content"] == _KEY


class TestUpdateMemoryFieldsIsTheFloor:
    """The structural half: no content writer can skip the null-out."""

    def test_content_change_nulls_stale_enrichment(self, storage):
        """A bare update_memory_fields(content=...) — no embeddings engine, no
        settings, no caller cooperation — must not leave the six enrichment
        columns describing the superseded text."""
        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)

        mid = _insert_enriched(storage, embeddings, settings, "went camping at Yellowstone")
        assert storage.get_memory(mid).get("enriched_content"), "setup sanity: starts enriched"

        storage.update_memory_fields(mid, content="went hiking at Yosemite")

        after = storage.get_memory(mid)
        assert after["content"] == "went hiking at Yosemite"
        for col in (
            "enriched_content",
            "enrichment_concepts",
            "enrichment_comet",
            "enrichment_queries",
            "enrichment_logic",
            "enrichment_model_versions",
        ):
            assert not after.get(col), (
                f"{col} still describes the superseded content after a bare "
                "update_memory_fields(content=...) — the floor must be in the funnel"
            )

    def test_non_content_change_leaves_enrichment_intact(self, storage):
        """The floor is gated on content exactly like the memory_doc cache bust
        beside it — a heat/tags patch must not wipe valid enrichment."""
        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)

        mid = _insert_enriched(storage, embeddings, settings, "went camping at Yellowstone")
        before = storage.get_memory(mid)

        storage.update_memory_fields(mid, heat=0.4, tags=["a", "b"])

        after = storage.get_memory(mid)
        assert after.get("enriched_content") == before.get("enriched_content")
        assert after.get("enrichment_logic") == before.get("enrichment_logic")

    def test_conflict_resolver_update_inherits_the_floor(self, storage, monkeypatch):
        """_phase_contradiction's UPDATE op writes content through the same
        funnel — it needs no edit of its own to stop echoing stale enrichment."""
        from yadgar.backend.write_exec import _memorize_phases as _phases
        from yadgar.backend.write_exec._memorize_phases import _phase_contradiction as pc

        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)
        mid = _insert_enriched(storage, embeddings, settings, "went camping at Yellowstone")
        assert storage.get_memory(mid).get("enriched_content"), "setup sanity: starts enriched"

        monkeypatch.setattr(pc, "_get_storage", lambda: storage)
        ctx = MagicMock()
        ctx.content = "went hiking at Yosemite"
        ctx.tags = ["t"]
        assert pc._handle_update(ctx, mid, "conflict") is not None
        assert _phases is not None  # import guard: package must expose the phase module

        after = storage.get_memory(mid)
        assert after["content"] == "went hiking at Yosemite"
        assert not after.get("enriched_content"), (
            "conflict-resolver UPDATE must not leave enriched_content describing "
            "the content it just replaced (task 296)"
        )

    def test_curation_merge_inherits_the_floor(self, storage):
        """The third caller — merge_memory concatenates content onto a live row
        and never touched enrichment. Free coverage from the shared floor."""
        from yadgar.backend.curation.ingestion import merge_memory

        settings = _settings_logic_only()
        embeddings = _mock_embeddings(storage)
        mid = _insert_enriched(storage, embeddings, settings, "went camping at Yellowstone")
        assert storage.get_memory(mid).get("enriched_content"), "setup sanity: starts enriched"

        result = merge_memory(
            storage,
            embeddings,
            mid,
            "went hiking at Yosemite",
            ["t2"],
            storage._floats_to_bytes(_vec(0, 1, 0, 0)),
            None,
        )
        assert result["action"] == "merged"

        after = storage.get_memory(mid)
        assert "Yosemite" in after["content"]
        assert not after.get("enriched_content"), (
            "merge_memory must not leave enriched_content describing only the "
            "pre-merge half of the row (task 296)"
        )
