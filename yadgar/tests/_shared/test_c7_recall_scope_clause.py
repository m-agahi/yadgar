"""Car C7 (0047 §5 C7) — the ONE stage-1 recall WHERE clause.

Three things ride in one clause, and each is pinned here:

  1. the project predicate       ``project_id = $p``
  2. the ``global`` REACH tag    ``'global' IN tags``
  3. the page_type exclusion     DERIVED from ``POLICY_BY_TYPE``

THE ANTI-DRIFT TEST (``TestExclusionIsDerivedNotDuplicated``) is the reason
this file exists. The car's claim is "single source of truth": flipping a
disposition in ``policy.py`` ALONE must change the emitted SQL. If it does not,
the claim is decoration and someone has hard-coded a second list. That test
mutates ``POLICY_BY_TYPE`` and asserts the SQL moves — it cannot pass against a
hand-maintained list.

THE OPT-IN ARM (``TestOptInArm``) is blocking, not optional. Emitting
``page_type NOT IN (<every exclude type>)`` unconditionally makes
``recall(type="wiki", tags=["agent-prompt"])`` return NOTHING — the documented
targeted lookup for the entire agent-prompt library (ADR-0007), and the read
side of the dispatch discipline every agent on this train depends on.

THE UNSTAMPED-ROW DECISION (``TestUnstampedRowsDoNotMatch``) is the one choice
that is invisible when wrong. C6 made ``project_id`` an ``option<string>``, so
an un-backfilled row reads as ``None``. Admitting ``IS NONE`` as a sentinel
(the shape the retired ``_ALWAYS_ELIGIBLE`` had) rebuilds the permissive
fallback ADR-0227 deletes, and it looks like it is working.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage.directory import (
    build_project_scope_clause,
    build_recall_scope_clause,
    is_project_eligible,
)
from yadgar._shared.wiki.policy import (
    POLICY_BY_TYPE,
    WikiPolicy,
    build_page_type_exclusion_clause,
    excluded_page_types,
)
from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
    PAGE_TYPE_TASK_LIST,
)

_PROJECT = "m-agahi/yadgar"
_OTHER = "m-agahi/aws-work"


# ── 1. The project predicate + the reach tag ─────────────────────────────────


class TestProjectAndReachArms:
    """Both arms present; dropping either is a silent narrowing."""

    def test_clause_names_project_id(self):
        sql, params = build_project_scope_clause(_PROJECT)
        assert "project_id" in sql, f"project arm missing from clause: {sql!r}"
        assert _PROJECT in params.values(), f"project id not bound: {params}"

    def test_clause_carries_the_global_reach_tag_arm(self):
        """Dropping this arm narrows ~429 globally-reachable rows to one project.

        C6's backfill moves the ``global`` REACH from
        ``directory_context='global'`` onto a TAG. A predicate that only tests
        ``project_id`` therefore loses every cross-project row, and the symptom
        ("recall got worse") does not point at the cause.
        """
        sql, params = build_project_scope_clause(_PROJECT)
        assert "IN tags" in sql, f"reach-tag arm missing from clause: {sql!r}"
        assert "global" in params.values(), (
            f"the reach tag must be BOUND, not inlined, so it cannot drift: {params}"
        )

    def test_arms_are_disjunctive_not_conjunctive(self):
        """A ``global``-tagged row of ANOTHER project must still be reachable."""
        sql, _ = build_project_scope_clause(_PROJECT)
        assert " OR " in sql, (
            f"project and reach arms must be OR-ed — AND would require a row to be "
            f"both this project AND global: {sql!r}"
        )

    def test_absent_project_disables_filtering(self):
        assert build_project_scope_clause(None) == ("", {})
        assert build_project_scope_clause("") == ("", {})

    def test_prefix_prevents_param_collision(self):
        _, a = build_project_scope_clause(_PROJECT, prefix="aa")
        _, b = build_project_scope_clause(_OTHER, prefix="bb")
        assert not (set(a) & set(b)), f"param names collided: {a} vs {b}"


# ── 2. The unstamped-row decision ────────────────────────────────────────────


class TestUnstampedRowsDoNotMatch:
    """``project_id IS NONE`` is NOT a sentinel. This is the C7 decision."""

    def test_clause_has_no_is_none_sentinel_arm(self):
        sql, _ = build_project_scope_clause(_PROJECT)
        assert "IS NONE" not in sql.upper().replace("PAGE_TYPE IS NONE", ""), (
            f"an ``IS NONE`` arm on project_id rebuilds the permissive fallback "
            f"ADR-0227 deletes — every unattributed row would leak into every "
            f"project's recall: {sql!r}"
        )

    def test_row_guard_rejects_unstamped_row(self):
        """The Python mirror agrees with the SQL: unstamped ≠ eligible."""
        assert not is_project_eligible(None, [], _PROJECT)
        assert not is_project_eligible(None, ["yadgar"], _PROJECT)

    def test_row_guard_admits_unstamped_row_carrying_the_reach_tag(self):
        assert is_project_eligible(None, ["global"], _PROJECT)

    def test_row_guard_rejects_other_project(self):
        assert not is_project_eligible(_OTHER, ["yadgar"], _PROJECT)

    def test_row_guard_admits_other_project_with_reach_tag(self):
        assert is_project_eligible(_OTHER, ["global"], _PROJECT)

    def test_row_guard_is_a_noop_without_a_caller_project(self):
        assert is_project_eligible(None, None, None)


# ── 3. The page_type exclusion is DERIVED (the anti-drift test) ──────────────


class TestExclusionIsDerivedNotDuplicated:
    """Flipping a disposition in ``policy.py`` ALONE must move the SQL."""

    def test_task_list_is_excluded(self):
        """C7 absorbed C8 item 4: ``task_list`` → ``exclude``."""
        assert PAGE_TYPE_TASK_LIST in excluded_page_types()

    def test_no_downweight_disposition_survives(self):
        """C7 retired ``downweight`` — the multiply inverted on negative CE logits."""
        survivors = {t for t, p in POLICY_BY_TYPE.items() if p.recall_disposition == "downweight"}
        assert not survivors, (
            f"``downweight`` machinery was deleted in C7; these types still declare it "
            f"and would now silently resolve to a disposition nothing implements: {survivors}"
        )

    def test_flipping_a_disposition_changes_the_emitted_sql(self, monkeypatch):
        """THE anti-drift proof. A hard-coded list cannot pass this.

        ``adr`` is ``include`` today, so it is absent from the exclusion list.
        Flip it to ``exclude`` in the registry ONLY — touch nothing else — and
        the emitted SQL params must now name it.
        """
        before_sql, before_params = build_page_type_exclusion_clause()
        assert "adr" not in _excluded_from(before_params), (
            "precondition: ``adr`` must start as an INCLUDE type for this test to prove anything"
        )

        flipped = dict(POLICY_BY_TYPE)
        original = flipped["adr"]
        flipped["adr"] = WikiPolicy(
            gate_mode=original.gate_mode,
            recall_disposition="exclude",
            dir_scope=original.dir_scope,
            merge=original.merge,
            storage_scope=original.storage_scope,
            opt_in_tag=original.opt_in_tag,
            mutability=original.mutability,
        )
        monkeypatch.setattr("yadgar._shared.wiki.policy.POLICY_BY_TYPE", flipped, raising=True)

        after_sql, after_params = build_page_type_exclusion_clause()
        assert "adr" in _excluded_from(after_params), (
            "flipping ONE disposition in POLICY_BY_TYPE did not change the emitted "
            "SQL — the exclusion list is hard-coded somewhere and will drift silently "
            f"(before={before_sql!r} after={after_sql!r})"
        )

    def test_untyped_pages_are_never_excluded(self):
        """A ``page_type``-less page resolves to DEFAULT_POLICY (``include``).

        Dropping ``IS NONE`` rows would narrow the corpus to typed pages only —
        the overwhelming majority of the wiki carries no page_type at all.
        """
        sql, _ = build_page_type_exclusion_clause()
        assert "page_type IS NONE" in sql, f"untyped rows must survive the exclusion arm: {sql!r}"

    def test_exclusion_binds_a_list_rather_than_inlining_it(self):
        _, params = build_page_type_exclusion_clause()
        excluded = _excluded_from(params)
        assert isinstance(excluded, list) and excluded, (
            f"the excluded set must be a bound param, not string-interpolated: {params}"
        )


# ── 4. The opt-in arm ────────────────────────────────────────────────────────


class TestOptInArm:
    """``recall(tags=["agent-prompt"])`` must still reach the library."""

    @pytest.mark.parametrize(
        "page_type",
        [PAGE_TYPE_AGENT_PATTERN, PAGE_TYPE_AGENT_DISCIPLINE],
    )
    def test_library_types_excluded_without_the_tag(self, page_type):
        assert page_type in excluded_page_types()

    @pytest.mark.parametrize(
        "page_type",
        [PAGE_TYPE_AGENT_PATTERN, PAGE_TYPE_AGENT_DISCIPLINE],
    )
    def test_library_types_unlocked_by_the_tag(self, page_type):
        """Without this the entire agent-prompt library becomes unreachable."""
        assert page_type not in excluded_page_types(["agent-prompt"]), (
            f"{page_type} stayed excluded under tags=['agent-prompt'] — "
            "recall(type='wiki', tags=['agent-prompt']) would return NOTHING, "
            "breaking ADR-0007's documented lookup and every dispatch that reads it"
        )

    def test_toc_survives_every_subtraction(self):
        """``agent_index`` declares ``opt_in_tag=None`` — unconditional.

        It sits under a DIFFERENT policy object in the SAME tag family as the
        library, so a naive "subtract anything opted-in" loses the §1.4 fix.
        """
        for tags in (None, [], ["agent-prompt"], ["agent-prompt-toc"], ["rollup"]):
            assert PAGE_TYPE_AGENT_INDEX in excluded_page_types(tags), (
                f"the TOC leaked back into recall under tags={tags!r}"
            )

    def test_task_list_survives_every_subtraction(self):
        """``task_list`` also declares ``opt_in_tag=None``."""
        for tags in (None, [], ["agent-prompt"], ["task"], ["rollup"]):
            assert PAGE_TYPE_TASK_LIST in excluded_page_types(tags)

    def test_rollup_unlocked_only_by_its_own_key(self):
        assert "wiki_rollup" in excluded_page_types(["agent-prompt"])
        assert "wiki_rollup" not in excluded_page_types(["rollup"])

    def test_opt_in_does_not_unlock_unrelated_types(self):
        """``rollup`` must not unlock the library, nor vice versa."""
        unlocked_by_rollup = excluded_page_types() - excluded_page_types(["rollup"])
        assert unlocked_by_rollup == {"wiki_rollup"}, (
            f"opting into 'rollup' unlocked more than the rollup type: {unlocked_by_rollup}"
        )


# ── 5. The composed clause ───────────────────────────────────────────────────


class TestComposedRecallScopeClause:
    """One clause carrying all three things."""

    def test_all_three_arms_present_for_wiki(self):
        sql, params = build_recall_scope_clause(_PROJECT)
        assert "project_id" in sql
        assert "IN tags" in sql
        assert "page_type" in sql
        assert " AND " in sql, f"arms must compose conjunctively: {sql!r}"
        assert set(params), "composed clause must bind params"

    def test_memory_variant_omits_the_page_type_arm(self):
        """The ``memory`` table has no ``page_type`` column."""
        sql, _ = build_recall_scope_clause(_PROJECT, page_types=False)
        assert "page_type" not in sql, (
            f"memory rows have no page_type column — this clause would error: {sql!r}"
        )
        assert "project_id" in sql and "IN tags" in sql

    def test_opt_in_tags_flow_through_the_composed_clause(self):
        plain = build_recall_scope_clause(_PROJECT)[1]
        opted = build_recall_scope_clause(_PROJECT, opt_in_tags=["agent-prompt"])[1]
        assert _excluded_from(plain) != _excluded_from(opted), (
            "opt_in_tags did not reach the derived exclusion through the composed "
            "clause — the library lookup breaks even though the unit arm passes"
        )

    def test_no_param_name_collision_between_arms(self):
        sql, params = build_recall_scope_clause(_PROJECT, prefix="zz")
        for key in params:
            assert f"${key}" in sql, f"bound param {key!r} unused in SQL: {sql!r}"
        assert len(params) == len(set(params)), "duplicate param names"


def _excluded_from(params: dict) -> list:
    """Pull the excluded-page_type list out of a params dict, whatever its key."""
    for key, value in params.items():
        if key.endswith("_excl_types"):
            return value
    return []
