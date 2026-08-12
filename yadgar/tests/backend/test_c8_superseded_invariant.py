"""Car C8 (0047 §5 C8) — the SQL-status ↔ recall-exclusion invariant.

WHY THIS INVARIANT HAD TO BE BUILT ALONGSIDE THE MECHANISM

C8 makes recall depend on a set loaded out-of-band from a *different engine*.
When that set is stale, short, or empty, recall does not raise, does not come
back empty, and does not look wrong — superseded ADRs simply rank normally
again. Every symptom this repo has learned to watch for is absent. So the
mechanism ships with a check that can SEE the disagreement, wired into
``REQUIRED_CHECKS`` so it runs nightly rather than only under pytest.

WHAT MAKES IT NON-TAUTOLOGICAL

The check writes its OWN ``SELECT ... WHERE status='superseded'`` and compares
that against what the PRODUCTION path binds — loader → ``RecallScope`` →
``clause()``. A check that called the loader on both sides would agree with
itself for every bug the loader can have. The tests below therefore drive the
two sides APART on purpose: a loader that returns nothing, a loader that
returns the wrong slug, and a row the loader cannot possibly cover.
"""

from __future__ import annotations

import pytest

from yadgar.backend.admin_exec import invariants_cross_engine as ce

_PROJECT = "m-agahi/yadgar"


class _FakeEngine:
    """Engine #2 double with the two accessors DRIVEN APART on purpose.

    ``rows`` feeds ``list_superseded_adr_rows`` — the check's own independent
    read. ``loader_rows`` feeds ``list_adr_rows`` — what the production loader
    sees. Every disagreement below is staged by passing different values for
    the two, which is exactly what a self-comparing check could never detect.
    """

    def __init__(self, rows, loader_rows=None, tables=("adr",)):
        self._rows = rows
        self._loader_rows = rows if loader_rows is None else loader_rows
        self._tables = list(tables)

    async def list_tables(self):
        return list(self._tables)

    async def list_superseded_adr_rows(self):
        return list(self._rows)

    async def list_adr_rows(self, *, project_id, status=None, **_kw):
        return [
            r
            for r in self._loader_rows
            if str(r.get("project_id")) == project_id
            and (status is None or r.get("status", "superseded") == status)
        ]


def _row(adr_id, slug, project_id=_PROJECT):
    return {"project_id": project_id, "id": adr_id, "body_slug": slug}


# ── membership: without this the check exists but never runs ─────────────────


class TestWiredIntoTheNightlyArm:
    def test_check_is_a_required_member(self):
        assert ce.CHECK_SUPERSEDED_ADR_EXCLUSION in ce.REQUIRED_CHECKS

    def test_check_is_in_the_registry(self):
        """``REQUIRED_CHECKS`` alone would make the arm report a violation.

        Membership without a registry entry is the "never reported" case
        ``_aggregate`` turns into a violation — the check would be loud but
        useless. Both halves are needed.
        """
        assert ce.CHECK_SUPERSEDED_ADR_EXCLUSION in {name for name, _run in ce._CHECK_REGISTRY}


# ── agreement ────────────────────────────────────────────────────────────────


class TestAgreementIsOk:
    @pytest.mark.asyncio
    async def test_matching_sets_report_ok_with_evidence(self):
        rows = [_row(114, "yadgar_adr-0114"), _row(196, "yadgar_adr-0196")]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows))
        assert out["status"] == ce.STATUS_OK, out
        assert out["detail"]["superseded_rows"] == 2
        assert out["detail"]["projects"] == [_PROJECT]

    @pytest.mark.asyncio
    async def test_zero_superseded_rows_is_ok_but_carries_the_count(self):
        """``ok`` must be backed by a number, never by silence."""
        out = await ce.check_superseded_adr_exclusion(_FakeEngine([]))
        assert out["status"] == ce.STATUS_OK
        assert out["detail"]["superseded_rows"] == 0

    @pytest.mark.asyncio
    async def test_multiple_projects_are_each_checked(self):
        rows = [_row(1, "a-adr-0001"), _row(2, "b-adr-0002", project_id="m-agahi/other")]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows))
        assert out["status"] == ce.STATUS_OK
        assert out["detail"]["projects"] == ["m-agahi/other", _PROJECT]


