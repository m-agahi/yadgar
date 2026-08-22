"""Car C9 task 71 — ``wiki_append_section`` must cap the appended body.

Pre-fix: ``wiki_append_section`` forwarded ``content`` to the backend with no
size check. A 50 KB body would grow the page past the read cap (task 70,
``_WIKI_READ_CONTENT_CAP_BYTES = 8_192``) — the append "succeeded" but every
subsequent ``wiki_read`` returns a truncated view with ``content_truncated:
True``, making the page effectively unreadable until it is rewritten.

Post-fix: the cap is enforced BEFORE the forward (line ~1594 of
``yadgar/core/server/tools/wiki.py``), so the call returns
``{"error": "section_content_too_large", "content_bytes": N, "max_bytes": 8192}``
and never reaches the backend. The constant is the same one the read path
uses; bumping one without the other would re-open this defect.

BOTH DIRECTIONS PINNED:
- Under-cap body (≤ 8 KB) forwards unchanged — no regression.
- Over-cap body (≥ 8 KB) returns the refusal envelope, never calls the
  forwarder (verified by stubbing it).

The forwarder is a stub that records calls; if the cap regresses, the stub
fires and the test fails with a clear "we should have refused" message.
"""

from __future__ import annotations

from typing import Any

import pytest

_YADGAR_PROJECT = "m-agahi/yadgar"
_YADGAR_DIR = "/home/max/git/yadgar"
_FLUX_PROJECT = "quinyx/flux"
_FLUX_SLUG = "quinyx_flux_adr-0016"


class _FakeForwarder:
    """Captures calls so a cap regression cannot silently route to backend."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, payload))
        return {
            "page_id": 7,
            "new_version": 2,
            "section_heading": payload.get("section_heading", ""),
            "action": "appended",
            "size_before": 100,
            "size_after": 100 + len(payload.get("content") or ""),
        }


class _FakeWikiStore:
    """Resolution only — slug + project key, like the real backend."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def read_by_project(self, slug: str, project_id: str | None) -> dict | None:
        for p in self.pages:
            if p["slug"] == slug and project_id and p["project_id"] == project_id:
                return dict(p)
        return None

    # _resolve_page_id_by_slug falls back here when the project rung misses
    # (the tolerant scope resolver degrades to None on UnresolvedProjectError),
    # and the call site passes directory=None. Match by slug only — unique
    # corpus-wide (2523 distinct per the live audit 2026-08-19).
    def read_by_directory(self, slug: str, directory: str | None) -> dict | None:
        for p in self.pages:
            if p["slug"] == slug:
                return dict(p)
        return None


@pytest.fixture
def wiki_tool(monkeypatch):
    """Return ``(wiki_module, wire)`` — installs a wiki store + forwarder stub."""
    import yadgar._shared.runtime.state as _state
    from yadgar._shared.runtime import cache_epoch as _epoch_bus
    from yadgar.core.server.tools import wiki as wtool

    forwarder = _FakeForwarder()
    monkeypatch.setattr(wtool, "_forward_admin", forwarder)
    monkeypatch.setattr(_state, "_wiki", _FakeWikiStore([]))
    _epoch_bus._reset_for_test()

    def wire(pages: list[dict[str, Any]]) -> _FakeForwarder:
        fake = _FakeWikiStore(pages)
        monkeypatch.setattr(_state, "_wiki", fake)
        _epoch_bus._reset_for_test()
        return forwarder

    return wtool, wire


# ── under cap: forwards normally ────────────────────────────────────────────


