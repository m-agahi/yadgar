"""Car 1 (ledger tasks 309 + 89) — ``stamp_project_id``, the graph-table backfill.

WHY THIS OP EXISTS
------------------
``project_id`` is the sole scoping key (ADR-0233 / ADR-0227). ``memory`` and
``wiki_page`` were stamped by C6's operator-reviewed backfill. Six other tables
were never covered — measured live 2026-08-21 via ``db_inspect``:

    entity 2052/2052 unstamped, relationship 5560/5560, memory_cluster
    3175/3175, checkpoint 157/160, memory_block 50/52, episode 3/3.

A later car flips ``checkpoint_restore.py:522`` onto ``project_id``; anything
unstamped goes invisible the moment it lands.

WHAT THESE TESTS PIN
--------------------
1. **Derivation, never invention.** Every stamp traces to a row that a
   host-resolved, operator-reviewed backfill already adjudicated. A row whose
   owner is ambiguous, absent or genuinely multi-project lands in a NAMED
   bucket and is left alone — a wrong stamp makes a row reachable from the
   wrong project, which is silent corruption and strictly worse than a gap.
2. **Reach is not ownership.** ``global`` / ``system`` / ``unresolved`` are
   reach markers and mis-stamp sinks, not owners (ADR-0227, §1.4). They get
   their own bucket, distinct from genuine ambiguity — two different facts.
3. **Dry-run parity** (Car 19 / ledger task 176 discipline). Whatever guard
   the write path runs, the dry run runs — over EVERY derived target, not one.
   A preview that cannot fail the way the apply fails is worthless.
4. **The apply writes exactly the manifest.** Ids, not predicates.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from yadgar._shared.storage.client import _ClientMixin
from yadgar.backend.admin_exec import identity_stamp
from yadgar.backend.admin_exec.identity_stamp import (
    _WRITE_PATH_GUARDS,
    stamp_project_id,
)

_YADGAR = "/home/max/git/yadgar"
_QWFM = "/home/max/quinyx/qwfm"
_YADGAR_ID = "m-agahi/yadgar"
_QWFM_ID = "quinyx/qwfm"

_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


class _UnknownProjectError(RuntimeError):
    """Stand-in for the ADR-0078 registry guard's error.

    Deliberately not ``AttributeError`` / ``TypeError``: the op must not
    recognise a structural fault by exception type (task 175's lesson).
    """


class _FakeStorage:
    """In-memory SurrealDB double.

    Answers the op's per-table discovery SELECTs off ``tables`` and RECORDS
    every statement, so a test can assert a dry run issued no mutation at all
    — the property the whole design rests on.
    """

    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.tables = tables
        self.statements: list[tuple[str, dict | None]] = []

    def _q(self, query: str, params: dict | None = None) -> list[dict]:
        self.statements.append((query, params))
        if not query.strip().upper().startswith("SELECT"):
            return []
        match = _FROM_RE.search(query)
        table = match.group(1) if match else ""
        return [dict(r) for r in self.tables.get(table, [])]

    @property
    def mutations(self) -> list[tuple[str, dict | None]]:
        return [(q, p) for q, p in self.statements if not q.strip().upper().startswith("SELECT")]

    #: THE REAL extractor, bound off ``_ClientMixin`` rather than reimplemented.
    #: A private ``int(str(raw).split(':')[-1])`` in the fake handles the string
    #: shape and silently blesses an op that would raise on the ``RecordID``
    #: object the live driver actually returns — i.e. the fake would certify
    #: exactly the bug it exists to catch.
    _extract_id = _ClientMixin._extract_id


class _LedgerFake:
    """Ledger handle carrying the guard the real write path reaches.

    ``assert_project_registered`` is the ADR-0078 gate that lives INSIDE the
    row write (``MariaStorageEngine.assert_project_registered``) — the
    reachable one, and since task 384 the only one: the standalone
    ``admin_exec`` guard Car 5 proved unusable by construction is deleted.
    """

    def __init__(self, known: list[str]) -> None:
        self.known = set(known)
        self.checked: list[str] = []

    async def assert_project_registered(self, project_id: str) -> None:
        self.checked.append(project_id)
        if project_id not in self.known:
            raise _UnknownProjectError(f"project {project_id!r} is not registered")


class _NoGuardLedger:
    """A handle with ZERO guard methods — the task-168 wrong-engine shape."""


def _corpus() -> dict[str, list[dict]]:
    """A miniature carrying every measured class, with small counts."""
    return {
        "memory": [
            # Stamped rows — these are the evidence every derivation joins to.
            {
                "id": "memory:1",
                "project_id": _YADGAR_ID,
                "directory_context": _YADGAR,
                "cluster_id": 10,
            },
            {
                "id": "memory:2",
                "project_id": _YADGAR_ID,
                "directory_context": _YADGAR,
                "cluster_id": 10,
            },
            {
                "id": "memory:3",
                "project_id": _QWFM_ID,
                "directory_context": _QWFM,
                "cluster_id": 11,
            },
            # `global` directory_context: a REACH marker, never an owner. Its
            # OWNER is still real, and differs from its cluster-mate's — which
            # is what makes cluster 11 genuinely cross-project.
            {
                "id": "memory:4",
                "project_id": _YADGAR_ID,
                "directory_context": "global",
                "cluster_id": 11,
            },
            # A directory two projects disagree about — genuine ambiguity.
            {
                "id": "memory:5",
                "project_id": _YADGAR_ID,
                "directory_context": "/shared/dir",
                "cluster_id": None,
            },
            {
                "id": "memory:6",
                "project_id": _QWFM_ID,
                "directory_context": "/shared/dir",
                "cluster_id": None,
            },
        ],
        "wiki_page": [
            {"id": "wiki_page:1", "project_id": _YADGAR_ID, "directory_context": _YADGAR},
        ],
        "entity": [
            # `memory:N` — the unambiguous class (1789 of 2052 live).
            {"id": "entity:1", "name": "memory:1"},
            {"id": "entity:2", "name": "memory:2"},
            {"id": "entity:3", "name": "memory:3"},
            # Content-extracted — ONE row reinforced by every project that
            # mentions the name (`insert_entity` is preceded by a global
            # `get_entity_by_name`). A single owner is not ambiguous, it is
            # wrong.
            {"id": "entity:4", "name": "ValidationError"},
            # Points at a memory row that no longer exists.
            {"id": "entity:5", "name": "memory:999"},
        ],
        "relationship": [
            # both endpoints resolve to the same project
            {
                "id": "relationship:1",
                "source_entity_id": 1,
                "target_entity_id": 2,
                "relationship_type": "derived_from",
            },
            # endpoints resolve to DIFFERENT projects — real, and not a bug
            {
                "id": "relationship:2",
                "source_entity_id": 1,
                "target_entity_id": 3,
                "relationship_type": "derived_from",
            },
            # endpoint entity row does not exist — ledger task 89
            {
                "id": "relationship:3",
                "source_entity_id": 1,
                "target_entity_id": 4242,
                "relationship_type": "co_occurrence",
            },
            # endpoint is a content-extracted entity — undecidable
            {
                "id": "relationship:4",
                "source_entity_id": 1,
                "target_entity_id": 4,
                "relationship_type": "co_occurrence",
            },
        ],
        "memory_cluster": [
            {"id": "memory_cluster:10"},  # members 1,2 -> one project
            {"id": "memory_cluster:11"},  # members 3,4 -> two projects
            {"id": "memory_cluster:12"},  # no members at all
        ],
        "checkpoint": [
            {"id": "checkpoint:1", "directory_context": _YADGAR},
            {"id": "checkpoint:2", "directory_context": "global"},
            {"id": "checkpoint:3", "directory_context": "/never/seen"},
            {"id": "checkpoint:4", "directory_context": "/shared/dir"},
        ],
        "memory_block": [
            {"id": "memory_block:1", "directory": _QWFM, "scope": "project"},
            {"id": "memory_block:2", "directory": _YADGAR, "scope": "global"},
        ],
        "episode": [
            {"id": "episode:1", "directory": ""},
            {"id": "episode:2", "directory": _YADGAR},
        ],
    }


def _install(monkeypatch, storage: Any, sql: Any) -> None:
    monkeypatch.setattr(identity_stamp, "_get_storage", lambda: storage)
    monkeypatch.setattr(identity_stamp, "_get_sql_storage", lambda: sql)


def _plan_ids(manifest: dict, table: str, project_id: str) -> list[int]:
    return sorted(
        i
        for entry in manifest["plan"].get(table, [])
        if entry["project_id"] == project_id
        for i in entry["ids"]
    )


def _reasons(manifest: dict, table: str) -> dict[str, int]:
    return manifest["tables"][table]["undecidable_by_reason"]


@pytest.fixture
def ledger() -> _LedgerFake:
    return _LedgerFake([_YADGAR_ID, _QWFM_ID])


@pytest.fixture
def storage() -> _FakeStorage:
    return _FakeStorage(_corpus())


# ── derivation ─────────────────────────────────────────────────────────────


class TestEntityDerivation:
    async def test_memory_named_entity_inherits_its_memory_project(
        self, monkeypatch, storage, ledger
    ):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _plan_ids(m, "entity", _YADGAR_ID) == [1, 2]
        assert _plan_ids(m, "entity", _QWFM_ID) == [3]

    async def test_content_extracted_entity_is_undecidable_not_guessed(
        self, monkeypatch, storage, ledger
    ):
        """`ValidationError` is ONE row every project reinforces. Not ambiguous — wrong."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _reasons(m, "entity")["shared_by_construction"] == 1
        assert 4 not in [i for e in m["plan"]["entity"] for i in e["ids"]]

    async def test_entity_naming_a_dead_memory_is_undecidable(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _reasons(m, "entity")["source_memory_missing"] == 1


class TestRelationshipDerivation:
    async def test_same_project_endpoints_stamp(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _plan_ids(m, "relationship", _YADGAR_ID) == [1]

    async def test_endpoints_in_different_projects_are_cross_project(
        self, monkeypatch, storage, ledger
    ):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        cross = [c for c in m["tables"]["relationship"]["cross_project"] if c["id"] == 2]
        assert cross and sorted(cross[0]["project_ids"]) == sorted([_YADGAR_ID, _QWFM_ID])

    async def test_dangling_endpoint_is_reported_with_its_id(self, monkeypatch, storage, ledger):
        """Ledger task 89 — the rows ``check_invariants`` counts but cannot repair."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        dangling = m["dangling_relationships"]
        assert dangling["count"] == 1
        assert dangling["by_type"] == {"co_occurrence": 1}
        assert [r["id"] for r in dangling["rows"]] == [3]
        assert dangling["rows"][0]["missing_entity_ids"] == [4242]

    async def test_dangling_rows_are_never_stamped(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        stamped = {i for e in m["plan"]["relationship"] for i in e["ids"]}
        assert 3 not in stamped
        assert _reasons(m, "relationship")["dangling_endpoint"] == 1

    async def test_undecidable_endpoint_propagates(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _reasons(m, "relationship")["endpoint_undecidable"] == 1


class TestMemoryClusterDerivation:
    async def test_single_project_members_stamp(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _plan_ids(m, "memory_cluster", _YADGAR_ID) == [10]

    async def test_multi_project_members_are_cross_project(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert [c["id"] for c in m["tables"]["memory_cluster"]["cross_project"]] == [11]

    async def test_member_less_cluster_is_undecidable(self, monkeypatch, storage, ledger):
        """3,128 of 3,175 live clusters have no members at all."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _reasons(m, "memory_cluster")["no_members"] == 1


class TestDirectoryKeyedTables:
    async def test_checkpoint_maps_through_the_corpus_derived_map(
        self, monkeypatch, storage, ledger
    ):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _plan_ids(m, "checkpoint", _YADGAR_ID) == [1]

    async def test_global_directory_is_a_reach_marker_not_an_owner(
        self, monkeypatch, storage, ledger
    ):
        """ADR-0227 / §1.4: reach travels as a tag, NEVER as a project_id."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert "global" not in m["directory_map"]
        assert "global" in m["reach_markers"]
        assert _reasons(m, "checkpoint")["reach_marker_not_an_owner"] == 1

    async def test_directory_two_projects_disagree_about_is_ambiguous(
        self, monkeypatch, storage, ledger
    ):
        """Distinct from a reach marker: two owners claim it, so nobody does."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        conflicts = {c["directory"]: sorted(c["project_ids"]) for c in m["map_conflicts"]}
        assert conflicts["/shared/dir"] == sorted([_YADGAR_ID, _QWFM_ID])
        assert _reasons(m, "checkpoint")["ambiguous_directory"] == 1

    async def test_unknown_directory_is_undecidable(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _reasons(m, "checkpoint")["directory_not_in_map"] == 1

    async def test_memory_block_keys_on_its_own_directory_column(
        self, monkeypatch, storage, ledger
    ):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _plan_ids(m, "memory_block", _QWFM_ID) == [1]

    async def test_global_scoped_block_has_no_owner_axis(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _reasons(m, "memory_block")["non_project_scope"] == 1

    async def test_episode_without_a_directory_is_undecidable(self, monkeypatch, storage, ledger):
        """All 3 live episodes carry ``directory = ''``; no session join exists."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _reasons(m, "episode")["no_directory"] == 1
        assert _plan_ids(m, "episode", _YADGAR_ID) == [2]


class _RecordID:
    """The driver's id object — ``.id`` + ``.table_name``, and no useful ``str()``.

    ``_extract_id`` branches on exactly these two attributes, and this is the
    shape a private ``str(id).split(':')`` parser raises on. Its ``repr`` is
    deliberately NOT ``table:id``, so a parser that "works by accident" on the
    string form cannot pass this.
    """

    def __init__(self, table_name: str, id: int) -> None:  # noqa: A002
        self.table_name = table_name
        self.id = id


class TestDriverIdShapes:
    """The scan normalises every id shape the live driver returns.

    Measured 2026-08-21: ``db_inspect`` renders ids as ``'entity:1'`` strings
    and ``meta::id(id)`` projections as bare ints, while the Python driver
    hands back ``RecordID`` objects. Only ``_extract_id`` knows all three, and
    an op that parsed ids itself would die inside the scan on first operator
    contact — with a string-shaped fake reporting green.
    """

    async def test_record_id_objects_are_accepted(self, monkeypatch, ledger):
        corpus = _corpus()
        for rows in corpus.values():
            for row in rows:
                table, _, rid = str(row["id"]).partition(":")
                row["id"] = _RecordID(table, int(rid))
        storage = _FakeStorage(corpus)
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _plan_ids(m, "entity", _YADGAR_ID) == [1, 2]

    async def test_bare_integer_ids_are_accepted(self, monkeypatch, ledger):
        corpus = _corpus()
        for rows in corpus.values():
            for row in rows:
                row["id"] = int(str(row["id"]).split(":")[-1])
        storage = _FakeStorage(corpus)
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert _plan_ids(m, "entity", _YADGAR_ID) == [1, 2]


class TestOperatorMapping:
    async def test_operator_mapping_wins_over_the_corpus(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True, "mapping": {_YADGAR: _QWFM_ID}})
        assert _plan_ids(m, "checkpoint", _QWFM_ID) == [1]
        assert m["directory_map"][_YADGAR] == _QWFM_ID

    async def test_operator_mapping_resolves_a_conflicted_directory(
        self, monkeypatch, storage, ledger
    ):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True, "mapping": {"/shared/dir": _YADGAR_ID}})
        assert 4 in _plan_ids(m, "checkpoint", _YADGAR_ID)

    async def test_operator_mapping_cannot_mint_a_reach_marker_as_an_owner(
        self, monkeypatch, storage, ledger
    ):
        """ADR-0227's sentinels are refused as VALUES, wherever they come from."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True, "mapping": {_YADGAR: "global"}})
        assert m["ok"] is False
        assert "global" in m["error"]


# ── dry-run parity (Car 19 discipline) ─────────────────────────────────────


class TestDryRunGuardParity:
    def test_guard_tuple_names_the_reachable_registry_gate(self):
        assert _WRITE_PATH_GUARDS == ("assert_project_registered",)

    async def test_dry_run_runs_the_write_path_guard(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        await stamp_project_id({"dry_run": True})
        assert set(ledger.checked) == {_YADGAR_ID, _QWFM_ID}

    async def test_guard_runs_over_every_derived_target_not_one(self, monkeypatch, storage):
        """One unregistered target among many must fail the PREVIEW, not the apply."""
        partial = _LedgerFake([_YADGAR_ID])
        _install(monkeypatch, storage, partial)
        m = await stamp_project_id({"dry_run": True})
        assert m["ok"] is False
        assert _QWFM_ID in m["error"]
        assert m["applied"] is False

    async def test_absent_ledger_handle_is_an_error_not_a_clean_preview(self, monkeypatch, storage):
        _install(monkeypatch, storage, None)
        m = await stamp_project_id({"dry_run": True})
        assert m["ok"] is False
        assert "assert_project_registered" in m["error"]

    async def test_handle_without_the_guard_method_is_an_error(self, monkeypatch, storage):
        _install(monkeypatch, storage, _NoGuardLedger())
        m = await stamp_project_id({"dry_run": True})
        assert m["ok"] is False
        assert "assert_project_registered" in m["error"]

    async def test_a_rejected_preview_still_shows_the_manifest(self, monkeypatch, storage):
        _install(monkeypatch, storage, _LedgerFake([_YADGAR_ID]))
        m = await stamp_project_id({"dry_run": True})
        assert m["totals"]["rows_seen"] > 0
        assert m["plan"]["entity"]

    async def test_apply_refuses_on_the_same_guard_failure(self, monkeypatch, storage):
        _install(monkeypatch, storage, _LedgerFake([_YADGAR_ID]))
        m = await stamp_project_id({"dry_run": False})
        assert m["ok"] is False
        assert m["applied"] is False
        assert storage.mutations == []


# ── writes ─────────────────────────────────────────────────────────────────


class TestApply:
    async def test_dry_run_issues_no_mutation_at_all(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        await stamp_project_id({"dry_run": True})
        assert storage.mutations == []

    async def test_apply_writes_exactly_the_manifest(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": False})
        assert m["ok"] is True and m["applied"] is True
        written: dict[str, set[tuple[int, str]]] = {}
        for query, params in storage.mutations:
            assert query.strip().upper().startswith("UPDATE")
            table = query.split()[1]
            for rid in params["ids"]:
                written.setdefault(table, set()).add((rid, params["pid"]))
        planned = {
            table: {(i, entry["project_id"]) for entry in entries for i in entry["ids"]}
            for table, entries in m["plan"].items()
            if entries
        }
        assert written == planned

    async def test_apply_never_writes_a_bucketed_row(self, monkeypatch, storage, ledger):
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": False})
        touched = {
            (query.split()[1], rid) for query, params in storage.mutations for rid in params["ids"]
        }
        for table, report in m["tables"].items():
            for entry in report["cross_project"]:
                assert (table, entry["id"]) not in touched
            for entry in report["undecidable_sample"]:
                assert (table, entry["id"]) not in touched

    async def test_totals_account_for_every_row_seen(self, monkeypatch, storage, ledger):
        """No row may count in ``rows_seen`` and land in no bucket."""
        _install(monkeypatch, storage, ledger)
        m = await stamp_project_id({"dry_run": True})
        t = m["totals"]
        assert t["rows_seen"] == t["rows_stamped"] + t["rows_cross_project"] + t["rows_undecidable"]

    async def test_storage_unavailable_is_an_error(self, monkeypatch, ledger):
        _install(monkeypatch, None, ledger)
        m = await stamp_project_id({"dry_run": True})
        assert m["ok"] is False
        assert "storage" in m["error"]
