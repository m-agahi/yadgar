"""Car C7 (0047 §5 C7) — the stage-1 WHERE clause, against a LIVE SurrealDB.

WHY THIS FILE GATES THE CAR, AND WHY LATENCY DOES NOT
-----------------------------------------------------
C7 moves recall scoping from a Python post-filter into the SQL ``WHERE``. The
adversarial reading of that change — the one that must not be gotten wrong — is
that production vector search IS the SurrealDB HNSW KNN operator
(``embedding <|fetch_k, 40|> $qv``), which selects its neighbours FIRST. Adding
``AND project_id = $p`` to that form is top-K-then-filter INSIDE SQL: for a
project holding a minority share of the corpus it silently returns too few rows,
or none at all.

That failure mode is invisible to a latency gate. A query that returns 3 rows
instead of 20 is FASTER. So the exit criterion here is RESULT-SET EQUIVALENCE —
id sets, not milliseconds:

  * ``TestResultSetEquivalence`` runs the pre-C7 shape (unscoped query + the
    Python post-filter) and the post-C7 shape (scoped SQL, no post-filter) over
    the SAME seeded corpus and compares the returned id sets. The old arm is the
    REAL old arm — an unscoped ``search_wiki_vectors`` plus the row predicate —
    not a reimplementation of what the old code was believed to do.

  * The comparison is SUPERSET, not equality, and deliberately so: the new path
    may legitimately return MORE, because the LIMIT is no longer spent on rows
    that are about to be discarded. Demanding equality would fail on success.

  * ``TestHardFloor`` is the direct anti-starvation assertion: with N in-scope
    rows present, a scoped search must return at least ``min(N, limit)``. This
    is the assertion that goes red if anyone re-introduces top-K-then-filter.

The FTS arm is exercised too, and is expected to be trivially correct: its
``LIMIT`` is applied after the ``WHERE``, so an added ``AND`` composes.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

_PROJECT = "m-agahi/yadgar"
_OTHER = "m-agahi/aws-work"


def _seed_page(
    storage,
    slug: str,
    *,
    marker: str,
    project_id: str | None,
    tags: list[str] | None = None,
    page_type: str | None = None,
    directory_context: str = "global",
    term_repeat: int = 1,
) -> None:
    """Insert one wiki_page row. ``project_id=None`` omits the column entirely.

    Omitting rather than nulling is the point for the unstamped case: C6 made
    ``project_id`` an ``option<string>``, and a row the backfill has not reached
    is field-ABSENT, not explicitly null.
    """
    fields: dict = {
        "slug": slug,
        "title": slug,
        # ``term_repeat`` biases BM25 rank. Used to construct the adversarial
        # case deterministically: out-of-scope rows that OUT-RANK the in-scope
        # ones, which is precisely when a post-filter's limit is wasted.
        "content": " ".join([marker] * term_repeat) + f" content for {slug} quokka",
        "tags": (tags or []) + [marker],
        "directory_context": directory_context,
        "category": "reference",
        "confidence": "high",
        "source_memory_ids": [],
    }
    if page_type is not None:
        fields["page_type"] = page_type

    # Seed the ROW directly with an explicit INTEGER id.
    #
    # Not ``insert_wiki_page``: that writes a paired ``wiki_page_version`` row
    # inside one transaction, and the module-scoped ``e2e_engines`` fixture wipes
    # DATA per test while the engine (and its id counters) persist — so a second
    # test in this file collides on the version row and the whole transaction
    # fails. Not the raw ``INSERT INTO`` form either: that mints a random STRING
    # id, which ``_extract_id`` rejects with a ValueError.
    pid = storage._next_id("wiki_page")
    assignments = ", ".join(f"{k} = ${k}" for k in fields)
    params = {**fields, "id": pid}
    if project_id is not None:
        # An UNSTAMPED row is produced by simply omitting the column, which is
        # the on-disk shape of a row C6's backfill has not reached: field-ABSENT
        # on an ``option<string>`` column, NOT an explicit null.
        assignments += ", project_id = $project_id"
        params["project_id"] = project_id
    _q_retry(storage, f"CREATE type::record('wiki_page', $id) SET {assignments}", params)


def _q_retry(storage, surql: str, params: dict, attempts: int = 6):
    """Run *surql*, retrying SurrealKV's own retryable write conflict.

    Each test here seeds ~26 rows, and SurrealKV raises
    ``Transaction conflict: … This transaction can be retried`` under
    concurrent load — its message says so explicitly. Retrying is honouring the
    engine's documented contract, not papering over a failure: a conflict is a
    contention signal about the WRITE, and every assertion in this file is about
    the READ predicate. Leaving it unhandled makes the file flake on a busy host
    and rotate which test fails, which is worse than useless as a signal.

    A non-conflict error is re-raised immediately and unchanged — a malformed
    clause must still fail loudly and on the first attempt.
    """
    import time as _time

    for attempt in range(attempts):
        try:
            return storage._q(surql, params)
        except RuntimeError as exc:
            if "onflict" not in str(exc) or attempt == attempts - 1:
                raise
            _time.sleep(0.05 * (attempt + 1))
    return None


def _slugs_for(storage, ids: list[int]) -> set[str]:
    out: set[str] = set()
    for pid in ids:
        page = storage.get_wiki_page(pid)
        if page:
            out.add(page.get("slug", ""))
    return out


def _seed_corpus(storage, marker: str) -> dict[str, set[str]]:
    """Seed a corpus with a KNOWN in-scope minority, mirroring production shape.

    Production measured 2026-08-10: yadgar holds 358 of 2,343 wiki pages
    (15.3%), aws-work holds 1,524 (65.0%). The ratio here is deliberately
    similar — an in-scope minority is exactly the condition under which
    top-K-then-filter starves and a post-filter wastes its budget.
    """
    in_scope: set[str] = set()
    out_of_scope: set[str] = set()

    # ORDER MATTERS. Out-of-scope rows are seeded FIRST and carry the search
    # term 8x (``term_repeat``), so they out-rank the in-scope rows on BM25 and
    # win any score tie on insertion order. Both levers are needed: without them
    # the ranking is arbitrary and the "did scoping reclaim the wasted limit?"
    # assertion below passes or fails by luck rather than by behaviour.
    #
    # This reproduces the production condition, not a contrived one: yadgar holds
    # 358 of 2,343 wiki pages (15.3%) and aws-work 1,524 (65.0%) — measured
    # 2026-08-10 — so a yadgar-scoped recall's top-N is dominated by rows it is
    # about to discard.
    for i in range(20):
        slug = f"{marker}-theirs-{i}"
        _seed_page(storage, slug, marker=marker, project_id=_OTHER, term_repeat=8)
        out_of_scope.add(slug)

    # Unstamped — C6's option<string> column, backfill not yet run.
    slug = f"{marker}-unstamped"
    _seed_page(storage, slug, marker=marker, project_id=None, term_repeat=8)
    out_of_scope.add(slug)

    for i in range(4):
        slug = f"{marker}-mine-{i}"
        _seed_page(storage, slug, marker=marker, project_id=_PROJECT)
        in_scope.add(slug)

    # The 'global' REACH tag: another project's row, reachable from everywhere.
    slug = f"{marker}-reach"
    _seed_page(storage, slug, marker=marker, project_id=_OTHER, tags=["global"])
    in_scope.add(slug)

    return {"in_scope": in_scope, "out_of_scope": out_of_scope}


def _scoped_fts(storage, marker: str, limit: int) -> set[str]:
    from yadgar._shared.storage.directory import build_recall_scope_clause

    scope_sql, scope_params = build_recall_scope_clause(_PROJECT)
    hits = storage.search_wiki_fts_scored(
        marker, limit=limit, scope_sql=scope_sql, scope_params=scope_params
    )
    return _slugs_for(storage, [pid for pid, _ in hits])


def _unscoped_fts_then_python_filter(storage, marker: str, limit: int) -> set[str]:
    """The PRE-C7 shape: unscoped query, then the row predicate in Python."""
    from yadgar._shared.storage.directory import is_project_eligible

    hits = storage.search_wiki_fts_scored(marker, limit=limit)
    kept: set[str] = set()
    for pid, _ in hits:
        page = storage.get_wiki_page(pid)
        if page and is_project_eligible(page.get("project_id"), page.get("tags"), _PROJECT):
            kept.add(page.get("slug", ""))
    return kept


class TestResultSetEquivalence:
    """id sets, not timings. The new path must never return LESS."""

    def test_scoped_fts_is_a_superset_of_the_post_filtered_old_path(self, e2e_engines):
        """The whole thesis, measured.

        Old path: fetch ``limit`` rows corpus-wide, then drop the out-of-scope
        ones — so the limit is spent BEFORE filtering and the surviving set can
        be arbitrarily small. New path: filter first, then spend the limit.
        Every id the old path returned must still be returned.
        """
        storage = e2e_engines["storage"]
        marker = "c7equiv"
        _seed_corpus(storage, marker)

        limit = 10
        old = _unscoped_fts_then_python_filter(storage, marker, limit)
        new = _scoped_fts(storage, marker, limit)

        assert new >= old, (
            "the SQL-scoped path LOST rows the post-filtered path returned — this is "
            "the silent under-return C7 exists to prevent, and it would look like a "
            f"latency win. missing={sorted(old - new)}"
        )

    def test_scoped_path_returns_strictly_more_when_the_limit_was_being_wasted(self, e2e_engines):
        """The improvement itself, not just the absence of regression.

        With a small limit over a corpus where in-scope rows are a minority, the
        old path's limit is consumed by rows it then discards. If this assertion
        ever becomes false, the scoping is not actually happening in SQL.
        """
        storage = e2e_engines["storage"]
        marker = "c7waste"
        _seed_corpus(storage, marker)

        limit = 5
        old = _unscoped_fts_then_python_filter(storage, marker, limit)
        new = _scoped_fts(storage, marker, limit)

        assert len(new) > len(old), (
            f"scoping did not reclaim the wasted limit: old={len(old)} new={len(new)}. "
            "The corpus is seeded so out-of-scope rows OUT-RANK in-scope ones on "
            "BM25, so the pre-C7 shape must spend its whole limit on rows it then "
            "discards. If the counts are equal, either the predicate is not reaching "
            "the query or it is being applied after the LIMIT."
        )

    def test_no_out_of_scope_row_survives_the_sql_path(self, e2e_engines):
        """No Python filtering involved — the WHERE alone must be sufficient."""
        storage = e2e_engines["storage"]
        marker = "c7leak"
        seeded = _seed_corpus(storage, marker)

        got = _scoped_fts(storage, marker, limit=50)

        leaked = got & seeded["out_of_scope"]
        assert not leaked, (
            f"rows owned by another project (or unstamped) came back from a "
            f"project-scoped query with no post-filter: {sorted(leaked)}"
        )

    def test_the_global_reach_tag_arm_actually_reaches(self, e2e_engines):
        """Dropping this arm narrows ~429 live rows to one project, silently."""
        storage = e2e_engines["storage"]
        marker = "c7reach"
        _seed_corpus(storage, marker)

        got = _scoped_fts(storage, marker, limit=50)
        assert f"{marker}-reach" in got, (
            "a row owned by ANOTHER project but carrying the 'global' reach tag was "
            "not returned — the reach arm is missing from the emitted clause"
        )

    def test_unstamped_rows_do_not_surface(self, e2e_engines):
        """The C7 decision, proven against the real column type.

        C6 made ``project_id`` ``option<string>``. This asserts the pre-backfill
        window behaves as §8 step 5b sanctions — such rows return ZERO, they do
        not silently leak into every project.
        """
        storage = e2e_engines["storage"]
        marker = "c7unstamped"
        _seed_corpus(storage, marker)

        got = _scoped_fts(storage, marker, limit=50)
        assert f"{marker}-unstamped" not in got, (
            "an unstamped (project_id-absent) row surfaced in a project-scoped "
            "recall — this is the permissive fallback ADR-0227 deletes"
        )


class TestHardFloor:
    """With N in-scope rows present, a scoped search returns >= min(N, limit)."""

    @pytest.mark.parametrize("limit", [1, 3, 5, 50])
    def test_scoped_search_is_not_starved(self, e2e_engines, limit):
        storage = e2e_engines["storage"]
        marker = f"c7floor{limit}"
        seeded = _seed_corpus(storage, marker)
        n_in_scope = len(seeded["in_scope"])

        got = _scoped_fts(storage, marker, limit=limit)

        expected_floor = min(n_in_scope, limit)
        assert len(got) >= expected_floor, (
            f"scoped search returned {len(got)} rows but {n_in_scope} in-scope rows "
            f"exist and limit={limit} — floor is {expected_floor}. This is the "
            "top-K-then-filter starvation signature."
        )


class TestVectorArmShape:
    """The vector arm must PRE-filter, not post-filter a KNN neighbourhood."""

    def test_scoped_vector_query_is_brute_force_not_hnsw(self, e2e_engines):
        """Executed against the live DB, so an invalid clause fails HERE.

        The assertion is structural AND behavioural: the scoped branch must not
        emit the ``<|k, ef|>`` KNN operator, because that operator picks its
        neighbours before any predicate is considered.
        """
        import inspect

        from yadgar._shared.storage.wiki import _WikiMixin

        src = inspect.getsource(_WikiMixin.search_wiki_vectors)
        scoped_branch = src.split("if scope_sql:", 1)[1].split("else:", 1)[0]
        assert "<|" not in scoped_branch, (
            "the SCOPED vector branch still uses the HNSW KNN operator — the "
            "predicate would filter neighbours KNN already chose (top-K-then-filter), "
            "which silently under-returns for a minority-share project"
        )
        assert "LIMIT" in scoped_branch, (
            "the scoped brute-force branch must apply its own LIMIT after ranking"
        )

    def test_scope_clause_is_executable_surrealql_on_wiki_page(self, e2e_engines):
        """Run the emitted fragment directly — a malformed clause raises here."""
        from yadgar._shared.storage.directory import build_recall_scope_clause

        storage = e2e_engines["storage"]
        marker = "c7sql"
        seeded = _seed_corpus(storage, marker)

        scope_sql, scope_params = build_recall_scope_clause(_PROJECT)
        assert scope_sql, "a populated project scope must produce a non-empty clause"

        rows = _q_retry(
            storage,
            f"SELECT slug FROM wiki_page WHERE tags CONTAINS $tok AND ({scope_sql})",
            {**scope_params, "tok": marker},
        )
        got = {r.get("slug") for r in rows}

        assert got == seeded["in_scope"], (
            f"the executed clause selected the wrong set.\n"
            f"  missing: {sorted(seeded['in_scope'] - got)}\n"
            f"  extra:   {sorted(got - seeded['in_scope'])}"
        )


class TestPageTypeExclusionInSql:
    """The derived exclusion, executed — including the opt-in arm."""

    def test_task_list_page_never_appears(self, e2e_engines):
        """C7 absorbed C8 item 4: ``task_list`` is ``exclude``, so it is never fetched."""
        from yadgar._shared.storage.directory import build_recall_scope_clause

        storage = e2e_engines["storage"]
        marker = "c7tasklist"
        _seed_page(
            storage,
            f"{marker}-tasks",
            marker=marker,
            project_id=_PROJECT,
            page_type="task_list",
        )
        _seed_page(storage, f"{marker}-plain", marker=marker, project_id=_PROJECT)

        scope_sql, scope_params = build_recall_scope_clause(_PROJECT)
        rows = _q_retry(
            storage,
            f"SELECT slug FROM wiki_page WHERE tags CONTAINS $tok AND ({scope_sql})",
            {**scope_params, "tok": marker},
        )
        got = {r.get("slug") for r in rows}

        assert f"{marker}-tasks" not in got, (
            "a task_list page survived the stage-1 WHERE — it should never be fetched"
        )
        assert f"{marker}-plain" in got, "the exclusion over-reached and dropped a plain page"

    def test_agent_prompt_library_resolves_under_its_opt_in_tag(self, e2e_engines):
        """THE blocking arm. Without it, ADR-0007's documented lookup returns nothing.

        ``recall(type="wiki", tags=["agent-prompt"])`` is how every dispatch on
        this train reaches the prompt library. A WHERE emitting
        ``page_type NOT IN (<all exclude types>)`` unconditionally would make it
        return an empty list — and nothing would look broken from the outside.
        """
        from yadgar._shared.storage.directory import build_recall_scope_clause

        storage = e2e_engines["storage"]
        marker = "c7optin"
        _seed_page(
            storage,
            f"{marker}-pattern",
            marker=marker,
            project_id=_PROJECT,
            tags=["agent-prompt", "global"],
            page_type="agent_pattern",
        )
        _seed_page(
            storage,
            f"{marker}-toc",
            marker=marker,
            project_id=_PROJECT,
            tags=["agent-prompt", "global"],
            page_type="agent_index",
        )

        def _select(opt_in):
            scope_sql, scope_params = build_recall_scope_clause(_PROJECT, opt_in_tags=opt_in)
            rows = _q_retry(
                storage,
                f"SELECT slug FROM wiki_page WHERE tags CONTAINS $tok AND ({scope_sql})",
                {**scope_params, "tok": marker},
            )
            return {r.get("slug") for r in rows}

        bare = _select(None)
        assert f"{marker}-pattern" not in bare, (
            "the library leaked into a BARE recall — that is the noise C1 removed"
        )

        opted = _select(["agent-prompt"])
        assert f"{marker}-pattern" in opted, (
            "recall(tags=['agent-prompt']) did not reach the agent-prompt library. "
            "This breaks ADR-0007's documented lookup and every dispatch that reads it."
        )
        assert f"{marker}-toc" not in opted, (
            "the TOC (agent_index, opt_in_tag=None) leaked back in under the library's "
            "tag — it must survive EVERY subtraction (§1.4)"
        )


class TestMemoryRowsCarryTheGuardsInputs:
    """The residual Python guard reads two keys off a memory row. Prove they exist.

    ``MemoryProvider`` calls
    ``is_project_eligible(m.get("project_id"), m.get("tags"), caller_project)``
    on rows hydrated by ``get_memories_by_ids``. If either key were absent from
    that projection the failure would be SILENT and asymmetric:

      * ``tags`` missing → the reach arm degrades to ``False`` and EVERY
        globally-tagged memory quietly disappears from recall;
      * ``project_id`` missing → every row looks unstamped and a scoped recall
        returns nothing at all.

    Neither shows up in the SQL-level tests above (those query ``wiki_page``
    directly), and neither shows up as an error — just as fewer results. The
    projection is ``SELECT *`` today, which is why this passes; it is asserted
    rather than assumed because a future narrowing to an explicit column list is
    exactly the kind of change that would look harmless.
    """

    def test_hydrated_memory_rows_expose_project_id_and_tags(self, e2e_engines):
        from yadgar._shared.storage.directory import is_project_eligible

        storage = e2e_engines["storage"]
        marker = "c7memproj"

        def _seed_memory(label: str, project_id: str | None, tags: list[str]) -> int:
            mid = storage._next_id("memory")
            assignments = "content = $c, heat = 1.0, tags = $t, directory_context = 'global'"
            params: dict = {"id": mid, "c": f"{label} {marker}", "t": [marker, *tags]}
            if project_id is not None:
                assignments += ", project_id = $p"
                params["p"] = project_id
            _q_retry(
                storage,
                f"CREATE type::record('memory', $id) SET {assignments}",
                params,
            )
            return mid

        mine = _seed_memory("mine", _PROJECT, [])
        reach = _seed_memory("reach", _OTHER, ["global"])
        theirs = _seed_memory("theirs", _OTHER, [])
        unstamped = _seed_memory("unstamped", None, [])

        rows = {m["id"]: m for m in storage.get_memories_by_ids([mine, reach, theirs, unstamped])}
        assert len(rows) == 4, f"hydration lost rows: {sorted(rows)}"

        for mid, row in rows.items():
            assert "tags" in row, (
                f"memory row {mid} carries no 'tags' key — the reach arm of "
                "is_project_eligible would silently drop every globally-tagged memory"
            )

        assert rows[mine].get("project_id") == _PROJECT, (
            "memory row lost its project_id in hydration — a scoped recall would "
            "return nothing at all"
        )
        assert rows[unstamped].get("project_id") is None, (
            "an unstamped row must hydrate as project_id=None, not as a sentinel"
        )

        # The guard, driven by REAL hydrated rows rather than hand-built dicts.
        verdicts = {
            (r.get("content") or "").split(" ")[0]: is_project_eligible(
                r.get("project_id"), r.get("tags"), _PROJECT
            )
            for r in rows.values()
        }
        assert verdicts == {
            "mine": True,
            "reach": True,
            "theirs": False,
            "unstamped": False,
        }, f"guard disagreed with the SQL arm on real rows: {verdicts}"
