# SPDX-License-Identifier: Apache-2.0
"""_LedgerMixin — spine Car A.

The engine seam for all ledger tables (task, adr, agent_prompt, runtime_config).
D30: scalar columns only, identity/authorization/batching expressed as
capabilities. D20: every row access goes through this mixin; chokepoint
enforced by scripts/check_ledger_chokepoint.py.

id is the AUTO_INCREMENT PK and also the semantic number. No separate
number column — per-project numbering is not needed; global uniqueness
across all projects is sufficient. INSERT returns the generated id.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.alembic_models import ADR as ADRModel
from yadgar._shared.storage.alembic_models import AgentPrompt as AgentPromptModel
from yadgar._shared.storage.alembic_models import Base, RuntimeConfig, Task

logger = logging.getLogger(__name__)


class _LedgerMixin:
    """Spine engine seam — runtime_config + task + adr + agent_prompt.

    Mixes into StorageEngine alongside the SurrealDB _MigrationsMixin.
    Owns the MariaDB connection and exposes the four table roots.

    id is the AUTO_INCREMENT PK and also the semantic number — no separate
    number column. INSERT returns the generated id.
    """

    _mariadb_url: str = ""
    _mariadb_engine: Engine | None = None

    @observe(tier="stage", metric="ledger._init_ledger")
    def _init_ledger(self, mariadb_url: str) -> None:
        """Initialize the MariaDB engine and run Alembic migrations.

        Called from StorageEngine.__init__. Gated on self._db_url being set
        (server mode only, same as _run_migrations). Uses the same
        fcntl.flock on STATE_DIR/.migration.lock.
        """
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

        if not getattr(self, "_db_url", None):
            logger.debug("ledger: no _db_url — skipping Alembic (embedded mode)")
            return

        try:
            from alembic import command
            from alembic.config import Config as AlembicConfig

            alembic_cfg = AlembicConfig("alembic.ini")
            alembic_cfg.set_main_option("sqlalchemy.url", mariadb_url)
            command.upgrade(alembic_cfg, "head")
            logger.info("ledger: Alembic migrations applied")
        except Exception:
            logger.exception("ledger: Alembic migration failed")

    @observe(tier="stage", metric="ledger._ledger_table")
    def _ledger_table(self, name: str) -> Any:
        """Return the SQLAlchemy table object for a ledger table."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        return Base.metadata.tables[f"{name}"]

    @observe(tier="stage", metric="ledger._ledger_healthcheck")
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

    # ── Runtime config CRUD (task #0119) ──────────────────────────────────────

    @observe(tier="stage", metric="ledger.set_config_row")
    def set_config_row(self, key: str, value: str, *, directory: str | None = None) -> dict:
        """Upsert a runtime_config row in MariaDB."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = (
                    session.query(RuntimeConfig)
                    .filter(
                        RuntimeConfig.key == key,
                        RuntimeConfig.directory == directory,
                    )
                    .one_or_none()
                )
                if row is not None:
                    row.value = value
                else:
                    row = RuntimeConfig(key=key, directory=directory, value=value)
                    session.add(row)
                session.flush()
                return {"key": row.key, "directory": row.directory, "value": row.value}

    @observe(tier="stage", metric="ledger.delete_config_row")
    def delete_config_row(self, key: str, *, directory: str | None = None) -> None:
        """Delete a runtime_config row (idempotent)."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = (
                    session.query(RuntimeConfig)
                    .filter(
                        RuntimeConfig.key == key,
                        RuntimeConfig.directory == directory,
                    )
                    .one_or_none()
                )
                if row is not None:
                    session.delete(row)

    # ── Task CRUD (Car D) ─────────────────────────────────────────────────────

    @observe(tier="stage", metric="ledger.create_task_row")
    def create_task_row(
        self,
        *,
        project_id: str,
        origin: str,
        title: str,
        active_form: str | None = None,
        state: str = "open",
        plan_path: str | None = None,
        body_slug: str | None = None,
        directory: str | None = None,
    ) -> dict:
        """Create a task row. id is the AUTO_INCREMENT number."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = Task(
                    project_id=project_id,
                    origin=origin,
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
                    "title": row.title,
                    "status": row.status,
                    "state": row.state,
                    "body_slug": row.body_slug,
                    "active_form": row.active_form,
                    "plan_path": row.plan_path,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }

    @observe(tier="stage", metric="ledger.list_task_rows")
    def list_task_rows(
        self,
        *,
        project_id: str,
        status: list[str] | None = None,
        directory: str | None = None,
    ) -> list[dict]:
        """List task rows for a project. If status is given, filter to those statuses.

        Use `list_task_rows_all_projects` for cross-project sweeps (Car K).
        """
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            q = session.query(Task).filter(Task.project_id == project_id)
            if status:
                q = q.filter(Task.status.in_(status))
            rows = q.order_by(Task.id).all()
            return [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "origin": r.origin,
                    "title": r.title,
                    "status": r.status,
                    "state": r.state,
                }
                for r in rows
            ]

    @observe(tier="stage", metric="ledger.list_task_rows_all_projects")
    def list_task_rows_all_projects(self, *, status: list[str] | None = None) -> list[dict]:
        """Car K — list task rows across ALL projects (for the archive sweep)."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            q = session.query(Task)
            if status:
                q = q.filter(Task.status.in_(status))
            rows = q.order_by(Task.project_id, Task.id).all()
            return [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "origin": r.origin,
                    "title": r.title,
                    "status": r.status,
                    "state": r.state,
                    "modified_at": r.modified_at.isoformat() if r.modified_at else None,
                    "body_slug": r.body_slug,
                }
                for r in rows
            ]

    @observe(tier="stage", metric="ledger.update_task_status")
    def update_task_status(self, *, project_id: str, number: int, status: str) -> bool:
        """Car K — flip a task's status (used by the archive sweep)."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = (
                    session.query(Task)
                    .filter(Task.project_id == project_id, Task.id == number)
                    .one_or_none()
                )
                if row is None:
                    return False
                row.status = status
                return True

    @observe(tier="stage", metric="ledger.get_task_row")
    def get_task_row(
        self,
        *,
        project_id: str,
        number: int,
        directory: str | None = None,
    ) -> dict:
        """Fetch one task row by (project_id, id). Returns {} if not found."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            row = (
                session.query(Task)
                .filter(Task.project_id == project_id, Task.id == number)
                .one_or_none()
            )
            if row is None:
                return {}
            return {
                "id": row.id,
                "project_id": row.project_id,
                "origin": row.origin,
                "title": row.title,
                "status": row.status,
                "state": row.state,
                "body_slug": row.body_slug,
                "active_form": row.active_form,
                "plan_path": row.plan_path,
            }

    # ── ADR CRUD (Car F) ──────────────────────────────────────────────────────

    @observe(tier="stage", metric="ledger.create_adr_row")
    def create_adr_row(
        self,
        *,
        project_id: str,
        origin: str,
        title: str,
        status: str = "open",
        body_slug: str | None = None,
        date: str | None = None,
        subsystem: str | None = None,
        tier: str | None = None,
        supersedes: list[int] | None = None,
        superseded_by: list[int] | None = None,
    ) -> dict:
        """Create an ADR row. id is the AUTO_INCREMENT number."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = ADRModel(
                    project_id=project_id,
                    origin=origin,
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
                    "title": row.title,
                    "status": row.status,
                    "body_slug": row.body_slug,
                    "date": row.date,
                    "subsystem": row.subsystem,
                    "tier": row.tier,
                }

    @observe(tier="stage", metric="ledger.list_adr_rows")
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
            rows = q.order_by(ADRModel.id).offset(offset).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "origin": r.origin,
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

    @observe(tier="stage", metric="ledger.get_adr_row")
    def get_adr_row(
        self,
        *,
        project_id: str,
        number: int,
    ) -> dict:
        """Fetch one ADR row by (project_id, id). Returns {} if not found."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            row = (
                session.query(ADRModel)
                .filter(ADRModel.project_id == project_id, ADRModel.id == number)
                .one_or_none()
            )
            if row is None:
                return {}
            return {
                "id": row.id,
                "project_id": row.project_id,
                "origin": row.origin,
                "title": row.title,
                "status": row.status,
                "body_slug": row.body_slug,
                "date": row.date,
                "subsystem": row.subsystem,
                "tier": row.tier,
                "supersedes": row.supersedes,
                "superseded_by": row.superseded_by,
            }

    @observe(tier="stage", metric="ledger.set_adr_body_slug")
    def set_adr_body_slug(self, *, project_id: str, number: int, body_slug: str) -> bool:
        """Set body_slug on an existing ADR row. Returns True if row found."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = (
                    session.query(ADRModel)
                    .filter(ADRModel.project_id == project_id, ADRModel.id == number)
                    .one_or_none()
                )
                if row is None:
                    return False
                row.body_slug = body_slug
                return True

    # ── Agent prompt CRUD (Car I) ─────────────────────────────────────────────

    @observe(tier="stage", metric="ledger.save_agent_prompt")
    def save_agent_prompt(
        self,
        *,
        origin: str,
        title: str,
        kind: str,
        purpose: str | None = None,
        body_slug: str | None = None,
        composes: list[str] | None = None,
    ) -> dict:
        """Create or update an agent_prompt row. Upsert by title (unique)."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                existing = (
                    session.query(AgentPromptModel)
                    .filter(AgentPromptModel.title == title)
                    .one_or_none()
                )
                if existing is not None:
                    existing.kind = kind
                    existing.purpose = purpose
                    existing.body_slug = body_slug
                    existing.composes = composes
                    session.flush()
                    return {
                        "id": existing.id,
                        "pattern": title,
                        "title": existing.title,
                        "kind": existing.kind,
                        "purpose": existing.purpose,
                        "uses": existing.uses,
                    }
                row = AgentPromptModel(
                    project_id="global",
                    origin=origin,
                    title=title,
                    kind=kind,
                    purpose=purpose,
                    body_slug=body_slug,
                    composes=composes,
                )
                session.add(row)
                session.flush()
                return {
                    "id": row.id,
                    "pattern": title,
                    "title": row.title,
                    "kind": row.kind,
                    "purpose": row.purpose,
                    "uses": row.uses,
                }

    @observe(tier="stage", metric="ledger.list_agent_prompt_rows")
    def list_agent_prompt_rows(self, *, status: str | None = None) -> list[dict]:
        """List agent_prompt rows. Default sort: uses DESC (D40 — surface popular first)."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            q = session.query(AgentPromptModel)
            if status:
                q = q.filter(AgentPromptModel.status == status)
            rows = q.order_by(AgentPromptModel.uses.desc()).all()
            return [
                {
                    "id": r.id,
                    "title": r.title,
                    "kind": r.kind,
                    "purpose": r.purpose,
                    "uses": r.uses,
                    "status": r.status,
                    "composes": r.composes,
                    "body_slug": r.body_slug,
                }
                for r in rows
            ]

    @observe(tier="stage", metric="ledger.get_agent_prompt_row")
    def get_agent_prompt_row(self, *, title: str) -> dict:
        """Fetch one agent_prompt row by title (unique). Returns {} if not found."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            row = (
                session.query(AgentPromptModel)
                .filter(AgentPromptModel.title == title)
                .one_or_none()
            )
            if row is None:
                return {}
            return {
                "id": row.id,
                "title": row.title,
                "kind": row.kind,
                "purpose": row.purpose,
                "uses": row.uses,
                "status": row.status,
                "composes": row.composes,
                "body_slug": row.body_slug,
            }

    @observe(tier="stage", metric="ledger.increment_agent_prompt_uses")
    def increment_agent_prompt_uses(self, *, title: str) -> int:
        """D40 — increment the uses counter for an agent_prompt by title. Returns new count."""
        if self._mariadb_engine is None:
            raise RuntimeError("ledger: MariaDB engine not initialized")
        SessionLocal = sessionmaker(bind=self._mariadb_engine)
        with SessionLocal() as session:
            with session.begin():
                row = (
                    session.query(AgentPromptModel)
                    .filter(AgentPromptModel.title == title)
                    .one_or_none()
                )
                if row is None:
                    return 0
                row.uses = (row.uses or 0) + 1
                return row.uses
