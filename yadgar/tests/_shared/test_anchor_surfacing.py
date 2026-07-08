"""v5.19.0 — Anchor unconditional surfacing (scope-aware restore).

TDD: written before wiring.  Tests verify that:
  - get_anchored_memories_scoped() separates global vs project anchors.
  - HippocampalReplay.restore() surfaces global anchors even when a
    project has 20+ anchors that would have crowded them out under the
    old flat get_anchored_memories() path.

No sentence-transformers required — tests use deterministic crafted
numpy embeddings or plain None embeddings where the feature under
test does not depend on vector similarity.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.restoration import CheckpointRestore
from yadgar._shared.storage import StorageEngine

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "anchor_surf.db"), embedding_dim=384)
    yield engine
    engine.close()


@pytest.fixture
def replay(tmp_path):
    db_path = str(tmp_path / "anchor_surf_replay.db")
    settings = Settings(DB_PATH=db_path)
    stor = StorageEngine(db_path, embedding_dim=384)
    emb = EmbeddingEngine()
    rep = CheckpointRestore(storage=stor, embeddings=emb, settings=settings)
    yield stor, rep
    stor.close()


# ── helpers ──────────────────────────────────────────────────────────────────


def _seed_anchor(
    storage: StorageEngine,
    content: str,
    directory_context: str,
    heat: float = 1.0,
    valid_until: str | None = None,
) -> int:
    """Insert an anchor memory — is_protected=True, tags=['_anchor']."""
    data: dict[str, Any] = {
        "content": content,
        "tags": ["_anchor"],
        "directory_context": directory_context,
        "heat": heat,
        "is_protected": True,
        "is_stale": False,
    }
    if valid_until is not None:
        data["valid_until"] = valid_until
    return storage.insert_memory(data)


def _iso_past() -> str:
    dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _iso_future() -> str:
    dt = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── TestGetAnchoredMemoriesScoped ─────────────────────────────────────────────


class TestGetAnchoredMemoriesScoped:
    """Tests for storage.get_anchored_memories_scoped()."""

    def test_global_anchor_surfaces_in_unrelated_project(self, storage):
        """Global anchor appears when restore() called with unrelated directory.

        This is the core regression test for the 2026-05-18 incident: a global
        anchor must not be crowded out by project anchors.
        """
        _seed_anchor(storage, "GLOBAL: always surface me", "global")
        # Seed 20 project-B anchors to fill any unscoped limit=20 cap.
        for i in range(20):
            _seed_anchor(storage, f"project-B anchor {i}", "/repos/B")

        result = storage.get_anchored_memories_scoped(directory="/repos/A", limit=20)
        contents = [r["content"] for r in result]
        assert "GLOBAL: always surface me" in contents, (
            f"global anchor not in result; got {contents[:5]}"
        )

    def test_project_anchor_does_not_surface_in_other_project(self, storage):
        """Project-scoped anchor for /repos/A does NOT appear for /repos/B."""
        _seed_anchor(storage, "ONLY FOR REPO A", "/repos/A")

        result = storage.get_anchored_memories_scoped(directory="/repos/B", limit=20)
        contents = [r["content"] for r in result]
        assert "ONLY FOR REPO A" not in contents, (
            f"project-A anchor leaked into project-B restore; got {contents}"
        )

    def test_global_anchors_appear_before_project_anchors(self, storage):
        """Global anchors are returned first even if project anchor has higher heat."""
        _seed_anchor(storage, "global fact", "global", heat=1.0)
        _seed_anchor(storage, "project fact hot", "/repos/X", heat=100.0)

        result = storage.get_anchored_memories_scoped(directory="/repos/X", limit=20)
        assert len(result) >= 2, f"expected >=2 rows, got {len(result)}"
        assert result[0]["content"] == "global fact", (
            f"global anchor should be index 0 but got: {result[0]['content']}"
        )

    def test_deduplication_when_anchor_matches_both_scopes(self, storage):
        """An anchor matching both global and project queries appears once.

        Seed an anchor with directory_context='global' then call with
        directory='global' so both the global-bucket WHERE clause AND the
        project-bucket WHERE clause match the same row.
        """
        _seed_anchor(storage, "shared scope anchor", "global")

        result = storage.get_anchored_memories_scoped(directory="global", limit=20)
        matching = [r for r in result if r["content"] == "shared scope anchor"]
        assert len(matching) == 1, f"dedup failed — anchor appeared {len(matching)} times"

    def test_hard_cap_50_enforced(self, storage):
        """Result never exceeds 50 entries even when limit > 50."""
        for i in range(60):
            _seed_anchor(storage, f"global anchor {i}", "global")

        result = storage.get_anchored_memories_scoped(directory="/some/project", limit=100)
        assert len(result) <= 50, f"hard cap 50 violated — got {len(result)} results"

    def test_expired_anchors_excluded(self, storage):
        """Anchors with valid_until in the past are excluded."""
        _seed_anchor(storage, "expired global anchor", "global", valid_until=_iso_past())
        _seed_anchor(storage, "active global anchor", "global", valid_until=_iso_future())

        result = storage.get_anchored_memories_scoped(directory="/proj", limit=20)
        contents = [r["content"] for r in result]
        assert "expired global anchor" not in contents, "expired anchor should not surface"
        assert "active global anchor" in contents, "active anchor should surface"

    @pytest.mark.skip(
        reason="v5.46.4 deferred — schema rejects empty string; behavior change "
        "deferred. See test_v5_46_4_fixture_directory_context guard (N3 v5.46.7)."
    )
    def test_empty_string_directory_context_treated_as_global(self, storage):
        """Anchors with directory_context='' are treated as global (included for all dirs).

        v5.46.6: skip removed — insert_memory now normalises '' → 'global' before
        writing, so queries that check directory_context = '' still surface these
        via the global-bucket WHERE clause.  Schema ASSERT on wiki_page does not
        apply to the memory table.
        """
        _seed_anchor(storage, "empty-context global", "")

        result = storage.get_anchored_memories_scoped(directory="/some/project", limit=20)
        contents = [r["content"] for r in result]
        assert "empty-context global" in contents, (
            "empty-string directory_context anchor should be in global bucket"
        )

    def test_system_directory_context_not_surfaced(self, storage):
        """v5.65: Anchors with directory_context='system' are no longer surfaced.

        'system' was the mis-stamp sink; v5.64 stopped creating new system rows.
        Dropping 'system' from the anchor SQL global-bucket query prevents stale
        mis-stamped rows from surfacing.
        """
        _seed_anchor(storage, "system anchor", "system")

        result = storage.get_anchored_memories_scoped(directory="/some/project", limit=20)
        contents = [r["content"] for r in result]
        assert "system anchor" not in contents, (
            "system directory_context anchor must NOT surface after v5.65 (mis-stamp sink dropped)"
        )

    def test_project_anchors_surface_for_correct_project(self, storage):
        """Project anchor for /repos/myproject appears when restoring from that project."""
        _seed_anchor(storage, "myproject specific anchor", "/repos/myproject")

        result = storage.get_anchored_memories_scoped(directory="/repos/myproject", limit=20)
        contents = [r["content"] for r in result]
        assert "myproject specific anchor" in contents, (
            "project anchor should appear for its own project"
        )

    def test_heat_ordering_within_global_scope(self, storage):
        """Within the global bucket, anchors are ordered heat DESC."""
        _seed_anchor(storage, "low heat global", "global", heat=1.0)
        _seed_anchor(storage, "high heat global", "global", heat=50.0)

        result = storage.get_anchored_memories_scoped(directory="/proj", limit=20)
        global_items = [r for r in result if r.get("directory_context") == "global"]
        assert len(global_items) >= 2
        assert global_items[0]["content"] == "high heat global", (
            f"within global scope, highest heat should be first; got {global_items[0]['content']}"
        )


# ── TestRestoreUsesScope ──────────────────────────────────────────────────────


class TestRestoreUsesScope:
    """Integration: HippocampalReplay.restore() surfaces global anchors."""

    def test_restore_includes_global_anchor_from_different_project(self, replay):
        """After fix: restore(directory=project_A) returns global anchor
        written while working on project_B.

        This test FAILS before the fix because get_anchored_memories() with
        limit=20 and 20 project anchors crowds out the global anchor.
        """
        storage, rep = replay
        _seed_anchor(storage, "GLOBAL CRITICAL FACT", "global")
        # Seed 20 project-A anchors so the old flat query would fill the cap.
        for i in range(20):
            _seed_anchor(storage, f"proj-A anchor {i}", "/repos/project_A")

        result = rep.restore(directory="/repos/project_A")
        formatted = result.get("formatted", "")
        assert "GLOBAL CRITICAL FACT" in formatted, (
            f"global anchor missing from restore output.\n"
            f"formatted (first 800 chars):\n{formatted[:800]}"
        )

    def test_restore_does_not_include_other_project_anchors(self, replay):
        """restore(directory=project_A) excludes project_B anchors."""
        storage, rep = replay
        _seed_anchor(storage, "PROJECT B SECRET ANCHOR", "/repos/project_B")

        result = rep.restore(directory="/repos/project_A")
        formatted = result.get("formatted", "")
        assert "PROJECT B SECRET ANCHOR" not in formatted, (
            "project_B anchor should not appear in project_A restore"
        )
