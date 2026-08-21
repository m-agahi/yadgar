"""Car 1 (ledger tasks 309 + 89) — stamp ``project_id`` on the graph tables.

WHAT IS MISSING AND WHY IT MATTERS
----------------------------------
``project_id`` is the sole scoping key (ADR-0233 / ADR-0227). C6's
operator-reviewed backfill covered ``memory`` and ``wiki_page`` — both now
100% stamped. Six tables it never named are not. Measured live 2026-08-21
(``db_inspect``):

===============  ======  ==================
table            rows    missing project_id
===============  ======  ==================
entity            2052                 2052
relationship      5560                 5560
memory_cluster    3175                 3175
checkpoint         160                  157
memory_block        52                   50
episode              3                    3
===============  ======  ==================

10,997 rows with no identity. A later car flips ``checkpoint_restore.py``
onto ``project_id``; every unstamped row goes invisible the moment it lands.
This op ships in an EARLIER release than that reader flip, deliberately.

WHY DERIVING FROM THE CORPUS IS NOT THE ADR-0227 FAILURE
--------------------------------------------------------
``project_backfill``'s module docstring opens with **THE OP DERIVES
NOTHING**, and that rule is intact here. What it forbids is a container with
no git and no host mounts *inventing* an identity from a path — migration
031's in-migration backfill, which would have stamped ``local/<basename>``
on every row, silently and always.

This op invents nothing. Every stamp is INHERITED from a row whose
``project_id`` a host-resolved, operator-reviewed backfill already
adjudicated. That is the same move ``resolve_project_id_from_rows`` makes for
sessionless writers, and its own docstring says why it is legitimate: *"This
is NOT a derivation. Each row's project_id was stamped by the session that
wrote it; reading it back is inheritance."* This module reuses that helper
rather than restating its voting rule, so the two cannot drift.

The operator ``mapping`` override still takes precedence, and every derived
map entry is IN the manifest, so the whole basis is reviewable before an
apply.

THE SIX DERIVATIONS
-------------------
``entity``
    ``name`` of the form ``memory:N`` (1789 of 2052 live, and every one of
    them names a memory row that still exists — measured, 0 dead pointers)
    inherits that memory's ``project_id``.

    Every OTHER entity is UNDECIDABLE and stays that way. ``insert_entity``
    is preceded by a global ``get_entity_by_name``, so ``ValidationError`` is
    ONE row that every project mentioning it reinforces. A single owner is
    not ambiguous, it is *wrong*. Reaching for
    ``find_memory_ids_by_entity_name`` would not help: ``to``, ``where``,
    ``pages`` and ``Error`` are all live entity names and would match nearly
    every memory in the corpus. These rows need a multi-project reach model,
    which is out of this car's scope.

``relationship``
    Both endpoints resolved through the ``entity`` decision above. Same
    project → stamp. Different projects → ``cross_project``, which is REAL
    and expected: ``dream.py`` exists to find cross-project connections.
    A missing endpoint row → ``dangling_endpoint`` (ledger task 89).
    ``source_memory_id`` is set on 0 of 5560 rows, so it is not a shortcut.

``memory_cluster``
    Members are the ``memory`` rows whose ``cluster_id`` names the cluster.
    Live, only 47 of 3175 clusters have ANY member, and most of those span
    several projects — see the module's own report; this table wants a
    vacuum more than it wants a stamp.

``checkpoint`` / ``memory_block`` / ``episode``
    Keyed through the corpus-derived ``directory → project_id`` map
    (``checkpoint.directory_context``; ``memory_block.directory``;
    ``episode.directory``). The three already-stamped checkpoints and two
    already-stamped memory_blocks carry exactly this correspondence, so the
    rule mirrors what a human already did by hand.

REACH IS NOT OWNERSHIP
----------------------
``global`` / ``system`` / ``unresolved`` / ``""`` are the manufactured
identities ADR-0227 deletes and the pre-v5.64 mis-stamp sink. They are
excluded from the map as ``reach_markers`` — a NAMED bucket, distinct from
genuine ambiguity, because they are different facts: a reach marker has no
owner axis at all, whereas ``/home/max/quinyx/qwfm/services/tools/db-copy``
(live: claimed by both ``quinyx/qwfm`` and ``quinyx/db-copy``) has two.
Collapsing them into one "ambiguous" list is how the reach decision gets
silently reversed.

DRY-RUN PARITY
--------------
Car 19 (ledger task 176) established that a dry run which cannot fail the way
the apply fails is worthless. :data:`_WRITE_PATH_GUARDS` and
:func:`_preflight_write_guards` mirror ``adr_seed``'s shape exactly, with one
difference that matters: this op derives MANY distinct targets, so the guard
runs over the WHOLE set on both paths, and the set it checked is recorded in
the manifest. The guard is ``MariaStorageEngine.assert_project_registered``
— the one that actually runs inside the ledger write, NOT
``_ensure_project_exists_sync``, which Car 5 proved has no call site.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage._project_id_writer import (
    _NON_IDENTIFYING_PROJECT_IDS,
    resolve_project_id_from_rows,
)

logger = logging.getLogger(__name__)

#: The guards ``UPDATE ... SET project_id`` must clear. ``assert_project_registered``
#: is the ADR-0078 registry gate that runs INSIDE the ledger write path
#: (``MariaStorageEngine.assert_project_registered``). ``_preflight_write_guards``
#: ITERATES this tuple, so extending it extends the dry run too — that coupling is
#: the whole point of naming it once.
_WRITE_PATH_GUARDS: tuple[str, ...] = ("assert_project_registered",)

#: ``entity.name`` shape that names a memory row. Anchored on both ends: a name
#: like ``memory:1 and memory:2`` is prose, not a pointer.
_MEMORY_ENTITY_RE = re.compile(r"^memory:(\d+)$")

#: How many undecidable rows per table the manifest names individually. The
#: COUNT is always exact and always per-reason; the sample exists so an operator
#: can go look at one, not so they can read 5,000 ids.
_SAMPLE_CAP = 25

#: How many dangling relationship rows (task 89) the manifest names. Higher than
#: ``_SAMPLE_CAP`` because these ids are the deliverable, not an illustration:
#: 487 live, and the delete decision needs the list.
_DANGLING_CAP = 1000

#: Rows are written back in chunks this size. Set-based per ``(table,
#: project_id)`` so the apply is bounded, but keyed on the exact ids the
#: manifest names — an apply must not be able to WIDEN the set it was reviewed on.
_UPDATE_CHUNK = 500

#: Tables this op stamps, in manifest order.
_TABLES: tuple[str, ...] = (
    "entity",
    "relationship",
    "memory_cluster",
    "checkpoint",
    "memory_block",
    "episode",
)


@observe(exempt="one-line lazy-import accessor; the op carries the boundary span")
def _get_storage() -> Any:
    """The composed SurrealDB ``StorageEngine``, or None. Patched by tests."""
    from yadgar._shared.runtime.lifecycle import _get_storage as _live  # noqa: PLC0415

    return _live()


@observe(exempt="one-line slot read; mirrors admin_exec/adr_seed.py's own accessor")
def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None. Patched by tests."""
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    return _st._sql_storage


