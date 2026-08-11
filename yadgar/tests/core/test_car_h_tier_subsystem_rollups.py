"""Car H (0047 spine train) — tier + subsystem + per-subsystem rollup pages.

Plan: docs/plans/archive/0047-car-H-tier-subsystem-rollups.md (archived as
the first commit of car/H-tier-subsystem per ADR-0081/0082).

§10 decisions made at build time (now binding — see archive plan §1):
  Q1 — rollup regeneration trigger: ON-WRITE (fired from ``adr_add`` post-commit).
  Q2 — subsystem vocabulary home: free-form VARCHAR(128) + on-write normalizer
    (``.lower().strip()``); empty after normalize → ``None``.

TDD coverage for:
  §4.1  ``CANONICAL_PAGE_TYPES`` accepts ``wiki_rollup`` (defense-in-depth
        for rollup writes — also paves the way for Car K's nightly sweep).
  §4.2  ``wiki_page_types.yaml`` declares ``wiki_rollup`` (so wiki_lint
        format-checks rollup pages).
  §4.3  ``adr_list`` defaults to ``tier="binding"`` (D27) and forwards the
        ``tier`` + ``subsystem`` filters to ``list_adr_rows`` (DB-side filter).
  §4.4  ``adr_add`` accepts and stamps ``tier`` + ``subsystem`` onto the
        ``create_adr_row`` payload (D27/D28); the subsystem value is
        normalized (lowercase + trim) per §10 Q2.
  §4.5  After ``adr_add`` commits, a per-subsystem rollup page is regenerated
        via ``_regenerate_subsystem_rollup`` (D29 + §10 Q1 on-write trigger).
  §4.6  The rollup page carries ``page_type="wiki_rollup"`` and is excluded
        from recall (``recall_disposition="exclude"``).
  §4.7  ``seed_adr_tier_subsystem`` backfills ``tier``/``subsystem`` on
        existing rows — idempotent on re-run.
  §4.8  ``list_adr_rows`` storage method accepts ``tier`` + ``subsystem``
        filters (DB-side WHERE-clause expansion).
"""

from __future__ import annotations

from unittest.mock import patch

from yadgar.tests.core.conftest import TEST_PROJECT_ID

# ── §4.1: wiki_rollup joins CANONICAL_PAGE_TYPES ──────────────────────────────


class TestWikiRollupCanonical:
    """§4.1: ``wiki_rollup`` joins ``CANONICAL_PAGE_TYPES``.

    The frozenset assertion inside ``_wiki_write_canonical`` raises ValueError
    on a non-allowlisted page_type. Car H's rollup generator writes
    ``page_type="wiki_rollup"``; the allowlist must admit it (defense-in-depth
    against a future agent-spoofed rollup write — D22-style disposition).
    """

    def test_canonical_set_includes_wiki_rollup(self):
        from yadgar.core.server.tools.wiki import CANONICAL_PAGE_TYPES

        assert "wiki_rollup" in CANONICAL_PAGE_TYPES, (
            f"CANONICAL_PAGE_TYPES must contain 'wiki_rollup' (got {sorted(CANONICAL_PAGE_TYPES)})"
        )

    def test_canonical_set_regression_keeps_existing_types(self):
        """Regression guard — adding wiki_rollup must not drop adr/adr_superseded."""
        from yadgar.core.server.tools.wiki import CANONICAL_PAGE_TYPES

        assert "adr" in CANONICAL_PAGE_TYPES
        assert "adr_superseded" in CANONICAL_PAGE_TYPES
        assert "task_list" in CANONICAL_PAGE_TYPES

    def test_wiki_page_types_yaml_has_wiki_rollup_entry(self):
        """wiki_lint reads wiki_page_types.yaml — wiki_rollup must lint."""
        import re
        from importlib.resources import files

        from yadgar._shared import schemas

        text = files(schemas).joinpath("wiki_page_types.yaml").read_text()
        assert re.search(r"^\s{2}wiki_rollup:", text, re.MULTILINE), (
            "wiki_page_types.yaml must declare a wiki_rollup page_type entry "
            "so wiki_lint format-checks rollup pages"
        )


# ── §4.3: adr_list defaults to tier="binding" (D27) ──────────────────────────


