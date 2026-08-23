"""Car W4 (ledger task 226) — the slug-resolution family must honour ``project=``.

``_resolve_page_id_by_slug`` (``wiki.py``) carries the defect car W2 fixed one
layer above it in ``wiki_read``: it narrows the lookup on ``directory`` alone::

    page = _st._wiki.read_by_directory(slug, directory)

Its thirteen ``@_tool`` callers — the whole section-patch / versioning family —
each call ``accept_project_param(project, directory)`` and **drop the return
value on the floor**, so ``project=`` reaches validation and nothing else.  That
is the same structural shape car 3 named on the prelude path: "DID validate
``project`` (via ``accept_project_param``) and then dropped the return value on
the floor" (``test_car3_unscoped_reads.py``).

MEASURED ON THE LIVE CORPUS, 2026-08-19 (``db_inspect``, 2523 rows):

* ``project_id`` ↔ ``directory_context`` is **not** 1:1.  ``quinyx/qwfm`` owns
  rows at six distinct ``directory_context`` values, ``quinyx/infrastructure``
  three, ``quinyx/ai`` two — ~57 rows sit at SUBDIRECTORY paths.
* the directory ladder is exact-match then ``'global'``, so every one of those
  rows is structurally unreachable from its own project ROOT directory.
* slugs are unique corpus-wide (2523 rows, 2523 distinct slugs), so re-keying
  cannot change WHICH row resolves — only whether one resolves at all.

WHAT THIS CAR DELIBERATELY DOES NOT DO: it does not make an unresolved project
fail loud.  ``wiki_read``'s strict ``_resolve_wiki_read_project`` predates W2
(car M shipped it); W2's own change was the one-line key swap.  The thirteen
tools here are reached by four browser endpoints
(``api_wiki_history`` / ``api_wiki_read_version`` / ``api_wiki_diff`` /
``api_wiki_restore``) that pass a slug and nothing else, so a new fail-loud tier
would break a live surface.  ``TestUnscopedAndDirectoryOnlyCallsStillResolve``
pins that floor, and the residual ``directory`` rung is reported, not hidden.

BOTH DIRECTIONS ARE PINNED, per the W2 shape: the project-keyed read must start
resolving AND the directory-keyed/unscoped reads must not regress.

THE DOUBLE IS FAITHFUL IN BOTH KEYS.  ``_FakeWikiStore`` implements
``read_by_directory`` (exact directory, then the ``'global'`` rung) AND
``read_by_project`` (own project, then the Car C7 reach tag), and the tests
drive the REAL tools.  So a pre-fix RED is a behavioural not-found — never an
``AttributeError`` that would only prove a new method is absent.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

# ── the corpus this car reasons about (shapes taken from the live rows) ──────

_YADGAR_PROJECT = "m-agahi/yadgar"
_YADGAR_DIR = "/home/max/git/yadgar"
_FLUX_PROJECT = "quinyx/flux"
_FLUX_DIR = "/home/max/quinyx/flux"
_FLUX_SLUG = "quinyx_flux_adr-0016"

#: The measured monorepo shape: one project_id, many directory_context values.
_QWFM_PROJECT = "quinyx/qwfm"
_QWFM_ROOT = "/home/max/quinyx/qwfm"
_QWFM_SUBDIR = "/home/max/quinyx/qwfm/services/analytics"
_QWFM_SLUG = "agent-prompt-locate-config-monorepo"

#: Car C7: reach travels as a tag, not as ``directory_context``.
_REACH_TAG = "global"

_WIKI_PY = pathlib.Path(__file__).resolve().parents[2] / "core" / "server" / "tools" / "wiki.py"

#: Every ``@_tool`` in the family, with a minimal valid call.  ``slug`` and the
#: scoping arguments are injected by the driver; everything here is the payload
#: each tool needs to reach ``_resolve_page_id_by_slug``.  ``anchor_hint`` must
#: be >= 20 chars, and the written text must survive the I26 secret gate.
_ANCHOR = "x" * 25
_FAMILY: dict[str, dict[str, Any]] = {
    "wiki_history": {},
    "wiki_read_version": {"version": 1},
    "wiki_diff": {"v1": 1, "v2": 2},
    "wiki_restore": {"version": 1},
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
    """Faithful stand-in for both resolution ladders over an in-memory corpus."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.dir_calls: list[tuple[str, str | None]] = []
        self.project_calls: list[tuple[str, str | None]] = []

    # §25 keyed on directory: exact directory → 'global' (ADR-0171) → $reach IN
    # tags (Car C9 task 272: mirror the project-side reach rung) → None.
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

    # §25 re-keyed onto project_id: own project → global reach tag → None.
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

    # wiki_history's downstream read.
    def history(self, page_id: int, limit: int = 20) -> list[dict]:
        return [{"version": 1, "page_id": page_id}][:limit]


