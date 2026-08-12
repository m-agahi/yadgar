"""Car K (0047 §7 row K) — nightly archive sweep, policy-dispatched.

Sweep corpus (per project_id for task/adr; global for agent_pattern /
agent_discipline):
  - task rows past TASK_ARCHIVE_RETENTION_DAYS — flip status='archived',
    clear state=NULL, retype body wiki page to task_archived.
  - adr rows past ADR_ARCHIVE_RETENTION_DAYS in
    ('superseded','rejected','deprecated') — flip status='archived', retype
    body wiki page to adr_archived.
  - agent_pattern rows with uses=0 AND status='deprecated' — retype body to
    agent_pattern_archived.
  - agent_discipline rows with uses=0 AND status='deprecated' AND
    always_applied=FALSE — retype body to agent_discipline_archived.

PER-PAGE OPT-OUT: a body wiki page whose ``mutability_override`` is set to
``"locked"`` or ``"derived"`` is SKIPPED (operator pinned it). The sweep is
sanctioned (``_sanctioned=True``) so per-type defaults do NOT trip the gate —
the override is the explicit opt-out.

AGE FIELDS (C15a — the Car K deviation is RETIRED):
  Car K aged task rows off ``updated_at`` and ADR rows off ``created_at``,
  because the ``task``/``adr`` tables carried neither a ``completed_at`` nor
  a ``superseded_at`` column and K read the project no-migration rule as
  forbidding one. C15a (0047 remediation §5.C15a items 1+2) establishes that
  ``002_ledger_tables`` is unreleased and CREATES both tables itself, so the
  columns were added in place. The sweep now ages off:

  - task: ``completed_at``. ``updated_at`` bumps on every edit, so under K
    editing a completed task reset its 90-day clock — the exact regression
    §14.2 named. ``TestTaskArchivePolicy`` pins BOTH directions: an old
    ``completed_at`` with a fresh ``updated_at`` still archives, and a fresh
    ``completed_at`` with an old ``updated_at`` does NOT.
  - adr: ``superseded_at``, falling back to ``created_at`` when unset. Under
    K an ADR created 120 days ago and superseded today archived on the very
    next sweep with ZERO grace. The fallback is load-bearing, not tidiness:
    ``rejected`` / ``deprecated`` rows are swept too and are never stamped
    with a ``superseded_at``, so without it two of the three sweep statuses
    would silently stop archiving altogether. ``TestAdrArchivePolicy`` pins
    all three cases.
  - agent_pattern / agent_discipline rows use ``updated_at`` (unchanged —
    those tables carry no other timestamp).

ORDERING (§4.1): body page retype FIRST, then ledger row flip. A mid-sweep
crash leaves a retyped page + stale row — detectable by
``invariants_cross_engine.py``'s content_hash check. The inverse order
(flip-then-retype) is NOT used.

IDEMPOTENT: re-running on already-archived rows is a no-op.

CIRCUIT-BREAKER: if the candidate count exceeds
``LEDGER_ARCHIVE_CIRCUIT_BREAKER`` (default 500), the sweep aborts and
archives nothing — mirrors ``MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER``.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from yadgar.backend import admin_exec
from yadgar.backend.admin_exec import nightly_sweep
from yadgar.backend.admin_exec.nightly_sweep import run_nightly_archive_sweep

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ago(days: float) -> str:
    """ISO timestamp ``days`` ago (UTC). Mirrors archive_retention tests."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


