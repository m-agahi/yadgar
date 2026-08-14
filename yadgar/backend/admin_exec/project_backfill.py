"""C6 — the operator-invoked ``project_id`` backfill (0047 PR#40 §5.C6, T2).

Shape borrowed from ``reslug_adr_pages``: build a manifest, return it
UN-APPLIED, operator reviews, re-runs with ``dry_run=False``.

THE OP DERIVES NOTHING
----------------------
It takes a host-resolved ``directory_context → project_id`` mapping produced
by the C2 mint running host-side. ADR-0227: migration 031's in-migration
backfill could not stand because the migration runs inside a container that
installs no git and mounts no host project directory — it would have stamped
``local/<basename>`` on every row, silently and always, producing a
well-formed key indistinguishable at read time from a correct one. Every
identity in this op arrives from outside it, including the sentinel classes:
``"global"`` is simply another key in the caller's mapping.

WHY THE MANIFEST IS REVIEWED RATHER THAN DERIVED
------------------------------------------------
Measured live (``db_inspect``, 2026-08-10): 31 distinct ``directory_context``
values on ``wiki_page``, 128 on ``memory``, and **1,033 of 5,349 rows — ~19%
— carry a sentinel** (80 wiki + 349 memory ``global``, 604 memory
``system``). A further 18 distinct values are free-text prose, i.e.
``memorize(context=)`` used as a description, which its own docstring forbids.
No heuristic covers those, and a backfill that reported success while
silently bucketing 19% is precisely the ADR-0222 failure mode.

So the op REFUSES to apply in two situations, and writes nothing in either:

  * ``unmapped`` is non-empty and the caller did not pass
    ``quarantine_unmapped=True``;
  * ``deletes`` is non-empty and the caller did not pass
    ``confirm_deletes=True``.

Plus two more that admit no acknowledgement at all. Any mapping target that
is not a registered project (ADR-0223 — registry enforcement is FAIL LOUD);
discovering that as a per-row FK error halfway through the apply is the
failure the pre-flight check exists to prevent. And any row with NO
``directory_context``: it has no basis for a mapping, no cohort, and nothing
to quarantine, so there is no flag to wave it through with — the operator
fixes or forgets those rows first.

THE FOUR CLASSES
----------------
  mapped paths      → ``project_id`` from the caller's mapping. Subdirectories
                      of one repo COLLAPSE onto one key (``qwfm`` appears
                      under 6 distinct values, ``infrastructure-services``
                      under 8) — expected and correct.
  ``global``        → an owner (mapping key) PLUS the ``global`` reach tag.
                      Owner and reach are recorded separately (§1.4); dropping
                      the tag would silently narrow those rows from
                      every-project to one-project visibility.
  ``system``        → DELETED (D3). Already unreadable since v5.65 removed
                      ``'system'`` from ``_ALWAYS_ELIGIBLE``, so deletion
                      changes no observable behaviour.
  ``_memify_derive``
  at ``global``     → DELETED (D4), matched on a FOUR-way producer signature.
                      UNLIKE D3 these rows are CURRENTLY READABLE — a real
                      behaviour change, flagged as such per cohort.
  everything else   → quarantined into ``legacy_directory``, never guessed.

ORDERING IS LOAD-BEARING: deletes run BEFORE updates, because the D4 cohort
is a subset of ``directory_context='global'``. Stamping first would write a
project_id onto rows about to be deleted, and the manifest's ``global`` count
would describe a larger set than the one that survives.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

#: Content marker of the ``_memify_derive`` co-occurrence producer
#: (``backend/curation/strengthen.py``). Every one of the 604 ``system`` rows
#: and 238 of the 349 ``global`` ones match it — real samples:
#: *"…automation-tests-policy.json and ARN are frequently modified together"*.
MEMIFY_CONTENT_MARKER = "are frequently modified together"

#: Tags the same producer stamps. Both are required by the D4 signature.
MEMIFY_TAGS = frozenset({"derived", "auto-generated"})

#: The sentinel that means "every project" rather than a path.
GLOBAL_SENTINEL = "global"

#: The reach tag §1.4's read predicate keys on. Rows leaving the ``global``
#: ``directory_context`` must gain it or they lose their reach.
GLOBAL_TAG = "global"

_TABLES = ("memory", "wiki_page")


@observe(exempt="one-line lazy-import accessor; the ops that use it carry the boundary span")
def _get_storage() -> Any:
    """The composed SurrealDB ``StorageEngine``, or None. Patched by tests."""
    from yadgar._shared.runtime.lifecycle import _get_storage as _live  # noqa: PLC0415

    return _live()


@observe(exempt="one-line slot read; mirrors admin_exec/ledger.py's own accessor")
def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None. Patched by tests."""
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    return _st._sql_storage


