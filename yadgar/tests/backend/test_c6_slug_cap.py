"""C6 (#17) — the ADR-0202 slug cap: 256 chars with a hash suffix on overflow.

ADR-0202 mandates slug construction "cap at 256 chars with a hash suffix on
overflow". Neither the cap nor the suffix existed before this car:
``_project_id_to_slug`` was a bare ``str.replace`` and ``_SlugTemplate.format``
emitted whatever length its inputs produced, while ``adr.body_slug`` is a
``VARCHAR`` — so an overflowing project_id produced a slug the column could
not hold.

The invariant these tests pin is deliberately phrased on the EMIT point, not
on the helper: **no slug this module emits exceeds ``SLUG_MAX_CHARS``.**
``_project_id_to_slug`` stays a pure ``/`` → ``_`` mapper (its own docstring's
contract); ``cap_slug`` is the single capping layer, applied by
``_SlugTemplate.format``. One layer, so the hash is taken over exactly one
well-defined string and injectivity is easy to state.
"""

from __future__ import annotations

from yadgar.backend.admin_exec.reslug import (
    NEW_SLUG_TEMPLATE,
    SLUG_MAX_CHARS,
    _project_id_to_slug,
    cap_slug,
)

# A project_id long enough that ``<id>_adr-0001`` overflows the cap on its own.
_LONG = "o" * 300


def test_slug_max_chars_is_the_adr_0202_value():
    """The cap is 256 — ADR-0202's number, not an approximation of it."""
    assert SLUG_MAX_CHARS == 256


def test_cap_slug_is_identity_below_the_cap():
    """A slug that fits is returned byte-for-byte — no gratuitous rewriting.

    The overwhelming majority of slugs are short; capping must not perturb
    them, or every existing slug in the corpus would change meaning.
    """
    short = "m-agahi_yadgar_adr-0042"
    assert cap_slug(short) == short


def test_cap_slug_is_identity_exactly_at_the_cap():
    """Boundary: length == SLUG_MAX_CHARS is NOT an overflow."""
    exact = "x" * SLUG_MAX_CHARS
    assert cap_slug(exact) == exact


def test_overflowing_slug_is_truncated_to_the_cap():
    """An overflowing slug comes back at exactly the cap, never above it."""
    capped = cap_slug("y" * (SLUG_MAX_CHARS + 500))
    assert len(capped) == SLUG_MAX_CHARS


def test_overflowing_slug_ends_in_a_hash_suffix():
    """The tail is the hash, so the truncation is visible rather than silent.

    A bare truncation would map every long slug sharing a prefix onto one
    value; the suffix is what makes the mapping injective in practice.
    """
    capped = cap_slug("z" * (SLUG_MAX_CHARS + 500))
    assert "-h" in capped[-20:], f"no hash marker in tail: {capped[-20:]!r}"
    # The suffix is hex — the last chars after the marker.
    tail = capped.rsplit("-h", 1)[1]
    assert tail, "empty hash suffix"
    assert all(c in "0123456789abcdef" for c in tail), f"suffix not hex: {tail!r}"


def test_two_different_overflowing_slugs_get_different_caps():
    """The RED the spec names: distinct overflowing inputs → distinct slugs.

    Both inputs share a prefix far longer than the cap, so a naive
    truncate-only implementation collapses them into one slug and two
    projects silently share a page.
    """
    a = ("p" * 400) + "alpha"
    b = ("p" * 400) + "beta"
    assert cap_slug(a) != cap_slug(b)
    assert len(cap_slug(a)) == len(cap_slug(b)) == SLUG_MAX_CHARS


def test_cap_slug_is_idempotent_on_an_already_capped_value():
    """Re-capping a capped slug is a no-op — it is already within the cap.

    Matters because the cap sits at an emit point that a caller may route
    an already-emitted slug back through.
    """
    once = cap_slug("q" * 900)
    assert cap_slug(once) == once


def test_template_emits_a_capped_slug_for_an_overflowing_project_id():
    """The emit point — ``NEW_SLUG_TEMPLATE.format`` — respects the cap.

    This is the property that keeps ``adr.body_slug`` insertable; the
    helper being correct is not enough if the template bypasses it.
    """
    slug = NEW_SLUG_TEMPLATE.format(project_id=_LONG, n=1)
    assert len(slug) == SLUG_MAX_CHARS
    assert "-h" in slug[-20:]


def test_template_keeps_distinct_overflowing_project_ids_distinct():
    """Two overflowing project_ids must not collapse onto one ADR slug."""
    one = NEW_SLUG_TEMPLATE.format(project_id=_LONG + "alpha", n=7)
    two = NEW_SLUG_TEMPLATE.format(project_id=_LONG + "beta", n=7)
    assert one != two


def test_template_keeps_distinct_adr_numbers_distinct_when_capped():
    """Capping must not merge two ADRs of the SAME overflowing project.

    The readable ``_adr-NNNN`` tail is truncated away in the overflow case
    (ADR-0202: the slug is OPAQUE, never parsed), so the ONLY thing keeping
    ADR 1 and ADR 2 apart is that the hash is taken over the full pre-cap
    string — which includes the number.
    """
    one = NEW_SLUG_TEMPLATE.format(project_id=_LONG, n=1)
    two = NEW_SLUG_TEMPLATE.format(project_id=_LONG, n=2)
    assert one != two


def test_template_is_unchanged_for_a_normal_project_id():
    """No behaviour change for the real corpus — the Car L shape still holds."""
    assert NEW_SLUG_TEMPLATE.format(project_id="m-agahi/yadgar", n=42) == (
        "m-agahi_yadgar_adr-0042"
    )


def test_project_id_to_slug_stays_a_pure_separator_swap():
    """``_project_id_to_slug`` does NOT cap — the cap lives at the emit point.

    Pinned deliberately: a future reader adding a second cap here would put
    a hash over a hash, which is still injective but makes the emitted value
    impossible to reason about from either end.
    """
    assert _project_id_to_slug("a/b/c") == "a_b_c"
    assert _project_id_to_slug(_LONG) == _LONG
