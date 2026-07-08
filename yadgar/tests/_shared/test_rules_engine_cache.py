"""Tests for RulesEngine applicable-rules cache (v5.1 C5).

Verifies that get_applicable_rules() caches results per directory,
that the cache is invalidated on add_rule() and delete_rule(),
and that memorize() second call issues zero memory_rule-touching queries.
"""

from unittest.mock import MagicMock

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.rules_engine import RulesEngine

# ── fixtures ────────────────────────────────────────────────────────────


def _make_storage(*, global_rules=None, dir_rules=None, file_rules=None):
    """Return a mock StorageEngine that tracks rule-query call counts."""
    storage = MagicMock()
    storage.get_rules_for_scope.return_value = global_rules or []
    storage.get_all_active_rules_by_scope.return_value = dir_rules or []
    storage.insert_rule.return_value = 1
    storage.get_rule.return_value = {"id": 1, "is_active": True}
    return storage


@pytest.fixture
def settings(tmp_path):
    return Settings(DB_PATH=str(tmp_path / "cache_test.db"))


@pytest.fixture
def storage():
    return _make_storage()


@pytest.fixture
def engine(storage, settings):
    return RulesEngine(storage, settings)


# ── test 1: cache hit skips DB on second call ────────────────────────────


def test_get_applicable_rules_cache_hit_skips_db(engine, storage):
    """Second call for same directory must hit zero DB queries."""
    engine.get_applicable_rules("/foo")
    # reset counters after first (cold) call
    storage.get_rules_for_scope.reset_mock()
    storage.get_all_active_rules_by_scope.reset_mock()

    engine.get_applicable_rules("/foo")

    storage.get_rules_for_scope.assert_not_called()
    storage.get_all_active_rules_by_scope.assert_not_called()


# ── test 2: first call issues exactly 3 queries ──────────────────────────


def test_get_applicable_rules_first_call_issues_three_queries(engine, storage):
    """First call for a directory must issue exactly 3 rule-scope queries."""
    engine.get_applicable_rules("/bar")

    # 1 call to get_rules_for_scope("global")
    assert storage.get_rules_for_scope.call_count == 1
    storage.get_rules_for_scope.assert_called_once_with("global")

    # 2 calls to get_all_active_rules_by_scope: "directory" + "file"
    assert storage.get_all_active_rules_by_scope.call_count == 2
    calls = storage.get_all_active_rules_by_scope.call_args_list
    scopes = {c.args[0] for c in calls}
    assert scopes == {"directory", "file"}


# ── test 3: add_rule clears the cache ───────────────────────────────────


def test_add_rule_clears_cache(engine, storage):
    """add_rule() must clear the cache; subsequent call re-issues 3 queries."""
    engine.get_applicable_rules("/foo")

    engine.add_rule(
        rule_type="soft",
        scope="global",
        condition="importance > 0.5",
        action="boost:0.2",
    )

    # Cache must now be empty
    assert engine._applicable_rules_cache == {}

    # Reset counters so we can measure the next get_applicable_rules call
    storage.get_rules_for_scope.reset_mock()
    storage.get_all_active_rules_by_scope.reset_mock()

    engine.get_applicable_rules("/foo")

    # Must have re-fetched (3 queries again)
    assert storage.get_rules_for_scope.call_count == 1
    assert storage.get_all_active_rules_by_scope.call_count == 2


# ── test 4: cache is keyed per directory ────────────────────────────────


def test_cache_separates_directories(engine, storage):
    """Each directory is cached independently; no cross-contamination."""
    engine.get_applicable_rules("/foo")
    engine.get_applicable_rules("/bar")

    # Both are now cached; reset counters
    storage.get_rules_for_scope.reset_mock()
    storage.get_all_active_rules_by_scope.reset_mock()

    # Second calls for both directories must be cache hits (zero queries each)
    engine.get_applicable_rules("/foo")
    engine.get_applicable_rules("/bar")

    storage.get_rules_for_scope.assert_not_called()
    storage.get_all_active_rules_by_scope.assert_not_called()

    # Both directories independently present in cache
    assert "/foo" in engine._applicable_rules_cache
    assert "/bar" in engine._applicable_rules_cache


# ── test 5: delete_rule also clears the cache ───────────────────────────


def test_delete_rule_clears_cache(engine, storage):
    """delete_rule() must clear the cache so next call re-fetches."""
    engine.get_applicable_rules("/foo")
    assert "/foo" in engine._applicable_rules_cache

    engine.delete_rule(1)

    assert engine._applicable_rules_cache == {}


# ── test 6: memorize integration — second invocation hits cache ──────────


def test_memorize_check_write_policy_query_count(settings, tmp_path):
    """check_write_policy() second call within same RulesEngine issues zero rule queries.

    This is the C5 acceptance criterion: the async-enqueue path's latency
    budget is not blown by repeated DB round-trips per memorize() call.
    """
    from yadgar._shared.storage import StorageEngine

    db_path = str(tmp_path / "c5_accept.db")
    real_storage = StorageEngine(db_path)
    try:
        engine = RulesEngine(real_storage, settings)

        # Wrap the three rule-query methods to count calls
        orig_global = real_storage.get_rules_for_scope
        orig_by_scope = real_storage.get_all_active_rules_by_scope

        call_counts = {"global": 0, "by_scope": 0}

        def counting_global(scope, **kw):
            call_counts["global"] += 1
            return orig_global(scope, **kw)

        def counting_by_scope(scope):
            call_counts["by_scope"] += 1
            return orig_by_scope(scope)

        real_storage.get_rules_for_scope = counting_global
        real_storage.get_all_active_rules_by_scope = counting_by_scope

        # First call: cold — should hit DB
        engine.check_write_policy("content one", "/project", ["tag"])
        first_global = call_counts["global"]
        first_by_scope = call_counts["by_scope"]
        assert first_global == 1, f"Expected 1 global query on first call, got {first_global}"
        assert first_by_scope == 2, (
            f"Expected 2 by-scope queries on first call, got {first_by_scope}"
        )

        # Reset counters
        call_counts["global"] = 0
        call_counts["by_scope"] = 0

        # Second call: same context — must be fully cached
        engine.check_write_policy("content two", "/project", ["tag"])

        assert call_counts["global"] == 0, (
            f"Second call issued {call_counts['global']} global rule queries; expected 0 (cache hit)"
        )
        assert call_counts["by_scope"] == 0, (
            f"Second call issued {call_counts['by_scope']} by-scope rule queries; expected 0 (cache hit)"
        )
    finally:
        real_storage.close()
