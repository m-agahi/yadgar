"""ReDoS sandboxing regression tests for rules_engine.py (S3 — v5.2.0 security cluster).

Verifies that a caller-supplied catastrophic-backtracking regex pattern
does not hang the rules engine indefinitely (H-6).

Fix: rules_engine.py uses the third-party `regex` library (not stdlib `re`)
which supports a timeout= kwarg. A 1-second timeout is enforced per-call.
"""

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.rules_engine import RulesEngine

# The classic catastrophic-backtracking (ReDoS) pattern.
# stdlib `re.sub(EVIL_PATTERN, '', EVIL_INPUT)` hangs for > 10s.
# `regex.sub(EVIL_PATTERN, '', EVIL_INPUT, timeout=1.0)` raises regex.TimeoutError.
EVIL_PATTERN = r"(a+)+b"
EVIL_INPUT = "a" * 35 + "X"  # no 'b' → exponential backtracking

# Action string format: "redact:PATTERN:REPLACEMENT"
EVIL_ACTION = f"redact:{EVIL_PATTERN}:"
SAFE_ACTION = r"redact:\bSECRET\b:***"


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture
def settings(tmp_path):
    return Settings(DB_PATH=str(tmp_path / "test_redos.db"))


@pytest.fixture
def engine(storage, settings):
    return RulesEngine(storage, settings)


@pytest.mark.timeout(5, func_only=True)
def test_redos_pattern_times_out(engine):
    """A catastrophic-backtracking pattern must not hang the engine.

    The test asserts the call completes within pytest-timeout's 5s budget
    (i.e. the engine's internal 1s timeout fires, raising an exception that
    is caught and logged, not propagated to the caller).

    Option A chosen (third-party `regex` library): stdlib `re` has no timeout;
    `regex` supports `timeout=` kwarg, limiting CPU time per call. The timeout
    is caught inside check_write_policy and logged as a warning (same path as
    re.error today), so callers see an unmodified result rather than a hang.
    """
    engine.add_rule(
        rule_type="write_redact",
        scope="global",
        condition="content contains aaa",  # matches EVIL_INPUT
        action=EVIL_ACTION,
    )

    # The engine must not hang — must complete within 5s (pytest-timeout budget).
    # After the fix the internal timeout fires and check_write_policy returns
    # (False, "", None) — the content is unmodified but no hang occurs.
    blocked, reason, modified = engine.check_write_policy(EVIL_INPUT, "/test", ["redact-target"])
    # Did not hang — test passes regardless of whether redaction was applied.
    assert not blocked


@pytest.mark.timeout(5, func_only=True)
def test_safe_pattern_still_works(engine):
    """A non-evil pattern continues to work correctly after adding timeout support."""
    engine.add_rule(
        rule_type="write_redact",
        scope="global",
        condition="content contains SECRET",
        action=SAFE_ACTION,
    )

    blocked, _, modified = engine.check_write_policy("Do not share this SECRET value", "/test", [])

    assert not blocked
    assert modified is not None
    assert "SECRET" not in modified
    assert "***" in modified
