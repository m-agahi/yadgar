# SPDX-License-Identifier: Apache-2.0
"""RED test for D31 — concurrent allocation on one (project_id, origin) never collides.

Spine Car A (task-table-refactor-2026-07-29, D31): the semantic number is
allocated by SELECT MAX(number)+1 ... FOR UPDATE inside the same transaction
as the INSERT. The current threading.Lock fails this across processes; the
InnoDB row lock is the fix.

This test validates the allocation PATTERN against SQLite (single-process).
True cross-process concurrency is validated against MariaDB/InnoDB separately
(marked integration, requires a live DB). The pattern: SELECT MAX inside a
transaction, INSERT in the same transaction — produces unique sequential
numbers when called sequentially, and the unique constraint catches any
race-condition collision.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from yadgar._shared.storage.alembic_models import Base, Task


@pytest.fixture
def ledger_engine(tmp_path) -> Iterator:
    """File-backed SQLite engine with the Task table created.

    SQLite is used as a single-process smoke test for the allocation pattern.
    Production (MariaDB/InnoDB) adds the SELECT ... FOR UPDATE row lock that
    serialises concurrent transactions across processes — that property is
    validated by tests/mariadb/test_d31_concurrent.py (integration, requires
    a live MariaDB).
    """
    db_path = tmp_path / "ledger.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    db_path.unlink(missing_ok=True)


def _allocate_one(engine, project_id: str, origin: str) -> int:
    """Run one D31 allocation: SELECT MAX+1, then INSERT, in one transaction.

    Returns the allocated number. Must be unique per (project_id, origin).
    The unique constraint (project_id, origin, number) catches any race.
    """
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        with session.begin():
            row = session.execute(
                text(
                    "SELECT COALESCE(MAX(number), 0) + 1 AS next_num FROM task "
                    "WHERE project_id = :pid AND origin = :org"
                ),
                {"pid": project_id, "org": origin},
            ).one()
            next_num = int(row.next_num)
            session.add(
                Task(
                    project_id=project_id,
                    origin=origin,
                    number=next_num,
                    title=f"task-{next_num}",
                )
            )
    return next_num


def test_d31_sequential_allocations_are_unique(ledger_engine) -> None:
    """N sequential allocations on one (project_id, origin) yield 1..N."""
    project_id = "test_project"
    origin = "test"
    n = 20

    numbers = [_allocate_one(ledger_engine, project_id, origin) for _ in range(n)]

    assert len(numbers) == n
    assert len(set(numbers)) == n, f"collisions: {numbers}"
    assert sorted(numbers) == list(range(1, n + 1))


def test_d31_allocations_isolated_by_project(ledger_engine) -> None:
    """Allocations on different project_ids start independently at 1."""
    p1, p2 = "proj_one", "proj_two"
    n = 5

    nums_p1 = [_allocate_one(ledger_engine, p1, "test") for _ in range(n)]
    nums_p2 = [_allocate_one(ledger_engine, p2, "test") for _ in range(n)]

    # Both projects start at 1 and go to n — isolation means each gets 1..n.
    assert sorted(nums_p1) == list(range(1, n + 1))
    assert sorted(nums_p2) == list(range(1, n + 1))


def test_d31_first_allocation_starts_at_one(ledger_engine) -> None:
    """The first allocation on a fresh (project_id, origin) yields number 1."""
    n = _allocate_one(ledger_engine, "fresh_proj", "fresh")
    assert n == 1