# ── the failures the mechanism can produce invisibly ─────────────────────────


class TestDisagreementFiresLoudly:
    @pytest.mark.asyncio
    async def test_silently_empty_loader_is_a_violation(self):
        """THE named failure mode: SQL has superseded rows, the clause binds none.

        Nothing else in the system notices this — recall keeps returning
        results and the superseded ADRs are simply back in the ranking.
        """
        rows = [_row(114, "yadgar_adr-0114"), _row(196, "yadgar_adr-0196")]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows, loader_rows=[]))
        assert out["status"] == ce.STATUS_VIOLATION, out
        assert "2 superseded page(s)" in out["message"]
        assert "binds 0" in out["message"]
        assert "yadgar_adr-0114" in out["message"], "the message must name what is missing"

    @pytest.mark.asyncio
    async def test_stale_loader_set_is_a_violation(self):
        """A slug the ledger no longer says is superseded, and one it now does."""
        rows = [_row(114, "yadgar_adr-0114")]
        stale = [_row(9, "yadgar_adr-0009")]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows, loader_rows=stale))
        assert out["status"] == ce.STATUS_VIOLATION
        assert "yadgar_adr-0114" in out["message"]
        assert "yadgar_adr-0009" in out["message"]

    @pytest.mark.asyncio
    async def test_partial_loader_set_is_a_violation(self):
        rows = [_row(114, "yadgar_adr-0114"), _row(196, "yadgar_adr-0196")]
        partial = [_row(114, "yadgar_adr-0114")]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows, loader_rows=partial))
        assert out["status"] == ce.STATUS_VIOLATION
        assert "yadgar_adr-0196" in out["message"]

    @pytest.mark.asyncio
    async def test_unstamped_body_slug_is_a_violation(self):
        """``body_slug`` is nullable — such a row cannot be excluded BY anything.

        The loader is right to skip it (there is no slug to bind); the check is
        the layer that can say the row *should* be excludable and is not.
        """
        rows = [_row(114, None)]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows))
        assert out["status"] == ce.STATUS_VIOLATION
        assert "no body_slug" in out["message"]
        assert "114" in out["message"], "the message must name the adr id to fix"

    @pytest.mark.asyncio
    async def test_a_dropped_field_on_the_scope_hop_is_a_violation(self, monkeypatch):
        """SABOTAGE TEST — the reason assertion (b) walks the real hops.

        The first cut of this check re-built the ``RecallScope`` itself. With
        ``excluded_slugs`` deleted from ``WikiProvider``'s conversion, the whole
        invariant suite stayed GREEN while production excluded nothing — the
        exact vacuous pass ADR-0195's arm exists to eliminate. The check now
        walks ``Scope.to_recall_scope`` (the conversion the provider uses), so
        breaking that conversion breaks this test.
        """
        from yadgar.backend.retrieval.providers.base import Scope

        monkeypatch.setattr(
            Scope,
            "to_recall_scope",
            lambda self, opt_in: __import__(
                "yadgar._shared.storage.directory", fromlist=["RecallScope"]
            ).RecallScope(project_id=self.project_id or None, opt_in_tags=opt_in),
        )
        rows = [_row(114, "yadgar_adr-0114")]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows))
        assert out["status"] == ce.STATUS_VIOLATION, (
            "the Scope→RecallScope hop dropped excluded_slugs and the invariant "
            "did not notice — it is comparing itself against itself again"
        )
        assert "yadgar_adr-0114" in out["message"]

    @pytest.mark.asyncio
    async def test_a_deleted_where_arm_is_a_violation(self, monkeypatch):
        """(c) emission: a refactor that drops the arm must not pass silently."""
        from yadgar._shared.storage import directory as _dir

        monkeypatch.setattr(_dir, "build_slug_exclusion_clause", lambda *_a, **_k: ("", {}))
        rows = [_row(114, "yadgar_adr-0114")]
        out = await ce.check_superseded_adr_exclusion(_FakeEngine(rows))
        assert out["status"] == ce.STATUS_VIOLATION
        assert "binds 0" in out["message"] or "no slug-exclusion arm" in out["message"]


