"""C0 (2026-08-22 train) — ``wiki_add(project=...)`` satisfies the directory gate.

Before this car the gate at ``_check_wiki_add_context`` (yadgar/core/server/tools/wiki.py)
rejected any call with an empty ``directory`` BEFORE the resolver ran, even when the
caller had supplied a valid ``project=`` override. The MCP transport never sees
``directory``, so every ``mcp__yadgar__wiki_add(project="m-agahi/yadgar", ...)`` came
back with the resolver's ``{"stored": False, "ok": False, "error": "unresolved_project"}``
envelope — even though the override was sufficient to resolve.

RED → GREEN: this file was written before the implementation.

Two assertions cover the contract:

1. ``test_wiki_add_project_override_satisfies_directory_gate`` — explicit ``project=``
   with no ``directory=`` MUST NOT short-circuit at the gate. Pre-fix: gate returns
   the unresolved envelope (red). Post-fix: gate returns ``({}, resolved_id)`` and
   the call advances to the secret/size gates or the registry check.

2. ``test_wiki_add_directory_None_without_project_still_rejects`` — no ``project=``
   AND no ``directory=`` still rejects. The fix must not accidentally open the gate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

_REGISTERED = "m-agahi/yadgar"
_REGISTRY_ROWS = {"rows": [{"key": _REGISTERED}, {"key": "quinyx/aws2slack"}]}


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Every test starts with a cold registry cache (mirror Car 5)."""
    from yadgar.core.server.tools._project_registry import invalidate_project_registry_cache

    invalidate_project_registry_cache()
    yield
    invalidate_project_registry_cache()


class TestDirectoryGateAcceptsProjectOverride:
    """The directory gate must be transparent to a valid ``project=``."""

    def test_project_override_alone_passes_the_gate(self) -> None:
        """``project=`` alone must satisfy the gate; the resolved id flows back."""
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context(None, project=_REGISTERED)
        assert decision == {}, f"gate must accept a valid project override, got {decision}"
        assert resolved == _REGISTERED

    def test_empty_directory_with_project_passes_the_gate(self) -> None:
        """Empty string is treated the same as None — the override still wins."""
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context("", project=_REGISTERED)
        assert decision == {}
        assert resolved == _REGISTERED

    def test_whitespace_directory_with_project_passes_the_gate(self) -> None:
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context("   ", project=_REGISTERED)
        assert decision == {}
        assert resolved == _REGISTERED

    def test_a_real_directory_still_returns_no_gate_decision(self) -> None:
        """A non-empty ``directory=`` is unchanged: gate returns ``({}, None)``
        and the call site resolves on its own."""
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context("/home/max/git/yadgar", project=_REGISTERED)
        assert decision == {}
        assert resolved is None


class TestDirectoryGateStillRejectsWithoutProject:
    """The fix must not open the gate to unscoped callers."""

    def test_directory_None_no_project_rejects(self) -> None:
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context(None)
        assert "error" in decision
        assert decision["error"] == "unresolved_project"
        assert resolved is None

    def test_empty_directory_no_project_rejects(self) -> None:
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, _ = _check_wiki_add_context("")
        assert decision.get("error") == "unresolved_project"

    def test_sentinel_project_still_rejects_at_resolution(self) -> None:
        """A sentinel ``project=`` must NOT be accepted by the gate — the
        resolver raises ``InvalidProjectOverrideError`` and the gate surfaces
        it as ``invalid_project_override`` (finding #3: the override's own
        envelope, not the unrelated ``unresolved_project`` envelope)."""
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        for sentinel in ("global", "unresolved", "system"):
            decision, resolved = _check_wiki_add_context(None, project=sentinel)
            assert decision.get("error") == "invalid_project_override", (
                f"sentinel {sentinel!r} must reject as invalid override; got {decision!r}"
            )
            assert resolved is None

    def test_empty_string_project_rejects(self) -> None:
        """Empty-string ``project=""`` is "treated as a present-but-invalid
        override" — the resolver raises ``InvalidProjectOverrideError`` and
        the gate surfaces it as ``invalid_project_override`` (the override's
        own envelope, not the missing-identity envelope). Pre-split, this
        path was bundled with ``project=None`` into the early reject at
        wiki.py:117; splitting the predicate (PR #65 review finding #3)
        routes empty-string through the override branch.
        """
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context(None, project="")
        assert decision.get("error") == "invalid_project_override", (
            f"empty-string project='' must reject as an invalid override (the "
            f"callers DID pass it as project=, just with no characters); "
            f"got {decision!r}"
        )
        assert resolved is None

    # Whitespace-only ``project='   '`` is NOT covered by finding #3: the
    # resolver returns it literally (line 183: ``if not project:`` is False
    # for "   "), so the gate accepts it as an identity and hands it off to
    # the registry check. Whitespace handling would be a SEPARATE finding
    # about ``project_id_value_error`` (which also uses bare ``not value``,
    # not ``not value.strip()``) — outside the scope of finding #3.


