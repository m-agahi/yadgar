"""Tests for yadgar._shared.wiki.policy — WikiPolicy + get_policy().

Car A of #83 (repo-wiki page-type).

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
    """POLICY_BY_TYPE has the expected repo_wiki entry."""

    def test_repo_wiki_present(self):
        assert "repo_wiki" in POLICY_BY_TYPE

    def test_repo_wiki_gate_mode_identity(self):
        assert POLICY_BY_TYPE["repo_wiki"].gate_mode == "identity"

    def test_repo_wiki_recall_exclude(self):
        assert POLICY_BY_TYPE["repo_wiki"].recall_disposition == "exclude"

    def test_repo_wiki_dir_scope_strict(self):
        assert POLICY_BY_TYPE["repo_wiki"].dir_scope == "strict"

    def test_repo_wiki_merge_never(self):
        assert POLICY_BY_TYPE["repo_wiki"].merge == "never"


# ── D. get_policy() resolver ──────────────────────────────────────────────────


class TestGetPolicy:
    """get_policy routes to the correct WikiPolicy instance."""

    def test_repo_wiki_returns_identity_policy(self):
        p = get_policy("repo_wiki")
        assert p.gate_mode == "identity"
        assert p.recall_disposition == "exclude"
        assert p.dir_scope == "strict"
        assert p.merge == "never"

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

    def test_repo_wiki_storage_scope_project(self):
        """'repo_wiki' → storage_scope='project' (project-scoped structural pages)."""
        p = get_policy("repo_wiki")
        assert p.storage_scope == "project"

    def test_repo_wiki_is_not_default(self):
        """repo_wiki must differ from DEFAULT (single source assertion)."""
        assert get_policy("repo_wiki") != DEFAULT_POLICY
