"""C13 — the guard that replaces ``patch("…identity.derive_project_id")``.

Cars C3, C4, C4b and Car L each proved "this write path did not derive its own
identity" the only way that was available at the time: patch the container-side
classifier to RAISE, run the path, and let a surviving derivation blow up. C5
(ADR-0227) deleted ``derive_project_id`` outright, and with it the thing those
blocks patched — ``unittest.mock.patch`` resolves its target at ``__enter__``
and raises ``AttributeError`` when the attribute is absent, so every one of
those ~24 blocks reds for a reason that has nothing to do with the behaviour
under test.

The assertion is RE-POINTED, not dropped, and the re-point is *stronger* than
what it replaces. "The mint was patched to explode and nothing hit it" is a
runtime observation about one code path; "the mint does not exist to be
reached" is a structural fact about every path at once. The context manager
shape is deliberate: the guard stays at the exact lines that used to carry the
patch, so a future car that reintroduces a container-side mint reds in the
tests that care about it rather than only in the residue sweep.

Not a fixture, and never autouse: this supplies no ``project=`` to anybody. It
asserts an absence. A test that forgets to use it still reds if the path it
exercises fails to thread a real ``project_id``, which is the property the
sweep is protecting.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

#: Names ADR-0227 deleted from ``yadgar.core.identity``. Listed by name here
#: (and not merely by the module's ``__all__``) because the failure this guards
#: is a *re-addition*: a future car that puts ``derive_project_id`` back as a
#: private helper without exporting it would still be reachable by the write
#: paths these tests cover.
DELETED_MINT_NAMES: tuple[str, ...] = ("derive_project_id", "_local_fallback")


@contextlib.contextmanager
def identity_mint_absent() -> Iterator[None]:
    """Assert the container-side identity mint cannot be reached, then run the body.

    Replaces::

        with patch("yadgar.core.identity.derive_project_id", side_effect=_explode):
            ...

    Raises:
        AssertionError: a deleted mint symbol is back on ``yadgar.core.identity``.
    """
    from yadgar.core import identity

    for name in DELETED_MINT_NAMES:
        assert not hasattr(identity, name), (
            f"yadgar.core.identity.{name} is back — ADR-0227 deleted the "
            "container-side mint, and a write path that can reach it can "
            "manufacture a project_id it was never given"
        )
    yield


__all__ = ["DELETED_MINT_NAMES", "identity_mint_absent"]
