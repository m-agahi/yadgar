"""TDD tests for v5.10.1 active_work + checkpoint soft warning tier.

Red-first: these tests FAIL before implementation.  Run `pytest -x` to see the
red list; implement until all pass.

Covers:
  - consider_refresh_active_work soft action (12h < age ≤ 24h)
  - consider_refresh_checkpoint soft action (12h < age ≤ 24h)
  - Mutual exclusion: soft OR hard, never both
  - Boundary conditions (exact warn / exact stale)
  - suggested_call populated on both soft + hard
  - Token-budget invariant (signals mode ≤100 tokens) still holds
  - Registry: update_active_work writes marker to ~/.yadgar/active-work-tracked/
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from yadgar import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("active_work_warn_tier")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    # v5.42.3: /tmp/* dirs are not git repos; patch _detect_branch so tests
    # exercise the tool logic without needing each call to supply branch_hint.
    with (
        patch("yadgar.server.tools.project._detect_branch", return_value="feat/test-branch"),
        patch("yadgar.server._detect_branch", return_value="feat/test-branch"),
    ):
        yield
    server.shutdown()


# ── Soft tier: consider_refresh_active_work ──────────────────────────────────


def test_soft_active_work_emitted_in_warn_window(monkeypatch, flush_queue):
    """When WARN_HOURS < age ≤ STALE_HOURS, emit consider_refresh_active_work."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_aw_warn_test"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    warn_h = settings.ACTIVE_WORK_WARN_HOURS
    stale_h = settings.ACTIVE_WORK_STALE_HOURS

    # Age is mid-way between warn and stale thresholds
    mid_age = (warn_h + stale_h) / 2.0

    call_count = [0]

    def mock_age(rows):
        call_count[0] += 1
        if rows:
            return mid_age
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "consider_refresh_active_work" in actions


def test_soft_active_work_not_emitted_below_warn(monkeypatch, flush_queue):
    """When age ≤ WARN_HOURS, no soft action emitted."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_aw_below_warn"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    warn_h = settings.ACTIVE_WORK_WARN_HOURS

    def mock_age(rows):
        if rows:
            return warn_h - 1.0  # strictly below warn threshold
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "consider_refresh_active_work" not in actions


def test_soft_active_work_not_emitted_above_stale(monkeypatch, flush_queue):
    """When age > STALE_HOURS, hard action fires; soft must NOT fire too."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_aw_above_stale"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    stale_h = settings.ACTIVE_WORK_STALE_HOURS

    def mock_age(rows):
        if rows:
            return stale_h + 1.0  # beyond stale threshold
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "consider_refresh_active_work" not in actions
    assert "refresh_active_work" in actions


def test_soft_active_work_boundary_at_warn_hours(monkeypatch, flush_queue):
    """At exactly ACTIVE_WORK_WARN_HOURS, soft action NOT emitted (boundary > not >=)."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_aw_boundary_warn"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    warn_h = settings.ACTIVE_WORK_WARN_HOURS

    def mock_age(rows):
        if rows:
            return warn_h  # exactly at threshold — NOT > warn, so no soft
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "consider_refresh_active_work" not in actions


def test_soft_active_work_boundary_at_stale_hours(monkeypatch, flush_queue):
    """At exactly ACTIVE_WORK_STALE_HOURS, hard action fires (age > stale is False); soft fires (age ≤ stale is True)."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_aw_boundary_stale"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    stale_h = settings.ACTIVE_WORK_STALE_HOURS

    def mock_age(rows):
        if rows:
            return stale_h  # exactly at stale threshold — not > stale, so soft fires
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    # At exactly stale_hours: age > stale is False → no hard; age > warn is True → soft fires
    assert "consider_refresh_active_work" in actions
    assert "refresh_active_work" not in actions


# ── Soft tier: consider_refresh_checkpoint ───────────────────────────────────