# ── decisions ──────────────────────────────────────────────────────────────


class _Decision:
    """One row's verdict: a project, or a named reason there isn't one.

    A three-state result rather than ``str | None`` because the two negative
    states are not interchangeable — ``cross_project`` is a row with several
    real owners (leave alone, report), ``undecidable`` is a row with none
    (leave alone, report differently). Collapsing them loses the distinction
    the whole manifest exists to preserve.
    """

    __slots__ = ("project_id", "project_ids", "reason")

    def __init__(
        self,
        project_id: str | None = None,
        *,
        reason: str | None = None,
        project_ids: list[str] | None = None,
    ) -> None:
        self.project_id = project_id
        self.reason = reason
        self.project_ids = project_ids or []

    @property
    def is_cross_project(self) -> bool:
        return self.project_id is None and len(self.project_ids) > 1


def _stamped(project_id: str) -> _Decision:
    return _Decision(project_id)


def _undecidable(reason: str) -> _Decision:
    return _Decision(reason=reason)


def _cross(project_ids: list[str]) -> _Decision:
    return _Decision(project_ids=sorted(set(project_ids)))


@observe(tier="hot", span=False)
def _identifying(value: Any) -> str | None:
    """Return *value* when it names a real project, else None.

    The membership test is ``_project_id_writer``'s own frozenset, imported
    rather than restated: ADR-0227's sentinel list is a policy that must have
    exactly one definition, and a second copy here would be the drift this
    module's own conflict bucket exists to catch.
    """
    if isinstance(value, str) and value not in _NON_IDENTIFYING_PROJECT_IDS:
        return value
    return None