class _FakeStorage:
    @staticmethod
    def get_max_version_for_page(page_id: int) -> int:  # noqa: ARG004
        return 1


@pytest.fixture
def wiki_tool(monkeypatch):
    """Return ``(wiki_module, wire)``; ``wire(pages)`` installs the fake store.

    Also pins ``lookup_project_for_directory`` to "nothing registered" so the
    resolver's map tier cannot make a directory-only call resolve by accident
    on a host whose ``session_projects.json`` happens to name these paths —
    that would silently turn the directory-rung tests into project-rung tests.
    """
    import yadgar._shared.runtime.session_map as _session_map
    import yadgar._shared.runtime.state as _state
    from yadgar.core.server.tools import wiki as wtool

    monkeypatch.setattr(_session_map, "lookup_project_for_directory", lambda _d: None)
    monkeypatch.setattr(wtool, "_get_storage", lambda: _FakeStorage())

    def wire(pages: list[dict[str, Any]]) -> _FakeWikiStore:
        fake = _FakeWikiStore(pages)
        monkeypatch.setattr(_state, "_wiki", fake)
        return fake

    return wtool, wire


# ── the defect: a correct project= must reach the lookup ─────────────────────


class TestSlugResolutionIsProjectKeyed:
    def test_subdirectory_page_resolves_from_the_project_root(self, wiki_tool):
        """THE MEASURED SHAPE: one project_id, many directory_context values.

        A row stamped ``directory_context=/home/max/quinyx/qwfm/services/analytics``
        is invisible to a caller in ``/home/max/quinyx/qwfm`` — the directory
        ladder is exact-match, and there is no ``'global'`` row to fall to.
        Keyed on ``project_id`` it resolves.
        """
        wtool, wire = wiki_tool
        wire([_page(_QWFM_SLUG, _QWFM_PROJECT, _QWFM_SUBDIR, page_id=41)])

        result = wtool.wiki_history(_QWFM_SLUG, directory=_QWFM_ROOT, project=_QWFM_PROJECT)

        assert "error" not in result, (
            f"a subdirectory row was unreachable from its own project root — got {result!r}"
        )
        assert result["page_id"] == 41

    def test_cross_project_read_resolves_with_a_foreign_directory(self, wiki_tool):
        """W2's measured failure, one layer down: correct project, foreign dir."""
        wtool, wire = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        result = wtool.wiki_history(_FLUX_SLUG, directory=_YADGAR_DIR, project=_FLUX_PROJECT)

        assert "error" not in result, (
            f"a correct project= was defeated by a foreign directory= — got {result!r}"
        )
        assert result["page_id"] == 7952

    def test_lookup_is_never_narrowed_to_the_caller_directory(self, wiki_tool):
        """The discriminating structural assertion.

        With a project named, no directory-keyed lookup may be issued at all.
        Pins the fix at the seam so a later edit cannot reintroduce the
        narrowing while the behavioural tests stay green on a lucky corpus.
        """
        wtool, wire = wiki_tool
        fake = wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR)])

        wtool.wiki_history(_FLUX_SLUG, directory=_YADGAR_DIR, project=_FLUX_PROJECT)

        assert not fake.dir_calls, f"lookup narrowed on directory: {fake.dir_calls!r}"
        assert fake.project_calls == [(_FLUX_SLUG, _FLUX_PROJECT)], (
            f"expected one project-keyed lookup; got {fake.project_calls!r}"
        )

    def test_directory_argument_cannot_change_the_resolved_page(self, wiki_tool):
        """ADR-0233's property: with a project named, ``directory`` is inert.

        There is NO read cache on this path (``_wiki_read_cache`` sits on
        ``wiki_read``), so each variant issues a real lookup and the assertion
        measures resolution rather than a warm entry.
        """
        wtool, wire = wiki_tool
        fake = wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, page_id=7952)])

        variants = [
            wtool.wiki_history(_FLUX_SLUG, project=_FLUX_PROJECT),
            wtool.wiki_history(_FLUX_SLUG, directory=_FLUX_DIR, project=_FLUX_PROJECT),
            wtool.wiki_history(_FLUX_SLUG, directory=_YADGAR_DIR, project=_FLUX_PROJECT),
            wtool.wiki_history(_FLUX_SLUG, directory="/tmp/other/tree", project=_FLUX_PROJECT),
        ]

        assert all("error" not in v for v in variants), (
            f"directory= changed whether the page resolved: {[v.get('error') for v in variants]!r}"
        )
        assert all(v["page_id"] == 7952 for v in variants)
        assert len(fake.project_calls) == 4, (
            "each variant must issue its own lookup — a cache on this path would "
            f"make the assertion above measure the cache: {fake.project_calls!r}"
        )


