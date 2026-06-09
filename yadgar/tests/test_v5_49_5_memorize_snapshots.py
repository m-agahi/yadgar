"""v5.49.5 snapshot harness — golden-output tests for memorize() behavior.

Captures v5.49.4 behavior before the refactor. Must pass identically
before AND after the v5.49.5 refactor (behavior preservation contract).

Non-deterministic fields (queue_id UUIDs, memory id, timestamps, vector_clock)
are normalized/masked before comparison so tests are stable across runs.

Tests 1–6 per plan § 5.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Snapshot dir
# ---------------------------------------------------------------------------

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
_SNAPSHOT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


def _normalize(obj):
    """Recursively normalize non-deterministic values for snapshot comparison."""
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str):
        s = _UUID_RE.sub("<UUID>", obj)
        s = _ISO_TS_RE.sub("<TIMESTAMP>", s)
        return s
    if isinstance(obj, int) and obj > 10_000:
        # Large integers are likely memory IDs — normalize
        return "<ID>"
    return obj


def _save_snapshot(name: str, data: dict) -> None:
    p = _SNAPSHOT_DIR / f"{name}.json"
    p.write_text(json.dumps(_normalize(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_snapshot(name: str) -> dict | None:
    p = _SNAPSHOT_DIR / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _assert_matches_snapshot(name: str, actual: dict) -> None:
    """Compare normalized actual to saved snapshot, saving if first run."""
    normalized = _normalize(actual)
    existing = _load_snapshot(name)
    if existing is None:
        _save_snapshot(name, actual)
        return  # First run: save snapshot, test passes
    assert normalized == existing, (
        f"Snapshot mismatch for '{name}'.\n"
        f"Expected: {json.dumps(existing, indent=2)}\n"
        f"Actual:   {json.dumps(normalized, indent=2)}"
    )


# ---------------------------------------------------------------------------
# Shared mock environment builder (mirrors test_memorize_anchor_parity.py)
# ---------------------------------------------------------------------------

_FIXED_MEMORY_ID = 42


def _make_mock_settings(**overrides):
    defaults = {
        "ANCHOR_CONDITIONAL_TTL_DAYS": 90,
        "ANCHOR_EPHEMERAL_TTL_DAYS": 14,
        "ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON": False,
        "DECISION_AUTO_PROTECT": False,
        "CONTEXTUAL_PREFIX_ENABLED": False,
        "REINJECT_ON_WRITE": False,
        "REINJECTION_ENABLED": False,
        "REINJECTION_MAX_RESULTS": 3,
        "MICRO_CHECKPOINT_ENABLED": False,
        "CRDT_AGENT_ID": "test-agent",
        "HOT_THRESHOLD": 0.5,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _build_sync_env(monkeypatch, *, memory_id=_FIXED_MEMORY_ID, get_memory_return=None):
    """Set up sync (is_draining=True) environment with deterministic mocks.

    Returns dict with storage, embeddings, buffer mocks.
    """
    import yadgar.file_queue as _fq
    import yadgar.server._state as _st

    _mem_mod = importlib.import_module("yadgar.server.tools.memorize")

    monkeypatch.setattr(_fq, "is_draining", lambda: True)
    monkeypatch.setattr(_mem_mod, "is_draining", lambda: True)

    if get_memory_return is None:
        get_memory_return = {
            "id": memory_id,
            "content": "snapshot test content",
            "tags": ["test", "snapshot"],
            "heat": 1.0,
            "is_protected": False,
            "tier": None,
            "valid_until": None,
            "directory_context": "/tmp/snapshot-test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "file_hash": None,
            "embedding_model": "test-model",
            "provenance_agent": "default",
        }

    mock_storage = MagicMock()
    mock_storage.insert_memory.return_value = memory_id
    mock_storage.get_memory.return_value = get_memory_return
    mock_storage.update_memory_fields.return_value = None
    mock_storage.update_memory_scores.return_value = None
    mock_storage.upsert_file_hash.return_value = None
    mock_storage._q.return_value = []

    mock_embeddings = MagicMock()
    mock_embeddings.encode.return_value = [0.1, 0.2, 0.3]
    mock_embeddings.get_model_name.return_value = "test-model"

    mock_buffer = MagicMock()
    mock_buffer.capture.return_value = None
    mock_buffer.capture_action.return_value = None

    monkeypatch.setattr(_st, "_storage", mock_storage)
    monkeypatch.setattr(_st, "_embeddings", mock_embeddings)
    monkeypatch.setattr(_st, "_buffer", mock_buffer)
    monkeypatch.setattr(_st, "_curator", None)
    monkeypatch.setattr(_st, "_thermo", None)
    monkeypatch.setattr(_st, "_write_gate", None)
    monkeypatch.setattr(_st, "_retriever", None)
    monkeypatch.setattr(_st, "_consolidation", None)
    monkeypatch.setattr(_st, "_pool", None)
    monkeypatch.setattr(_st, "_prospective", None)
    monkeypatch.setattr(_st, "_engram", None)
    monkeypatch.setattr(_st, "_replay", None)
    monkeypatch.setattr(_st, "_rules_engine", None)

    monkeypatch.setattr("yadgar.server.lifecycle._get_storage", lambda: mock_storage)
    monkeypatch.setattr("yadgar.server.lifecycle._get_embeddings", lambda: mock_embeddings)
    monkeypatch.setattr("yadgar.server.lifecycle._get_buffer", lambda: mock_buffer)

    mock_settings = _make_mock_settings()
    monkeypatch.setattr(_mem_mod, "settings", mock_settings)

    return {
        "storage": mock_storage,
        "embeddings": mock_embeddings,
        "buffer": mock_buffer,
        "settings": mock_settings,
        "mem_mod": _mem_mod,
    }


# ---------------------------------------------------------------------------
# Test 1 — accept path returns expected dict
# ---------------------------------------------------------------------------


def test_memorize_accept_path_returns_expected_dict(monkeypatch, tmp_path):
    """memorize() sync accept path returns dict with stable shape.

    Snapshot: memorize_accept_v5_49_4.json
    """
    _build_sync_env(monkeypatch)
    from yadgar.server.tools.memorize import memorize

    with patch("yadgar.server._detect_branch", return_value="feat/snapshot-test"):
        result = memorize(
            content="snapshot test content",
            context="/tmp/snapshot-test",
            tags=["test", "snapshot"],
        )

    # Core shape assertions (stable regardless of snapshot)
    assert result.get("id") == _FIXED_MEMORY_ID
    assert result.get("content") == "snapshot test content"
    assert result.get("curation_action") in ("created", "merged", "updated", None)

    _assert_matches_snapshot("memorize_accept_v5_49_4", result)


# ---------------------------------------------------------------------------
# Test 2 — reject: duplicate / similarity-gate (write-gate rejection)
# ---------------------------------------------------------------------------


def test_memorize_reject_write_gate_returns_expected_dict(monkeypatch, tmp_path):
    """memorize() write-gate rejection (surprisal too low) returns stable dict.

    Snapshot: memorize_reject_write_gate_v5_49_4.json
    """
    import yadgar.server._state as _st

    _build_sync_env(monkeypatch)

    mock_gate = MagicMock()
    mock_gate.should_store.return_value = (False, 0.05, "low_surprise")
    monkeypatch.setattr(_st, "_write_gate", mock_gate)

    from yadgar.server.tools.memorize import memorize

    with patch("yadgar.server._detect_branch", return_value="feat/snapshot-test"):
        result = memorize(
            content="snapshot test content for gate rejection",
            context="/tmp/snapshot-test",
            tags=["test"],
        )

    assert result.get("stored") is False
    assert "surprisal" in result or "reason" in result

    _assert_matches_snapshot("memorize_reject_write_gate_v5_49_4", result)


# ---------------------------------------------------------------------------
# Test 3 — reject: missing branch
# ---------------------------------------------------------------------------


def test_memorize_reject_missing_branch_returns_expected_dict(monkeypatch, tmp_path):
    """memorize() with no branch context returns missing_branch error dict.

    Snapshot: memorize_reject_missing_branch_v5_49_4.json
    """
    import importlib

    import yadgar.file_queue as _fq
    import yadgar.server._state as _st

    _mem_mod = importlib.import_module("yadgar.server.tools.memorize")

    # NOT draining — fast path, branch check fires
    monkeypatch.setattr(_fq, "is_draining", lambda: False)
    monkeypatch.setattr(_mem_mod, "is_draining", lambda: False)

    mock_settings = _make_mock_settings()
    monkeypatch.setattr(_mem_mod, "settings", mock_settings)
    monkeypatch.setattr(_st, "_rules_engine", None)

    from yadgar.server.tools.memorize import memorize

    # No branch_hint, _detect_branch returns None, no YADGAR_CI_BRANCH env
    with (
        patch("yadgar.server._detect_branch", return_value=None),
        patch.dict("os.environ", {}, clear=False),
    ):
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)
        # gate_or_reject must pass (no secret content)
        with patch("yadgar.server.tools.memorize.gate_or_reject", return_value=None):
            result = memorize(
                content="missing branch test content",
                context="/tmp/snapshot-test",
                tags=["test"],
            )

    assert result.get("stored") is False
    assert result.get("error") == "missing_branch"

    _assert_matches_snapshot("memorize_reject_missing_branch_v5_49_4", result)


# ---------------------------------------------------------------------------
# Test 4 — reject: secret leak detected
# ---------------------------------------------------------------------------


def test_memorize_reject_secret_leak_returns_expected_dict(monkeypatch, tmp_path):
    """memorize() with secret content returns gate rejection dict.

    Snapshot: memorize_reject_secret_leak_v5_49_4.json
    """
    from yadgar.server.tools.memorize import memorize

    # gate_or_reject returns a rejection dict for secrets
    _secret_reject = {"stored": False, "reason": "secret_detected", "pattern": "aws_key"}

    with patch("yadgar.server.tools.memorize.gate_or_reject", return_value=_secret_reject):
        result = memorize(
            content="AKIAIOSFODNN7EXAMPLE secret key",
            context="/tmp/snapshot-test",
            tags=["test"],
        )

    assert result.get("stored") is False

    _assert_matches_snapshot("memorize_reject_secret_leak_v5_49_4", result)


# ---------------------------------------------------------------------------
# Test 5 — reject: invalid tier
# ---------------------------------------------------------------------------


def test_memorize_reject_invalid_tier_returns_expected_dict(monkeypatch, tmp_path):
    """memorize() with invalid tier returns validation rejection dict.

    Snapshot: memorize_reject_invalid_tier_v5_49_4.json
    """
    from yadgar.server.tools.memorize import memorize

    result = memorize(
        content="tier validation test",
        context="/tmp/snapshot-test",
        tags=["test"],
        tier="bogus_tier",
    )

    assert result.get("stored") is False
    assert "invalid tier" in result.get("reason", "")

    _assert_matches_snapshot("memorize_reject_invalid_tier_v5_49_4", result)


# ---------------------------------------------------------------------------
# Test 6 — queue side-effects (fast path enqueues correctly)
# ---------------------------------------------------------------------------


def test_memorize_writes_to_queue(monkeypatch, tmp_path):
    """memorize() fast path enqueues a job with the correct payload shape.

    Captures: queue_id returned + job schema (fields present).
    Not a byte-for-byte snapshot — asserts structural invariants.
    """
    import importlib

    import yadgar.file_queue as _fq
    import yadgar.server._state as _st

    _mem_mod = importlib.import_module("yadgar.server.tools.memorize")

    monkeypatch.setattr(_fq, "is_draining", lambda: False)
    monkeypatch.setattr(_mem_mod, "is_draining", lambda: False)

    mock_settings = _make_mock_settings()
    monkeypatch.setattr(_mem_mod, "settings", mock_settings)
    monkeypatch.setattr(_st, "_rules_engine", None)

    captured_payloads: list[dict] = []

    def _mock_enqueue(op_type: str, payload: dict) -> str:
        captured_payloads.append({"op_type": op_type, "payload": dict(payload)})
        return "mock-queue-id-001"

    mock_fq = MagicMock()
    mock_fq.enqueue.side_effect = _mock_enqueue

    monkeypatch.setattr(_mem_mod, "_get_file_queue", lambda: mock_fq)

    from yadgar.server.tools.memorize import memorize

    with (
        patch("yadgar.server._detect_branch", return_value="feat/queue-test"),
        patch("yadgar.server.tools.memorize.gate_or_reject", return_value=None),
    ):
        result = memorize(
            content="queue side-effect test content",
            context="/tmp/queue-test",
            tags=["test", "queue"],
        )

    assert result.get("stored") is True
    assert result.get("queued") is True
    assert result.get("queue_id") == "mock-queue-id-001"

    assert len(captured_payloads) == 1, f"Expected 1 enqueue call, got {len(captured_payloads)}"
    payload = captured_payloads[0]
    assert payload["op_type"] == "memorize"
    pdata = payload["payload"]
    assert pdata["content"] == "queue side-effect test content"
    assert pdata["context"] == "/tmp/queue-test"
    assert "test" in pdata["tags"]
    assert pdata["branch"] == "feat/queue-test"

    # Structural snapshot (masks UUID queue_id but preserves payload shape)
    snapshot_data = {
        "result_keys": sorted(result.keys()),
        "result_stored": result["stored"],
        "result_queued": result["queued"],
        "enqueue_count": len(captured_payloads),
        "payload_fields": sorted(pdata.keys()),
        "payload_op": payload["op_type"],
    }
    _assert_matches_snapshot("memorize_queue_v5_49_4", snapshot_data)
