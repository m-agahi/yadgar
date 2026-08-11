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

    def test_adr_returns_c3_policy(self):
        """Car C3 (0047 §7 D21): 'adr' resolves to _ADR_POLICY (identity gate).

        Pre-C3 'adr' fell through to DEFAULT_POLICY. C3 introduces _ADR_POLICY
        with gate_mode='identity' so canonical ADR pages bypass the similarity
        gate without needing the ``force=True`` payload flag.
        """
        from yadgar._shared.wiki.policy import _ADR_POLICY

        p = get_policy("adr")
        assert p == _ADR_POLICY
        assert p.gate_mode == "identity"

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

        Car C3 (0047 §7 D21): gate_mode flipped from "similarity" to
        "identity" — agent-prompt pages share structural prose (all
        dispatch scaffolding of the same shape), so the content-similarity
        gate false-positives on every write. dir_scope and merge match
        the default (strict/allow). recall_disposition is "exclude" (Car C
        policy-driven recall exclusion — agent-prompt pages must not pollute
        everyday recall fanout). storage_scope is "global" (the C2 new axis
        — see test_agent_prompt_storage_scope_global).
        """
        p = get_policy("agent_prompt")
        assert p.gate_mode == "identity"  # Car C3: identity gate (was "similarity")
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


# ── F. Car C7 — "downweight" disposition RETIRED; task_list is now "exclude" ──


class TestDownweightDisposition:
    """Car C7 (0047 §5 C7) retired the "downweight" recall_disposition (D22/C2).

    C2's ``task_list`` policy stayed recall-visible but scored its ranking
    down via a multiply on ``placement_score``. That multiply was a VERIFIED
    SIGN BUG: ``placement_score = ce + wiki_prior_weight * native_score`` is
    commonly negative (``ce`` is a raw cross-encoder logit), and multiplying a
    negative value by a sub-1.0 factor moves it TOWARD ZERO — an INCREASE
    under "higher ranks first", i.e. the penalty promoted exactly the pages it
    was meant to sink. See ``yadgar/backend/retrieval/providers/fusion.py``
    and the ``PAGE_TYPE_TASK_LIST`` entry in ``POLICY_BY_TYPE`` for the full
    account.

    C7's fix: ``task_list`` is now ``recall_disposition="exclude"`` (dropped
    from search entirely, in the stage-1 SQL WHERE, before it can consume a
    pool slot) and the disposition set is CLOSED at ``{"include", "exclude"}``
    — ``downweight_multiplier`` is deleted, not just unused. These tests pin
    that closure and the concrete task_list flip; renamed from
    ``TestDownweightDisposition``'s original "penalty survives" assertions
    (all of which are now the OPPOSITE of the current contract).
    """

    def test_task_list_resolves_to_exclude_not_downweight(self):
        """``task_list`` now resolves to ``recall_disposition="exclude"``.

        Was: asserted ``"downweight"``. C7 retired that value outright — the
        page is now dropped from search, not ranked-down within it.
        """
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert get_policy(PAGE_TYPE_TASK_LIST).recall_disposition == "exclude"

    def test_task_list_differs_from_default(self):
        """``task_list`` policy must differ from DEFAULT (single source assertion).

        Unchanged invariant — only the REASON it differs moved (gate_mode +
        recall_disposition both diverge from DEFAULT_POLICY now, previously
        recall_disposition alone did under the retired "downweight" value).
        """
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert get_policy(PAGE_TYPE_TASK_LIST) != DEFAULT_POLICY

    def test_downweight_multiplier_helper_is_deleted(self):
        """``downweight_multiplier`` no longer exists on the policy module.

        Was: ``downweight_multiplier({"page_type": PAGE_TYPE_TASK_LIST}, 0.5)
        == 0.5``. The helper — and every call site in fusion.py and
        wiki_query — is deleted along with the disposition value it served.
        This pins the deletion so a future "quick fix" cannot silently
        reintroduce a multiply-based penalty (the sign-bug-prone shape).
        """
        import yadgar._shared.wiki.policy as policy_mod

        assert not hasattr(policy_mod, "downweight_multiplier")

    def test_recall_disposition_set_is_closed_at_include_exclude(self):
        """Every registered policy's ``recall_disposition`` is include or exclude.

        Was: ``downweight_multiplier({"page_type": None}, 0.5) == 1.0`` (an
        include-disposition page is unaffected by the penalty helper). That
        helper is gone; the more valuable invariant it protected — the
        disposition set never grows a third silent value — is asserted
        directly across the whole registry plus DEFAULT_POLICY.
        """
        from yadgar._shared.wiki.policy import DEFAULT_POLICY, POLICY_BY_TYPE

        all_dispositions = {p.recall_disposition for p in POLICY_BY_TYPE.values()} | {
            DEFAULT_POLICY.recall_disposition
        }
        assert all_dispositions <= {"include", "exclude"}, (
            f"recall_disposition set must stay closed at "
            f"{{'include', 'exclude'}}; found {all_dispositions}"
        )

    def test_task_list_gate_mode_is_identity(self):
        """``task_list``'s gate_mode is "identity" (Car C3, unrelated to C2/C7).

        Was: ``downweight_multiplier({"page_type": PAGE_TYPE_AGENT_PATTERN},
        0.5) == 1.0`` — an exclude-disposition page (agent_pattern) is
        unaffected by the deleted helper. Re-pointed to a still-live axis of
        the SAME policy object under test (task_list): the slug-is-identity
        gate that lets canonical task-list writers upsert without tripping
        the content-similarity gate.
        """
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert get_policy(PAGE_TYPE_TASK_LIST).gate_mode == "identity"

    def test_is_recall_visible_now_drops_task_list(self):
        """A task_list page is DROPPED by the visibility filter — the opposite of before.

        Was: ``is_recall_visible({"page_type": PAGE_TYPE_TASK_LIST}) is True``
        ("downweight is a ranking penalty, not an exclusion — must remain
        visible"). Under ``recall_disposition="exclude"`` with
        ``opt_in_tag=None`` (unconditional), the SAME call must now return
        False: the page is excluded outright, not ranked down within a
        visible result set.
        """
        from yadgar._shared.wiki.policy import is_recall_visible
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        assert is_recall_visible({"page_type": PAGE_TYPE_TASK_LIST}) is False