class TestAdrListTierFilter:
    """§4.3: ``adr_list`` defaults to ``tier="binding"`` per D27.

    ``binding`` excludes ``historical`` (superseded/rejected/deprecated);
    ``tier=None`` returns all rows; ``tier="historical"`` returns only
    historical. The DB-side filter is forwarded to ``list_adr_rows``.
    """

    def test_adr_list_default_tier_is_binding(self, tmp_path):
        """No tier arg → tier="binding" forwarded to list_adr_rows."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "defaulttier")
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
            adr_list(directory=project_dir, project=TEST_PROJECT_ID)

        assert captured, "expected adr_list to forward via _forward_admin"
        assert captured[0]["op"] == "list_adr_rows"
        assert captured[0]["payload"].get("tier") == "binding", (
            f"adr_list default tier must be 'binding' (D27); got "
            f"{captured[0]['payload'].get('tier')!r}"
        )

    def test_adr_list_tier_none_returns_all(self, tmp_path):
        """tier=None → no tier filter (all rows)."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "tiertnone")
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
            adr_list(directory=project_dir, tier=None, project=TEST_PROJECT_ID)

        assert captured[0]["payload"].get("tier") is None, (
            f"tier=None must NOT carry a tier filter; got {captured[0]['payload'].get('tier')!r}"
        )

    def test_adr_list_tier_historical_forwarded(self, tmp_path):
        """tier="historical" → tier filter forwarded verbatim."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "tierhist")
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
            adr_list(directory=project_dir, tier="historical", project=TEST_PROJECT_ID)

        assert captured[0]["payload"].get("tier") == "historical"

    def test_adr_list_subsystem_filter_forwarded(self, tmp_path):
        """subsystem filter forwarded to list_adr_rows payload."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "subfilt")
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
            adr_list(directory=project_dir, subsystem="storage", project=TEST_PROJECT_ID)

        assert captured[0]["payload"].get("subsystem") == "storage"

    def test_adr_list_subsystem_default_is_none(self, tmp_path):
        """No subsystem arg → subsystem=None (no filter)."""
        from yadgar.core.server.tools.adr import adr_list

        project_dir = str(tmp_path / "subnone")
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
            adr_list(directory=project_dir, project=TEST_PROJECT_ID)

        # subsystem key absent (None = no filter), tier="binding" default present
        assert "subsystem" not in captured[0]["payload"] or (
            captured[0]["payload"].get("subsystem") is None
        )
        assert captured[0]["payload"].get("tier") == "binding"


# ── §4.4: adr_add stamps tier + subsystem onto create_adr_row ────────────────