@observe(tier="hot", span=False)
def _is_memify_global(row: dict) -> bool:
    """True when *row* matches D4's FOUR-way ``_memify_derive`` signature.

    All four conjuncts are required — ``directory_context == 'global'`` AND
    both producer tags AND the content marker. Matching on fewer over-deletes:
    on ``global`` + content alone a hand-written note quoting the phrase dies;
    on the tags alone, every derived memory in the corpus does.

    A pure-Python predicate rather than a SurrealQL ``WHERE``, so the ids it
    selects can be recorded IN the manifest and the apply can delete exactly
    those — which is what makes "apply exactly what the dry run showed" a
    literal property rather than an aspiration.
    """
    if row.get("directory_context") != GLOBAL_SENTINEL:
        return False
    if not MEMIFY_TAGS.issubset(set(row.get("tags") or ())):
        return False
    return MEMIFY_CONTENT_MARKER in (row.get("content") or "")


@observe(tier="hot", span=False)
def _is_system(row: dict) -> bool:
    """True for D3's cohort — ``directory_context == 'system'``.

    Kept as its own predicate rather than folded into a generic sentinel test
    because the two delete cohorts differ in the one way a reviewer cares
    about: these rows are already unreadable, D4's are not.
    """
    return row.get("directory_context") == "system"


#: The delete cohorts, in manifest order. ``currently_readable`` is the field
#: that keeps D3 and D4 apart for a reviewer — collapsing them into one list
#: with no distinction is how the destructive one gets waved through.
_DELETE_COHORTS: tuple[dict[str, Any], ...] = (
    {
        "cohort": "system",
        "table": "memory",
        "match": _is_system,
        "currently_readable": False,
        "reason": (
            "D3 — entity-extraction output from one producer (_memify_derive), zero "
            "provenance (source_memory_ids empty on all 604), and already unreadable: "
            "v5.65 removed 'system' from _ALWAYS_ELIGIBLE. Deleting changes no "
            "observable behaviour. Self-healing: any pair still above the "
            "co-occurrence threshold is re-derived with a correct stamp."
        ),
    },
    {
        "cohort": "memify_global",
        "table": "memory",
        "match": _is_memify_global,
        "currently_readable": True,
        "reason": (
            "D4 — the same producer redirected into the always-eligible bucket by "
            "v5.64. Under §1.4's read predicate these would surface in EVERY "
            "project's recall, permanently. UNLIKE D3 THESE ROWS ARE CURRENTLY "
            "READABLE: this is a real behaviour change, not a cleanup. Rows from the "
            "same producer that carry a real project directory are KEPT and migrated "
            "normally — the single-project ones are the ones the vote got right."
        ),
    },
)


@observe(tier="stage")
def _scan(storage: Any) -> dict[str, list[dict]]:
    """Read every row's classification fields from both tables.

    ``content`` is projected for ``memory`` ONLY. It is needed by exactly one
    predicate — D4's ``_is_memify_global`` — and both delete cohorts are
    ``memory`` cohorts, so projecting it for ``wiki_page`` would pull 2,343
    full page bodies (every ADR body among them) that nothing reads. That is
    the one place a one-shot op over this corpus could actually hurt, and no
    test would show it: the fakes carry three-character bodies.

    Reading the column at all — rather than pushing a SurrealQL
    ``string::contains`` into a ``DELETE ... WHERE`` — is what lets the
    matched ids live IN the manifest, which is what makes
    apply-exactly-the-manifest a property rather than a claim.
    """
    out: dict[str, list[dict]] = {}
    for table in _TABLES:
        columns = (
            "id, directory_context, tags, content"
            if table == "memory"
            else ("id, directory_context, tags")
        )
        rows = storage._q(  # noqa: SLF001 — the established migration/admin-op idiom
            f"SELECT {columns} FROM {table}"  # noqa: S608
        )
        out[table] = list(rows or [])
    return out


@observe(tier="stage")
def _plan_deletes(scanned: dict[str, list[dict]]) -> list[dict]:
    """Return the delete cohorts with their matched row ids.

    Ids, not predicates: the manifest names the exact rows, so an operator's
    review is over a set rather than over a WHERE clause they must evaluate
    in their head.
    """
    out: list[dict] = []
    for spec in _DELETE_COHORTS:
        ids = [str(r.get("id")) for r in scanned[spec["table"]] if spec["match"](r)]
        out.append(
            {
                "cohort": spec["cohort"],
                "table": spec["table"],
                "rows": len(ids),
                "ids": ids,
                "currently_readable": spec["currently_readable"],
                "reason": spec["reason"],
            }
        )
    return out


