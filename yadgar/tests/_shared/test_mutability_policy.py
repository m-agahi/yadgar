"""Tests for the wiki page mutability policy (Car J, ADR-0081/0082).

Scope: per-page mutability axis on ``WikiPolicy`` + per-page override
``mutability_override`` on ``wiki_page`` + storage-layer enforcement of
locked/derived writes.

Mutability is a write-side concern: it blocks well-intentioned repairs
(rewriting a derived rollup, stripping an ADR's superseded tag) but does NOT
block sanctioned server-side lifecycle transitions (the Car G supersede
retype, the Car K nightly sweep on ``derived`` rollups).

Three mutability values:

- ``"free"`` — no enforcement; agent/tool writes allowed.
- ``"locked"`` — block agent/tool writes; sanctioned transitions pass.
- ``"derived"`` — block all writes (regenerated, not hand-edited); sanctioned
  transitions pass (Car K nightly sweep deletes + recreates).

Per-type defaults (D26):
- ``adr`` / ``adr_superseded`` → ``locked``
- ``task`` / ``agent_pattern`` / ``agent_discipline`` / ``agent_prompt``
  → ``free``
- everything else (including ``None``) → ``free``

A per-page ``mutability_override`` on the wiki_page row wins over the per-type
default.

Enforcement lives at the STORAGE chokepoint (``_WikiMixin.update_wiki_page``
+ insert/delete for symmetry) so it covers every write path —
``WikiStore._apply_text_edit`` (8 anchor-text/positional edit ops) and the
``admin_exec.wiki_update`` op that bypasses ``WikiStore`` entirely.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar._shared.wiki.policy import (
    DEFAULT_POLICY,
    MUTABILITY_BY_TYPE,
    WikiPolicy,
    get_effective_mutability,
    get_policy,
)
from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_PATTERN,
    PAGE_TYPE_AGENT_PROMPT_LEGACY,
)

# C13 (0047 PR#40 §5): seeds must NAME the project they write into —
# C5 deleted every fallback that used to answer an unnamed write (ADR-0227).
# A per-file constant, deliberately NOT a shared fixture default: a new test
# that builds its own write payload still reds — the signal of the flip.
_PROJECT = "m-agahi/yadgar"

# ── Module-scoped storage fixture (matches test_storage.py pattern) ──────────


@pytest.fixture(scope="module")
def storage(module_storage):  # noqa: ARG001 — delegation pattern (test_storage.py)
    return module_storage


def _insert_wiki_page(
    storage: StorageEngine,
    slug: str,
    *,
    page_type: str | None = None,
    mutability_override: str | None = None,
    directory_context: str = "/tmp/mut-test",
) -> int:
    """Insert a wiki page row with optional mutability_override.

    Seeds bypass the mutability gate at insert time (the test helper writes
    pages of every mutability tier; production insert paths go through
    ``WikiStore.add`` or are sanctioned). We pass ``_sanctioned=True`` at
    insert so the test isn't gated on its own setup.
    """
    page: dict = {
        "slug": slug,
        "title": f"Test page {slug}",
        "content": f"# {slug}\n\nbody",
        "category": "reference",
        "tags": [],
        "confidence": "high",
        "source_memory_ids": [],
        "links": [],
        "directory_context": directory_context,
        "project_id": _PROJECT,
        "_sanctioned": True,
    }
    if page_type is not None:
        page["page_type"] = page_type
    pid = storage.insert_wiki_page(page)
    if mutability_override is not None:
        # Write directly — _shared doesn't have a helper yet (Car J's
        # ``WikiStore.set_mutability_by_slug`` lives in ``_shared/wiki/store.py``).
        # We bypass the gate via _sanctioned=True because we're seeding.
        storage.update_wiki_page(
            pid,
            {"mutability_override": mutability_override},
            _sanctioned=True,
        )
    return pid


# ── A. WikiPolicy dataclass — mutability field #6 ─────────────────────────────


class TestWikiPolicyMutabilityField:
    """WikiPolicy gains ``mutability`` as field #6 with a default."""

    def test_field_exists_with_default_free(self):
        """A WikiPolicy constructed with 4 positional args defaults mutability='free'.

        Car J appends the field; existing 4–5-arg positional callers (tests in
        this file's neighbours) must keep constructing without error.
        """
        p = WikiPolicy("similarity", "include", "strict", "allow")
        assert p.mutability == "free"

    def test_field_can_be_set_explicitly(self):
        p = WikiPolicy(
            "identity",
            "include",
            "strict",
            "allow",
            storage_scope="project",
            opt_in_tag=None,
            mutability="locked",
        )
        assert p.mutability == "locked"

    def test_field_can_be_set_with_full_kwargs(self):
        p = WikiPolicy(
            gate_mode="identity",
            recall_disposition="include",
            dir_scope="strict",
            merge="allow",
            storage_scope="project",
            opt_in_tag=None,
            mutability="derived",
        )
        assert p.mutability == "derived"

    def test_positional_six_arg_construction(self):
        """Existing 5-arg positional callers (test_wiki_policy.py et al.) still pass.

        Adding field #6 with a default preserves their behaviour. Constructing
        a 6-arg policy is also legal now.
        """
        p = WikiPolicy("similarity", "include", "strict", "allow", "project", None)
        assert p.mutability == "free"  # default still applies — 7th field

    def test_default_policy_mutability_is_free(self):
        """DEFAULT_POLICY (fallback for unrecognised types) is free."""
        assert DEFAULT_POLICY.mutability == "free"