class _FakeSqlStorage:
    """In-memory double for ``MariaStorageEngine``.

    Honours the slice the sweep uses: ``list_task_rows`` / ``list_adr_rows`` /
    ``list_agent_prompt_rows`` (treated as the global patterns+disciplines
    list, see below) / ``update_task_row`` / ``_flip_adr_status``.
    Records every call so assertions can verify call ordering.

    The ledger chokepoint exposes patterns via ``list_agent_prompt_rows`` (the
    existing method — patterns + disciplines are both on agent_prompt-ish
    tables per Car I). For sweep purposes the existing surface is too coarse
    (we need separate list methods for patterns and disciplines, plus a
    ``always_applied`` flag the agent_prompt_rows reader doesn't return).
    The sweep is therefore allowed to call *new* helper methods when they
    exist on the engine and fall back to deriving discipline info from
    ``list_agent_prompt_rows`` + a separate ``list_agent_discipline_rows``
    method when needed. Tests patch the engine directly with the methods
    the sweep is allowed to call — the engine method whitelist is tested in
    Car I (not K's scope).
    """

    def __init__(
        self,
        *,
        task_rows: list[dict] | None = None,
        task_rows_all_projects: list[dict] | None = None,
        adr_rows: list[dict] | None = None,
        agent_pattern_rows: list[dict] | None = None,
        agent_discipline_rows: list[dict] | None = None,
    ) -> None:
        self._task_rows = list(task_rows or [])
        self._task_rows_all_projects = list(task_rows_all_projects or [])
        self._adr_rows = list(adr_rows or [])
        self._agent_pattern_rows = list(agent_pattern_rows or [])
        self._agent_discipline_rows = list(agent_discipline_rows or [])

        # Recorded operations
        self.task_updates: list[tuple[int, dict]] = []
        self.adr_flips: list[tuple[int, str]] = []
        # (engine method name, args)
        self.calls: list[tuple[str, tuple]] = []

    # ── task ──

    async def list_task_rows(
        self, *, project_id: str, status: list[str] | str | None = None
    ) -> list[dict]:
        self.calls.append(("list_task_rows", (project_id, status)))
        # Fall back to ``_task_rows_all_projects`` when the per-project
        # bucket is empty — the sweep's ``list_task_rows_all_projects``
        # pass populates that bucket in the no-project_id branch.
        bucket = self._task_rows or self._task_rows_all_projects
        rows = [r for r in bucket if r["project_id"] == project_id]
        if status is not None:
            wanted = {status} if isinstance(status, str) else set(status)
            rows = [r for r in rows if r["status"] in wanted]
        return rows

    async def list_task_rows_all_projects(
        self, *, status: list[str] | str | None = None
    ) -> list[dict]:
        self.calls.append(("list_task_rows_all_projects", (status,)))
        rows = list(self._task_rows_all_projects or self._task_rows)
        if status is not None:
            wanted = {status} if isinstance(status, str) else set(status)
            rows = [r for r in rows if r["status"] in wanted]
        return rows

    async def update_task_row(self, task_id: int, **fields: Any) -> None:
        self.task_updates.append((task_id, dict(fields)))
        for r in self._task_rows:
            if r["id"] == task_id:
                r.update(fields)
                return

    # ── adr ──

    async def list_adr_rows(
        self, *, project_id: str, status: list[str] | str | None = None
    ) -> list[dict]:
        self.calls.append(("list_adr_rows", (project_id, status)))
        rows = [r for r in self._adr_rows if r["project_id"] == project_id]
        if status is not None:
            wanted = {status} if isinstance(status, str) else set(status)
            rows = [r for r in rows if r["status"] in wanted]
        return rows

    async def _flip_adr_status(self, adr_id: int, status: str) -> None:
        self.adr_flips.append((adr_id, status))
        for r in self._adr_rows:
            if r["id"] == adr_id:
                r["status"] = status
                return

    # ── agent_pattern / agent_discipline (global) ──

    async def list_agent_prompt_rows(self) -> list[dict]:
        return list(self._agent_pattern_rows)

    async def list_agent_discipline_rows(self) -> list[dict]:
        return list(self._agent_discipline_rows)


class _FakeSurrealStorage:
    """In-memory double for ``StorageEngine`` (SurrealDB).

    Mirrors the slice the sweep uses: ``get_wiki_page`` (by id) and
    ``update_wiki_page`` (which honours ``_sanctioned``). The sweep reads
    body pages by id (``update_task_row`` returns the id, and the
    ``body_slug`` on the ledger row maps to a wiki page via
    ``get_wiki_page_by_slug``).

    We provide both ``get_wiki_page`` and ``get_wiki_page_by_slug`` because
    the sweep prefers the slug lookup (the ledger ``body_slug`` is the
    canonical pointer per D4). The id lookup is a fallback for tests that
    don't set ``body_slug``.
    """

    def __init__(self, *, pages: list[dict] | None = None) -> None:
        self._pages: list[dict] = [dict(p) for p in (pages or [])]
        self.updates: list[tuple[int, dict, bool]] = []
        # Lookup by slug counter — used by the sweep to detect cross-page calls.
        self.slug_lookups = 0
        self.id_lookups = 0

    def get_wiki_page(self, page_id: int) -> dict | None:
        self.id_lookups += 1
        for p in self._pages:
            if p.get("id") == page_id:
                return dict(p)
        return None

    def get_wiki_page_by_slug(self, slug: str) -> dict | None:
        self.slug_lookups += 1
        for p in self._pages:
            if p.get("slug") == slug:
                return dict(p)
        return None

    def update_wiki_page(self, page_id: int, updates: dict, _sanctioned: bool = False) -> bool:
        self.updates.append((page_id, dict(updates), _sanctioned))
        for p in self._pages:
            if p.get("id") == page_id:
                p.update(updates)
                return True
        return False


