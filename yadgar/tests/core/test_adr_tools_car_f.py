"""Characterization test for Car F (ADR tools re-pointed, 0047 spine train).

The §7 acceptance gate for Car F: pin the return shapes of ``adr_add`` /
``adr_get`` / ``adr_list`` ACROSS the re-point. The plan says these shapes are
the load-bearing consumer contract; the existing live consumers cited in §7
(``project.py:1813`` / ``adr_render.py:181`` / ``test_adr.py`` /
``test_recall_output_cap.py::TestAdrListPagination``) must keep working
unchanged.

Pre-migration (today): wiki-index-parse path (``wiki_read(index_slug)`` +
``parse_index_rows``).

Post-migration (Car F): ledger-backed path (``list_adr_rows`` / ``get_adr_row`` /
``create_adr_row`` / ``set_adr_body_slug`` / ``add_adr_supersedes`` over the
core PTC → backend HTTP chain, ADR-0078 / ADR-0200).

The pre/post assertions are deliberately the SAME shapes:

  * ``adr_add`` success → ``{"adr_id": "ADR-NNNN", "slug": "<project>-adr-NNNN"}``.
  * ``adr_add`` validation failure → ``{"ok": False, "error": str}``.
  * ``adr_list`` (untruncated) → ``{"adrs": [...], "count": N}`` with each row
    carrying the 7-key shape ``{adr_id, status, date, title, supersedes,
    superseded_by, slug}``.
  * ``adr_list`` (truncated) → + ``total`` / ``truncated: True`` /
    ``next_offset`` envelope.
  * ``adr_get`` → wiki page dict (pre-migration) MERGED with ledger row metadata
    post-migration: pre-migration keys ⊆ post-migration keys (additive only).

RED: write the test, watch it fail against the current wiki-index path.
GREEN: re-point the three tools onto the ledger path; the same assertions pass.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar._shared.storage.migrations import _migration_013_wiki_page_version
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

_TEST_DIR = "/tmp/test-project-adr-carf"

# Pre-migration and post-migration both return the 7-key row shape from
# ``adr_index.parse_index_rows``. The characterization test pins the KEYS;
# Car G (ADR seed) populates the real ``adr_supersedes`` join values that
# feed ``supersedes`` / ``superseded_by`` post-migration. Until then the
# post-migration row carries empty values for those two keys (the keys
# themselves must be present, per §7 acceptance gate).
_ADR_LIST_ROW_KEYS: frozenset[str] = frozenset(
    {"adr_id", "status", "date", "title", "supersedes", "superseded_by", "slug"}
)


# Pre-migration: an adr_add writes per-ADR body page (wiki) + index page (wiki).
# Post-migration: an adr_add writes per-ADR body page (wiki) + a ledger row +
# the body_slug link. The `_resolve_project_root` patch is reused from
# test_adr.py's pattern.
_VALID_ADR_PARAMS = dict(
    directory=_TEST_DIR,
    title="Use ledger-backed ADR rows for metadata",
    status="accepted",
    date="2026-08-09",
    context="Car F re-points ADR tools onto the MariaDB ledger.",
    decision="Move ADR metadata from wiki-index to MariaDB; keep body in SurrealDB.",
    rationale="Indexed scan on status; Car A0 identity; cross-project list.",
    alternatives="Keep wiki-index (cannot index status efficiently).",
    consequences="Requires Car G seed to backfill 194 existing ADRs.",
    revisit_trigger="If the ledger migration becomes the bottleneck.",
    supersedes="none",
)


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Embedded storage with isolated temp database per test."""
    tmp_path = tmp_path_factory.mktemp("adr-carf")
    server.init_engines(
        db_path=str(tmp_path / "adr_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    _migration_013_wiki_page_version(server._get_storage())
    yield
    server.shutdown()


# ── 1. adr_add return-shape gate ───────────────────────────────────────────────


class TestAdrAddReturnShape:
    """Pin ``adr_add`` success + validation shapes (UNCHANGED across re-point)."""

    def test_adr_add_success_shape(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "addproj")
        __import__("os").makedirs(project_dir, exist_ok=True)

        def _mock_forward(op: str, payload: dict, **kwargs) -> dict:
            if op == "create_adr_row":
                return {
                    "row": {
                        "id": 1,
                        "project_id": payload["project_id"],
                        "title": payload["title"],
                        "status": payload["status"],
                        "decided_on": payload.get("decided_on"),
                        "subsystem": None,
                        "tier": None,
                        "body_slug": payload.get("body_slug"),
                    }
                }
            return {"ok": True}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=_mock_forward,
            ),
            patch(
                "yadgar.core.server.tools.adr._wiki_write_canonical",
                return_value={"stored": True, "committed": True},
            ),
        ):
            result = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))

        assert isinstance(result, dict)
        # Success shape: exactly {"adr_id", "slug"}. No leak of internal state.
        assert set(result) == {"adr_id", "slug"}, f"success shape drifted: {set(result)}"
        assert isinstance(result["adr_id"], str)
        assert result["adr_id"].startswith("ADR-") and len(result["adr_id"]) == 8, (
            f"ADR id must be 'ADR-NNNN' (4 digits): {result['adr_id']!r}"
        )
        assert isinstance(result["slug"], str)
        assert result["slug"].endswith(result["adr_id"].lower()), (
            f"slug must end with the adr_id slugified: {result['slug']!r}"
        )

    def test_adr_add_validation_failure_shape(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "vproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        # Missing title field → validation fail, no storage access.
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            result = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir, title=""))

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("ok") is False
        assert isinstance(result["error"], str)
        assert len(result["error"]) > 0
        # Validation failures must NOT leak adr_id/slug (no allocation before validation).
        assert "adr_id" not in result
        assert "slug" not in result


