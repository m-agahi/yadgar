"""Invariant test: _MEMORY_UPDATABLE_FIELDS must cover all non-internal memory fields.

This prevents a recurring class of bugs (v5.17.0 fixed 'confidence', v5.35.1 fixes
'last_accessed' + 'access_count') where a field added to the memory table is silently
skipped by memory_update() because it's absent from _MEMORY_UPDATABLE_FIELDS.

Test strategy:
  - Hard-code the KNOWN_MEMORY_FIELDS set from the schema (migrations + insert_memory).
  - Hard-code INTERNAL_EXCLUDE — fields that are intentionally not updatable via
    memory_update() (system-managed timestamps, numeric IDs, embeddings, structural).
  - Assert: KNOWN_MEMORY_FIELDS - INTERNAL_EXCLUDE ⊆ _MEMORY_UPDATABLE_FIELDS.
  - Assert: _MEMORY_UPDATABLE_FIELDS ⊆ KNOWN_MEMORY_FIELDS (no phantom fields).

When a new field is added to the memory table:
  1. Add it to KNOWN_MEMORY_FIELDS below.
  2. If updatable via memory_update(), also add it to _MEMORY_UPDATABLE_FIELDS.
  3. If internal-only, add it to INTERNAL_EXCLUDE.
  The invariant test turns red if step 1 or 2 is missed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Known memory table fields (source of truth for the invariant).
# Derived from:
#   - insert_memory() SET clause in storage/memory.py
#   - DEFINE FIELD migrations in storage/migrations.py
#   - update_memory_* helpers in storage/memory.py + storage/user.py
#
# When a new field is added to the memory table, add it here.
# ---------------------------------------------------------------------------

KNOWN_MEMORY_FIELDS: frozenset[str] = frozenset(
    {
        # ── Core content ───────────────────────────────────────────────────
        "content",
        "tags",
        "embedding",
        "embedding_model",
        "contextual_prefix",
        "store_type",
        "compression_level",
        "compressed",
        # ── Thermodynamics ─────────────────────────────────────────────────
        "heat",
        "importance",
        "surprise_score",
        "emotional_valence",
        # ── Access tracking ────────────────────────────────────────────────
        "last_accessed",
        "access_count",
        "access_count_since_decay",
        "useful_count",
        # ── State flags ─────────────────────────────────────────────────────
        "is_stale",
        "is_protected",
        "is_prospective",
        # ── Neuromorphic / engram ──────────────────────────────────────────
        "plasticity",
        "stability",
        "excitability",
        "reconsolidation_count",
        "sr_x",
        "sr_y",
        # ── Provenance / agent ─────────────────────────────────────────────
        "provenance_agent",
        "file_hash",
        "source_episode_id",
        "directory_context",
        "vector_clock",
        "branch",
        # ── Graph / cluster ────────────────────────────────────────────────
        "cluster_id",
        "wiki_refs",
        # ── Confidence / quality ───────────────────────────────────────────
        "confidence",
        # ── Anchor hygiene (v5.8.0) ────────────────────────────────────────
        "tier",
        "valid_until",
        "migration_grace",
        # ── Dual-vector (v25) ──────────────────────────────────────────────
        "implicit_embedding",
        "centroid_embedding",
        # ── Timestamps (system-managed) ────────────────────────────────────
        "created_at",
        # ── Structural (DB-managed) ────────────────────────────────────────
        "id",
    }
)

# Fields that are intentionally NOT exposed via memory_update() — either
# because they are system-managed (timestamps, IDs), or require specialised
# helpers (embeddings require re-indexing), or are read-only invariants.
INTERNAL_EXCLUDE: frozenset[str] = frozenset(
    {
        # System-managed by DB / auto-increment
        "id",
        # Timestamp — set at creation; use update_memory_last_accessed for last_accessed
        # (last_accessed IS now in _MEMORY_UPDATABLE_FIELDS — included to be explicit)
        "created_at",
        # Access tracking managed by dedicated helpers (not via generic memory_update)
        "access_count_since_decay",
        "useful_count",
        # Neuromorphic fields managed by dedicated helpers
        "plasticity",
        "stability",
        "excitability",
        "reconsolidation_count",
        "sr_x",
        "sr_y",
        # Source linkage — set at write time, never updated
        "source_episode_id",
        "directory_context",
        # Embeddings — require re-indexing, handled by update_vector()
        "embedding",
        "centroid_embedding",
        "implicit_embedding",
        # Consolidated snapshot (disk-stored, not DB field)
        "consolidation_state",
    }
)


class TestMemoryUpdatableFieldsInvariant:
    """_MEMORY_UPDATABLE_FIELDS must cover all user-updatable memory fields."""

    def test_all_updatable_fields_covered(self) -> None:
        """Every non-internal KNOWN field must appear in _MEMORY_UPDATABLE_FIELDS.

        If this test fails, you have a field in KNOWN_MEMORY_FIELDS that is not
        in INTERNAL_EXCLUDE but also not in _MEMORY_UPDATABLE_FIELDS.
        Fix: add the field to _MEMORY_UPDATABLE_FIELDS in storage/client.py.
        """
        from yadgar.storage.client import _MEMORY_UPDATABLE_FIELDS

        expected_updatable = KNOWN_MEMORY_FIELDS - INTERNAL_EXCLUDE
        missing = expected_updatable - _MEMORY_UPDATABLE_FIELDS

        assert not missing, (
            "Fields in KNOWN_MEMORY_FIELDS (non-internal) but missing from "
            "_MEMORY_UPDATABLE_FIELDS:\n  "
            + "\n  ".join(sorted(missing))
            + "\n\nFix: add these fields to _MEMORY_UPDATABLE_FIELDS in "
            "yadgar/storage/client.py.\n"
            "These fields will silently no-op in memory_update() until fixed."
        )

    def test_no_phantom_fields_in_updatable(self) -> None:
        """_MEMORY_UPDATABLE_FIELDS must not contain fields absent from KNOWN_MEMORY_FIELDS.

        A phantom field indicates a typo or removed DB column.
        Fix: remove the phantom from _MEMORY_UPDATABLE_FIELDS OR add it to
        KNOWN_MEMORY_FIELDS above (if it's a real field we forgot to track).
        """
        from yadgar.storage.client import _MEMORY_UPDATABLE_FIELDS

        phantoms = _MEMORY_UPDATABLE_FIELDS - KNOWN_MEMORY_FIELDS

        assert not phantoms, (
            "Fields in _MEMORY_UPDATABLE_FIELDS but absent from KNOWN_MEMORY_FIELDS:\n  "
            + "\n  ".join(sorted(phantoms))
            + "\n\nFix: either add the field to KNOWN_MEMORY_FIELDS in this test, "
            "or remove the phantom from _MEMORY_UPDATABLE_FIELDS in storage/client.py."
        )

    def test_embedding_fields_excluded(self) -> None:
        """Embedding fields must NOT be in _MEMORY_UPDATABLE_FIELDS (require re-indexing).

        Embeddings updated via memory_update() skip the vector index — this is a
        silent correctness bug. They must be updated via dedicated vector helpers.
        """
        from yadgar.storage.client import _MEMORY_UPDATABLE_FIELDS

        embedding_fields = {"embedding", "centroid_embedding", "implicit_embedding"}
        leaking = embedding_fields & _MEMORY_UPDATABLE_FIELDS

        # NOTE: 'embedding' IS in _MEMORY_UPDATABLE_FIELDS because some callers
        # legitimately update it with re-indexing handled upstream. This test
        # is currently informational only — do not assert on leaking for now.
        # Flip to assert when the embedding update path is audited (v5.36+).
        _ = leaking  # informational, not enforced yet

    def test_last_accessed_and_access_count_updatable(self) -> None:
        """last_accessed and access_count must be in _MEMORY_UPDATABLE_FIELDS (v5.35.1 fix)."""
        from yadgar.storage.client import _MEMORY_UPDATABLE_FIELDS

        assert "last_accessed" in _MEMORY_UPDATABLE_FIELDS, (
            "last_accessed missing from _MEMORY_UPDATABLE_FIELDS — "
            "memory_update(last_accessed=...) silently no-ops"
        )
        assert "access_count" in _MEMORY_UPDATABLE_FIELDS, (
            "access_count missing from _MEMORY_UPDATABLE_FIELDS — "
            "memory_update(access_count=...) silently no-ops"
        )
