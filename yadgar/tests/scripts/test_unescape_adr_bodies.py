"""Bug-bag-2 train 2026-08-23, task 196 — unescape script unit tests.

Pure-function tests against the helpers in ``scripts/unescape_adr_bodies.py``.
The script's DB-touching path is integration territory; this file pins the
non-trivial decisions on the unescape primitives:

  * which entities count as "dirty" (the pattern),
  * that ``html.unescape`` is the single-pass correct tool — especially for
    ``&amp;amp;`` which a naive replace-chain mis-handles,
  * that an EMPTY page is NOT dirty (no entities, no work),
  * idempotence: a second pass over the unescaped output is a no-op.

No fixture / no DB. Run via ``pytest yadgar/tests/scripts/test_unescape_adr_bodies.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from unescape_adr_bodies import (  # noqa: E402
    _count_entities,
    unescape_body,
)


class TestCountEntities:
    def test_zero_on_clean_text(self):
        assert _count_entities("Use ``&`` for bitwise AND.") == 0

    def test_counts_single_amp(self):
        assert _count_entities("A &amp; B") == 1

    def test_counts_multiple_entities_per_line(self):
        # ``_ENTITY_PATTERN`` matches each entity left-to-right and overlap is
        # possible: ``"a &amp;amp;"`` matches ``&amp;`` at offset 2 and
        # ``amp;`` is NOT a recognized entity, so the count is 1 — the
        # residue-after check (``residue_after`` in the script) is the
        # canonical pass/fail, not the pattern count. The pattern's job is
        # only to flag dirty pages, so we don't constrain overlap semantics
        # here; we just pin the non-trivial multi-entity case.
        assert _count_entities("a &amp; b &lt; c &gt; d &quot;e&quot; &apos;f&apos; &nbsp; g") == 8

    def test_counts_repeated_amp(self):
        assert _count_entities("&amp;&amp;&amp;") == 3

    def test_amp_amp_in_one_token(self):
        # ``&amp;amp;`` — ``&amp;`` at offset 0 (1 match); the trailing ``amp;``
        # is not a recognized entity (the leading ``&`` is missing). So the
        # pattern counts 1; the unescape then resolves to literal ``&amp;``
        # (a single escape that is itself a no-op for the prose reader).
        assert _count_entities("&amp;amp;") == 1

    def test_empty_string_is_not_dirty(self):
        assert _count_entities("") == 0

    def test_entity_pattern_is_case_insensitive(self):
        # A malformed page could have &AMP; — still counts as one entity.
        assert _count_entities("&AMP;") == 1


class TestUnescapeBody:
    def test_basic_amp(self):
        assert unescape_body("A &amp; B") == "A & B"

    def test_basic_lt_gt(self):
        assert unescape_body("x &lt; y &gt; z") == "x < y > z"

    def test_quot_apos(self):
        assert unescape_body("&quot;hi&quot; &apos;there&apos;") == "\"hi\" 'there'"

    def test_nbsp(self):
        # NBSP — common in migrated prose.
        assert unescape_body("a&nbsp;b") == "a\xa0b"

    def test_double_escaped_amp_idempotently_resolves(self):
        """``html.unescape`` handles ``&amp;amp;`` → ``&amp;`` (NOT ``&``).

        A naive ``str.replace('&amp;', '&').replace('&lt;', '<')`` chain would
        collapse ``&amp;amp;`` to ``&`` in two passes. This is the regression
        the test guards against — using ``html.unescape`` once is the correct
        tool.
        """
        assert unescape_body("&amp;amp;") == "&amp;"

    def test_idempotent_second_pass_is_noop(self):
        # After one unescape pass, no entity remains — a second pass is a no-op.
        once = unescape_body("a &amp; b &lt; c")
        twice = unescape_body(once)
        assert once == twice
        assert once == "a & b < c"

    def test_empty_string(self):
        assert unescape_body("") == ""

    def test_text_without_entities_unchanged(self):
        s = "No HTML entities in this line."
        assert unescape_body(s) == s
