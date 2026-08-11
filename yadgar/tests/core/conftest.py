"""Shared project-identity constants for ``yadgar/tests/core`` (C13, ADR-0227).

C5 removed every way the system had of producing a ``project_id`` it was not
given: ``derive_project_id``, ``_local_fallback``, the ``GLOBAL_FALLBACK`` tier
and the ``"unresolved"`` catch are all deleted. A scoped call that names no
project now raises ``UnresolvedProjectError``, and a test that never named one
reds — by design.

**Why this is a constant and an EXPLICITLY-REQUESTED fixture, and never an
autouse one.** An autouse fixture that quietly supplied ``project=`` would be
the deleted fallback wearing test clothing: every future call site that forgot
to name a project would keep passing, and the class of production bug this car
exists to surface (a path that HOLDS a project_id and drops it) would be
invisible again. A test that wants an identity has to ask for one — either by
requesting ``test_project`` in its signature, or by importing
``TEST_PROJECT_ID`` for the dict literals and keyword arguments a fixture
cannot reach.

Shape note: ``owner/repo``. Deliberately not ``local/...`` and not ``global`` —
both are keys ADR-0227 abolished, and a test corpus that keeps writing them
teaches the next reader the wrong shape.
"""

from __future__ import annotations

import pytest

#: The identity used by tests that need *an* identity and do not care which.
#: Tests asserting cross-project behaviour name their own values instead.
TEST_PROJECT_ID = "test-owner/test-repo"

#: A second, distinct identity for tests that must show two projects do not
#: see each other's rows.
OTHER_PROJECT_ID = "other-owner/other-repo"


def memorize_scoped(*args, **kwargs):
    """``memorize_sync`` with a project NAMED (C13, ADR-0227).

    ``memorize`` maps ``UnresolvedProjectError`` onto its error envelope
    instead of raising, so an unnamed call does not blow up — it returns
    ``{"error": "unresolved_project", ...}`` and stores nothing, and the test
    fails several asserts later on an empty recall. That indirection is why
    the naming is centralised in one helper rather than left to be
    rediscovered per file.

    ``setdefault`` rather than a fixed value: a test doing cross-project work
    passes its own ``project=`` and this helper gets out of the way.
    """
    from yadgar.tests.conftest import memorize_sync  # noqa: PLC0415

    kwargs.setdefault("project", TEST_PROJECT_ID)
    return memorize_sync(*args, **kwargs)


@pytest.fixture
def test_project() -> str:
    """The default test identity, for tests whose signature can take it.

    Explicitly requested — never autouse. See the module docstring for why
    that distinction is the whole point of this file.
    """
    return TEST_PROJECT_ID
