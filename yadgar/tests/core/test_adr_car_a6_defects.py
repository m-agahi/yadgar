"""Car A6 — two defects in ``yadgar/core/server/tools/adr.py``.

Ledger task 213 (defect 1): ``adr_add`` never stamped ``tier``, so every row it
wrote landed with ``tier=NULL``.  ``adr_list`` defaults to ``tier="binding"``
and forwards it verbatim, so a NULL-tier row matches NEITHER ``"binding"`` NOR
``"historical"`` — the row is unreachable through every argument value the tool
accepts.  ``adr.py``'s own docstring already promised the default ("binding
(default if None; inferred from status when None)") — the promise was never
implemented anywhere on the write path (core forwards verbatim, the backend
admin op forwards verbatim, the SQL layer inserts NULL).

D27 mapping (``docs/plans/task-table-refactor-2026-07-29.md:295``, and
already implemented for the one-shot backfill at
``yadgar/backend/admin_exec/seed_adr_tier_subsystem.py:57``):

    superseded | rejected | deprecated  -> historical
    open       | accepted               -> binding

Ledger task 214 (defect 2): ``adr_get`` cannot read a CROSS-PROJECT ADR's
prose.  The body read narrowed the wiki lookup to the CALLER'S directory
(``wiki_read(slug, directory=resolved, ...)``); ``wiki_read`` scopes on that
directory and NOT on ``project=`` (``wiki.py:923`` →
``read_by_directory(slug, _caller_dir)``), so a page whose
``directory_context`` belongs to another project never resolves.  The legacy
fallback slug compounded it by deriving from ``os.path.basename(directory)``.

The same-project direction WORKS today and must keep working — these tests pin
BOTH directions, because a one-direction test stays green through the whole
defect lifetime.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from yadgar.core.server.tools.adr_render import _VALID_STATUSES
from yadgar.tests.core.conftest import TEST_PROJECT_ID

# D27 (task 213): the status → tier mapping the fix must implement.
_D27_EXPECTED: dict[str, str] = {
    "open": "binding",
    "accepted": "binding",
    "superseded": "historical",
    "rejected": "historical",
    "deprecated": "historical",
}


def _adr_add_params(project_dir: str, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = dict(
        directory=project_dir,
        project=TEST_PROJECT_ID,
        title="Car A6 ADR",
        status="accepted",
        date="2026-08-19",
        context="Car A6.",
        decision="Derive tier from status.",
        rationale="D27.",
        alternatives="Hardcode binding — rejected, D27.",
        consequences="Rows reachable through adr_list.",
        revisit_trigger="D27 gains a status.",
        supersedes="none",
    )
    params.update(overrides)
    return params


# ── Defect 1 (task 213): adr_add derives tier from status ─────────────────────


class TestAdrAddDerivesTierFromStatus:
    """``adr_add`` must stamp a D27 ``tier`` on every row it writes.

    NON-VACUOUS BY CONSTRUCTION: the mapping is asserted per-status over the
    FULL ``_VALID_STATUSES`` enum, so a hardcoded ``"binding"`` reds on the
    three historical statuses and a hardcoded ``"historical"`` reds on the two
    binding ones.  Neither hardcode survives.
    """

    @pytest.mark.parametrize("status", sorted(_VALID_STATUSES))
    def test_tier_derived_from_status(self, tmp_path, status):
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / f"tier-{status}")
        os.makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
            return {"ok": True}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=_capture_forward,
            ),
            patch(
                "yadgar.core.server.tools.adr._wiki_write_canonical",
                return_value={"stored": True, "committed": True},
            ),
        ):
            result = adr_add(**_adr_add_params(project_dir, status=status))

        assert "error" not in result, f"unexpected error: {result.get('error')}"
        payload = next(c["payload"] for c in captured if c["op"] == "create_adr_row")
        assert payload.get("tier") == _D27_EXPECTED[status], (
            f"D27: status={status!r} must stamp tier={_D27_EXPECTED[status]!r}; "
            f"got {payload.get('tier')!r}"
        )

    def test_tier_is_never_null_or_empty(self, tmp_path):
        """The reachability property itself: no row may carry a falsy tier.

        A NULL/empty tier matches neither ``adr_list`` filter value, which is
        what made ADR-0255 invisible through every argument.
        """
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "tier-nonnull")
        os.makedirs(project_dir, exist_ok=True)
        seen: list[Any] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            if op == "create_adr_row":
                seen.append(payload.get("tier"))
                return {"row": {"id": 1, **payload}}
            return {"ok": True}

        for status in sorted(_VALID_STATUSES):
            with (
                patch(
                    "yadgar.core.server.tools.adr._resolve_project_root",
                    return_value=project_dir,
                ),
                patch(
                    "yadgar.core.server.tools.adr._forward_admin",
                    side_effect=_capture_forward,
                ),
                patch(
                    "yadgar.core.server.tools.adr._wiki_write_canonical",
                    return_value={"stored": True, "committed": True},
                ),
            ):
                adr_add(**_adr_add_params(project_dir, status=status))

        assert seen and all(t in {"binding", "historical"} for t in seen), (
            f"every row must carry a D27 tier; got {seen!r}"
        )

    @pytest.mark.parametrize(
        ("status", "explicit_tier"),
        [
            ("accepted", "historical"),  # binding-by-status, overridden down
            ("superseded", "binding"),  # historical-by-status, overridden up
        ],
    )
    def test_explicit_tier_wins_over_derivation(self, tmp_path, status, explicit_tier):
        """An explicitly supplied ``tier=`` must NOT be overwritten.

        Guards against a naive always-derive implementation: both cases here
        contradict the D27 derivation, so an unconditional derive reds.
        """
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / f"tier-explicit-{status}")
        os.makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
            return {"ok": True}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=_capture_forward,
            ),
            patch(
                "yadgar.core.server.tools.adr._wiki_write_canonical",
                return_value={"stored": True, "committed": True},
            ),
        ):
            adr_add(**_adr_add_params(project_dir, status=status, tier=explicit_tier))

        payload = next(c["payload"] for c in captured if c["op"] == "create_adr_row")
        assert payload.get("tier") == explicit_tier, (
            f"explicit tier={explicit_tier!r} must survive the derivation; "
            f"got {payload.get('tier')!r}"
        )


# ── Defect 2 (task 214): adr_get body read is project-keyed, not dir-keyed ────


_FLUX_PROJECT = "quinyx/flux"
_FLUX_BODY_SLUG = "quinyx_flux_adr-0016"


class _WikiReadRecorder:
    """Records every ``wiki_read`` call and serves ONLY the pages it holds.

    Serving by slug alone (no directory filter) is deliberate: it models the
    fact that the target page EXISTS — so a "not found" in these tests can only
    come from the caller asking for the wrong slug, and the ``directory``
    assertion is what catches the narrowing.
    """

    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def __call__(self, slug: str, directory: str | None = None, **kwargs: Any) -> dict:
        self.calls.append({"slug": slug, "directory": directory, **kwargs})
        page = self.pages.get(slug)
        if page is None:
            return {"error": f"Wiki page '{slug}' not found"}
        return dict(page)


def _row(project_id: str, body_slug: str | None) -> dict:
    return {
        "id": 16,
        "project_id": project_id,
        "title": "Tune cluster-autoscaler scale-down",
        "status": "open",
        "decided_on": "2026-08-17",
        "subsystem": "cluster-autoscaler",
        "tier": "binding",
        "body_slug": body_slug,
        "created_at": "2026-08-17T00:00:00",
        "updated_at": "2026-08-17T00:00:00",
    }


class TestAdrGetCrossProjectBody:
    """``adr_get(project=<other>)`` must reach the OTHER project's prose.

    Two directions, both pinned:
      * cross-project — the failing case (measured 2026-08-19: reading
        flux's ADR-0016 from the yadgar tree returned
        ``"Wiki page 'yadgar-adr-0016' not found"``);
      * same-project  — the live, common path that must NOT regress.
    """

    def test_cross_project_read_does_not_narrow_to_caller_directory(self, tmp_path):
        """The discriminating assertion: no body read may be scoped to the
        caller's own tree.  ``wiki_read`` resolves on ``directory``, NOT on
        ``project=`` (``wiki.py:923``), so a caller-directory narrowing makes
        another project's page structurally unreachable."""
        from yadgar.core.server.tools.adr import adr_get

        caller_dir = str(tmp_path / "yadgar")
        os.makedirs(caller_dir, exist_ok=True)
        recorder = _WikiReadRecorder(
            {
                _FLUX_BODY_SLUG: {
                    "content": "# ADR-0016: cluster-autoscaler\n",
                    "slug": _FLUX_BODY_SLUG,
                    "directory_context": "/home/max/quinyx/flux",
                    "tags": ["adr", "decisions", "adr-status:open", "adr-0016"],
                }
            }
        )

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=caller_dir,
            ),
            patch("yadgar.core.server.tools.adr.wiki_read", side_effect=recorder),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                return_value={"row": _row(_FLUX_PROJECT, _FLUX_BODY_SLUG)},
            ),
        ):
            result = adr_get(directory=caller_dir, adr_id="ADR-0016", project=_FLUX_PROJECT)

        assert recorder.calls, "expected at least one wiki_read"
        narrowed = [c for c in recorder.calls if c.get("directory") not in (None, "")]
        assert not narrowed, (
            "body read must not narrow to the caller's directory — "
            f"wiki_read resolves on directory, not project. Got: {narrowed!r}"
        )
        assert recorder.calls[0]["slug"] == _FLUX_BODY_SLUG, (
            f"first body read must use the project's slug scheme "
            f"(row body_slug={_FLUX_BODY_SLUG!r}); got {recorder.calls[0]['slug']!r}"
        )
        assert "error" not in result, f"cross-project prose unreachable: {result}"
        assert result.get("content", "").startswith("# ADR-0016"), (
            f"cross-project prose must be returned; got {result.get('content')!r}"
        )

    def test_caller_directory_basename_slug_is_never_requested(self, tmp_path):
        """No read may ask for ``<basename(caller_dir)>-adr-NNNN``.

        That legacy slug is derived from the WORKING DIRECTORY
        (``adr_index.adr_page_slug``), which is what produced the measured
        ``yadgar-adr-0016`` while reading a ``quinyx/flux`` ADR.
        """
        from yadgar.core.server.tools.adr import adr_get

        caller_dir = str(tmp_path / "yadgar")
        os.makedirs(caller_dir, exist_ok=True)
        # Serve NOTHING — every rung of the fallback chain is exercised.
        recorder = _WikiReadRecorder({})

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=caller_dir,
            ),
            patch("yadgar.core.server.tools.adr.wiki_read", side_effect=recorder),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                return_value={"row": _row(_FLUX_PROJECT, _FLUX_BODY_SLUG)},
            ),
        ):
            adr_get(directory=caller_dir, adr_id="ADR-0016", project=_FLUX_PROJECT)

        forbidden = "yadgar-adr-0016"
        assert all(c["slug"] != forbidden for c in recorder.calls), (
            f"a directory-derived slug was requested: {[c['slug'] for c in recorder.calls]!r}"
        )
        # Every slug tried must belong to the RESOLVED project.
        for call in recorder.calls:
            assert call["slug"] in {_FLUX_BODY_SLUG, "flux-adr-0016"}, (
                f"slug {call['slug']!r} does not belong to project {_FLUX_PROJECT!r}"
            )

    def test_same_project_read_still_resolves(self, tmp_path):
        """The direction that WORKS today and must not regress."""
        from yadgar.core.server.tools.adr import adr_get

        caller_dir = str(tmp_path / "flux")
        os.makedirs(caller_dir, exist_ok=True)
        recorder = _WikiReadRecorder(
            {
                _FLUX_BODY_SLUG: {
                    "content": "# ADR-0016: cluster-autoscaler\n",
                    "slug": _FLUX_BODY_SLUG,
                    "directory_context": caller_dir,
                    "tags": ["adr", "decisions", "adr-status:open", "adr-0016"],
                }
            }
        )

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=caller_dir,
            ),
            patch("yadgar.core.server.tools.adr.wiki_read", side_effect=recorder),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                return_value={"row": _row(_FLUX_PROJECT, _FLUX_BODY_SLUG)},
            ),
        ):
            result = adr_get(directory=caller_dir, adr_id="ADR-0016", project=_FLUX_PROJECT)

        assert "error" not in result, f"same-project read regressed: {result}"
        assert result.get("slug") == _FLUX_BODY_SLUG
        assert result.get("content", "").startswith("# ADR-0016")
        assert result.get("subsystem") == "cluster-autoscaler", "row metadata still merged (D5)"

    def test_body_slug_from_a_foreign_project_is_ignored(self, tmp_path):
        """Task-188 blast-radius guard.

        ``get_adr_row`` used to discard ``project_id`` (ledger task 188, fixed
        in Car B1), so a row for the WRONG project could come back.  Coupling
        the body read to ``row["body_slug"]`` would then serve another
        project's PROSE, not merely its metadata.  A ``body_slug`` inconsistent
        with the resolved project_id must be ignored in favour of the derived
        D32 ③ slug.

        KEPT AFTER THE B1 FIX, deliberately: the guard is now defence in depth
        rather than the only line, and it is the half that survives a future
        caller reaching the row by some other path.  It costs one comparison.
        """
        from yadgar.core.server.tools.adr import adr_get

        caller_dir = str(tmp_path / "yadgar")
        os.makedirs(caller_dir, exist_ok=True)
        recorder = _WikiReadRecorder({})

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=caller_dir,
            ),
            patch("yadgar.core.server.tools.adr.wiki_read", side_effect=recorder),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                # Row leaked from ANOTHER project (task 188 symptom).
                return_value={"row": _row("someone/else", "someone_else_adr-0016")},
            ),
        ):
            adr_get(directory=caller_dir, adr_id="ADR-0016", project=_FLUX_PROJECT)

        assert all("someone_else" not in c["slug"] for c in recorder.calls), (
            "a foreign project's body_slug must never be read: "
            f"{[c['slug'] for c in recorder.calls]!r}"
        )
        assert recorder.calls[0]["slug"] == _FLUX_BODY_SLUG, (
            f"must fall back to the derived D32 3 slug; got {recorder.calls[0]['slug']!r}"
        )

    def test_missing_ledger_row_falls_back_to_derived_slug(self, tmp_path):
        """``_fetch_adr_ledger_row`` returns ``None`` on forward failure — the
        body read must still work off the derived project slug."""
        from yadgar.core.server.tools.adr import adr_get

        caller_dir = str(tmp_path / "yadgar")
        os.makedirs(caller_dir, exist_ok=True)
        recorder = _WikiReadRecorder(
            {
                _FLUX_BODY_SLUG: {
                    "content": "# ADR-0016: cluster-autoscaler\n",
                    "slug": _FLUX_BODY_SLUG,
                    "directory_context": "/home/max/quinyx/flux",
                    "tags": ["adr"],
                }
            }
        )

        def _boom(op: str, payload: dict, **kwargs) -> dict:
            raise RuntimeError("backend unreachable")

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=caller_dir,
            ),
            patch("yadgar.core.server.tools.adr.wiki_read", side_effect=recorder),
            patch("yadgar.core.server.tools.adr._forward_admin", side_effect=_boom),
        ):
            result = adr_get(directory=caller_dir, adr_id="ADR-0016", project=_FLUX_PROJECT)

        assert "error" not in result, f"body must still resolve without a row: {result}"
        assert result.get("content", "").startswith("# ADR-0016")
