"""Car M (0047 spine train, §7 row M) — wiki WRITE-side cross-project scope.

The WRITE-SIDE half-heal for the cross-project wiki scoping drift (ledger #50
#364). ``wiki_query`` reads scope by ``project_id``, but ``wiki_write`` paths
accept ``page_id`` and update without re-validating the writer's ``project_id``
against the page's stored ``project_id``. So an operator holding auth for project
A could mutate a wiki page that belongs to project B by passing only the page
id — the page's own ``project_id`` was never consulted at the write seam.

Car L (0047 §7 row L) added the ``project_id`` column on ``wiki_page``. This car
adds the WRITE-SIDE GATE: every ``@_tool`` write shell compares the resolved
caller project_id (the ``_pid`` already returned by ``_resolve_slug_scope_project``)
against the resolved page's stored ``project_id``. Mismatch → ``AdminRefusal``
(``cross_project_write_refused``), HTTP 409 — the same shape Car J used for
mutability lock and Car 5 used for the registry check.

THE ATTACK SHAPE. Three ways the bug is reachable today:

  1. Browser endpoints (``api_wiki_history`` / ``api_wiki_read_version`` /
     ``api_wiki_diff`` / ``api_wiki_restore``) call the tools positionally
     with a slug and no project or directory. ``_resolve_slug_scope_project``
     returns ``None`` and ``_resolve_page_id_by_slug`` falls to rung 3 (unscoped
     slug match) — which returns a row from ANY project. Today the tools then
     pass that page_id straight to the write seam with no project check.
  2. The same shape with ``project=`` declared: ``_resolve_page_id_by_slug``
     does the right thing on the READ (returns None for a foreign project),
     so today an attacker would only exploit this via the unscoped rung. But
     a future refactor that "helps" by widening rung 1 would re-open it.
  3. ``wiki_update`` (admin_other.py) takes a raw ``page_id`` — no slug at all.
     An attacker who learned a page_id from a prior read can pass it directly.

THE GLOBAL-REACH CARVE-OUT. Car C7 tagged the agent-prompt library pages with
``global`` reach so they remain readable from every project (ADR-0171/0159).
Cross-project WRITES to those pages are still allowed — the reach tag is an
explicit cross-project contract, not a passive bypass. A page WITHOUT the reach
tag must refuse cross-project writes.

THE MUTABILITY-LOCK + RESTAMP CARVE-OUT. ``wiki_set_metadata`` restamps
``project_id`` ITSELF (Car 9 retired the legacy retype route and pushed every
project_id restamp through this tool). The seam that enforces cross-project
scope on every OTHER write must not deadlock this path — the restamp is the
mechanism by which a wrongly-keyed page is corrected. Tests pin that.

NOT IN SCOPE (ADR-0337, data half): the 9 legacy rows whose ``project_id``
already mismatches. Those wait for a separate cleanup car; this car only stops
NEW writes from widening the gap.

RED → GREEN: written before the fix. The RED is the cross-project write
succeeding against an unauthorised page — that is the live defect.
"""

from __future__ import annotations

from typing import Any

import pytest

# ── corpus shape (same shape as car-W4 — pinned) ─────────────────────────────

_YADGAR_PROJECT = "m-agahi/yadgar"
_YADGAR_DIR = "/home/max/git/yadgar"
_FLUX_PROJECT = "quinyx/flux"
_FLUX_DIR = "/home/max/quinyx/flux"
_FLUX_SLUG = "quinyx_flux_adr-0016"
_GLOBAL_SLUG = "agent-prompt-pr-review"

#: Car C7: reach travels as a tag, not as ``project_id``.
_REACH_TAG = "global"

_ANCHOR = "x" * 25

