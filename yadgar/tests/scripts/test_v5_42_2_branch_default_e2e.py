"""E2E RED test — v5.42.2 branch-default scope mismatch.

Reproduces the production sequence that causes the silent similarity gate:
1. wiki_add seed page via the drainer (wait=True) — no explicit branch.
   Drainer's _fill_wiki_add_defaults sets branch="master" (pre-fix) or None (post-fix).
2. wiki_check_duplicate against a near-clone — no explicit branch.
   Pre-fix: scope = {None}, excludes branch="master" pages → 0 candidates (bug).
   Post-fix: auto-detects current/default branch → scope includes stored page → >=1 candidate.

Two tests:
- test_check_duplicate_finds_drainer_written_page: exercises Phase 2 (drainer fix).
  Validates that scope={None} now finds the post-fix canonical page (branch=None).
- test_check_duplicate_finds_legacy_master_page: exercises Phase 3 (auto-detect fix).
  Manually inserts a legacy page with branch="master", then asserts wiki_check_duplicate
  (no branch arg) finds it via the auto-detected scope={None, default_branch}.
  This is the production regression case: pre-v5.42.2 pages all have branch="master".

Marker: integration (requires sentence-transformers; run via .venv-test).

RED phase:  fails because drainer sets branch="master", check_duplicate filters {None}.
GREEN phase: passes after both fixes are applied.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar.backend.queue_drainer import FileQueue, QueueDrainer
from yadgar.core import server

# ---------------------------------------------------------------------------
# Content — unique payload so this test doesn't cross-contaminate others
# ---------------------------------------------------------------------------

_SEED_TITLE = "branch-default-probe-v5422"

_SEED_CONTENT = """# Branch Default Probe v5422

## Purpose
This page exists solely to validate the v5.42.2 fix for the branch-default
scope mismatch bug. The drainer historically set branch='master' while
wiki_check_duplicate defaulted to None, creating an incoherent scope filter.

## Expected behavior after fix
When a page is written via the drainer without an explicit branch parameter,
and wiki_check_duplicate is also called without an explicit branch parameter,
the duplicate check must find the page. Both paths must agree on the canonical
branch slot.

## Architecture note
The fix normalizes the canonical slot: drainer writes branch=None (not 'master'),
and wiki_check_duplicate auto-detects current/default branch so its scope always
includes the None slot plus any explicitly-branched pages.
"""

_PROBE_TITLE = "branch-default-probe-v5422-near-clone"

_PROBE_CONTENT = """# Branch Default Probe v5422 (Near-Clone)

## Purpose
Near-clone of the seed page to trigger the similarity gate.
The drainer historically set branch='master' while wiki_check_duplicate
defaulted to None, which made the scope filter exclude all stored pages.

## Expected behavior
After the v5.42.2 fix, the duplicate check should find the seed page above
with similarity >= 0.85, confirming the gate is functional.

