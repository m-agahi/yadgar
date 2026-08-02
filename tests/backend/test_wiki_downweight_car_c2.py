# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car C2 — implement downweight disposition.

Spine task-table-refactor-2026-07-29, Car C2: downweight is documented in
policy.py:34 as a valid disposition but treated as include. C2 makes it
actually downweight the candidate's native_score.

The downweight factor is configurable via a constant on the provider.
"""

from __future__ import annotations


def test_c2_downweight_factor_exists() -> None:
    """The downweight factor is exposed as a module-level constant."""
    from yadgar.backend.retrieval.providers.wiki import DOWNWEIGHT_FACTOR

    assert isinstance(DOWNWEIGHT_FACTOR, float)
    assert 0.0 < DOWNWEIGHT_FACTOR < 1.0


def test_c2_downweight_applies_to_score() -> None:
    """A candidate from a downweighted page_type has its score multiplied by the factor."""
    from yadgar.backend.retrieval.providers.wiki import DOWNWEIGHT_FACTOR, _apply_downweight

    raw_score = 0.8
    adjusted = _apply_downweight("task", raw_score)
    assert adjusted == raw_score * DOWNWEIGHT_FACTOR


def test_c2_downweight_does_not_affect_included_pages() -> None:
    """Included pages are not downweighted."""
    from yadgar.backend.retrieval.providers.wiki import _apply_downweight

    raw_score = 0.8
    assert _apply_downweight("adr", raw_score) == raw_score
    assert _apply_downweight(None, raw_score) == raw_score


def test_c2_downweight_does_not_affect_excluded_pages() -> None:
    """Excluded pages are dropped, not downweighted (exclusion wins)."""
    from yadgar.backend.retrieval.providers.wiki import _apply_downweight

    # agent_prompt is excluded → downweight should NOT be applied because
    # the page is dropped before scoring. The function returns the raw
    # score unchanged because exclusion is a filter, not a scorer.
    raw_score = 0.8
    assert _apply_downweight("agent_prompt", raw_score) == raw_score
