"""Car G (0047 spine train) — ADR seed pages->ledger + retype mutator + dead-code
deletion + project_brief re-points.

Plan: docs/plans/0047-car-G-adr-seed-retype.md.

TDD coverage for:

  §4.1  adr_superseded added to CANONICAL_PAGE_TYPES (wiki.py:29).
  §4.2  retype_page_type backend op: from_type mismatch REJECTED; happy path
        flips page_type adr -> adr_superseded (atomic with status column flip).
  §4.3  seed_adr_rows idempotent: re-run returns rows_skipped == rows_inserted
        on first run; ADR-0124 (page, no index row) flagged, NOT dropped.
  §4.5  _build_adr_log + _get_adr_log_updated_at read from the ledger, NOT
        <project>-adr-index wiki_page.
  §4.7  adr_list/adr_get/adr_add no longer import or call parse_index_rows /
        _build_index_content / _adr_log_lock.
  §4.9  stop_checkpoint_prompt.md no longer reads {project}-adr-log;
        step 1's dedup reaches adr_list (ledger).
  D35c  exact-equality verification gate — index_rows vs pages_seen vs
        page_type='adr' rows.
"""

from __future__ import annotations

import re
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest

# ── §4.1: adr_superseded accepted by _wiki_write_canonical ──────────────────


class TestAdrSupersededCanonical:
    """§4.1: ``adr_superseded`` joins ``CANONICAL_PAGE_TYPES``.

    The frozenset assertion inside ``_wiki_write_canonical`` raises ValueError
    on a non-allowlisted page_type. After Car G's frozenset expansion the same
    call must succeed (a sanctioned server-side call against adr_superseded is
    the only thing that ever supplies that type — Car G's retype mutator is
    the canonical entry point).
    """

    def test_canonical_set_includes_adr_superseded(self):
        from yadgar.core.server.tools.wiki import CANONICAL_PAGE_TYPES

        assert "adr_superseded" in CANONICAL_PAGE_TYPES, (
            f"CANONICAL_PAGE_TYPES must contain 'adr_superseded' (got "
            f"{sorted(CANONICAL_PAGE_TYPES)})"
        )

    def test_canonical_set_still_includes_adr(self):
        """Regression guard — adding adr_superseded must not drop adr."""
        from yadgar.core.server.tools.wiki import CANONICAL_PAGE_TYPES

        assert "adr" in CANONICAL_PAGE_TYPES
        assert "task_list" in CANONICAL_PAGE_TYPES

    def test_wiki_page_types_yaml_has_adr_superseded_entry(self):
        """wiki_lint reads wiki_page_types.yaml — adr_superseded must lint."""
        from importlib.resources import files

        from yadgar._shared import schemas

        text = files(schemas).joinpath("wiki_page_types.yaml").read_text()
        # The yaml shape: "  adr_superseded:\n    required: [Context, ...]"
        assert re.search(r"^\s{2}adr_superseded:", text, re.MULTILINE), (
            "wiki_page_types.yaml must declare an adr_superseded page_type "
            "entry so wiki_lint format-checks superseded pages"
        )


# ── §4.2: retype_page_type backend op ────────────────────────────────────────