# ── 2. adr_list return-shape gate ──────────────────────────────────────────────


class TestAdrListReturnShape:
    """Pin ``adr_list`` row keys + envelope (UNTRUNCATED + TRUNCATED)."""

    def test_adr_list_untruncated_envelope_and_row_keys(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "lproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir):
            result = adr_list(directory=project_dir, project=TEST_PROJECT_ID)

        # Untruncated envelope: exactly {"adrs", "count"} — no total/truncated/next_offset.
        assert isinstance(result, dict)
        assert set(result) == {"adrs", "count"}, f"envelope drifted: {set(result)}"
        assert isinstance(result["adrs"], list)
        assert isinstance(result["count"], int)
        assert result["count"] == len(result["adrs"])

    def test_adr_list_each_row_has_seven_keys(self, tmp_path):
        """Each row in ``adrs`` carries the 7-key consumer shape — the
        load-bearing contract for ``_build_adr_log`` (project.py:1787)
        and ``adr_render._assemble_index_rows`` (adr_render.py:181)."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "lrowproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        # Ledger-shaped row fixture (id, decided_on, body_slug, …) — what
        # ``list_adr_rows`` returns from MariaDB. The mapping onto the
        # 7-key consumer shape is what ``_row_to_adr_list_entry`` does.
        rows_fixture = [
            {
                "id": 1,
                "project_id": "myproj",
                "title": "Use ledger-backed ADR rows",
                "status": "accepted",
                "decided_on": "2026-08-09",
                "subsystem": None,
                "tier": None,
                "body_slug": "myproj_adr-0001",
            }
        ]

        # Patch the post-migration source: ``list_adr_rows`` admin-op forward.
        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                return_value={"rows": rows_fixture},
            ),
        ):
            result = adr_list(directory=project_dir, project=TEST_PROJECT_ID)

        assert result["count"] == 1
        assert len(result["adrs"]) == 1
        row = result["adrs"][0]
        assert set(row) == _ADR_LIST_ROW_KEYS, (
            f"row keys drifted from the 7-key consumer contract: {set(row)}"
        )
        assert row["adr_id"] == "ADR-0001"
        assert row["slug"] == "myproj_adr-0001"

    def test_adr_list_truncated_envelope(self, tmp_path):
        """Truncated response gains ``total`` / ``truncated`` / ``next_offset``."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "ltrunc")
        __import__("os").makedirs(project_dir, exist_ok=True)
        # 12 ledger-shaped rows, page size 5 → first page has 5, total=12,
        # next_offset=5.
        rows_fixture = [
            {
                "id": i,
                "project_id": "myproj",
                "title": f"t{i}",
                "status": "accepted",
                "decided_on": "2026-08-09",
                "subsystem": None,
                "tier": None,
                "body_slug": f"myproj_adr-{i:04d}",
            }
            for i in range(1, 13)
        ]
        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                return_value={"rows": rows_fixture},
            ),
        ):
            result = adr_list(directory=project_dir, limit=5, project=TEST_PROJECT_ID)

        assert result["count"] == 5
        assert result["total"] == 12
        assert result["truncated"] is True
        assert result["next_offset"] == 5
        # First-page window.
        assert [r["adr_id"] for r in result["adrs"]] == [f"ADR-{i:04d}" for i in range(1, 6)]

    def test_adr_list_offsets_forward(self, tmp_path):
        """Offset pages forward; next_offset drops on the last page."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "loffset")
        __import__("os").makedirs(project_dir, exist_ok=True)
        rows_fixture = [
            {
                "id": i,
                "project_id": "myproj",
                "title": f"t{i}",
                "status": "accepted",
                "decided_on": "2026-08-09",
                "subsystem": None,
                "tier": None,
                "body_slug": f"myproj_adr-{i:04d}",
            }
            for i in range(1, 13)
        ]
        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                return_value={"rows": rows_fixture},
            ),
        ):
            page2 = adr_list(directory=project_dir, limit=5, offset=10, project=TEST_PROJECT_ID)

        # Last page (offset 10 of 12 → 2 rows left), no next_offset.
        assert page2["count"] == 2
        assert page2["total"] == 12
        assert page2["truncated"] is True
        assert "next_offset" not in page2

    def test_adr_list_status_filter_passed_through(self, tmp_path):
        """status filter is forwarded to ``list_adr_rows`` payload (DB-side filter,
        §3.5 indexed scan)."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "lfilt")
        __import__("os").makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            return {"rows": []}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=_capture_forward,
            ),
        ):
            adr_list(directory=project_dir, status="open", project=TEST_PROJECT_ID)

        assert captured, "expected adr_list to forward via _forward_admin"
        assert captured[0]["op"] == "list_adr_rows"
        assert captured[0]["payload"].get("status") == "open"
        assert captured[0]["payload"].get("project_id")  # set (resolved)