class TestUnderCapForwards:
    def test_under_cap_body_reaches_forwarder(self, wiki_tool):
        """A small section body must forward unchanged — the cap must not
        leak into the under-cap path."""
        wtool, wire = wiki_tool
        wire(
            [
                {
                    "id": 7,
                    "slug": _FLUX_SLUG,
                    "title": _FLUX_SLUG,
                    "content": "old",
                    "project_id": _FLUX_PROJECT,
                    "directory_context": "/home/max/quinyx/flux",
                    "tags": [],
                }
            ]
        )

        body = "## New section\n\nSome prose."  # well under 8 KB
        result = wtool.wiki_append_section(_FLUX_SLUG, "New section", body, project=_FLUX_PROJECT)

        assert "error" not in result, f"under-cap append regressed: {result!r}"
        assert "section_content_too_large" not in result.get("error", "")
        assert result.get("action") == "appended", (
            f"under-cap append must forward to the backend; got {result!r}"
        )

    def test_under_cap_body_passes_through_unchanged(self, wiki_tool):
        """Pin: the cap does NOT mutate the body before forwarding. A
        regression that truncated under-cap input would land here."""
        wtool, wire = wiki_tool
        forwarder = wire(
            [
                {
                    "id": 7,
                    "slug": _FLUX_SLUG,
                    "title": _FLUX_SLUG,
                    "content": "old",
                    "project_id": _FLUX_PROJECT,
                    "directory_context": "/home/max/quinyx/flux",
                    "tags": [],
                }
            ]
        )

        body = "x" * 8_000  # exactly at the cap, must pass
        wtool.wiki_append_section(_FLUX_SLUG, "Heading", body, project=_FLUX_PROJECT)

        assert len(forwarder.calls) == 1, (
            f"under-cap body must call forwarder exactly once; got {len(forwarder.calls)}"
        )
        _, payload = forwarder.calls[0]
        assert payload["content"] == body, (
            "under-cap body must be forwarded unchanged; if this fails, the cap "
            "regressed and started slicing legitimate input"
        )


# ── over cap: refuses, never forwards ───────────────────────────────────────


class TestOverCapRefuses:
    def test_over_cap_body_returns_refusal_envelope(self, wiki_tool):
        """A body over the cap must return the refusal — same shape as the
        pre-existing ``wiki_add`` cap refusal at line 267 of tools/wiki.py."""
        wtool, wire = wiki_tool
        wire(
            [
                {
                    "id": 7,
                    "slug": _FLUX_SLUG,
                    "title": _FLUX_SLUG,
                    "content": "old",
                    "project_id": _FLUX_PROJECT,
                    "directory_context": "/home/max/quinyx/flux",
                    "tags": [],
                }
            ]
        )

        body = "y" * 16_384  # 16 KB, twice the cap
        result = wtool.wiki_append_section(_FLUX_SLUG, "Heading", body, project=_FLUX_PROJECT)

        assert result.get("error") == "section_content_too_large", (
            f"over-cap append must refuse with the named error; got {result!r}"
        )
        assert result.get("content_bytes") == 16_384
        assert result.get("max_bytes") == 8_192

    def test_over_cap_body_never_reaches_forwarder(self, wiki_tool):
        """Pin the consequence: a refused append must not hit the backend.
        If the cap regresses to a warn-and-continue, this fires."""
        wtool, wire = wiki_tool
        forwarder = wire(
            [
                {
                    "id": 7,
                    "slug": _FLUX_SLUG,
                    "title": _FLUX_SLUG,
                    "content": "old",
                    "project_id": _FLUX_PROJECT,
                    "directory_context": "/home/max/quinyx/flux",
                    "tags": [],
                }
            ]
        )

        body = "y" * 9_000  # 1 KB over the cap
        result = wtool.wiki_append_section(_FLUX_SLUG, "Heading", body, project=_FLUX_PROJECT)

        assert result.get("error") == "section_content_too_large"
        assert forwarder.calls == [], (
            f"refused append must NOT call forwarder; got {forwarder.calls!r}"
        )

    def test_4byte_chars_counted_as_bytes(self, wiki_tool):
        """Pin the byte-vs-char choice on the WRITE side. 4-byte emoji
        count by UTF-8 byte length (matching the read cap). 2000 emoji
        = 8000 bytes (under the 8192-byte cap, passes); 3000 emoji =
        12000 bytes (well over, refuses)."""
        wtool, wire = wiki_tool
        forwarder = wire(
            [
                {
                    "id": 7,
                    "slug": _FLUX_SLUG,
                    "title": _FLUX_SLUG,
                    "content": "old",
                    "project_id": _FLUX_PROJECT,
                    "directory_context": "/home/max/quinyx/flux",
                    "tags": [],
                }
            ]
        )

        under_cap = "🚀" * 2000  # 8000 bytes — under the 8192 cap
        result_under = wtool.wiki_append_section(
            _FLUX_SLUG, "Heading", under_cap, project=_FLUX_PROJECT
        )
        assert "error" not in result_under, f"8000 bytes must pass; got {result_under!r}"

        over_cap = "🚀" * 3000  # 12000 bytes — over the cap
        result_over = wtool.wiki_append_section(
            _FLUX_SLUG, "Heading 2", over_cap, project=_FLUX_PROJECT
        )
        assert result_over.get("error") == "section_content_too_large"
        assert result_over.get("content_bytes") == 12_000
        assert len(forwarder.calls) == 1, (
            "only the under-cap body must forward; over-cap must refuse"
        )
