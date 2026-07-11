"""v5.29.0 — Bi-temporal extension tests (Adopt-3).

Covers Tier 1 tables: user_profile and derived_belief.

T1 — Migration 010/011 add valid_from/valid_until columns.
T2 — Insert defaults valid_from = now(), valid_until = NULL.
T3 — Supersession: close-and-insert on value change; threshold suppression.
T4 — As-of-date queries.
T5 — App-side unique constraint (partial index fallback — SurrealDB v3 does not
     support DEFINE INDEX ... WHERE, so uniqueness is enforced in insert_profile).
T6 — Back-compat: existing callers unaffected.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar._shared.storage.narrative import BeliefRecord

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "bitext_test.db"), embedding_dim=384)
    yield engine
    engine.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _insert_bare_memory(storage, content: str = "test") -> int:
    now = storage._now_iso()
    mid = storage._next_id("memory")
    storage._q(
        "CREATE type::record('memory', $id) SET "
        "content = $content, tags = $tags, directory_context = $dir, "
        "created_at = $ts, last_accessed = $ts, heat = $heat, "
        "is_stale = false, plasticity = 1.0, stability = 0.0, "
        "excitability = 1.0, store_type = $st, compression_level = 0, "
        "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
        "provenance_agent = $agent, vector_clock = $vc, is_protected = false",
        {
            "id": mid,
            "content": content,
            "tags": [],
            "dir": "/tmp",
            "ts": now,
            "heat": 1.0,
            "st": "episodic",
            "agent": "default",
            "vc": "{}",
        },
    )
    return mid


# ── T1 — Migrations add columns ───────────────────────────────────────────────


class TestMigration010AddColumns:
    """T1 — Migration 010 adds valid_from/valid_until to user_profile."""

    def test_migration_010_adds_columns_to_user_profile(self, storage):
        """Inserting a user_profile row must expose valid_from/valid_until fields."""
        storage.insert_profile(
            entity_name="Alice_t1",
            attribute_type="role",
            attribute_key="current_role",
            attribute_value="engineer",
        )
        rows = storage._q(
            "SELECT valid_from, valid_until FROM user_profile WHERE entity_name = 'Alice_t1'"
        )
        assert rows, "Expected a user_profile row"
        row = rows[0]
        assert "valid_from" in row, "valid_from column missing from user_profile"
        assert row.get("valid_until") is None, "valid_until should be NULL on new row"


class TestMigration011AddColumns:
    """T1 — Migration 011 adds valid_from/valid_until to derived_belief."""

    def test_migration_011_adds_columns_to_derived_belief(self, storage):
        """Inserting a derived_belief row must expose valid_from/valid_until fields."""
        storage.insert_belief(
            BeliefRecord(
                belief_type="preference",
                subject="Alice_t1",
                content="Prefers dark mode",
                confidence=0.8,
            )
        )
        rows = storage._q(
            "SELECT valid_from, valid_until FROM derived_belief WHERE subject = 'Alice_t1'"
        )
        assert rows, "Expected a derived_belief row"
        row = rows[0]
        assert "valid_from" in row, "valid_from column missing from derived_belief"
        assert row.get("valid_until") is None, "valid_until should be NULL on new row"


# ── T2 — Insert defaults ─────────────────────────────────────────────────────


class TestInsertDefaults:
    """T2 — Inserts populate valid_from = now(), valid_until = NULL."""

    def test_insert_profile_defaults_valid_from_now(self, storage):
        storage.insert_profile(
            entity_name="Bob",
            attribute_type="skill",
            attribute_key="python",
            attribute_value="expert",
        )
        rows = storage._q("SELECT valid_from, valid_until FROM user_profile")
        assert rows, "Expected at least one user_profile row"
        assert rows[0].get("valid_from") is not None, "valid_from should be set"
        assert rows[0].get("valid_until") is None, "valid_until should be NULL"

    def test_insert_derived_belief_defaults_valid_from_now(self, storage):
        storage.insert_belief(
            BeliefRecord(
                belief_type="interest",
                subject="Bob",
                content="Interested in distributed systems",
                confidence=0.7,
            )
        )
        rows = storage._q("SELECT valid_from, valid_until FROM derived_belief")
        assert rows, "Expected at least one derived_belief row"
        assert rows[0].get("valid_from") is not None, "valid_from should be set"
        assert rows[0].get("valid_until") is None, "valid_until should be NULL"


# ── T3 — Supersession ────────────────────────────────────────────────────────


class TestUserProfileSupersession:
    """T3 — user_profile close-and-insert on value change."""

    def test_user_profile_change_value_supersedes_prior_row(self, storage):
        """Insert v1, insert v2 with same key/different value → v1.valid_until set, v2 current."""
        storage.insert_profile(
            entity_name="Carol",
            attribute_type="role",
            attribute_key="job_title",
            attribute_value="junior_engineer",
            confidence=0.9,
        )
        rows_before = storage._q(
            "SELECT valid_from, valid_until FROM user_profile WHERE entity_name = 'Carol'"
        )
        assert len(rows_before) == 1, "Expected 1 row before supersession"
        assert rows_before[0].get("valid_until") is None

        # Insert v2 with different value — should supersede v1
        storage.insert_profile(
            entity_name="Carol",
            attribute_type="role",
            attribute_key="job_title",
            attribute_value="senior_engineer",
            confidence=0.95,
        )
        rows_after = storage._q(
            "SELECT valid_from, valid_until, attribute_value FROM user_profile "
            "WHERE entity_name = 'Carol'"
        )
        assert len(rows_after) == 2, f"Expected 2 rows after supersession, got {len(rows_after)}"
        # One row should be closed, one should be current
        closed = [r for r in rows_after if r.get("valid_until") is not None]
        current = [r for r in rows_after if r.get("valid_until") is None]
        assert len(closed) == 1, "Expected exactly 1 closed (superseded) profile"
        assert len(current) == 1, "Expected exactly 1 currently-valid profile"
        assert closed[0].get("attribute_value") == "junior_engineer", "v1 value mismatch"
        assert current[0].get("attribute_value") == "senior_engineer", "v2 value mismatch"

    def test_user_profile_minor_confidence_drift_does_not_create_new_row(self, storage):
        """Small confidence change below PROFILE_BITEMPORAL_VERSION_DELTA → in-place update."""
        import os

        os.environ.setdefault("PROFILE_BITEMPORAL_VERSION_DELTA", "0.05")

        storage.insert_profile(
            entity_name="Dave",
            attribute_type="skill",
            attribute_key="python",
            attribute_value="proficient",
            confidence=0.5,
        )
        # Same value, confidence change of 0.02 — below threshold
        storage.insert_profile(
            entity_name="Dave",
            attribute_type="skill",
            attribute_key="python",
            attribute_value="proficient",
            confidence=0.52,
        )
        rows = storage._q("SELECT valid_until FROM user_profile WHERE entity_name = 'Dave'")
        # Should still be 1 row (in-place update, no supersession)
        assert len(rows) == 1, f"Expected 1 row for minor drift, got {len(rows)}"
        assert rows[0].get("valid_until") is None, "Row should still be current after minor drift"

    def test_row_growth_bounded_for_same_key(self, storage):
        """Bulk inserts with confidence drift below threshold keep row count bounded."""
        import os

        os.environ.setdefault("PROFILE_BITEMPORAL_VERSION_DELTA", "0.05")

        # Start with base value
        storage.insert_profile(
            entity_name="Eve",
            attribute_type="metric",
            attribute_key="score",
            attribute_value="base",
            confidence=0.5,
        )
        # 10 in-place updates (below threshold, same value)
        for _ in range(10):
            storage.insert_profile(
                entity_name="Eve",
                attribute_type="metric",
                attribute_key="score",
                attribute_value="base",
                confidence=0.52,  # below 0.05 threshold from 0.5 base
            )

        rows = storage._q("SELECT id FROM user_profile WHERE entity_name = 'Eve'")
        assert len(rows) < 5, (
            f"Row count should stay bounded with minor confidence drift, got {len(rows)}"
        )


class TestDerivedBeliefSupersession:
    """T3 — derived_belief supersede-on-insert for same (subject, belief_type, dc)."""

    def test_derived_belief_new_supersedes_prior(self, storage):
        """Insert belief, insert competing belief for same subject → prior is closed."""
        storage.insert_belief(
            BeliefRecord(
                belief_type="preference",
                subject="Frank",
                content="Prefers light mode",
                confidence=0.7,
                directory_context="/work",
            )
        )

        rows_before = storage._q("SELECT valid_until FROM derived_belief WHERE subject = 'Frank'")
        assert rows_before[0].get("valid_until") is None

        # Insert superseding belief
        storage.insert_belief(
            BeliefRecord(
                belief_type="preference",
                subject="Frank",
                content="Prefers dark mode",
                confidence=0.9,
                directory_context="/work",
            )
        )

        rows_after = storage._q(
            "SELECT valid_until, content FROM derived_belief WHERE subject = 'Frank'"
        )
        assert len(rows_after) == 2, f"Expected 2 rows, got {len(rows_after)}"
        # One row should be closed, one should be current
        closed = [r for r in rows_after if r.get("valid_until") is not None]
        current = [r for r in rows_after if r.get("valid_until") is None]
        assert len(closed) == 1, "Expected exactly 1 closed (superseded) belief"
        assert len(current) == 1, "Expected exactly 1 currently-valid belief"
        assert closed[0].get("content") == "Prefers light mode", "Prior belief content mismatch"
        assert current[0].get("content") == "Prefers dark mode", "New belief content mismatch"

    def test_derived_belief_supersede_false_keeps_old(self, storage):
        """supersede=False opts out of supersession — both rows remain current."""
        storage.insert_belief(
            BeliefRecord(
                belief_type="hypothesis",
                subject="Grace",
                content="Hypothesis A",
                confidence=0.6,
                directory_context="/research",
            )
        )
        storage.insert_belief(
            BeliefRecord(
                belief_type="hypothesis",
                subject="Grace",
                content="Hypothesis B",
                confidence=0.6,
                directory_context="/research",
            ),
            supersede=False,
        )
        rows = storage._q("SELECT valid_until FROM derived_belief WHERE subject = 'Grace'")
        assert len(rows) == 2, f"Expected 2 rows with supersede=False, got {len(rows)}"
        # Both should have valid_until = NULL (currently valid)
        for row in rows:
            assert row.get("valid_until") is None, (
                "Both beliefs should be current with supersede=False"
            )


# ── T4 — As-of-date queries ───────────────────────────────────────────────────


class TestAsOfFilter:
    """T4 — as_of_filter helper returns correct rows for current and historical state."""

    def test_as_of_filter_current_state_excludes_invalidated(self, storage):
        """as_of_filter(table, as_of=None) excludes invalidated rows."""
        from yadgar._shared.storage.bitemporal import as_of_filter, invalidate_edge

        pid = storage.insert_profile(
            entity_name="Hank",
            attribute_type="role",
            attribute_key="title",
            attribute_value="manager",
            confidence=0.9,
        )
        # Invalidate the row
        invalidate_edge(storage, "user_profile", pid)

        clause = as_of_filter("user_profile")
        rows = storage._q(f"SELECT id FROM user_profile WHERE 1=1{clause}")
        assert not any(
            str(r.get("id", "")).endswith(str(pid)) or r.get("id") == pid for r in rows
        ), "Invalidated row should be excluded by as_of_filter(as_of=None)"

    def test_as_of_filter_past_date_returns_historical_value(self, storage):
        """as_of_filter with as_of between two versions returns the older value."""
        from datetime import UTC, datetime, timedelta

        from yadgar._shared.storage.bitemporal import as_of_filter

        # Insert v1 with explicit valid_from in the past
        past_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        mid_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        # Insert v1 directly (bypass insert_profile to control timestamps)
        pid1 = storage._next_id("user_profile")
        storage._q(
            "CREATE type::record('user_profile', $id) SET "
            "entity_name = $en, attribute_type = $at, attribute_key = $ak, "
            "attribute_value = $av, confidence = $conf, "
            "created_at = $ts, updated_at = $ts, directory_context = $dc, "
            "valid_from = $vf, valid_until = $vu",
            {
                "id": pid1,
                "en": "Ivy",
                "at": "role",
                "ak": "title",
                "av": "engineer",
                "conf": 0.9,
                "ts": past_ts,
                "dc": "/work",
                "vf": past_ts,
                "vu": mid_ts,  # closed at mid_ts
            },
        )
        # Insert v2 valid from mid_ts onward
        pid2 = storage._next_id("user_profile")
        storage._q(
            "CREATE type::record('user_profile', $id) SET "
            "entity_name = $en, attribute_type = $at, attribute_key = $ak, "
            "attribute_value = $av, confidence = $conf, "
            "created_at = $ts, updated_at = $ts, directory_context = $dc, "
            "valid_from = $vf, valid_until = NONE",
            {
                "id": pid2,
                "en": "Ivy",
                "at": "role",
                "ak": "title",
                "av": "senior_engineer",
                "conf": 0.95,
                "ts": mid_ts,
                "dc": "/work",
                "vf": mid_ts,
            },
        )

        # Query as of between past_ts and mid_ts — should get v1 (engineer)
        query_ts = (datetime.fromisoformat(past_ts) + timedelta(minutes=30)).isoformat()
        clause = as_of_filter("user_profile", as_of=query_ts)
        rows = storage._q(
            f"SELECT attribute_value FROM user_profile WHERE entity_name = 'Ivy'{clause}"
        )
        assert len(rows) == 1, f"Expected 1 historical row, got {len(rows)}"
        assert rows[0].get("attribute_value") == "engineer", (
            f"Expected 'engineer' at {query_ts}, got {rows[0].get('attribute_value')}"
        )

        # Query current (as_of=None) — should get v2 (senior_engineer)
        clause_current = as_of_filter("user_profile")
        rows_current = storage._q(
            f"SELECT attribute_value FROM user_profile WHERE entity_name = 'Ivy'{clause_current}"
        )
        assert len(rows_current) == 1
        assert rows_current[0].get("attribute_value") == "senior_engineer"


# ── T5 — App-side unique constraint ──────────────────────────────────────────


class TestUserProfileUniqueConstraint:
    """T5 — Uniqueness on currently-valid rows is enforced application-side.

    SurrealDB v3.0.5 does not support DEFINE INDEX ... WHERE (partial index).
    insert_profile must enforce: only one currently-valid row per
    (entity_name, attribute_type, attribute_key, directory_context).
    """

    def test_user_profile_unique_constraint_scoped_to_current(self, storage):
        """Close v1 (valid_until set), then insert v2 with same key — must succeed."""
        from yadgar._shared.storage.bitemporal import invalidate_edge

        pid1 = storage.insert_profile(
            entity_name="Jack",
            attribute_type="role",
            attribute_key="title",
            attribute_value="analyst",
            confidence=0.8,
        )
        # Close v1
        invalidate_edge(storage, "user_profile", pid1)

        # Insert v2 with same key — must not fail
        pid2 = storage.insert_profile(
            entity_name="Jack",
            attribute_type="role",
            attribute_key="title",
            attribute_value="senior_analyst",
            confidence=0.9,
        )
        assert pid2 != pid1, "v2 should be a new row"

        # Only v2 should be currently valid
        rows = storage._q(
            "SELECT valid_until, attribute_value FROM user_profile "
            "WHERE entity_name = 'Jack' AND valid_until IS NONE"
        )
        assert len(rows) == 1, f"Expected 1 currently-valid row, got {len(rows)}"
        assert rows[0].get("attribute_value") == "senior_analyst"

    def test_no_duplicate_current_rows_on_same_key(self, storage):
        """Two consecutive insert_profile calls for same key produce only 1 current row."""
        storage.insert_profile(
            entity_name="Kim",
            attribute_type="pref",
            attribute_key="theme",
            attribute_value="light",
            confidence=0.9,
        )
        # Second call with different value — triggers supersession
        storage.insert_profile(
            entity_name="Kim",
            attribute_type="pref",
            attribute_key="theme",
            attribute_value="dark",
            confidence=0.9,
        )
        current_rows = storage._q(
            "SELECT attribute_value FROM user_profile "
            "WHERE entity_name = 'Kim' AND valid_until IS NONE"
        )
        assert len(current_rows) == 1, (
            f"Expected exactly 1 currently-valid row, got {len(current_rows)}"
        )
        assert current_rows[0].get("attribute_value") == "dark"


# ── T6 — Back-compat ─────────────────────────────────────────────────────────


class TestBackwardCompat:
    """T6 — Existing callers continue to work without changes."""

    def test_existing_insert_profile_callers_pass_unchanged(self, storage):
        """Call insert_profile exactly as v5.12.x callers do — no exceptions."""
        mem_id = _insert_bare_memory(storage, "Alice uses Python daily")

        # Old signature: (entity_name, attr_type, attr_key, attr_val, memory_id, confidence, dc)
        pid = storage.insert_profile(
            entity_name="Alice",
            attribute_type="skill",
            attribute_key="language",
            attribute_value="python",
            memory_id=mem_id,
            confidence=0.8,
            directory_context="/work",
        )
        assert isinstance(pid, int) and pid > 0

        # search_profiles_fts still works
        results = storage.search_profiles_fts("python")
        assert any(r.get("attribute_value") == "python" for r in results)

        # get_profiles_for_entity still works
        profiles = storage.get_profiles_for_entity("Alice", directory_context="/work")
        assert any(r.get("attribute_key") == "language" for r in profiles)

    def test_insert_belief_via_record_works(self, storage):
        """Call insert_belief with BeliefRecord — returns int id, search/query still work."""
        bid = storage.insert_belief(
            BeliefRecord(
                belief_type="preference",
                subject="Alice",
                content="Alice prefers concise code",
                confidence=0.7,
            )
        )
        assert isinstance(bid, int) and bid > 0

        # search_beliefs_fts still works
        results = storage.search_beliefs_fts("concise")
        assert any(r.get("content") and "concise" in r.get("content", "") for r in results)

        # get_beliefs_for_subject still works
        beliefs = storage.get_beliefs_for_subject("Alice")
        assert any(b.get("belief_type") == "preference" for b in beliefs)

    def test_get_full_graph_default_unchanged(self, storage):
        """get_full_graph() with no args returns the same shape as before."""
        from yadgar.backend.graph.graph_api import GraphAPI

        api = GraphAPI(storage)
        graph = api.get_full_graph()
        assert "nodes" in graph
        assert "edges" in graph
        # No exceptions; type check
        assert isinstance(graph["nodes"], list)
        assert isinstance(graph["edges"], list)

    def test_invalidate_user_profile_works(self, storage):
        """invalidate_edge now accepts 'user_profile' without ValueError."""
        from yadgar._shared.storage.bitemporal import invalidate_edge

        pid = storage.insert_profile(
            entity_name="Lucy",
            attribute_type="role",
            attribute_key="title",
            attribute_value="intern",
        )
        # Must not raise ValueError
        invalidate_edge(storage, "user_profile", pid, reason="role change")

        rows = storage._q("SELECT valid_until FROM user_profile WHERE entity_name = 'Lucy'")
        assert rows[0].get("valid_until") is not None

    def test_invalidate_derived_belief_works(self, storage):
        """invalidate_edge now accepts 'derived_belief' without ValueError."""
        from yadgar._shared.storage.bitemporal import invalidate_edge

        bid = storage.insert_belief(
            BeliefRecord(
                belief_type="fact",
                subject="Lucy",
                content="Lucy works at ACME",
            )
        )
        invalidate_edge(storage, "derived_belief", bid, reason="changed employer")

        rows = storage._q("SELECT valid_until FROM derived_belief WHERE subject = 'Lucy'")
        assert rows[0].get("valid_until") is not None

    def test_get_all_causal_edges_as_of_default_unchanged(self, storage):
        """get_all_causal_edges() with no as_of param returns same default behaviour."""
        # Just verifies the signature still works
        edges = storage.get_all_causal_edges()
        assert isinstance(edges, list)

    def test_get_full_graph_as_of_default_unchanged(self, storage):
        """get_full_graph(as_of=None) behaves identically to old no-arg call."""
        from yadgar.backend.graph.graph_api import GraphAPI

        api = GraphAPI(storage)
        graph_old = api.get_full_graph()
        graph_new = api.get_full_graph(as_of=None)
        assert graph_old.keys() == graph_new.keys()

    def test_search_profiles_fts_include_invalidated_default_excludes(self, storage):
        """search_profiles_fts() default excludes invalidated rows."""
        from yadgar._shared.storage.bitemporal import invalidate_edge

        pid = storage.insert_profile(
            entity_name="Max",
            attribute_type="role",
            attribute_key="title",
            attribute_value="outdated_role",
        )
        invalidate_edge(storage, "user_profile", pid)

        results = storage.search_profiles_fts("outdated_role")
        assert not any(r.get("attribute_value") == "outdated_role" for r in results), (
            "Invalidated profile should not appear in FTS by default"
        )

    def test_get_beliefs_for_subject_default_excludes_invalidated(self, storage):
        """get_beliefs_for_subject() default excludes invalidated rows."""
        from yadgar._shared.storage.bitemporal import invalidate_edge

        bid = storage.insert_belief(
            BeliefRecord(
                belief_type="stale",
                subject="Max",
                content="Stale belief content",
            )
        )
        invalidate_edge(storage, "derived_belief", bid)

        beliefs = storage.get_beliefs_for_subject("Max")
        assert not any(b.get("content") == "Stale belief content" for b in beliefs), (
            "Invalidated belief should not appear in get_beliefs_for_subject by default"
        )