#: 10 page_id-keyed tools × minimal valid payload. Each variant drives a real
#: write shell, so a pre-fix RED is a behavioural cross-project leak — never
#: a missing-attr artifact. ``anchor_hint`` ≥20 chars + written text survive
#: the I26 secret gate.
_FAMILY: dict[str, dict[str, Any]] = {
    "wiki_append_section": {"section_heading": "Notes", "content": "body"},
    "wiki_replace_text": {"old_text": "old", "new_text": "new"},
    "wiki_delete_text": {"text": "gone"},
    "wiki_insert_after": {"anchor_text": "anchor", "new_text": "new"},
    "wiki_insert_before": {"anchor_text": "anchor", "new_text": "new"},
    "wiki_replace_at": {
        "line": 1,
        "col": 1,
        "length": 3,
        "new_text": "new",
        "anchor_hint": _ANCHOR,
    },
    "wiki_delete_at": {"line": 1, "col": 1, "length": 3, "anchor_hint": _ANCHOR},
    "wiki_insert_at": {"line": 1, "col": 1, "new_text": "new", "anchor_hint": _ANCHOR},
    "wiki_replace_markdown_block": {
        "block_type": "paragraph",
        "block_index": 0,
        "new_content": "new",
    },
    "wiki_restore": {"version": 1},
}


def _page(
    slug: str,
    project_id: str,
    directory_context: str,
    *,
    page_id: int = 1,
    content: str = "body",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": page_id,
        "slug": slug,
        "title": slug,
        "content": content,
        "project_id": project_id,
        "directory_context": directory_context,
        "tags": list(tags or []),
    }