# ── the reach rung: a global-reach page stays visible from every project ─────


class TestGlobalReachSurvivesTheReKey:
    def test_global_reach_page_resolves_from_another_project(self, wiki_tool):
        """ADR-0171/ADR-0159: the agent-prompt library is cross-project.

        Its rows carry ANOTHER project's ``project_id`` (ownership) plus the
        Car C7 reach tag (reach). A fix that implements only rung 1 makes the
        whole library unpatchable from every other project.
        """
        wtool, wire = wiki_tool
        wire(
            [
                _page(
                    "agent-prompt-pr-review",
                    "local/aws-work",
                    "global",
                    page_id=55,
                    tags=[_REACH_TAG, "agent-prompt"],
                )
            ]
        )

        result = wtool.wiki_history(
            "agent-prompt-pr-review", directory=_YADGAR_DIR, project=_YADGAR_PROJECT
        )

        assert "error" not in result, f"global-reach page unreachable: {result!r}"
        assert result["page_id"] == 55

    def test_own_project_page_wins_over_a_global_reach_page_of_the_same_slug(self, wiki_tool):
        """§25's precedence: own project FIRST, reach as the FALLBACK.

        Reds on a single-clause ``(project_id = $p OR $reach IN tags)`` lookup,
        which under ``LIMIT 1`` picks between the two rows arbitrarily.
        """
        wtool, wire = wiki_tool
        wire(
            [
                _page("shared-slug", "local/aws-work", "global", page_id=1, tags=[_REACH_TAG]),
                _page("shared-slug", _YADGAR_PROJECT, _YADGAR_DIR, page_id=2),
            ]
        )

        result = wtool.wiki_history("shared-slug", directory=_YADGAR_DIR, project=_YADGAR_PROJECT)

        assert result.get("page_id") == 2, (
            f"own-project rung must win over the reach rung; got {result!r}"
        )

    def test_absent_page_still_reports_not_found(self, wiki_tool):
        wtool, wire = wiki_tool
        wire([])

        result = wtool.wiki_history("nope", directory=_YADGAR_DIR, project=_YADGAR_PROJECT)

        assert result.get("error") == "Wiki page 'nope' not found"


# ── the floor this car deliberately keeps ────────────────────────────────────


class TestUnscopedAndDirectoryOnlyCallsStillResolve:
    """The four browser endpoints pass a slug and nothing else.

    ``api_wiki_history`` / ``api_wiki_read_version`` / ``api_wiki_diff`` /
    ``api_wiki_restore`` (``http_wiki_versioning.py``) call the tools
    POSITIONALLY with no ``directory`` and no ``project``. A fail-loud tier for
    an unresolved project would take the viz's version-history, diff and
    restore surfaces offline, so this car keeps the directory rung and the
    unscoped rung intact and reports the residue instead.
    """

    def test_directory_only_call_still_resolves(self, wiki_tool):
        wtool, wire = wiki_tool
        wire([_page("caller-repo-page", _YADGAR_PROJECT, "/caller/repo", page_id=9)])

        result = wtool.wiki_history("caller-repo-page", directory="/caller/repo")

        assert "error" not in result, f"directory-only resolution regressed: {result!r}"
        assert result["page_id"] == 9

    def test_directory_only_call_is_not_widened_to_the_whole_corpus(self, wiki_tool):
        """A directory that names no identity must stay SCOPED, not go corpus-wide.

        The trap in a naive "resolve or None" fix: ``project=None`` with an
        unregistered ``directory`` falls through to an unscoped ``LIMIT 1``
        slug match, so a caller in tree A can be served tree B's row. The
        directory rung is what closes it.
        """
        wtool, wire = wiki_tool
        fake = wire([_page("dupe", _FLUX_PROJECT, _FLUX_DIR, page_id=3)])

        result = wtool.wiki_history("dupe", directory=_YADGAR_DIR)

        assert result.get("error") == "Wiki page 'dupe' not found", (
            f"an unresolved directory widened to a corpus-wide match: {result!r}"
        )
        assert fake.dir_calls == [("dupe", _YADGAR_DIR)]

    def test_slug_only_call_still_resolves(self, wiki_tool):
        """What the browser endpoints do: slug and nothing else."""
        wtool, wire = wiki_tool
        wire([_page("viz-page", _YADGAR_PROJECT, _YADGAR_DIR, page_id=11)])

        result = wtool.wiki_history("viz-page")

        assert "error" not in result, f"the unscoped viz path regressed: {result!r}"
        assert result["page_id"] == 11


