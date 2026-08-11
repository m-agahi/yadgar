"""Tests for write-path rules: write_block, write_redact, and policy enforcement."""

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.rules_engine import RulesEngine, _parse_write_action
from yadgar._shared.storage import StorageEngine


@pytest.fixture
def settings(tmp_path):
    return Settings(DB_PATH=str(tmp_path / "test.db"))


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture
def engine(storage, settings):
    return RulesEngine(storage, settings)


# ── _parse_write_action ───────────────────────────────────────────────


class TestParseWriteAction:
    def test_filter_action(self):
        action_type, pattern, replacement = _parse_write_action("filter")
        assert action_type == "filter"
        assert pattern == ""
        assert replacement == ""

    def test_redact_action(self):
        action_type, pattern, replacement = _parse_write_action(
            "redact:password=[A-Za-z0-9]+:password=***"
        )
        assert action_type == "redact"
        assert pattern == "password=[A-Za-z0-9]+"
        assert replacement == "password=***"

    def test_redact_with_colon_in_replacement(self):
        # The rest after "redact:" is split on the FIRST colon only,
        # so colons inside the replacement are preserved intact.
        action_type, pattern, replacement = _parse_write_action(
            "redact:TOKEN=[A-Z]+:TOKEN=::REDACTED::"
        )
        assert action_type == "redact"
        assert pattern == "TOKEN=[A-Z]+"
        assert replacement == "TOKEN=::REDACTED::"

    def test_invalid_action_raises(self):
        with pytest.raises(ValueError):
            _parse_write_action("boost:0.3")

    def test_invalid_redact_missing_replacement_raises(self):
        with pytest.raises(ValueError):
            _parse_write_action("redact:pattern_only")


# ── add_rule validation ────────────────────────────────────────────────


class TestAddWriteBlockRule:
    def test_creates_write_block_rule(self, engine):
        rule_id = engine.add_rule(
            rule_type="write_block",
            scope="global",
            condition="content contains classified",
            action="filter",
        )
        assert isinstance(rule_id, int)
        rules = engine.get_all_rules()
        assert len(rules) == 1
        assert rules[0]["rule_type"] == "write_block"

    def test_write_block_must_use_filter_action(self, engine):
        with pytest.raises(ValueError):
            engine.add_rule("write_block", "global", "content contains secret", "boost:0.3")

    def test_write_block_invalid_condition_raises(self, engine):
        with pytest.raises(ValueError):
            engine.add_rule("write_block", "global", "bad condition here", "filter")


class TestAddWriteRedactRule:
    def test_creates_write_redact_rule(self, engine):
        rule_id = engine.add_rule(
            rule_type="write_redact",
            scope="global",
            condition="content contains password=",
            action="redact:password=[A-Za-z0-9]+:password=***",
        )
        assert isinstance(rule_id, int)
        rules = engine.get_all_rules()
        assert len(rules) == 1
        assert rules[0]["rule_type"] == "write_redact"

    def test_write_redact_invalid_action_raises(self, engine):
        with pytest.raises(ValueError):
            engine.add_rule("write_redact", "global", "content contains x", "filter_wrong")


class TestAddRuleTypeValidation:
    def test_invalid_rule_type_raises(self, engine):
        with pytest.raises(ValueError, match="rule_type"):
            engine.add_rule("invalid_type", "global", "content contains x", "filter")

    def test_hard_rule_still_works(self, engine):
        rule_id = engine.add_rule("hard", "global", "importance > 0.5", "filter")
        assert isinstance(rule_id, int)

    def test_soft_rule_still_works(self, engine):
        rule_id = engine.add_rule("soft", "global", "tag contains architecture", "boost:0.2")
        assert isinstance(rule_id, int)


# ── check_write_policy ────────────────────────────────────────────────