class _FakeWikiStore:
    """Faithful stand-in for the resolver's ladder — and the write counters."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.dir_calls: list[tuple[str, str | None]] = []
        self.project_calls: list[tuple[str, str | None]] = []

    def read_by_directory(self, slug: str, caller_directory: str | None) -> dict | None:
        self.dir_calls.append((slug, caller_directory))
        rows = [p for p in self.pages if p["slug"] == slug]
        if caller_directory is None:
            return dict(rows[0]) if rows else None
        caller_dir = caller_directory.rstrip("/")
        for p in rows:
            if p["directory_context"] == caller_dir:
                return dict(p)
        for p in rows:
            if p["directory_context"] == "global":
                return dict(p)
        for p in rows:
            if _REACH_TAG in (p.get("tags") or []):
                return dict(p)
        return None

    def read_by_project(self, slug: str, project_id: str | None) -> dict | None:
        self.project_calls.append((slug, project_id))
        rows = [p for p in self.pages if p["slug"] == slug]
        for p in rows:
            if project_id and p.get("project_id") == project_id:
                return dict(p)
        for p in rows:
            if _REACH_TAG in (p.get("tags") or []):
                return dict(p)
        return None

    def history(self, page_id: int, limit: int = 20) -> list[dict]:
        return [{"version": 1, "page_id": page_id}][:limit]


@pytest.fixture
def wiki_tool(monkeypatch):
    """Return ``(wiki_module, wire, forwarded)``.

    Patches ``_forward_admin`` so the test never races the real /admin endpoint
    (which would need a live backend with ``YADGAR_EMBED_URL`` set — not the
    case in CI). The patched forward returns a success envelope and RECORDS
    every call in ``forwarded`` — that is the structural assertion the
    cross-project gate is meant to make: a refused write must never forward.
    """
    import yadgar._shared.runtime.session_map as _session_map
    import yadgar._shared.runtime.state as _state
    from yadgar.core.server.tools import admin_other as _aot
    from yadgar.core.server.tools import wiki as wtool

    monkeypatch.setattr(_session_map, "lookup_project_for_directory", lambda _d: None)

    forwarded: list[tuple[str, dict]] = []

    def _fake_forward(op: str, payload: dict, **_kw) -> dict:
        forwarded.append((op, payload))
        # Mirror the real ``wiki_set_metadata`` / ``wiki_set_mutability``
        # return shapes — those ops are slug-keyed (no page_id in payload),
        # so they return ``{ok, slug, rows_updated, page_ids}``. The page_id-
        # keyed ops return ``{committed: True, page_id: N, ...}``.
        if op in ("wiki_set_metadata", "wiki_set_mutability"):
            return {
                "ok": True,
                "slug": payload.get("slug"),
                "rows_updated": 1,
                "page_ids": [1],
            }
        return {"committed": True, "op": op, "page_id": payload.get("page_id")}

    monkeypatch.setattr(wtool, "_forward_admin", _fake_forward)
    monkeypatch.setattr(_aot, "_forward_admin", _fake_forward)

    def wire(pages: list[dict[str, Any]]) -> _FakeWikiStore:
        fake = _FakeWikiStore(pages)

        class _FakeStorage:
            def __init__(self, _pages):
                self._pages = _pages

            def get_wiki_page_ids_by_slug(self, slug: str) -> list[int]:
                return [p["id"] for p in self._pages if p["slug"] == slug]

            def get_wiki_page(self, page_id: int) -> dict | None:
                for p in self._pages:
                    if p["id"] == page_id:
                        return dict(p)
                return None

        fake._storage = _FakeStorage(pages)  # type: ignore[attr-defined]
        monkeypatch.setattr(_state, "_wiki", fake)
        return fake

    return wtool, wire, forwarded


# ── the defect: every page_id-keyed write must refuse cross-project ─────────


class TestPageIdKeyedWritesRefuseCrossProject:
    """A caller holding auth for project A must NOT mutate a page stamped B.

    THE ATTACK SHAPE (the one this car closes): the caller passes a slug
    whose resolver returns a foreign page, AND declares a ``project=`` that
    does not match the page's stored ``project_id``. Today the tools trust
    the integer and forward the write. Post-fix the gate compares the
    resolved page's ``project_id`` against the caller's declared identity
    and refuses the mismatch.

    THE BROWSER-ENDPOINT TOLERANCE. The four viz endpoints (``api_wiki_history``
    etc.) and the pre-Car-M integration tests pass a slug with NO ``project=``
    and NO ``directory=``. Car W4's resolver is tolerant on purpose — an
    unresolved identity degrades to ``None`` and the directory rung is
    reached — so failing loud on unscoped would take the viz offline. The
    defect this car closes is the EXPLICIT attack (caller declares A, page
    is B), not the unscoped-rung reading. A follow-up car that closes the
    browser-endpoint rung would extend the gate to ``caller_project_id is
    None``; that is OUT OF SCOPE here.
    """

    @pytest.mark.parametrize("tool_name,payload", list(_FAMILY.items()))
    def test_declared_project_cannot_write_to_a_foreign_slug_is_refused(
        self, wiki_tool, tool_name, payload
    ):
        """The explicit-attack shape: caller declares A, page is stamped B.

        Today the write goes through (the slug→page_id resolver returns the
        page because the unscoped rung 3 matches the slug, and the tools
        forward the integer to the backend op). Post-fix the gate sees the
        page's ``project_id`` (``flux``) does not match the caller's
        declared identity (``yadgar``) and refuses.
        """
        wtool, wire, forwarded = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        tool_fn = getattr(wtool, tool_name)
        # Caller declares yadgar; page is stamped flux; directory is yadgar's
        # tree (so rung 2 would NOT find this row — rung 3 is the attack
        # vector). The slug is the discriminator, and rung 3 returns the
        # only row for the slug, regardless of project.
        result = tool_fn(_FLUX_SLUG, directory=_YADGAR_DIR, project=_YADGAR_PROJECT, **payload)

        assert result.get("refused") is True, (
            f"{tool_name} let a cross-project write through: {result!r}"
        )
        assert result.get("reason") == "cross_project_write_refused", (
            f"{tool_name} refused without the canonical reason: {result!r}"
        )
        assert not forwarded, (
            f"{tool_name} reached the forward seam despite a cross-project caller: {forwarded!r}"
        )
        # The structural assertion: the page's project_id never leaks to the
        # caller through the refusal envelope (defence against log scraping).
        assert "page_id" not in result, (
            f"{tool_name} leaked the B-owned page_id to the caller: {result!r}"
        )

    def test_own_project_write_still_succeeds(self, wiki_tool):
        """Sanity: same-project writes stay green (project=flux, page=flux)."""
        wtool, wire, forwarded = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        result = wtool.wiki_replace_text(
            _FLUX_SLUG,
            old_text="old",
            new_text="new",
            directory=_FLUX_DIR,
            project=_FLUX_PROJECT,
        )

        assert "refused" not in (result or {}), f"same-project write was refused: {result!r}"
        # The forward was reached with the right page_id.
        assert forwarded, "same-project write did not reach the forward seam"
        assert any(
            op == "wiki_replace_text" and payload.get("page_id") == 7952
            for op, payload in forwarded
        ), f"unexpected forward: {forwarded!r}"

    def test_unscoped_caller_still_writes(self, wiki_tool):
        """Browser-endpoint tolerance: unscoped callers are NOT refused.

        Car W4's resolver is tolerant on purpose; the gate mirrors that
        tolerance for the unscoped path (a follow-up car would close it).
        Pre-fix the unscoped path forwarded; post-fix it still forwards,
        so existing viz endpoints and pre-Car-M integration tests keep
        working.
        """
        wtool, wire, forwarded = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        result = wtool.wiki_replace_text(
            _FLUX_SLUG,
            old_text="old",
            new_text="new",
        )

        assert "refused" not in (result or {}), (
            f"unscoped write was refused (out of scope for this car): {result!r}"
        )
        assert forwarded, "unscoped write did not reach the forward seam"


# ── the global-reach carve-out ──────────────────────────────────────────────


class TestGlobalReachTagAllowsCrossProjectWrites:
    """The reach tag is an EXPLICIT cross-project contract (ADR-0171/C7)."""

    def test_global_reach_page_is_writable_from_a_foreign_project(self, wiki_tool):
        """A page carrying the global reach tag stays writable cross-project.

        This is the agent-prompt library surface (ADR-0007) — owned by one
        project, but reachable AND writable from every project so the dispatch
        family can extend the library. The RED case is a stricter rule that
        narrows writes to same-project only; that would lock the library down.
        """
        wtool, wire, forwarded = wiki_tool
        wire(
            [
                _page(
                    _GLOBAL_SLUG,
                    _FLUX_PROJECT,
                    "global",
                    page_id=99,
                    tags=[_REACH_TAG, "agent-prompt"],
                )
            ]
        )

        result = wtool.wiki_replace_text(
            _GLOBAL_SLUG,
            old_text="old",
            new_text="new",
            directory=_YADGAR_DIR,
            project=_YADGAR_PROJECT,
        )

        assert "refused" not in (result or {}), (
            f"global-reach page was refused from a foreign project: {result!r}"
        )
        assert forwarded, "global-reach write did not reach the forward seam"


# ── the restamp carve-out (wiki_set_metadata on project_id) ─────────────────


class TestSetMetadataRestampIsNotDeadlocked:
    """``wiki_set_metadata`` restamps ``project_id`` ITSELF (Car 9).

    The cross-project write gate must NOT block this — a wrongly-keyed page
    is corrected by this tool, so blocking it on the very field it sets would
    freeze every mis-stamp in place. The gate still runs (and refuses when the
    field is something OTHER than ``project_id`` on a foreign page), but the
    restamp itself is sanctioned at the MCP boundary.
    """

    def test_restamping_project_id_on_a_foreign_slug_is_allowed(self, wiki_tool):
        """The restamp IS the mechanism by which the scoping drift is repaired.

        A yadgar operator finds a yadgar-owned page mistakenly stamped
        ``quinyx/flux`` — calling ``wiki_set_metadata(slug, "project_id",
        "m-agahi/yadgar")`` is the documented correction (Car 9 docstring).
        Pinning this as GREEN is what stops the half-heal from being a
        full-block.
        """
        wtool, wire, forwarded = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        result = wtool.wiki_set_metadata(
            _FLUX_SLUG,
            "project_id",
            _YADGAR_PROJECT,
            directory=_YADGAR_DIR,
            project=_YADGAR_PROJECT,
        )

        assert result.get("ok") is True, (
            f"project_id restamp was refused from the operator's own project: {result!r}"
        )
        # Forward reached with the restamp payload.
        assert any(
            op == "wiki_set_metadata"
            and payload == {"slug": _FLUX_SLUG, "field": "project_id", "value": _YADGAR_PROJECT}
            for op, payload in forwarded
        ), f"restamp did not reach forward: {forwarded!r}"

    def test_directory_context_restamp_on_a_foreign_slug_is_refused(self, wiki_tool):
        """``directory_context`` is NOT the carve-out (only ``project_id`` is).

        ``directory_context`` is no longer the scoping key (ADR-0233) but it
        still feeds ``_resolve_page_id_by_slug``'s rung 2 — a cross-project
        rewrite of that field would let a foreign operator shift where the
        page resolves. The restamp carve-out is restricted to ``project_id``
        because that field is the documented correction path for the drift
        this car exists to enforce.
        """
        wtool, wire, forwarded = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        result = wtool.wiki_set_metadata(
            _FLUX_SLUG,
            "directory_context",
            _YADGAR_DIR,
            directory=_YADGAR_DIR,
            project=_YADGAR_PROJECT,
        )

        assert result.get("refused") is True, (
            f"directory_context restamp was permitted cross-project: {result!r}"
        )
        assert not forwarded


# ── the slug-keyed all-rows paths (set_metadata, set_mutability) ─────────────


class TestSlugKeyedAllRowsWritesRefuseForeignSlug:
    """``wiki_set_metadata`` / ``wiki_set_mutability`` reach EVERY row for a slug.

    A slug whose row set contains a foreign project is no longer writable from
    a caller in a single project — the gate runs against the WHOLE set. The
    restamp carve-out (``project_id`` field) is its own class.
    """

    def test_set_metadata_with_a_non_restamp_field_on_a_foreign_slug_is_refused(self, wiki_tool):
        wtool, wire, forwarded = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        result = wtool.wiki_set_metadata(
            _FLUX_SLUG,
            "directory_context",
            "/new/dir",
            directory=_YADGAR_DIR,
            project=_YADGAR_PROJECT,
        )

        assert result.get("refused") is True, (
            f"wiki_set_metadata let a yadgar operator restamp directory_context "
            f"on a fully-flux slug: {result!r}"
        )
        assert result.get("reason") == "cross_project_write_refused"
        assert not forwarded, (
            f"wiki_set_metadata reached forward despite a foreign slug: {forwarded!r}"
        )

    def test_set_metadata_on_a_slug_mixed_across_projects_is_refused(self, wiki_tool):
        """A slug with rows in TWO projects is not writable from a single caller.

        Today (Car L) the schema permits a slug appearing in N projects' pages.
        A yadgar operator finding one such slug must not get to rewrite either
        project's row — neither is theirs alone.
        """
        wtool, wire, forwarded = wiki_tool
        wire(
            [
                _page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952),
                _page(_FLUX_SLUG, _YADGAR_PROJECT, _YADGAR_DIR, page_id=7953),
            ]
        )

        result = wtool.wiki_set_metadata(
            _FLUX_SLUG,
            "directory_context",
            "/new/dir",
            directory=_YADGAR_DIR,
            project=_YADGAR_PROJECT,
        )

        assert result.get("refused") is True, f"mixed-project slug was writable: {result!r}"
        assert not forwarded

    def test_set_mutability_on_a_foreign_slug_is_refused(self, wiki_tool):
        wtool, wire, forwarded = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        result = wtool.wiki_set_mutability(
            _FLUX_SLUG, "locked", "audit reason", directory=_YADGAR_DIR, project=_YADGAR_PROJECT
        )

        assert result.get("refused") is True, (
            f"wiki_set_mutability let a foreign-project mutability change through: {result!r}"
        )
        assert result.get("reason") == "cross_project_write_refused"
        assert not forwarded


# ── out-of-scope notes (car M half-heal boundaries) ──────────────────────────


class TestWikiUpdateIsOutOfScope:
    """The ``wiki_update`` page_id-only path is a known gap.

    ``wiki_update`` (in ``admin_other.py``, not ``wiki.py``) takes a raw
    ``page_id`` integer — no slug, no ``project=`` argument. A caller who
    learned a page_id from a prior read can pass it directly. The full fix
    requires extending the tool's signature (adding a ``project=`` kwarg),
    which conflicts with the master branch's call sites and is broader than
    this half-heal car wants to take on.

    The slug-keyed family this car fixes (10 page_id-keyed tools +
    ``wiki_set_metadata`` + ``wiki_set_mutability``) covers the cross-
    project exposure on the SURGICAL EDIT path and the METADATA path —
    every operational write shape except the legacy content-replace
    tool. A follow-up car can close ``wiki_update`` once the call-site
    inventory is done.
    """

    def test_placeholder_does_not_regress(self, wiki_tool):
        """A sanity check that the test class itself runs."""
        wtool, wire, forwarded = wiki_tool
        assert wtool is not None  # wiki_update gap is documented, not silently closed
