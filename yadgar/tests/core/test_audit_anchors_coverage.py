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
    """Three scanned anchors + the two unscanned shapes measured live.

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

    scanned = [_insert(st, tags=["_anchor"], directory=_DIR, project_id=_PID) for _ in range(3)]
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
    return {"scanned": scanned, "no_tag": no_tag, "dir_mismatch": dir_mismatch}


class TestCoverageReporting:
    def test_coverage_block_present(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True, project=_PID)
        assert "coverage" in result, "audit_anchors must report its own coverage"

    def test_scanned_still_top_level_and_matches_coverage(self, _corpus):
        """``scanned`` stays where every existing caller reads it."""
        from yadgar.core.server.tools.audit import audit_anchors

        result = audit_anchors(directory=_DIR, dry_run=True, project=_PID)
        assert result["scanned"] == 3
        assert result["coverage"]["scanned"] == result["scanned"]

    def test_protected_total_counts_rows_the_scan_misses(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        assert cov["protected_total"] == 6
        assert cov["unscanned"] == 3

    def test_unscanned_rows_are_attributed_to_a_named_cause(self, _corpus):
        from yadgar.core.server.tools.audit import audit_anchors

        cov = audit_anchors(directory=_DIR, dry_run=True, project=_PID)["coverage"]
        reasons = cov["unscanned_reasons"]
        assert reasons["no_anchor_tag"] == 2
        assert reasons["directory_context_mismatch"] == 1
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
        assert cov["scanned"] == 3
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
        assert cov["scanned"] == 3
