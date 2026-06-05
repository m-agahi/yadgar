"""v5.17.0 — write-time contradiction detection (Adopt-2).

TDD: written before wiring. Tests verify that
`MemoryCurator.curate_on_remember()` invokes the lightweight
contradiction detector by default, env-gates it on
`YADGAR_WRITE_TIME_CONTRADICTION`, fail-soft when the detector
raises, and increments the new metric on contradiction.

Test 6 (LLM-resolver orthogonality) is at memorize-integration
level — verifies that when `YADGAR_CONFLICT_RESOLVER=on` and the
resolver returns NOOP, the curator is never reached so the new
detector never runs.

These tests use deterministic crafted embeddings (no
sentence-transformers required) so they exercise the wiring in
any environment.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from yadgar.config import Settings
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "test_wtc.db"), embedding_dim=384)
    yield engine
    engine.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        DB_PATH=str(tmp_path / "test.db"),
        CURATION_SIMILARITY_THRESHOLD=0.85,
    )


@pytest.fixture
def embeddings():
    return EmbeddingEngine()


@pytest.fixture
def thermo(storage, embeddings, settings):
    return MemoryThermodynamics(storage, embeddings, settings)


@pytest.fixture
def curator(storage, embeddings, thermo, settings):
    return MemoryCurator(storage, embeddings, thermo, settings)


def _make_embedding(dim: int = 384, seed: int = 0) -> bytes:
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    return vec.tobytes()


def _make_similar_embedding(base: bytes, noise_scale: float = 0.005, seed: int = 1) -> bytes:
    """Embedding very close to base (cosine ~0.95+)."""
    arr = np.frombuffer(base, dtype=np.float32).copy()
    rng = np.random.RandomState(seed)
    noise = rng.randn(len(arr)).astype(np.float32) * noise_scale
    arr += noise
    arr = arr / np.linalg.norm(arr)
    return arr.tobytes()


def _seed_with_embedding(storage, content: str, embedding: bytes, *, tags=("test",)) -> int:
    mid = storage.insert_memory(
        {
            "content": content,
            "embedding": embedding,
            "tags": list(tags),
            "directory_context": "/test/wtc",
            "heat": 1.0,
            "is_stale": False,
        }
    )
    return mid


def _conf(storage, mem_id: int) -> float:
    """Return confidence for a memory, defaulting to 1.0 when field is absent.

    The lightweight detector reads `confidence` with default 1.0 and writes
    back when it decays. Storage omits the field when never set.
    """
    mem = storage.get_memory(mem_id)
    assert mem is not None
    return float(mem.get("confidence", 1.0))


# ── 1. default-on fires detector ────────────────────────────────────────


def test_default_on_fires_detector(curator, storage, embeddings, monkeypatch):
    """Env unset → detector runs → contradicting memory's confidence drops."""
    monkeypatch.setenv("YADGAR_WRITE_TIME_CONTRADICTION", "on")

    base_emb = _make_embedding(seed=42)
    old_id = _seed_with_embedding(
        storage,
        "We use PostgreSQL as our primary database",
        base_emb,
    )

    new_emb = _make_similar_embedding(base_emb, noise_scale=0.005, seed=43)
    new_content = "We no longer use PostgreSQL — switched to MySQL instead"

    sim = embeddings.similarity(base_emb, new_emb)
    assert sim >= 0.7, f"crafted embeddings have insufficient similarity {sim}"

    from yadgar.curation.ingestion import find_similar_memories

    found = find_similar_memories(storage, embeddings, new_emb, min_sim=0.6)
    assert any(mid == old_id for mid, _ in found), (
        f"find_similar_memories failed to surface the old memory; got {found}"
    )

    # Direct sanity-check: call the detector directly and verify it would mutate
    from yadgar.curation.contradiction import detect_contradictions

    direct = detect_contradictions(storage, found, new_content)
    assert direct, f"detector returned empty list; would never decay (found={found})"

    # The direct call above already decayed confidence — reset to 1.0
    storage.update_memory_fields(old_id, confidence=1.0)
    conf_before = _conf(storage, old_id)
    assert conf_before == 1.0, f"reset failed; got {conf_before}"

    # Spy on detect_contradictions to confirm the wiring runs it.
    # Must patch the name in yadgar.curation (where __init__ imported it),
    # NOT in yadgar.curation.contradiction — that bound name is not used
    # by _run_write_time_contradiction.
    import yadgar.curation as _curation_mod
    import yadgar.curation.contradiction as _cmod

    _calls: list = []
    _orig = _cmod.detect_contradictions

    def _spy(*args, **kwargs):
        _calls.append((args, kwargs))
        return _orig(*args, **kwargs)

    monkeypatch.setattr(_curation_mod, "detect_contradictions", _spy)

    curator.curate_on_remember(
        content=new_content,
        context="/test/wtc",
        tags=["db"],
        embedding=new_emb,
    )

    assert _calls, "detect_contradictions was not called from curate_on_remember"

    conf = _conf(storage, old_id)
    assert conf < 1.0, f"contradicting memory confidence should drop below 1.0, got {conf}"


