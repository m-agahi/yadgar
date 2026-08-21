"""Batched (set-based) heat-decay writes — equivalence + safety guards.

WHY THIS EXISTS (ledger task 14, car 10)
========================================
``_decay_memories`` / ``_decay_entities`` emit ONE ``UPDATE`` intent per changed
row.  Measured on the live corpus 2026-08-21: 1740 decay-eligible memory rows.
In server mode ``_build_chunk_body`` expands every intent into 3 ``LET`` + 1
``UPDATE``, so the DB parses ~4 statements per row (~6960 for the corpus) every
cycle; in embedded mode (the nightly cycle) ``batch_writes`` executes each intent
as its own round-trip.

``_compact_heat_intents`` folds a run of identical-template intents into a
bounded number of ``FOR $r IN [...] { UPDATE $r.i SET ... }`` statements.

The decay ARITHMETIC deliberately stays in Python — see the module docstring of
``heat_decay`` for why — so equivalence is structural, not numerical.  These
tests prove it at the DB level anyway: the same fixture decayed with compaction
ON and with compaction OFF must leave byte-identical rows behind.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from yadgar._shared.config import get_settings
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar.backend.consolidation import heat_decay as hd
from yadgar.backend.consolidation.heat_decay import (
    _ENT_DECAY_COLD_SQL,
    _ENT_DECAY_SQL,
    _MAX_ROWS_PER_FOR,
    _MEM_DECAY_SQL,
    _compact_heat_intents,
    _HeatDecayMixin,
)

_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
_THREE_DAYS_AGO = (_NOW - timedelta(days=3)).isoformat()
_TWO_YEARS_AGO = (_NOW - timedelta(days=730)).isoformat()
_DIR = "/tmp/test_heat_decay_batched"


# ---------------------------------------------------------------------------
# Part 1 — the compactor as a pure function
# ---------------------------------------------------------------------------


class TestCompactorShape:
    def test_memory_intents_collapse_to_one_statement(self):
        now = _NOW.isoformat()
        intents = [(_MEM_DECAY_SQL, {"id": i, "heat": 0.5, "now": now}) for i in range(1, 11)]

        out = _compact_heat_intents(intents)

        assert len(out) == 1, f"10 memory intents must collapse to 1 statement, got {len(out)}"
        sql, params = out[0]
        assert sql.startswith("FOR $r IN [")
        assert "UPDATE $r.i SET heat = $r.h" in sql
        assert "access_count_since_decay = 0" in sql
        assert params == {"now": now}
        for i in range(1, 11):
            assert f"memory:{i}" in sql

    def test_entity_templates_group_separately(self):
        """The cold entity template (archived = true) must not merge with the warm one."""
        now = _NOW.isoformat()
        intents = [
            (_ENT_DECAY_SQL, {"id": 1, "heat": 0.4, "now": now}),
            (_ENT_DECAY_COLD_SQL, {"id": 2, "heat": 0.0, "now": now}),
            (_ENT_DECAY_SQL, {"id": 3, "heat": 0.3, "now": now}),
        ]

        out = _compact_heat_intents(intents)

        assert len(out) == 2, f"two entity templates → two groups, got {len(out)}"
        warm = [s for s, _ in out if "archived" not in s]
        cold = [s for s, _ in out if "archived = true" in s]
        assert len(warm) == 1 and len(cold) == 1
        assert "entity:1" in warm[0] and "entity:3" in warm[0]
        assert "entity:2" in cold[0] and "entity:2" not in warm[0]

    def test_memory_and_entity_never_merge(self):
        now = _NOW.isoformat()
        intents = [
            (_MEM_DECAY_SQL, {"id": 1, "heat": 0.5, "now": now}),
            (_ENT_DECAY_SQL, {"id": 1, "heat": 0.4, "now": now}),
        ]

        out = _compact_heat_intents(intents)

        assert len(out) == 2
        assert any("memory:1" in s and "entity:" not in s for s, _ in out)
        assert any("entity:1" in s and "memory:" not in s for s, _ in out)

    def test_chunked_at_max_rows_and_no_empty_tail(self):
        now = _NOW.isoformat()
        n = _MAX_ROWS_PER_FOR * 2  # exact multiple — the empty-tail trap
        intents = [(_MEM_DECAY_SQL, {"id": i, "heat": 0.5, "now": now}) for i in range(1, n + 1)]

        out = _compact_heat_intents(intents)

        assert len(out) == 2, f"{n} rows at {_MAX_ROWS_PER_FOR}/stmt → 2 statements, got {len(out)}"
        assert all("FOR $r IN []" not in s for s, _ in out), "emitted an empty FOR array"

    def test_distinct_now_values_do_not_merge(self):
        a, b = _NOW.isoformat(), (_NOW + timedelta(seconds=1)).isoformat()
        intents = [
            (_MEM_DECAY_SQL, {"id": 1, "heat": 0.5, "now": a}),
            (_MEM_DECAY_SQL, {"id": 2, "heat": 0.5, "now": b}),
        ]

        out = _compact_heat_intents(intents)

        assert len(out) == 2
        assert {p["now"] for _, p in out} == {a, b}

    def test_empty_input_is_empty_output(self):
        assert _compact_heat_intents([]) == []


class TestCompactorSafety:
    """Anything the compactor cannot prove safe is forwarded per-row, untouched."""

    @pytest.mark.parametrize(
        "bad_params",
        [
            {"id": "1", "heat": 0.5, "now": "x"},  # str id — never inline
            {"id": True, "heat": 0.5, "now": "x"},  # bool is an int subclass
            {"id": 1, "heat": float("inf"), "now": "x"},  # non-finite literal
            {"id": 1, "heat": float("nan"), "now": "x"},
            {"id": 1, "heat": "0.5", "now": "x"},  # str heat
            {"id": 1, "heat": 0.5, "now": 12345},  # non-str now
            {"id": 1, "heat": 0.5},  # missing now
        ],
    )
    def test_unsafe_params_pass_through(self, bad_params):
        intents = [(_MEM_DECAY_SQL, bad_params)]
        assert _compact_heat_intents(intents) == intents

    def test_unknown_template_passes_through(self):
        intents = [("UPDATE type::record('memory', $id) SET heat = $heat", {"id": 1, "heat": 0.5})]
        assert _compact_heat_intents(intents) == intents

    def test_none_params_pass_through(self):
        intents = [("DELETE memory:1", None)]
        assert _compact_heat_intents(intents) == intents

    def test_float_literals_are_round_trip_exact(self):
        """The inlined literal must be the SAME text json.dumps would emit for a LET."""
        now = _NOW.isoformat()
        awkward = [0.1 + 0.2, 1e-05, 0.4999999999999999, 0.0, 1.0]
        intents = [
            (_MEM_DECAY_SQL, {"id": i, "heat": h, "now": now})
            for i, h in enumerate(awkward, start=1)
        ]

        sql, _ = _compact_heat_intents(intents)[0]

        for h in awkward:
            assert f"h: {json.dumps(h)}" in sql, f"{h!r} not inlined round-trip-exact"

    def test_compaction_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("YADGAR_BATCHED_DECAY_WRITES_ENABLED", "0")
        now = _NOW.isoformat()
        intents = [(_MEM_DECAY_SQL, {"id": i, "heat": 0.5, "now": now}) for i in range(1, 6)]

        assert _compact_heat_intents(intents) == intents


class TestCompactedSqlSurvivesTheChunkBuilder:
    """The compacted statement must survive `_build_chunk_body`'s param rewrite.

    `_prefix_param_tokens` rewrites `$now` → `$p0_now`; the statement also holds
    `$r`, `$r.i`, `$r.h` (the FOR-loop variable, NOT a param). A regex that ate
    those would be silent until production.
    """

    def test_only_the_named_param_is_rewritten(self):
        import json as _json

        now = _NOW.isoformat()
        intents = [(_MEM_DECAY_SQL, {"id": i, "heat": 0.5, "now": now}) for i in (1, 2)]
        compacted = _compact_heat_intents(intents)

        body = StorageEngine._build_chunk_body(compacted, _json).decode()

        assert f'LET $p0_now = "{now}"' in body
        assert "$p0_now" in body
        assert "$r.i" in body, "FOR-loop variable was rewritten"
        assert "$r.h" in body, "FOR-loop variable was rewritten"
        assert "$p0_r" not in body


# ---------------------------------------------------------------------------
# Part 2 — DB-level differential: compaction ON vs OFF must be indistinguishable
# ---------------------------------------------------------------------------


def _insert_memory(
    storage: StorageEngine,
    *,
    heat: float,
    last_accessed: str,
    importance: float,
    tags,
    is_protected: bool = False,
    access_count_since_decay: int = 0,
) -> int:
    """CREATE memory:{id} inline — the embedded SDK rejects int params in type::record."""
    mid = storage._next_id("memory")
    storage._q(
        f"CREATE memory:{mid} SET "
        "content = $content, directory_context = $dir, heat = $heat, "
        "last_accessed = $la, created_at = $la, is_protected = $prot, tags = $tags, "
        "importance = $imp, emotional_valence = 0.0, confidence = 1.0, "
        "access_count = 0, access_count_since_decay = $acsd",
        {
            "content": f"fixture memory {mid}",
            "dir": _DIR,
            "heat": heat,
            "la": last_accessed,
            "prot": is_protected,
            "tags": tags,
            "imp": importance,
            "acsd": access_count_since_decay,
        },
    )
    return mid


def _insert_entity(storage: StorageEngine, *, heat: float, last_accessed: str) -> int:
    eid = storage._next_id("entity")
    storage._q(
        f"CREATE entity:{eid} SET "
        "name = $name, entity_type = 'test', heat = $heat, last_accessed = $la, "
        "created_at = $la, archived = false",
        {"name": f"ent{eid}", "heat": heat, "la": last_accessed},
    )
    return eid


def _seed(storage: StorageEngine) -> dict[str, int]:
    """Seed every row class the decay branch structure distinguishes."""
    ids = {
        # importance > 0.7 → IMPORTANCE_DECAY_FACTOR (the slow branch)
        "important": _insert_memory(
            storage, heat=0.9, last_accessed=_THREE_DAYS_AGO, importance=0.95, tags=[]
        ),
        # importance <= 0.7 → DECAY_FACTOR (the fast branch)
        "ordinary": _insert_memory(
            storage, heat=0.9, last_accessed=_THREE_DAYS_AGO, importance=0.30, tags=[]
        ),
        # protected → excluded from decay entirely (SQL WHERE *and* Python continue)
        "protected": _insert_memory(
            storage,
            heat=0.5,
            last_accessed=_TWO_YEARS_AGO,
            importance=0.95,
            tags=["_anchor"],
            is_protected=True,
        ),
        # two years unaccessed at the fast factor → below COLD_THRESHOLD → 0.0
        "cold": _insert_memory(
            storage, heat=0.30, last_accessed=_TWO_YEARS_AGO, importance=0.10, tags=[]
        ),
        # _action_stream uses its own cold threshold; tags as a real array
        "action_array": _insert_memory(
            storage,
            heat=0.30,
            last_accessed=_TWO_YEARS_AGO,
            importance=0.10,
            tags=["_action_stream"],
        ),
        # ...and the JSON-STRING tags shape the live corpus also carries
        "action_str": _insert_memory(
            storage,
            heat=0.30,
            last_accessed=_TWO_YEARS_AGO,
            importance=0.10,
            tags=json.dumps(["_action_stream"]),
        ),
        # zero elapsed hours + a pending recall counter → the "reset the counter"
        # branch (heat may or may not move; the counter must land at 0)
        "fresh_counted": _insert_memory(
            storage,
            heat=0.60,
            last_accessed=_NOW.isoformat(),
            importance=0.50,
            tags=[],
            access_count_since_decay=4,
        ),
        # entity that changes heat WITHOUT going cold
        "ent_warm": _insert_entity(storage, heat=0.80, last_accessed=_THREE_DAYS_AGO),
        # entity that crosses COLD_THRESHOLD → archived = true (2nd SQL template)
        "ent_cold": _insert_entity(storage, heat=0.30, last_accessed=_TWO_YEARS_AGO),
    }
    return ids


def _snapshot(storage: StorageEngine) -> dict:
    mem = storage._q(
        "SELECT meta::id(id) AS id, heat, last_decay_at, access_count_since_decay "
        "FROM memory ORDER BY id"
    )
    ent = storage._q(
        "SELECT meta::id(id) AS id, heat, last_decay_at, archived FROM entity ORDER BY id"
    )
    return {"memory": mem, "entity": ent}


class _Runner(_HeatDecayMixin):
    """Minimal host for the decay mixin — no scheduler, no embedding model."""

    def __init__(self, storage, settings):
        self._storage = storage
        self._settings = settings
        self._thermo = MemoryThermodynamics(storage, None, settings)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Pin ``datetime.now(UTC)`` inside heat_decay so both arms decay by the same span."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return _NOW

    monkeypatch.setattr(hd, "datetime", _FrozenDatetime)
    return _NOW


def _run_decay(tmp_path, monkeypatch, *, compaction: bool, name: str) -> tuple[dict, dict]:
    monkeypatch.delenv("YADGAR_DB_URL", raising=False)
    monkeypatch.setenv("YADGAR_BATCHED_DECAY_WRITES_ENABLED", "1" if compaction else "0")
    get_settings.cache_clear()
    settings = get_settings()

    storage = StorageEngine(str(tmp_path / f"{name}.db"))
    try:
        _seed(storage)
        runner = _Runner(storage, settings)
        stats = {"memories_updated": 0, "memories_archived": 0}
        runner._apply_decay(stats)
        return _snapshot(storage), stats
    finally:
        storage.close()
        get_settings.cache_clear()


class TestCompactionIsIndistinguishableAtTheDbLevel:
    def test_rows_identical_with_and_without_compaction(self, tmp_path, monkeypatch, frozen_now):
        batched, batched_stats = _run_decay(tmp_path, monkeypatch, compaction=True, name="batched")
        per_row, per_row_stats = _run_decay(tmp_path, monkeypatch, compaction=False, name="per_row")

        assert batched["memory"] == per_row["memory"], (
            "batched decay wrote different memory rows than the per-row path"
        )
        assert batched["entity"] == per_row["entity"], (
            "batched decay wrote different entity rows than the per-row path"
        )
        assert batched_stats == per_row_stats

    def test_fixture_actually_exercised_every_branch(self, tmp_path, monkeypatch, frozen_now):
        """A differential over an inert fixture proves nothing — assert it moved."""
        snap, stats = _run_decay(tmp_path, monkeypatch, compaction=True, name="branches")
        by_id = {r["id"]: r for r in snap["memory"]}
        ents = {r["id"]: r for r in snap["entity"]}

        assert stats["memories_updated"] > 0, "no memory heat changed — fixture is inert"
        assert stats["memories_archived"] > 0, "no memory crossed the cold threshold"
        assert any(r["heat"] == 0.0 for r in snap["memory"]), "no row landed at heat 0.0"
        assert any(r["archived"] for r in ents.values()), "no entity archived"
        assert any(not r["archived"] and r["last_decay_at"] for r in ents.values()), (
            "no entity decayed without archiving — the 2nd entity template is untested"
        )
        # importance > 0.7 must decay strictly slower than importance <= 0.7
        heats = sorted((r["heat"] for r in by_id.values() if r["heat"] > 0.5), reverse=True)
        assert len(heats) >= 2 and heats[0] > heats[1], (
            "the importance>0.7 slow branch did not outrank the fast branch"
        )

    def test_protected_rows_are_never_touched(self, tmp_path, monkeypatch, frozen_now):
        """Anchors must survive decay untouched — heat AND watermark.

        HOW MUCH THIS PROVES, stated honestly. Protection is enforced TWICE
        upstream of the compactor: the SQL ``WHERE (is_protected = false OR
        is_protected = NONE)`` in ``get_all_memories_for_decay_scalar`` and the
        ``if mem.get("is_protected"): continue`` in ``_decay_memories``. A
        protected row therefore never becomes an intent and never reaches
        ``_compact_heat_intents`` at all, so this test does NOT discriminate
        between a correct and an anchor-eating compactor.

        Measured by mutation 2026-08-21: removing EITHER guard alone leaves this
        test green; removing BOTH turns it red (the anchor decayed 0.5 → 0.102).
        It is a defence-in-depth forward regression guard on the PAIR — which is
        the thing worth guarding, since a set-based rewrite that lost both would
        silently decay every anchor in the corpus and no other test would say so.
        """
        for compaction in (True, False):
            monkeypatch.delenv("YADGAR_DB_URL", raising=False)
            monkeypatch.setenv("YADGAR_BATCHED_DECAY_WRITES_ENABLED", "1" if compaction else "0")
            get_settings.cache_clear()
            settings = get_settings()
            storage = StorageEngine(str(tmp_path / f"prot_{compaction}.db"))
            try:
                ids = _seed(storage)
                runner = _Runner(storage, settings)
                runner._apply_decay({"memories_updated": 0, "memories_archived": 0})
                rows = storage._q(
                    f"SELECT heat, last_decay_at, is_protected FROM memory:{ids['protected']}"
                )
                assert rows and rows[0]["heat"] == 0.5, (
                    f"protected anchor decayed (compaction={compaction}): {rows}"
                )
                assert not rows[0].get("last_decay_at"), (
                    "protected anchor got a decay watermark — it was written to"
                )
            finally:
                storage.close()
                get_settings.cache_clear()

    def test_missing_row_is_not_resurrected(self, tmp_path, monkeypatch):
        """FOR+UPDATE must never create a row deleted between fetch and write.

        This is why the compacted form is UPDATE-in-a-loop and not an upsert.
        """
        monkeypatch.delenv("YADGAR_DB_URL", raising=False)
        get_settings.cache_clear()
        storage = StorageEngine(str(tmp_path / "ghost.db"))
        try:
            _insert_memory(
                storage, heat=0.5, last_accessed=_THREE_DAYS_AGO, importance=0.5, tags=[]
            )
            before = storage._q("SELECT count() FROM memory GROUP ALL")
            ghost = _compact_heat_intents(
                [(_MEM_DECAY_SQL, {"id": 999_999, "heat": 0.1, "now": _NOW.isoformat()})]
            )
            storage.batch_writes(ghost)
            after = storage._q("SELECT count() FROM memory GROUP ALL")
            assert before == after, f"compacted write resurrected a row: {before} → {after}"
        finally:
            storage.close()
            get_settings.cache_clear()


class TestStatementCountCollapses:
    """The point of the car: bounded statement count instead of O(rows)."""

    def test_statement_count_is_bounded(self, tmp_path, monkeypatch, frozen_now):
        monkeypatch.delenv("YADGAR_DB_URL", raising=False)
        monkeypatch.setenv("YADGAR_BATCHED_DECAY_WRITES_ENABLED", "1")
        get_settings.cache_clear()
        settings = get_settings()
        storage = StorageEngine(str(tmp_path / "count.db"))
        try:
            for _ in range(60):
                _insert_memory(
                    storage, heat=0.9, last_accessed=_THREE_DAYS_AGO, importance=0.5, tags=[]
                )
            runner = _Runner(storage, settings)
            captured: list[list] = []
            real = storage.batch_writes
            storage.batch_writes = lambda stmts: (captured.append(list(stmts)), real(stmts))[1]  # type: ignore[method-assign]
            runner._apply_decay({"memories_updated": 0, "memories_archived": 0})
        finally:
            storage.close()
            get_settings.cache_clear()

        assert captured, "batch_writes was never called"
        assert len(captured[0]) == 1, (
            f"60 changed rows must flush as 1 statement, got {len(captured[0])}"
        )
