"""v5.46.6 — B15: NLI spy patches the correct module binding.

Meta-test verifying that:
1. `yadgar.curation` exports `detect_contradictions` as a bound name
   (i.e. it can be patched via `monkeypatch.setattr(yadgar.curation, ...)`).
2. Patching the bound name in `yadgar.curation` causes spy calls to appear
   when `curate_on_remember` is invoked, whereas patching only
   `yadgar.curation.contradiction` does NOT intercept the call.

This guards against regression where patching the source module
(`yadgar.curation.contradiction.detect_contradictions`) fails to intercept
calls made through the bound name that `__init__` imported.
"""

from __future__ import annotations

import numpy as np
import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar.core.curation import MemoryCurator


def _make_embedding(seed: int = 0) -> bytes:
    rng = np.random.RandomState(seed)
    vec = rng.randn(384).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tobytes()


def _make_similar_embedding(base: bytes, noise_scale: float = 0.005, seed: int = 1) -> bytes:
    arr = np.frombuffer(base, dtype=np.float32).copy()
    rng = np.random.RandomState(seed)
    noise = rng.randn(len(arr)).astype(np.float32) * noise_scale
    arr += noise
    return (arr / np.linalg.norm(arr)).tobytes()


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "spy_test.db"), embedding_dim=384)
    yield engine
    engine.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(DB_PATH=str(tmp_path / "spy_test.db"))


@pytest.fixture
def curator(storage, settings):
    embeddings = EmbeddingEngine()
    thermo = MemoryThermodynamics(storage, embeddings, settings)
    return MemoryCurator(storage, embeddings, thermo, settings)


class TestNLISpyModuleBinding:
    """detect_contradictions bound name in yadgar.curation is the correct patch target."""

    def test_yadgar_curation_exports_detect_contradictions(self):
        """yadgar.curation must expose detect_contradictions as a direct attribute."""
        import yadgar.core.curation as _cmod

        assert hasattr(_cmod, "detect_contradictions"), (
            "yadgar.curation must export detect_contradictions (imported in __init__)"
        )

    def test_bound_name_is_same_function_as_source(self):
        """yadgar.curation.detect_contradictions and yadgar.curation.contradiction.detect_contradictions
        must be the same underlying function object (pre-patch identity check)."""
        import yadgar.core.curation as _pkg
        import yadgar.core.curation.contradiction as _src

        assert _pkg.detect_contradictions is _src.detect_contradictions, (
            "yadgar.curation.detect_contradictions must be the same object as "
            "yadgar.curation.contradiction.detect_contradictions at import time"
        )

    def test_patching_curation_init_intercepts_calls(self, storage, curator, monkeypatch):
        """Spy on yadgar.curation.detect_contradictions captures calls from curator."""
        import yadgar.core.curation as _curation_mod
        import yadgar.core.curation.contradiction as _cont_mod

        monkeypatch.setenv("YADGAR_WRITE_TIME_CONTRADICTION", "on")

        base_emb = _make_embedding(seed=42)
        storage.insert_memory(
            {
                "content": "We use PostgreSQL as our primary database",
                "embedding": base_emb,
                "tags": ["db"],
                "directory_context": "/test/spy",
            }
        )

        new_emb = _make_similar_embedding(base_emb, noise_scale=0.005, seed=43)

        _calls: list = []
        _orig = _cont_mod.detect_contradictions

        def _spy(*args, **kwargs):
            _calls.append((args, kwargs))
            return _orig(*args, **kwargs)

        # Patch the bound name in the package, NOT the source module.
        monkeypatch.setattr(_curation_mod, "detect_contradictions", _spy)

        curator.curate_on_remember(
            content="We no longer use PostgreSQL — switched to MySQL instead",
            context="/test/spy",
            tags=["db"],
            embedding=new_emb,
        )

        assert _calls, (
            "spy was never called — patch target is wrong or "
            "_run_write_time_contradiction is not being reached. "
            "Correct patch target is yadgar.curation.detect_contradictions, "
            "NOT yadgar.curation.contradiction.detect_contradictions."
        )