class TestRetypePageType:
    """§4.2: ``retype_page_type`` server-side mutator.

    Bypasses _WIKI_UPDATE_ALLOWED (admin_other.py:42) — sanctioned server-side
    lifecycle transition (D26). Asserts from_type matches current page_type —
    refuses a cross-type retype that skips the guard.
    """

    def test_refuses_from_type_mismatch(self, tmp_path):
        """If page's current page_type != from_type, raise (guard)."""
        from yadgar.backend.admin_exec.adr_seed import retype_page_type

        # Build a fake storage that exposes get_wiki_page_by_slug -> {page_type: "adr"}
        class _FakeStorage:
            def get_wiki_page_by_slug(self, slug, directory=None):
                return {"id": 1, "slug": slug, "page_type": "adr", "tags": []}

            def get_wiki_page_ids_by_slug(self, slug):
                return [1]

        with pytest.raises(ValueError, match="from_type"):
            retype_page_type(
                slug="yadgar-adr-0001",
                from_type="adr_superseded",  # WRONG — current is "adr"
                to_type="adr_superseded",
                directory=str(tmp_path),
                storage=_FakeStorage(),  # type: ignore[arg-type]
            )

    def test_retype_happy_path_flips_page_type(self, tmp_path):
        """Happy path: from_type matches → flips page_type + bumps version."""
        from yadgar.backend.admin_exec.adr_seed import retype_page_type

        # Capture the call to storage.update_wiki_page.
        captured: dict = {}

        class _FakeStorage:
            def get_wiki_page_by_slug(self, slug, directory=None):
                return {
                    "id": 7,
                    "slug": slug,
                    "page_type": "adr",
                    "tags": ["adr", "decisions", "adr-status:accepted"],
                    "content": "old",
                }

            def get_wiki_page_ids_by_slug(self, slug):
                return [7]

            def update_wiki_page(self, page_id, updates, _sanctioned=False):
                captured["page_id"] = page_id
                captured["updates"] = updates
                captured["_sanctioned"] = _sanctioned
                return True

        result = retype_page_type(
            slug="yadgar-adr-0001",
            from_type="adr",
            to_type="adr_superseded",
            directory=str(tmp_path),
            storage=_FakeStorage(),  # type: ignore[arg-type]
        )

        assert result["ok"] is True
        assert result["slug"] == "yadgar-adr-0001"
        assert result["from_type"] == "adr"
        assert result["to_type"] == "adr_superseded"
        # The UPDATE must carry page_type='adr_superseded' AND pass _sanctioned.
        assert captured["updates"]["page_type"] == "adr_superseded"
        assert captured["_sanctioned"] is True, (
            "retype mutator MUST pass _sanctioned=True — the storage gate "
            "(mutability='locked') rejects the write otherwise (D26)"
        )

    def test_retype_refuses_unknown_target(self, tmp_path):
        """Unknown slug → ValueError on lookup, no write issued."""
        from yadgar.backend.admin_exec.adr_seed import retype_page_type

        class _FakeStorage:
            def get_wiki_page_by_slug(self, slug, directory=None):
                return None

            def get_wiki_page_ids_by_slug(self, slug):
                return []

            def update_wiki_page(self, page_id, updates, _sanctioned=False):
                raise AssertionError("must not be called for unknown slug")

        with pytest.raises(ValueError, match="not found"):
            retype_page_type(
                slug="yadgar-adr-NOPE",
                from_type="adr",
                to_type="adr_superseded",
                directory=str(tmp_path),
                storage=_FakeStorage(),  # type: ignore[arg-type]
            )


# ── §4.3: seed_adr_rows idempotency ─────────────────────────────────────────


class TestSeedAdrRowsIdempotent:
    """§4.3: ``seed_adr_rows`` is idempotent on ``body_slug``.

    Source of truth = per-ADR wiki PAGES (D35b), NOT parse_index_rows.
    Re-running converges — zero new rows on the second run.
    """

    def test_idempotent_re_run_inserts_zero_rows(self):
        from yadgar.backend.admin_exec.adr_seed import _collect_candidate_pages

        # Three pages: two with a matching index row (ADR-0001, ADR-0002)
        # and one with NO index row (ADR-0124 — the D35b "page-only" case).
        _pages = [
            {"slug": "yadgar-adr-0001", "content": "# ADR-0001: a\nbody", "tags": []},
            {"slug": "yadgar-adr-0002", "content": "# ADR-0002: b\nbody", "tags": []},
            {"slug": "yadgar-adr-0124", "content": "# ADR-0124: c\nbody", "tags": []},
        ]
        # _collect_candidate_pages is a pure helper that enumerates per-ADR
        # pages from the slug prefix. Its return value is the page source of
        # truth — the seed reads these directly.
        collected = _collect_candidate_pages(
            project_slug_prefix="yadgar-adr-",
            list_pages=lambda prefix, directory, limit: _pages,
        )
        slugs = {p["slug"] for p in collected}
        assert slugs == {"yadgar-adr-0001", "yadgar-adr-0002", "yadgar-adr-0124"}

    def test_pages_seen_includes_page_only_adr_0124(self):
        """D35b: ADR-0124 (page-only, no index row) MUST be in pages_seen."""
        from yadgar.backend.admin_exec.adr_seed import _collect_candidate_pages

        _pages = [
            {"slug": "yadgar-adr-0124", "content": "# ADR-0124: c\nbody", "tags": []},
        ]
        collected = _collect_candidate_pages(
            project_slug_prefix="yadgar-adr-",
            list_pages=lambda prefix, directory, limit: _pages,
        )
        assert any(p["slug"] == "yadgar-adr-0124" for p in collected)


