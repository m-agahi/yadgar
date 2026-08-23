"""Car C9 task 70 — ``wiki_read`` must cap ``content`` to a single-payload window.

Pre-fix: ``wiki_read`` returned the FULL ``content`` blob (only ``embedding``
was stripped). A 50 KB ADR rollup page therefore dragged 50 KB over the MCP
boundary on every call, with no opt-out. The truncation target is
``_WIKI_READ_CONTENT_CAP_BYTES = 8_192`` (set in
``yadgar/core/server/tools/wiki.py:691-697``) — below the write-side
``content_too_large`` ceiling of 65 536 (line 267, 554) so legacy over-size
rows still get truncated, above ``wiki_find_similar_pages``'s 4 000-byte
search window (``_shared/wiki/store.py:1239``) so full-page reads stay useful.

The cap is applied BEFORE the cache put, so a warm hit returns the same
truncated view as the cold hit (consistent across hits/misses — no race
where one call sees full and the next sees truncated).

BOTH DIRECTIONS PINNED:
- A small page (< 8 KB) returns byte-identical — no `content_truncated` key.
- A large page (>= 8 KB) returns truncated content + the markers.

The pre-fix RED is a behavioural "content is the full uncut blob" — same
shape as the measured live failure (the 50 KB drag), never an AttributeError
that would only prove a new field is absent.
"""

from __future__ import annotations

from typing import Any

import pytest

# ── the corpus this car reasons about (shapes taken from live rows) ────────

_YADGAR_PROJECT = "m-agahi/yadgar"
_YADGAR_DIR = "/home/max/git/yadgar"
_FLUX_PROJECT = "quinyx/flux"
_FLUX_SLUG = "quinyx_flux_adr-0016"


def _page(
    slug: str,
    project_id: str,
    directory_context: str,
    *,
    content: str = "body",
) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": slug,
        "content": content,
        "project_id": project_id,
        "directory_context": directory_context,
        "tags": [],
    }


