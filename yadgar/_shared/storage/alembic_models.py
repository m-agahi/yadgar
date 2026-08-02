# SPDX-License-Identifier: Apache-2.0
"""SQLAlchemy declarative base + spine table models.

Spine tables (task-table-refactor-2026-07-29, §3 Schema):
  - runtime_config (moved from SurrealDB, task #0119)
  - task
  - adr
  - agent_prompt

All tables live in MariaDB. Bodies stay as wiki pages in SurrealDB (D4).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all spine MariaDB tables."""


class RuntimeConfig(Base):
    """Runtime config store — moved from SurrealDB to MariaDB (task #0119).

    Scoped by absolute filesystem path (D32 ②). The lookup key is the path,
    not project_id, so a config read can happen before project_id is derived
    (D33 cycle resolution).
    """

    __tablename__ = "runtime_config"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    directory: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("key", "directory", name="runtime_config_key_dir_idx"),)


class Task(Base):
    """Task ledger row.

    identity: (project_id, origin, number) — D8
    semantic number: allocated via MAX(number)+1 FOR UPDATE scoped to
    (project_id, origin) — D31
    surrogate PK: engine-native AUTO_INCREMENT, never touched by app code — D6a
    """

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reach: Mapped[str] = mapped_column(String(16), nullable=False, default="project")
    title: Mapped[str] = mapped_column(String(200), nullable=False)  # D12: cap 200
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")  # D36
    body_slug: Mapped[str] = mapped_column(String(255), nullable=True)  # D4
    active_form: Mapped[str | None] = mapped_column(String(200), nullable=True)
    plan_path: Mapped[str | None] = mapped_column(String(512), nullable=True)  # D36
    blocked_by: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)  # D39
    blocks: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)  # D39
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    modified_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "origin", "number", name="task_pk_identity"),
        Index("task_project_status_idx", "project_id", "status"),
    )


class ADR(Base):
    """ADR ledger row."""

    __tablename__ = "adr"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reach: Mapped[str] = mapped_column(String(16), nullable=False, default="project")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    body_slug: Mapped[str] = mapped_column(String(255), nullable=False)  # D4: mandatory
    date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subsystem: Mapped[str | None] = mapped_column(String(128), nullable=True)  # D28
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True)  # D27
    supersedes: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    superseded_by: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    modified_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "origin", "number", name="adr_pk_identity"),
        Index("adr_project_status_idx", "project_id", "status"),
    )


class AgentPrompt(Base):
    """Agent-prompt ledger row. Reach always global — D3."""

    __tablename__ = "agent_prompt"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, default="global")
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)  # D3: assigned + ignored
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reach: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    body_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # pattern|discipline|contract
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    composes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # D40
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    modified_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "origin", "number", name="agent_prompt_pk_identity"),
    )