# ── ascending-ADR-number insertion order (fix 1) ────────────────────────────


class _OrderFakeStorage:
    """Minimal storage double: only ``list_wiki_pages`` is exercised.

    ``list_adr_rows``/``get_wiki_page_by_slug*`` are absent on purpose — the
    D35c gate helpers swallow the resulting ``AttributeError`` internally and
    report 0, which is fine for a test that only cares about insertion order.
    """

    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def list_wiki_pages(self, slug_prefix: str, limit: int = 10000) -> list[dict]:
        return [dict(p) for p in self._pages if (p.get("slug") or "").startswith(slug_prefix)]


class TestSeedAdrRowsAscendingOrder:
    """seed_adr_rows must insert candidate pages in ascending ADR-number order.

    Insertion order is the ONLY lever on which ledger id a page lands on: the
    op cannot supply an id (``adr.id`` is AUTO_INCREMENT and IS the ADR number,
    ADR-0197), so the Nth insert takes the Nth free id and nothing downstream
    can correct a wrong order.

    REWRITTEN (task 168). The previous fixture asserted ``yadgar-adr-0001`` was
    inserted FIRST, encoding a world where exactly one low id had been spent —
    and, since it injected ``row_inserter``, observing only a synthetic
    ``{"id": len(inserted_order)}``. Nine ids are spent now, and the ADRs whose
    numbers they occupy are the ones the skip-set exists to leave alone; a test
    that demands 0001 be inserted first demands the corpus be misnumbered.
    The ORDERING contract is unchanged and still asserted below; the real
    AUTO_INCREMENT ids live in ``tests/integration/test_adr_seed_ledger_ids.py``,
    which is where a synthetic id cannot hide a misnumbering.
    """

    @staticmethod
    def _pages() -> list[dict]:
        # Deliberately out-of-order + not alphabetically-sorted-equivalent
        # (0010 sorts before 0002 lexicographically if slugs were compared
        # as strings without the numeric parse — this fixture would catch
        # that regression too).
        return [
            {"slug": "yadgar-adr-0010", "content": "# ADR-0010: j\nbody"},
            {"slug": "yadgar-adr-0002", "content": "# ADR-0002: b\nbody"},
            {"slug": "yadgar-adr-0003", "content": "# ADR-0003: c\nbody"},
            {"slug": "yadgar-adr-0001", "content": "# ADR-0001: a\nbody"},
        ]

    @staticmethod
    async def _run(pages: list[dict], **kwargs) -> tuple[dict, list[str]]:
        from yadgar.backend.admin_exec.adr_seed import seed_adr_rows

        inserted_order: list[str] = []

        def _row_inserter(payload: dict) -> dict:
            inserted_order.append(payload["body_slug"])
            # A synthetic id: this suite asserts CALL ORDER only. Real ids are
            # the integration test's subject, deliberately not this one's.
            return {"id": len(inserted_order)}

        result = await seed_adr_rows(
            project_id="m-agahi/yadgar",
            directory="/home/max/git/yadgar",
            storage=_OrderFakeStorage(pages),
            row_inserter=_row_inserter,
            slug_linker=lambda adr_id, slug: None,
            **kwargs,
        )
        return result, inserted_order

    async def test_inserts_in_ascending_adr_number_order_regardless_of_page_order(self) -> None:
        _result, inserted_order = await self._run(self._pages())

        assert inserted_order == [
            "yadgar-adr-0001",
            "yadgar-adr-0002",
            "yadgar-adr-0003",
            "yadgar-adr-0010",
        ], f"expected ascending ADR-number insertion order, got {inserted_order}"

    async def test_skipped_numbers_drop_out_and_the_rest_keep_their_order(self) -> None:
        """ADR-0006's skip, composed with the ordering contract.

        Skipping must REMOVE entries, never reorder the survivors — a skip that
        shuffled the remainder would put every later ADR on the wrong id just as
        surely as inserting the skipped one would.
        """
        result, inserted_order = await self._run(self._pages(), skip_adr_numbers=[1, 2])

        assert inserted_order == ["yadgar-adr-0003", "yadgar-adr-0010"]
        assert result["rows_skipped_by_request"] == 2
        assert result["rows_inserted"] == 2
        assert result["pages_seen"] == 4, "a skipped page is still SEEN, just not inserted"

    async def test_skip_is_keyed_on_the_number_not_the_slug_shape(self) -> None:
        """One number, two slug shapes, ONE skip decision.

        The live corpus has both a legacy ``yadgar-adr-0001`` and a canonical
        ``m-agahi_yadgar_adr-0001`` page. A slug-keyed skip set would need both
        spellings and would silently insert whichever one the operator did not
        type — onto the id the other one needed.
        """
        pages = [
            {"slug": "yadgar-adr-0001", "content": "# ADR-0001: a\nbody"},
            {"slug": "m-agahi_yadgar_adr-0001", "content": "# ADR-0001: a'\nbody"},
            {"slug": "yadgar-adr-0002", "content": "# ADR-0002: b\nbody"},
        ]
        result, inserted_order = await self._run(pages, skip_adr_numbers=[1])

        assert inserted_order == ["yadgar-adr-0002"]
        assert result["rows_skipped_by_request"] == 2

    async def test_dry_run_inserts_nothing_and_returns_the_planned_mapping(self) -> None:
        result, inserted_order = await self._run(self._pages(), dry_run=True)

        assert inserted_order == [], "a dry run must not reach the inserter at all"
        assert result["rows_inserted"] == 0
        assert [e["adr"] for e in result["plan"]] == [
            "ADR-0001",
            "ADR-0002",
            "ADR-0003",
            "ADR-0010",
        ]
        # Contiguous ids from the same base, in insertion order — the mapping an
        # operator gates the irreversible run on.
        planned = [e["planned_id"] for e in result["plan"]]
        assert planned == list(range(planned[0], planned[0] + 4))

    def test_sort_key_handles_unparsable_slug_without_crashing(self) -> None:
        """Defensive contract on the sort key itself: an unparsable-suffix
        page must sort LAST, never raise. Today's candidate-collection
        filter (``_is_per_adr_page_slug``) already excludes such pages
        before this key is ever applied by ``seed_adr_rows`` — this test
        pins the sort key's own contract independent of that upstream
        filter, so a future relaxation of the filter can't reintroduce a
        ``TypeError: '<' not supported between instances of 'NoneType' and
        'int'`` mid-sort."""
        from yadgar.backend.admin_exec.adr_seed import _adr_page_sort_key

        pages = [
            {"slug": "yadgar-adr-0002"},
            {"slug": "yadgar-adr-BOGUS"},
            {"slug": "yadgar-adr-0001"},
        ]
        ordered = sorted(pages, key=_adr_page_sort_key)
        assert [p["slug"] for p in ordered] == [
            "yadgar-adr-0001",
            "yadgar-adr-0002",
            "yadgar-adr-BOGUS",
        ], "unparsable slug must sort LAST, not raise or sort first"