def _task_row(
    *,
    id_: int = 1,
    project_id: str = "p/r",
    status: str = "completed",
    body_slug: str | None = None,
    updated_at: str | None = None,
    completed_at: str | None = None,
) -> dict:
    if body_slug is None:
        # Default to the test-flavoured canonical slug (the same shape
        # Car L's reslug writes: ``{project_id_safe}_task-{id}``).
        project_id_safe = project_id.replace("/", "_")
        body_slug = f"{project_id_safe}_task-{id_}"
    resolved_updated = updated_at if updated_at is not None else _ago(100)
    return {
        "id": id_,
        "project_id": project_id,
        "title": f"task-{id_}",
        "status": status,
        "state": "closed",
        "active_form": None,
        "plan_path": None,
        "body_slug": body_slug,
        "created_at": _ago(200),
        "updated_at": resolved_updated,
        # C15a: the sweep ages off ``completed_at``. A caller that says only
        # "this row is old" through ``updated_at`` means the task completed
        # then too, so the default MIRRORS it and every pre-C15a caller keeps
        # its original meaning. The tests that actually pin the age field pass
        # BOTH values explicitly and so do not depend on this default.
        "completed_at": completed_at if completed_at is not None else resolved_updated,
    }


def _adr_row(
    *,
    id_: int = 1,
    project_id: str = "p/r",
    status: str = "superseded",
    body_slug: str | None = None,
    created_at: str | None = None,
    superseded_at: str | None = None,
) -> dict:
    if body_slug is None:
        project_id_safe = project_id.replace("/", "_")
        body_slug = f"{project_id_safe}_adr-{id_}"
    resolved_created = created_at if created_at is not None else _ago(100)
    return {
        "id": id_,
        "project_id": project_id,
        "title": f"adr-{id_}",
        "status": status,
        "decided_on": None,
        "subsystem": None,
        "tier": None,
        "body_slug": body_slug,
        "created_at": resolved_created,
        "updated_at": _ago(50),
        # C15a: mirrors ``created_at`` by default for the same reason
        # ``_task_row.completed_at`` mirrors ``updated_at`` — a caller that
        # says only "this ADR is old" keeps meaning that. Set it to ``None``
        # explicitly to exercise the created_at FALLBACK path.
        "superseded_at": superseded_at if superseded_at is not None else resolved_created,
    }


def _agent_pattern_row(
    *,
    name: str = "pat",
    status: str = "deprecated",
    uses: int = 0,
    body_slug: str | None = None,
) -> dict:
    if body_slug is None:
        body_slug = f"p_r_agent_pattern-{name}"
    return {
        "id": hash(name) & 0xFFFF,
        "name": name,
        "body_slug": body_slug,
        "purpose": "test pattern",
        "status": status,
        "baseline_hash": None,
        "content_hash": "h",
        "uses": uses,
        "created_at": _ago(200),
        "updated_at": _ago(100),
    }


def _agent_discipline_row(
    *,
    name: str = "disc",
    status: str = "deprecated",
    uses: int = 0,
    always_applied: bool = False,
    body_slug: str | None = None,
) -> dict:
    if body_slug is None:
        body_slug = f"p_r_agent_discipline-{name}"
    return {
        "id": hash(name) & 0xFFFF,
        "name": name,
        "body_slug": body_slug,
        "purpose": "test discipline",
        "always_applied": always_applied,
        "position": 0,
        "status": status,
        "baseline_hash": None,
        "content_hash": "h",
        "created_at": _ago(200),
        "updated_at": _ago(100),
    }


def _body_page(
    *,
    id_: int = 10,
    slug: str | None = None,
    page_type: str = "task",
    mutability_override: str | None = None,
) -> dict:
    if slug is None:
        slug = f"p_r_task-{id_}"
    return {
        "id": id_,
        "slug": slug,
        "title": "Body",
        "content": "body",
        "page_type": page_type,
        "mutability_override": mutability_override,
        "directory_context": "/tmp/k-sweep",
    }


def _patch_sql(monkeypatch: pytest.MonkeyPatch, sql: _FakeSqlStorage) -> None:
    """Patch the SQL storage seam in nightly_sweep."""
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.nightly_sweep._get_sql_storage",
        lambda: sql,
    )