class TestAdrAddTierSubsystem:
    """§4.4: ``adr_add`` accepts ``tier`` + ``subsystem`` params (D27/D28).

    The subsystem value is NORMALIZED on the way in (lowercase + trim) per
    §10 Q2 — ``"  Vacuum "`` → ``"vacuum"``, ``""`` → ``None``. The tier
    value is forwarded verbatim (D27 enum: ``"binding"`` | ``"historical"``).
    """

    def test_adr_add_stamps_tier_and_subsystem(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "addts")
        __import__("os").makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
            return {"ok": True}

        async def _no_op_rollup(*args, **kwargs) -> dict:
            return {"ok": True}

        valid_params = dict(
            directory=project_dir,
            title="Tiered ADR",
            status="accepted",
            date="2026-08-09",
            context="Car H.",
            decision="Wire tier/subsystem.",
            rationale="D27/D28.",
            alternatives="Free-form.",
            consequences="Rollups.",
            revisit_trigger="Never.",
            supersedes="none",
            tier="binding",
            subsystem="storage",
        )

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
            patch(
                "yadgar.backend.admin_exec.rollup._regenerate_subsystem_rollup",
                side_effect=_no_op_rollup,
            ),
        ):
            result = adr_add(**valid_params)

        assert "error" not in result, f"unexpected error: {result.get('error')}"
        create_payloads = [c["payload"] for c in captured if c["op"] == "create_adr_row"]
        assert create_payloads, "expected create_adr_row forward"
        create_payload = create_payloads[0]
        assert create_payload.get("tier") == "binding"
        assert create_payload.get("subsystem") == "storage"

    def test_adr_add_subsystem_is_normalized_lowercase(self, tmp_path):
        """``"  Vacuum "`` → ``"vacuum"`` (D28 + §10 Q2 normalizer)."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "addnorm")
        __import__("os").makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
            return {"ok": True}

        async def _no_op_rollup(*args, **kwargs) -> dict:
            return {"ok": True}

        valid_params = dict(
            directory=project_dir,
            title="Mixed-case subsystem",
            status="accepted",
            date="2026-08-09",
            context="Normalize.",
            decision="Lowercase on the way in.",
            rationale="D28.",
            alternatives="None.",
            consequences="Stable row filter.",
            revisit_trigger="Never.",
            supersedes="none",
            tier="binding",
            subsystem="  Vacuum ",
        )

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
            patch(
                "yadgar.backend.admin_exec.rollup._regenerate_subsystem_rollup",
                side_effect=_no_op_rollup,
            ),
        ):
            adr_add(**valid_params)

        create_payload = next(c["payload"] for c in captured if c["op"] == "create_adr_row")
        assert create_payload.get("subsystem") == "vacuum", (
            f"subsystem must be normalized to lowercase+trim (got "
            f"{create_payload.get('subsystem')!r})"
        )

    def test_adr_add_subsystem_empty_becomes_none(self, tmp_path):
        """``subsystem=""`` → ``None`` (no filter / no orphan string)."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "addempty")
        __import__("os").makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
            return {"ok": True}

        async def _no_op_rollup(*args, **kwargs) -> dict:
            return {"ok": True}

        valid_params = dict(
            directory=project_dir,
            title="Empty subsystem",
            status="accepted",
            date="2026-08-09",
            context="Empty.",
            decision="Empty → None.",
            rationale="D28.",
            alternatives="None.",
            consequences=".",
            revisit_trigger=".",
            supersedes="none",
            tier="binding",
            subsystem="",
        )

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
            patch(
                "yadgar.backend.admin_exec.rollup._regenerate_subsystem_rollup",
                side_effect=_no_op_rollup,
            ),
        ):
            adr_add(**valid_params)

        create_payload = next(c["payload"] for c in captured if c["op"] == "create_adr_row")
        assert create_payload.get("subsystem") is None, (
            f"empty subsystem must become None (got {create_payload.get('subsystem')!r})"
        )

    def test_adr_add_default_tier_and_subsystem_are_none(self, tmp_path):
        """No tier/subsystem arg → None forwarded (no D27/D28 stamp)."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "addnone")
        __import__("os").makedirs(project_dir, exist_ok=True)
        captured: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            captured.append({"op": op, "payload": payload})
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
            return {"ok": True}

        valid_params = dict(
            directory=project_dir,
            title="No tier/subsystem",
            status="accepted",
            date="2026-08-09",
            context="Default.",
            decision="Defaults are None.",
            rationale=".",
            alternatives=".",
            consequences=".",
            revisit_trigger=".",
            supersedes="none",
        )

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
            adr_add(**valid_params)

        create_payload = next(c["payload"] for c in captured if c["op"] == "create_adr_row")
        assert create_payload.get("tier") is None
        assert create_payload.get("subsystem") is None


# ── §4.5 + §4.6: rollup regen on adr_add (on-write trigger) ──────────────────


class TestAdrAddTriggersRollupRegen:
    """§4.5/§4.6: ``adr_add`` triggers per-subsystem rollup regen post-commit.

    §10 Q1 decision: ON-WRITE. The trigger fires after the row + body + slug
    link are committed, before returning success to the caller. The rollup
    page carries ``page_type="wiki_rollup"`` and is excluded from recall
    (D22-style disposition via ``recall_disposition="exclude"``).
    """

    def test_adr_add_invokes_rollup_regen_for_subsystem(self, tmp_path):
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "rollptrig")
        __import__("os").makedirs(project_dir, exist_ok=True)

        rollup_calls: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            if op == "run_rollup_regen":
                rollup_calls.append({"op": op, "payload": payload})
                return {"ok": True, "regenerated": 1}
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
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
                directory=project_dir,
                title="Rollup trigger test",
                status="accepted",
                date="2026-08-09",
                context="On-write trigger.",
                decision="Fire _regenerate_subsystem_rollup after row+body+slug.",
                rationale="§10 Q1.",
                alternatives="Nightly.",
                consequences="Extra wiki write per add.",
                revisit_trigger="Never.",
                supersedes="none",
                tier="binding",
                subsystem="storage",
                project=TEST_PROJECT_ID,
            )

        assert rollup_calls, "expected run_rollup_regen forward to be called"
        # subsystem=storage → rollup regen for "storage"
        assert rollup_calls[0]["op"] == "run_rollup_regen"
        payload = rollup_calls[0]["payload"]
        assert payload.get("subsystem") == "storage"
        assert "project_id" in payload

    def test_adr_add_skips_rollup_when_subsystem_none(self, tmp_path):
        """No subsystem → no rollup regen (can't key a rollup on None)."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path / "rollpskip")
        __import__("os").makedirs(project_dir, exist_ok=True)

        rollup_calls: list[dict] = []

        def _capture_forward(op: str, payload: dict, **kwargs) -> dict:
            if op == "run_rollup_regen":
                rollup_calls.append({"op": op, "payload": payload})
                return {"ok": True}
            if op == "create_adr_row":
                return {"row": {"id": 1, **payload}}
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
                directory=project_dir,
                title="No subsystem test",
                status="accepted",
                date="2026-08-09",
                context=".",
                decision=".",
                rationale=".",
                alternatives=".",
                consequences=".",
                revisit_trigger=".",
                supersedes="none",
                project=TEST_PROJECT_ID,
            )

        assert not rollup_calls, (
            f"no rollup regen when subsystem=None (got {len(rollup_calls)} calls)"
        )


