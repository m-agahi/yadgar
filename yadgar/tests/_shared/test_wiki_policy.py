"""Tests for yadgar._shared.wiki.policy — WikiPolicy + get_policy().

Car A of #83 (repo-wiki page-type). The repo_wiki page_type itself was
decommissioned (#33/ADR-0162); this resolver mechanism remains in use by
other page types (e.g. agent_prompt).

Policy resolver maps page_type → {gate_mode, recall_disposition, dir_scope, merge}.
Unknown type (including None) returns DEFAULT_POLICY.
"""

from __future__ import annotations

import pytest

# ── Import (RED until policy.py created) ─────────────────────────────────────
from yadgar._shared.wiki.policy import (  # noqa: E402
    DEFAULT_POLICY,
    POLICY_BY_TYPE,
    WikiPolicy,
    get_policy,
)
from yadgar._shared.wiki.wiki_meta import (  # noqa: E402
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
)

# ── A. WikiPolicy dataclass ────────────────────────────────────────────────────


class TestWikiPolicyDataclass:
    """WikiPolicy is a frozen dataclass with the four required fields."""

    def test_fields_exist(self):
        p = WikiPolicy(
            gate_mode="similarity",
            recall_disposition="include",
            dir_scope="strict",
            merge="allow",
        )
        assert p.gate_mode == "similarity"
        assert p.recall_disposition == "include"
        assert p.dir_scope == "strict"
        assert p.merge == "allow"

    def test_frozen_rejects_mutation(self):
        p = WikiPolicy("similarity", "include", "strict", "allow")
        with pytest.raises((AttributeError, TypeError)):
            p.gate_mode = "identity"  # type: ignore[misc]

    def test_equality(self):
        a = WikiPolicy("identity", "exclude", "strict", "never")
        b = WikiPolicy("identity", "exclude", "strict", "never")
        assert a == b

    def test_inequality(self):
        a = WikiPolicy("similarity", "include", "strict", "allow")
        b = WikiPolicy("identity", "exclude", "strict", "never")
        assert a != b


# ── B. DEFAULT_POLICY ─────────────────────────────────────────────────────────


class TestDefaultPolicy:
    """DEFAULT_POLICY has the expected field values."""

    def test_gate_mode_similarity(self):
        assert DEFAULT_POLICY.gate_mode == "similarity"

    def test_recall_disposition_include(self):
        assert DEFAULT_POLICY.recall_disposition == "include"

    def test_dir_scope_strict(self):
        assert DEFAULT_POLICY.dir_scope == "strict"

    def test_merge_allow(self):
        assert DEFAULT_POLICY.merge == "allow"


# ── C. POLICY_BY_TYPE registry ────────────────────────────────────────────────


class TestPolicyByType:
    """POLICY_BY_TYPE has the expected agent_prompt entry."""

    def test_agent_prompt_present(self):
        assert "agent_prompt" in POLICY_BY_TYPE

    def test_agent_prompt_storage_scope_global(self):
        assert POLICY_BY_TYPE["agent_prompt"].storage_scope == "global"


# ── D. get_policy() resolver ──────────────────────────────────────────────────


class TestGetPolicy:
    """get_policy routes to the correct WikiPolicy instance."""

    def test_adr_returns_default(self):
        """'adr' has no override entry → DEFAULT_POLICY."""
        p = get_policy("adr")
        assert p == DEFAULT_POLICY

    def test_none_returns_default(self):
        """None page_type → DEFAULT_POLICY."""
        p = get_policy(None)
        assert p == DEFAULT_POLICY

    def test_random_string_returns_default(self):
        """Unrecognised page_type → DEFAULT_POLICY."""
        p = get_policy("random_nonexistent_type")
        assert p == DEFAULT_POLICY

    def test_agent_prompt_storage_scope_global(self):
        """'agent_prompt' page type resolves storage_scope='global'."""
        p = get_policy("agent_prompt")
        assert p.storage_scope == "global"

    def test_agent_prompt_non_storage_axes(self):
        """'agent_prompt' policy: write-behaviour axes match expected values.

        gate_mode, dir_scope, merge match the default (similarity/strict/allow).
        recall_disposition is "exclude" (Car C policy-driven recall exclusion —
        agent-prompt pages must not pollute everyday recall fanout).
        storage_scope is "global" (the C2 new axis — see test_agent_prompt_storage_scope_global).
        """
        p = get_policy("agent_prompt")
        assert p.gate_mode == "similarity"
        assert p.recall_disposition == "exclude"  # Car C: excluded from fanout recall
        assert p.dir_scope == "strict"
        assert p.merge == "allow"

    def test_none_storage_scope_project(self):
        """None page_type → DEFAULT_POLICY → storage_scope='project'."""
        p = get_policy(None)
        assert p.storage_scope == "project"

    def test_agent_prompt_is_not_default(self):
        """agent_prompt must differ from DEFAULT (single source assertion)."""
        assert get_policy("agent_prompt") != DEFAULT_POLICY


# ── E. ADR-0209 page-type split ───────────────────────────────────────────────