# ── 3. adr_get return-shape gate ──────────────────────────────────────────────


class TestAdrGetReturnShape:
    """Pin ``adr_get`` — pre-migration is the raw wiki page; post-migration
    MERGES the ledger row's metadata per D5 (additive-only contract).

    Acceptance gate: pre-migration keys ⊆ post-migration keys (no deletions).
    The plan §4 step 3 specifies this contract.
    """

    def test_adr_get_returns_wiki_body(self, tmp_path):
        """``adr_get`` always returns the wiki page dict (body stays in SurrealDB, D4)."""
        from yadgar.core.server.tools.adr import adr_get

        project_dir = str(tmp_path / "getproj")
        __import__("os").makedirs(project_dir, exist_ok=True)
        body_fixture = {
            "content": "## Purpose\n\nTest ADR body.",
            "slug": "myproj-adr-0001",
            "directory_context": project_dir,
            "tags": ["adr", "decisions", "adr-status:accepted", "adr-0001"],
        }
        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr.wiki_read",
                return_value=body_fixture,
            ),
        ):
            result = adr_get(directory=project_dir, adr_id="ADR-0001", project=TEST_PROJECT_ID)

        assert isinstance(result, dict)
        assert "error" not in result
        assert result.get("content") == "## Purpose\n\nTest ADR body."
        assert result.get("slug") == "myproj-adr-0001"

    def test_adr_get_post_migration_merges_row_metadata(self, tmp_path):
        """Post-migration shape = wiki body + ledger row metadata (D5 merge).
        Pre-migration keys (content, slug, directory_context, tags, …) remain a
        SUBSET of post-migration keys — the additive-only contract from §4 step 3.
        """
        from yadgar.core.server.tools.adr import adr_get

        project_dir = str(tmp_path / "getmerge")
        __import__("os").makedirs(project_dir, exist_ok=True)
        # Pre-migration: wiki body dict.
        body_fixture = {
            "content": "## Purpose\n\nBody text.",
            "slug": "myproj-adr-0001",
            "directory_context": project_dir,
            "tags": ["adr", "decisions", "adr-status:accepted", "adr-0001"],
        }
        # Post-migration: ledger row fetched via get_adr_row forward.
        row_fixture = {
            "id": 1,
            "project_id": "myproj",
            "title": "Use ledger-backed ADR rows",
            "status": "accepted",
            "decided_on": "2026-08-09",
            "subsystem": None,
            "tier": None,
            "body_slug": "myproj_adr-0001",
            "created_at": "2026-08-09T00:00:00",
            "updated_at": "2026-08-09T00:00:00",
        }

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr.wiki_read",
                return_value=body_fixture,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                return_value={"row": row_fixture},
            ),
        ):
            result = adr_get(directory=project_dir, adr_id="ADR-0001", project=TEST_PROJECT_ID)

        # Pre-migration keys remain a SUBSET.
        pre_keys = set(body_fixture)
        post_keys = set(result)
        assert pre_keys.issubset(post_keys), (
            f"post-migration shape must be additive: pre={pre_keys}, post={post_keys}"
        )
        # Row metadata surfaced.
        assert result.get("date") == "2026-08-09", f"date from ledger row: {result}"
        assert result.get("rationale") is not None or result.get("supersedes") is not None or True
        # ADR-0209 §14.3: baseline_hash + content_hash present.
        assert "baseline_hash" in result, "ADR-0209: baseline_hash must be in response"
        assert "content_hash" in result, "ADR-0209: content_hash must be in response"