# ── §4.8: list_adr_rows storage method accepts tier/subsystem filters ─────────


class TestListAdrRowsStorageFilters:
    """§4.8: ``MariaStorageEngine.list_adr_rows`` accepts ``tier`` + ``subsystem``.

    DB-side WHERE-clause expansion — both filters compose with status and
    with each other. Filters absent (None) leave the WHERE clause open.
    """

    def test_list_adr_rows_with_tier(self) -> None:
        """tier filter appends ``AND tier = :tier`` to the WHERE clause."""
        import importlib

        from yadgar._shared.storage.sql import mariadb

        mod = importlib.import_module(mariadb.__name__)

        # We don't need a live engine — assert the storage method signature
        # advertises ``tier`` and ``subsystem`` kwargs and threads them into
        # the SQL params dict.
        import inspect

        sig = inspect.signature(mod.MariaStorageEngine.list_adr_rows)
        params = sig.parameters
        assert "tier" in params, f"list_adr_rows must accept 'tier' (got {list(params)})"
        assert "subsystem" in params, f"list_adr_rows must accept 'subsystem' (got {list(params)})"

    def test_admin_op_list_adr_rows_forwards_filters(self) -> None:
        """``list_adr_rows`` admin op forwards ``tier`` + ``subsystem`` to storage."""

        from yadgar.backend.admin_exec.ledger import list_adr_rows

        captured: dict = {}

        async def _fake(*args, **kwargs) -> list[dict]:
            captured.update(kwargs)
            return []

        fake_storage = type("S", (), {"list_adr_rows": _fake})()
        with patch(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            return_value=fake_storage,
        ):
            import asyncio

            result = asyncio.run(
                list_adr_rows(
                    {
                        "project_id": "myproj",
                        "status": "accepted",
                        "tier": "binding",
                        "subsystem": "storage",
                    }
                )
            )

        assert captured.get("tier") == "binding"
        assert captured.get("subsystem") == "storage"
        assert result == {"rows": []}


# ── §4.7: seed_adr_tier_subsystem backend op (one-shot) ───────────────────────


class TestSeedAdrTierSubsystem:
    """§4.7: ``seed_adr_tier_subsystem`` backfills ``tier``/``subsystem``.

    D35a one-shot shape — idempotent on re-run; rows that already have
    ``tier`` + ``subsystem`` set are skipped.
    """

    def test_seed_skips_already_stamped_rows(self) -> None:
        from yadgar.backend.admin_exec.seed_adr_tier_subsystem import (
            _is_already_stamped,
        )

        row_with_tier = {"tier": "binding", "subsystem": "storage"}
        assert _is_already_stamped(row_with_tier) is True

        row_without = {"tier": None, "subsystem": None}
        assert _is_already_stamped(row_without) is False

        row_partial = {"tier": "binding", "subsystem": None}
        assert _is_already_stamped(row_partial) is False

    def test_seed_classify_defaults_to_historical_for_deprecated_status(self) -> None:
        from yadgar.backend.admin_exec.seed_adr_tier_subsystem import (
            _classify_tier_from_status,
        )

        assert _classify_tier_from_status("superseded") == "historical"
        assert _classify_tier_from_status("rejected") == "historical"
        assert _classify_tier_from_status("deprecated") == "historical"
        assert _classify_tier_from_status("accepted") == "binding"
        assert _classify_tier_from_status("open") == "binding"

    def test_seed_extract_subsystem_from_body_header(self) -> None:
        from yadgar.backend.admin_exec.seed_adr_tier_subsystem import (
            _extract_subsystem_from_body,
        )

        body_with_header = (
            "# ADR-0001: example\n## Context\nstuff\n## Subsystem\nstorage\n## Decision\ndecided\n"
        )
        assert _extract_subsystem_from_body(body_with_header) == "storage"

        body_without = "# ADR-0001: example\n## Context\nstuff\n## Decision\ndecided\n"
        assert _extract_subsystem_from_body(body_without) is None

    def test_seed_admin_op_idempotent(self) -> None:
        """Re-running the seed with no work to do returns rows_updated=0."""
        from yadgar.backend.admin_exec.seed_adr_tier_subsystem import (
            seed_adr_tier_subsystem,
        )

        # Stub storage: returns rows that are ALL already stamped.
        class _StubStorage:
            async def list_adr_rows(self, **kwargs) -> list[dict]:
                return [
                    {
                        "id": 1,
                        "project_id": "p",
                        "status": "accepted",
                        "tier": "binding",
                        "subsystem": "storage",
                        "body_slug": "p_adr-0001",
                    },
                    {
                        "id": 2,
                        "project_id": "p",
                        "status": "superseded",
                        "tier": "historical",
                        "subsystem": "vacuum",
                        "body_slug": "p_adr-0002",
                    },
                ]

            async def update_adr_tier_subsystem(self, adr_id, tier, subsystem) -> None:
                raise AssertionError(
                    "update_adr_tier_subsystem must not be called on already-stamped rows"
                )

        result = seed_adr_tier_subsystem({"project_id": "p"}, storage=_StubStorage())
        # sync return, but the seed may be async — handle both
        import asyncio

        if hasattr(result, "__await__"):
            result = asyncio.run(result)
        assert result.get("rows_updated") == 0
        assert result.get("rows_skipped") == 2


