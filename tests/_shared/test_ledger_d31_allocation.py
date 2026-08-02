# SPDX-License-Identifier: Apache-2.0
"""Tests for spine ledger id allocation — id is AUTO_INCREMENT PK.

Spine Car A (task-table-refactor-2026-07-29): id is the AUTO_INCREMENT PK
and also the semantic number. No separate number column — INSERT returns
the generated id. Sequential INSERTs produce sequential ids.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from yadgar._shared.storage.alembic_models import Base, Task


@pytest.fixture
def ledger_engine(tmp_path) -> Iterator:
    """File-backed SQLite engine with the Task table created."""
    db_path = tmp_path / "ledger.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    db_path.unlink(missing_ok=True)


def _insert_one(engine, project_id: str, origin: str) -> int:
    """Insert one task row. Returns the generated id (AUTO_INCREMENT)."""
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        with session.begin():
            row = Task(
                project_id=project_id,
                origin=origin,
                title=f"task-{project_id}",
            )
            session.add(row)
            session.flush()
            return row.id


def test_sequential_inserts_produce_sequential_ids(ledger_engine) -> None:
    """N sequential INSERTs on one (project_id, origin) yield sequential ids."""
    project_id = "test_project"
    origin = "test"
    n = 20

    ids = [_insert_one(ledger_engine, project_id, origin) for _ in range(n)]

    assert len(ids) == n
    assert len(set(ids)) == n, f"collisions: {ids}"
    assert sorted(ids) == list(range(1, n + 1))


def test_ids_are_global_not_per_project(ledger_engine) -> None:
    """INSERTs across different project_ids share the same AUTO_INCREMENT series."""
    p1, p2 = "proj_one", "proj_two"
    n = 5

    ids_p1 = [_insert_one(ledger_engine, p1, "test") for _ in range(n)]
    ids_p2 = [_insert_one(ledger_engine, p2, "test") for _ in range(n)]

    assert sorted(ids_p1) == list(range(1, n + 1))
    assert sorted(ids_p2) == list(range(n + 1, 2 * n + 1))


def test_first_insert_starts_at_one(ledger_engine) -> None:
    """The first INSERT on a fresh table yields id 1."""
    n = _insert_one(ledger_engine, "fresh_proj", "fresh")
    assert n == 1