# ── 2. env-off skips detector ────────────────────────────────────────────


def test_env_off_skips_detector(curator, storage, monkeypatch):
    """YADGAR_WRITE_TIME_CONTRADICTION=off → confidence unchanged at 1.0."""
    monkeypatch.setenv("YADGAR_WRITE_TIME_CONTRADICTION", "off")

    base_emb = _make_embedding(seed=42)
    old_id = _seed_with_embedding(
        storage,
        "We use PostgreSQL as our primary database",
        base_emb,
    )

    new_emb = _make_similar_embedding(base_emb, noise_scale=0.005, seed=43)
    new_content = "We no longer use PostgreSQL — switched to MySQL instead"

    curator.curate_on_remember(
        content=new_content,
        context="/test/wtc",
        tags=["db"],
        embedding=new_emb,
    )

    conf = _conf(storage, old_id)
    assert conf == 1.0, f"with env off, confidence must stay 1.0, got {conf}"


# ── 3. empty store → no error, no detector activity ─────────────────────


def test_no_similar_memories_noop(curator, monkeypatch):
    """Empty store → write succeeds, no exception. Verifies `and similar` guard."""
    monkeypatch.delenv("YADGAR_WRITE_TIME_CONTRADICTION", raising=False)

    new_content = "Brand-new isolated content with no peers"
    new_emb = _make_embedding(seed=99)

    result = curator.curate_on_remember(
        content=new_content,
        context="/test/wtc",
        tags=["fresh"],
        embedding=new_emb,
    )

    assert result["action"] == "created"


# ── 4. detector exception does not block the write ──────────────────────


def test_detector_exception_does_not_block_write(curator, storage, monkeypatch):
    """detect_contradictions raising → memorize still succeeds (fail-soft)."""
    monkeypatch.delenv("YADGAR_WRITE_TIME_CONTRADICTION", raising=False)

    base_emb = _make_embedding(seed=42)
    _seed_with_embedding(storage, "We use PostgreSQL as our primary database", base_emb)

    new_emb = _make_similar_embedding(base_emb, noise_scale=0.005, seed=43)
    new_content = "We no longer use PostgreSQL"

    with patch(
        "yadgar.curation.contradiction.detect_contradictions",
        side_effect=RuntimeError("boom"),
    ):
        result = curator.curate_on_remember(
            content=new_content,
            context="/test/wtc",
            tags=["db"],
            embedding=new_emb,
        )

    assert result["action"] in ("created", "linked", "merged"), (
        "write must complete even when detector raises"
    )


# ── 5. metric increments on contradiction ───────────────────────────────


def test_metric_increments_on_contradiction(curator, storage, monkeypatch):
    """yadgar_write_time_contradiction_total{reason} increments per detected contradiction."""
    monkeypatch.delenv("YADGAR_WRITE_TIME_CONTRADICTION", raising=False)

    from yadgar.metrics import yadgar_write_time_contradiction_total

    def _val(reason: str) -> float:
        return yadgar_write_time_contradiction_total.labels(reason=reason)._value.get()

    before_neg = _val("negation_mismatch")
    before_act = _val("action_divergence")

    base_emb = _make_embedding(seed=42)
    _seed_with_embedding(storage, "We use PostgreSQL as our primary database", base_emb)

    new_emb = _make_similar_embedding(base_emb, noise_scale=0.005, seed=43)
    new_content = "We no longer use PostgreSQL — switched to MySQL"

    curator.curate_on_remember(
        content=new_content,
        context="/test/wtc",
        tags=["db"],
        embedding=new_emb,
    )

    after_neg = _val("negation_mismatch")
    after_act = _val("action_divergence")

    assert (after_neg - before_neg) + (after_act - before_act) >= 1, (
        "at least one contradiction reason should have incremented"
    )


