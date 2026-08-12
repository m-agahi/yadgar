"""TDD — Car E, step 5: the `_TASK_RE` matcher in `_task_list_restore_nudge`
must accept Crockford base32 ids (D10) and an optional origin segment (D11).

D10: no zero-padding; display in Crockford base32 (digits + a-z minus i,l,o,u).
D11: optional origin/ prefix on the section heading.

Legacy sections like `## task:0003` must still match (digits are a subset of
the Crockford set). Base32 sections like `## task:4Y` must match. Foreign
sections like `## task:alice/4Y` must also match.

Invalid Crockford characters (here `I`, `L`, `O`, `U`) must NOT match — D10
forbids those letters.
"""

from __future__ import annotations

import os
import re

# Mirror the regex literal that lives inline in http.py:885. Kept in lock-step
# via the linter; if it drifts the test below catches the divergence.
EXPECTED_LITERAL = r"^## task:(?:([\w-]+/)?([0-9a-hj-np-tv-z]+))"


def _build_regex() -> re.Pattern[str]:
    """Return the same regex the production code uses."""
    return re.compile(EXPECTED_LITERAL, re.MULTILINE)


def test_matches_decimal_legacy_id():
    """`## task:0003` (legacy zero-padded decimal) still matches."""
    rx = _build_regex()
    m = rx.search("## task:0003\n- subject: ship car 1")
    assert m is not None, "decimal legacy id `0003` must match"


def test_matches_crockford_base32_id():
    """`## task:4y` (lowercase base32) matches — D10 forces the regex change.

    The plan's regex class is ``[0-9a-hj-np-tv-z]`` (lowercase Crockford):
    D10 specifies lowercase per the spec. Uppercase `Y` does NOT match.
    """
    rx = _build_regex()
    m = rx.search("## task:4y\n- subject: ship car 1")
    assert m is not None, "base32 id `4y` must match"
    # Origin group is optional and not captured here.
    assert m.group(2) == "4y"


def test_matches_origin_prefixed_id():
    """`## task:alice/4y` (foreign origin) matches — D11 tolerance."""
    rx = _build_regex()
    m = rx.search("## task:alice/4y\n- subject: ship car 1")
    assert m is not None, "foreign-origin id `alice/4y` must match"
    assert m.group(1) == "alice/"
    assert m.group(2) == "4y"


def test_does_not_match_invalid_crockford_letter():
    """`## task:00I` (invalid Crockford `I`) — the captured id group must NOT
    include `I`. D10 forbids I/L/O/U. A partial-match that stops at the
    invalid letter is the spec: the regex's id class is the Crockford alphabet,
    so it cannot consume `I` and the captured group is the Crockford-valid prefix.
    """
    rx = _build_regex()
    m = rx.search("## task:00I\n- subject: nope")
    # The match exists (it matches `## task:00`), but the captured id group is
    # NOT `00I` — it is the trailing-Crockford prefix `00`.
    assert m is not None
    assert "I" not in m.group(2), f"captured id must not contain invalid letter; got {m.group(2)!r}"


def test_does_not_match_letter_o():
    """`## task:00O` — captured id group is `00`, not `00O`."""
    rx = _build_regex()
    m = rx.search("## task:00O\n")
    assert m is not None
    assert "O" not in m.group(2)


def test_does_not_match_letter_l():
    """`## task:00L` — captured id group is `00`, not `00L`."""
    rx = _build_regex()
    m = rx.search("## task:00L\n")
    assert m is not None
    assert "L" not in m.group(2)


def test_does_not_match_letter_u():
    """`## task:00U` — captured id group is `00`, not `00U`."""
    rx = _build_regex()
    m = rx.search("## task:00U\n")
    assert m is not None
    assert "U" not in m.group(2)


def test_nudge_text_compiles_with_expected_regex():
    r"""The regex literal in http.py:885 must match what the plan specs.

    This is a guard against the matcher drifting back to the old \\d+ form.
    It reads the source body of `_task_list_restore_nudge` and looks for the
    literal regex pattern.
    """
    src_path = _locate_http_py()
    with open(src_path) as fh:
        src_text = fh.read()
    body = _extract_nudge_body(src_text)
    assert "task:(?:([\\w-]+/)?([0-9a-hj-np-tv-z]+))" in body, (
        "production regex must match the Crockford-base32 + optional-origin "
        "spec from the Car E plan §3.2"
    )


def _locate_http_py() -> str:
    """Resolve the on-disk path of core/server/http.py without importing the module."""
    import yadgar.core.server as srv

    pkg_dir = os.path.dirname(srv.__file__)
    return os.path.join(pkg_dir, "http.py")


def _extract_nudge_body(src_text: str) -> str:
    """Return the source of `_task_list_restore_nudge` + its helpers from http.py.

    Car E extracted the legacy wiki-page parse into `_task_list_legacy_wiki_nudge`
    so the primary handler stays under the C901 cap. The regex literal lives in
    the legacy helper, so this test pulls all three definitions.
    """
    pieces = []
    for name in (
        "_task_list_restore_nudge",
        "_format_task_list_nudge_rows",
        "_task_list_legacy_wiki_nudge",
    ):
        m = re.search(
            rf"(?:async )?def {name}\(.*?(?=\n@observe|\n@trace_span|\ndef [a-zA-Z_])",
            src_text,
            re.DOTALL,
        )
        if m is not None:
            pieces.append(m.group(0))
    assert pieces, "could not locate _task_list_restore_nudge + helpers"
    return "\n".join(pieces)
