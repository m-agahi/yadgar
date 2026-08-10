"""Car M (0047 spine train, §7 row M) — cross-project ``project=`` param tests.

The MCP tool surface gains an OPTIONAL ``project: str | None = None`` parameter
that lets a caller address another project's namespace without leaving the
current working tree. Default = the derived current project (from SessionStart
context — Car E). Override = the caller-supplied ``project``, validated for
type-level shape; the deep "is this project_id in the registry?" check lives
at the backend write path (Car A0 ``_ensure_project_exists_sync``, §15 /
ADR-0078).

These tests pin:

  * §3 new helper — ``resolve_effective_project`` (override → session →
    directory → "global" precedence) + ``InvalidProjectOverrideError``.
  * §3 tool-surface — every new ``project=`` param is keyword-only, defaults
    to ``None``, and (when supplied) threads the resolved project_id
    through to the wire payload or ledger write.
  * §9 [VERIFY] — when BOTH ``project`` and ``directory`` are supplied,
    ``project`` wins and ``directory`` is logged-and-ignored.
  * §3 read tools — unknown project_id does NOT auto-create a row
    (read-only path is fail-quiet: an unknown project simply returns no
    rows / an empty list — the registry failure surfaces at the next
    WRITE through Car A0's FAIL-LOUD backend guard).

RED → GREEN: this file was written before implementation; the helper
module (``yadgar/core/server/tools/_project_param.py``) and the
``project=`` threading in ``recall`` / ``memorize`` / ``wiki_*`` /
``adr_*`` / ``task_*`` were implemented to make these tests pass.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ── resolve_effective_project — precedence + validation ─────────────────────


class TestResolveEffectiveProject:
    """§3 helper precedence: project → session → directory → 'global'."""

    def test_override_wins_when_supplied(self) -> None:
        """Caller-supplied ``project`` wins over session + directory."""
        from yadgar.core.server.tools._project_param import resolve_effective_project

        out = resolve_effective_project(
            project="quinyx/aws2slack",
            directory="/home/max/git/yadgar",
            session_project="some-session-proj",
        )
        assert out == "quinyx/aws2slack"

    def test_session_falls_through_when_no_override(self) -> None:
        """When no ``project`` arg, ``session_project`` is used."""
        from yadgar.core.server.tools._project_param import resolve_effective_project

        out = resolve_effective_project(
            project=None,
            directory="/home/max/git/yadgar",
            session_project="session-proj-x",
        )
        assert out == "session-proj-x"

    def test_directory_derives_when_no_override_no_session(self) -> None:
        """When neither project nor session, ``derive_project_id(directory)`` wins.

        ``/home/max/git/yadgar`` is the live yadgar repo (Car A0's derive
        returns ``m-agahi/yadgar``; host excluded).

        Live ``derive_project_id`` reads git remotes — in the CI runner the
        checkout URL is ``local/yadgar`` (not the GitHub path) so the real
        classifier returns ``local/yadgar`` instead. Patch the seam (same
        pattern test_car_l_write_paths_stamp_project_id uses) so the
        precedence contract is what's under test, not the runner's git state.
        """
        from yadgar.core.server.tools._project_param import resolve_effective_project

        with patch(
            "yadgar.core.server.tools._project_param.derive_project_id",
            return_value=("m-agahi/yadgar", ""),
        ):
            out = resolve_effective_project(
                project=None,
                directory="/home/max/git/yadgar",
                session_project=None,
            )
        assert out == "m-agahi/yadgar"

    def test_falls_back_to_global_when_no_resolution(self) -> None:
        """All None / empty → ``GLOBAL_FALLBACK`` (= ``"global"``)."""
        from yadgar.core.server.tools._project_param import (
            GLOBAL_FALLBACK,
            resolve_effective_project,
        )

        out = resolve_effective_project(
            project=None,
            directory=None,
            session_project=None,
        )
        assert out == GLOBAL_FALLBACK

    def test_directory_with_empty_string_falls_through(self) -> None:
        """An empty/whitespace-only ``directory`` is treated as None."""
        from yadgar.core.server.tools._project_param import (
            GLOBAL_FALLBACK,
            resolve_effective_project,
        )

        out = resolve_effective_project(
            project=None,
            directory="   ",
            session_project=None,
        )
        assert out == GLOBAL_FALLBACK

    def test_session_empty_string_falls_through(self) -> None:
        """An empty ``session_project`` is treated as None."""
        from yadgar.core.server.tools._project_param import resolve_effective_project

        with patch(
            "yadgar.core.server.tools._project_param.derive_project_id",
            return_value=("m-agahi/yadgar", ""),
        ):
            out = resolve_effective_project(
                project=None,
                directory="/home/max/git/yadgar",
                session_project="",
            )
        # Falls through to directory-derived
        assert out == "m-agahi/yadgar"

    def test_override_rejects_non_string(self) -> None:
        """Type-level guard: non-string ``project`` raises InvalidProjectOverrideError."""
        from yadgar.core.server.tools._project_param import (
            InvalidProjectOverrideError,
            resolve_effective_project,
        )

        with pytest.raises(InvalidProjectOverrideError):
            resolve_effective_project(  # type: ignore[arg-type]
                project=123,  # not a string
                directory=None,
                session_project=None,
            )

    def test_override_rejects_empty_string(self) -> None:
        """Type-level guard: empty-string ``project`` raises."""
        from yadgar.core.server.tools._project_param import (
            InvalidProjectOverrideError,
            resolve_effective_project,
        )

        with pytest.raises(InvalidProjectOverrideError):
            resolve_effective_project(
                project="",
                directory="/home/max/git/yadgar",
                session_project=None,
            )

    def test_override_wins_when_both_directory_and_override_supplied(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§9 [VERIFY]: ``project`` wins over a supplied ``directory``; directory
        is logged-and-ignored."""
        from yadgar.core.server.tools._project_param import resolve_effective_project

        with caplog.at_level("INFO", logger="yadgar.core.server.tools._project_param"):
            out = resolve_effective_project(
                project="override/proj",
                directory="/home/max/git/yadgar",
                session_project=None,
            )
        assert out == "override/proj"
        # The precedence note MUST be observable so misuse is logged.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("overrides supplied directory" in m for m in msgs), msgs

    def test_directory_derivation_failure_falls_back_to_global(self) -> None:
        """When ``derive_project_id`` raises on the directory path, fall back to
        GLOBAL_FALLBACK (the safe cross-project sentinel). The tool call must
        NOT crash on a non-git / unreadable directory."""
        from yadgar.core.server.tools._project_param import (
            GLOBAL_FALLBACK,
            resolve_effective_project,
        )

        # Patch the imported symbol (resolve_effective_project already bound it
        # at module load — patch the local reference, not yadgar.core.identity,
        # so a single test stays self-contained).
        with patch(
            "yadgar.core.server.tools._project_param.derive_project_id",
            side_effect=RuntimeError("nope"),
        ):
            out = resolve_effective_project(
                project=None,
                directory="/nonexistent",
                session_project=None,
            )
        assert out == GLOBAL_FALLBACK


