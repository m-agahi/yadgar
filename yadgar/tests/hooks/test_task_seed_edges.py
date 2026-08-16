"""Car E — the harness seeder writes real ``blockedBy`` / ``blocks`` arrays.

Car C shipped the mechanical seeder writing both arrays as ``[]``
unconditionally, because nothing could read the ledger's ``task_blocked_by``
edges. Car E makes them readable; this file pins what the seeder does with
them.

Two guards are load-bearing and neither is cosmetic:

* Harness ids are JSON STRINGS (``"id": "41"``, measured across 400 live
  files), so edge targets are written as strings. The edge arrays were empty in
  every one of those 400 files, so their element type is INFERRED from ``id``'s
  rather than measured — which is why anything unrecognisable degrades to
  ``[]`` instead of being passed through. This store's doctrine is that a wrong
  write is worse than no write.
* An edge target that is not itself seeded is dropped. Open tasks can depend on
  CLOSED ones, and a closed task is never written to the harness store, so the
  ledger's edge set is not a subset of the seeded set by construction.
"""

from __future__ import annotations

from yadgar.core.hooks.task_seed import _normalise_rows


def _rows(*specs):
    return [{"id": i, "title": f"task {i}", "status": "pending", **extra} for i, extra in specs]


class TestEdgesReachTheRecord:
    def test_blocked_by_is_written_as_string_ids(self) -> None:
        out, _ = _normalise_rows(_rows((1, {"blocked_by": [2]}), (2, {})))

        assert out[0]["blockedBy"] == ["2"]

    def test_blocks_is_written_as_string_ids(self) -> None:
        out, _ = _normalise_rows(_rows((1, {"blocks": [2]}), (2, {})))

        assert out[0]["blocks"] == ["2"]

    def test_int_and_string_inputs_both_normalise(self) -> None:
        """The ledger returns ints; a JSON round-trip elsewhere could give strings."""
        out, _ = _normalise_rows(_rows((1, {"blocked_by": [2, "3"]}), (2, {}), (3, {})))

        assert out[0]["blockedBy"] == ["2", "3"]

    def test_a_row_with_no_edges_still_gets_both_arrays(self) -> None:
        """The harness record has both keys in every one of the 400 live files."""
        out, _ = _normalise_rows(_rows((1, {})))

        assert out[0]["blocks"] == []
        assert out[0]["blockedBy"] == []


class TestUnwritableEdgesDegradeToEmpty:
    def test_a_non_list_is_dropped(self) -> None:
        out, _ = _normalise_rows(_rows((1, {"blocked_by": "2"})))

        assert out[0]["blockedBy"] == []

    def test_an_unparseable_element_is_dropped_not_guessed(self) -> None:
        out, _ = _normalise_rows(_rows((1, {"blocked_by": [None, "abc"]}), (2, {})))

        assert out[0]["blockedBy"] == []

    def test_a_non_positive_id_is_dropped(self) -> None:
        out, _ = _normalise_rows(_rows((1, {"blocked_by": [0, -3]}), (2, {})))

        assert out[0]["blockedBy"] == []


class TestDanglingTargetsArePruned:
    def test_a_target_outside_the_seeded_set_is_dropped(self) -> None:
        """Open tasks can be blocked by CLOSED ones, which are never seeded."""
        out, _ = _normalise_rows(_rows((1, {"blocked_by": [2, 99]}), (2, {})))

        assert out[0]["blockedBy"] == ["2"]

    def test_a_target_skipped_by_a_row_guard_is_dropped_too(self) -> None:
        """Row 2 is closed, so it is skipped — the edge to it must go with it."""
        rows = [
            {"id": 1, "title": "a", "status": "pending", "blocked_by": [2]},
            {"id": 2, "title": "b", "status": "completed"},
        ]

        out, skipped = _normalise_rows(rows)

        assert skipped == 1
        assert out[0]["blockedBy"] == []

    def test_a_self_edge_is_dropped(self) -> None:
        out, _ = _normalise_rows(_rows((1, {"blocks": [1]})))

        assert out[0]["blocks"] == []