class TestWriteBlockPolicy:
    def test_blocks_matching_content(self, engine):
        engine.add_rule("write_block", "global", "content contains classified", "filter")
        blocked, reason, modified = engine.check_write_policy(
            "this is classified information", "/project", []
        )
        assert blocked is True
        assert reason != ""
        assert modified is None

    def test_passes_non_matching_content(self, engine):
        engine.add_rule("write_block", "global", "content contains classified", "filter")
        blocked, reason, modified = engine.check_write_policy(
            "this is public information", "/project", []
        )
        assert blocked is False
        assert modified is None

    def test_path_scoped_block_matches(self, engine):
        """C10(a): `context` is a PATH, so scope="path" is its migration target.

        The retired scope="directory" prefix-matched `context` against a
        filesystem `scope_value`. Its replacement here is scope="path" with a
        glob — verified against the real caller, which forwards `ctx.context`
        (a working-directory path), never a project_id.
        """
        engine.add_rule(
            "write_block",
            "path",
            "content contains secret",
            "filter",
            scope_value="/work/classified*",
        )
        blocked, _, _ = engine.check_write_policy("this is secret", "/work/classified/docs", [])
        assert blocked is True

    def test_path_scoped_block_does_not_match_other_path(self, engine):
        engine.add_rule(
            "write_block",
            "path",
            "content contains secret",
            "filter",
            scope_value="/work/classified*",
        )
        blocked, _, _ = engine.check_write_policy("this is secret", "/other/project", [])
        assert blocked is False

    def test_project_scoped_block_never_fires_on_the_write_path(self, engine):
        """The write path has no project identity, so project rules cannot match.

        Stated as a contract rather than left as a surprise: `check_write_policy`
        passes an EMPTY project_id because none of its three callers holds one
        (ADR-0227 — no match beats a wrong match). Site (f) is what supplies it.
        """
        engine.add_rule(
            "write_block",
            "project",
            "content contains secret",
            "filter",
            scope_value="acme/classified",
        )
        blocked, _, _ = engine.check_write_policy("this is secret", "acme/classified", [])
        assert blocked is False

    def test_no_rules_returns_not_blocked(self, engine):
        blocked, reason, modified = engine.check_write_policy("anything", "/project", [])
        assert blocked is False
        assert reason == ""
        assert modified is None


class TestWriteRedactPolicy:
    def test_redacts_matching_content(self, engine):
        engine.add_rule(
            "write_redact",
            "global",
            "content contains password=",
            "redact:password=[A-Za-z0-9]+:password=***",
        )
        blocked, _, modified = engine.check_write_policy(
            "db config: password=hunter2 host=localhost", "/project", []
        )
        assert blocked is False
        assert modified is not None
        assert "hunter2" not in modified
        assert "password=***" in modified

    def test_non_matching_redact_returns_none_modified(self, engine):
        engine.add_rule(
            "write_redact",
            "global",
            "content contains password=",
            "redact:password=[A-Za-z0-9]+:password=***",
        )
        blocked, _, modified = engine.check_write_policy("nothing sensitive here", "/project", [])
        assert blocked is False
        assert modified is None

    def test_redact_applied_before_block(self, engine):
        # write_redact rule should redact; write_block rule evaluates on original
        # both conditions match, block fires first (higher priority)
        engine.add_rule("write_block", "global", "content contains DANGER", "filter", priority=10)
        engine.add_rule(
            "write_redact",
            "global",
            "content contains token=",
            "redact:token=[a-z]+:token=***",
            priority=0,
        )
        # Only the redact condition matches (no DANGER keyword)
        blocked, _, modified = engine.check_write_policy("auth token=abcdef used", "/project", [])
        assert blocked is False
        assert modified is not None
        assert "abcdef" not in modified


# ── export / import ───────────────────────────────────────────────────


class TestExportImport:
    def test_export_returns_serializable_dicts(self, engine):
        engine.add_rule("write_block", "global", "content contains classified", "filter")
        engine.add_rule("soft", "global", "tag contains architecture", "boost:0.2")
        rules = engine.export_rules()
        assert len(rules) == 2
        for r in rules:
            assert "rule_type" in r
            assert "scope" in r
            assert "condition" in r
            assert "action" in r
            assert "priority" in r
            # scope_value is always present (may be None)
            assert "scope_value" in r

    def test_export_empty_rules(self, engine):
        rules = engine.export_rules()
        assert rules == []

    def test_import_roundtrip(self, tmp_path, settings):
        # Export from one engine, import into another
        storage_a = StorageEngine(str(tmp_path / "a.db"))
        engine_a = RulesEngine(storage_a, settings)
        engine_a.add_rule(
            "write_block", "global", "content contains classified", "filter", priority=5
        )
        engine_a.add_rule("soft", "global", "tag contains architecture", "boost:0.3")
        exported = engine_a.export_rules()
        storage_a.close()

        storage_b = StorageEngine(str(tmp_path / "b.db"))
        try:
            engine_b = RulesEngine(storage_b, settings)
            count = engine_b.import_rules(exported)
            assert count == 2
            imported = engine_b.get_all_rules()
            assert len(imported) == 2
            rule_types = {r["rule_type"] for r in imported}
            assert "write_block" in rule_types
            assert "soft" in rule_types
        finally:
            storage_b.close()

    def test_import_skips_invalid_rules(self, engine):
        bad_rules = [
            {"rule_type": "invalid", "scope": "global", "condition": "x > 1", "action": "filter"},
            {
                "rule_type": "soft",
                "scope": "global",
                "condition": "tag contains foo",
                "action": "boost:0.1",
            },
        ]
        count = engine.import_rules(bad_rules)
        assert count == 1  # Only the valid rule was imported
        assert len(engine.get_all_rules()) == 1