# ── the directory map ──────────────────────────────────────────────────────


@observe(tier="stage")
def _build_directory_map(
    memory: list[dict], wiki: list[dict], overrides: dict[str, str]
) -> tuple[dict[str, str], list[dict], list[str]]:
    """Return ``(map, conflicts, reach_markers)`` from the already-stamped corpus.

    ``memory`` and ``wiki_page`` carry BOTH the legacy directory column and
    the adjudicated ``project_id``, so the correspondence between them is
    evidence rather than inference. A directory whose stamped rows disagree
    yields NO map entry — recorded in ``conflicts`` with every claimant, so
    the operator can settle it with an explicit ``mapping`` entry.

    *overrides* is applied LAST and unconditionally: an operator who names a
    directory has out-of-band knowledge this join does not, including for a
    conflicted key.
    """
    claims: dict[str, set[str]] = {}
    reach: set[str] = set()
    for row in (*memory, *wiki):
        raw = row.get("directory_context")
        if not isinstance(raw, str) or not raw:
            continue
        if raw in _NON_IDENTIFYING_PROJECT_IDS:
            # A row whose directory IS a reach marker still has an owner; the
            # marker just never becomes a map KEY. Recording it here is what
            # keeps it out of ``conflicts``, where it would read as ambiguity.
            reach.add(raw)
            continue
        owner = _identifying(row.get("project_id"))
        if owner is not None:
            claims.setdefault(raw, set()).add(owner)

    mapping: dict[str, str] = {}
    conflicts: list[dict] = []
    for directory, owners in sorted(claims.items()):
        if len(owners) == 1:
            mapping[directory] = next(iter(owners))
        else:
            conflicts.append({"directory": directory, "project_ids": sorted(owners)})

    mapping.update(overrides)
    return mapping, conflicts, sorted(reach)


@observe(tier="hot", span=False)
def _decide_by_directory(raw: Any, mapping: dict[str, str], conflicted: set[str]) -> _Decision:
    """Resolve one directory-keyed row. Four outcomes, all named."""
    if not isinstance(raw, str) or not raw:
        return _undecidable("no_directory")
    if raw in _NON_IDENTIFYING_PROJECT_IDS:
        return _undecidable("reach_marker_not_an_owner")
    target = mapping.get(raw)
    if target is not None:
        return _stamped(target)
    if raw in conflicted:
        return _undecidable("ambiguous_directory")
    return _undecidable("directory_not_in_map")


# ── per-table derivation ───────────────────────────────────────────────────


@observe(tier="stage")
def _decide_entities(entities: list[dict], memory_by_id: dict[int, dict]) -> dict[int, _Decision]:
    """Verdict per entity row. See the module docstring for why the split is here."""
    out: dict[int, _Decision] = {}
    for row in entities:
        eid = _row_id(row)
        match = _MEMORY_ENTITY_RE.match(str(row.get("name") or ""))
        if match is None:
            out[eid] = _undecidable("shared_by_construction")
            continue
        source = memory_by_id.get(int(match.group(1)))
        if source is None:
            out[eid] = _undecidable("source_memory_missing")
            continue
        owner = resolve_project_id_from_rows([source])
        out[eid] = _stamped(owner) if owner else _undecidable("source_memory_unstamped")
    return out