@observe(tier="stage")
def _plan_updates(
    scanned: dict[str, list[dict]],
    mapping: dict[str, str],
    doomed: set[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split the surviving rows into mapped updates, unmapped, and no-directory.

    *doomed* is the set of row ids the delete cohorts claim; they are excluded
    from the counts so the ``global`` entry reports the SURVIVORS (349 minus
    D4's 238 ≈ 111) rather than the whole class.
    """
    counts: dict[tuple[str, str], int] = {}
    no_directory: list[dict] = []
    for table, rows in scanned.items():
        for row in rows:
            if str(row.get("id")) in doomed:
                continue
            dc = row.get("directory_context")
            if dc is None or dc == "":
                # A row with no directory has NO BASIS for any decision — it
                # cannot be mapped, and quarantining it would preserve
                # nothing. It gets its own manifest bucket rather than a
                # `continue`: a row that counts in ``rows_seen`` and lands in
                # no bucket is precisely the silent-bucketing failure this op
                # exists to prevent, and the totals identity would not catch
                # it because the identity would simply never be checked
                # against that row. Migrations 018/023 backfilled the empty
                # case and the memory table carries a non-empty ASSERT, so
                # the real count is probably zero — "probably zero" is what a
                # manifest replaces.
                no_directory.append({"table": table, "id": str(row.get("id"))})
                continue
            counts[(table, str(dc))] = counts.get((table, str(dc)), 0) + 1

    updates: list[dict] = []
    unmapped: list[dict] = []
    for (table, dc), row_count in sorted(counts.items()):
        target = mapping.get(dc)
        # G2 item 5: an empty-string target (operator typo, stripped value, a
        # bad host-side join) is exactly as "no derivable owner" as an absent
        # mapping key — `if target is None` alone let it through as a real
        # update carrying `project_id=""`, which satisfies no scope predicate
        # and is not the NONE-literal convention every other writer in this
        # train uses. Falsy (None OR "") both route to the reviewed/quarantine
        # path instead.
        if not target:
            unmapped.append({"table": table, "directory_context": dc, "rows": row_count})
            continue
        updates.append(
            {
                "table": table,
                "directory_context": dc,
                "project_id": target,
                "rows": row_count,
                # Rows leaving the ``global`` sentinel must gain the reach tag
                # or §1.4's predicate narrows them to a single project.
                "add_global_tag": dc == GLOBAL_SENTINEL,
            }
        )
    return updates, unmapped, no_directory


@observe(tier="stage")
def _plan_visibility_changes(scanned: dict[str, list[dict]], doomed: set[str]) -> list[dict]:
    """List rows whose VISIBILITY changes when C7's predicate lands (D2).

    A row with a real ``directory_context`` that already carries a
    self-granted ``global`` tag is project-scoped today and globally visible
    afterwards. Measured live: 4 of 7 tagged memory rows and some of the 3
    wiki ones. That is a visibility change, not a migration detail, and it
    gets eyeball approval before the migration runs.

    Rows whose ``directory_context`` IS the sentinel are excluded — they were
    already globally visible, so nothing about them changes.
    """
    out: list[dict] = []
    for table, rows in scanned.items():
        for row in rows:
            rid = str(row.get("id"))
            if rid in doomed:
                continue
            dc = row.get("directory_context")
            if dc == GLOBAL_SENTINEL or dc is None or dc == "":
                continue
            if GLOBAL_TAG in set(row.get("tags") or ()):
                out.append(
                    {
                        "table": table,
                        "id": rid,
                        "directory_context": dc,
                        "tags": list(row.get("tags") or ()),
                    }
                )
    return out


@observe(tier="stage")
async def _unknown_registry_targets(mapping: dict[str, str]) -> tuple[list[str], bool]:
    """Return ``(unregistered mapping targets, registry_available)``.

    ADR-0223: registry enforcement is FAIL LOUD. When engine #2 is absent the
    check cannot run at all — reported as unavailable rather than silently
    passing, because "could not check" and "checked and clean" are different
    facts and only one of them licenses an apply.
    """
    engine = _get_sql_storage()
    if engine is None:
        return [], False
    rows = await engine.list_project_rows()
    known = {str(r.get("key")) for r in rows or ()}
    return sorted({t for t in mapping.values() if t not in known}), True


@observe(tier="stage")
def _apply(storage: Any, manifest: dict) -> None:
    """Execute the manifest. DELETES FIRST — see the module docstring.

    Updates are set-based (one statement per ``directory_context``) because
    they are idempotent by construction: re-running stamps the same value.
    Deletes are id-based because the manifest names the exact rows and the
    apply must not be able to widen that set.
    """
    for cohort in manifest["deletes"]:
        for rid in cohort["ids"]:
            storage._q(  # noqa: SLF001
                f"DELETE type::record('{cohort['table']}', $id)",  # noqa: S608
                {"id": storage._extract_id(rid)},  # noqa: SLF001
            )

    for entry in manifest["updates"]:
        table = entry["table"]
        set_clause = "project_id = $pid"
        if entry["add_global_tag"]:
            # Union rather than append: re-running must not duplicate the tag.
            set_clause += f", tags = array::union(tags, ['{GLOBAL_TAG}'])"
        storage._q(  # noqa: SLF001
            f"UPDATE {table} SET {set_clause} WHERE directory_context = $dc",  # noqa: S608
            {"pid": entry["project_id"], "dc": entry["directory_context"]},
        )

    for entry in manifest["quarantine"]:
        # legacy_directory ONLY — no project_id. These rows have no derivable
        # owner; the original value is preserved for human adjudication rather
        # than replaced by a guess.
        storage._q(  # noqa: SLF001
            f"UPDATE {entry['table']} SET legacy_directory = $dc "  # noqa: S608
            "WHERE directory_context = $dc",
            {"dc": entry["directory_context"]},
        )


@observe(tier="stage")
async def _build_manifest(
    storage: Any, mapping: dict[str, str], payload: dict, *, dry_run: bool
) -> dict[str, Any]:
    """Scan, classify and count. PURE — issues no mutation of any kind.

    Split out from the op body so the read half is one function with one job:
    the dry-run path returns exactly this and nothing else runs, which is what
    makes "a dry run cannot write" true by construction rather than by
    inspection of a longer function.

    ``quarantine`` and ``unmapped`` name the SAME rows before and after the
    operator's acknowledgement — two keys because the manifest must show what
    is UNREVIEWED separately from what will be written. Exactly one of
    ``rows_quarantined`` / ``rows_unmapped`` is non-zero for that reason.
    """
    acknowledged = bool(payload.get("quarantine_unmapped"))
    scanned = _scan(storage)
    deletes = _plan_deletes(scanned)
    doomed = {rid for cohort in deletes for rid in cohort["ids"]}
    updates, unmapped, no_directory = _plan_updates(scanned, mapping, doomed)
    unknown_targets, registry_available = await _unknown_registry_targets(mapping)
    pending = sum(u["rows"] for u in unmapped)

    return {
        "dry_run": dry_run,
        "applied": False,
        "registry": {"available": registry_available, "unknown_targets": unknown_targets},
        "updates": updates,
        "deletes": deletes,
        "quarantine": list(unmapped) if acknowledged else [],
        "unmapped": unmapped,
        "no_directory": no_directory,
        "visibility_changes": _plan_visibility_changes(scanned, doomed),
        "totals": {
            "rows_seen": sum(len(rows) for rows in scanned.values()),
            "rows_updated": sum(u["rows"] for u in updates),
            "rows_deleted": sum(d["rows"] for d in deletes),
            "rows_quarantined": pending if acknowledged else 0,
            "rows_unmapped": 0 if acknowledged else pending,
            "rows_no_directory": len(no_directory),
        },
    }


@observe(tier="boundary", metric="backend.admin.project_id_backfill")
async def project_id_backfill(payload: dict) -> dict:
    """Backfill ``project_id`` from a host-resolved mapping. Dry-run by default.

    payload:
      ``mapping``              REQUIRED ``{directory_context: project_id}``,
                               host-resolved. ``"global"`` is a key like any
                               other (Decision G maps it to the owner of the
                               directory that has no git remote).
      ``dry_run``              default True — returns the manifest, writes nothing.
      ``quarantine_unmapped``  acknowledge the ``unmapped`` bucket, sending
                               those rows to ``legacy_directory``.
      ``confirm_deletes``      acknowledge the delete cohorts. Separate from
                               the above because D4 destroys rows that are
                               currently readable.

    Returns the manifest. On refusal, ``{"ok": False, "reason": ...}`` PLUS the
    full manifest, so the operator sees what would have happened.
    """
    mapping = dict(payload.get("mapping") or {})
    dry_run = bool(payload.get("dry_run", True))
    if not mapping:
        return {"ok": False, "reason": "missing_mapping", "dry_run": dry_run, "applied": False}

    storage = _get_storage()
    if storage is None:
        return {"ok": False, "reason": "storage_unavailable", "dry_run": dry_run, "applied": False}

    manifest = await _build_manifest(storage, mapping, payload, dry_run=dry_run)

    if dry_run:
        return manifest

    refusal = _refusal(manifest, payload)
    if refusal is not None:
        manifest["ok"] = False
        manifest["reason"] = refusal
        logger.warning("project_id_backfill REFUSED to apply: %s", refusal)
        return manifest

    _apply(storage, manifest)
    manifest["ok"] = True
    manifest["applied"] = True
    logger.info("project_id_backfill applied: %s", manifest["totals"])
    return manifest


@observe(tier="hot", span=False)
def _refusal(manifest: dict, payload: dict) -> str | None:
    """Return the reason this apply must not proceed, or None.

    Three gates, checked before any write. Ordered most-fundamental first so
    a deployment problem is not reported as a review problem.
    """
    registry = manifest["registry"]
    if not registry["available"]:
        return "registry_unavailable"
    if registry["unknown_targets"]:
        return "unknown_registry_targets"
    if manifest["deletes"] and not payload.get("confirm_deletes"):
        if any(cohort["rows"] for cohort in manifest["deletes"]):
            return "unconfirmed_deletes"
    if manifest["unmapped"] and not payload.get("quarantine_unmapped"):
        return "unreviewed_directory_contexts"
    if manifest["no_directory"]:
        # No directory means no basis for ANY of the three decisions above —
        # not a mapping, not a delete cohort, not even a quarantine (there is
        # nothing to preserve). There is deliberately no acknowledgement flag:
        # the operator's move is to fix or forget those rows, not to wave
        # them through.
        return "rows_without_a_directory_context"
    return None


__all__ = [
    "MEMIFY_CONTENT_MARKER",
    "MEMIFY_TAGS",
    "project_id_backfill",
    "rekey_discover_directories",
]


@observe(tier="stage", metric="backend.admin.rekey.discover")
def rekey_discover_directories(payload: dict) -> dict:
    """Car D — count DISTINCT ``directory_context`` values in the corpus.

    Reads ``directory_context`` from ``memory`` + ``wiki_page`` (lightweight
    projection, no full rows) and returns the aggregate as
    ``{directory_context: {memory_rows: int, wiki_rows: int}}`` so the
    host-side migration can derive ``owner/repo`` for each path and
    write the operator-reviewable map.

    Car D's host-side dry-run goes through this op via ``_forward_admin``
    rather than importing a storage handle (the migration is core-side;
    layer-boundary import-linter forbids it).

    ALSO returns ``cohorts`` — the SUB-counts the host cannot compute.
    ``directory_context = 'global'`` is not one decision but two (§1.5 G and
    D4): the ``_memify_derive`` producer's rows are DELETED, the remainder
    gets ``local/aws-work`` plus the ``global`` reach tag. The discriminator
    is a CONTENT+TAGS predicate, so a host holding only per-directory counts
    cannot split it — the count has to come from here, and it has to come
    from ``_is_memify_global`` itself rather than a second copy of the
    four-way signature that would drift from it.

    The extra read is scoped to ``directory_context = 'global'`` (~350 rows
    live) rather than widening the projection above: ``_scan``'s docstring
    exists because pulling ``content`` across ``wiki_page`` drags 2,343 full
    page bodies.
    """
    storage = _get_storage()
    if storage is None:
        return {"ok": False, "reason": "storage_unavailable"}

    counts: dict[str, dict[str, int]] = {}
    for table in _TABLES:
        rows = storage._q(  # noqa: SLF001 — established migration idiom
            f"SELECT directory_context FROM {table}"  # noqa: S608
        )
        for row in rows or ():
            dc = row.get("directory_context")
            if dc is None:
                continue
            bucket = counts.setdefault(str(dc), {"memory_rows": 0, "wiki_rows": 0})
            bucket["memory_rows" if table == "memory" else "wiki_rows"] += 1

    global_rows = storage._q(  # noqa: SLF001
        "SELECT id, directory_context, tags, content FROM memory WHERE directory_context = $dc",
        {"dc": GLOBAL_SENTINEL},
    )
    memify_rows = sum(1 for row in global_rows or () if _is_memify_global(row))

    return {
        "ok": True,
        "counts": counts,
        "cohorts": {
            # wiki_rows is 0 by construction, not by measurement: D4's
            # signature keys on ``content``+``tags``, and _memify_derive
            # writes to ``memory`` only.
            "memify_global": {"memory_rows": memify_rows, "wiki_rows": 0},
        },
    }