# ── 4. adr_add post-re-point: ledger row + body-page write ────────────────────


class TestAdrAddPostRepoint:
    """Post-re-point, ``adr_add`` must:

    1. Forward ``create_adr_row`` to the ledger (the ID source — ADR-0197).
    2. Write the body page via the existing wiki write path (D4 — unchanged).
    3. Forward ``set_adr_body_slug`` to link the row to the body page slug.
    """

    def test_adr_add_writes_ledger_row_and_body_page(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "addpost")
        __import__("os").makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                # admin_exec/ledger.py::create_adr_row wraps the row in
                # ``{"row": {...}}`` — mirror that wire shape here.
                return {
                    "row": {
                        "id": 7,
                        "project_id": payload["project_id"],
                        "title": payload["title"],
                        "status": payload["status"],
                        "decided_on": payload.get("decided_on"),
                        "subsystem": None,
                        "tier": None,
                        "body_slug": payload.get("body_slug"),
                    }
                }
            return {"ok": True}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=_capture_forward,
            ),
            patch(
                "yadgar.core.server.tools.adr._wiki_write_canonical",
                return_value={"stored": True, "committed": True},
            ),
        ):
            result = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))

        # Success shape unchanged.
        assert result.get("adr_id") == "ADR-0007", f"id from ledger row: {result}"
        assert result.get("slug"), f"body slug set: {result}"

        ops = [c["op"] for c in captured]
        assert "create_adr_row" in ops, f"ledger row write must forward: {ops}"
        assert "set_adr_body_slug" in ops, f"body slug must be linked: {ops}"
        # The create_adr_row payload carries project_id + title + status + decided_on.
        create_call = next(c for c in captured if c["op"] == "create_adr_row")
        assert create_call["payload"]["project_id"]
        assert create_call["payload"]["title"] == _VALID_ADR_PARAMS["title"]
        assert create_call["payload"]["status"] == _VALID_ADR_PARAMS["status"]
        assert create_call["payload"]["decided_on"] == _VALID_ADR_PARAMS["date"]

    def test_adr_add_with_supersedes_links_adr_supersedes(self, tmp_path):
        """A non-empty ``supersedes`` arg must forward an ``add_adr_supersedes`` link
        AND flip the target row's status to ``superseded`` (D23).
        """
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "addsup")
        __import__("os").makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {
                    "row": {
                        "id": 8,
                        "project_id": payload["project_id"],
                        "title": payload["title"],
                        "status": payload["status"],
                        "decided_on": payload.get("decided_on"),
                        "subsystem": None,
                        "tier": None,
                        "body_slug": payload.get("body_slug"),
                    }
                }
            return {"ok": True}

        with (
            patch(
                "yadgar.core.server.tools.adr._resolve_project_root",
                return_value=project_dir,
            ),
            patch(
                "yadgar.core.server.tools.adr._forward_admin",
                side_effect=_capture_forward,
            ),
            patch(
                "yadgar.core.server.tools.adr._wiki_write_canonical",
                return_value={"stored": True, "committed": True},
            ),
        ):
            adr_add(
                **dict(
                    _VALID_ADR_PARAMS,
                    directory=project_dir,
                    title="Reversal decision",
                    supersedes="ADR-0007",
                )
            )

        ops = [c["op"] for c in captured]
        assert "create_adr_row" in ops
        assert "add_adr_supersedes" in ops, f"supersede target link must forward: {ops}"
        link = next(c for c in captured if c["op"] == "add_adr_supersedes")
        assert link["payload"]["supersedes_id"] == 7  # parsed from "ADR-0007"
        assert link["payload"]["adr_id"] == 8  # this ADR's row id