def _patch_surreal(monkeypatch: pytest.MonkeyPatch, surreal: _FakeSurrealStorage) -> None:
    """Patch the SurrealDB storage seam in nightly_sweep."""
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.nightly_sweep._get_storage",
        lambda: surreal,
    )


# ---------------------------------------------------------------------------
# 1. Registration in _ADMIN_OPS
# ---------------------------------------------------------------------------


class TestSweepRegistered:
    def test_run_nightly_archive_sweep_is_in_admin_ops(self) -> None:
        """The sweep is dispatched as a backend admin op (wiring option b)."""
        assert "run_nightly_archive_sweep" in admin_exec._ADMIN_OPS
        assert admin_exec._ADMIN_OPS["run_nightly_archive_sweep"] is run_nightly_archive_sweep

    async def test_run_nightly_archive_sweep_is_async(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The op is async (asyncmy is async-only); dispatch routes via run_admin_op_async."""
        # No real DB call — verify registration + async-ness shape.
        _patch_sql(monkeypatch, _FakeSqlStorage())
        _patch_surreal(monkeypatch, _FakeSurrealStorage())
        result = await admin_exec.run_admin_op_async(
            "run_nightly_archive_sweep",
            {"retention_days": 90},
        )
        assert isinstance(result, dict)
        assert "archived_tasks" in result


# ---------------------------------------------------------------------------
# 2. Task archive policy
# ---------------------------------------------------------------------------


class TestTaskArchivePolicy:
    async def test_completed_task_past_retention_is_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task past TASK_ARCHIVE_RETENTION_DAYS is flipped + body retyped.

        C15a rewrite of the Car K pin. ``updated_at`` is deliberately FRESH
        (1 day) while ``completed_at`` is old (120 days): the row completed
        long ago and was merely edited yesterday, which §14.2 says must not
        reset the retention clock. Under Car K's ``updated_at`` policy this
        row survived the sweep; it must now archive.
        """
        task = _task_row(
            id_=42,
            body_slug="p_r_task-42",
            completed_at=_ago(120),
            updated_at=_ago(1),
        )
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=100, slug="p_r_task-42", page_type="task")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 1
        assert result["projects_swept"] == ["p/r"]
        # Row flipped
        assert sql.task_updates == [(42, {"status": "archived", "state": None})]
        # Body retyped FIRST (page update recorded before row update in the
        # sweep; we only verify both happened, not the wall-clock ordering —
        # the ordering test is in class 7 below).
        assert surreal.updates  # page retype happened

    async def test_archive_sweep_ages_off_completed_at_not_updated_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A freshly-completed task with an OLD ``updated_at`` is NOT archived.

        The RED the Car K plan promised at its ``:151`` and never wrote. This
        is the whole point of the ``completed_at`` column: without this
        assertion the column is decorative, because every other task test
        would still pass with the sweep reading ``updated_at``.

        The row completed 10 days ago (well inside the 90-day window) but
        carries an ``updated_at`` of 100 days — the shape a row acquires when
        it is created and edited long before it is finally completed. Aging
        off ``updated_at`` archives it; aging off ``completed_at`` does not.
        """
        task = _task_row(
            id_=43,
            body_slug="p_r_task-43",
            completed_at=_ago(10),
            updated_at=_ago(100),
        )
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=430, slug="p_r_task-43", page_type="task")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 0, (
            "the sweep archived a task completed 10 days ago — it is aging off "
            "updated_at, not completed_at"
        )
        assert sql.task_updates == []
        assert surreal.updates == []

    async def test_task_with_null_completed_at_is_never_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``completed`` row with no ``completed_at`` stamp is left alone.

        NULL is not "infinitely old". A row whose completion clock never
        started cannot have run out, and archiving on a missing timestamp
        would make an unstamped corpus sweep itself away on the first run.
        """
        task = _task_row(id_=44, body_slug="p_r_task-44", updated_at=_ago(365))
        task["completed_at"] = None
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=440, slug="p_r_task-44", page_type="task")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 0
        assert sql.task_updates == []

    async def test_completed_task_within_retention_is_not_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task with completed_at inside the retention window stays put."""
        task = _task_row(id_=7, updated_at=_ago(10))
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage()
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 0
        assert sql.task_updates == []
        assert surreal.updates == []

    async def test_open_task_is_never_archived(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An open task row is not sweep-eligible regardless of age."""
        task = _task_row(id_=8, status="open", updated_at=_ago(365))
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage()
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 0
        assert sql.task_updates == []


# ---------------------------------------------------------------------------
# 3. Immutability skip (locked + derived)
# ---------------------------------------------------------------------------


