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
    """Second call for the same (project_id, path) must hit zero DB queries."""
    engine.get_applicable_rules("/foo")
    # reset counters after first (cold) call
    storage.get_rules_for_scope.reset_mock()
    storage.get_all_active_rules_by_scope.reset_mock()

    engine.get_applicable_rules("/foo")

    storage.get_rules_for_scope.assert_not_called()
    storage.get_all_active_rules_by_scope.assert_not_called()


# ── test 2: first call issues exactly 3 queries ──────────────────────────


def test_get_applicable_rules_first_call_issues_expected_queries(engine, storage):
    """First call must issue 1 global query + the project query + the legacy census.

    C10(a): the scope kinds are "project"/"path". ``path`` is None here, so the
    "path" query is correctly SKIPPED — a filesystem predicate with no file to
    test cannot match. The two extra reads are the one-shot legacy census
    (``_report_unmigrated_rules``), which surfaces rows still on the retired
    "directory"/"file" kinds instead of silently dropping them.
    """
    engine.get_applicable_rules("acme/bar")

    # 1 call to get_rules_for_scope("global")
    assert storage.get_rules_for_scope.call_count == 1
    storage.get_rules_for_scope.assert_called_once_with("global")

    # "project" + the census pair. No "path" — no path was supplied.
    calls = storage.get_all_active_rules_by_scope.call_args_list
    scopes = {c.args[0] for c in calls}
    assert scopes == {"project", "directory", "file"}
    assert "path" not in scopes


def test_path_rules_are_queried_only_when_a_path_is_supplied(engine, storage):
    """C10(a): scope="path" is consulted only with a real path to glob."""
    engine.get_applicable_rules("acme/bar", "/repo/src/app.ts")

    scopes = {c.args[0] for c in storage.get_all_active_rules_by_scope.call_args_list}
    assert "path" in scopes


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

    # Must have re-fetched. The census is one-shot per engine and already ran,
    # so only the "project" query follows the global one this time.
    assert storage.get_rules_for_scope.call_count == 1
    assert storage.get_all_active_rules_by_scope.call_count == 1


# ── test 4: cache is keyed per directory ────────────────────────────────


def test_cache_separates_projects(engine, storage):
    """Each project is cached independently; no cross-contamination."""
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

    # Both projects independently present in cache. C10(a): the key is the
    # (project_id, path) tuple — path participates in cache identity.
    assert ("/foo", None) in engine._applicable_rules_cache
    assert ("/bar", None) in engine._applicable_rules_cache


# ── test 5: delete_rule also clears the cache ───────────────────────────


def test_delete_rule_clears_cache(engine, storage):
    """delete_rule() must clear the cache so next call re-fetches."""
    engine.get_applicable_rules("/foo")
    assert ("/foo", None) in engine._applicable_rules_cache

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
        # C10(a): "project" + the one-shot legacy census ("directory", "file").
        # No "path" query — check_write_policy supplies no filesystem path.
        assert first_by_scope == 3, (
            f"Expected 3 by-scope queries on first call, got {first_by_scope}"
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