# ── B. MUTABILITY_BY_TYPE — D26 per-type defaults ─────────────────────────────


class TestMutabilityByType:
    """D26 per-type mutability defaults.

    ``adr`` / ``adr_superseded`` → locked (decisions are immutable, lifecycle
    transitions are the only path). ``task``, ``agent_pattern``,
    ``agent_discipline``, ``agent_prompt`` → free (operational knowledge).
    """

    def test_adr_locked(self):
        assert MUTABILITY_BY_TYPE["adr"] == "locked"

    def test_adr_superseded_locked(self):
        """Superseded ADRs stay locked — the supersede retype (Car G) is the
        only sanctioned path that mutates an ADR's mutability."""
        assert MUTABILITY_BY_TYPE["adr_superseded"] == "locked"

    @pytest.mark.parametrize(
        "page_type",
        [
            "task",
            PAGE_TYPE_AGENT_PATTERN,
            PAGE_TYPE_AGENT_DISCIPLINE,
            PAGE_TYPE_AGENT_PROMPT_LEGACY,
        ],
    )
    def test_free_types(self, page_type):
        assert MUTABILITY_BY_TYPE[page_type] == "free"

    def test_rollup_type_is_derived(self):
        """``wiki_rollup`` (Car K) is regenerated, not hand-edited → derived."""
        assert MUTABILITY_BY_TYPE["wiki_rollup"] == "derived"

    def test_unknown_type_falls_through_to_free(self):
        """get_effective_mutability returns 'free' for unknown page_type when no override."""
        assert get_effective_mutability({"page_type": "unknown_xyz"}, override=None) == "free"


# ── C. get_effective_mutability — resolver ────────────────────────────────────


class TestGetEffectiveMutability:
    """Single resolver: override wins over per-type default; per-type default
    wins over the schema-level free fallback.

    Centralising the resolution here (Car J §4.7) means storage + tool layer
    share one function — no duplicated logic.
    """

    def test_override_wins_over_default(self):
        """A per-page override trumps the per-type default."""
        assert get_effective_mutability({"page_type": "adr"}, override="free") == "free"

    def test_override_wins_over_free_default(self):
        """Override to 'derived' on a 'task' (free-default) page → derived."""
        assert get_effective_mutability({"page_type": "task"}, override="derived") == "derived"

    def test_override_to_none_clears_back_to_default(self):
        """A None override resolves to the per-type default."""
        assert get_effective_mutability({"page_type": "adr"}, override=None) == "locked"
        assert get_effective_mutability({"page_type": "task"}, override=None) == "free"

    def test_adr_default_is_locked_when_no_override(self):
        assert get_effective_mutability({"page_type": "adr"}, override=None) == "locked"

    def test_no_page_type_is_free_when_no_override(self):
        assert get_effective_mutability({}, override=None) == "free"

    def test_get_policy_returns_locked_for_adr(self):
        """get_policy surfaces the new field on the policy instance."""
        p = get_policy("adr")
        assert p.mutability == "locked"


# ── D. storage.update_wiki_page — mutability enforcement ──────────────────────


