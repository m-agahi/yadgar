"""C3 (0047 PR#40 remediation) — the ``project`` parameter surface.

Two contracts, both written RED before the implementation:

1. **Every scoped MCP tool accepts ``project``.** A tool that takes
   ``directory`` is scoped to a project, so it must also expose the
   replacement key ``project`` (keyword-only, default ``None``). C5 later
   REMOVES ``directory``; C3 only adds ``project`` alongside it.

   The walk keys on the ``@_tool`` decorator, NOT on ``tools/__init__.py``'s
   ``__all__``. ``__all__`` is demonstrably incomplete — ``recent_memories``,
   ``wiki_replace_at``, ``wiki_delete_at``, ``wiki_insert_at`` and
   ``wiki_replace_markdown_block`` are live MCP tools absent from it — so a
   test keyed on ``__all__`` could be satisfied by a tool nobody exported.

2. **The backend HTTP boundary accepts ``project_id``.** ``recall.py``'s
   ``_forward_to_backend`` already puts ``project_id`` on the wire, while
   ``RecallRequest`` is ``extra="forbid"`` and has no such field — so
   ``resp.raise_for_status()`` raised HTTP 422 on EVERY ``recall(project=…)``
   call. Cross-project recall has never worked on any branch. The test feeds
   the payload the real forwarder builds into the real model, so the two ends
   are pinned against each other rather than against a hand-copied dict.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

_TOOLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "core" / "server" / "tools"

# Guard against a gate-blind scan (ADR-0080): the walk must find a
# non-trivial number of scoped tools, else an empty result set would make
# every assertion below vacuously true.
_MIN_SCOPED_TOOLS = 40


def _is_tool_decorated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "_tool":
            return True
        if isinstance(dec, ast.Name) and dec.id == "_tool":
            return True
    return False


def _scoped_tools() -> list[tuple[str, str, int, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every ``@_tool``-decorated function that takes ``directory``."""
    found: list[tuple[str, str, int, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for path in sorted(_TOOLS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _is_tool_decorated(node):
                continue
            names = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
            if "directory" in names:
                found.append((node.name, path.name, node.lineno, node))
    return found


class TestEveryScopedToolAcceptsProject:
    """Contract 1 — the 42-signature surface."""

    def test_scan_finds_the_scoped_tool_surface(self) -> None:
        """The walk itself must not be vacuous (ADR-0080)."""
        tools = _scoped_tools()
        assert len(tools) >= _MIN_SCOPED_TOOLS, (
            f"only {len(tools)} @_tool functions take `directory` — the AST walk "
            "found less than the known surface, so the assertions below would be "
            "vacuously true"
        )

    def test_every_directory_tool_also_takes_project(self) -> None:
        missing = [
            f"{name} ({fname}:{lineno})"
            for name, fname, lineno, node in _scoped_tools()
            if "project"
            not in ([a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs])
        ]
        assert not missing, (
            f"{len(missing)} scoped MCP tools take `directory` but not `project`:\n  "
            + "\n  ".join(missing)
        )

    def test_project_is_keyword_only_with_none_default(self) -> None:
        """``project`` is keyword-only and defaults to None on every scoped tool.

        ONE documented exception: ``wiki_write_task_list`` has carried a
        REQUIRED, positional ``project`` since Car 1 — and it is not the same
        parameter. There it is a slug component (the page is stored at
        ``{project}-task-list``), not a project_id override, so it is neither
        optional nor keyword-only. Named explicitly rather than pattern-matched
        so a second such tool cannot slip in behind the exemption.
        """
        bad: list[str] = []
        for name, fname, lineno, node in _scoped_tools():
            if name == "wiki_write_task_list":
                continue
            kwonly = {a.arg: i for i, a in enumerate(node.args.kwonlyargs)}
            if "project" not in kwonly:
                bad.append(f"{name} ({fname}:{lineno}): `project` is not keyword-only")
                continue
            default = node.args.kw_defaults[kwonly["project"]]
            if not (isinstance(default, ast.Constant) and default.value is None):
                bad.append(f"{name} ({fname}:{lineno}): `project` default is not None")
        assert not bad, "\n  ".join(bad)


class TestRecallRequestAcceptsProjectId:
    """Contract 2 — the live 422 on every ``recall(project=…)``."""

    def test_model_has_project_id_field(self) -> None:
        from yadgar.backend.embed_service.embed_service_models import RecallRequest

        assert "project_id" in RecallRequest.model_fields

    def test_forwarded_payload_validates_against_the_model(self, monkeypatch) -> None:
        """The payload ``_forward_to_backend`` actually builds must validate.

        Pins both ends: the wire payload is captured from the real forwarder
        (not hand-copied) and fed to the real Pydantic model.
        """
        from importlib import import_module

        import httpx

        from yadgar.backend.embed_service.embed_service_models import RecallRequest

        # NOT ``from ... tools import recall`` — ``tools/__init__.py`` re-exports
        # the recall FUNCTION under that name, shadowing the submodule.
        recall_mod = import_module("yadgar.core.server.tools.recall")

        captured: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"results": []}

        def _fake_post(url: str, json: dict, headers: dict, timeout: float) -> _Resp:  # noqa: A002
            captured.update(json)
            return _Resp()

        monkeypatch.setenv("YADGAR_EMBED_URL", "http://stub-backend:8001")
        monkeypatch.setattr(httpx, "post", _fake_post)

        recall_mod._forward_to_backend(
            query="q",
            directory="/home/max/git/yadgar",
            max_results=5,
            min_heat=0.0,
            type_filter="all",
            tags=None,
            mode=None,
            profile=None,
            project_id="a/b",
        )

        assert captured["project_id"] == "a/b"
        model = RecallRequest(**captured)
        assert model.project_id == "a/b"

    def test_model_still_rejects_genuinely_unknown_keys(self) -> None:
        """``extra="forbid"`` survives — the fix adds a field, not a hole."""
        from pydantic import ValidationError

        from yadgar.backend.embed_service.embed_service_models import RecallRequest

        with pytest.raises(ValidationError):
            RecallRequest.model_validate({"query": "q", "directory": "/tmp", "not_a_real_field": 1})


class TestResolveEffectiveProjectSignatureUnchanged:
    """C3 is additive: the resolver keeps its C2-era tiers."""

    def test_resolver_still_takes_three_keyword_args(self) -> None:
        from yadgar.core.server.tools._project_param import resolve_effective_project

        params = inspect.signature(resolve_effective_project).parameters
        assert set(params) == {"project", "directory", "session_project"}
        assert all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in params.values())


class TestAcceptProjectParamIsNotDecorative:
    """The helper the 42 no-sink tools route their ``project`` into.

    Without these, the sweep's only assertion is signature SHAPE — the helper
    could ``return None`` unconditionally and every other test would stay
    green (ADR-0080: a check that cannot fail).
    """

    def test_valid_override_is_returned(self) -> None:
        from yadgar.core.server.tools._project_param import accept_project_param

        assert (
            accept_project_param("quinyx/aws2slack", "/home/max/git/yadgar") == "quinyx/aws2slack"
        )

    def test_empty_override_raises_at_the_boundary(self) -> None:
        from yadgar.core.server.tools._project_param import (
            InvalidProjectOverrideError,
            accept_project_param,
        )

        with pytest.raises(InvalidProjectOverrideError):
            accept_project_param("", "/home/max/git/yadgar")

    def test_non_string_override_raises_at_the_boundary(self) -> None:
        from yadgar.core.server.tools._project_param import (
            InvalidProjectOverrideError,
            accept_project_param,
        )

        with pytest.raises(InvalidProjectOverrideError):
            accept_project_param(17, "/home/max/git/yadgar")  # type: ignore[arg-type]

    def test_absent_override_never_touches_the_classifier(self, monkeypatch) -> None:
        """``project=None`` must NOT pay for a derivation nothing reads yet.

        ``derive_project_id`` shells out to git twice and is uncached; running
        it on every call of every scoped tool to compute a value with no sink
        would be a straight latency regression (see the helper's docstring).
        """
        from yadgar.core.server.tools import _project_param as pp

        def _explode(*_a: object, **_kw: object) -> tuple[str, str]:
            raise AssertionError("derive_project_id must not be called when project is None")

        monkeypatch.setattr(pp, "derive_project_id", _explode)
        assert pp.accept_project_param(None, "/home/max/git/yadgar") is None

    def test_every_scoped_tool_rejects_an_empty_project(self) -> None:
        """Boundary validation is reachable through the REAL tool signatures.

        Spot-checks one tool per module that got the sweep, so a helper that
        stopped raising, or a tool wired to swallow the error, is caught.
        """
        from yadgar.core.server.tools._project_param import InvalidProjectOverrideError
        from yadgar.core.server.tools.blocks import block_list
        from yadgar.core.server.tools.runtime_config import config_list

        for fn in (block_list, config_list):
            with pytest.raises(InvalidProjectOverrideError):
                fn(directory="/home/max/git/yadgar", project="")
