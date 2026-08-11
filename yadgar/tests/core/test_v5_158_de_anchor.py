"""Car #85 — de_anchor tool + memory_update allowlist widening.

The anchor-audit maintenance car adds a core MCP tool ``de_anchor(memory_id)``
that RETIRES an anchor so it re-enters the normal heat-decay path:

  is_protected → False   (re-enables decay; the decay query excludes protected
                          rows — memory.py:526/554)
  importance   → 0.5     (re-enables the FAST decay factor; compute_decay uses
                          IMPORTANCE_DECAY_FACTOR=0.9999 only when importance>0.7,
                          so an anchor left at importance=1.0 barely decays even
                          after is_protected is cleared — thermodynamics.py:186)
  tags         → strip `_anchor` + any `anchor:*` tag
  tier         → cleared (cosmetic; tier does NOT drive decay)

The discriminating test proves BOTH halves matter:
  - clearing is_protected makes the row appear in the decay query, and
  - resetting importance makes it actually go cold within ~2 years.

Also covers the allowlist widening: memory_update must accept ``importance`` and
``tier`` now (previously silently rejected), while still rejecting unknown keys.
"""

from __future__ import annotations

import pytest

from yadgar._shared.config import get_settings
from yadgar._shared.thermodynamics.thermodynamics import MemoryThermodynamics
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "de_anchor.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _insert_anchor(content: str = "Immortal workflow rule that must survive compaction") -> int:
    """Insert a fully-anchored memory: is_protected=True, importance=1.0, heat=1.0."""
    storage = server._get_storage()
    embeddings = server._get_embeddings()
    mid = storage.insert_memory(
        {
            "content": content,
            "embedding": embeddings.encode(content),
            "tags": ["_anchor", "anchor:workflow-rule", "yadgar"],
            "store_type": "episodic",
            "directory_context": "/home/user/project",
            "heat": 1.0,
            "importance": 1.0,
            "is_protected": True,
            "tier": "conditional",
            "is_stale": False,
            "file_hash": None,
            "embedding_model": embeddings.get_model_name(),
            "project_id": TEST_PROJECT_ID,
        }
    )
    return mid


@pytest.mark.usefixtures("admin_backend_bypass")
class TestDeAnchor:
    def test_de_anchor_retires_row_and_lets_it_decay_cold(self):
        """DISCRIMINATING: de_anchor must clear protection AND reset importance so a
        de-anchored memory goes below COLD_THRESHOLD after ~2 years of no access.

        Left at importance=1.0 this fails (IMPORTANCE_DECAY_FACTOR keeps heat ~0.2).
        """
        mid = _insert_anchor()
        server.de_anchor(mid)

        row = server.memory_get(mid)
        assert row is not None
        # Field half.
        assert row["is_protected"] is False, "de_anchor must clear is_protected"
        assert abs(float(row["importance"]) - 0.5) < 1e-9, "de_anchor must reset importance to 0.5"
        assert "_anchor" not in (row.get("tags") or []), "de_anchor must strip _anchor tag"
        assert not any(str(t).startswith("anchor:") for t in (row.get("tags") or [])), (
            "de_anchor must strip anchor:* tags"
        )

        # is_protected clear is load-bearing: the decay query only returns rows
        # where is_protected is false/NONE. A still-protected row is excluded.
        storage = server._get_storage()
        decay_ids = {r["id"] for r in storage.get_all_memories_for_decay_scalar()}
        assert mid in decay_ids, "de-anchored row must be eligible for decay"

        # importance reset is load-bearing: at importance=0.5 the row uses the fast
        # DECAY_FACTOR and goes cold; at importance=1.0 it would stay ~0.2.
        thermo = MemoryThermodynamics(storage, server._get_embeddings(), get_settings())
        decayed_row = storage.get_memory(mid)
        new_heat = thermo.compute_decay(decayed_row, hours_elapsed=17520)  # ~2 years
        cold = get_settings().COLD_THRESHOLD
        assert new_heat < cold, (
            f"de-anchored heat {new_heat:.5f} must fall below COLD_THRESHOLD {cold} "
            "after 2y — proves importance was reset to 0.5 (importance=1.0 would stay hot)"
        )

    def test_de_anchor_missing_memory_returns_error_not_crash(self):
        """A non-existent memory_id must return an error dict, not raise."""
        result = server.de_anchor(999999999)
        assert isinstance(result, dict)
        assert result.get("ok") is False or "error" in result


@pytest.mark.usefixtures("admin_backend_bypass")
class TestMemoryUpdateAllowlistWidened:
    def _insert(self) -> int:
        return server._get_storage().insert_memory(
            {
                "content": "row for allowlist test",
                "tags": ["t"],
                "store_type": "episodic",
                "heat": 0.7,
                "importance": 1.0,
                "directory_context": "/tmp/test",
                "project_id": TEST_PROJECT_ID,
            }
        )

    def test_memory_update_accepts_importance(self):
        mid = self._insert()
        result = server.memory_update(mid, {"importance": 0.5})
        assert abs(float(result["importance"]) - 0.5) < 1e-9

    def test_memory_update_accepts_tier(self):
        mid = self._insert()
        # tier is a plain string field; just prove it is no longer rejected.
        server.memory_update(mid, {"tier": "ephemeral"})
        assert server.memory_get(mid)["tier"] == "ephemeral"

    def test_memory_update_still_rejects_unknown_key(self):
        mid = self._insert()
        with pytest.raises(ValueError):
            server.memory_update(mid, {"heat": 0.9})