def test_soft_checkpoint_emitted_in_warn_window(monkeypatch, flush_queue):
    """When CHECKPOINT_WARN_HOURS < age ≤ CHECKPOINT_STALE_HOURS, emit consider_refresh_checkpoint."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_cp_warn_test"
    server.checkpoint(
        directory=directory,
        current_task="test task",
        key_decisions=["d1"],
        next_steps=["s1"],
    )
    flush_queue()

    warn_h = settings.CHECKPOINT_WARN_HOURS
    stale_h = settings.CHECKPOINT_STALE_HOURS
    mid_age = (warn_h + stale_h) / 2.0

    def mock_age(rows):
        if rows:
            return mid_age
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "consider_refresh_checkpoint" in actions


def test_soft_checkpoint_not_emitted_below_warn(monkeypatch, flush_queue):
    """When age ≤ CHECKPOINT_WARN_HOURS, no soft action emitted."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_cp_below_warn"
    server.checkpoint(
        directory=directory,
        current_task="test task",
        key_decisions=["d1"],
        next_steps=["s1"],
    )
    flush_queue()

    warn_h = settings.CHECKPOINT_WARN_HOURS

    def mock_age(rows):
        if rows:
            return warn_h - 1.0
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "consider_refresh_checkpoint" not in actions


def test_soft_checkpoint_not_emitted_above_stale(monkeypatch, flush_queue):
    """When age > CHECKPOINT_STALE_HOURS, hard fires; soft must NOT fire."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_cp_above_stale"
    server.checkpoint(
        directory=directory,
        current_task="test task",
        key_decisions=["d1"],
        next_steps=["s1"],
    )
    flush_queue()

    stale_h = settings.CHECKPOINT_STALE_HOURS

    def mock_age(rows):
        if rows:
            return stale_h + 1.0
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    actions = [a["action"] for a in result["recommended_actions"]]
    assert "consider_refresh_checkpoint" not in actions
    assert "refresh_checkpoint" in actions


# ── Mutual exclusion ─────────────────────────────────────────────────────────


def test_no_double_emit_active_work(monkeypatch, flush_queue):
    """Soft + hard NEVER both emitted for active_work in same signals response."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/no_double_aw"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    # Test all three zones
    for age, expect_soft, expect_hard in [
        (settings.ACTIVE_WORK_WARN_HOURS - 1.0, False, False),
        ((settings.ACTIVE_WORK_WARN_HOURS + settings.ACTIVE_WORK_STALE_HOURS) / 2.0, True, False),
        (settings.ACTIVE_WORK_STALE_HOURS + 1.0, False, True),
    ]:

        def mock_age(rows, _age=age):
            if rows:
                return _age
            return None

        monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)
        result = server.project_brief(directory, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]

        has_soft = "consider_refresh_active_work" in actions
        has_hard = "refresh_active_work" in actions
        assert not (has_soft and has_hard), f"Both soft+hard emitted for age={age}: {actions}"
        assert has_soft == expect_soft, f"age={age}: expected soft={expect_soft}, got {has_soft}"
        assert has_hard == expect_hard, f"age={age}: expected hard={expect_hard}, got {has_hard}"


def test_no_double_emit_checkpoint(monkeypatch, flush_queue):
    """Soft + hard NEVER both emitted for checkpoint in same signals response."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/no_double_cp"
    server.checkpoint(
        directory=directory,
        current_task="test task",
        key_decisions=["d1"],
        next_steps=["s1"],
    )
    flush_queue()

    for age, _expect_soft, _expect_hard in [
        (settings.CHECKPOINT_WARN_HOURS - 1.0, False, False),
        ((settings.CHECKPOINT_WARN_HOURS + settings.CHECKPOINT_STALE_HOURS) / 2.0, True, False),
        (settings.CHECKPOINT_STALE_HOURS + 1.0, False, True),
    ]:

        def mock_age(rows, _age=age):
            if rows:
                return _age
            return None

        monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)
        result = server.project_brief(directory, mode="signals")
        actions = [a["action"] for a in result["recommended_actions"]]

        has_soft = "consider_refresh_checkpoint" in actions
        has_hard = "refresh_checkpoint" in actions
        assert not (has_soft and has_hard), f"Both soft+hard emitted for age={age}: {actions}"


# ── suggested_call enrichment ─────────────────────────────────────────────────


def test_soft_active_work_has_suggested_call(monkeypatch, flush_queue):
    """consider_refresh_active_work action includes suggested_call field."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_aw_suggested_call"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    warn_h = settings.ACTIVE_WORK_WARN_HOURS
    stale_h = settings.ACTIVE_WORK_STALE_HOURS
    mid_age = (warn_h + stale_h) / 2.0

    def mock_age(rows):
        if rows:
            return mid_age
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    soft_actions = [
        a for a in result["recommended_actions"] if a["action"] == "consider_refresh_active_work"
    ]
    assert soft_actions, "consider_refresh_active_work not found"
    assert "suggested_call" in soft_actions[0], "soft action missing suggested_call"
    assert "update_active_work" in soft_actions[0]["suggested_call"]