class TestInvalidProjectOverrideTypeReportsTypeError:
    """PR #65 review finding #3: ``project=123`` (non-string) raises
    ``InvalidProjectOverrideError`` at the resolver; the gate's
    ``except InvalidProjectOverrideError`` at wiki.py:130 currently catches
    it AND remaps to ``{"error": "unresolved_project"}``. That's wrong: a
    caller passing a non-string is a TYPE defect, not an "unresolved
    project" defect. The caller reads "you forgot to pass a project" and
    might add the wrong fix.

    The fix: when the override raises, return the override envelope
    directly (its own error_code), do NOT remap to unresolved_project.
    """

    def test_non_string_project_does_NOT_report_unresolved_project(self) -> None:
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context(None, project=123)  # int, not str
        # Pre-fix bug: assert below passes because the code remaps to
        # unresolved_project. Post-fix: the error name reflects the actual
        # defect (invalid override / type / caller's project= wasn't a string).
        assert decision.get("error") != "unresolved_project", (
            f"non-string project= must NOT be reported as unresolved_project; "
            f"a missing project and a wrong-type project are different defects; "
            f"got {decision!r}"
        )
        assert resolved is None

    def test_empty_string_project_does_NOT_report_unresolved_project(self) -> None:
        """Empty-string ``project=""`` is a present-and-invalid override — the
        resolver raises ``InvalidProjectOverrideError`` and the gate surfaces
        ``invalid_project_override``. Same logic as ``project=123``: the
        override raised, so the override's own envelope wins.
        """
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context(None, project="")
        assert decision.get("error") != "unresolved_project", (
            f"empty-string project='' must NOT be reported as "
            f"unresolved_project; it's an invalid override, not a missing "
            f"identity; got {decision!r}"
        )
        assert resolved is None


class TestWikiAddEndToEnd:
    """``wiki_add`` no longer returns the gate envelope when ``project=`` is supplied."""

    def test_wiki_add_project_override_satisfies_directory_gate(self) -> None:
        """End-to-end: a real ``wiki_add`` call with ``project=`` and no ``directory=``
        must NOT return the gate's reject envelope. Pre-fix this is red; post-fix
        it advances to the secret/size gate which passes for trivial content.

        We patch the FileQueue.enqueue to capture the payload — the call MUST
        reach enqueue (proving the gate accepted), and the payload must carry
        the resolved ``project_id``.
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
                "yadgar.core.server.tools.wiki._forward_admin",
                return_value=dict(_REGISTRY_ROWS),
            ),
        ):
            result = wiki_add(
                title="c0 project override probe",
                content="probe body — must reach enqueue",
                directory=None,
                project=_REGISTERED,
            )

        # Pre-fix: result == {"stored": False, "ok": False, "error": "unresolved_project"}
        # Post-fix: result == {"stored": True, "queued": True, ...} and the captured
        # payload carries the resolved project_id.
        assert result.get("error") != "unresolved_project", (
            f"gate must accept project= override, got {result!r}"
        )
        assert captured, "wiki_add must reach enqueue when the gate accepts"
        assert captured["payload"]["project_id"] == _REGISTERED

    def test_wiki_add_directory_None_without_project_still_rejects(self) -> None:
        """Mirror assertion: without ``project=`` the gate still rejects. Guards
        against the fix accidentally opening the gate to unscoped callers."""
        from yadgar.core.server.tools.wiki import wiki_add

        with patch(
            "yadgar.core.server.tools.wiki._st._wiki",
            object(),
        ):
            result = wiki_add(
                title="c0 no-override probe",
                content="x",
                directory=None,
            )

        assert result.get("error") == "unresolved_project"
        assert result.get("stored") is False