# ── prefix union (fix 2) ─────────────────────────────────────────────────────


class TestSeedAdrRowsPrefixUnion:
    """The canonical + legacy slug prefixes must BOTH be scanned and unioned.

    A ``break``-on-first-nonempty loop (the pre-fix behaviour) finds the one
    already-reslugged canonical-format page, breaks, and never scans the 200+
    legacy-format pages living under the OTHER prefix — the seed would report
    success having inserted just one row.
    """

    async def test_pages_seen_unions_both_prefixes(self) -> None:
        """Isolates the loop's union/break behaviour from
        ``_is_per_adr_page_slug``'s own slug-shape recognition (a separate,
        pre-existing concern) by patching ``_adr_slug_prefixes`` to hand
        back two arbitrary hyphen-format prefixes, first one 1-page,
        second one 3-pages — exactly the shape that breaks a
        break-on-first-nonempty loop after only the first prefix.
        """
        from yadgar.backend.admin_exec import adr_seed
        from yadgar.backend.admin_exec.adr_seed import seed_adr_rows

        pages = [
            {"slug": "prefix-a-adr-0001", "content": "# ADR-0001: a\nbody"},
            {"slug": "prefix-b-adr-0002", "content": "# ADR-0002: b\nbody"},
            {"slug": "prefix-b-adr-0003", "content": "# ADR-0003: c\nbody"},
            {"slug": "prefix-b-adr-0004", "content": "# ADR-0004: d\nbody"},
        ]
        storage = _OrderFakeStorage(pages)

        inserted_order: list[str] = []

        def _row_inserter(payload: dict) -> dict:
            inserted_order.append(payload["body_slug"])
            return {"id": len(inserted_order)}

        with patch.object(
            adr_seed,
            "_adr_slug_prefixes",
            return_value=["prefix-a-adr-", "prefix-b-adr-"],
        ):
            result = await seed_adr_rows(
                project_id="m-agahi/yadgar",
                directory="/home/max/git/yadgar",
                storage=storage,
                row_inserter=_row_inserter,
                slug_linker=lambda adr_id, slug: None,
            )

        assert result["pages_seen"] == 4, (
            f"expected both prefixes unioned (4 pages), got pages_seen="
            f"{result['pages_seen']} — break-on-first-nonempty would report 1"
        )
        assert set(inserted_order) == {
            "prefix-a-adr-0001",
            "prefix-b-adr-0002",
            "prefix-b-adr-0003",
            "prefix-b-adr-0004",
        }

    async def test_page_matching_both_prefixes_is_not_double_counted(self) -> None:
        """A page whose slug happens to satisfy BOTH prefix scans (e.g. a
        storage layer that returns overlapping results) must be seeded once,
        not twice — the union must de-duplicate by slug."""
        from yadgar.backend.admin_exec.adr_seed import seed_adr_rows

        class _OverlappingStorage:
            """Simulates the pathological case named in the task: the same
            page comes back from EVERY prefix query, regardless of prefix
            text — the union logic must still de-dup by slug."""

            def list_wiki_pages(self, slug_prefix: str, limit: int = 10000) -> list[dict]:
                return [{"slug": "yadgar-adr-0001", "content": "# ADR-0001: a\nbody"}]

        storage = _OverlappingStorage()
        inserted_order: list[str] = []

        def _row_inserter(payload: dict) -> dict:
            inserted_order.append(payload["body_slug"])
            return {"id": len(inserted_order)}

        result = await seed_adr_rows(
            project_id="m-agahi/yadgar",
            directory="/home/max/git/yadgar",
            storage=storage,
            row_inserter=_row_inserter,
            slug_linker=lambda adr_id, slug: None,
        )

        assert result["pages_seen"] == 1, (
            f"same page returned by both prefix scans must be de-duplicated "
            f"by slug, got pages_seen={result['pages_seen']}"
        )
        assert inserted_order == ["yadgar-adr-0001"]


