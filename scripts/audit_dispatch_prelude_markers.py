#!/usr/bin/env python3
"""Car I (ledger #347) — audit the ``_dispatch_prelude`` marker rows.

Ledger task #288 + #339 claim 34 "anchor pollution" rows tagged
``_anchor + dispatch_prelude`` with ``is_protected=true`` and content
``"dispatch_prelude marker"``, and the brief asks to retire them via
``audit_anchors`` + ``forget(memory_id=N)``.

The corpus shape is DIFFERENT from that description. Static analysis of
``yadgar/_shared/storage/wiki.py:1066-1111`` (``upsert_dispatch_prelude_marker``)
shows the marker row is created with::

    content  = "dispatch_prelude marker"
    tags     = ["_dispatch_prelude"]        # NOT ["_anchor", "_dispatch_prelude"]
    is_protected = NOT set (defaults to False)
    tier     = NOT set (NULL)
    heat     = 1.0
    store_type = "episodic"

So the marker rows are:

  * NOT tagged ``_anchor`` — the dispatch_prelude path does not interact
    with the anchor machinery at all. ``audit_anchors`` will not find them.
  * NOT ``is_protected=True`` — they enter the normal heat-decay path like
    any other memory; the "permanent decay-proof slot" claim in the brief
    does not apply to them.
  * Not content-free in the way the brief assumes — the fixed string IS
    the marker semantic that ``_get_dispatch_prelude_updated_at`` reads
    to drive the ``use_agent_prompt_library`` signal in
    ``yadgar/core/server/tools/project.py:1632-1657``.

The 34-row count in the brief is consistent with one row per
``directory_context`` (the upsert is atomic DELETE-then-INSERT scoped to
one directory) — 34 distinct directories have called
``agent_dispatch_prelude`` since the marker scheme landed.

WHY THIS SCRIPT DOES NOT DELETE
-------------------------------

The marker is a FUNCTIONAL row, not pollution. Deleting it would:

  * make ``_get_dispatch_prelude_updated_at`` return ``None`` for that
    directory,
  * which makes ``_apply_dispatch_prelude_signal`` fire
    ``use_agent_prompt_library`` in ``update_active_work`` for every
    subsequent prompt in that directory, even if the operator just ran
    a prelude,
  * which IS a regression in the signal's discrimination — it can no
    longer tell "this directory ran a prelude recently" from "this
    directory has never run a prelude".

If the ledger task is to retire the marker scheme entirely, the right
fix is BOTH:

  (a) delete the rows (this script can do it on operator approval), AND
  (b) refactor ``_get_dispatch_prelude_updated_at`` / the signal so the
      prelude check no longer depends on a memory row at all (e.g. track
      in the per-process state OrderedDict that already exists for
      session context and prompt recall throttles).

(b) is a Car of its own. (a) alone is the regression. So this script
AUDITS and REPORTS — it does not delete. Operator decides per-row.

WHAT IT DOES
------------

Runs the same SurrealQL query the live read-side uses, against the
live backend, and prints:

  * a per-directory enumeration: row id, created_at, is_protected, tags,
    store_type, content;
  * a counter summary: total rows, unique directories, is_protected count
    (the brief's "32% of 106 protected rows" claim is testable here);
  * a safety check that the row shape matches the upsert contract — if a
    row is _anchor-tagged or is_protected, that is an unexpected
    variation worth surfacing rather than silently deleting;
  * a `--delete` flag that RETURNS the delete plan as SQL ``DELETE``
    statements, one per row, written to stdout. Operator can pipe to
    ``yadgar read_query`` (or equivalent) to apply. The script never
    executes the delete itself.

USAGE
-----

    # audit (read-only, default)
    python scripts/audit_dispatch_prelude_markers.py

    # emit DELETE plan (NOT executed)
    python scripts/audit_dispatch_prelude_markers.py --emit-delete-plan

Requires a live ``YADGAR_EMBED_URL`` so the script can forward the read
query to the backend via the canonical path (``yadgar.core.forward``).
On a bare host with no backend running, the script prints a clear
"backend unreachable" and exits 2 — the same contract
``_forward_read_query`` enforces.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Marker contract — sourced from upsert_dispatch_prelude_marker
# (yadgar/_shared/storage/wiki.py:1066-1111). Pinned here so an
# accidental drift between the writer and the auditor is loud, not silent.
EXPECTED_CONTENT = "dispatch_prelude marker"
EXPECTED_TAGS = frozenset({"_dispatch_prelude"})
UNEXPECTED_TAGS_TO_FLAG = frozenset({"_anchor", "anchor"})  # would be a drift


def _fetch_marker_rows() -> list[dict[str, Any]]:
    """Forward the marker read to the backend read_query endpoint.

    Mirrors the live read path's query
    (``yadgar/core/server/tools/project.py:1638-1643``) so the audit
    sees exactly what the signal sees. We list ALL rows here, not just
    one per directory, so the operator can see duplicate writes that
    pre-date the atomic DELETE-then-INSERT.
    """
    from yadgar.core.forward import _forward_read_query  # noqa: PLC0415

    return _forward_read_query(
        "SELECT meta::id(id) AS id, content, tags, is_protected, "
        "directory_context, created_at, store_type, tier "
        "FROM memory WHERE '_dispatch_prelude' INSIDE tags"
    ).get("rows", [])


def _check_shape(row: dict[str, Any]) -> list[str]:
    """Return a list of human-readable anomalies for one row.

    Anomalies are NOT errors — they are the operator's signal that the
    brief's description of the corpus is stale or wrong for THIS row.
    Each one is something the live upsert contract would never produce.
    """
    anomalies: list[str] = []
    if row.get("content") != EXPECTED_CONTENT:
        anomalies.append(
            f"unexpected content {row.get('content')!r} (expected {EXPECTED_CONTENT!r})"
        )
    tags = set(row.get("tags") or [])
    unexpected = tags & UNEXPECTED_TAGS_TO_FLAG
    if unexpected:
        anomalies.append(
            f"row carries anchor-tag(s) {sorted(unexpected)} — brief's "
            f"'`_anchor + dispatch_prelude`' shape would match THIS row, "
            f"not the common case"
        )
    if tags != EXPECTED_TAGS:
        anomalies.append(f"tag set {sorted(tags)} != expected {sorted(EXPECTED_TAGS)}")
    if row.get("is_protected") is True:
        anomalies.append(
            "is_protected=True — the upsert never sets this, so a True "
            "value here means the row was patched post-insert. Brief's "
            "'compaction-proof slot' claim applies to THIS row only."
        )
    if row.get("store_type") != "episodic":
        anomalies.append(f"store_type={row.get('store_type')!r} (expected 'episodic')")
    return anomalies


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the headline counts the operator reads first."""
    protected = sum(1 for r in rows if r.get("is_protected") is True)
    by_dir: dict[str, int] = {}
    for r in rows:
        d = r.get("directory_context") or "<missing>"
        by_dir[d] = by_dir.get(d, 0) + 1
    return {
        "total_rows": len(rows),
        "unique_directories": len(by_dir),
        "is_protected_count": protected,
        "directories_with_multiple_markers": {d: n for d, n in by_dir.items() if n > 1},
    }


