"""Car B2 — ledger task 195: render the supersede edges the ledger now returns.

The backend read ops attach ``supersedes`` / ``superseded_by`` to every ``adr``
row as lists of ids (``yadgar/tests/backend/test_adr_car_b2_supersedes_attach``).
Core's two row→shape mappers still had to be told what to do with them: both
read ``row.get("supersedes") or "none"``, which turns a list into… the list, and
an empty list into ``"none"``.  The 7-key consumer shape is STRINGS.

FORMATS, and why they differ.  Each half matches its own existing producer
rather than a newly-invented house style:

  * ``supersedes`` → ``"ADR-0023, ADR-0024"``.  That is ``adr_add``'s own
    documented INPUT format and it round-trips through ``_parse_supersedes``
    (``re.findall(r"ADR-(\\d{4})")``), so a value read out of ``adr_list`` can
    be fed straight back into ``adr_add``.
  * ``superseded_by`` → ``"0025"`` — bare, zero-padded, comma-joined with no
    space.  That is what ``_flip_superseded_target`` (the only code that has
    ever written this field) emits, including its ``f"{prev},{nnnn}"`` join.

Placed in ``adr_render`` rather than ``adr.py``: ``adr.py`` sits within ~10
lines of I13's HARD 1000-line file cap and is NOT in the complexity allowlist,
so it has no headroom a formatter pair should be spending.  ``adr_render`` is
the module the ADR body/tag/supersede rendering already lives in, and it is at
roughly a quarter of the cap.
"""

from __future__ import annotations

import pytest

from yadgar.core.server.tools.adr import (
    _row_to_adr_list_entry,
    _row_to_response_metadata,
)
from yadgar.core.server.tools.adr_render import _fmt_superseded_by, _fmt_supersedes


class TestSupersedesFormatter:
    def test_one_target_renders_the_adr_add_input_format(self) -> None:
        assert _fmt_supersedes([23]) == "ADR-0023"

    def test_many_targets_are_comma_joined(self) -> None:
        assert _fmt_supersedes([23, 24]) == "ADR-0023, ADR-0024"

    def test_empty_is_the_none_placeholder(self) -> None:
        assert _fmt_supersedes([]) == "none"
        assert _fmt_supersedes(None) == "none"

    def test_the_result_round_trips_through_the_parser(self) -> None:
        """``adr_add`` accepts exactly this string; a value read out of
        ``adr_list`` must be feedable straight back in."""
        from yadgar.core.server.tools.adr_render import _parse_supersedes

        assert _parse_supersedes(_fmt_supersedes([23, 24])) == ["ADR-0023", "ADR-0024"]

    def test_a_string_already_on_the_row_is_left_alone(self) -> None:
        """Defensive: a pre-existing string value must not be re-formatted into
        ``ADR-A, ADR-D, ADR-R…`` by iterating its characters."""
        assert _fmt_supersedes("ADR-0023") == "ADR-0023"


class TestSupersededByFormatter:
    def test_one_superseder_renders_bare_and_zero_padded(self) -> None:
        assert _fmt_superseded_by([25]) == "0025"

    def test_many_are_comma_joined_without_a_space(self) -> None:
        """``_flip_superseded_target`` builds ``f"{prev},{nnnn}"`` — no space."""
        assert _fmt_superseded_by([25, 252]) == "0025,0252"

    def test_empty_is_the_dash_placeholder(self) -> None:
        assert _fmt_superseded_by([]) == "-"
        assert _fmt_superseded_by(None) == "-"

    def test_a_string_already_on_the_row_is_left_alone(self) -> None:
        assert _fmt_superseded_by("0025") == "0025"


_SUPERSEDER_ROW = {
    "id": 25,
    "status": "accepted",
    "decided_on": "2026-08-19",
    "title": "The superseder",
    "body_slug": "m-agahi_yadgar_adr-0025",
    "supersedes": [23, 24],
    "superseded_by": [],
}

_TARGET_ROW = {
    "id": 23,
    "status": "superseded",
    "decided_on": "2026-07-01",
    "title": "The superseded",
    "body_slug": "m-agahi_yadgar_adr-0023",
    "supersedes": [],
    "superseded_by": [25],
}


class TestAdrListEntryCarriesTheEdges:
    """The live symptom: 22/22 supersede-bearing ADRs render ``"none"`` / ``"-"``."""

    def test_superseder_entry_names_its_targets(self) -> None:
        assert _row_to_adr_list_entry(_SUPERSEDER_ROW)["supersedes"] == "ADR-0023, ADR-0024"

    def test_superseder_entry_has_no_reverse_pointer(self) -> None:
        assert _row_to_adr_list_entry(_SUPERSEDER_ROW)["superseded_by"] == "-"

    def test_target_entry_names_its_superseder(self) -> None:
        assert _row_to_adr_list_entry(_TARGET_ROW)["superseded_by"] == "0025"

    def test_target_entry_supersedes_nothing(self) -> None:
        assert _row_to_adr_list_entry(_TARGET_ROW)["supersedes"] == "none"

    def test_the_seven_key_shape_is_unchanged(self) -> None:
        """The load-bearing contract (Car F §7 acceptance gate) — this car
        changes the VALUES, never the key set."""
        assert set(_row_to_adr_list_entry(_SUPERSEDER_ROW)) == {
            "adr_id",
            "status",
            "date",
            "title",
            "supersedes",
            "superseded_by",
            "slug",
        }

    def test_a_row_without_the_attached_keys_still_renders(self) -> None:
        """A row from a backend too old to attach, or a degraded attach, must
        not raise — it renders the pre-fix placeholders."""
        entry = _row_to_adr_list_entry({"id": 26, "status": "accepted", "title": "bare"})
        assert entry["supersedes"] == "none"
        assert entry["superseded_by"] == "-"


class TestAdrGetMetadataCarriesTheEdges:
    def test_supersedes_is_formatted_not_a_raw_list(self) -> None:
        assert _row_to_response_metadata(_SUPERSEDER_ROW)["supersedes"] == "ADR-0023, ADR-0024"

    def test_superseded_by_is_now_emitted_at_all(self) -> None:
        """``adr_get`` never carried this key.  D5's merge is additive-only, so
        adding it is licensed — and without it the ``adr_get`` of a superseded
        ADR cannot say what replaced it."""
        assert _row_to_response_metadata(_TARGET_ROW)["superseded_by"] == "0025"

    def test_the_pre_existing_metadata_keys_survive(self) -> None:
        """D5 additive-only: pre-migration keys ⊆ post-migration keys."""
        keys = set(_row_to_response_metadata(_SUPERSEDER_ROW))
        assert {
            "date",
            "rationale",
            "alternatives",
            "revisit_trigger",
            "supersedes",
            "subsystem",
            "tier",
            "baseline_hash",
            "content_hash",
        } <= keys


@pytest.mark.parametrize("row", [_SUPERSEDER_ROW, _TARGET_ROW])
def test_both_mappers_agree_on_the_supersedes_rendering(row: dict) -> None:
    """``adr_list`` and ``adr_get`` describing the same ADR differently is the
    kind of drift that made this defect survive two corpora."""
    assert _row_to_adr_list_entry(row)["supersedes"] == _row_to_response_metadata(row)["supersedes"]