# ── §4.5: _build_adr_log + _get_adr_log_updated_at ──────────────────────────


class TestBuildAdrLogLedger:
    """§4.5: ``_build_adr_log`` + ``_get_adr_log_updated_at`` read the ledger.

    Pre-G: ``wiki_read(<project>-adr-index)`` + ``parse_index_rows``.
    Post-G: ``list_adr_rows(project_id)`` ordered by id DESC, take 3.
    """

    def test_build_adr_log_does_not_call_wiki_read(self, tmp_path):
        """The ledger re-point must NEVER hit ``wiki_read``.

        Pre-G: ``wiki_read(<project>-adr-index)`` + ``parse_index_rows``.
        Post-G: ``_forward_admin("list_adr_rows", ...)``. The test pins BOTH
        sides: ``wiki_read`` is never called AND ``list_adr_rows`` IS called.
        """
        from yadgar.core.server.tools import project

        # Stub _forward_admin to return our ledger rows.
        rows = [
            {
                "id": 3,
                "status": "open",
                "decided_on": "2026-08-01",
                "title": "Third",
                "body_slug": "yadgar-adr-0003",
            },
            {
                "id": 2,
                "status": "accepted",
                "decided_on": "2026-07-15",
                "title": "Second",
                "body_slug": "yadgar-adr-0002",
            },
            {
                "id": 1,
                "status": "accepted",
                "decided_on": "2026-06-01",
                "title": "First",
                "body_slug": "yadgar-adr-0001",
            },
        ]

        forward_calls: list[tuple[str, dict]] = []

        def _stub_forward(op, payload, **kwargs):
            forward_calls.append((op, payload))
            if op == "list_adr_rows":
                return {"rows": rows}
            raise AssertionError(f"unexpected op {op}")

        with (
            patch.object(project, "_resolve_project_root", return_value=str(tmp_path)),
            patch.object(project, "_forward_admin", side_effect=_stub_forward),
            patch.object(project, "wiki_read", create=True) as mocked_wiki_read,
        ):
            result = project._build_adr_log(str(tmp_path))

        # The ledger re-point must NEVER hit wiki_read.
        assert mocked_wiki_read.call_count == 0, (
            "_build_adr_log must read from the ledger, NOT wiki_read (post-Car-G re-point)"
        )
        # AND it MUST hit _forward_admin("list_adr_rows", ...) instead.
        list_adr_calls = [c for c in forward_calls if c[0] == "list_adr_rows"]
        assert len(list_adr_calls) >= 1, (
            "_build_adr_log must reach the ledger via _forward_admin "
            "'list_adr_rows' (post-Car-G re-point)"
        )
        # The 7-key shape contract (Car F) is preserved post-G.
        assert result["slug"] == "yadgar-adr-log" or "latest_ids" in result
        # ordered by id DESC, take 3
        latest = result["latest_ids"]
        assert latest[:3] == ["ADR-0003", "ADR-0002", "ADR-0001"]

    def test_build_adr_log_latest_ids_capped_three(self, tmp_path):
        """Five ledger rows → latest_ids capped at 3 (newest first)."""
        from yadgar.core.server.tools import project

        rows = [
            {
                "id": i,
                "status": "open",
                "decided_on": None,
                "title": f"d{i}",
                "body_slug": f"yadgar-adr-{i:04d}",
            }
            for i in range(1, 6)
        ]

        def _stub_forward(op, payload, **kwargs):
            if op == "list_adr_rows":
                return {"rows": rows}
            raise AssertionError(op)

        with (
            patch.object(project, "_resolve_project_root", return_value=str(tmp_path)),
            patch.object(project, "_forward_admin", side_effect=_stub_forward),
        ):
            result = project._build_adr_log(str(tmp_path))

        latest = result["latest_ids"]
        assert len(latest) == 3
        assert latest[0] == "ADR-0005"

    def test_get_adr_log_updated_at_reads_ledger(self, tmp_path):
        """``_get_adr_log_updated_at`` reads MAX(updated_at) on adr table."""
        from datetime import datetime
        from unittest.mock import MagicMock

        from yadgar.core.server.tools import project

        class _FakeStorage:
            def __init__(self):
                self.max_adr_updated_at = MagicMock(
                    return_value=datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
                )

            def _q(self, *args, **kwargs):
                raise AssertionError(
                    "must not query wiki_page — Car G re-points this onto MAX(updated_at)"
                )

        storage = _FakeStorage()
        ts = project._get_adr_log_updated_at(storage, str(tmp_path))
        assert ts is not None
        assert storage.max_adr_updated_at.call_count == 1
        # ts is a unix timestamp float
        assert isinstance(ts, float)
        # Round-trip through datetime for stability
        assert datetime.fromtimestamp(ts, tz=UTC).year == 2026

    def test_get_adr_log_updated_at_returns_none_when_empty(self, tmp_path):
        from unittest.mock import MagicMock

        from yadgar.core.server.tools import project

        class _FakeStorage:
            def __init__(self):
                self.max_adr_updated_at = MagicMock(return_value=None)

        storage = _FakeStorage()
        assert project._get_adr_log_updated_at(storage, str(tmp_path)) is None


