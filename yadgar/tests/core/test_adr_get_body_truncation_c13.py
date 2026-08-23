"""Fix #1 (PR #65 review): adr_get body-truncation surface.

Pre-fix, when an ADR's wiki body page was over the 8 KB read cap, the page
fetch returned ``{content: <truncated>, content_truncated: True,
content_total_bytes: N}`` and ``_build_adr_get_response`` did
``merged = dict(body); merged.update(row_metadata)`` — leaking the wiki
keys onto the top-level ADR response. A caller reading the ADR got a
truncated ``content`` with NO signal at the top level that anything was
missing. The wiki-internal ``content_*`` keys also escaped.

Post-fix: the body's ``content_*`` keys are stripped at the merge seam
(wiki-internal leakage removed), and the truncation status is surfaced
at the top level as ``body_truncated: bool`` + ``body_total_bytes: int``.
Additive — a non-truncated body returns ``body_truncated: False`` and
omits ``body_total_bytes`` (key-absent is the no-truncation signal,
mirroring wiki_read's contract).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def over_cap_body() -> dict:
    return {
        "content": "x" * 100,
        "content_truncated": True,
        "content_total_bytes": 24_576,
        "slug": "m-agahi/yadgar-adr-0001",
    }


@pytest.fixture
def under_cap_body() -> dict:
    return {"content": "small", "slug": "m-agahi/yadgar-adr-0001"}


@pytest.fixture
def empty_body() -> dict:
    return {"content": "", "slug": ""}


@pytest.fixture
def stub_row() -> dict:
    return {"row": {"decided_on": "2026-08-23"}}


class TestAdrGetBodyTruncation:
    """Review finding #1: surface body truncation at the top level."""

    def test_over_cap_body_strips_leaky_wiki_keys(self, over_cap_body, stub_row) -> None:
        """Wiki-internal ``content_truncated`` / ``content_total_bytes`` MUST
        NOT leak through to the adr_get top level. Pre-fix they did; the
        caller reads ``response["content_truncated"]`` as a top-level hint
        and wonders what it means at the ADR layer.
        """
        from yadgar.core.server.tools.adr import _build_adr_get_response

        out = _build_adr_get_response(over_cap_body, stub_row)
        assert "content_truncated" not in out, (
            f"wiki-internal content_truncated leaked to top level; got {sorted(out)!r}"
        )
        assert "content_total_bytes" not in out, (
            f"wiki-internal content_total_bytes leaked to top level; got {sorted(out)!r}"
        )

    def test_over_cap_body_surfaces_body_truncated_at_top_level(
        self, over_cap_body, stub_row
    ) -> None:
        """Truncation status belongs at the adr_get top level as
        ``body_truncated``. Pre-fix: caller had no signal.
        """
        from yadgar.core.server.tools.adr import _build_adr_get_response

        out = _build_adr_get_response(over_cap_body, stub_row)
        assert out.get("body_truncated") is True, (
            f"adr_get must surface body_truncated=True at the top level when "
            f"the body was truncated; got {sorted(out)!r}"
        )

    def test_over_cap_body_surfaces_body_total_bytes_at_top_level(
        self, over_cap_body, stub_row
    ) -> None:
        """The uncut byte length belongs at the top level as
        ``body_total_bytes`` so the caller can decide to refetch / split.
        """
        from yadgar.core.server.tools.adr import _build_adr_get_response

        out = _build_adr_get_response(over_cap_body, stub_row)
        assert out.get("body_total_bytes") == 24_576, (
            f"adr_get must surface the uncut byte length at the top level; "
            f"got {out.get('body_total_bytes')!r}"
        )

    def test_under_cap_body_does_not_mark_truncated(self, under_cap_body, stub_row) -> None:
        """An under-cap body carries no wiki truncation keys; the response
        must NOT claim body_truncated=True (no false positives). The
        negative path mirrors wiki_read's under-cap contract: no
        ``content_truncated`` key on the response."""
        from yadgar.core.server.tools.adr import _build_adr_get_response

        out = _build_adr_get_response(under_cap_body, stub_row)
        assert out.get("body_truncated") is False, (
            f"under-cap body must report body_truncated=False; got {out.get('body_truncated')!r}"
        )
        assert "body_total_bytes" not in out, (
            f"under-cap body must not report body_total_bytes "
            f"(key-absent is the no-truncation signal); got {sorted(out)!r}"
        )

    def test_row_metadata_still_present_alongside_truncation(self, over_cap_body, stub_row) -> None:
        """D5 additive merge: row metadata MUST still land on the response.
        Adding the body-truncation keys is additive — the merge contract
        is unchanged for the row side."""
        from yadgar.core.server.tools.adr import _build_adr_get_response

        out = _build_adr_get_response(over_cap_body, stub_row)
        assert out.get("date") == "2026-08-23", (
            f"row metadata (date) must still merge onto the response; got {sorted(out)!r}"
        )
        assert out.get("slug") == "m-agahi/yadgar-adr-0001", (
            f"body slug must still merge; got {sorted(out)!r}"
        )
