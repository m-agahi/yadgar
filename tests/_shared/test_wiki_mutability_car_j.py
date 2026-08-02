# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car J — mutability policy.

Spine task-table-refactor-2026-07-29, Car J: add `mutability` as WikiPolicy
field #6 + nullable per-page `mutability_override` + power-gated logged
`wiki_set_mutability`. Enforced at storage/wiki.py:215 update_wiki_page
and the insert/delete paths (NOT WikiStore.add — _apply_text_edit and
8 edit tools bypass it).

D26: per-type policy:
  adr / adr_superseded → locked
  task → free
  agent_prompt → free
  rollups → derived

`locked` blocks agent/tool edits, not sanctioned server-side lifecycle
transitions (otherwise the supersede retype deadlocks against its own guard).
"""

from __future__ import annotations

from yadgar._shared.wiki.policy import DEFAULT_POLICY, POLICY_BY_TYPE, WikiPolicy

# ── Policy field: mutability ─────────────────────────────────────────────────


def test_j_mutability_field_exists() -> None:
    """WikiPolicy has a mutability field with default 'free'."""
    p = WikiPolicy(
        gate_mode="similarity",
        recall_disposition="include",
        dir_scope="strict",
        merge="allow",
    )
    assert hasattr(p, "mutability")
    assert p.mutability == "free"


def test_j_adr_policy_is_locked() -> None:
    """ADR pages are locked (D26)."""
    p = POLICY_BY_TYPE.get("adr", DEFAULT_POLICY)
    assert p.mutability == "locked"


def test_j_task_policy_is_free() -> None:
    """Task pages are free (D26)."""
    p = POLICY_BY_TYPE.get("task", DEFAULT_POLICY)
    assert p.mutability == "free"


def test_j_agent_prompt_policy_is_free() -> None:
    """Agent prompt pages are free (D26)."""
    p = POLICY_BY_TYPE["agent_prompt"]
    assert p.mutability == "free"


# ── Per-page override ───────────────────────────────────────────────────────


def test_j_per_page_override_resolver() -> None:
    """The resolver picks per-page override when set, else policy default."""
    from yadgar._shared.wiki.mutability import effective_mutability

    # No override → use policy
    assert effective_mutability(page_type="adr", override=None) == "locked"
    assert effective_mutability(page_type="task", override=None) == "free"

    # Override beats policy
    assert effective_mutability(page_type="adr", override="free") == "free"
    assert effective_mutability(page_type="task", override="locked") == "locked"


# ── wiki_set_mutability tool ────────────────────────────────────────────────


def test_j_wiki_set_mutability_tool_exists() -> None:
    """The wiki_set_mutability MCP tool is registered."""
    from yadgar.core.server.tools import wiki

    assert hasattr(wiki, "wiki_set_mutability")


def test_j_wiki_set_mutability_validates_value() -> None:
    """wiki_set_mutability rejects values outside {free, locked, derived}."""
    from yadgar.core.server.tools.wiki import wiki_set_mutability

    # Called directly (bypassing MCP wrapper) — returns error dict on bad value
    result = wiki_set_mutability(
        slug="test-slug",
        mutability="bogus",
        reason="test",
        directory="/home/max/git/yadgar",
    )
    assert result.get("ok") is False
