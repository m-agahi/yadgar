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
from yadgar._shared.restoration.checkpoint_restore import CheckpointRestore
from yadgar._shared.storage import StorageEngine

# C10g (0047 PR#40 §5): the scope tokens in this file are PROJECT IDS, not
# paths. ``get_anchored_memories_scoped``'s project bucket is keyed on the
# project_id now (its writer, ``anchor_memory``, moved onto the same key in the
# same car), so path-shaped literals here would have been a fixture that no
# production writer can produce. ``"global"`` / ``"system"`` / ``""`` are left
# verbatim — those are the legacy sentinel values whose absence is the assertion.
#
# C13 (0047 PR#40 §5): seeds must NAME the project they write into —
# C5 deleted every fallback that used to answer an unnamed write (ADR-0227).
# A per-file constant, deliberately NOT a shared fixture default: a new test
# that builds its own write payload still reds — the signal of the flip.
_PROJECT = "m-agahi/yadgar"

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
    *,
    global_reach: bool = False,
) -> int:
    """Insert an anchor memory — is_protected=True, tags=['_anchor'].

    C13 (0047 PR#40 §5): ``global_reach`` adds the ``global`` TAG, which is
    what ``get_anchored_memories_scoped`` keys its global bucket on since C5.
    It is a separate argument from ``directory_context`` on purpose — §1.4
    splits ownership from reach, and the two are no longer the same fact.
    """
    tags = ["_anchor", "global"] if global_reach else ["_anchor"]
    data: dict[str, Any] = {
        "content": content,
        "tags": tags,
        "directory_context": directory_context,
        "heat": heat,
        "is_protected": True,
        "is_stale": False,
        "project_id": _PROJECT,
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
        _seed_anchor(storage, "GLOBAL: always surface me", "global", global_reach=True)
        # Seed 20 project-B anchors to fill any unscoped limit=20 cap.
        for i in range(20):
            _seed_anchor(storage, f"project-B anchor {i}", "acme/repo-b")

        result = storage.get_anchored_memories_scoped(project_id="acme/repo-a", limit=20)
        contents = [r["content"] for r in result]
        assert "GLOBAL: always surface me" in contents, (
            f"global anchor not in result; got {contents[:5]}"
        )

    def test_project_anchor_does_not_surface_in_other_project(self, storage):
        """Project-scoped anchor for acme/repo-a does NOT appear for acme/repo-b."""
        _seed_anchor(storage, "ONLY FOR REPO A", "acme/repo-a")

        result = storage.get_anchored_memories_scoped(project_id="acme/repo-b", limit=20)
        contents = [r["content"] for r in result]
        assert "ONLY FOR REPO A" not in contents, (
            f"project-A anchor leaked into project-B restore; got {contents}"
        )

    def test_global_anchors_appear_before_project_anchors(self, storage):
        """Global anchors are returned first even if project anchor has higher heat."""
        _seed_anchor(storage, "global fact", "global", heat=1.0, global_reach=True)
        _seed_anchor(storage, "project fact hot", "acme/repo-x", heat=100.0)

        result = storage.get_anchored_memories_scoped(project_id="acme/repo-x", limit=20)
        assert len(result) >= 2, f"expected >=2 rows, got {len(result)}"
        assert result[0]["content"] == "global fact", (
            f"global anchor should be index 0 but got: {result[0]['content']}"
        )

    def test_deduplication_when_anchor_matches_both_scopes(self, storage):
        """An anchor matching both global and project queries appears once.

        C13: the global bucket is keyed on the ``global`` TAG since C5, so
        matching BOTH clauses now needs the tag AND directory_context='global'
        with directory='global' passed in. Seeding only the directory made this
        match one bucket, which is not the case it claims to cover.
        """
        _seed_anchor(storage, "shared scope anchor", "global", global_reach=True)

        result = storage.get_anchored_memories_scoped(project_id="global", limit=20)
        matching = [r for r in result if r["content"] == "shared scope anchor"]
        assert len(matching) == 1, f"dedup failed — anchor appeared {len(matching)} times"

    def test_hard_cap_50_enforced(self, storage):
        """Result never exceeds 50 entries even when limit > 50."""
        for i in range(60):
            _seed_anchor(storage, f"global anchor {i}", "global", global_reach=True)

        result = storage.get_anchored_memories_scoped(project_id="acme/some-project", limit=100)
        assert len(result) <= 50, f"hard cap 50 violated — got {len(result)} results"

    def test_expired_anchors_excluded(self, storage):
        """Anchors with valid_until in the past are excluded."""
        _seed_anchor(
            storage,
            "expired global anchor",
            "global",
            valid_until=_iso_past(),
            global_reach=True,
        )
        _seed_anchor(
            storage,
            "active global anchor",
            "global",
            valid_until=_iso_future(),
            global_reach=True,
        )

        result = storage.get_anchored_memories_scoped(project_id="acme/proj", limit=20)
        contents = [r["content"] for r in result]
        assert "expired global anchor" not in contents, "expired anchor should not surface"
        assert "active global anchor" in contents, "active anchor should surface"

    def test_empty_string_directory_context_is_refused_at_write(self, storage):
        """INVERTED by C13: an empty directory_context is not global — it is not writable.

        The v5.46.6 premise this test was written on — "insert_memory normalises
        '' → 'global' before writing, so queries that check directory_context =
        '' still surface these via the global-bucket WHERE clause" — is one of
        the two ``or "global"`` expressions C5 deleted from the memory insert
        dict (ADR-0227: nothing is invented on a write's behalf). That
        normalisation was also the only reason the row could be stored at all:
        the memory table's schema ASSERT requires a non-empty
        ``directory_context``, so with the sentinel-minting expression gone the
        write is refused outright rather than quietly re-homed under 'global'.

        The old docstring's "Schema ASSERT on wiki_page does not apply to the
        memory table" is stale — it does now, which is why this asserts the
        raise rather than an absence from the result set.

        Reach is declared, not inferred: a genuinely global anchor carries the
        ``global`` tag — see ``test_global_anchor_surfaces_in_unrelated_project``.
        """
        with pytest.raises(RuntimeError, match="directory_context"):
            _seed_anchor(storage, "empty-context anchor", "")

    def test_system_directory_context_not_surfaced(self, storage):
        """v5.65: Anchors with directory_context='system' are no longer surfaced.

        'system' was the mis-stamp sink; v5.64 stopped creating new system rows.
        Dropping 'system' from the anchor SQL global-bucket query prevents stale
        mis-stamped rows from surfacing.
        """
        _seed_anchor(storage, "system anchor", "system")

        result = storage.get_anchored_memories_scoped(project_id="acme/some-project", limit=20)
        contents = [r["content"] for r in result]
        assert "system anchor" not in contents, (
            "system directory_context anchor must NOT surface after v5.65 (mis-stamp sink dropped)"
        )

    def test_project_anchors_surface_for_correct_project(self, storage):
        """Project anchor for acme/myproject appears when restoring from that project."""
        _seed_anchor(storage, "myproject specific anchor", "acme/myproject")

        result = storage.get_anchored_memories_scoped(project_id="acme/myproject", limit=20)
        contents = [r["content"] for r in result]
        assert "myproject specific anchor" in contents, (
            "project anchor should appear for its own project"
        )

    def test_heat_ordering_within_global_scope(self, storage):
        """Within the global bucket, anchors are ordered heat DESC."""
        _seed_anchor(storage, "low heat global", "global", heat=1.0, global_reach=True)
        _seed_anchor(storage, "high heat global", "global", heat=50.0, global_reach=True)

        result = storage.get_anchored_memories_scoped(project_id="acme/proj", limit=20)
        # C13: membership of the global bucket is the ``global`` TAG since C5,
        # not directory_context — filtering on the directory here would select
        # rows by a key the write path can no longer produce.
        global_items = [r for r in result if "global" in r.get("tags", [])]
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
        _seed_anchor(storage, "GLOBAL CRITICAL FACT", "global", global_reach=True)
        # Seed 20 project-A anchors so the old flat query would fill the cap.
        for i in range(20):
            _seed_anchor(storage, f"proj-A anchor {i}", "acme/project-a")

        # C10g: the anchor bucket is keyed on project_id. Passing only a
        # directory would leave the project half empty and make the crowding-out
        # premise of this test unreachable.
        result = rep.restore(directory="/repos/project_A", project_id="acme/project-a")
        formatted = result.get("formatted", "")
        assert "GLOBAL CRITICAL FACT" in formatted, (
            f"global anchor missing from restore output.\n"
            f"formatted (first 800 chars):\n{formatted[:800]}"
        )

    def test_restore_does_not_include_other_project_anchors(self, replay):
        """restore(directory=project_A) excludes project_B anchors."""
        storage, rep = replay
        _seed_anchor(storage, "PROJECT B SECRET ANCHOR", "acme/project-b")

        result = rep.restore(directory="/repos/project_A", project_id="acme/project-a")
        formatted = result.get("formatted", "")
        assert "PROJECT B SECRET ANCHOR" not in formatted, (
            "project_B anchor should not appear in project_A restore"
        )