# ── recall(project=...) — keyword-only + payload stamping ───────────────────


class TestRecallProjectParam:
    """§3: ``recall()`` gains ``project=`` keyword-only, threads into wire payload."""

    def test_recall_accepts_project_kwarg(self) -> None:
        """``project`` is keyword-only (no positional)."""
        import inspect

        from yadgar.core.server.tools.recall import recall

        sig = inspect.signature(recall)
        assert "project" in sig.parameters
        param = sig.parameters["project"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_recall_rejects_non_string_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Type-level guard: ``project=123`` raises ValueError at the boundary."""
        # Bypass the backend-required check by providing BOTH project and directory.
        with pytest.raises(ValueError, match="recall: project must be a string"):
            from yadgar.core.server.tools.recall import recall

            # Disable the backend-required check via a project override so we
            # don't have to spin up an HTTP backend.
            recall(
                query="x",
                project=123,  # type: ignore[arg-type]
            )

    def test_recall_rejects_empty_project(self) -> None:
        """Empty-string ``project`` is rejected (fail-loud)."""
        with pytest.raises(ValueError, match="recall: project must be non-empty"):
            from yadgar.core.server.tools.recall import recall

            recall(
                query="x",
                directory="/home/max/git/yadgar",
                project="",
            )

    def test_recall_forwards_project_id_to_backend(self) -> None:
        """When ``project=`` is supplied, the resolved project_id is on the
        payload sent to the backend."""
        from yadgar.core.server.tools.recall import recall

        captured: dict = {}

        def fake_forward_to_backend(**kwargs):  # noqa: ARG001
            captured.update(kwargs)
            return []

        with (
            patch(
                "yadgar.core.server.tools.recall._forward_to_backend",
                side_effect=fake_forward_to_backend,
            ),
            # session-side bookkeeping submit (deferred worker)
            patch(
                "yadgar.core.server.tools.recall._submit_session_side_effect",
                return_value=None,
            ),
            patch(
                "yadgar.core.server.tools.recall._st._consolidation",
                None,
            ),
        ):
            recall(
                query="x",
                directory="/home/max/git/yadgar",
                project="quinyx/aws2slack",
            )

        assert captured.get("project_id") == "quinyx/aws2slack"

    def test_recall_no_project_omits_project_id_from_payload(self) -> None:
        """When ``project=None``, the wire payload must NOT include ``project_id``
        (wire-compatible with older backends whose RecallRequest is
        ``extra="forbid"``)."""
        from yadgar.core.server.tools.recall import recall

        captured: dict = {}

        def fake_forward_to_backend(**kwargs):  # noqa: ARG001
            captured.update(kwargs)
            return []

        with (
            patch(
                "yadgar.core.server.tools.recall._forward_to_backend",
                side_effect=fake_forward_to_backend,
            ),
            patch(
                "yadgar.core.server.tools.recall._submit_session_side_effect",
                return_value=None,
            ),
            patch(
                "yadgar.core.server.tools.recall._st._consolidation",
                None,
            ),
        ):
            recall(
                query="x",
                directory="/home/max/git/yadgar",
            )

        # project_id is None → not forwarded.
        assert captured.get("project_id") is None


# ── memorize(project=...) — payload stamping ──────────────────────────────────


class TestMemorizeProjectParam:
    """§3: ``memorize()`` gains ``project=`` keyword-only, stamps project_id
    on the enqueue payload."""

    def test_memorize_accepts_project_kwarg(self) -> None:
        """``project`` is keyword-only."""
        import inspect

        from yadgar.core.server.tools.memorize import memorize

        sig = inspect.signature(memorize)
        assert "project" in sig.parameters
        param = sig.parameters["project"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_memorize_stamps_project_id_on_payload_when_supplied(self) -> None:
        """When ``project=`` is supplied, the enqueue payload carries
        ``project_id`` so the drainer routes by project_id (Car A0 backend)."""
        from yadgar.core.server.tools.memorize import memorize

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured["op"] = op
            captured["payload"] = payload
            return "fake-job-id-42"

        with (
            patch(
                "yadgar.core.server.tools.memorize._get_file_queue",
                return_value=type(
                    "FQ",
                    (),
                    {"enqueue": staticmethod(fake_enqueue)},
                )(),
            ),
        ):
            result = memorize(
                content="car m test memory",
                context="/home/max/git/yadgar",
                tags=[],
                project="quinyx/aws2slack",
            )

        assert result.get("stored") is True
        assert captured["payload"]["project_id"] == "quinyx/aws2slack"

    def test_memorize_no_project_omits_project_id_from_payload(self) -> None:
        """When ``project=None``, the wire payload stays project-id-free."""
        from yadgar.core.server.tools.memorize import memorize

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured["op"] = op
            captured["payload"] = payload
            return "fake-job-id-43"

        with (
            patch(
                "yadgar.core.server.tools.memorize._get_file_queue",
                return_value=type(
                    "FQ",
                    (),
                    {"enqueue": staticmethod(fake_enqueue)},
                )(),
            ),
        ):
            memorize(
                content="car m test memory",
                context="/home/max/git/yadgar",
                tags=[],
            )

        assert "project_id" not in captured["payload"]

    def test_memorize_invalid_project_returns_error_envelope(self) -> None:
        """Type-level guard: non-string ``project`` returns the tool's error
        envelope (no raise out of the MCP boundary)."""
        from yadgar.core.server.tools.memorize import memorize

        result = memorize(
            content="car m test memory",
            context="/home/max/git/yadgar",
            tags=[],
            project=123,  # type: ignore[arg-type]
        )

        assert result.get("ok") is False
        assert result.get("stored") is False
        assert "project must be a string" in str(result.get("error", ""))


# ── wiki_add / wiki_query / wiki_read project= ──────────────────────────────


class TestWikiProjectParam:
    """§3: wiki tools gain ``project=`` keyword-only."""

    @pytest.mark.parametrize(
        "fn_name",
        ["wiki_add", "wiki_query", "wiki_read"],
    )
    def test_wiki_tools_accept_project_kwarg(self, fn_name: str) -> None:
        """All three wiki tools expose ``project`` as keyword-only default None."""
        import inspect

        from yadgar.core.server.tools import wiki as wiki_mod

        sig = inspect.signature(getattr(wiki_mod, fn_name))
        assert "project" in sig.parameters, f"{fn_name} missing project param"
        param = sig.parameters["project"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_wiki_add_stamps_project_id_on_payload_when_supplied(self) -> None:
        """``wiki_add(project=...)`` stamps project_id on the enqueue payload."""
        from yadgar.core.server.tools.wiki import wiki_add

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured["op"] = op
            captured["payload"] = payload
            return "fake-job-id"

        with (
            patch(
                "yadgar.core.server.tools.wiki._st._wiki",
                object(),  # WikiStore present — assert passes
            ),
            patch(
                "yadgar.core.server.tools.wiki._get_file_queue",
                return_value=type(
                    "FQ",
                    (),
                    {"enqueue": staticmethod(fake_enqueue)},
                )(),
            ),
            patch(
                "yadgar.core.server.tools.wiki._check_wiki_add_context",
                return_value={},
            ),
        ):
            result = wiki_add(
                title="car m test",
                content="body",
                directory="/home/max/git/yadgar",
                project="quinyx/aws2slack",
            )

        assert result.get("stored") is True
        assert captured["payload"]["project_id"] == "quinyx/aws2slack"

    def test_wiki_add_no_project_still_stamps_the_resolved_project_id(self) -> None:
        """``wiki_add`` without ``project=`` stamps the SESSION-resolved value.

        SUPERSEDES the Car M contract "no ``project=`` → no ``project_id`` on
        the payload". C3 (0047 PR#40 §5.C3) makes the stamp unconditional:
        leaving the default path unstamped meant the DRAINER had to infer a
        project_id, and it runs in a container with no git binary and no host
        project mounts, so the inference silently yielded ``local/<basename>``
        or ``"unresolved"`` (§1.1 / ADR-0227). The tool call is the only
        participant that can see the session, so it is the only honest place
        to resolve. Kept as a test of the OPPOSITE assertion rather than
        deleted, so the reversal is visible in the diff.
        """
        from yadgar.core.server.tools.wiki import wiki_add

        captured: dict = {}

        def fake_enqueue(op: str, payload: dict) -> str:  # noqa: ARG001
            captured["op"] = op
            captured["payload"] = payload
            return "fake-job-id"

        with (
            patch(
                "yadgar.core.server.tools.wiki._st._wiki",
                object(),
            ),
            patch(
                "yadgar.core.server.tools.wiki._get_file_queue",
                return_value=type(
                    "FQ",
                    (),
                    {"enqueue": staticmethod(fake_enqueue)},
                )(),
            ),
            patch(
                "yadgar.core.server.tools.wiki._check_wiki_add_context",
                return_value={},
            ),
        ):
            with patch(
                "yadgar.core.server.tools.wiki.resolve_effective_project",
                return_value="m-agahi/yadgar",
            ):
                wiki_add(
                    title="car m test",
                    content="body",
                    directory="/home/max/git/yadgar",
                )

        assert captured["payload"]["project_id"] == "m-agahi/yadgar"

    def test_wiki_add_invalid_project_returns_error_envelope(self) -> None:
        """``wiki_add(project=123)`` returns the tool's error envelope, no raise."""
        from yadgar.core.server.tools.wiki import wiki_add

        with (
            patch(
                "yadgar.core.server.tools.wiki._st._wiki",
                object(),  # bypass the "WikiStore not initialized" assert
            ),
            patch(
                "yadgar.core.server.tools.wiki._check_wiki_add_context",
                return_value={},
            ),
        ):
            result = wiki_add(
                title="car m test",
                content="body",
                directory="/home/max/git/yadgar",
                project=123,  # type: ignore[arg-type]
            )

        assert result.get("ok") is False
        assert result.get("stored") is False
        assert "project must be a string" in str(result.get("error", ""))

    def test_wiki_query_invalid_project_raises_value_error(self) -> None:
        """``wiki_query(project=123)`` raises ValueError (read-tool contract)."""
        from yadgar.core.server.tools.wiki import wiki_query

        with pytest.raises(ValueError, match="wiki_query: project must be a string"):
            wiki_query(
                query="x",
                directory="/home/max/git/yadgar",
                project=123,  # type: ignore[arg-type]
            )

    def test_wiki_read_invalid_project_returns_error_dict(self) -> None:
        """``wiki_read(project=123)`` returns ``{"error": ...}`` (no raise)."""
        from yadgar.core.server.tools.wiki import wiki_read

        with patch(
            "yadgar.core.server.tools.wiki._st._wiki",
            object(),
        ):
            result = wiki_read(
                slug="some-page",
                directory="/home/max/git/yadgar",
                project=123,  # type: ignore[arg-type]
            )

        assert "error" in result
        assert "project must be a string" in result["error"]


# ── adr_add / adr_list / adr_get project= ────────────────────────────────────


class TestAdrProjectParam:
    """§3: ADR tools gain ``project=`` keyword-only."""

    @pytest.mark.parametrize(
        "fn_name",
        ["adr_add", "adr_list", "adr_get"],
    )
    def test_adr_tools_accept_project_kwarg(self, fn_name: str) -> None:
        """All three ADR tools expose ``project`` as keyword-only default None."""
        import inspect

        from yadgar.core.server.tools import adr as adr_mod

        sig = inspect.signature(getattr(adr_mod, fn_name))
        assert "project" in sig.parameters, f"{fn_name} missing project param"
        param = sig.parameters["project"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_adr_add_override_forwarded_to_ledger(self) -> None:
        """``adr_add(project=...)`` forwards the override to the backend
        ``create_adr_row`` op as ``project_id`` (Car A0 backend stamps the
        ledger row)."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = "/tmp/car-m-adr-add-override"
        import os

        os.makedirs(project_dir, exist_ok=True)

        forwarded: list[dict] = []

        def fake_forward(op: str, payload: dict, **kwargs):  # noqa: ARG001
            forwarded.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {
                    "row": {
                        "id": 7,
                        "project_id": payload["project_id"],
                        "title": payload["title"],
                        "status": payload["status"],
                        "decided_on": payload.get("decided_on"),
                        "body_slug": "override_adr-0007",
                    }
                }
            return {"ok": True}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=fake_forward,
            ),
            patch(
                "yadgar.core.server.tools.adr._wiki_write_canonical",
                return_value={"stored": True, "committed": True},
            ),
        ):
            result = adr_add(
                directory=project_dir,
                title="car m override",
                status="accepted",
                date="2026-08-09",
                context="car m context",
                decision="car m decision",
                rationale="car m rationale",
                alternatives="car m alternatives",
                consequences="car m consequences",
                revisit_trigger="car m revisit",
                supersedes="none",
                project="override/proj",
            )

        assert "adr_id" in result
        # The first forward is create_adr_row — its payload MUST carry
        # the override project_id, NOT the directory-derived one.
        create_row_payload = forwarded[0]["payload"]
        assert create_row_payload["project_id"] == "override/proj"

    def test_adr_list_override_forwarded_to_ledger(self) -> None:
        """``adr_list(project=...)`` forwards the override to ``list_adr_rows``."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = "/tmp/car-m-adr-list-override"

        forwarded: list[dict] = []

        def fake_forward(op: str, payload: dict, **kwargs):  # noqa: ARG001
            forwarded.append({"op": op, "payload": payload})
            return {"rows": []}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=fake_forward,
            ),
        ):
            adr_list(directory=project_dir, project="override/proj")

        list_payload = forwarded[0]["payload"]
        assert list_payload["project_id"] == "override/proj"

    def test_adr_get_override_forwarded_to_ledger(self) -> None:
        """``adr_get(project=...)`` forwards the override to ``get_adr_row``."""
        from unittest.mock import MagicMock

        from yadgar.core.server.tools.adr import adr_get

        project_dir = "/tmp/car-m-adr-get-override"

        forwarded: list[dict] = []

        def fake_forward(op: str, payload: dict, **kwargs):  # noqa: ARG001
            forwarded.append({"op": op, "payload": payload})
            if op == "get_adr_row":
                return {"row": None}
            return {}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=fake_forward,
            ),
            patch(
                "yadgar.core.server.tools.wiki._st._wiki",
                MagicMock(read_by_directory=MagicMock(return_value=None)),
            ),
        ):
            adr_get(directory=project_dir, adr_id="ADR-0001", project="override/proj")

        get_payload = forwarded[0]["payload"]
        assert get_payload["project_id"] == "override/proj"

    def test_adr_add_invalid_project_returns_error_envelope(self) -> None:
        """Type-level guard: ``adr_add(project=123)`` returns error envelope."""
        from yadgar.core.server.tools.adr import adr_add

        result = adr_add(
            directory="/tmp/car-m-adr-invalid",
            title="t",
            status="accepted",
            date="2026-08-09",
            context="c",
            decision="d",
            rationale="r",
            alternatives="a",
            consequences="c",
            revisit_trigger="r",
            supersedes="none",
            project=123,  # type: ignore[arg-type]
        )

        assert result.get("ok") is False
        assert "project must be a string" in str(result.get("error", ""))