@observe(tier="stage")
def _decide_relationships(
    relationships: list[dict], entity_decisions: dict[int, _Decision]
) -> tuple[dict[int, _Decision], list[dict]]:
    """Verdict per relationship row, plus the task-89 dangling census.

    A relationship inherits from its endpoints or from nothing: there is no
    other column to read (``source_memory_id`` is set on 0 of 5560 live rows).
    """
    out: dict[int, _Decision] = {}
    dangling: list[dict] = []
    for row in relationships:
        rid = _row_id(row)
        endpoints = [row.get("source_entity_id"), row.get("target_entity_id")]
        resolved = [e for e in endpoints if isinstance(e, int) and e in entity_decisions]
        if len(resolved) != len(endpoints):
            dangling.append(
                {
                    "id": rid,
                    "relationship_type": row.get("relationship_type"),
                    "missing_entity_ids": [
                        e for e in endpoints if isinstance(e, int) and e not in entity_decisions
                    ],
                }
            )
            out[rid] = _undecidable("dangling_endpoint")
            continue
        owners = {entity_decisions[e].project_id for e in resolved}
        if None in owners:
            out[rid] = _undecidable("endpoint_undecidable")
            continue
        named = sorted(o for o in owners if o is not None)
        out[rid] = _stamped(named[0]) if len(named) == 1 else _cross(named)
    return out, dangling


@observe(tier="stage")
def _decide_clusters(clusters: list[dict], memory: list[dict]) -> dict[int, _Decision]:
    """Verdict per cluster, voted by the memories that name it."""
    members: dict[int, list[dict]] = {}
    for row in memory:
        cid = row.get("cluster_id")
        if isinstance(cid, int):
            members.setdefault(cid, []).append(row)

    out: dict[int, _Decision] = {}
    for row in clusters:
        cid = _row_id(row)
        rows = members.get(cid, [])
        if not rows:
            out[cid] = _undecidable("no_members")
            continue
        owner = resolve_project_id_from_rows(rows)
        if owner:
            out[cid] = _stamped(owner)
            continue
        owners = {o for o in (_identifying(r.get("project_id")) for r in rows) if o}
        out[cid] = _cross(sorted(owners)) if len(owners) > 1 else _undecidable("members_unstamped")
    return out


@observe(tier="stage")
def _decide_blocks(blocks: list[dict], mapping: dict[str, str], conflicted: set[str]) -> dict:
    """Verdict per memory_block. ``scope='global'`` has no owner axis to fill."""
    out: dict[int, _Decision] = {}
    for row in blocks:
        bid = _row_id(row)
        if row.get("scope") != "project":
            out[bid] = _undecidable("non_project_scope")
            continue
        out[bid] = _decide_by_directory(row.get("directory"), mapping, conflicted)
    return out


@observe(tier="hot", span=False)
def _row_id(row: dict) -> int:
    """Read the integer id off a row :func:`_scan` already normalised.

    Deliberately NOT an id parser. ``_scan`` runs every row's ``id`` through
    the engine's own ``_extract_id`` — the only thing that knows all three
    shapes the driver returns (a ``RecordID`` object with ``.id`` /
    ``.table_name``, the ``'entity:41'`` string, and a bare int). A private
    ``str(id).split(':')`` here would have handled two of the three and
    raised on the first, inside the scan, killing the whole op on first
    operator contact — with no test able to see it, because a fake returns
    plain strings.
    """
    return int(row["id"])


# ── manifest ───────────────────────────────────────────────────────────────


