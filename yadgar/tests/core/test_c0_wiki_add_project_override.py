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
        resolver raises ``InvalidProjectOverrideError`` and we surface it as
        ``unresolved_project`` (the documented envelope)."""
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        for sentinel in ("global", "unresolved", "system"):
            decision, resolved = _check_wiki_add_context(None, project=sentinel)
            assert decision.get("error") == "unresolved_project", (
                f"sentinel {sentinel!r} must reject"
            )
            assert resolved is None

    def test_empty_string_project_rejects(self) -> None:
        """An empty-string ``project=`` is treated as no override — still rejects."""
        from yadgar.core.server.tools.wiki import _check_wiki_add_context

        decision, resolved = _check_wiki_add_context(None, project="")
        assert decision.get("error") == "unresolved_project"
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