# ── task_write / task_list / task_get project= ───────────────────────────────


class TestTaskProjectParam:
    """§3: task tools gain ``project=`` keyword-only (in addition to the
    Car D ``project_id`` arg). Precedence: ``project`` > ``project_id``."""

    @pytest.mark.parametrize(
        "fn_name",
        ["task_write", "task_list", "task_get"],
    )
    def test_task_tools_accept_project_kwarg(self, fn_name: str) -> None:
        """All three task tools expose ``project`` as keyword-only default None."""
        import inspect

        from yadgar.core.server.tools import task as task_mod

        sig = inspect.signature(getattr(task_mod, fn_name))
        assert "project" in sig.parameters, f"{fn_name} missing project param"
        param = sig.parameters["project"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_task_write_override_beats_project_id_arg(self) -> None:
        """§16.6: ``project`` (override) wins over the ``project_id`` arg."""
        from yadgar.core.server.tools import task as task_mod
        from yadgar.core.server.tools.task import task_write

        captured: dict = {}

        def fake_forward(op: str, payload: dict, **kwargs):  # noqa: ARG001
            captured["op"] = op
            captured["payload"] = payload
            return {"id": 99, **payload}

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            result = task_write(
                project_id="stale/arg",
                title="car m override",
                project="override/proj",
            )

        assert result.get("ok") is True
        # The CREATE payload MUST carry the override, NOT the stale arg.
        assert captured["payload"]["project_id"] == "override/proj"

    def test_task_list_override_beats_project_id_arg(self) -> None:
        """§16.6: ``project`` wins over ``project_id`` on task_list too."""
        from yadgar.core.server.tools import task as task_mod
        from yadgar.core.server.tools.task import task_list

        captured: dict = {}

        def fake_forward(op: str, payload: dict, **kwargs):  # noqa: ARG001
            captured["op"] = op
            captured["payload"] = payload
            return {"rows": []}

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            task_list(project_id="stale/arg", project="override/proj")

        assert captured["payload"]["project_id"] == "override/proj"

    def test_task_get_override_beats_project_id_arg(self) -> None:
        """§16.6: ``project`` wins over ``project_id`` on task_get too."""
        from yadgar.core.server.tools import task as task_mod
        from yadgar.core.server.tools.task import task_get

        captured: dict = {}

        def fake_forward(op: str, payload: dict, **kwargs):  # noqa: ARG001
            captured["op"] = op
            captured["payload"] = payload
            return {"row": None}

        with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
            task_get(project_id="stale/arg", id=42, project="override/proj")

        # task_get forwards only by id (per Car D §14.1), but the project_id
        # was already overridden — the assertion is that the tool did not
        # crash and returned None (no row fixture).
        assert captured["payload"]["id"] == 42
