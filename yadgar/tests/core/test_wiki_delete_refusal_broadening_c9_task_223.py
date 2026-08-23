"""Car C9 task 223 — ``wiki_delete`` must surface a mutability refusal as a
refusal envelope, NOT as "Wiki page not found".

Pre-fix: ``wiki_delete``'s post-processing guard at line 1076 of
``yadgar/core/server/tools/wiki.py`` checked ONLY ``_res.get("refused")``.
A backend that returned the structured envelope with ``refused=True``
AND ``reason="wiki_page_locked"`` worked; an envelope variant that
carried ``reason`` but dropped the marker (or a future embed-service
refactor that dropped ``refused``) would silently fall through to the
``{"deleted": False, "error": "Wiki page '...' not found"}`` branch —
swapping the old bare HTTP 500 for a wrong answer. The defect class is
"correct-state refusal rendered as server fault or as a lie".

Post-fix: the guard keys on either ``refused=True`` OR a present
``reason`` field, so the live envelope shape AND any envelope-shape
drift still short-circuit BEFORE the "deleted"/"not found" branch.

BOTH DIRECTIONS PINNED:
- A refused envelope (``refused=True`` + ``reason=...``) returns the
  envelope intact. No "not found", no SSE push, no file-queue cleanup.
- A free-page delete (``deleted=True`` envelope) returns ``{deleted: True}``
  and DOES push the SSE event + cleanup the file-queue mirror.
- A not-found slug (``deleted=False`` envelope, no refusal markers)
  returns the legacy "not found" envelope untouched by the broadening.

The forwarder is a stub that records calls + lets each test choose its
envelope. If the broadening regresses, the refused-envelope test would
fall through and report "Wiki page not found" — a clean failure signal.
"""

from __future__ import annotations

from typing import Any

import pytest

_LOCKED_SLUG = "m-agahi_yadgar_adr-0007"
_FREE_SLUG = "m-agahi_yadgar_task-50"
_MISSING_SLUG = "m-agahi_yadgar_does-not-exist"