@observe(tier="stage")
def _scan(storage: Any) -> dict[str, list[dict]]:
    """Read the classification columns for every table. Projections only.

    ``content`` is deliberately projected NOWHERE: no predicate here reads it,
    and ``project_backfill._scan``'s docstring records what pulling it across
    ``wiki_page`` costs (2,343 full page bodies, ADR corpus included).

    Every row's ``id`` is normalised to a plain int HERE, through the engine's
    own ``_extract_id``, so no downstream helper has to know which of the
    driver's three id shapes it is holding. ``_apply`` then writes back
    ``WHERE meta::id(id) IN $ids`` — the int form, verified against the live
    engine (2026-08-21) rather than assumed, because two other candidate
    forms (``type::thing(...)`` in a projection) return Internal Server Error
    on this build.
    """
    reads = {
        "memory": "id, project_id, directory_context, cluster_id",
        "wiki_page": "id, project_id, directory_context",
        "entity": "id, name",
        "relationship": "id, source_entity_id, target_entity_id, relationship_type",
        "memory_cluster": "id",
        "checkpoint": "id, directory_context",
        "memory_block": "id, directory, scope",
        "episode": "id, directory",
    }
    out: dict[str, list[dict]] = {}
    for table, columns in reads.items():
        rows = storage._q(f"SELECT {columns} FROM {table}")  # noqa: SLF001,S608
        out[table] = [{**row, "id": storage._extract_id(row.get("id"))} for row in rows or ()]  # noqa: SLF001
    return out


@observe(tier="stage")
def _report_table(decisions: dict[int, _Decision]) -> tuple[list[dict], dict]:
    """Return ``(plan_entries, report)`` for one table.

    ``plan_entries`` is grouped by project_id and carries EXACT ids — the
    apply replays them rather than re-evaluating a predicate, which is what
    makes "apply exactly what the dry run showed" a property rather than a
    claim.
    """
    by_project: dict[str, list[int]] = {}
    cross: list[dict] = []
    undecidable: list[dict] = []
    reasons: dict[str, int] = {}
    for rid, decision in sorted(decisions.items()):
        if decision.project_id is not None:
            by_project.setdefault(decision.project_id, []).append(rid)
        elif decision.is_cross_project:
            cross.append({"id": rid, "project_ids": decision.project_ids})
        else:
            reason = decision.reason or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
            undecidable.append({"id": rid, "reason": reason})

    plan = [
        {"project_id": pid, "rows": len(ids), "ids": ids} for pid, ids in sorted(by_project.items())
    ]
    report = {
        "rows_seen": len(decisions),
        "rows_stamped": sum(len(ids) for ids in by_project.values()),
        "rows_cross_project": len(cross),
        "rows_undecidable": len(undecidable),
        "undecidable_by_reason": dict(sorted(reasons.items())),
        "undecidable_sample": undecidable[:_SAMPLE_CAP],
        "undecidable_sample_truncated": len(undecidable) > _SAMPLE_CAP,
        "cross_project": cross,
    }
    return plan, report