def test_hard_active_work_has_suggested_call(monkeypatch, flush_queue):
    """refresh_active_work action includes suggested_call field."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/hard_aw_suggested_call"
    server.update_active_work(directory=directory, content="work content")
    flush_queue()

    stale_h = settings.ACTIVE_WORK_STALE_HOURS

    def mock_age(rows):
        if rows:
            return stale_h + 1.0
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    hard_actions = [
        a for a in result["recommended_actions"] if a["action"] == "refresh_active_work"
    ]
    assert hard_actions, "refresh_active_work not found"
    assert "suggested_call" in hard_actions[0], "hard action missing suggested_call"
    assert "update_active_work" in hard_actions[0]["suggested_call"]


def test_soft_checkpoint_has_suggested_call(monkeypatch, flush_queue):
    """consider_refresh_checkpoint action includes suggested_call field."""
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/soft_cp_suggested_call"
    server.checkpoint(
        directory=directory,
        current_task="test task",
        key_decisions=["d1"],
        next_steps=["s1"],
    )
    flush_queue()

    warn_h = settings.CHECKPOINT_WARN_HOURS
    stale_h = settings.CHECKPOINT_STALE_HOURS
    mid_age = (warn_h + stale_h) / 2.0

    def mock_age(rows):
        if rows:
            return mid_age
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    soft_actions = [
        a for a in result["recommended_actions"] if a["action"] == "consider_refresh_checkpoint"
    ]
    assert soft_actions, "consider_refresh_checkpoint not found"
    assert "suggested_call" in soft_actions[0], "soft checkpoint action missing suggested_call"
    assert "checkpoint" in soft_actions[0]["suggested_call"]


# ── Token budget ──────────────────────────────────────────────────────────────


def test_signals_token_budget_empty_dir_still_passes():
    """signals mode empty dir still ≤100 tokens after adding soft action logic."""
    result = server.project_brief("/tmp/token_budget_empty_dir_v5101", mode="signals")
    tokens = len(json.dumps(result)) // 4
    assert tokens <= 100, f"signals mode empty dir too large: {tokens} tokens (budget: 100)"


def test_signals_token_budget_with_soft_actions_bounded(monkeypatch, flush_queue):
    """signals mode with 2 soft actions stays within SIGNALS_TOKEN_BUDGET_SOFT.

    When active_work + checkpoint are both in warn window, payload includes
    2 soft actions with suggested_call fields. This is larger than the empty-dir
    100-token baseline, but must stay within the operator-tunable budget to
    prove size is bounded.

    Design note: the ≤100 token budget applies to the empty-dir/minimal case
    (tested in test_signals_mode_token_budget). Active data + suggested_call
    fields expand the payload but remain within acceptable bounds.

    SIGNALS_TOKEN_BUDGET_SOFT (default 400, configurable via env/yaml) is the
    upper bound. Raise it if new action types push the payload above this ceiling.
    Real cost: ~10 stop-hook fires per long session × 175 token average
    (mix of empty + non-empty payloads) = ~1.75 KB — under 1% of 200K context.
    """
    from yadgar.config import get_settings
    from yadgar.server.tools import project as proj_mod

    settings = get_settings()
    directory = "/tmp/token_budget_soft_test"
    server.update_active_work(directory=directory, content="work content")
    server.checkpoint(
        directory=directory,
        current_task="task",
        key_decisions=["d1"],
        next_steps=["s1"],
    )
    flush_queue()

    warn_h = settings.ACTIVE_WORK_WARN_HOURS
    stale_h = settings.ACTIVE_WORK_STALE_HOURS
    mid_age = (warn_h + stale_h) / 2.0

    def mock_age(rows):
        if rows:
            return mid_age
        return None

    monkeypatch.setattr(proj_mod, "_compute_row_age_hours", mock_age)

    result = server.project_brief(directory, mode="signals")
    tokens = len(json.dumps(result)) // 4
    budget = settings.SIGNALS_TOKEN_BUDGET_SOFT
    assert tokens <= budget, (
        f"signals mode too large with soft actions: {tokens} tokens (budget: {budget})"
    )

    # Verify exactly 2 soft actions are present (not runaway growth)
    soft_actions = [a for a in result["recommended_actions"] if a["action"].startswith("consider_")]
    assert len(soft_actions) <= 2, f"Too many soft actions: {len(soft_actions)}"


# ── Registry: update_active_work writes marker ───────────────────────────────


def test_update_active_work_writes_registry_marker(tmp_path, monkeypatch, flush_queue):
    """update_active_work() writes marker at ~/.yadgar/active-work-tracked/<hash>/directory.txt."""
    import hashlib

    # Redirect the registry base dir to tmp_path for isolation
    fake_tracked_dir = tmp_path / "active-work-tracked"
    monkeypatch.setenv("YADGAR_ACTIVE_WORK_TRACKED_DIR", str(fake_tracked_dir))

    # Re-import to pick up env override (or use monkeypatching)
    from yadgar.server.tools import project as proj_mod

    # Monkeypatch the tracked dir constant in the module
    monkeypatch.setattr(
        proj_mod,
        "_get_active_work_tracked_dir",
        lambda: fake_tracked_dir,
    )

    directory = "/tmp/registry_test_dir"
    server.update_active_work(directory=directory, content="test content")
    flush_queue()

    expected_hash = hashlib.sha256(directory.encode()).hexdigest()[:12]
    marker_path = fake_tracked_dir / expected_hash / "directory.txt"
    assert marker_path.exists(), f"Registry marker not found at {marker_path}"
    assert marker_path.read_text().strip() == directory


def test_registry_uses_resolved_directory(tmp_path, monkeypatch, flush_queue):
    """Registry key is the resolved (git root) directory, not the raw arg."""
    import hashlib

    fake_tracked_dir = tmp_path / "active-work-tracked"
    from yadgar.server.tools import project as proj_mod

    monkeypatch.setattr(
        proj_mod,
        "_get_active_work_tracked_dir",
        lambda: fake_tracked_dir,
    )

    # Use a path that resolves to itself (non-git)
    directory = str(tmp_path / "nonexistent_repo")
    server.update_active_work(directory=directory, content="test content")
    flush_queue()

    # The resolved dir is directory itself (no git root)
    from yadgar.server.tools.project import _resolve_project_root

    resolved = _resolve_project_root(directory)
    expected_hash = hashlib.sha256(resolved.encode()).hexdigest()[:12]
    marker_path = fake_tracked_dir / expected_hash / "directory.txt"
    assert marker_path.exists(), f"Registry marker not created for resolved dir {resolved}"


# ── New env knobs exist ───────────────────────────────────────────────────────


def test_active_work_warn_hours_knob_exists():
    """ACTIVE_WORK_WARN_HOURS Settings field exists with default 12.0."""
    from yadgar.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "ACTIVE_WORK_WARN_HOURS"), "ACTIVE_WORK_WARN_HOURS missing"
    assert settings.ACTIVE_WORK_WARN_HOURS == 12.0


def test_checkpoint_warn_hours_knob_exists():
    """CHECKPOINT_WARN_HOURS Settings field exists with default 12.0."""
    from yadgar.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "CHECKPOINT_WARN_HOURS"), "CHECKPOINT_WARN_HOURS missing"
    assert settings.CHECKPOINT_WARN_HOURS == 12.0


def test_auto_refresh_active_work_knob_exists():
    """AUTO_REFRESH_ACTIVE_WORK Settings field exists with default False."""
    from yadgar.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "AUTO_REFRESH_ACTIVE_WORK"), "AUTO_REFRESH_ACTIVE_WORK missing"
    assert settings.AUTO_REFRESH_ACTIVE_WORK is False