class TestImmutabilitySkip:
    async def test_locked_page_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A body page with ``mutability_override='locked'`` is skipped."""
        task = _task_row(id_=5, body_slug="p_r_task-5", updated_at=_ago(200))
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=50, slug="p_r_task-5", mutability_override="locked")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 0
        assert result["skipped_immutable"] == 1
        # Row untouched, page untouched.
        assert sql.task_updates == []
        assert surreal.updates == []

    async def test_derived_page_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A body page with ``mutability_override='derived'`` is skipped."""
        task = _task_row(id_=6, body_slug="p_r_task-6", updated_at=_ago(200))
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=60, slug="p_r_task-6", mutability_override="derived")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 0
        assert result["skipped_immutable"] == 1

    async def test_default_mutability_sweeps_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A body page with no override is swept (per-type default does not block)."""
        task = _task_row(id_=9, body_slug="p_r_task-9", updated_at=_ago(200))
        sql = _FakeSqlStorage(task_rows=[task])
        # mutability_override=None (operator didn't pin) — sweep proceeds.
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=90, slug="p_r_task-9", mutability_override=None)]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 1
        assert result["skipped_immutable"] == 0


# ---------------------------------------------------------------------------
# 4. ADR archive policy
# ---------------------------------------------------------------------------


class TestAdrArchivePolicy:
    async def test_superseded_adr_past_retention_is_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ADR superseded past retention is flipped + retyped.

        C15a rewrite of the Car K pin, analogous to the task pin above.
        ``created_at`` is deliberately FRESH (1 day) while ``superseded_at``
        is old (120 days) — an ADR written yesterday and superseded 120 days
        ago is not a real history, but it is the only arrangement that proves
        the sweep reads ``superseded_at`` rather than ``created_at``.
        """
        adr = _adr_row(
            id_=100,
            body_slug="p_r_adr-100",
            created_at=_ago(1),
            superseded_at=_ago(120),
        )
        sql = _FakeSqlStorage(adr_rows=[adr], task_rows_all_projects=[])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=110, slug="p_r_adr-100", page_type="adr")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {
                "retention_days": 90,
                "project_id": "p/r",
                "now": datetime.now(UTC).timestamp(),
            }
        )

        assert result["archived_adrs"] == 1
        # Status flipped to 'archived' via _flip_adr_status (the engine
        # private helper; the dispatch op wraps it — see admin_exec/ledger.py).
        assert sql.adr_flips == [(100, "archived")]
        # Body retyped
        assert surreal.updates

    async def test_recently_superseded_old_adr_gets_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OLD ADR superseded TODAY is not archived on the next sweep.

        C15a item 2. Under Car K's ``created_at`` policy an ADR written 120
        days ago and superseded today was archived on the very next nightly
        run — zero grace, and the retention window silently measured the
        wrong interval (age-since-authoring, not age-since-retirement).
        """
        adr = _adr_row(
            id_=102,
            body_slug="p_r_adr-102",
            created_at=_ago(120),
            superseded_at=_ago(1),
        )
        sql = _FakeSqlStorage(adr_rows=[adr], task_rows_all_projects=[])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=112, slug="p_r_adr-102", page_type="adr")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {
                "retention_days": 90,
                "project_id": "p/r",
                "now": datetime.now(UTC).timestamp(),
            }
        )

        assert result["archived_adrs"] == 0, (
            "an ADR superseded 1 day ago was archived — the sweep is aging off "
            "created_at, so supersession grants no grace period at all"
        )
        assert sql.adr_flips == []
        assert surreal.updates == []

    async def test_rejected_adr_without_superseded_at_ages_off_created_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``rejected`` ADR with no ``superseded_at`` still ages off ``created_at``.

        The fallback is load-bearing, not tidiness. Two of the three sweep
        statuses (``rejected``, ``deprecated``) are never stamped with a
        ``superseded_at`` — nothing supersedes them. A strict
        ``superseded_at``-only policy would silently stop archiving them
        forever while every other test in this class stayed green.
        """
        adr = _adr_row(
            id_=103,
            status="rejected",
            body_slug="p_r_adr-103",
            created_at=_ago(120),
        )
        adr["superseded_at"] = None
        sql = _FakeSqlStorage(adr_rows=[adr], task_rows_all_projects=[])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=113, slug="p_r_adr-103", page_type="adr")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {
                "retention_days": 90,
                "project_id": "p/r",
                "now": datetime.now(UTC).timestamp(),
            }
        )

        assert result["archived_adrs"] == 1
        assert sql.adr_flips == [(103, "archived")]

    async def test_accepted_adr_is_not_archived(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted ADR is never sweep-eligible (D7 — accepted stays open)."""
        adr = _adr_row(id_=101, status="accepted", created_at=_ago(200))
        sql = _FakeSqlStorage(adr_rows=[adr])
        surreal = _FakeSurrealStorage()
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_adrs"] == 0
        assert sql.adr_flips == []


# ---------------------------------------------------------------------------
# 5. Per-project scoping
# ---------------------------------------------------------------------------


class TestPerProjectScoping:
    async def test_sweep_only_touches_target_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sweep scoped to project_id='p/r' only touches p/r's rows."""
        task_a = _task_row(id_=1, project_id="a/repo", updated_at=_ago(200))
        task_b = _task_row(id_=2, project_id="p/r", updated_at=_ago(200))
        sql = _FakeSqlStorage(task_rows=[task_a, task_b])
        surreal = _FakeSurrealStorage(
            pages=[
                _body_page(id_=11, slug="a_repo_task-1"),
                _body_page(id_=12, slug="p_r_task-2"),
            ]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        # No project_id in payload → sweep iterates ALL projects. For
        # per-project gating the caller must supply project_id explicitly.
        # When passed, only that project's rows are touched.
        result = await run_nightly_archive_sweep(
            {
                "retention_days": 90,
                "project_id": "p/r",
                "now": datetime.now(UTC).timestamp(),
            }
        )

        assert result["archived_tasks"] == 1
        assert sql.task_updates == [(2, {"status": "archived", "state": None})]
        # Sweep did NOT touch the a/repo task.
        assert all(rid != 1 for rid, _ in sql.task_updates)

    async def test_sweep_no_project_iterates_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No project_id → iterate every distinct project in the ledger."""
        task_a = _task_row(id_=1, project_id="a/repo", updated_at=_ago(200))
        task_b = _task_row(id_=2, project_id="p/r", updated_at=_ago(200))
        sql = _FakeSqlStorage(
            task_rows_all_projects=[task_a, task_b],
        )
        surreal = _FakeSurrealStorage(
            pages=[
                _body_page(id_=11, slug="a_repo_task-1"),
                _body_page(id_=12, slug="p_r_task-2"),
            ]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_tasks"] == 2
        assert set(result["projects_swept"]) == {"a/repo", "p/r"}


# ---------------------------------------------------------------------------
# 6. agent_pattern archive
# ---------------------------------------------------------------------------


class TestAgentPatternArchive:
    async def test_deprecated_zero_use_pattern_is_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uses=0 + status='deprecated' → archived."""
        pat = _agent_pattern_row(name="pat-old", uses=0, status="deprecated")
        sql = _FakeSqlStorage(agent_pattern_rows=[pat])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=20, slug="p_r_agent_pattern-pat-old", page_type="agent_pattern")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_patterns"] == 1

    async def test_active_pattern_with_zero_uses_is_not_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status='active' rows are NOT archived even if uses=0."""
        pat = _agent_pattern_row(name="pat-active", uses=0, status="active")
        sql = _FakeSqlStorage(agent_pattern_rows=[pat])
        surreal = _FakeSurrealStorage()
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_patterns"] == 0


# ---------------------------------------------------------------------------
# 7. agent_discipline archive (incl. always_applied NEVER-archive)
# ---------------------------------------------------------------------------


class TestAgentDisciplineArchive:
    async def test_always_applied_discipline_is_never_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contract singleton (always_applied=TRUE) is NEVER archived."""
        disc = _agent_discipline_row(
            name="disc-contract", uses=0, status="deprecated", always_applied=True
        )
        sql = _FakeSqlStorage(agent_discipline_rows=[disc])
        surreal = _FakeSurrealStorage()
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_disciplines"] == 0
        assert result["skipped_contract"] == 1

    async def test_deprecated_zero_use_discipline_is_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uses=0 + status='deprecated' + always_applied=FALSE → archived."""
        disc = _agent_discipline_row(
            name="disc-old", uses=0, status="deprecated", always_applied=False
        )
        sql = _FakeSqlStorage(agent_discipline_rows=[disc])
        surreal = _FakeSurrealStorage(
            pages=[
                _body_page(
                    id_=30,
                    slug="p_r_agent_discipline-disc-old",
                    page_type="agent_discipline",
                )
            ]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )

        assert result["archived_disciplines"] == 1


# ---------------------------------------------------------------------------
# 8. Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_second_sweep_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Running the sweep twice archives nothing new on the second pass."""
        task = _task_row(id_=10, body_slug="p_r_task-10", updated_at=_ago(200))
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(
            pages=[_body_page(id_=100, slug="p_r_task-10", page_type="task")]
        )
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        first = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )
        assert first["archived_tasks"] == 1

        # Second pass — same task is now status='archived', NOT 'completed'.
        second = await run_nightly_archive_sweep(
            {"retention_days": 90, "now": datetime.now(UTC).timestamp()}
        )
        assert second["archived_tasks"] == 0


