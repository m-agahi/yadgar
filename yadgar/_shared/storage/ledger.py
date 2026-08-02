# SPDX-License-Identifier: Apache-2.0
"""_LedgerMixin — spine Car A.

The engine seam for all ledger tables (task, adr, agent_prompt, runtime_config).
D30: scalar columns only, identity/authorization/batching expressed as
capabilities. D20: every row access goes through this mixin; chokepoint
enforced by scripts/check_ledger_chokepoint.py.

D31: semantic number allocated by SELECT MAX(number)+1 ... FOR UPDATE inside
the same transaction as the INSERT, scoped to (project_id, origin). This is
the fix for §1.5's ADR index drift — the number and its row are one atomic
write.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from yadgar._shared.storage.alembic_models import ADR as ADRModel
from yadgar._shared.storage.alembic_models import Base, Task

logger = logging.getLogger(__name__)


class _LedgerMixin:
    """Spine engine seam — runtime_config + task + adr + agent_prompt.

    Mixes into StorageEngine alongside the SurrealDB _MigrationsMixin.
    Owns the MariaDB connection and exposes the four table roots.

    D6a: surrogate AUTO_INCREMENT PKs — app code never reads or sets them.
    D6b: semantic `number` column — allocated via _next_number() per D31.
    D8: uniqueness key is (project_id, origin, number) for all three tables.
    D31: SELECT MAX(number)+1 ... FOR UPDATE inside same transaction as INSERT.
    """

    _mariadb_url: str = ""
    _mariadb_engine: Engine | None = None

    def _init_ledger(self, mariadb_url: str) -> None:
        """Initialize the MariaDB engine. Called from StorageEngine.__init__."""
        self._mariadb_url = mariadb_url
        if not mariadb_url:
            logger.debug("ledger: no mariadb_url — spine disabled")
            return
        self._mariadb_engine = create_engine(
            mariadb_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        logger.info("ledger: MariaDB engine initialized")

    def _next_number(self, table: str, project_id: str, origin: str) -> int:
        """D31 — allocate the next semantic number for (project_id, origin, table).

        Uses SELECT MAX(number)+1 ... FOR UPDATE inside the transaction that
        performs the INSERT. The caller MUST already hold a transaction;
        this method does not begin one. Reaches into SQLAlchemy's connection
        directly because the row lock must span SELECT + INSERT atomically.

        For global-reach entities (agent_prompt), D31 says project_id is
        the literal sentinel 'global' — the caller passes it explicitly.
        """
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        with self._mariadb_engine.begin() as conn:
            row = conn.execute(
                text(
                    f"SELECT COALESCE(MAX(number), 0) + 1 AS next_num FROM {table} "
                    "WHERE project_id = :pid AND origin = :org FOR UPDATE"
                ),
                {"pid": project_id, "org": origin},
            ).one()
            return int(row.next_num)

    def _ledger_table(self, name: str) -> Any:
        """Return the SQLAlchemy table object for a ledger table."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        return Base.metadata.tables[f"{name}"]

    def _ledger_healthcheck(self) -> bool:
        """Return True if MariaDB is reachable. Used by /health."""
        if self._mariadb_engine is None:
            return False
        try:
            with self._mariadb_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ── Task CRUD (Car D) ─────────────────────────────────────────────────────

    def allocate_task_number(self, *, project_id: str, origin: str) -> int:
        """D31 — allocate the next semantic task number for (project_id, origin)."""
        return self._next_number("task", project_id, origin)

    def create_task_row(
        self,
        *,
        project_id: str,
        origin: str,
        number: int,
        title: str,
        active_form: str | None = None,
        state: str = "open",
        plan_path: str | None = None,
        body_slug: str | None = None,
        directory: str | None = None,
    ) -> dict:
        """Create a task row. Caller must have allocated `number` via allocate_task_number."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = Task(
                    project_id=project_id,
                    origin=origin,
                    number=number,
                    title=title,
                    active_form=active_form,
                    state=state,
                    plan_path=plan_path,
                    body_slug=body_slug,
                )
                session.add(row)
                session.flush()
                return {
                    "id": row.id,
                    "project_id": row.project_id,
                    "origin": row.origin,
                    "number": row.number,
                    "title": row.title,
                    "status": row.status,
                    "state": row.state,
                    "body_slug": row.body_slug,
                    "active_form": row.active_form,
                    "plan_path": row.plan_path,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }

    def list_task_rows(
        self,
        *,
        project_id: str,
        status: list[str] | None = None,
        directory: str | None = None,
    ) -> list[dict]:
        """List task rows for a project. If status is given, filter to those statuses."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            q = session.query(Task).filter(Task.project_id == project_id)
            if status:
                q = q.filter(Task.status.in_(status))
            rows = q.order_by(Task.number).all()
            return [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "origin": r.origin,
                    "number": r.number,
                    "title": r.title,
                    "status": r.status,
                    "state": r.state,
                }
                for r in rows
            ]

    def get_task_row(
        self,
        *,
        project_id: str,
        number: int,
        directory: str | None = None,
    ) -> dict:
        """Fetch one task row by (project_id, number). Returns {} if not found."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            row = (
                session.query(Task)
                .filter(Task.project_id == project_id, Task.number == number)
                .one_or_none()
            )
            if row is None:
                return {}
            return {
                "id": row.id,
                "project_id": row.project_id,
                "origin": row.origin,
                "number": row.number,
                "title": row.title,
                "status": row.status,
                "state": row.state,
                "body_slug": row.body_slug,
                "active_form": row.active_form,
                "plan_path": row.plan_path,
            }

    # ── ADR CRUD (Car F) ──────────────────────────────────────────────────────

    def allocate_adr_number(self, *, project_id: str, origin: str) -> int:
        """D31 — allocate the next semantic ADR number for (project_id, origin)."""
        return self._next_number("adr", project_id, origin)

    def create_adr_row(
        self,
        *,
        project_id: str,
        origin: str,
        number: int,
        title: str,
        status: str = "open",
        body_slug: str | None = None,
        date: str | None = None,
        subsystem: str | None = None,
        tier: str | None = None,
        supersedes: list[int] | None = None,
        superseded_by: list[int] | None = None,
    ) -> dict:
        """Create an ADR row. Caller must have allocated `number` via allocate_adr_number."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = ADRModel(
                    project_id=project_id,
                    origin=origin,
                    number=number,
                    title=title,
                    status=status,
                    body_slug=body_slug,
                    date=date,
                    subsystem=subsystem,
                    tier=tier,
                    supersedes=supersedes,
                    superseded_by=superseded_by,
                )
                session.add(row)
                session.flush()
                return {
                    "id": row.id,
                    "project_id": row.project_id,
                    "origin": row.origin,
                    "number": row.number,
                    "title": row.title,
                    "status": row.status,
                    "body_slug": row.body_slug,
                    "date": row.date,
                    "subsystem": row.subsystem,
                    "tier": row.tier,
                }

    def list_adr_rows(
        self,
        *,
        project_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List ADR rows for a project. Optional status filter."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            q = session.query(ADRModel).filter(ADRModel.project_id == project_id)
            if status:
                q = q.filter(ADRModel.status == status)
            rows = q.order_by(ADRModel.number).offset(offset).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "origin": r.origin,
                    "number": r.number,
                    "title": r.title,
                    "status": r.status,
                    "date": r.date,
                    "subsystem": r.subsystem,
                    "tier": r.tier,
                    "supersedes": r.supersedes,
                    "superseded_by": r.superseded_by,
                }
                for r in rows
            ]

    def get_adr_row(
        self,
        *,
        project_id: str,
        number: int,
    ) -> dict:
        """Fetch one ADR row by (project_id, number). Returns {} if not found."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            row = (
                session.query(ADRModel)
                .filter(ADRModel.project_id == project_id, ADRModel.number == number)
                .one_or_none()
            )
            if row is None:
                return {}
            return {
                "id": row.id,
                "project_id": row.project_id,
                "origin": row.origin,
                "number": row.number,
                "title": row.title,
                "status": row.status,
                "body_slug": row.body_slug,
                "date": row.date,
                "subsystem": row.subsystem,
                "tier": row.tier,
                "supersedes": row.supersedes,
                "superseded_by": row.superseded_by,
            }
