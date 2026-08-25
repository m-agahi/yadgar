"""Car F / Task 296 — NULL-OUT FLOOR coverage for ``update_memory_fields``.

The structural fix (commit 1f026d25) added
``_enrichment_null_clauses(converted)`` to ``update_memory_fields`` so any
content write clears the six enrichment columns in the SAME UPDATE. Before
this car the helper had zero unit-test coverage and the integration path
was unverified — a regression in the conditional or the SET-clause splice
would have shipped silently.

Two angles:

* **Helper unit tests** — ``_enrichment_null_clauses`` is a pure function
  on a dict, so we exercise its gate directly without touching the DB.
  Three behaviours: empty when ``content`` absent, all six ``= NONE``
  clauses when ``content`` is a string, all six clauses when ``content``
  is explicitly ``None`` (defensive — a caller clearing content still
  passes the funnel).
* **Integration tests via the recorder** — mirror
  ``TestUpdateMemoryFieldsProjectIdIsNoneSafe`` in
  ``test_c11_project_id_writers.py``: spy on ``self._q`` to capture the
  real ``UPDATE ... SET ...`` statement and assert the six ``= NONE``
  literals ride along on a content change, stay out of a non-content
  update, and survive when a content change ships with siblings.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar._shared.storage.memory import (
    _ENRICHMENT_COLS,
    _enrichment_null_clauses,
)

#: The six enrichment columns the helper clears in one UPDATE. Locked in
#: here so a future rename that drops one column shows up as a clear
#: test diff rather than silent behaviour drift.
_ENRICHMENT_COL_SET = frozenset(_ENRICHMENT_COLS)


@pytest.fixture
def storage(tmp_path):
    return StorageEngine(str(tmp_path / "test.db"))


@pytest.fixture
def recorder(storage, monkeypatch):
    """Record every ``(sql, params)`` while still executing the real write."""
    calls: list[tuple[str, dict]] = []
    original = storage._q

    def _spy(surql, params=None):
        calls.append((surql, dict(params or {})))
        return original(surql, params)

    monkeypatch.setattr(storage, "_q", _spy)
    return calls


# --------------------------------------------------------------- helper unit


class TestEnrichmentNullClausesHelper:
    """``_enrichment_null_clauses`` — the gate, not the SET-clause splice."""

    def test_empty_dict_yields_no_clauses(self):
        assert _enrichment_null_clauses({}) == ()

    def test_unrelated_keys_yield_no_clauses(self):
        assert _enrichment_null_clauses({"tags": ["x"], "heat": 0.5}) == ()
        assert _enrichment_null_clauses({"is_protected": True}) == ()

    def test_content_string_yields_one_clause_per_enrichment_column(self):
        clauses = _enrichment_null_clauses({"content": "new text"})
        assert len(clauses) == len(_ENRICHMENT_COLS)
        assert all(c.endswith(" = NONE") for c in clauses), (
            f"every clause must be a literal `<col> = NONE`, got: {clauses!r}"
        )
        covered = {c.split(" = NONE", 1)[0] for c in clauses}
        assert covered == _ENRICHMENT_COL_SET, (
            f"clause columns must equal _ENRICHMENT_COLS exactly, "
            f"missing={_ENRICHMENT_COL_SET - covered}, extra={covered - _ENRICHMENT_COL_SET}"
        )

    def test_content_explicit_none_still_triggers_the_floor(self):
        """A caller passing ``content=None`` to clear the column still walks
        through ``update_memory_fields``; the stale-enrichment risk is
        identical to a string rewrite, so the floor MUST fire here too."""
        clauses = _enrichment_null_clauses({"content": None})
        assert len(clauses) == len(_ENRICHMENT_COLS)
        assert all(c.endswith(" = NONE") for c in clauses)

    def test_helper_is_pure_does_not_mutate_input(self):
        converted = {"content": "x", "tags": ["a"]}
        snapshot = dict(converted)
        _enrichment_null_clauses(converted)
        assert converted == snapshot, "helper mutated its input dict"


# --------------------------------------------------------------- integration


def _seed_memory(storage, mid: int) -> None:
    storage._q(
        f"CREATE memory:{mid} SET content = $c, heat = 1.0, is_stale = false, "
        f"directory_context = $d, tags = []",
        {"c": "enrichment null floor probe", "d": "/home/max/git/yadgar"},
    )


class TestUpdateMemoryFieldsEnrichmentNullFloor:
    """The structural half of task 296: the six ``= NONE`` clauses land in
    the same UPDATE that rewrites content, ride no other UPDATE, and never
    leak in when content is absent."""

    def test_content_change_carries_all_six_none_clauses(self, storage, recorder):
        _seed_memory(storage, 9101)
        storage.update_memory_fields(9101, content="rewritten body")
        sql, params = recorder[-1]
        for col in _ENRICHMENT_COLS:
            assert f"{col} = NONE" in sql, (
                f"expected `{col} = NONE` literal in UPDATE, got: {sql!r}"
            )
        # NONE is a literal — none of the six columns may be bound as a
        # parameter. Mirrors the project_id NONE-safety convention.
        for col in _ENRICHMENT_COLS:
            assert col not in params, (
                f"`{col}` was bound as a parameter, must ride as NONE literal; params={params!r}"
            )

    def test_non_content_update_carries_no_none_clauses(self, storage, recorder):
        _seed_memory(storage, 9102)
        storage.update_memory_fields(9102, tags=["enriched", "v2"])
        sql, params = recorder[-1]
        for col in _ENRICHMENT_COLS:
            assert f"{col} = NONE" not in sql, (
                f"a non-content update must not NULL enrichment columns; got: {sql!r}"
            )
        assert ["enriched", "v2"] in params.values()

    def test_content_change_with_sibling_field_still_nulls(self, storage, recorder):
        _seed_memory(storage, 9103)
        storage.update_memory_fields(9103, content="rewritten", tags=["k"])
        sql, params = recorder[-1]
        for col in _ENRICHMENT_COLS:
            assert f"{col} = NONE" in sql, (
                f"sibling fields must not suppress the floor; got: {sql!r}"
            )
        assert "rewritten" in params.values()
        assert ["k"] in params.values()