class _StubForwarder:
    """Returns the envelope the test pinned (refused | deleted | not-found)."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, payload))
        return self.envelope


class _StubFileQueue:
    """Records cleanup calls so a regression in the SSE/file-queue path fires."""

    def __init__(self) -> None:
        self.deleted_slugs: list[str] = []
        self.raise_on_call: Exception | None = None

    def delete_wiki(self, slug: str) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.deleted_slugs.append(slug)


class _StubPusher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest.fixture
def wiki_tool(monkeypatch):
    """Return ``(wiki_module, wire)`` — installs a forwarder + file-queue stub.

    ``wire(envelope)`` configures the forwarder envelope and returns
    ``(forwarder, pusher, file_queue)`` so each test can both pin the
    behaviour AND verify side-effects (or absence thereof).
    """
    from yadgar.core.server.tools import wiki as wtool

    forwarder = _StubForwarder(envelope={})
    pusher = _StubPusher()
    file_queue = _StubFileQueue()

    monkeypatch.setattr(wtool, "_forward_admin", forwarder)
    monkeypatch.setattr(wtool, "_push_event", pusher)
    monkeypatch.setattr(wtool, "_get_file_queue", lambda: file_queue)

    def wire(envelope: dict[str, Any]) -> tuple[_StubForwarder, _StubPusher, _StubFileQueue]:
        forwarder.envelope = envelope
        forwarder.calls.clear()
        pusher.events.clear()
        file_queue.deleted_slugs.clear()
        return forwarder, pusher, file_queue

    return wtool, wire


# ── live envelope shape (refused=True + reason) ─────────────────────────────


class TestLiveEnvelopeReturnsRefusal:
    def test_locked_page_returns_envelope_intact(self, wiki_tool):
        """The live 409 envelope shape carries BOTH ``refused=True`` and
        ``reason="wiki_page_locked"``. ``wiki_delete`` must return it
        verbatim — not the legacy "not found" envelope, not a 500."""
        wtool, wire = wiki_tool
        envelope = {
            "ok": False,
            "refused": True,
            "reason": "wiki_page_locked",
            "mutability": "locked",
            "page_id": 7,
            "slug": _LOCKED_SLUG,
            "page_type": "adr",
            "wiki_op": "delete_wiki_page",
            "op": "wiki_delete",
            "error": "wiki page is locked; use wiki_set_mutability to unlock",
        }
        forwarder, _pusher, _fq = wire(envelope)

        result = wtool.wiki_delete(_LOCKED_SLUG)

        assert result == envelope, f"locked-page refusal must be returned verbatim; got {result!r}"
        assert "deleted" not in result, (
            f"locked-page refusal must NOT carry a 'deleted' key (would mis-read as 'not found'); got {result.get('deleted')!r}"
        )

    def test_locked_page_does_not_push_sse_or_cleanup_file_queue(self, wiki_tool):
        """Pin the consequence: a refused delete must NOT push ``wiki_deleted``
        AND must NOT touch the file-queue mirror. A regression where the
        broadening was applied AFTER the side-effect calls would still
        mis-classify the row as deleted."""
        wtool, wire = wiki_tool
        envelope = {
            "ok": False,
            "refused": True,
            "reason": "wiki_page_locked",
            "mutability": "locked",
            "page_id": 7,
            "slug": _LOCKED_SLUG,
            "page_type": "adr",
            "wiki_op": "delete_wiki_page",
            "op": "wiki_delete",
            "error": "locked",
        }
        _forwarder, pusher, fq = wire(envelope)

        wtool.wiki_delete(_LOCKED_SLUG)

        assert pusher.events == [], f"refused delete must NOT push SSE event; got {pusher.events!r}"
        assert fq.deleted_slugs == [], (
            f"refused delete must NOT cleanup file-queue mirror; got {fq.deleted_slugs!r}"
        )


# ── envelope variant: reason WITHOUT refused marker ─────────────────────────


class TestEnvelopeVariantReturnsRefusal:
    def test_reason_without_refused_marker_still_short_circuits(self, wiki_tool):
        """Defence-in-depth case: a future envelope shape carries
        ``reason`` but drops ``refused``. The broadened guard must still
        recognise it as a refusal — otherwise a future refactor of
        ``yadgar/core/forward.py:115-120`` would silently re-open the
        bare-500 defect."""
        wtool, wire = wiki_tool
        envelope = {
            "ok": False,
            # NOTE: no ``refused`` key — simulates envelope-shape drift
            "reason": "wiki_page_locked",
            "mutability": "locked",
            "slug": _LOCKED_SLUG,
            "page_type": "adr",
            "error": "locked",
        }
        forwarder, pusher, fq = wire(envelope)

        result = wtool.wiki_delete(_LOCKED_SLUG)

        assert result is envelope or result == envelope, (
            f"reason-bearing envelope must be returned as a refusal; got {result!r}. "
            f"If you see {{'deleted': False, 'error': 'not found'}} here, the "
            f"broadened guard regressed to the narrow version."
        )
        assert "deleted" not in result, (
            f"reason-bearing envelope must NOT be mis-classified as 'not found'; got {result.get('deleted')!r}"
        )
        assert pusher.events == [], "no SSE push on a refused delete"
        assert fq.deleted_slugs == [], "no file-queue cleanup on a refused delete"


# ── free-page success path is untouched by the broadening ──────────────────


class TestFreePageDeleteStillWorks:
    def test_deleted_envelope_pushes_sse_and_cleans_mirror(self, wiki_tool):
        """Pin the negative direction: a legitimate delete (envelope has
        ``deleted=True`` + no refusal markers) must STILL push the SSE
        event and cleanup the file-queue mirror. The broadening must NOT
        swallow the success path."""
        wtool, wire = wiki_tool
        envelope = {
            "ok": True,
            "deleted": True,
            "slug": _FREE_SLUG,
        }
        forwarder, pusher, fq = wire(envelope)

        result = wtool.wiki_delete(_FREE_SLUG)

        assert result == {"deleted": True, "slug": _FREE_SLUG}, (
            f"successful delete must return the success envelope; got {result!r}"
        )
        assert len(pusher.events) == 1, (
            f"successful delete must push ONE wiki_deleted event; got {pusher.events!r}"
        )
        assert pusher.events[0] == {"event": "wiki_deleted", "slug": _FREE_SLUG}
        assert fq.deleted_slugs == [_FREE_SLUG], (
            f"successful delete must cleanup the file-queue mirror; got {fq.deleted_slugs!r}"
        )


# ── legacy not-found path is untouched by the broadening ────────────────────


class TestNotFoundPathUntouched:
    def test_not_found_envelope_carries_no_refusal_markers(self, wiki_tool):
        """Pin: a non-existent slug returns the legacy ``{deleted: False,
        error: 'not found'}`` envelope. No ``reason`` field, no
        ``refused=True`` — the broadened guard must NOT mis-fire here."""
        wtool, wire = wiki_tool
        envelope = {
            "ok": True,
            "deleted": False,
            "slug": _MISSING_SLUG,
        }
        forwarder, pusher, fq = wire(envelope)

        result = wtool.wiki_delete(_MISSING_SLUG)

        assert result.get("deleted") is False, (
            f"not-found delete must return deleted=False; got {result.get('deleted')!r}"
        )
        assert "not found" in result.get("error", "").lower(), (
            f"not-found envelope must carry the 'not found' error; got {result.get('error')!r}"
        )
        assert pusher.events == [], "no SSE push on a not-found slug"
        assert fq.deleted_slugs == [], "no file-queue cleanup on a not-found slug"


# ── the original narrow contract still works (regression guard) ─────────────


class TestNarrowContractRegressionGuard:
    def test_refused_true_alone_is_still_a_refusal(self, wiki_tool):
        """Pre-fix correctness: an envelope with ONLY ``refused=True``
        (no ``reason``) — the most minimal possible refusal marker —
        must STILL be treated as a refusal. If a future cleanup removes
        the ``or _res.get("reason")`` arm but leaves the original
        ``_res.get("refused")`` check, this pins it."""
        wtool, wire = wiki_tool
        envelope = {
            "ok": False,
            "refused": True,
            # NOTE: no ``reason`` field — minimal live envelope shape
            "slug": _LOCKED_SLUG,
        }
        forwarder, pusher, fq = wire(envelope)

        result = wtool.wiki_delete(_LOCKED_SLUG)

        assert result.get("refused") is True, (
            f"minimal refused envelope must still be treated as refusal; got {result!r}"
        )
        assert "deleted" not in result, (
            f"minimal refused envelope must NOT carry a 'deleted' key; got {result.get('deleted')!r}"
        )
        assert pusher.events == []
        assert fq.deleted_slugs == []


# ── success-with-reason must NOT be mis-classified as a refusal ──────────────


class TestSuccessWithReasonNotARefusal:
    def test_deleted_envelope_with_reason_does_not_short_circuit(self, wiki_tool):
        """PR #65 review finding #5: the broadened guard keys on
        ``refused`` OR ``reason``. A hypothetical future success envelope
        that carries ``deleted=True`` alongside a benign ``reason``
        string (e.g. an audit-trail field, a stale-reap note) must not
        be mis-classified as a refusal — the SSE push + file-queue
        cleanup must fire.

        The refusal contract (refusal.py:107) keys ONLY on ``refused``.
        Short-circuiting on a stray ``reason`` field in a success
        envelope invents a contract the live code does not speak.
        """
        wtool, wire = wiki_tool
        envelope = {
            "ok": True,
            "deleted": True,
            "slug": _FREE_SLUG,
            # Hypothetical future audit field — NOT a refusal marker.
            # The live refusal contract keys on ``refused`` only; a
            # success envelope carrying a ``reason`` field must be
            # processed as a success, not short-circuited.
            "reason": "auto-stale-cleanup",
        }
        _forwarder, pusher, fq = wire(envelope)

        result = wtool.wiki_delete(_FREE_SLUG)

        assert result == {"deleted": True, "slug": _FREE_SLUG}, (
            f"success-with-reason envelope must NOT be mis-classified; got {result!r}. "
            f"If you see the audit-shaped envelope echoed back here, the guard "
            f"short-circuited on a stray 'reason' field on the success path."
        )
        assert len(pusher.events) == 1, (
            f"success-with-reason must push ONE wiki_deleted SSE event; got {pusher.events!r}. "
            f"A short-circuit would suppress this."
        )
        assert pusher.events[0] == {"event": "wiki_deleted", "slug": _FREE_SLUG}
        assert fq.deleted_slugs == [_FREE_SLUG], (
            f"success-with-reason must cleanup the file-queue mirror; got {fq.deleted_slugs!r}. "
            f"A short-circuit would suppress this too."
        )