class TestStorageUpdateMutabilityGate:
    """The D25/D26 enforcement point — ``_WikiMixin.update_wiki_page`` rejects
    non-sanctioned writes on locked/derived pages.
    """

    def test_locked_adr_rejects_content_update(self, storage: StorageEngine):
        """A locked ``adr`` page rejects update_wiki_page with a MutabilityLocked error."""
        pid = _insert_wiki_page(storage, "test-locked-adr-1", page_type="adr")
        # _sanctioned=False (default) — gate fires
        with pytest.raises(PermissionError, match="locked"):
            storage.update_wiki_page(pid, {"content": "tampered"})

    def test_locked_adr_rejects_tags_update(self, storage: StorageEngine):
        """D25 vector — stripping `adr-status:superseded` via tags is blocked."""
        pid = _insert_wiki_page(storage, "test-locked-adr-tags-1", page_type="adr")
        with pytest.raises(PermissionError, match="locked"):
            storage.update_wiki_page(
                pid,
                {"tags": ["adr-status:superseded"]},
            )

    def test_locked_adr_rejects_unrelated_field_update(self, storage: StorageEngine):
        """Even a non-content field (e.g. category) is blocked on a locked page."""
        pid = _insert_wiki_page(storage, "test-locked-adr-cat-1", page_type="adr")
        with pytest.raises(PermissionError, match="locked"):
            storage.update_wiki_page(pid, {"category": "tampered"})

    def test_task_page_accepts_update(self, storage: StorageEngine):
        """A ``task`` (free-default) page accepts the same update."""
        pid = _insert_wiki_page(storage, "test-free-task-1", page_type="task")
        # No exception → success
        assert storage.update_wiki_page(pid, {"content": "new body"}) is True

    def test_sanctioned_update_bypasses_locked_gate(self, storage: StorageEngine):
        """A ``_sanctioned=True`` update on a locked ``adr`` page IS allowed.

        D26: ``locked`` blocks agent/tool edits, NOT sanctioned server-side
        lifecycle transitions. The Car G supersede retype is the canonical
        consumer of this seam.
        """
        pid = _insert_wiki_page(storage, "test-sanctioned-adr-1", page_type="adr")
        # Should NOT raise
        assert (
            storage.update_wiki_page(
                pid,
                {"page_type": "adr_superseded"},
                _sanctioned=True,
            )
            is True
        )

    def test_override_to_free_unlocks_page(self, storage: StorageEngine):
        """Setting ``mutability_override='free'`` on an ``adr`` page unblocks writes.

        Path: set override via _sanctioned=True (admin-only tool is the sole
        sanctioned writer of mutability_override), then write normally.
        """
        pid = _insert_wiki_page(
            storage,
            "test-override-free-adr-1",
            page_type="adr",
            mutability_override="free",
        )
        # Should NOT raise — override wins
        assert storage.update_wiki_page(pid, {"content": "now writable"}) is True

    def test_derived_page_rejects_update(self, storage: StorageEngine):
        """A ``derived`` (wiki_rollup) page rejects updates — regenerated, not hand-edited."""
        pid = _insert_wiki_page(
            storage,
            "test-derived-rollup-1",
            page_type="wiki_rollup",
        )
        with pytest.raises(PermissionError, match="derived"):
            storage.update_wiki_page(pid, {"content": "stale"})

    def test_sanctioned_update_bypasses_derived_gate(self, storage: StorageEngine):
        """A ``_sanctioned=True`` update on a ``derived`` page IS allowed (Car K sweep)."""
        pid = _insert_wiki_page(
            storage,
            "test-derived-sanctioned-1",
            page_type="wiki_rollup",
        )
        assert (
            storage.update_wiki_page(
                pid,
                {"content": "regenerated"},
                _sanctioned=True,
            )
            is True
        )


# ── E. storage.delete_wiki_page — mutability symmetry ─────────────────────────


class TestStorageDeleteMutabilityGate:
    """Insert/delete symmetry — locked/derived pages must not be hand-deleted."""

    def test_locked_adr_rejects_delete(self, storage: StorageEngine):
        pid = _insert_wiki_page(storage, "test-del-locked-adr-1", page_type="adr")
        with pytest.raises(PermissionError, match="locked"):
            storage.delete_wiki_page(pid, _sanctioned=False)

    def test_sanctioned_delete_on_locked_passes(self, storage: StorageEngine):
        pid = _insert_wiki_page(storage, "test-del-sanctioned-adr-1", page_type="adr")
        assert storage.delete_wiki_page(pid, _sanctioned=True) is True

    def test_free_task_accepts_delete(self, storage: StorageEngine):
        pid = _insert_wiki_page(storage, "test-del-free-task-1", page_type="task")
        assert storage.delete_wiki_page(pid) is True


# ── F. Effective-mutability resolution from a wiki_page dict ─────────────────


class TestResolveFromPageDict:
    """The resolver accepts the stored page dict and reads ``mutability_override``
    directly off it (storage layer passes the row it already has in hand).
    """

    def test_override_on_row_wins(self):
        page = {"page_type": "adr", "mutability_override": "free"}
        assert get_effective_mutability(page, override=None) == "free"

    def test_no_override_falls_back_to_per_type_default(self):
        page = {"page_type": "adr", "mutability_override": None}
        assert get_effective_mutability(page, override=None) == "locked"

    def test_explicit_override_kwarg_beats_row_override(self):
        """The ``override`` kwarg is an explicit value passed by callers
        that have already resolved it (e.g. wiki_set_mutability)."""
        page = {"page_type": "adr", "mutability_override": "free"}
        assert get_effective_mutability(page, override="derived") == "derived"