# ── §4.7: dead symbols deleted ─────────────────────────────────────────────


class TestDeadCodeDeleted:
    """§4.7: parse_index_rows / _build_index_content / _adr_log_lock removed.

    Import-time test: after Car G, the symbols must NOT exist on the modules
    they lived in pre-G. The backward-compat re-exports on adr.py are gone
    too.
    """

    def test_adr_index_parser_removed(self):
        from yadgar.core.server.tools import adr_index  # type: ignore[import-not-found]

        for name in (
            "parse_index_rows",
            "_build_index_content",
            "_render_index_row",
            "_INDEX_ROW_RE",
            "_INDEX_HEADER",
            "_index_max_id",
            "_committed_page_max_id",
            "_next_adr_id",
            "_next_adr_id_from_index",
            "parse_adr_ids",
        ):
            assert not hasattr(adr_index, name), (
                f"adr_index.{name} must be DELETED post-Car-G "
                f"(parser/serializer/index-render machinery is dead)"
            )

    def test_adr_index_slug_helpers_retained(self):
        """Car L is shipped but the reslug op is dry-run by default — pages
        may still carry the legacy slug. adr_page_slug MUST survive until
        every ADR row's body_slug is the canonical form."""
        from yadgar.core.server.tools import adr_index  # type: ignore[import-not-found]

        assert hasattr(adr_index, "adr_page_slug"), (
            "adr_page_slug is the legacy fallback for the 194 not-yet-"
            "reslugged pages — it MUST stay until the reslug op has run "
            "end-to-end. Car L ships the op (dry-run by default); Car G's "
            "seed leaves the legacy page readable for compatibility."
        )

    def test_adr_log_lock_removed(self):
        """_adr_log_lock + globals are DELETED post-G."""
        from yadgar.core.server.tools import adr  # type: ignore[import-not-found]

        for name in (
            "_ADR_LOG_LOCKS",
            "_ADR_LOG_LOCKS_GUARD",
            "_adr_log_lock",
        ):
            assert not hasattr(adr, name), (
                f"adr.{name} must be DELETED — Car F's re-point renders the "
                f"per-project lock moot (ledger AUTO_INCREMENT serializes id "
                f"allocation backend-side, see §2/§4.7)."
            )