## Architecture note
Canonical branch slot normalized to None; auto-detection in check_duplicate
ensures both writer and reader agree on scope.
"""


# ---------------------------------------------------------------------------
# Fixture — identical pattern to test_v5_42_1_gate_verification_e2e.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def _drainer_env(tmp_path, monkeypatch):
    """Isolated server with real FileQueue and a synchronous-on-demand QueueDrainer."""
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "yadgar_data"))
    # v5.42.5/6 added an MCP-boundary guard (_check_wiki_add_context) that rejects
    # wiki_add when branch/directory are absent and YADGAR_*_ENFORCEMENT default ON.
    # This v5.42.2 regression test deliberately writes the seed page WITHOUT a branch
    # (validating the drainer's canonical branch=None slot — see test docstring), so
    # the branch guard would trip before the behavior under test runs. The enforcement
    # guard is orthogonal to the branch-default scope mismatch these tests exercise;
    # disable it here to reach that pre-enforcement code path. Not #52 test-weakening:
    # no assertion is changed. _enforcement_on reads os.environ live per-call.
    monkeypatch.setenv("YADGAR_BRANCH_ENFORCEMENT", "false")
    monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "false")
    server.init_engines(
        db_path=str(tmp_path / "branch_default_e2e.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    real_fq = FileQueue(tmp_path)

    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.lifecycle.lifecycle as _cl

    drainer = QueueDrainer(
        queue=real_fq,
        storage_factory=lambda: _state_mod._storage,
        drain_interval=9999,  # never self-fires; tests call drain_now()
    )

    def _get_fq():
        return real_fq

    with (
        patch.object(_cl, "_get_file_queue", _get_fq),
        patch("yadgar.core.server.tools.wiki._get_file_queue", _get_fq),
        patch.object(_state_mod, "_queue_drainer", drainer),
        patch.object(_state_mod, "_file_queue", real_fq),
    ):
        yield drainer, real_fq

    server.shutdown()


def _write_sync(title: str, content: str, **kwargs) -> dict:
    """Write via is_draining=True sync path — bypasses queue and gate.

    Allows explicit branch= to simulate legacy pages written before v5.42.2.
    """
    import yadgar.backend.queue_drainer._locals as _loc

    _loc._drain_local.active = True
    try:
        return server.wiki_add(title=title, content=content, **kwargs)
    finally:
        _loc._drain_local.active = False


# ---------------------------------------------------------------------------
# E2E branch-default scope mismatch tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_check_duplicate_finds_drainer_written_page(_drainer_env):
    """wiki_check_duplicate finds a page that was written via the drainer without branch.

    This is the production sequence that was broken before v5.42.2:
    - Drainer sets branch="master" (pre-fix) or None (post-fix) when branch absent.
    - wiki_check_duplicate without branch: pre-fix scope={None}, post-fix auto-detects.
    - Pre-fix result: 0 candidates (bug). Post-fix: >= 1 candidate.

    Phase 1 (RED): FAILS — drainer branch="master", check scope={None}, mismatch.
    Phase 2 (GREEN): PASSES — both paths agree on canonical slot.
    """
    drainer, fq = _drainer_env

    # Step 1: Write seed page via drainer — no explicit branch.
    # wait=True so the page is committed before we proceed.
    seed_result = server.wiki_add(
        title=_SEED_TITLE,
        content=_SEED_CONTENT,
        force=True,  # bypass gate (no prior near-clone to block this insert)
        wait=True,  # go through drainer → _fill_wiki_add_defaults fills branch
    )
    assert seed_result.get("stored") is not False, (
        f"Seed page write failed unexpectedly: {seed_result}"
    )

    # Step 2: Call wiki_check_duplicate against a near-clone — no explicit branch.
    check_result = server.wiki_check_duplicate(
        title=_PROBE_TITLE,
        content=_PROBE_CONTENT,
    )
    candidates = check_result.get("candidates", [])

    seed_slug = server._wiki._slugify(_SEED_TITLE)

    # Core assertion: the seed page must appear in candidates.
    assert len(candidates) >= 1, (
        f"wiki_check_duplicate returned 0 candidates.\n"
        f"seed_slug={seed_slug!r}\n"
        f"This confirms the v5.42.2 branch-default scope mismatch bug:\n"
        f"  - Drainer wrote seed with branch='master' (pre-fix) or branch=None (post-fix)\n"
        f"  - check_duplicate ran with scope={{None}} (pre-fix) or auto-detected (post-fix)\n"
        f"  - Pre-fix: {{'master'}} ∉ {{None}} → excluded → 0 candidates\n"
        f"Full check_result: {check_result}"
    )

    slugs = [c["slug"] for c in candidates]
    assert seed_slug in slugs, (
        f"Seed slug {seed_slug!r} not in candidates: {slugs}\nFull check_result: {check_result}"
    )

    # Confirm similarity meets the gate threshold (>= 0.80, the default WIKI_SIM_CONTENT_THRESHOLD)
    seed_candidate = next(c for c in candidates if c["slug"] == seed_slug)
    assert seed_candidate["similarity"] >= 0.80, (
        f"Seed candidate similarity too low: {seed_candidate['similarity']:.4f} < 0.80\n"
        f"Candidate: {seed_candidate}"
    )


@pytest.mark.integration
def test_check_duplicate_finds_legacy_master_page(_drainer_env):
    """wiki_check_duplicate finds a pre-v5.42.2 legacy page with branch='master'.

    Exercises the Phase 3 fix (wiki_check_duplicate auto-detect).
    Legacy production pages all have branch='master' from the pre-fix drainer.
    Without auto-detect: scope={None} → excludes branch='master' → 0 candidates.
    With auto-detect: scope={None, default_branch} → includes branch='master' → found.

    This is the actual production regression case — all pre-v5.42.2 data has
    branch='master'. Phase 2 (drainer fix) alone doesn't help those pages.
    """
    drainer, fq = _drainer_env

    # Write a legacy page with branch="master" explicitly — simulating pre-fix drainer output.
    legacy_result = _write_sync(
        _SEED_TITLE,
        _SEED_CONTENT,
        branch="master",
        force=True,
    )
    assert legacy_result.get("stored") is not False, (
        f"Legacy page write failed unexpectedly: {legacy_result}"
    )

    # wiki_check_duplicate with no explicit branch — exercises auto-detect.
    check_result = server.wiki_check_duplicate(
        title=_PROBE_TITLE,
        content=_PROBE_CONTENT,
    )
    candidates = check_result.get("candidates", [])

    seed_slug = server._wiki._slugify(_SEED_TITLE)

    assert len(candidates) >= 1, (
        f"wiki_check_duplicate returned 0 candidates for legacy branch='master' page.\n"
        f"seed_slug={seed_slug!r}\n"
        f"This means the Phase 3 fix (auto-detect in wiki_check_duplicate) is not working.\n"
        f"Scope must include 'master' to find pre-v5.42.2 production pages.\n"
        f"Full check_result: {check_result}"
    )

    slugs = [c["slug"] for c in candidates]
    assert seed_slug in slugs, (
        f"Legacy seed slug {seed_slug!r} not in candidates: {slugs}\n"
        f"Full check_result: {check_result}"
    )

    legacy_candidate = next(c for c in candidates if c["slug"] == seed_slug)
    assert legacy_candidate["similarity"] >= 0.80, (
        f"Legacy candidate similarity too low: {legacy_candidate['similarity']:.4f} < 0.80\n"
        f"Candidate: {legacy_candidate}"
    )