# ── absence is never ok ──────────────────────────────────────────────────────


class TestAbsenceIsUnavailableNotOk:
    @pytest.mark.asyncio
    async def test_engine_absent(self):
        out = await ce.check_superseded_adr_exclusion(None)
        assert out["status"] == ce.STATUS_UNAVAILABLE
        assert out["reason"] == ce.REASON_ENGINE_TWO_ABSENT

    @pytest.mark.asyncio
    async def test_adr_table_absent(self):
        out = await ce.check_superseded_adr_exclusion(_FakeEngine([], tables=("config",)))
        assert out["status"] == ce.STATUS_UNAVAILABLE
        assert out["reason"] == ce.REASON_ADR_TABLE_ABSENT

    @pytest.mark.asyncio
    async def test_read_failure(self):
        class _Boom(_FakeEngine):
            async def list_tables(self):
                raise RuntimeError("connection reset")

        out = await ce.check_superseded_adr_exclusion(_Boom([]))
        assert out["status"] == ce.STATUS_UNAVAILABLE
        assert out["reason"] == ce.REASON_QUERY_FAILED


# ── the loader itself ────────────────────────────────────────────────────────


class TestLoaderFailsLoud:
    @pytest.mark.asyncio
    async def test_engine_absent_warns_rather_than_failing_silently(self, caplog):
        from yadgar.backend.retrieval.superseded import load_superseded_slugs

        with caplog.at_level("WARNING"):
            assert await load_superseded_slugs(None, project_id=_PROJECT) == ()
        assert any("INACTIVE" in r.message for r in caplog.records), (
            "an empty exclusion set is invisible at the call site — it MUST be logged"
        )

    @pytest.mark.asyncio
    async def test_read_failure_warns_and_returns_empty(self, caplog):
        from yadgar.backend.retrieval.superseded import load_superseded_slugs

        class _Boom(_FakeEngine):
            async def list_adr_rows(self, **_kw):
                raise RuntimeError("gone")

        with caplog.at_level("WARNING"):
            assert await load_superseded_slugs(_Boom([]), project_id=_PROJECT) == ()
        assert any("INACTIVE" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_returns_sorted_slugs(self):
        from yadgar.backend.retrieval.superseded import load_superseded_slugs

        rows = [_row(2, "z-adr-0002"), _row(1, "a-adr-0001")]
        got = await load_superseded_slugs(_FakeEngine(rows), project_id=_PROJECT)
        assert got == ("a-adr-0001", "z-adr-0002")


# ── reachability is preserved (GREEN-unchanged) ──────────────────────────────


class TestReachabilityIsNotTouched:
    """ADR-0206/ADR-0228: removal is from RECALL, never from REACHABILITY.

    ``adr_get`` resolves the body page by EXACT SLUG through ``wiki_read`` —
    it never enters the recall pipeline, so no scope clause and no exclusion
    set is consulted. Pinning it structurally (rather than only behaviourally)
    is what stops a later car from "unifying" the read paths and quietly making
    superseded ADRs unreachable.
    """

    def test_adr_get_body_fetch_is_an_exact_slug_read(self):
        import ast
        import inspect

        from yadgar.core.server.tools import adr as adr_mod

        source = inspect.getsource(adr_mod._fetch_adr_body_page)
        calls = {
            node.func.id
            for node in ast.walk(ast.parse(source.lstrip()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "wiki_read" in calls, "adr_get's body fetch must stay an exact-slug read"

    def test_adr_module_never_consults_the_recall_exclusion(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        source = (root / "yadgar/core/server/tools/adr.py").read_text(encoding="utf-8")
        for token in ("load_superseded_slugs", "excluded_slugs", "build_recall_scope_clause"):
            assert token not in source, (
                f"adr.py references {token!r} — exclusion must never reach the "
                "key-fetch path (ADR-0206 reachability, reaffirmed by ADR-0228)"
            )
