"""Task 391 — ``audit_anchors`` must tell the truth about its own coverage.

Measured on the live corpus 2026-08-27 (host ``m-agahi/yadgar``)::

    audit_anchors(directory=..., dry_run=True)  -> scanned: 95
    SELECT count() FROM memory
      WHERE is_protected = true AND project_id = 'm-agahi/yadgar'  -> 102

Seven protected rows sat outside the scan and the tool said nothing. The
scan's selector is ``'_anchor' INSIDE tags AND directory_context = $dir``;
the seven split into two causes, neither of them a tier filter (the three
``semantic_immortal`` rows are INSIDE the 95):

  * six rows carry ``is_protected = true`` but no ``_anchor`` tag
    (``_historical`` plan rows, ``_active_work``, ``_dispatch_prelude``);
  * one row is ``_anchor``-tagged and owned by the project via
    ``project_id`` but stores a project_id in the legacy
    ``directory_context`` column, so the directory-keyed WHERE misses it
    (ADR-0233 scope-key drift).

A third bucket, ``global_reach_not_scanned``, is zero on the live corpus and
covered here anyway: a ``global``-reach row is unscanned because the caller
passed ``include_global=False``, not because a scope key drifted, and one
label for two causes is not "the cause, precisely".

This car does NOT widen what the audit decides to retire — the action
builders are untouched. It makes the shortfall VISIBLE: silence was the
defect.

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

import pytest

from yadgar.core import server

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_DIR = "/tmp/test_audit_coverage_proj"
_PID = "m-agahi/coverage-proj"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("audit_coverage")
    server.init_engines(
        db_path=str(tmp_path / "test_audit_coverage.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar._shared.runtime.lifecycle import _get_storage

    return _get_storage()


def _insert(storage, *, tags, directory, project_id=None, tier="conditional", protected=True):
    now = storage._now_iso()
    mid = storage._next_id("memory")
    sql = (
        "CREATE type::record('memory', $id) SET "
        "content = $content, directory_context = $dir, tags = $tags, "
        "heat = 0.5, is_protected = $prot, "
        "access_count = 0, last_accessed = $now, created_at = $now"
    )
    params = {
        "id": mid,
        "content": f"row {mid}",
        "dir": directory,
        "tags": tags,
        "prot": protected,
        "now": now,
    }
    if tier is not None:
        sql += ", tier = $tier"
        params["tier"] = tier
    if project_id is not None:
        sql += ", project_id = $pid"
        params["pid"] = project_id
    storage._q(sql, params)
    return mid


@pytest.fixture()
def _corpus(_engines):
    """One anchor per tier in the scanned dir + every unscanned shape.

    Function-scoped and re-seeded per test on purpose: the engines started by
    ``init_engines`` run a background decay/consolidation pass that clears
    these hand-inserted rows part-way through a module (reproduced with a
    bare probe module, no audit code involved), so a module-scoped corpus
    makes later tests pass vacuously against an empty table.
    """
    from yadgar._shared.runtime.lifecycle import _get_storage

    st = _get_storage()
    st._q("DELETE FROM memory WHERE directory_context = $d", {"d": _DIR})
    st._q("DELETE FROM memory WHERE project_id = $p", {"p": _PID})

    st._q("DELETE FROM memory WHERE directory_context = 'global'")

    # One row per tier IN the scanned directory. ``semantic_immortal`` is here
    # deliberately: the car's central finding is that the tier guard governs
    # MUTATION, not visibility, and a fixture whose only immortal row sits in
    # an unscanned bucket would let that claim pass vacuously.
    scanned = [
        _insert(st, tags=["_anchor"], directory=_DIR, project_id=_PID, tier="conditional"),
        _insert(st, tags=["_anchor"], directory=_DIR, project_id=_PID, tier="ephemeral"),
        _insert(st, tags=["_anchor"], directory=_DIR, project_id=_PID, tier="semantic_immortal"),
        # Pre-v5.8 legacy anchor: protected, no tier at all.
        _insert(st, tags=["_anchor"], directory=_DIR, project_id=_PID, tier=None),
    ]
    # Cause 1 — protected, project-owned, but no ``_anchor`` tag.
    no_tag = [
        _insert(st, tags=["_active_work"], directory=_DIR, project_id=_PID, tier=None),
        _insert(st, tags=["_historical"], directory=_DIR, project_id=_PID, tier=None),
    ]
    # Cause 2 — ``_anchor``-tagged and project-owned, but the legacy
    # directory column holds a project_id rather than the path.
    dir_mismatch = [
        _insert(st, tags=["_anchor"], directory=_PID, project_id=_PID, tier="semantic_immortal")
    ]
    # Cause 3 — global reach, scanned only under include_global=True.
    global_reach = [
        _insert(st, tags=["_anchor"], directory="global", project_id=_PID, tier="conditional")
    ]
    return {
        "scanned": scanned,
        "no_tag": no_tag,
        "dir_mismatch": dir_mismatch,
        "global_reach": global_reach,
    }


class TestCoverageReporting:
    def test_coverage_block_present(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True, project=_PID)
        assert "coverage" in result, "audit_anchors must report its own coverage"

    def test_scanned_still_top_level_and_matches_coverage(self, _corpus):
        """``scanned`` stays where every existing caller reads it."""
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True, project=_PID)
        assert result["scanned"] == 4
        assert result["coverage"]["scanned"] == result["scanned"]

    def test_protected_total_counts_rows_the_scan_misses(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        assert cov["protected_total"] == 8
        assert cov["unscanned"] == 4

    def test_unscanned_rows_are_attributed_to_a_named_cause(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        reasons = cov["unscanned_reasons"]
        assert reasons["no_anchor_tag"] == 2
        assert reasons["directory_context_mismatch"] == 1
        assert reasons["global_reach_not_scanned"] == 1
        assert sum(reasons.values()) == cov["unscanned"]

    def test_every_tier_is_scanned_or_explicitly_accounted_for(self, _corpus):
        """No tier is silently dropped: each protected row lands in exactly one bucket.

        Pins the finding that ``semantic_immortal`` is NOT excluded from the
        scan — the tier guard governs MUTATION (``_is_safe_to_mutate``), never
        visibility.
        """
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        assert cov["scanned_protected"] + cov["unscanned"] == cov["protected_total"]
        # Every fixture row is protected, so the scan count and the protected
        # count agree and no ``scanned_unprotected`` key is emitted.
        assert cov["scanned_protected"] == cov["scanned"]
        assert "scanned_unprotected" not in cov
        # All four in-directory tiers — conditional, ephemeral,
        # semantic_immortal, and the untiered legacy row — are SCANNED, and
        # none of them appears in any unscanned bucket.
        assert cov["scanned_protected"] == len(_corpus["scanned"])
        unscanned_ids = {i for ids in cov["unscanned_sample"].values() for i in ids}
        assert unscanned_ids.isdisjoint(set(_corpus["scanned"]))

    def test_semantic_immortal_in_directory_is_counted_not_excluded(self, storage, _corpus):
        """The car's central finding, pinned against the row itself.

        ``_is_safe_to_mutate`` refuses to auto-mutate ``semantic_immortal``;
        the SCAN has no such filter. Read the tier back off the DB so this
        cannot pass on a mis-seeded fixture.
        """
        from yadgar.core.server.tools.audit import audit_anchors

        immortal = _corpus["scanned"][2]
        row = storage._q(
            "SELECT tier FROM memory WHERE id = type::record('memory', $id)", {"id": immortal}
        )
        assert row and row[0]["tier"] == "semantic_immortal"

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        unscanned_ids = {i for ids in cov["unscanned_sample"].values() for i in ids}
        assert immortal not in unscanned_ids

    def test_global_reach_row_is_not_blamed_on_scope_key_drift(self, _corpus):
        """A ``global`` row is unscanned because nobody asked for it.

        Folding it into ``directory_context_mismatch`` would report an
        ADR-0233 cause for a row ``include_global=True`` scans happily.
        """
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        assert set(cov["unscanned_sample"]["global_reach_not_scanned"]) == set(
            _corpus["global_reach"]
        )
        assert set(cov["unscanned_sample"]["directory_context_mismatch"]).isdisjoint(
            set(_corpus["global_reach"])
        )

    def test_include_global_moves_the_global_row_into_the_scan(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID, include_global=True)[
            "coverage"
        ]
        assert "global_reach_not_scanned" not in cov["unscanned_reasons"]
        assert cov["scanned"] == len(_corpus["scanned"]) + len(_corpus["global_reach"])

    def test_samples_name_the_offending_rows(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        samples = cov["unscanned_sample"]
        assert set(samples["no_anchor_tag"]) == set(_corpus["no_tag"])
        assert set(samples["directory_context_mismatch"]) == set(_corpus["dir_mismatch"])

    def test_scope_keys_are_reported(self, _corpus):
        """A coverage number is unreadable without the keys it was measured on."""
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        assert cov["scope_keys"]["directory_context"] == [_DIR]
        assert cov["scope_keys"]["project_id"] == _PID

    def test_actions_unchanged_by_coverage_reporting(self, _corpus):
        """Coverage is a REPORT. It must not enlarge the retire population."""
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True, project=_PID)
        # None of the fixture rows are expired / redundant / promote-worthy.
        assert result["actions"] == []

    def test_coverage_without_project_override_still_reports(self, _corpus):
        """No ``project=`` → the directory key alone; the block is still emitted."""
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True)["coverage"]
        assert cov["scanned"] == 4
        # The directory-context-mismatch row is unreachable without a project
        # key; the two untagged rows are still visible.
        assert cov["unscanned_reasons"]["no_anchor_tag"] == 2
        assert cov["scope_keys"]["project_id"] is None

    def test_a_failed_coverage_query_says_so_instead_of_reporting_zero(self, _corpus, monkeypatch):
        """ADR-0420: an empty result and a failed query must not look alike.

        Returning ``protected_total: 0`` on a blown-up query would claim
        perfect coverage — precisely the silence this car removes.
        """
        from yadgar.core.server.tools import _audit_coverage
        from yadgar.core.server.tools.audit import audit_anchors

        monkeypatch.setattr(_audit_coverage, "_fetch_protected_rows", lambda *a, **kw: None)
        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        assert "error" in cov
        assert "protected_total" not in cov
        assert cov["scanned"] == 4
