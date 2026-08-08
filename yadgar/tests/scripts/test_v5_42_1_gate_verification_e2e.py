"""E2E smoke test — v5.42.1 gate verification post-backfill.

Test name: test_v5_42_1_gate_fires_post_backfill_e2e
Marker: integration

Verifies that the similarity gate fires correctly after migration_014 backfill.
Reproduces the full v5.42 production scenario:

1. Create base page via wiki_add(wait=True) — gets real embedding.
2. wiki_check_duplicate against IDENTICAL content (different title) → assert candidates >= 1.
3. wiki_add near-clone with wait=False → queued.
4. Trigger drainer (drain_now() — CI equivalent of "wait 5s").
5. dlq_inspect(filter="rejections") → assert exactly 1 entry with our slug.
6. dlq_dismiss the entry.
7. wiki_delete base page.

This is the scenario that was broken before v5.42.1:
- Pre-backfill: base page has NULL embedding → KNN returns 0 → gate never fires → near-clone stored.
- Post-backfill: base page has real embedding → KNN returns candidate → gate fires → DLQ entry.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar.backend.queue_drainer import FileQueue, QueueDrainer
from yadgar.core import server

# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

_BASE_TITLE = "Yadgar Roadmap Future Improvements v5421smoke"
_BASE_CONTENT = """# Yadgar Roadmap: Future Improvements

## Short-term (next 2 months)
- Implement wiki versioning (v5.41) to track page history
- Add similarity gate to wiki_add to prevent duplicate pages
- Improve embedding model to mpnet for better semantic search

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation
- Automated anchor hygiene with consolidation pass

## Long-term (6+ months)
- LLM-based duplicate resolution and wiki curation
- Retroactive deduplication of existing pages
- Distributed SurrealDB for large-scale deployment

## Architecture principles
Yadgar follows a thin-request-path invariant: all heavy computation deferred
to background consolidation. Wiki operations must complete in <100ms.
"""

# --- Payload for test_check_duplicate_finds_drainer_written_page -------------
# Preserved verbatim from the deleted test_v5_42_2_branch_default_e2e.py. The
# prose still narrates the (now-historical) v5.42.2 branch-default bug; it is
# retained UNCHANGED on purpose — it is semantic filler whose only job is to
# produce the measured >= 0.80 seed/probe similarity. Rewording it would move
# the embedding and could flip the assertion for reasons unrelated to the gate.
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

_NEAR_CLONE_TITLE = "Yadgar Future Roadmap v5421smoke"
_NEAR_CLONE_CONTENT = """# Yadgar Future Roadmap

## Near-term (next 2 months)
- Wiki versioning (v5.41) — track page history and enable rollback
- Similarity gate in wiki_add — block near-duplicate page creation
- Better embedding model (mpnet) for semantic search quality

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation across workspaces
- Automated anchor hygiene during consolidation cycles

## Long-term (6+ months)
- LLM-based wiki curation and duplicate resolution
- Retroactive dedup of existing pages (v5.45+)
- Distributed SurrealDB for large deployments