class TestAgentPageTypeSplit:
    """ADR-0209: `agent_prompt` splits into `agent_pattern` + `agent_discipline`.

    The TOC index gets its own `agent_index` type — task 0134's null-page_type
    defect (null → DEFAULT_POLICY include → the index is recall-visible).
    All three carry the agent_prompt routing: exclude from fanout recall,
    global storage scope.
    """

    @pytest.mark.parametrize(
        "page_type",
        [PAGE_TYPE_AGENT_PATTERN, PAGE_TYPE_AGENT_DISCIPLINE, PAGE_TYPE_AGENT_INDEX],
    )
    def test_registered_in_policy_by_type(self, page_type):
        assert page_type in POLICY_BY_TYPE

    @pytest.mark.parametrize(
        "page_type",
        [PAGE_TYPE_AGENT_PATTERN, PAGE_TYPE_AGENT_DISCIPLINE, PAGE_TYPE_AGENT_INDEX],
    )
    def test_excluded_from_fanout_recall(self, page_type):
        assert get_policy(page_type).recall_disposition == "exclude"

    @pytest.mark.parametrize(
        "page_type",
        [PAGE_TYPE_AGENT_PATTERN, PAGE_TYPE_AGENT_DISCIPLINE, PAGE_TYPE_AGENT_INDEX],
    )
    def test_storage_scope_global(self, page_type):
        """The library is a cross-project shared resource (ADR-0159)."""
        assert get_policy(page_type).storage_scope == "global"

    def test_split_types_match_legacy_agent_prompt_policy(self):
        """Splitting the type must not change ROUTING — only the taxonomy.

        A behaviour change smuggled in with the migration would be invisible
        until a page landed in the wrong scope.
        """
        legacy = get_policy("agent_prompt")
        assert get_policy(PAGE_TYPE_AGENT_PATTERN) == legacy
        assert get_policy(PAGE_TYPE_AGENT_DISCIPLINE) == legacy

    def test_legacy_agent_prompt_entry_retained(self):
        """Un-migrated installs still carry page_type='agent_prompt' rows."""
        assert "agent_prompt" in POLICY_BY_TYPE

    def test_agent_index_not_in_page_type_schemas(self):
        """The TOC is a link index, not a Purpose/Prompt page.

        Registering it in wiki_page_types.yaml would demand a required section
        the TOC body does not carry, producing a permanent lint warning.
        check_page_type_format returns [] for unregistered types, so
        policy-only registration gets exclude-routing with zero lint noise.
        """
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPES

        assert PAGE_TYPE_AGENT_INDEX not in PAGE_TYPES

    @pytest.mark.parametrize(
        "page_type",
        [PAGE_TYPE_AGENT_PATTERN, PAGE_TYPE_AGENT_DISCIPLINE],
    )
    def test_split_types_registered_in_page_type_schemas(self, page_type):
        """Both prompt families keep the agent_prompt Purpose/Prompt shape."""
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPES

        assert PAGE_TYPES[page_type] == ["Purpose", "Prompt"]


# ── F. Car C2 — downweight disposition (D22) ──────────────────────────────────


class TestDownweightDisposition:
    """Car C2 (0047 §7 3b): third recall_disposition value "downweight".

    A ``task_list`` page stays recall-visible (``is_recall_visible`` returns
    True — exclusion still drops only ``"exclude"``) but its ranking score is
    multiplied by ``RECALL_DOWNWEIGHT_FACTOR`` (< 1.0) at the scoring stage
    so it sinks below ``"include"`` pages of comparable relevance.

    The helper ``downweight_multiplier(page, factor)`` is the single source of
    truth for the penalty — called from both fusion (unified recall) and
    ``wiki_query`` (the legacy search tool).
    """

    def test_task_list_resolves_to_downweight(self):
        """``task_list`` page type resolves to ``recall_disposition="downweight"``."""
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert get_policy(PAGE_TYPE_TASK_LIST).recall_disposition == "downweight"

    def test_task_list_differs_from_default(self):
        """``task_list`` policy must differ from DEFAULT (single source assertion)."""
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert get_policy(PAGE_TYPE_TASK_LIST) != DEFAULT_POLICY

    def test_downweight_multiplier_returns_factor_for_task_list(self):
        """A downweight-disposition page returns *factor* from the helper."""
        from yadgar._shared.wiki.policy import downweight_multiplier
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert downweight_multiplier({"page_type": PAGE_TYPE_TASK_LIST}, 0.5) == 0.5

    def test_downweight_multiplier_returns_one_for_include(self):
        """An include-disposition page returns 1.0 from the helper."""
        from yadgar._shared.wiki.policy import downweight_multiplier

        assert downweight_multiplier({"page_type": None}, 0.5) == 1.0

    def test_downweight_multiplier_returns_one_for_exclude(self):
        """An exclude-disposition page returns 1.0 from the helper.

        The penalty is for the SCORING stage; exclusion happens earlier
        (``is_recall_visible``) — an excluded page never reaches the scorer,
        so its multiplier is irrelevant. Returning 1.0 keeps the helper
        composable: it can be applied unconditionally without leaking
        penalty to excluded pages that happen to slip through.
        """
        from yadgar._shared.wiki.policy import downweight_multiplier

        assert downweight_multiplier({"page_type": PAGE_TYPE_AGENT_PATTERN}, 0.5) == 1.0

    def test_is_recall_visible_passes_downweight(self):
        """A downweight page survives the visibility filter (it is NOT excluded).

        Downweight is a RANKING penalty, not an exclusion — the page must
        remain visible to the search paths so the penalty can reorder it
        below include-disposition pages.
        """
        from yadgar._shared.wiki.policy import is_recall_visible
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert is_recall_visible({"page_type": PAGE_TYPE_TASK_LIST}) is True
