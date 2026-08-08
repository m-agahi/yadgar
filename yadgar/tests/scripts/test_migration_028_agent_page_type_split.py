"""Tests for migration_028 — split page_type=agent_prompt (ADR-0209).

Coverage:
- 028 is registered in _MIGRATIONS after 027, mapped to the callable
- slug prefix drives the mapping: agent-discipline-* → agent_discipline,
  agent-prompt-contract → agent_discipline (ADR-0209: the contract lives
  INSIDE the discipline type), agent-prompt-toc → agent_index, remaining
  agent-prompt-* → agent_pattern
- the null-page_type TOC is migrated (task 0134) — a page_type-keyed sweep
  would have missed it
- unrelated slugs that merely CONTAIN "agent-" are untouched
- idempotent: a second run issues zero UPDATEs
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.storage.migrations import (
    _MIGRATIONS,
    _migration_028_agent_page_type_split,
)
from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
)


class _FakeStorage:
    """Minimal storage double: one wiki_page table, SELECT + UPDATE only."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.updates: list[tuple[int, dict]] = []

    def _q(self, query: str, params: dict | None = None):  # noqa: ARG002
        return [dict(r) for r in self.rows]

    def _extract_id(self, raw):
        return raw

    def update_wiki_page(self, page_id: int, fields: dict) -> None:
        self.updates.append((page_id, fields))
        for r in self.rows:
            if r["id"] == page_id:
                r.update(fields)


def _rows() -> list[dict]:
    return [
        {"id": 1, "slug": "agent-prompt-dispatch-fix-bug", "page_type": "agent_prompt"},
        {"id": 2, "slug": "agent-prompt-contract", "page_type": "agent_prompt"},
        {"id": 3, "slug": "agent-discipline-adr-consult", "page_type": "agent_prompt"},
        {"id": 4, "slug": "agent-prompt-toc", "page_type": None},
        {"id": 5, "slug": "1password-ssh-agent-key-config", "page_type": None},
        {"id": 6, "slug": "yadgar-adr-0209", "page_type": "adr"},
    ]


def _applied(storage: _FakeStorage) -> dict[int, str]:
    return {pid: fields["page_type"] for pid, fields in storage.updates}


class TestMigration028Registration:
    def test_in_migrations_list(self):
        versions = [m["version"] for m in _MIGRATIONS]
        assert "028_agent_page_type_split" in versions

    def test_maps_to_callable(self):
        entry = next(m for m in _MIGRATIONS if m["version"] == "028_agent_page_type_split")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_028_agent_page_type_split

    def test_registered_after_027(self):
        versions = [m["version"] for m in _MIGRATIONS]
        assert versions.index("028_agent_page_type_split") > versions.index(
            "027_runtime_config_table"
        )


class TestMigration028Mapping:
    def test_pattern_page(self):
        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        assert _applied(st)[1] == PAGE_TYPE_AGENT_PATTERN

    def test_contract_becomes_discipline(self):
        """ADR-0209: the contract stays INSIDE the discipline type."""
        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        assert _applied(st)[2] == PAGE_TYPE_AGENT_DISCIPLINE

    def test_discipline_page(self):
        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        assert _applied(st)[3] == PAGE_TYPE_AGENT_DISCIPLINE

    def test_toc_with_null_page_type_is_migrated(self):
        """Task 0134: null page_type → DEFAULT_POLICY include → recall-visible."""
        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        assert _applied(st)[4] == PAGE_TYPE_AGENT_INDEX

    def test_unrelated_slug_containing_agent_untouched(self):
        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        assert 5 not in _applied(st)

    def test_other_page_types_untouched(self):
        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        assert 6 not in _applied(st)

    def test_stamps_schema_version(self):
        from yadgar._shared.wiki.wiki_meta import WIKI_SCHEMA_VERSION

        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        for _pid, fields in st.updates:
            assert fields["wiki_schema_version"] == WIKI_SCHEMA_VERSION


class TestMigration028Idempotent:
    def test_second_run_writes_nothing(self):
        st = _FakeStorage(_rows())
        _migration_028_agent_page_type_split(st)
        first = len(st.updates)
        assert first == 4
        st.updates.clear()
        _migration_028_agent_page_type_split(st)
        assert st.updates == []

    def test_no_rows_is_noop(self):
        st = _FakeStorage([])
        _migration_028_agent_page_type_split(st)
        assert st.updates == []

    def test_read_failure_propagates(self):
        """A failed backfill must NOT be marked applied.

        `_run_migrations_locked` writes the schema_version row only after the
        fn returns, so swallowing a read error here would permanently mark a
        migration that did nothing as done.
        """
        broken = MagicMock()
        broken._q.side_effect = RuntimeError("db down")
        with pytest.raises(RuntimeError):
            _migration_028_agent_page_type_split(broken)
