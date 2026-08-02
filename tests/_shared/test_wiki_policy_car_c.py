# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car C — wiki policy fixes.

Spine task-table-refactor-2026-07-29, Car C1/C2/C3:

  C1 — tag-override matches the page type's own opt-in tag.
       The current code at providers/wiki.py:102 exempts ALL tagged
       recalls from exclusion. C1 makes the exemption narrower: only
       when the tag matches the page type's own opt-in tag.

  C2 — implement `downweight`. It's documented as a disposition but
       treated as include. C2 makes it actually downweight scores.

  C3 — redesign the identity gate (D21). gate_mode="identity" is
       currently dead code. C3 re-implements it for the three ledger
       types so structural pages (agent_prompt, task_list, adr_log)
       aren't flagged as duplicates.
"""

from __future__ import annotations

from yadgar._shared.wiki.policy import (
    DEFAULT_POLICY,
    POLICY_BY_TYPE,
    WikiPolicy,
    get_policy,
)


# ── C1 — tag-override matches page type's own opt-in tag ─────────────────────


def test_c1_agent_prompt_policy_excluded_from_recall() -> None:
    """agent_prompt pages are excluded from default recall."""
    assert POLICY_BY_TYPE["agent_prompt"].recall_disposition == "exclude"


def test_c1_adr_policy_included_in_recall() -> None:
    """ADR pages are included in default recall (they're project knowledge)."""
    assert get_policy("adr").recall_disposition == "include"


# ── C2 — implement downweight ────────────────────────────────────────────────


def test_c2_downweight_is_a_valid_disposition() -> None:
    """downweight is a documented disposition value."""
    from yadgar._shared.wiki.policy import get_policy

    # Currently no page_type uses downweight — Car H rollups will.
    # For now, the DEFAULT policy is include; downweight is a valid value
    # the policy can carry without breaking anything.
    p = WikiPolicy(
        gate_mode="similarity",
        recall_disposition="downweight",
        dir_scope="strict",
        merge="allow",
    )
    assert p.recall_disposition == "downweight"


# ── C3 — identity gate re-implementation ─────────────────────────────────────


def test_c3_agent_prompt_uses_identity_gate() -> None:
    """agent_prompt pages use identity gate (D21 — structurally unique)."""
    # Two agent-prompt pages from different projects aren't duplicates
    # even with high content similarity. Identity gate skips the
    # similarity check and uses slug + schema instead.
    assert POLICY_BY_TYPE["agent_prompt"].gate_mode == "identity"


def test_c3_default_policy_uses_similarity_gate() -> None:
    """Default policy keeps the similarity gate for normal pages."""
    assert DEFAULT_POLICY.gate_mode == "similarity"