class _FakeWikiStore:
    """Minimal stand-in — ``wiki_read`` only consults ``read_by_project``."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def read_by_project(self, slug: str, project_id: str | None) -> dict | None:
        for p in self.pages:
            if p["slug"] == slug and project_id and p["project_id"] == project_id:
                return dict(p)
        return None


@pytest.fixture
def wiki_tool(monkeypatch):
    """Return ``(wiki_module, wire)``; ``wire(pages)`` installs the fake store."""
    import yadgar._shared.runtime.state as _state
    from yadgar._shared.runtime import cache_epoch as _epoch_bus
    from yadgar.core.server.tools import wiki as wtool

    monkeypatch.setattr(_state, "_wiki", _FakeWikiStore([]))
    # Pin the epoch bus so the read cache key is stable across the call pairs
    # in this file (cold then warm hit, etc.) and a sibling test's write
    # cannot move it mid-test.
    _epoch_bus._reset_for_test()

    def wire(pages: list[dict[str, Any]]) -> _FakeWikiStore:
        fake = _FakeWikiStore(pages)
        monkeypatch.setattr(_state, "_wiki", fake)
        # Bumping the epoch flushes the read cache from any prior test
        # sharing the module-level singleton.
        _epoch_bus._reset_for_test()
        return fake

    return wtool, wire


# ── small page: cap does NOT fire ───────────────────────────────────────────


class TestSmallPageReturnsUnchanged:
    def test_under_cap_returns_byte_identical(self, wiki_tool):
        """A page whose content fits under the cap returns unchanged.

        Pre-fix: same shape. Post-fix: same shape PLUS no new
        ``content_truncated`` key. Pins the negative direction — the cap
        must not leak keys onto every read.
        """
        wtool, wire = wiki_tool
        small = "x" * 1024  # 1 KB
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, "/home/max/quinyx/flux", content=small)])

        result = wtool.wiki_read(_FLUX_SLUG, project=_FLUX_PROJECT)

        assert "error" not in result, f"the small-page read regressed: {result!r}"
        assert result.get("content") == small, (
            f"under-cap content must be returned unchanged; got {len(result.get('content', ''))} chars"
        )
        assert "content_truncated" not in result, (
            f"under-cap read must not emit a content_truncated key; got {result.get('content_truncated')!r}"
        )
        assert "content_total_bytes" not in result


# ── large page: cap fires, markers attached ─────────────────────────────────


class TestLargePageTruncates:
    def test_over_cap_truncates_content(self, wiki_tool):
        """A page whose content exceeds the cap is truncated to the cap.

        Pre-fix: returned the full N KB blob. Post-fix: returned the first
        ``_WIKI_READ_CONTENT_CAP_BYTES`` characters and added the markers.
        """
        wtool, wire = wiki_tool
        big = "y" * (16_384 + 1)  # 16 KB + 1 byte; above the 8 KB cap
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, "/home/max/quinyx/flux", content=big)])

        result = wtool.wiki_read(_FLUX_SLUG, project=_FLUX_PROJECT)

        assert "error" not in result, f"large-page read regressed: {result!r}"
        # Char-length comparison: ASCII input means bytes == chars here.
        # utf-8 byte length is computed by the cap (len(content.encode('utf-8')))
        # — same number for ASCII content.
        assert len(result["content"]) == 8_192, (
            f"content must be truncated to the cap; got len={len(result['content'])}"
        )
        assert result["content_truncated"] is True
        assert result["content_total_bytes"] == len(big.encode("utf-8"))
        assert result["content_total_bytes"] > 8_192, (
            f"content_total_bytes must reflect the uncut size, not the cap; got {result['content_total_bytes']!r}"
        )

    def test_truncation_decision_uses_utf8_byte_length(self, wiki_tool):
        """Pin the byte-vs-char choice: 4-byte emoji push the byte length over
        the cap even when the character count is small. The cap is enforced
        on UTF-8 BYTES (what the cache byte budget tracks), NOT on chars.

        Concretely: 3000 emoji = 12 000 utf-8 bytes (4 bytes each). Byte
        length 12 000 (over the 8 192-byte cap) → marker fires. The
        byte-slice lands mid-codepoint; ``errors="ignore"`` drops the
        partial trailing codepoint cleanly. ``content_total_bytes`` reports
        the uncut byte length so the caller knows how much was elided.
        """
        wtool, wire = wiki_tool
        # 3000 emoji = 12 000 utf-8 bytes (4 bytes each), 3000 chars.
        # Char length: 3000 (under 8 KB cap). Byte length: 12 000 (over).
        big = "🚀" * 3000
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, "/home/max/quinyx/flux", content=big)])

        result = wtool.wiki_read(_FLUX_SLUG, project=_FLUX_PROJECT)

        assert result.get("content_truncated") is True, (
            f"4-byte chars must trip the cap decision; got {result!r}"
        )
        assert result["content_total_bytes"] == 12_000, (
            f"content_total_bytes must report the uncut byte length; got {result['content_total_bytes']!r}"
        )
        # Byte-slice enforces the cap on BYTES, not chars. 8192 bytes / 4
        # bytes-per-emoji = 2048 complete codepoints; the trailing partial
        # codepoint is dropped via errors="ignore" — NEVER mid-codepoint
        # bytes that decode to U+FFFD.
        assert result["content"] == "🚀" * 2048, (
            f"4-byte cap is on BYTES; expected 2048 complete emoji "
            f"(=8192 bytes), got {len(result['content'])} chars"
        )
        assert len(result["content"].encode("utf-8")) == 8_192, (
            f"returned content must be exactly at the byte cap; got "
            f"{len(result['content'].encode('utf-8'))} bytes"
        )
        # Defensive: errors="ignore" must not produce garbage.
        result["content"].encode("utf-8").decode("utf-8")  # raises if invalid

    def test_warm_hit_returns_same_truncated_view(self, wiki_tool):
        """Consistency: a warm cache hit returns the truncated view too.

        Pre-fix n/a (no cap). Post-fix: the cap is applied BEFORE the cache
        put, so a second read of the same page returns the same dict — same
        markers, same content length. A regression where the cap ran only
        on the cold path would diverge here.
        """
        wtool, wire = wiki_tool
        big = "z" * 20_000  # well over the cap
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, "/home/max/quinyx/flux", content=big)])

        cold = wtool.wiki_read(_FLUX_SLUG, project=_FLUX_PROJECT)
        warm = wtool.wiki_read(_FLUX_SLUG, project=_FLUX_PROJECT)

        assert cold["content_truncated"] is True
        assert warm["content_truncated"] is True
        assert cold["content"] == warm["content"]
        assert len(warm["content"]) == 8_192, (
            f"warm hit returned a different cap length than cold; cold={len(cold['content'])}, warm={len(warm['content'])}"
        )
        assert cold["content_total_bytes"] == warm["content_total_bytes"]

    def test_empty_content_does_not_emit_markers(self, wiki_tool):
        """Edge case: an empty / falsy content field is NOT truncated.

        Pins the isinstance(str) branch — a non-string content (None, bytes,
        a list) must not crash the cap and must not emit markers.
        """
        wtool, wire = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, "/home/max/quinyx/flux", content="")])

        result = wtool.wiki_read(_FLUX_SLUG, project=_FLUX_PROJECT)

        assert "content_truncated" not in result, (
            f"empty content must not be marked truncated; got {result.get('content_truncated')!r}"
        )
        assert result.get("content") == ""

    def test_over_cap_cjk_slice_is_byte_limited_not_char_limited(self, wiki_tool):
        """Pin the BYTE slice (not char slice) for multi-byte content over the cap.

        PR #65 review finding #2: the cap guarded on
        ``len(_content.encode("utf-8")) > _WIKI_READ_CONTENT_CAP_BYTES`` but
        sliced on ``_content[:_WIKI_READ_CONTENT_CAP_BYTES]``. For CJK content
        where each char is 3 UTF-8 bytes, 8192 chars = 24 576 bytes — 3× the
        promised cap. The byte-slice MUST land on a codepoint boundary, so
        encode once, slice bytes, decode with errors="ignore".

        10 000 CJK chars = 30 000 utf-8 bytes. Char slice at 8192 returns 8192
        CJK chars = 24 576 bytes — over the 8192-byte cap (the leak). The
        fix: byte slice at 8192 = up-to 2731 complete CJK chars = ≤ 8192 bytes.
        """
        wtool, wire = wiki_tool
        # 10 000 CJK chars × 3 bytes/char = 30 000 utf-8 bytes — well over cap.
        big = "中" * 10_000
        assert len(big.encode("utf-8")) == 30_000, "test setup invariant"
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, "/home/max/quinyx/flux", content=big)])

        result = wtool.wiki_read(_FLUX_SLUG, project=_FLUX_PROJECT)

        assert result.get("content_truncated") is True, (
            f"over-cap CJK content must trip the cap decision; got {result!r}"
        )
        # The CRITICAL pin: returned content's BYTE length must be <= cap.
        # Pre-fix this fails — 8192 CJK chars = 24 576 bytes.
        assert len(result["content"].encode("utf-8")) <= 8_192, (
            f"truncation must be on utf-8 BYTES, not chars; got "
            f"{len(result['content'].encode('utf-8'))} bytes for "
            f"{len(result['content'])} CJK chars (cap = 8192 bytes)"
        )
        # total_bytes reports the uncut size (defect class requires it).
        assert result["content_total_bytes"] == 30_000
        # Decoded content must be valid UTF-8 (errors="ignore" dropped a
        # partial trailing codepoint, NOT mid-codepoint garbage).
        result["content"].encode("utf-8").decode("utf-8")  # raises if invalid