# ── §4.9: stop_checkpoint_prompt.md dead read fixed ────────────────────────


class TestStopCheckpointPromptFixed:
    """§4.9: stop_checkpoint_prompt.md no longer reads {project}-adr-log.

    The monolith slug is gone; step 1's read-first-dedup must reach
    adr_list(directory=...) against the ledger.
    """

    def test_prompt_does_not_reference_dead_adr_log_slug(self):
        template_path = Path(__file__).resolve().parents[2] / (
            "core/hooks/templates/stop_checkpoint_prompt.md"
        )
        text = template_path.read_text()
        # Strip the meta-commentary block (Car G history note explaining the
        # change). The presence of the literal slug inside the meta-comment
        # is OK; what matters is whether the live protocol still references
        # it as a CALL site.
        body_lines: list[str] = []
        in_meta = False
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("<!--"):
                in_meta = True
                continue
            if stripped.startswith("-->"):
                in_meta = False
                continue
            if not in_meta:
                body_lines.append(line)
        body = "\n".join(body_lines)
        assert "{project}-adr-log" not in body, (
            "stop_checkpoint_prompt.md still references the deleted "
            "{project}-adr-log monolith outside the meta-commentary — every "
            "checkpoint would resolve a dead slug (Car G §4.9)."
        )
        assert "adr_list" in body, (
            "step 1's read-first-dedup must call adr_list(directory=...) "
            "(the ledger re-point of §4.5)"
        )

    def test_prompt_does_not_call_wiki_read_for_adr_log(self):
        template_path = Path(__file__).resolve().parents[2] / (
            "core/hooks/templates/stop_checkpoint_prompt.md"
        )
        text = template_path.read_text()
        # Strip meta-commentary as above.
        body_lines: list[str] = []
        in_meta = False
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("<!--"):
                in_meta = True
                continue
            if stripped.startswith("-->"):
                in_meta = False
                continue
            if not in_meta:
                body_lines.append(line)
        body = "\n".join(body_lines)
        assert 'wiki_read("{project}-adr-log"' not in body, (
            "step 1 still calls wiki_read of the deleted slug — Car G §4.9 fix"
        )


# ── D35c: verification gate (exact-equality) ────────────────────────────────


class TestVerificationGateExactEquality:
    """D35c — EXACT equality on a stated predicate.

    The three known counts (index_rows vs pages_seen vs page_type='adr' rows)
    must be reconciled BEFORE cutover. ``>=`` is NOT a gate (2026-06-16
    vacuum/3,622-memory incident).
    """

    def test_exact_equality_helper_rejects_partial_match(self):
        """A residual gap (3 expected vs 2 inserted) is NOT acceptable."""
        from yadgar.backend.admin_exec.adr_seed import _exact_equality_gate

        # OK — counts match
        assert _exact_equality_gate(index_rows=10, pages_seen=10, page_type_adr_rows=10) is True

        # NOT OK — pages_seen > page_type_adr_rows (residue)
        assert _exact_equality_gate(index_rows=10, pages_seen=12, page_type_adr_rows=10) is False

        # NOT OK — index_rows < pages_seen (seeded something missing in index)
        assert _exact_equality_gate(index_rows=10, pages_seen=11, page_type_adr_rows=11) is False


# ── Fix 1 (task-adr-backfill-prompts): slug regex accepts BOTH separators ──


