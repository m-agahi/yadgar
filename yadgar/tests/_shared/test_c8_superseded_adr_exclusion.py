"""Car C8 (0047 §5 C8) — superseded ADRs excluded in the STAGE-1 WHERE.

THE DESIGN, so a future reader does not re-derive the two dead alternatives:

  * Status lives SOLELY in SQL (``adr.status``, migration 002). SurrealDB
    carries NOTHING about ADR status — the ``adr-status:*`` wiki tag that
    exists on pages today is exactly the second writer ADR-0206 rejects, and
    the spine retires it. ``TestNoSurrealStatusRead`` is the grep guard.
  * The superseded slug set is loaded ONCE, in the ASYNC route, BEFORE
    ``asyncio.to_thread(_run_pipeline)``, and travels into the pipeline as
    plain data. It is NOT a lookup keyed by returned candidates: filtering
    after the providers return is the exact defect C7 exists to delete.
  * It is injected as ``slug NOT IN $sc_excl_slugs`` in the SAME clause
    builder C7 constructed.

THE FAILURE MODE THIS FILE EXISTS TO CATCH is a SILENTLY-EMPTY exclusion set.
Recall still returns results — just the wrong ones — so nothing surfaces it.
Two dataclass hops can drop the field without any clause-level test noticing:

  * ``RecallScope.with_default_opt_in`` re-constructs the scope;
  * ``WikiProvider.candidates`` re-constructs it again.

``TestFieldSurvivesTheDataclassHops`` is therefore NOT a redundancy test. It is
the only thing standing between a green clause suite and a production path that
excludes nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from yadgar._shared.storage.directory import (
    RecallScope,
    build_recall_scope_clause,
    build_slug_exclusion_clause,
)

_PROJECT = "m-agahi/yadgar"
_SUPERSEDED = ("m-agahi_yadgar_adr-0114", "m-agahi_yadgar_adr-0196")


def _slug_param(params: dict) -> list | None:
    """Pull the excluded-slug list out of a params dict, whatever its key."""
    for key, value in params.items():
        if key.endswith("_excl_slugs"):
            return value
    return None


# ── 1. The slug-exclusion arm itself ─────────────────────────────────────────


class TestSlugExclusionArm:
    def test_arm_emits_a_bound_not_in_predicate(self):
        sql, params = build_slug_exclusion_clause(_SUPERSEDED)
        assert "slug" in sql, f"slug arm missing: {sql!r}"
        assert "NOT IN" in sql, f"arm must EXCLUDE, not select: {sql!r}"
        assert _slug_param(params) == list(_SUPERSEDED), (
            f"the slug set must be BOUND, not inlined into the SQL: {params}"
        )

    def test_empty_set_emits_nothing(self):
        """No superseded ADRs is a legitimate state, not a degenerate clause.

        ``slug NOT IN []`` would be a no-op predicate at best and a syntax
        error at worst; emitting nothing keeps the composed clause clean.
        """
        assert build_slug_exclusion_clause(()) == ("", {})
        assert build_slug_exclusion_clause(None) == ("", {})

    def test_slug_order_is_stable(self):
        """A set literal would make the emitted SQL non-deterministic.

        Two identical recalls must produce byte-identical SQL or the query
        plan cache (and every SQL-comparing test) becomes flaky.
        """
        first = build_slug_exclusion_clause({"b-slug", "a-slug"})[1]
        second = build_slug_exclusion_clause({"a-slug", "b-slug"})[1]
        assert _slug_param(first) == _slug_param(second) == ["a-slug", "b-slug"]


# ── 2. The composed clause ───────────────────────────────────────────────────


class TestComposedClauseCarriesTheSlugArm:
    def test_wiki_clause_carries_all_four_arms(self):
        sql, params = build_recall_scope_clause(_PROJECT, excluded_slugs=_SUPERSEDED)
        assert "project_id" in sql
        assert "IN tags" in sql
        assert "page_type" in sql
        assert "slug NOT IN" in sql, f"C8's slug arm missing from composed clause: {sql!r}"
        assert _slug_param(params) == list(_SUPERSEDED)

    def test_memory_variant_omits_the_slug_arm(self):
        """The ``memory`` table has no ``slug`` column — this clause would error."""
        sql, params = build_recall_scope_clause(
            _PROJECT, excluded_slugs=_SUPERSEDED, page_types=False
        )
        assert "slug" not in sql, f"memory rows have no slug column: {sql!r}"
        assert _slug_param(params) is None
        assert "project_id" in sql and "IN tags" in sql

    def test_slug_arm_survives_an_unscoped_read(self):
        """Exclusion is NOT conditional on a project predicate.

        A daemon-internal caller with no project id still must not surface
        superseded ADRs when a set was supplied; coupling the two arms would
        make the exclusion silently vanish on exactly those paths.
        """
        # Car H1 §1.3: the whole-corpus read this test is about is now an
        # EXPLICIT `unscoped=True` rather than a falsy project_id, which raises.
        # The test's premise is unchanged — the slug arm must survive a read
        # that carries no project predicate — only how that read is spelled.
        sql, params = build_recall_scope_clause(None, excluded_slugs=_SUPERSEDED, unscoped=True)
        assert "slug NOT IN" in sql
        assert _slug_param(params) == list(_SUPERSEDED)

    def test_no_param_name_collision_between_arms(self):
        sql, params = build_recall_scope_clause(
            _PROJECT, excluded_slugs=_SUPERSEDED, opt_in_tags=["rollup"], prefix="zz"
        )
        for key in params:
            assert f"${key}" in sql, f"bound param {key!r} unused in SQL: {sql!r}"
        assert len(params) == len(set(params)), "duplicate param names"


# ── 3. The two dataclass hops — the invisible-failure guard ──────────────────


class TestFieldSurvivesTheDataclassHops:
    """Both re-construct a ``RecallScope``; either can silently drop the field.

    If one does, every clause-level test above still passes while production
    excludes nothing — which is exactly the invisible failure C8 exists to
    prevent.
    """

    def test_recall_scope_clause_carries_excluded_slugs(self):
        sql, params = RecallScope(project_id=_PROJECT, excluded_slugs=_SUPERSEDED).clause()
        assert "slug NOT IN" in sql
        assert _slug_param(params) == list(_SUPERSEDED)

    def test_with_default_opt_in_preserves_excluded_slugs(self):
        """Hop 1: ``WikiStore.query`` calls this on every single recall.

        The pre-C8 body re-built the scope with an explicit two-field
        constructor. A third field added to the dataclass but not to that
        constructor is dropped here, on the hottest path in the read stack.
        """
        scoped = RecallScope(project_id=_PROJECT, excluded_slugs=_SUPERSEDED)
        widened = scoped.with_default_opt_in(["agent-prompt"])
        assert widened.excluded_slugs == _SUPERSEDED, (
            "with_default_opt_in dropped excluded_slugs — every recall that "
            "passes tags= would silently stop excluding superseded ADRs"
        )
        assert widened.opt_in_tags == ["agent-prompt"]

    def test_with_default_opt_in_is_identity_when_nothing_to_default(self):
        scoped = RecallScope(project_id=_PROJECT, excluded_slugs=_SUPERSEDED)
        assert scoped.with_default_opt_in(None) is scoped

    def test_wiki_provider_threads_excluded_slugs_into_the_store(self):
        """Hop 2: ``WikiProvider.candidates`` re-constructs the scope."""
        from yadgar.backend.retrieval.providers.base import Scope
        from yadgar.backend.retrieval.providers.wiki import WikiProvider

        captured: dict = {}

        class _FakeWiki:
            def query(self, _query, **kwargs):
                captured.update(kwargs)
                return []

        provider = WikiProvider(_FakeWiki())
        provider.candidates(
            "q",
            Scope(project_id=_PROJECT, excluded_slugs=_SUPERSEDED),
            limit=5,
        )
        assert captured["scope"].excluded_slugs == _SUPERSEDED, (
            "WikiProvider dropped excluded_slugs on the Scope→RecallScope hop"
        )


# ── 4. The store pushes it into stage 1, not into a post-filter ──────────────


class TestStorePushesItIntoStageOne:
    """The whole point of C8: superseded ADRs never consume a pool slot.

    Asserting on the emitted stage-1 SQL (not on the final ranking) is the
    difference between "excluded" and "ranked last" — a post-filter satisfies
    the second and re-creates the defect C7 deleted.
    """

    @pytest.fixture
    def store(self):
        from yadgar._shared.wiki.store import WikiStore

        return WikiStore(storage=object(), embeddings=object())

    def test_query_pushes_the_slug_exclusion_into_the_stage_one_clause(self, store):
        captured: dict = {}

        def _fake_dispatch(_q, _scores, _max, _include_tag, scope_sql="", scope_params=None):
            captured["sql"] = scope_sql
            captured["params"] = scope_params or {}

        store._collect_scores_dispatch = _fake_dispatch
        store.query(
            "adr",
            scope=RecallScope(project_id=_PROJECT, excluded_slugs=_SUPERSEDED),
        )
        assert "slug NOT IN" in captured["sql"], (
            f"WikiStore.query did not push C8's exclusion into stage 1: {captured['sql']!r}"
        )
        assert _slug_param(captured["params"]) == list(_SUPERSEDED)

    def test_query_still_pushes_it_when_the_caller_passed_tags(self, store):
        """``tags=`` routes through ``with_default_opt_in`` — hop 1, live."""
        captured: dict = {}

        def _fake_dispatch(_q, _scores, _max, _include_tag, scope_sql="", scope_params=None):
            captured["sql"] = scope_sql
            captured["params"] = scope_params or {}

        store._collect_scores_dispatch = _fake_dispatch
        store.query(
            "adr",
            tags=["agent-prompt"],
            scope=RecallScope(project_id=_PROJECT, excluded_slugs=_SUPERSEDED),
        )
        assert "slug NOT IN" in captured["sql"]
        assert _slug_param(captured["params"]) == list(_SUPERSEDED)


# ── 5. Status is never read from SurrealDB ───────────────────────────────────


_RECALL_PATH_MODULES = (
    "yadgar/_shared/storage/directory.py",
    "yadgar/_shared/wiki/store.py",
    "yadgar/_shared/wiki/policy.py",
    "yadgar/backend/retrieval/superseded.py",
    "yadgar/backend/retrieval/recall_pipeline.py",
    "yadgar/backend/retrieval/providers/wiki.py",
    "yadgar/backend/retrieval/providers/base.py",
    "yadgar/backend/embed_service/embed_service_routes.py",
)


class TestNoSurrealStatusRead:
    """The recall path must not resolve ADR status from SurrealDB.

    Scoped to the READ path deliberately: the ADR WRITE path legitimately still
    emits ``adr-status:*`` tags onto pages today (the spine retires them later),
    so a repo-wide grep would false-fire on correct code. The claim under test
    is narrower and is the one the user made: *the recall path* learns status
    from the SQL ledger and from nowhere else.
    """

    @pytest.mark.parametrize("rel", _RECALL_PATH_MODULES)
    def test_module_never_references_the_adr_status_tag(self, rel):
        root = Path(__file__).resolve().parents[3]
        source = (root / rel).read_text(encoding="utf-8")
        offenders = [
            line
            for line in source.splitlines()
            if "adr-status" in line and "adr-status" not in line.split("#", 1)[-1]
        ]
        assert not offenders, (
            f"{rel} references the adr-status wiki tag on the recall path — "
            f"status lives SOLELY in SQL (ADR-0206, ADR-0228): {offenders}"
        )

    def test_loader_reads_the_sql_ledger_and_nothing_else(self):
        """The loader's only data source is the engine-#2 ``adr`` table."""
        root = Path(__file__).resolve().parents[3]
        source = (root / "yadgar/backend/retrieval/superseded.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "list_adr_rows" in calls, (
            "the loader must read status from the SQL ledger (list_adr_rows)"
        )
        for surreal_call in ("_q", "query", "get_wiki_page_by_slug", "list_wiki_pages"):
            assert surreal_call not in calls, (
                f"the loader calls SurrealDB ({surreal_call}) — SurrealDB carries "
                "NOTHING about ADR status"
            )