def _emit_delete_plan(rows: list[dict[str, Any]]) -> list[str]:
    """Return DELETE statements, one per row. Operator-applied, not auto-run."""
    return [
        f"DELETE FROM memory WHERE meta::id(id) == {row['id']!r};"
        for row in rows
        if row.get("id") is not None
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--emit-delete-plan",
        action="store_true",
        help="Print DELETE statements (one per row) to stdout. NOT executed. "
        "Pipe to your DB shell to apply after manual review.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a single JSON document.")
    args = parser.parse_args()

    try:
        rows = _fetch_marker_rows()
    except Exception as exc:  # noqa: BLE001 — operator-visible error, not a refusal
        sys.stderr.write(f"backend unreachable or refused: {exc}\n")
        return 2

    summary = _summarize(rows)

    if args.emit_delete_plan:
        for stmt in _emit_delete_plan(rows):
            print(stmt)
        return 0

    if args.json:
        # Anomaly-flagged rows; the operator reads the JSON to decide
        # which deletes are safe.
        out_rows = []
        for row in rows:
            row_out = dict(row)
            row_out["_anomalies"] = _check_shape(row)
            out_rows.append(row_out)
        json.dump({"summary": summary, "rows": out_rows}, sys.stdout, indent=2)
        print()  # trailing newline
        return 0

    # Human-readable default
    print("=== _dispatch_prelude marker audit (Car I, ledger #347) ===")
    print(f"total rows          : {summary['total_rows']}")
    print(f"unique directories  : {summary['unique_directories']}")
    print(f"is_protected rows   : {summary['is_protected_count']}")
    multi = summary["directories_with_multiple_markers"]
    if multi:
        print(f"dirs with >1 marker : {multi}  (atomic upsert should prevent this)")
    print()
    print("--- per-row detail ---")
    for row in rows:
        rid = row.get("id", "?")
        d = row.get("directory_context") or "<missing>"
        prot = row.get("is_protected")
        tags = sorted(row.get("tags") or [])
        ts = row.get("created_at")
        anomalies = _check_shape(row)
        flag = " [ANOMALY] " if anomalies else "           "
        print(f"{flag}id={rid}  dir={d}  is_protected={prot}  tags={tags}  created_at={ts}")
        for a in anomalies:
            print(f"             - {a}")
    print()
    print("Refusing to delete: markers are functional rows read by")
    print("_get_dispatch_prelude_updated_at (project.py:1632-1657). Use")
    print("--emit-delete-plan to get SQL after operator review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