@observe(tier="stage")
def _build_manifest(storage: Any, payload: dict, *, dry_run: bool) -> dict[str, Any]:
    """Scan, classify, count. PURE — issues no mutation of any kind.

    Split from the op body so the read half is one function with one job: the
    dry-run path returns exactly this and nothing else runs, which makes "a
    dry run cannot write" true by construction rather than by inspection.
    """
    scanned = _scan(storage)
    overrides = {str(k): str(v) for k, v in (payload.get("mapping") or {}).items()}
    mapping, conflicts, reach = _build_directory_map(
        scanned["memory"], scanned["wiki_page"], overrides
    )
    conflicted = {c["directory"] for c in conflicts}
    memory_by_id = {_row_id(r): r for r in scanned["memory"]}

    entity_decisions = _decide_entities(scanned["entity"], memory_by_id)
    rel_decisions, dangling = _decide_relationships(scanned["relationship"], entity_decisions)
    per_table: dict[str, dict[int, _Decision]] = {
        "entity": entity_decisions,
        "relationship": rel_decisions,
        "memory_cluster": _decide_clusters(scanned["memory_cluster"], scanned["memory"]),
        "checkpoint": {
            _row_id(r): _decide_by_directory(r.get("directory_context"), mapping, conflicted)
            for r in scanned["checkpoint"]
        },
        "memory_block": _decide_blocks(scanned["memory_block"], mapping, conflicted),
        "episode": {
            _row_id(r): _decide_by_directory(r.get("directory"), mapping, conflicted)
            for r in scanned["episode"]
        },
    }

    plan: dict[str, list[dict]] = {}
    tables: dict[str, dict] = {}
    for table in _TABLES:
        plan[table], tables[table] = _report_table(per_table[table])

    return {
        "ok": True,
        "error": None,
        "dry_run": dry_run,
        "applied": False,
        "directory_map": mapping,
        "map_conflicts": conflicts,
        "reach_markers": reach,
        "plan": plan,
        "tables": tables,
        "dangling_relationships": {
            "count": len(dangling),
            "by_type": _count_by_type(dangling),
            "rows": dangling[:_DANGLING_CAP],
            "rows_truncated": len(dangling) > _DANGLING_CAP,
        },
        "totals": {
            "rows_seen": sum(t["rows_seen"] for t in tables.values()),
            "rows_stamped": sum(t["rows_stamped"] for t in tables.values()),
            "rows_cross_project": sum(t["rows_cross_project"] for t in tables.values()),
            "rows_undecidable": sum(t["rows_undecidable"] for t in tables.values()),
        },
    }


