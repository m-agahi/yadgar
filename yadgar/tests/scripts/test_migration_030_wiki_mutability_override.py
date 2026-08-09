"""Tests for migration_030 — add nullable ``mutability_override`` column to wiki_page.

Car J (0047 §7 D25/D26). Schema-only: ``DEFINE FIELD IF NOT EXISTS
mutability_override ON TABLE wiki_page TYPE option<string>;``. No backfill —
pre-migration rows fall through to per-type default (adr→locked,
task/agent→free, else→free).

Coverage here is the LOGIC and the IDEMPOTENCY — driven by ``_FakeStorage``
(an in-memory stand-in that records the statements the migration emits).
The real DDL runs against a live SurrealDB in the e2e harness.

Why idempotency matters: ``DEFINE FIELD IF NOT EXISTS`` must remain
idempotent so a daemon restart after the migration already ran can re-init
the schema without raising.
"""

from __future__ import annotations

from yadgar._shared.storage.migrations import (
    _MIGRATIONS,
    _migration_030_wiki_mutability_override,
)


class _FakeStorage:
    """In-memory stand-in that records the SQL the migration emits.

    The migration is one DEFINE FIELD — no rows to track. We just record
    every ``_q`` call so the assertion can inspect the statement shape.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def _q(self, surql: str, params: dict | None = None) -> list[dict]:  # noqa: ARG002
        self.statements.append(surql)
        return []


class TestMigration030MutabilityOverride:
    """Migration 030 adds the mutability_override field on wiki_page."""

    def test_emits_define_field_option_string(self):
        """The migration issues exactly one DEFINE FIELD statement with the
        correct table, column, and ``option<string>`` type.
        """
        storage = _FakeStorage()
        _migration_030_wiki_mutability_override(storage)
        assert storage.statements == [
            "DEFINE FIELD IF NOT EXISTS mutability_override ON TABLE wiki_page TYPE option<string>;"
        ]

    def test_migration_registered_in_list(self):
        """The migration is appended to _MIGRATIONS (no reordering of earlier entries)."""
        versions = [m["version"] for m in _MIGRATIONS]
        assert "030_wiki_mutability_override" in versions
        # And 029 is still immediately before it — append-only invariant.
        idx_029 = versions.index("029_drop_branch_column")
        idx_030 = versions.index("030_wiki_mutability_override")
        assert idx_030 == idx_029 + 1

    def test_idempotent_on_rerun(self):
        """Re-running the migration is a no-op at the SQL layer
        (``IF NOT EXISTS``). The fake storage records both calls; we only
        assert the shape stays identical — SurrealDB itself enforces the
        no-op semantics.
        """
        storage = _FakeStorage()
        _migration_030_wiki_mutability_override(storage)
        _migration_030_wiki_mutability_override(storage)
        assert len(storage.statements) == 2
        assert storage.statements[0] == storage.statements[1]