# ── Car C9 task 272: directory-keyed ladder mirrors the project-keyed reach ──


class TestDirectoryKeyedLadderReachesTagReach:
    """The rung-3 widening on ``read_by_directory``.

    The Car C7 reach tag (``GLOBAL_REACH_TAG = "global"``) is the post-ADR-0171
    way a cross-project page announces itself. Car C9 added the same rung to the
    directory-keyed ladder so ``wiki_append_section`` / ``wiki_restore`` /
    ``wiki_history`` / ``wiki_diff`` / ``wiki_read_version`` — all of which
    fall through to ``read_by_directory`` — can find the same reach rows
    ``wiki_read`` (project-keyed) and the directory rung already caught for
    legacy ``directory_context = 'global'`` pages.

    The pre-fix RED shape: a row whose ``directory_context`` is a foreign
    project path AND whose only reach signal is the tag (e.g. the
    ``agent-prompt-pr-review`` row tagged ``['global', 'agent-prompt']`` under
    ``project_id=local/aws-work`` / ``directory_context=local/aws-work``)
    was unreachable from any tool that fell through to ``read_by_directory``.
    The wiki browser's positional calls (``http_wiki_versioning.py``'s
    ``api_wiki_*``) hit this rung and got "Wiki page '...' not found".

    The post-fix GREEN: rung 3 catches the tag-reach row. Rungs 1+2 still
    win first; rung 3 is additive only.
    """

    def test_tag_reach_row_resolves_when_directory_ladder_falls_through(self, wiki_tool):
        """The measured live shape: ``directory_context`` is a foreign project
        path; the only reach signal is the tag. Pre-fix: not found. Post-fix:
        rung 3 catches it.
        """
        wtool, wire = wiki_tool
        wire(
            [
                _page(
                    "agent-prompt-pr-review",
                    "local/aws-work",
                    "local/aws-work",  # foreign project path; rung 1 misses
                    page_id=55,
                    tags=[_REACH_TAG, "agent-prompt"],  # rung 3 hits
                )
            ]
        )

        # Directory-keyed path: no project= on the call, so we exercise the
        # directory-keyed ladder (the four browser endpoints do this
        # positionally; Car C9's bug class). The pre-fix RED was
        # "Wiki page 'agent-prompt-pr-review' not found" — same string the
        # Car C7 project-side fix had already eliminated on the project-keyed
        # path.
        result = wtool.wiki_history("agent-prompt-pr-review", directory=_YADGAR_DIR)

        assert "error" not in result, (
            f"directory-keyed ladder missed a tag-reach row; rung 3 missing — got {result!r}"
        )
        assert result["page_id"] == 55

    def test_own_directory_still_wins_over_tag_reach(self, wiki_tool):
        """§25 precedence holds: rung 1 > rung 2 > rung 3.

        A page that exists in the caller's own directory AND has the reach tag
        must resolve to the own-directory row, not the tag-reach page. Pins
        rung 3's status as FALLBACK, not EQUAL.
        """
        wtool, wire = wiki_tool
        wire(
            [
                _page("shared-slug", "local/aws-work", "global", page_id=1, tags=[_REACH_TAG]),
                _page("shared-slug", _YADGAR_PROJECT, _YADGAR_DIR, page_id=2),
            ]
        )

        result = wtool.wiki_history("shared-slug", directory=_YADGAR_DIR)

        assert result.get("page_id") == 2, (
            f"own-directory rung must win over rung 3 (tag reach); got {result!r}"
        )

    def test_global_directory_still_wins_over_tag_reach(self, wiki_tool):
        """§25 precedence: rung 2 ('global' directory) > rung 3 (tag reach).

        A page whose ``directory_context='global'`` is the legacy reach shape;
        tag reach is the post-ADR-0171 shape. A row that carries BOTH signals
        resolves via rung 2, not rung 3.
        """
        wtool, wire = wiki_tool
        wire(
            [
                # rung-2 row: directory_context='global', NO tag reach
                _page("dupe", "local/aws-work", "global", page_id=10, tags=[]),
                # rung-3 row: foreign directory, HAS tag reach
                _page("dupe", "local/aws-work", "local/aws-work", page_id=11, tags=[_REACH_TAG]),
            ]
        )

        result = wtool.wiki_history("dupe", directory=_YADGAR_DIR)

        assert result.get("page_id") == 10, (
            f"rung 2 (directory='global') must win over rung 3 (tag reach); got {result!r}"
        )

    def test_absent_page_still_reports_not_found_with_rung_3(self, wiki_tool):
        """Rung 3 widening must NOT widen the miss to a corpus-wide match.

        A page that exists nowhere — own directory, 'global' directory, AND no
        tag — still returns the not-found envelope. Pins rung 3 as a SCOPED
        lookup, not a "find any row anywhere" escape hatch.
        """
        wtool, wire = wiki_tool
        wire([])

        result = wtool.wiki_history("ghost", directory=_YADGAR_DIR)

        assert result.get("error") == "Wiki page 'ghost' not found", (
            f"rung 3 widening turned a miss into a corpus-wide match: {result!r}"
        )


