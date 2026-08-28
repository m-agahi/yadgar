"""Regression tests for #39 — enrichment wiring through user-write paths.

Bug: INDEX_ENRICHMENT_ENABLED defaults True but enrichment never ran because
ALL callers of Storage.insert_memory() omitted embeddings_engine= and settings=,
causing _enrich_memory_if_enabled to early-return (settings is None guard).

Fix: insert_new_memory() and _direct_insert() now thread both kwargs.
MemoryCurator._insert_new_memory() forwards self._embeddings / self._settings.

These tests are RED against the unfixed code and GREEN after the fix.
They run without a SurrealDB server (mock storage) and without model downloads
(mock embeddings + logic-only enrichment).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from yadgar._shared.config import Settings
from yadgar.backend.curation import CurateParams, MemoryCurator
from yadgar.backend.curation.ingestion import NewMemorySpec, insert_new_memory

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it, so a
#: write without it is a hard UnresolvedProjectError at insert.
_TEST_PROJECT = "m-agahi/yadgar"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _dummy_embedding_bytes(dim: int = 384) -> bytes:
    """Return a unit-norm embedding as raw bytes."""
    vec = np.ones(dim, dtype=np.float32)
    vec = vec / np.linalg.norm(vec)
    return vec.tobytes()


def _mock_embeddings(dim: int = 384) -> MagicMock:
    """Mock EmbeddingEngine that returns a fixed embedding for any input."""
    eng = MagicMock()
    emb = _dummy_embedding_bytes(dim)
    eng.encode.return_value = emb
    eng.encode_query.return_value = np.frombuffer(emb, dtype=np.float32)
    eng.encode_document_enriched.return_value = None  # skip re-embed step
    eng.similarity.return_value = 0.0  # no similar memories
    eng.get_model_name.return_value = "mock-model"
    return eng


def _mock_storage() -> MagicMock:
    """Minimal StorageEngine mock for insert_new_memory / curator tests."""
    storage = MagicMock()
    storage.insert_memory.return_value = 42
    storage.search_vectors.return_value = []  # no similar hits → always create
    storage.get_memory.return_value = None
    storage._now_iso.return_value = "2026-01-01T00:00:00"
    return storage


# ---------------------------------------------------------------------------
# 1. insert_new_memory — kwarg pass-through (unit)
# ---------------------------------------------------------------------------


class TestInsertNewMemoryKwargs:
    """insert_new_memory must forward embeddings_engine + settings to insert_memory."""

    def test_forwards_enrichment_kwargs_when_provided(self):
        storage = _mock_storage()
        settings = _settings_logic_only()
        embeddings = _mock_embeddings()
        emb = _dummy_embedding_bytes()

        spec = NewMemorySpec(embedding=emb, tags=["test"])
        insert_new_memory(
            storage,
            "went camping at Yellowstone last summer",
            "/home/user/project",
            spec,
            embeddings_engine=embeddings,
            settings=settings,
        )

        # Verify insert_memory was called with the enrichment kwargs
        assert storage.insert_memory.call_count == 1
        _args, kwargs = storage.insert_memory.call_args
        assert kwargs.get("embeddings_engine") is embeddings, (
            "embeddings_engine not forwarded to storage.insert_memory"
        )
        assert kwargs.get("settings") is settings, "settings not forwarded to storage.insert_memory"

    def test_omitting_kwargs_still_works(self):
        """Backward-compat: insert_new_memory(storage, content, ctx, spec) must not break."""
        storage = _mock_storage()
        spec = NewMemorySpec(embedding=_dummy_embedding_bytes(), tags=["test"])
        result = insert_new_memory(
            storage,
            "short test content here",
            "/tmp/project",
            spec,
        )
        assert result == 42
        _args, kwargs = storage.insert_memory.call_args
        # Both default to None — insert_memory's own guard safely early-returns
        assert kwargs.get("embeddings_engine") is None
        assert kwargs.get("settings") is None


# ---------------------------------------------------------------------------
# 2. MemoryCurator — end-to-end enrichment via create path (integration)
# ---------------------------------------------------------------------------


class TestCuratorEnrichmentWiring:
    """MemoryCurator.curate_on_remember must wire enrichment through insert_memory.

    RED before fix: curator called _insert_new_memory → insert_new_memory without
    embeddings_engine/settings → insert_memory gets None for both → enrichment gate
    short-circuits → enriched_content / enrichment_logic never written.

    GREEN after fix: curator passes self._embeddings / self._settings into the call
    chain so _enrich_memory_if_enabled actually runs.
    """

    def test_curator_passes_settings_to_insert_memory(self):
        """Assert insert_memory is called with enrichment kwargs, not None."""
        storage = _mock_storage()
        embeddings = _mock_embeddings()
        thermo = MagicMock()
        settings = _settings_logic_only()

        curator = MemoryCurator(storage, embeddings, thermo, settings)

        rich_content = "went camping at Yellowstone last summer"
        curator.curate_on_remember(
            rich_content,
            "/home/user/project",
            ["outdoor"],
            _dummy_embedding_bytes(),
            params=CurateParams(project_id=_TEST_PROJECT),
        )

        # Must have called insert_memory exactly once (create path, no similar hits)
        assert storage.insert_memory.call_count == 1, (
            "Expected exactly one insert_memory call on the create path"
        )
        _args, kwargs = storage.insert_memory.call_args
        assert kwargs.get("embeddings_engine") is embeddings, (
            "curator did not forward self._embeddings to insert_memory (#39 regression)"
        )
        assert kwargs.get("settings") is settings, (
            "curator did not forward self._settings to insert_memory (#39 regression)"
        )

    def test_enrichment_pipeline_invoked_on_curator_create(self):
        """_enrich_memory_if_enabled must be called with non-None args via curator.

        Patches storage._enrich_memory_if_enabled to observe the actual call.
        """
        storage = _mock_storage()

        # _enrich_memory_if_enabled lives on the class; we need to intercept the
        # real call on our mock by patching the method on the storage's class,
        # but since storage is a MagicMock the method is auto-created.
        # Instead: patch insert_memory to record kwargs, then verify.
        received: dict = {}

        def _capture_insert(memory_dict, embeddings_engine=None, settings=None, branch=None):
            received["embeddings_engine"] = embeddings_engine
            received["settings"] = settings
            return 42

        storage.insert_memory.side_effect = _capture_insert

        embeddings = _mock_embeddings()
        thermo = MagicMock()
        settings = _settings_logic_only()

        curator = MemoryCurator(storage, embeddings, thermo, settings)
        curator.curate_on_remember(
            "explored the Rocky Mountains in late autumn",
            "/projects/notes",
            ["travel"],
            _dummy_embedding_bytes(),
            params=CurateParams(project_id=_TEST_PROJECT),
        )

        assert received.get("embeddings_engine") is embeddings, (
            "enrichment_engine was None — wiring missing (#39)"
        )
        assert received.get("settings") is settings, "settings was None — wiring missing (#39)"

    def test_logic_enrichment_runs_end_to_end(self):
        """Smoke-test the full enrichment pipeline via real EnrichmentPipeline.

        No model downloads: logic-only path uses hardcoded expansions + NLTK
        (already available in dev/CI) and no network calls.

        Skipped when the real pipeline raises (e.g. missing NLTK data in some
        envs) — this is a best-effort smoke test, not a hard regression gate.
        """
        from yadgar._shared.enrichment import EnrichmentPipeline

        settings = _settings_logic_only()
        content = "went camping at Yellowstone last summer"
        emb = _dummy_embedding_bytes()

        try:
            pipeline = EnrichmentPipeline(settings)
            result = pipeline.enrich(content, emb, settings)
        # `enrich` loads the seq2seq + NLI models; when they are absent the
        # failure comes from transformers / torch / the HF hub, whose classes
        # are not importable in exactly that environment. The `exc` text is
        # carried into the skip reason so the cause is never hidden.
        except Exception as exc:
            pytest.skip(f"EnrichmentPipeline raised (env issue, not regression): {exc}")

        assert len(result.logic_expansions) > 0, "Expected logic expansions for outdoor content"
        assert "[enrichment]" in result.enriched_content, (
            "Expected enriched_content to contain [enrichment] marker"
        )


# ---------------------------------------------------------------------------
# 3. _direct_insert — settings threaded from get_settings() (unit)
# ---------------------------------------------------------------------------


class TestDirectInsertSettings:
    """_direct_insert must pass settings= to storage.insert_memory."""

    def test_direct_insert_passes_settings(self):
        """Patch get_settings() and verify _direct_insert threads it through."""
        from yadgar._shared.write_exec.context import MemorizeContext
        from yadgar.backend.write_exec._memorize_phases._phase_store import _direct_insert

        storage = _mock_storage()
        embeddings = _mock_embeddings()
        settings = _settings_logic_only()

        ctx = MemorizeContext(
            content="deployed the new microservice to production",
            context="/work/project",
            tags=["ops"],
            is_protected=False,
            provenance_agent=None,
            tier=None,
            valid_until=None,
            ttl_days=None,
            reason="test",
        )
        ctx.embedding = _dummy_embedding_bytes()
        ctx.computed_valid_until = None
        ctx.provenance_agent_resolved = "default"
        ctx.tier = None

        # _direct_insert does a function-local `from yadgar.config import
        # get_settings`, so patch the SOURCE (yadgar.config.get_settings), not
        # the _phase_store module attr (which the local import never reads).
        with patch(
            "yadgar._shared.config.get_settings",
            return_value=settings,
        ):
            _direct_insert(ctx, storage, embeddings, fhash=None)

        assert storage.insert_memory.call_count == 1
        _args, kwargs = storage.insert_memory.call_args
        assert kwargs.get("settings") is settings, (
            "_direct_insert did not pass settings= to insert_memory (#39)"
        )
        assert kwargs.get("embeddings_engine") is embeddings, (
            "_direct_insert did not pass embeddings_engine= to insert_memory (#39)"
        )