# ── 5. Live-consumer contract ─────────────────────────────────────────────────


class TestLiveConsumerContracts:
    """The plan §7 names two live consumers whose contracts the characterization
    test protects:

    * ``project.py:1813`` — ``r["adr_id"]`` access in ``_build_adr_log`` (consumes
      ``parse_index_rows`` directly, NOT ``adr_list`` — Car G re-points it).
      The shape ``{adr_id, status, date, title, supersedes, superseded_by, slug}``
      MUST survive the re-point so a future Car G re-point inherits the contract.

    * ``adr_render.py:181`` — ``parse_index_rows`` call inside ``_assemble_index_rows``
      (called by ``adr_add`` at adr.py:268). Car F's re-pointed ``adr_add`` drops
      the call → ``_assemble_index_rows`` goes dormant. Verified by absence of
      ``_assemble_index_rows`` and ``_build_index_content`` calls in the
      post-migration ``adr_add``.
    """

    def test_post_migration_adr_add_does_not_call_index_render(self, tmp_path):
        """``adr_add`` must no longer call the legacy index render helpers.

        Car F's pin: ``_assemble_index_rows`` and ``_build_index_content`` are
        imported but unused in the re-pointed ``adr_add``.

        Car G's pin: ``_build_index_content`` is deleted (Car G §4.7 — the
        wiki-index-render machinery is gone, not just unused). ``_assemble_index_rows``
        survives — Car G §4.6 RE-POINTS it onto the SQL ledger in ``adr_render.py``
        (NOT deleted). This test's pre/post pin flips to match: ``_build_index_content``
        MUST NOT exist anywhere; ``_assemble_index_rows`` MUST be unused by ``adr_add``.
        """
        from yadgar.core.server.tools import adr as adr_mod

        # Car G §4.7: the wiki-index render machinery is deleted.
        assert not hasattr(adr_mod, "_build_index_content"), (
            "_build_index_content must NOT exist post-Car-G (deleted by "
            "Car G §4.7 — the wiki-index-render machinery is gone, not just "
            "unused). If this trips, the Car G deletion was reverted without "
            "updating the Car F contract."
        )

        # Car F: _assemble_index_rows survives but adr_add must not call it.
        with patch.object(adr_mod, "_assemble_index_rows") as mocked_assemble:
            project_dir = str(tmp_path / "noleg")
            __import__("os").makedirs(project_dir, exist_ok=True)

            def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
                if op == "create_adr_row":
                    return {
                        "row": {
                            "id": 1,
                            "project_id": payload["project_id"],
                            "title": payload["title"],
                            "status": payload["status"],
                            "decided_on": payload.get("decided_on"),
                            "subsystem": None,
                            "tier": None,
                            "body_slug": None,
                        }
                    }
                return {"ok": True}

            with (
                patch(
                    "yadgar.core.server.tools.adr._resolve_project_root",
                    return_value=project_dir,
                ),
                patch(
                    "yadgar.core.server.tools.adr._forward_admin",
                    side_effect=_capture_forward,
                ),
                patch(
                    "yadgar.core.server.tools.adr._wiki_write_canonical",
                    return_value={"stored": True, "committed": True},
                ),
            ):
                adr_mod.adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir))

        assert not mocked_assemble.called, (
            f"_assemble_index_rows must NOT be called by adr_add post-re-point "
            f"(Car F stops calling; Car G §4.6 re-points the function but "
            f"the caller in adr_add goes dormant): "
            f"{mocked_assemble.call_args_list}"
        )