# ---------------------------------------------------------------------------
# 9. Default retention
# ---------------------------------------------------------------------------


class TestDefaultRetention:
    async def test_retention_days_defaults_to_90(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no retention_days in payload, the default is 90."""
        task = _task_row(id_=11, updated_at=_ago(91))
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(pages=[_body_page(id_=110, slug="p_r_task-11")])
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep({"now": datetime.now(UTC).timestamp()})

        assert result["archived_tasks"] == 1
        assert result["retention_days"] == 90

    async def test_now_defaults_to_current_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``now`` defaults to the current wall-clock time when absent."""
        task = _task_row(id_=12, updated_at=_ago(91))
        sql = _FakeSqlStorage(task_rows=[task])
        surreal = _FakeSurrealStorage(pages=[_body_page(id_=120, slug="p_r_task-12")])
        _patch_sql(monkeypatch, sql)
        _patch_surreal(monkeypatch, surreal)

        result = await run_nightly_archive_sweep({"retention_days": 90})

        assert result["archived_tasks"] == 1


# ---------------------------------------------------------------------------
# 10. Observability rationale honesty (C15a item 4 / ADR-0074)
# ---------------------------------------------------------------------------


#: A rationale that asserts something about its callees while naming none —
#: "each leaf already carries its own @observe span". Only these need the
#: function body walked; a rationale that names its callees is checked
#: against those names directly.
_GENERIC_PLURAL_CLAIM = re.compile(r"\b(each|every|both|all)\b|\btheir own\b", re.IGNORECASE)


def _observe_call(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Call | None:
    """Return the ``@observe(...)`` decorator call on *node*, if any."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            fn = dec.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "observe":
                return dec
    return None


def _observe_kwarg(call: ast.Call, key: str) -> ast.expr | None:
    """Return the value node for keyword *key* on an ``@observe(...)`` call."""
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return None


def _scan_span_claims(source: str) -> tuple[list[str], dict[str, bool], list[str]]:
    """Return ``(violations, span_map, exempt_names)`` for a module's source.

    ``span_map`` maps each module-level function name to whether it actually
    OPENS an ``@observe`` span: a bare ``@observe`` or one with ``span=True``
    does; ``exempt=`` (a pure passthrough — ``observe.py`` returns the
    undecorated function) and ``span=False`` do not.

    A violation is an ``exempt=`` rationale that uses the word "span" while
    some observed function it delegates to opens none. "Delegates to" is
    read two ways, because a false claim comes in both shapes:

      * the rationale NAMES the callee ("…their own @observe on
        ``_retype_body_page``"), which is checked always; or
      * the rationale claims it GENERICALLY and in the plural ("each leaf
        already carries its own @observe span"), naming nobody — then and
        only then are the callees resolved from the function body.

    The body arm is deliberately gated on that plural marker rather than run
    for every span-mentioning rationale. Ungated, it fires on a rationale
    that is TRUE: ``_resolve_projects`` is correctly exempt and calls
    ``_dedupe_projects`` (``span=False``), so the moment someone reworded
    its "carries the boundary metric" to "…span" the scan would report a
    defect that is not there — measured, not assumed. A generic plural claim
    is the one shape that asserts something about callees while naming none,
    which is exactly why it needs the body to be checkable at all.

    Only module-local names resolve; a dotted ``sql.foo`` lives on another
    module and is out of reach by construction. Callees carrying no
    ``@observe`` at all are out of scope — a bare one-line shorthand like
    ``_days`` is not what "leaf" means, and flagging it would bury the real
    finding in noise.

    This is the I33 blind spot the scan exists for: the coverage lint counts
    ``exempt=`` as satisfied and never reads the prose, so a rationale can
    point at spans that do not exist while it reports ``MISSING=0``.
    """
    tree = ast.parse(source)
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    span_map: dict[str, bool] = {}
    observed: set[str] = set()
    rationales: dict[str, str] = {}
    bodies: dict[str, ast.AST] = {}
    for node in functions:
        bodies[node.name] = node
        call = _observe_call(node)
        if call is None:
            span_map[node.name] = False
            continue
        observed.add(node.name)
        exempt = _observe_kwarg(call, "exempt")
        if exempt is not None:
            span_map[node.name] = False
            rationales[node.name] = ast.literal_eval(exempt)
            continue
        span_kw = _observe_kwarg(call, "span")
        span_map[node.name] = not (isinstance(span_kw, ast.Constant) and span_kw.value is False)

    violations: list[str] = []
    for name, text in sorted(rationales.items()):
        if "span" not in text.lower():
            continue
        named = {
            other
            for other in span_map
            if other != name and re.search(rf"(?<![\w.]){re.escape(other)}\b", text)
        }
        called: set[str] = set()
        if _GENERIC_PLURAL_CLAIM.search(text):
            called = {
                child.func.id
                for child in ast.walk(bodies[name])
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in observed
                and child.func.id != name
            }
        for other in sorted(named | called):
            if not span_map[other]:
                violations.append(
                    f"{name}'s exempt rationale claims a span on {other}, "
                    f"but {other} opens none (it is exempt or span=False)"
                )
    return violations, span_map, sorted(rationales)


class TestObservabilityRationaleHonesty:
    """C15a item 4 — an ``exempt=`` rationale may not name a span that is absent."""

    def test_no_exempt_rationale_claims_a_span_that_does_not_exist(self) -> None:
        """Every ``exempt=`` span claim in nightly_sweep.py resolves to a real span.

        Before C15a, ``_sweep_tasks_and_adrs`` was exempt "because each leaf
        already carries its own @observe span" while both leaves
        (``_sweep_project_tasks`` / ``_sweep_project_adrs``) were themselves
        exempt no-ops — zero spans anywhere below the boundary, and I33
        reporting MISSING=0 throughout. Per ADR-0074 those loop helpers now
        use ``span=False``, which keeps the metric and the lint sentinel
        while opening no per-item span.
        """
        source = Path(nightly_sweep.__file__).read_text(encoding="utf-8")
        violations, span_map, exempt_names = _scan_span_claims(source)

        # Guard against a vacuous pass: the scan must have parsed real
        # decorators, or an empty result would prove nothing at all.
        assert span_map, "the AST scan found no module-level functions"
        assert exempt_names, "the AST scan found no exempt= rationales to check"
        assert span_map.get("run_nightly_archive_sweep") is True, (
            "the boundary op should open a span; the scan is misreading decorators"
        )
        assert not violations, "\n".join(violations)

    def test_the_scan_reports_a_planted_false_span_claim(self) -> None:
        """The scan is not vacuous — a planted violation IS reported.

        Without this, the assertion above would keep passing if the matcher
        silently stopped matching (the exact shape of 031's dead idempotency
        filter, which never projected the column it filtered on).
        """
        planted = (
            "from yadgar._shared.observability.observe import observe\n"
            "\n"
            "@observe(tier='hot', span=False)\n"
            "def _leaf():\n"
            "    return 1\n"
            "\n"
            "@observe(exempt='dispatch only; _leaf carries its own @observe span')\n"
            "def _caller():\n"
            "    return _leaf()\n"
        )
        violations, _, _ = _scan_span_claims(planted)
        assert violations, "the scan failed to report a planted false span claim"
        assert "_leaf" in violations[0]

    def test_a_true_span_claim_naming_no_callee_is_not_flagged(self) -> None:
        """The scan does not fire on a rationale that is TRUE.

        ``_resolve_projects`` is correctly exempt and calls
        ``_dedupe_projects`` (``span=False``). Its rationale currently says
        "carries the boundary metric"; an earlier version of this scan walked
        the body of EVERY span-mentioning rationale, so merely rewording that
        to "span" — a plausible tidy-up — reported a defect that is not
        there. The body walk is now gated on a generic PLURAL claim ("each
        leaf…"), which is the only shape that asserts something about callees
        while naming none. A precise scan nobody has to work around beats a
        broad one the next car learns to ignore.
        """
        source = Path(nightly_sweep.__file__).read_text(encoding="utf-8")
        reworded = source.replace(
            "carries the boundary metric for the whole sweep",
            "carries the boundary span for the whole sweep",
        )
        assert reworded != source, (
            "the _resolve_projects rationale moved; re-point this probe at the "
            "current text rather than deleting the test"
        )
        violations, _, _ = _scan_span_claims(reworded)
        assert not violations, (
            "the scan flagged a rationale whose only sin is the word 'span': "
            + "\n".join(violations)
        )