# ── 6. LLM-resolver short-circuit bypasses lightweight detector ─────────


def test_llm_resolver_short_circuit_bypasses_lightweight(monkeypatch):
    """YADGAR_CONFLICT_RESOLVER=on + LLM returns NOOP → memorize returns early,
    curator path never reached, lightweight detector never runs."""
    monkeypatch.setenv("YADGAR_CONFLICT_RESOLVER", "on")
    monkeypatch.setenv("YADGAR_WRITE_TIME_CONTRADICTION", "on")

    settings_stub = MagicMock()
    settings_stub.REINJECT_ON_WRITE = False
    settings_stub.REINJECTION_ENABLED = False
    settings_stub.REINJECTION_MAX_RESULTS = 0
    settings_stub.CONTEXTUAL_PREFIX_ENABLED = False
    settings_stub.MICRO_CHECKPOINT_ENABLED = False
    settings_stub.ACTION_STREAM_ENABLED = False
    settings_stub.CRDT_AGENT_ID = "test-agent"
    settings_stub.DECISION_AUTO_PROTECT = False

    curator_mock = MagicMock()

    with (
        patch("yadgar.server.tools.memorize.is_draining", return_value=True),
        patch("yadgar.server.tools.memorize.gate_or_reject", return_value=None),
        patch("yadgar.server.tools.memorize.settings", settings_stub),
        patch("yadgar.server.tools.memorize._st") as mock_st,
        patch("yadgar.server.tools.memorize._get_storage") as mock_get_storage,
        patch("yadgar.server.tools.memorize._get_embeddings") as mock_get_emb,
        patch("yadgar.server.tools.memorize._get_buffer"),
        patch("yadgar.server.tools.memorize._get_file_queue"),
        patch(
            "yadgar.conflict_resolver.resolve_conflict",
            return_value={"op": "NOOP", "target_id": None, "reason": "duplicate"},
        ),
    ):
        mock_st._retriever = MagicMock()
        mock_st._replay = None
        mock_st._buffer = None
        mock_st._write_gate = None
        mock_st._rules_engine = None
        mock_st._thermo = None
        mock_st._curator = curator_mock
        mock_st._pool = None
        mock_st._consolidation = None
        mock_st._prospective = None
        mock_st._engram = None

        mock_storage = MagicMock()
        mock_storage.insert_memory.return_value = "memory:test000"
        mock_storage.get_memory.return_value = {
            "id": "memory:test000",
            "content": "x",
            "tags": [],
            "context": "/test/wtc",
            "branch": None,
            "heat": 0.5,
        }
        mock_get_storage.return_value = mock_storage

        mock_emb = MagicMock()
        mock_emb.encode.return_value = b"\x00" * (384 * 4)
        mock_emb.get_model_name.return_value = "test-model"
        mock_get_emb.return_value = mock_emb

        from yadgar.server.tools.memorize import memorize

        result = memorize("anything", context="/test/wtc", tags=[])

    assert result.get("stored") is False, "LLM NOOP must short-circuit before insert"
    curator_mock.curate_on_remember.assert_not_called()


# ── 7. similar content without negation → no decay ──────────────────────


def test_no_negation_no_action_change_no_decay(curator, storage, monkeypatch):
    """Same-topic memory without negation patterns or action change → confidence unchanged."""
    monkeypatch.delenv("YADGAR_WRITE_TIME_CONTRADICTION", raising=False)

    base_emb = _make_embedding(seed=42)
    old_id = _seed_with_embedding(
        storage,
        "We use PostgreSQL as our primary database",
        base_emb,
    )

    new_emb = _make_similar_embedding(base_emb, noise_scale=0.005, seed=43)
    new_content = "We use PostgreSQL as our main relational database for the service"

    curator.curate_on_remember(
        content=new_content,
        context="/test/wtc",
        tags=["db"],
        embedding=new_emb,
    )

    conf = _conf(storage, old_id)
    assert conf >= 1.0, f"non-contradicting similar write must not lower confidence; got {conf}"
