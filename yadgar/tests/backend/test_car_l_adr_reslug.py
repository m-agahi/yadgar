"""Tests for the Car L ADR re-slug admin op — ``reslug_adr_pages``.

D32 ③. The op re-slugs every ADR wiki page to the new
``{project_id}_adr-NNNN`` format (``/`` → ``_``), updates
``wiki_crossref.from_slug``/``to_slug``, replaces inline ``[[old-slug]]``
text in page bodies, and (via MariaStorageEngine) stamps the SQL
``adr.body_slug``.

Coverage here is the BACKEND execution body. ``asyncio.run`` calls into
``MariaStorageEngine.set_adr_body_slug`` are mocked — the test only
verifies that body-slug stamping fires once per page (Car A ships the
async Maria helper; this op CALLS it).

Dry-run mode (the safe default) returns the manifest WITHOUT writing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from yadgar.backend.admin_exec.reslug import (
    ADR_BODY_RE,
    NEW_SLUG_TEMPLATE,
    reslug_adr_pages,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


class _FakeStorage:
    """In-memory double: wiki_page, wiki_crossref.

    Honors a tiny slice of the contract that ``reslug_adr_pages`` uses:
    ``_q`` returns rows for SELECTs, ``update_wiki_page`` records
    its arguments, and ``_extract_id`` strips the ``wiki_page:N``
    prefix. The wiki_crossref table is mirrored via ``update_crossrefs``.
    """

    def __init__(
        self,
        pages: list[dict] | None = None,
        crossrefs: list[dict] | None = None,
    ) -> None:
        self.pages: list[dict] = list(pages or [])
        self.crossrefs: list[dict] = list(crossrefs or [])
        self.updates: list[tuple[int, dict]] = []
        self.crossref_updates: list[dict] = []

    def _q(self, surql: str, params: dict | None = None) -> list[dict]:
        sql = surql.strip().upper()
        params = params or {}

        if "FROM WIKI_PAGE WHERE SLUG = $" in sql or "FROM WIKI_PAGE WHERE SLUG = $SLUG" in sql:
            target_slug = params.get("slug") or params.get("s")
            return [dict(p) for p in self.pages if p.get("slug") == target_slug]

        if "FROM WIKI_PAGE" in sql and "SLUG STARTSWITH" in sql:
            prefix = params.get("slug_prefix", "")
            return [dict(p) for p in self.pages if p.get("slug", "").startswith(prefix)]

        if "FROM WIKI_PAGE WHERE" in sql and "TAGS CONTAINS" in sql:
            return [dict(p) for p in self.pages]

        return []

    def _extract_id(self, raw: Any) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, str) and ":" in raw:
            return int(raw.rsplit(":", 1)[1])
        return int(raw)

    def update_wiki_page(self, page_id: int, fields: dict, _sanctioned: bool = False) -> bool:
        self.updates.append((page_id, fields))
        for p in self.pages:
            if p["id"] == page_id:
                p.update(fields)
                return True
        return False

    def update_wiki_crossref_from(self, old_slug: str, new_slug: str) -> None:
        for r in self.crossrefs:
            if r["from_slug"] == old_slug:
                r["from_slug"] = new_slug
        self.crossref_updates.append({"from": old_slug, "to": new_slug})

    def update_wiki_crossref_to(self, old_slug: str, new_slug: str) -> None:
        for r in self.crossrefs:
            if r["to_slug"] == old_slug:
                r["to_slug"] = new_slug
        self.crossref_updates.append({"from": old_slug, "to": new_slug})


def _adr_pages() -> list[dict]:
    """3 fixture ADR pages — old ``yadgar-adr-NNNN`` slugs."""
    return [
        {
            "id": 1,
            "slug": "yadgar-adr-0001",
            "content": "See [[yadgar-adr-0002]]",
            "directory_context": "/home/max/git/yadgar",
        },
        {
            "id": 2,
            "slug": "yadgar-adr-0002",
            "content": "Cross-ref to [[yadgar-adr-0001]]",
            "directory_context": "/home/max/git/yadgar",
        },
        {
            "id": 3,
            "slug": "yadgar-adr-0003",
            "content": "Independent body",
            "directory_context": "/home/max/git/yadgar",
        },
    ]


def _crossrefs() -> list[dict]:
    """One crossref linking adr-0001 → adr-0002."""
    return [{"from_slug": "yadgar-adr-0001", "to_slug": "yadgar-adr-0002"}]


# ── Tests ──────────────────────────────────────────────────────────────────


class TestReslugConstants:
    """Slug regex + template sanity."""

    def test_adr_body_re_matches_old_format(self) -> None:
        assert ADR_BODY_RE.match("yadgar-adr-0001")
        assert ADR_BODY_RE.match("yadgar-adr-1234")
        assert not ADR_BODY_RE.match("yadgar-adr-foo")
        assert not ADR_BODY_RE.match("not-adr-0001")

    def test_new_slug_template(self) -> None:
        # The template renders owner/repo → owner_repo
        rendered = NEW_SLUG_TEMPLATE.format(project_id="m-agahi/yadgar", n=42)
        assert rendered == "m-agahi_yadgar_adr-0042"


class TestReslugDryRun:
    """Dry-run returns the manifest WITHOUT writing."""

    def test_dry_run_writes_nothing(self) -> None:
        storage = _FakeStorage(pages=_adr_pages(), crossrefs=_crossrefs())
        manifest = reslug_adr_pages(
            {
                "project_id": "m-agahi/yadgar",
                "dry_run": True,
            },
            storage=storage,
        )
        # Manifest must have 3 entries (one per ADR page).
        assert len(manifest["rewrites"]) == 3
        # Manifest must NOT have written any updates.
        assert storage.updates == []
        # Pages must still carry old slugs.
        assert all(p["slug"].startswith("yadgar-adr-") for p in storage.pages)
        # Crossrefs untouched.
        assert storage.crossrefs[0]["from_slug"] == "yadgar-adr-0001"

    def test_dry_run_returns_old_to_new_map(self) -> None:
        storage = _FakeStorage(pages=_adr_pages(), crossrefs=_crossrefs())
        manifest = reslug_adr_pages(
            {
                "project_id": "m-agahi/yadgar",
                "dry_run": True,
            },
            storage=storage,
        )
        by_old = {r["old"]: r["new"] for r in manifest["rewrites"]}
        assert by_old["yadgar-adr-0001"] == "m-agahi_yadgar_adr-0001"
        assert by_old["yadgar-adr-0002"] == "m-agahi_yadgar_adr-0002"
        assert by_old["yadgar-adr-0003"] == "m-agahi_yadgar_adr-0003"


class TestReslugApply:
    """Apply mode rewrites slug, crossrefs, inline ``[[]]`` links, body_slug."""

    def test_apply_rewrites_wiki_page_slugs(self) -> None:
        storage = _FakeStorage(pages=_adr_pages(), crossrefs=_crossrefs())
        with patch("yadgar.backend.admin_exec.reslug._sync_set_adr_body_slug") as mock_set:
            reslug_adr_pages(
                {
                    "project_id": "m-agahi/yadgar",
                    "dry_run": False,
                    "adr_id_by_slug": {
                        "yadgar-adr-0001": 1,
                        "yadgar-adr-0002": 2,
                        "yadgar-adr-0003": 3,
                    },
                },
                storage=storage,
            )

        assert mock_set.call_count == 3
        assert all(p["slug"].startswith("m-agahi_yadgar_adr-") for p in storage.pages)

    def test_apply_rewrites_crossref_from_slug(self) -> None:
        storage = _FakeStorage(pages=_adr_pages(), crossrefs=_crossrefs())
        with patch("yadgar.backend.admin_exec.reslug._sync_set_adr_body_slug"):
            reslug_adr_pages(
                {
                    "project_id": "m-agahi/yadgar",
                    "dry_run": False,
                    "adr_id_by_slug": {
                        "yadgar-adr-0001": 1,
                        "yadgar-adr-0002": 2,
                        "yadgar-adr-0003": 3,
                    },
                },
                storage=storage,
            )
        # The original from_slug was yadgar-adr-0001; now it must be the new one.
        assert storage.crossrefs[0]["from_slug"] == "m-agahi_yadgar_adr-0001"
        assert storage.crossrefs[0]["to_slug"] == "m-agahi_yadgar_adr-0002"

    def test_apply_replaces_inline_double_brackets(self) -> None:
        storage = _FakeStorage(pages=_adr_pages(), crossrefs=_crossrefs())
        with patch("yadgar.backend.admin_exec.reslug._sync_set_adr_body_slug"):
            reslug_adr_pages(
                {
                    "project_id": "m-agahi/yadgar",
                    "dry_run": False,
                    "adr_id_by_slug": {
                        "yadgar-adr-0001": 1,
                        "yadgar-adr-0002": 2,
                        "yadgar-adr-0003": 3,
                    },
                },
                storage=storage,
            )
        page1 = next(p for p in storage.pages if p["id"] == 1)
        assert "[[m-agahi_yadgar_adr-0002]]" in page1["content"]
        assert "[[yadgar-adr-0002]]" not in page1["content"]

    def test_apply_stamps_body_slug_per_page(self) -> None:
        storage = _FakeStorage(pages=_adr_pages(), crossrefs=_crossrefs())
        with patch("yadgar.backend.admin_exec.reslug._sync_set_adr_body_slug") as mock_set:
            reslug_adr_pages(
                {
                    "project_id": "m-agahi/yadgar",
                    "dry_run": False,
                    "adr_id_by_slug": {
                        "yadgar-adr-0001": 1,
                        "yadgar-adr-0002": 2,
                        "yadgar-adr-0003": 3,
                    },
                },
                storage=storage,
            )
        stamped = [c.args[1] for c in mock_set.call_args_list]
        assert "m-agahi_yadgar_adr-0001" in stamped
        assert "m-agahi_yadgar_adr-0002" in stamped
        assert "m-agahi_yadgar_adr-0003" in stamped


class TestReslugCollisionGuard:
    """A page whose TARGET slug already exists (occupied by a DIFFERENT page)
    must be skipped, not written — no unique-index violation, no partial
    run that dies mid-way. ``yadgar-adr-0001`` -> ``m-agahi_yadgar_adr-0001``
    is exactly this: the target already exists as its own (already-reslugged)
    page in the live corpus.
    """

    def _pages_with_collision(self) -> list[dict]:
        return [
            # This page is the OLD-format page that WOULD reslug to
            # "m-agahi_yadgar_adr-0001" — but that slug is already taken
            # by a different page (id=99, the occupant).
            {
                "id": 1,
                "slug": "yadgar-adr-0001",
                "content": "old body",
                "directory_context": "/home/max/git/yadgar",
            },
            {
                "id": 99,
                "slug": "m-agahi_yadgar_adr-0001",
                "content": "occupant body",
                "directory_context": "/home/max/git/yadgar",
            },
            # A page with no collision — must still be rewritten normally.
            {
                "id": 2,
                "slug": "yadgar-adr-0002",
                "content": "body 2",
                "directory_context": "/home/max/git/yadgar",
            },
        ]

    def test_dry_run_reports_collision_without_writing(self) -> None:
        storage = _FakeStorage(pages=self._pages_with_collision(), crossrefs=[])
        manifest = reslug_adr_pages(
            {
                "project_id": "m-agahi/yadgar",
                "dry_run": True,
            },
            storage=storage,
        )
        collisions = manifest.get("collisions")
        assert collisions, "dry_run manifest must report the collision the apply would skip"
        assert any(
            c["old"] == "yadgar-adr-0001" and c["new"] == "m-agahi_yadgar_adr-0001"
            for c in collisions
        )
        # A dry run performs no writes regardless of collisions.
        assert storage.updates == []

    def test_apply_skips_colliding_page_and_continues(self) -> None:
        storage = _FakeStorage(pages=self._pages_with_collision(), crossrefs=[])
        with patch("yadgar.backend.admin_exec.reslug._sync_set_adr_body_slug") as mock_set:
            result = reslug_adr_pages(
                {
                    "project_id": "m-agahi/yadgar",
                    "dry_run": False,
                    "adr_id_by_slug": {
                        "yadgar-adr-0001": 1,
                        "yadgar-adr-0002": 2,
                    },
                },
                storage=storage,
            )

        # The colliding page (id=1) must NOT have been rewritten.
        colliding_page = next(p for p in storage.pages if p["id"] == 1)
        assert colliding_page["slug"] == "yadgar-adr-0001", (
            "colliding page must be skipped, not overwritten"
        )
        # The occupant (id=99) must be untouched.
        occupant = next(p for p in storage.pages if p["id"] == 99)
        assert occupant["slug"] == "m-agahi_yadgar_adr-0001"
        assert occupant["content"] == "occupant body"

        # The non-colliding page (id=2) must still be rewritten normally —
        # one collision must not abort the whole run.
        rewritten = next(p for p in storage.pages if p["id"] == 2)
        assert rewritten["slug"] == "m-agahi_yadgar_adr-0002"

        # Reported in the result so the operator sees it.
        collisions = result.get("collisions")
        assert collisions, "apply result must report the skipped collision"
        collision = next(c for c in collisions if c["old"] == "yadgar-adr-0001")
        assert collision["new"] == "m-agahi_yadgar_adr-0001"
        assert collision["occupant_id"] == 99

        # Only the non-colliding page's body_slug was stamped.
        assert mock_set.call_count == 1


class TestReslugIdempotency:
    """A second run skips every page already in the new format."""

    def test_already_reslugged_pages_are_skipped(self) -> None:
        # First fixture page is already in the new format.
        pages = [
            {
                "id": 1,
                "slug": "m-agahi_yadgar_adr-0001",
                "content": "Body",
                "directory_context": "/home/max/git/yadgar",
            },
            {
                "id": 2,
                "slug": "yadgar-adr-0042",
                "content": "Body",
                "directory_context": "/home/max/git/yadgar",
            },
        ]
        storage = _FakeStorage(pages=pages, crossrefs=[])
        with patch("yadgar.backend.admin_exec.reslug._sync_set_adr_body_slug") as mock_set:
            reslug_adr_pages(
                {
                    "project_id": "m-agahi/yadgar",
                    "dry_run": False,
                    "adr_id_by_slug": {"yadgar-adr-0042": 2},
                },
                storage=storage,
            )
        # Only adr-0042 (the old-format one) was rewritten.
        assert len(storage.updates) == 1
        # body_slug was set exactly once.
        assert mock_set.call_count == 1
        # adr-0042's content/page was updated (slug→ new format).
        target = next(p for p in storage.pages if p["id"] == 2)
        assert target["slug"] == "m-agahi_yadgar_adr-0042"
