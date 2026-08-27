"""Anchor-audit coverage reporting (task 391).

Split out of ``tools/audit.py`` — that module sits at 98% of its 1000-line
HARD complexity cap, and this block is self-contained.

``audit_anchors``'s scan selector is
``'_anchor' INSIDE tags AND directory_context = $dir``. That is NARROWER
than "this project's protected rows", and until this car the tool reported
only the narrow number. Measured on the live corpus 2026-08-27:
``scanned: 95`` against 102 rows with ``is_protected = true AND project_id =
'm-agahi/yadgar'`` — seven rows outside the scan, reported by nobody.

The seven split into exactly two causes, and NEITHER is a tier filter: the
three ``semantic_immortal`` rows are INSIDE the 95. The tier guard
(``_is_safe_to_mutate``) governs MUTATION, never visibility.

  no_anchor_tag               ``is_protected = true`` with no ``_anchor`` tag
                              (``_historical`` plan rows, ``_active_work``,
                              ``_dispatch_prelude``).
  directory_context_mismatch  ``_anchor``-tagged and owned by the project via
                              ``project_id``, but the legacy
                              ``directory_context`` column holds a project_id
                              rather than the path, so the directory-keyed
                              WHERE misses it (ADR-0233 scope-key drift;
                              re-keying the scan itself is C7's job, not
                              this car's).
  global_reach_not_scanned    ``_anchor``-tagged with the ``global`` reach
                              sentinel in ``directory_context``, which
                              ``audit_anchors`` scans only under
                              ``include_global=True``. Zero on this corpus
                              today, and split out anyway: folding it into
                              ``directory_context_mismatch`` would blame
                              ADR-0233 drift for a row the caller simply did
                              not ask for.

Everything here REPORTS. It deliberately does not widen the population the
action builders run over — what ``audit_anchors`` decides to retire is
user-gated policy.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

_COVERAGE_SAMPLE_CAP = 10

# The ``directory_context`` sentinel ``audit_anchors`` scans only when
# ``include_global=True`` — see its ``audit_dirs`` list.
_GLOBAL_DIR = "global"


@observe(tier="stage", metric="tools._audit_coverage._coverage_project_id")
def _coverage_project_id(project: str | None) -> str | None:
    """Best-effort project_id for the coverage query's second scope key.

    ``accept_project_param`` returns ``None`` when the caller named no
    project (audit_anchors is still directory-keyed until C7). Fall back to
    the SessionStart contextvar so the ordinary no-``project=`` MCP call still
    sees rows keyed the ADR-0233 way. Never raises: coverage is a report, and
    a report that can fail the audit is worse than a narrower report.
    """
    if project:
        return project
    try:
        from yadgar._shared.runtime.session_project import (
            get_current_session_project,
        )

        return get_current_session_project() or None
    except Exception:  # noqa: BLE001 - reporting must never break the audit
        logger.warning("_coverage_project_id: session lookup failed", exc_info=True)
        return None


@observe(tier="stage", metric="tools._audit_coverage._fetch_protected_rows")
def _fetch_protected_rows(
    storage, audit_dirs: list[str], project_id: str | None
) -> list[dict] | None:
    """Fetch every protected row this audit owns, by EITHER scoping key.

    The ``project_id`` arm is added only when a project is actually
    resolvable. Passing ``NONE`` for it would sweep in every project-less
    protected row in the corpus, which is a different (and wrong) question.

    Returns ``None`` — NOT ``[]`` — when the query fails (ADR-0420). An empty
    list here would be indistinguishable from "this project owns no protected
    rows", and a coverage report that quietly claims full coverage because its
    own query blew up is the exact defect this module exists to remove.
    """
    params: dict = {"dirs": audit_dirs}
    clause = "AND directory_context IN $dirs"
    if project_id:
        clause = "AND (directory_context IN $dirs OR project_id = $pid)"
        params["pid"] = project_id
    try:
        return storage._q(
            "SELECT id, tags, directory_context, tier FROM memory "
            f"WHERE is_protected = true {clause}",
            params,
        )
    except Exception:
        logger.warning(
            "_fetch_protected_rows failed; coverage will report unavailable", exc_info=True
        )
        return None


@observe(tier="stage", metric="tools._audit_coverage._build_coverage")
def _build_coverage(
    storage,
    audit_dirs: list[str],
    scanned: int,
    project: str | None,
) -> dict:
    """Report what the scan covered and, for the rest, WHY it did not.

    Silence about the shortfall was the defect (task 391); this makes the
    number auditable without changing which rows get acted on.
    """
    project_id = _coverage_project_id(project)
    scope_keys = {"directory_context": list(audit_dirs), "project_id": project_id}
    rows = _fetch_protected_rows(storage, audit_dirs, project_id)
    if rows is None:
        return {
            "scanned": scanned,
            "error": "protected-row query failed; coverage unavailable for this run",
            "scope_keys": scope_keys,
        }
    dirs = set(audit_dirs)

    scanned_protected = 0
    reasons: dict[str, int] = {}
    sample: dict[str, list[int]] = {}
    for row in rows:
        tags = row.get("tags") or []
        in_scan_tag = "_anchor" in tags
        in_scan_dir = row.get("directory_context") in dirs
        if in_scan_tag and in_scan_dir:
            scanned_protected += 1
            continue
        # Exhaustive: failing the conjunction means one of the two arms failed.
        if not in_scan_tag:
            reason = "no_anchor_tag"
        elif row.get("directory_context") == _GLOBAL_DIR:
            # Not scope-key drift — a global-reach row the caller simply did
            # not ask for. Labelling it ``directory_context_mismatch`` would
            # blame ADR-0233 for a row ``include_global=True`` would scan.
            reason = "global_reach_not_scanned"
        else:
            reason = "directory_context_mismatch"
        reasons[reason] = reasons.get(reason, 0) + 1
        bucket = sample.setdefault(reason, [])
        if len(bucket) < _COVERAGE_SAMPLE_CAP:
            bucket.append(storage._extract_id(row.get("id")))

    coverage: dict = {
        "scanned": scanned,
        "scanned_protected": scanned_protected,
        "protected_total": len(rows),
        "unscanned": sum(reasons.values()),
        "unscanned_reasons": reasons,
        "unscanned_sample": sample,
        "scope_keys": scope_keys,
    }
    # A row the scan counted but the protected query did not see is an
    # ``_anchor``-tagged row with ``is_protected`` unset. Surfaced only when
    # non-zero so the ordinary result stays readable.
    unprotected = scanned - scanned_protected
    if unprotected:
        coverage["scanned_unprotected"] = unprotected
    return coverage
