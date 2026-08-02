# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car B — ledger read cache.

Spine task-table-refactor-2026-07-29, Car B: the ledger read cache fronts
hot lookups (task_list, adr_list, agent_prompt_list) and invalidates on
write. Mirrors the pattern in ``core/cache/cache.py`` which already
caches project_brief / wiki_read / wiki_query / agent_prompt_prelude.

The cache key is (project_id, origin, query_kind) so different projects
don't collide. Invalidation is whole-flush per project on any write —
fine at single-digit writes per day per project.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def cache(monkeypatch) -> Iterator:
    """Fresh ledger cache for each test."""
    from yadgar.backend.cache.ledger_cache import LedgerCache

    c = LedgerCache(ttl_seconds=60)
    yield c


def test_cache_miss_returns_none(cache) -> None:
    """A key that was never set returns None."""
    result = cache.get_adr_list("m-agahi/yadgar")
    assert result is None


def test_cache_set_and_get(cache) -> None:
    """A set value is returned on get."""
    value = [{"number": 1, "title": "ADR-0001"}]
    cache.set_adr_list("m-agahi/yadgar", value)
    assert cache.get_adr_list("m-agahi/yadgar") == value


def test_cache_isolated_by_project(cache) -> None:
    """Two projects don't share cache entries."""
    cache.set_adr_list("m-agahi/yadgar", [{"number": 1}])
    cache.set_adr_list("quinyx/other", [{"number": 99}])

    assert cache.get_adr_list("m-agahi/yadgar") == [{"number": 1}]
    assert cache.get_adr_list("quinyx/other") == [{"number": 99}]


def test_cache_invalidate_project(cache) -> None:
    """Invalidating one project doesn't touch another."""
    cache.set_adr_list("m-agahi/yadgar", [{"number": 1}])
    cache.set_adr_list("quinyx/other", [{"number": 99}])
    cache.invalidate("m-agahi/yadgar")

    assert cache.get_adr_list("m-agahi/yadgar") is None
    assert cache.get_adr_list("quinyx/other") == [{"number": 99}]


def test_cache_task_list_separate_from_adr(cache) -> None:
    """task_list and adr_list cache entries are separate by kind."""
    cache.set_task_list("m-agahi/yadgar", [{"number": 42}])
    cache.set_adr_list("m-agahi/yadgar", [{"number": 1}])

    assert cache.get_task_list("m-agahi/yadgar") == [{"number": 42}]
    assert cache.get_adr_list("m-agahi/yadgar") == [{"number": 1}]


def test_cache_ttl_expiry(cache) -> None:
    """Expired entries return None."""
    import time

    cache.set_adr_list("m-agahi/yadgar", [{"number": 1}], ttl_seconds=0)
    time.sleep(0.01)
    assert cache.get_adr_list("m-agahi/yadgar") is None