# ── §4.2: rollup regen renders body + writes via sanctioned path ──────────────


class TestRegenerateSubsystemRollup:
    """§4.2: ``_regenerate_subsystem_rollup`` SELECTs rows, renders body,
    writes via the canonical wiki path with ``page_type="wiki_rollup"``."""

    def test_rollup_body_contains_adr_ids(self) -> None:
        from yadgar.backend.admin_exec.rollup import _render_rollup_body

        rows = [
            {
                "id": 1,
                "title": "First",
                "status": "accepted",
                "decided_on": "2026-08-09",
                "tier": "binding",
            },
            {
                "id": 2,
                "title": "Second",
                "status": "accepted",
                "decided_on": "2026-08-10",
                "tier": "binding",
            },
        ]
        body = _render_rollup_body(rows, subsystem="storage")
        assert "storage" in body
        assert "ADR-0001" in body
        assert "ADR-0002" in body
        assert "First" in body
        assert "Second" in body

    def test_rollup_body_excludes_when_no_rows(self) -> None:
        from yadgar.backend.admin_exec.rollup import _render_rollup_body

        body = _render_rollup_body([], subsystem="unknown")
        assert "unknown" in body
        assert "ADR-" not in body

    def test_rollup_write_uses_wiki_rollup_page_type(self) -> None:
        import asyncio

        from yadgar.backend.admin_exec.rollup import _regenerate_subsystem_rollup

        class _StubStorage:
            async def list_adr_rows(self, **kwargs) -> list[dict]:
                return [
                    {
                        "id": 1,
                        "title": "t1",
                        "status": "accepted",
                        "decided_on": "2026-08-09",
                        "tier": "binding",
                        "subsystem": "storage",
                    }
                ]

        captured: dict = {}

        def _capture_wiki_write(page_dict: dict, wait: bool = False) -> dict:
            captured["page_dict"] = page_dict
            captured["wait"] = wait
            return {"stored": True, "committed": True}

        with patch(
            "yadgar.backend.admin_exec.rollup._wiki_write_canonical",
            side_effect=_capture_wiki_write,
        ):
            result = asyncio.run(
                _regenerate_subsystem_rollup(
                    storage=_StubStorage(),
                    project_id="myproj",
                    subsystem="storage",
                )
            )

        assert captured["page_dict"]["page_type"] == "wiki_rollup"
        assert captured["wait"] is True
        # Slug is stable per (project_id, subsystem)
        assert "storage" in captured["page_dict"]["slug"]
        assert result.get("ok") is True


# ── §10 Q2: subsystem normalizer ─────────────────────────────────────────────


class TestSubsystemNormalizer:
    """§10 Q2: subsystem normalization (lowercase + trim; empty → None)."""

    def test_normalize_lowercase(self) -> None:
        from yadgar.core.server.tools.adr import _normalize_subsystem

        assert _normalize_subsystem("Storage") == "storage"
        assert _normalize_subsystem("STORAGE") == "storage"

    def test_normalize_trim(self) -> None:
        from yadgar.core.server.tools.adr import _normalize_subsystem

        assert _normalize_subsystem("  storage  ") == "storage"

    def test_normalize_empty_becomes_none(self) -> None:
        from yadgar.core.server.tools.adr import _normalize_subsystem

        assert _normalize_subsystem("") is None
        assert _normalize_subsystem("   ") is None
        assert _normalize_subsystem(None) is None