# ── prove the change reaches all thirteen callers ────────────────────────────


class TestEveryFamilyToolThreadsTheResolvedProject:
    """A fix to the helper alone does not show the FAMILY behaves.

    Each tool independently calls ``accept_project_param`` and discards the
    result; the thread has to be added thirteen times. This drives every one of
    them and asserts the value that reached the resolver is the project_id —
    and, separately, that it is NOT the directory (equality alone would pass a
    regression in any case where the two strings coincide).
    """

    @pytest.fixture
    def capture(self, monkeypatch):
        import yadgar._shared.runtime.state as _state
        from yadgar.core.server.tools import wiki as wtool

        # Three of the thirteen assert ``_st._wiki is not None`` before they
        # resolve; the store is never reached because the resolver is stubbed.
        monkeypatch.setattr(_state, "_wiki", _FakeWikiStore([]))
        seen: dict[str, Any] = {}

        def _fake_resolve(slug, directory=None, *, project_id=None):  # noqa: ARG001
            seen["directory"] = directory
            seen["project_id"] = project_id
            return None, None

        monkeypatch.setattr(wtool, "_resolve_page_id_by_slug", _fake_resolve)
        return wtool, seen

    def test_the_family_table_is_not_vacuous(self) -> None:
        """ADR-0080: the driver must cover the real call-site count."""
        sites = _resolve_call_sites()
        assert len(_FAMILY) == len(sites), (
            f"the table drives {len(_FAMILY)} tools but wiki.py has {len(sites)} "
            f"call sites ({sorted(sites)}) — a tool added to the family without a "
            "row here would never be exercised"
        )

    @pytest.mark.parametrize("tool_name", sorted(_FAMILY))
    def test_tool_passes_the_resolved_project_id_not_the_directory(self, capture, tool_name):
        wtool, seen = capture

        getattr(wtool, tool_name)(
            _FLUX_SLUG,
            directory=_YADGAR_DIR,
            project=_FLUX_PROJECT,
            **_FAMILY[tool_name],
        )

        assert seen.get("project_id") == _FLUX_PROJECT, (
            f"{tool_name} dropped the resolved project on the floor: {seen!r}"
        )
        assert seen["project_id"] != seen.get("directory"), (
            f"{tool_name} passed the directory where the project_id belongs: {seen!r}"
        )


def _resolve_call_sites() -> set[str]:
    """Every ``@_tool`` in ``wiki.py`` that calls ``_resolve_page_id_by_slug``."""
    tree = ast.parse(_WIKI_PY.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("wiki_"):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_resolve_page_id_by_slug"
            ):
                found.add(node.name)
    return found


class TestNoCallSiteResolvesOnDirectoryAlone:
    """Source-level backstop for the twelve tools the driver stubs out.

    The parametrized driver above proves the value ARRIVES; this proves no call
    site was left behind — a new family tool that forgets ``project_id=`` reds
    here even if nobody adds it to ``_FAMILY``.
    """

    def test_every_call_site_passes_project_id(self) -> None:
        tree = ast.parse(_WIKI_PY.read_text())
        offenders: list[str] = []
        total = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "_resolve_page_id_by_slug"):
                continue
            total += 1
            if not any(kw.arg == "project_id" for kw in node.keywords):
                offenders.append(f"line {node.lineno}")
        assert total >= len(_FAMILY), (
            f"only {total} call sites found — the AST walk went blind (ADR-0080)"
        )
        assert not offenders, (
            f"{len(offenders)} call site(s) still resolve without a project_id: {offenders}"
        )