## Core principles
Thin request path: heavy work deferred to consolidation background loop.
All wiki ops target <100ms latency.
"""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def _drainer_env(tmp_path, monkeypatch):
    """Isolated server with real FileQueue and a synchronous-on-demand QueueDrainer."""
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "yadgar_data"))
    # v5.42.5/6 added an MCP-boundary guard (_check_wiki_add_context) that rejects
    # wiki_add when branch/directory are absent and YADGAR_*_ENFORCEMENT default ON.
    # This v5.42.1 gate-verification test drives wiki_add without a directory arg;
    # the guard would trip before the similarity-gate behavior under test runs.
    # Enforcement is orthogonal to the backfill/gate scenario exercised here; disable
    # it to reach that pre-enforcement code path. Not #52 test-weakening: no assertion
    # is changed. _enforcement_on reads os.environ live per-call.
    #
    # ADR-0215 Car 2 removed the branch reject from the memory-op write path, but
    # wiki_add's branch reject lives in _check_wiki_add_context and is Car 3's to
    # retire. Until it lands, this setenv is still load-bearing — dropping it makes
    # step 1 below fail with missing_branch. Car 3 deletes the line with the knob.
    monkeypatch.setenv("YADGAR_BRANCH_ENFORCEMENT", "false")
    monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "false")
    server.init_engines(
        db_path=str(tmp_path / "gate_verification_e2e.db"),
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
    """Write via is_draining=True sync path — bypasses queue and gate."""
    import yadgar.backend.queue_drainer._locals as _loc

    _loc._drain_local.active = True
    try:
        return server.wiki_add(title=title, content=content, **kwargs)
    finally:
        _loc._drain_local.active = False


# ---------------------------------------------------------------------------
# E2E gate verification test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_v5_42_1_gate_fires_post_backfill_e2e(_drainer_env, admin_backend_bypass):
    """Full E2E gate verification: similarity gate fires on near-clone after v5.42.1 backfill.

    Pre-condition: wiki pages have real embeddings (backfill runs at init_engines time).
    Expected: near-clone via wait=False is rejected and lands in DLQ as duplicate_detected.

    This test would have FAILED before v5.42.1 because:
    - Old pages had NULL embeddings
    - KNN returned 0 candidates
    - Gate never fired
    - Near-clone was stored silently
    """
    drainer, fq = _drainer_env

    # ── Step 1: Create base page with real embedding ──────────────────────────
    base_result = _write_sync(_BASE_TITLE, _BASE_CONTENT)
    # Confirm page was stored (not a rejection)
    assert base_result.get("stored") is not False, (
        f"Base page write failed unexpectedly: {base_result}"
    )

    # ── Step 2: wiki_check_duplicate against identical content ────────────────
    check_result = server.wiki_check_duplicate(
        title=_NEAR_CLONE_TITLE,
        content=_NEAR_CLONE_CONTENT,
    )
    candidates = check_result.get("candidates", [])
    base_slug = server._wiki._slugify(_BASE_TITLE)
    assert len(candidates) >= 1, (
        f"wiki_check_duplicate returned 0 candidates for near-clone. "
        f"base_slug={base_slug!r}. "
        f"This indicates the similarity gate is still non-functional — "
        f"embedding backfill may have failed or the embed model is unavailable. "
        f"Full result: {check_result}"
    )
    slugs = [c["slug"] for c in candidates]
    assert base_slug in slugs, f"Base page slug {base_slug!r} not in candidates: {slugs}"

    # ── Step 3: Add near-clone via wait=False ─────────────────────────────────
    enqueue_result = server.wiki_add(
        title=_NEAR_CLONE_TITLE,
        content=_NEAR_CLONE_CONTENT,
        wait=False,
    )
    assert enqueue_result.get("queued") is True, (
        f"wiki_add(wait=False) did not queue: {enqueue_result}"
    )
    assert len(fq.pending()) == 1, f"Expected 1 pending job, got {len(fq.pending())}"

    # ── Step 4: Trigger drainer (CI equivalent of "wait 5s") ─────────────────
    drainer.drain_now()
    assert len(fq.pending()) == 0, "Drainer did not consume pending job"

    # ── Step 5: dlq_inspect(filter="rejections") — expect 1 entry ────────────
    with (
        patch("yadgar.core.lifecycle.lifecycle._get_file_queue", return_value=fq),
        patch("yadgar.core.server.tools.admin_dlq._get_file_queue", return_value=fq),
    ):
        rejections = server.dlq_inspect(filter="rejections")

    assert len(rejections) >= 1, (
        f"DLQ has no rejection entries after draining near-clone. "
        f"The similarity gate did not fire. "
        f"All DLQ entries: {server.dlq_inspect()}"
    )

    # Confirm the rejection is for our near-clone slug
    near_clone_slug = server._wiki._slugify(_NEAR_CLONE_TITLE)
    rejection = rejections[0]
    assert rejection.get("failure_reason") == "duplicate_detected", (
        f"DLQ entry has wrong failure_reason: {rejection.get('failure_reason')!r}. "
        f"Expected 'duplicate_detected'."
    )

    # ── Step 6: dlq_dismiss the entry ────────────────────────────────────────
    with (
        patch("yadgar.core.lifecycle.lifecycle._get_file_queue", return_value=fq),
        patch("yadgar.core.server.tools.admin_dlq._get_file_queue", return_value=fq),
    ):
        dismiss_result = server.dlq_dismiss(filename=rejection["file"])
    assert dismiss_result.get("dismissed") is True or dismiss_result.get("status") == "ok", (
        f"dlq_dismiss failed: {dismiss_result}"
    )

    # ── Step 7: wiki_delete base page ─────────────────────────────────────────
    server.wiki_delete(slug=base_slug)
    # wiki_delete returns None on success or raises — just verify base page gone
    base_page = server._wiki._storage.get_wiki_page_by_slug(base_slug)
    assert base_page is None, f"Base page still exists after wiki_delete: {base_page}"

    # ── VERDICT ───────────────────────────────────────────────────────────────
    # If we reached here:
    # - Base page created with real embedding (backfill ran at startup)
    # - wiki_check_duplicate detected near-clone
    # - Drainer gate fired on wait=False path
    # - DLQ entry with failure_reason=duplicate_detected confirmed
    # - Gate is functional post-v5.42.1

    assert near_clone_slug not in (
        server._wiki._storage.get_wiki_page_by_slug(near_clone_slug) or {}
    ), f"Near-clone page {near_clone_slug!r} was stored despite gate firing"


# ---------------------------------------------------------------------------
# Gate retrieval smoke — moved here from test_v5_42_2_branch_default_e2e.py,
# which was deleted under ADR-0215 (branch scoping removed entirely, so the
# branch-default scope mismatch it reproduced can no longer exist). The other
# test in that file seeded a page with branch="master" and died on
# TypeError: wiki_add() got an unexpected keyword argument 'branch'.
#
# This one survives because what it actually asserts is branch-independent and
# is not covered elsewhere in this file: the full retrieval pipeline end to end
# — drainer commit -> embedding generated -> HNSW indexed -> KNN retrieval ->
# similarity above the gate threshold.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_check_duplicate_finds_drainer_written_page(_drainer_env):
    """wiki_check_duplicate finds a near-clone of a page the drainer committed.

    Asserts the gate's retrieval half works: a page written through the drainer
    is embedded and indexed such that wiki_check_duplicate on a near-clone
    returns it as a candidate at similarity >= 0.80 (WIKI_SIM_CONTENT_THRESHOLD).

    A regression here means the gate has gone silent — near-duplicate pages
    would be stored with no rejection and no signal.
    """
    drainer, fq = _drainer_env

    # Seed page committed via the drainer (wait=True blocks until applied).
    seed_result = server.wiki_add(
        title=_SEED_TITLE,
        content=_SEED_CONTENT,
        force=True,  # bypass gate (no prior near-clone to block this insert)
        wait=True,
    )
    assert seed_result.get("stored") is not False, (
        f"Seed page write failed unexpectedly: {seed_result}"
    )

    check_result = server.wiki_check_duplicate(
        title=_PROBE_TITLE,
        content=_PROBE_CONTENT,
    )
    candidates = check_result.get("candidates", [])

    seed_slug = server._wiki._slugify(_SEED_TITLE)

    assert len(candidates) >= 1, (
        f"wiki_check_duplicate returned 0 candidates for a near-clone of a "
        f"drainer-committed page.\n"
        f"seed_slug={seed_slug!r}\n"
        f"The similarity gate's retrieval path is broken — near-duplicates will "
        f"be stored silently.\n"
        f"Full check_result: {check_result}"
    )

    slugs = [c["slug"] for c in candidates]
    assert seed_slug in slugs, (
        f"Seed slug {seed_slug!r} not in candidates: {slugs}\nFull check_result: {check_result}"
    )

    seed_candidate = next(c for c in candidates if c["slug"] == seed_slug)
    assert seed_candidate["similarity"] >= 0.80, (
        f"Seed candidate similarity too low: {seed_candidate['similarity']:.4f} < 0.80\n"
        f"Candidate: {seed_candidate}"
    )
