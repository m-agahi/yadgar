# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car C1 — tag-override matches page type's own opt-in tag.

Spine task-table-refactor-2026-07-29, Car C1: the current code at
providers/wiki.py:102 exempts ALL tagged recalls from exclusion. C1
narrows this: only when the caller's tag matches the page type's own
opt-in tag does the exemption apply.

This file tests the *resolver* — the function that decides whether a
given (page_type, caller_tags) pair is exempt from exclusion. The
provider call site is updated separately to use this resolver.
"""

from __future__ import annotations

from yadgar._shared.wiki.policy import POLICY_BY_TYPE

# ── Opt-in tag registry ──────────────────────────────────────────────────────


def test_c1_agent_prompt_opt_in_tag() -> None:
    """agent_prompt has an opt-in tag matching its page_type."""
    # The opt-in tag for a page_type is the page_type string itself.
    # This is the convention used by recall(tags=["agent-prompt"]) callers.
    assert "agent_prompt" in POLICY_BY_TYPE
    # The provider checks: caller_tag == page_type
    # We test this via the resolver below.


# ── Resolver: should_exclude_from_recall(page_type, caller_tags) ─────────────


def test_c1_resolver_module_exists() -> None:
    """The resolver function exists at the expected import path."""
    from yadgar.backend.retrieval.providers.wiki import _caller_tag_matches_page_type

    assert callable(_caller_tag_matches_page_type)


def test_c1_no_tags_means_no_exemption() -> None:
    """No caller tags → no exemption, policy exclusion applies."""
    from yadgar.backend.retrieval.providers.wiki import _caller_tag_matches_page_type

    assert _caller_tag_matches_page_type("agent_prompt", None) is False
    assert _caller_tag_matches_page_type("agent_prompt", []) is False


def test_c1_matching_tag_means_exemption() -> None:
    """Caller tag matches page_type → exemption applies."""
    from yadgar.backend.retrieval.providers.wiki import _caller_tag_matches_page_type

    assert _caller_tag_matches_page_type("agent_prompt", ["agent_prompt"]) is True


def test_c1_non_matching_tag_means_no_exemption() -> None:
    """Caller tag doesn't match page_type → no exemption, exclusion applies.

    This is the fix: previously, ANY tagged recall was exempted. Now only
    matching tags exempt. So recall(tags=["unrelated-tag"]) on an
    agent_prompt page still respects the exclusion.
    """
    from yadgar.backend.retrieval.providers.wiki import _caller_tag_matches_page_type

    assert _caller_tag_matches_page_type("agent_prompt", ["unrelated-tag"]) is False
    assert _caller_tag_matches_page_type("agent_prompt", ["adr"]) is False


def test_c1_included_page_type_with_any_tag() -> None:
    """Pages that are included by policy don't need exemption logic."""
    from yadgar.backend.retrieval.providers.wiki import _caller_tag_matches_page_type

    # ADR pages are included by default — tag matching is irrelevant.
    # But the resolver returns False because ADR doesn't need exemption
    # from anything. The provider's exclusion check never fires for ADR.
    assert _caller_tag_matches_page_type("adr", ["anything"]) is False