@observe(tier="hot", span=False)
def _count_by_type(dangling: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in dangling:
        key = str(row.get("relationship_type") or "unknown")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


@observe(tier="hot", span=False)
def _manifest_targets(manifest: dict) -> list[str]:
    """Every distinct project_id the plan would write. The guard set."""
    return sorted(
        {entry["project_id"] for entries in manifest["plan"].values() for entry in entries}
    )


# ── guards ─────────────────────────────────────────────────────────────────


@observe(tier="hot", span=False)
def _invalid_override(overrides: dict[str, str]) -> str | None:
    """Reject an operator mapping that names a sentinel as an OWNER.

    ADR-0227's manufactured identities are refused as VALUES wherever they
    come from. An override is the one path that could otherwise smuggle
    ``global`` into ``project_id`` — the corpus-derived half filters them at
    both ends, so without this the override would be the looser door.
    """
    bad = sorted({v for v in overrides.values() if _identifying(v) is None})
    if bad:
        return (
            f"mapping targets {bad} name no project — 'global', 'system', 'unresolved' and "
            "the empty string are reach markers and mis-stamp sinks (ADR-0227), never owners"
        )
    return None


@observe(tier="boundary", metric="backend.admin.identity_stamp._preflight_write_guards")
async def _preflight_write_guards(sql_storage: Any, project_ids: list[str]) -> str | None:
    """Run every :data:`_WRITE_PATH_GUARDS` guard over EVERY target. Error text or None.

    This is what makes a clean dry run evidence: the same validation the write
    path performs runs on the preview too, so an input the apply would reject
    is rejected by the preview at the same point (Car 19 / task 176).

    Over the whole target set, not one: this op derives many distinct
    project_ids, and a guard that checked only the first would pass a preview
    whose apply dies partway through — which is the same defect wearing a
    different hat.

    Returns a string rather than raising: ``admin_exec`` pins the never-raise
    error model, and the CLI already keys on ``ok is False`` BEFORE its
    dry-run ``return 0``.

    Three rejections, all of them "the apply cannot succeed from here":

    1. **The guard says no** — ``UnknownProjectError`` or anything else it
       raises.
    2. **No ledger handle at all** (engine #2 not composed). Without this
       branch the preview reads clean and the apply dies on the first write.
    3. **The handle cannot run the guard** (method absent) — the task-168
       wrong-engine shape. "Could not check" is not "checked and passed".
    """
    if not project_ids:
        return None
    if sql_storage is None:
        return (
            "the ledger handle is absent (engine #2 not composed), so the write-path "
            f"guards {list(_WRITE_PATH_GUARDS)} could not run — a dry run cannot "
            "predict an apply it was unable to validate"
        )
    for guard_name in _WRITE_PATH_GUARDS:
        guard = getattr(sql_storage, guard_name, None)
        if guard is None:
            return (
                f"the ledger surface has no {guard_name!r}, so the write-path guard "
                "could not run; a handle that cannot be checked is not a handle that passed"
            )
        for project_id in project_ids:
            try:
                result_obj = guard(project_id)
                if hasattr(result_obj, "__await__"):
                    await result_obj
            except Exception as exc:  # noqa: BLE001 — every guard failure is fatal here
                return (
                    f"write-path guard {guard_name!r} rejected project_id {project_id!r} "
                    f"({type(exc).__name__}: {exc}); the apply would fail identically on "
                    "every row targeting it"
                )
    return None


# ── apply ──────────────────────────────────────────────────────────────────


@observe(tier="stage")
def _apply(storage: Any, manifest: dict) -> None:
    """Execute the manifest. Ids only — the apply cannot widen its own set.

    Set-based per ``(table, project_id)`` in chunks so a 5,000-row table is
    not 5,000 round-trips, but keyed on the exact ids the operator reviewed
    rather than on a re-evaluated ``WHERE`` predicate.

    Idempotent by construction: re-running stamps the same value on the same
    ids.
    """
    for table in _TABLES:
        for entry in manifest["plan"][table]:
            ids = entry["ids"]
            for start in range(0, len(ids), _UPDATE_CHUNK):
                chunk = ids[start : start + _UPDATE_CHUNK]
                storage._q(  # noqa: SLF001 — the established migration/admin-op idiom
                    f"UPDATE {table} SET project_id = $pid WHERE meta::id(id) IN $ids",  # noqa: S608
                    {"pid": entry["project_id"], "ids": chunk},
                )


@observe(tier="boundary", metric="backend.admin.identity_stamp")
async def stamp_project_id(payload: dict) -> dict:
    """Stamp ``project_id`` on the six graph tables. Dry-run by default.

    payload:
      ``dry_run``  default True — returns the manifest, writes nothing.
      ``mapping``  optional ``{directory: project_id}`` override. Wins over
                   the corpus-derived map, including for a conflicted key.
                   Sentinel targets are refused (ADR-0227).

    Returns the manifest either way. On refusal ``ok`` is False and ``error``
    names the reason, with the FULL manifest still attached so the operator
    sees what would have happened.
    """
    dry_run = bool(payload.get("dry_run", True))
    base = {"ok": False, "dry_run": dry_run, "applied": False}

    storage = _get_storage()
    if storage is None:
        return {**base, "error": "storage_unavailable — no SurrealDB engine is composed"}

    overrides = {str(k): str(v) for k, v in (payload.get("mapping") or {}).items()}
    override_error = _invalid_override(overrides)
    if override_error is not None:
        return {**base, "error": override_error}

    manifest = _build_manifest(storage, payload, dry_run=dry_run)
    targets = _manifest_targets(manifest)
    manifest["guards"] = {"names": list(_WRITE_PATH_GUARDS), "checked_project_ids": targets}

    structural_error = await _preflight_write_guards(_get_sql_storage(), targets)
    if structural_error is not None:
        manifest["ok"] = False
        manifest["error"] = structural_error
        logger.warning("stamp_project_id REFUSED: %s", structural_error)
        return manifest

    if dry_run:
        return manifest

    _apply(storage, manifest)
    manifest["applied"] = True
    logger.info("stamp_project_id applied: %s", manifest["totals"])
    return manifest


__all__ = ["stamp_project_id"]