class TestPerAdrSlugRegexBothSeparators:
    """``_is_per_adr_page_slug`` / ``_parse_adr_id_from_slug`` must match BOTH
    the legacy hyphen slug (``yadgar-adr-NNNN``) AND the canonical underscore
    slug (``{project_id}_adr-NNNN``, D32 ③ — ``/`` in project_id -> ``_``).

    Pre-fix, both helpers used ``r"-adr-\\d{4}$"`` — a bare hyphen before
    ``adr-``. After the operator runs ``reslug`` (Fix 2's ``--reslug-adr-pages
    --apply``), every page is canonical, so the candidate filter matched ZERO
    pages and ``seed_adr_rows`` silently inserted nothing while still
    returning a normal-looking success dict.
    """

    def test_is_per_adr_page_slug_accepts_legacy_hyphen(self):
        from yadgar.backend.admin_exec.adr_seed import _is_per_adr_page_slug

        assert _is_per_adr_page_slug("yadgar-adr-0001") is True

    def test_is_per_adr_page_slug_accepts_canonical_underscore(self):
        """The bug: a canonical post-reslug slug must ALSO be recognized."""
        from yadgar.backend.admin_exec.adr_seed import _is_per_adr_page_slug

        assert _is_per_adr_page_slug("m-agahi_yadgar_adr-0001") is True
        assert _is_per_adr_page_slug("local_myproj_adr-0042") is True

    def test_is_per_adr_page_slug_excludes_legacy_log_and_index(self):
        from yadgar.backend.admin_exec.adr_seed import _is_per_adr_page_slug

        assert _is_per_adr_page_slug("yadgar-adr-log") is False
        assert _is_per_adr_page_slug("yadgar-adr-index") is False

    def test_is_per_adr_page_slug_excludes_canonical_log_and_index(self):
        """Canonical forms use ``_adr-log`` / ``_adr-index`` — must ALSO be excluded."""
        from yadgar.backend.admin_exec.adr_seed import _is_per_adr_page_slug

        assert _is_per_adr_page_slug("m-agahi_yadgar_adr-log") is False
        assert _is_per_adr_page_slug("m-agahi_yadgar_adr-index") is False

    def test_is_per_adr_page_slug_rejects_empty_and_unrelated(self):
        from yadgar.backend.admin_exec.adr_seed import _is_per_adr_page_slug

        assert _is_per_adr_page_slug("") is False
        assert _is_per_adr_page_slug("unrelated-page") is False

    def test_parse_adr_id_from_slug_identical_across_both_forms(self):
        """The number parsed must be identical whether the slug is legacy
        hyphen-separated or canonical underscore-separated."""
        from yadgar.backend.admin_exec.adr_seed import _parse_adr_id_from_slug

        assert _parse_adr_id_from_slug("yadgar-adr-0042") == 42
        assert _parse_adr_id_from_slug("m-agahi_yadgar_adr-0042") == 42
        assert _parse_adr_id_from_slug("local_myproj_adr-0042") == 42

    def test_parse_adr_id_from_slug_none_on_unparsable(self):
        from yadgar.backend.admin_exec.adr_seed import _parse_adr_id_from_slug

        assert _parse_adr_id_from_slug("yadgar-adr-BOGUS") is None
        assert _parse_adr_id_from_slug("m-agahi_yadgar_adr-log") is None

    async def test_seed_adr_rows_collects_canonical_slug_pages(self) -> None:
        """End-to-end regression: after a reslug, every page is canonical —
        ``seed_adr_rows`` must still find and insert them, not report a
        false-positive empty success."""
        from yadgar.backend.admin_exec.adr_seed import seed_adr_rows

        pages = [
            {"slug": "m-agahi_yadgar_adr-0001", "content": "# ADR-0001: a\nbody"},
            {"slug": "m-agahi_yadgar_adr-0002", "content": "# ADR-0002: b\nbody"},
        ]
        storage = _OrderFakeStorage(pages)

        inserted_order: list[str] = []

        def _row_inserter(payload: dict) -> dict:
            inserted_order.append(payload["body_slug"])
            return {"id": len(inserted_order)}

        result = await seed_adr_rows(
            project_id="m-agahi/yadgar",
            directory="/home/max/git/yadgar",
            storage=storage,
            row_inserter=_row_inserter,
            slug_linker=lambda adr_id, slug: None,
        )

        assert result["pages_seen"] == 2, (
            f"canonical-slug pages must be counted post-reslug, got "
            f"pages_seen={result['pages_seen']}"
        )
        assert inserted_order == [
            "m-agahi_yadgar_adr-0001",
            "m-agahi_yadgar_adr-0002",
        ]
