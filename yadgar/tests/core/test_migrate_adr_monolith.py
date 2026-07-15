"""TDD test suite for scripts/migrate_adr_monolith.py — Car 2 ADR migration.

Tests cover:
  1. parse_monolith_sections  — correct extraction from ## ADR-NNNN monolith format
  2. deprecated-audit (decide_retention) — all four §C.6.1 rules
  3. Full migration (end-to-end) against an embedded store seeded with monolith pages:
       a. per-project directory_context threaded through
       b. IDs per project preserved, ID gaps kept (no renumber)
       c. idempotent — 2nd run creates 0 new pages
       d. monolith deleted only after verify passes (never in dry-run)
  4. aws-work-adr-log-style non-git mis-pin (branch="master") — ADRs re-land
     canonical (branch NULL), readable without branch_hint
  5. dry-run mutates nothing
  6. Per-project isolation — one bad project does not abort others
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Make the scripts/ dir importable without install so we can call the
# parse/migrate helpers directly.
# ---------------------------------------------------------------------------
_repo_root = Path(__file__).resolve().parent.parent.parent.parent
_scripts_dir = _repo_root / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from migrate_adr_monolith import (  # noqa: E402
    compute_inbound_refs,
    decide_retention,
    parse_monolith_sections,
)

from yadgar._shared.storage.migrations import _migration_013_wiki_page_version  # noqa: E402
from yadgar.core import server  # noqa: E402

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Embedded storage with isolated temp database."""
    tmp = tmp_path_factory.mktemp("migrate_adr")
    server.init_engines(
        db_path=str(tmp / "adr_migrate_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    _migration_013_wiki_page_version(server._get_storage())
    yield
    server.shutdown()


# ── Helpers ────────────────────────────────────────────────────────────────────

_MONOLITH_TEMPLATE = """\
# {project} ADR Log

## ADR-{n1}: Use SurrealDB
- status: accepted
- date: 2026-01-01
- context: Need durable storage.
- decision: Adopt SurrealDB.
- rationale: Graph + relational queries.
- alternatives: SQLite, PostgreSQL.
- consequences: +30MB binary.
- revisit_trigger: Perf degrades.
- supersedes: none

## ADR-{n3}: Add caching layer
- status: open
- date: 2026-03-01
- context: Hot reads are slow.
- decision: Add Redis.
- rationale: Sub-ms reads.
- alternatives: In-process dict.
- consequences: New dependency.
- revisit_trigger: Memory budget hit.
- supersedes: none
"""

_MONOLITH_GAP = """\
# gapproj ADR Log

## ADR-0001: First accepted
- status: accepted
- date: 2026-01-01
- context: ctx1
- decision: dec1
- rationale: rat1
- alternatives: alt1
- consequences: con1
- revisit_trigger: rt1
- supersedes: none

## ADR-0003: Third (gap after 0001)
- status: accepted
- date: 2026-03-01
- context: ctx3
- decision: dec3
- rationale: rat3
- alternatives: alt3
- consequences: con3
- revisit_trigger: rt3
- supersedes: none
"""


def _seed_monolith(storage, slug: str, content: str, directory: str, branch: str | None = None):
    """Insert a fake monolith page directly into the embedded store."""
    page: dict = {
        "title": slug,
        "slug": slug,
        "content": content,
        "category": "reference",
        "tags": ["adr-log"],
        "confidence": "high",
        "directory_context": directory,
        "page_type": None,
    }
    return storage.insert_wiki_page(page, branch=branch)


def _make_monolith(n1: str, n3: str, project: str) -> str:
    return _MONOLITH_TEMPLATE.format(project=project, n1=n1, n3=n3)


# ── 1. Pure parse tests ────────────────────────────────────────────────────────


class TestParseMonolithSections:
    def test_parses_basic_two_sections(self):
        content = _make_monolith("0001", "0003", "myproj")
        sections = parse_monolith_sections(content)
        assert len(sections) == 2
        assert sections[0]["adr_id"] == "ADR-0001"
        assert sections[1]["adr_id"] == "ADR-0003"
        assert sections[0]["title"] == "Use SurrealDB"
        assert sections[0]["status"] == "accepted"
        assert sections[0]["date"] == "2026-01-01"

    def test_ascending_sort_order(self):
        # Sections out-of-order in source must be returned sorted ascending.
        content = "## ADR-0003: Third\n- status: open\n\n## ADR-0001: First\n- status: accepted\n"
        sections = parse_monolith_sections(content)
        assert [s["adr_id"] for s in sections] == ["ADR-0001", "ADR-0003"]

    def test_empty_monolith_returns_empty(self):
        assert parse_monolith_sections("") == []
        assert parse_monolith_sections("# Just a heading\n") == []

    def test_id_gap_preserved(self):
        sections = parse_monolith_sections(_MONOLITH_GAP)
        ids = [s["adr_id"] for s in sections]
        assert ids == ["ADR-0001", "ADR-0003"], f"Gap not preserved: {ids}"

    def test_supersedes_field_parsed(self):
        content = (
            "## ADR-0002: Reversal\n"
            "- status: accepted\n"
            "- date: 2026-02-01\n"
            "- context: c\n"
            "- decision: d\n"
            "- rationale: r\n"
            "- alternatives: a\n"
            "- consequences: con\n"
            "- revisit_trigger: rt\n"
            "- supersedes: ADR-0001\n"
        )
        sections = parse_monolith_sections(content)
        assert sections[0]["supersedes"] == "ADR-0001"


# ── 2. Deprecated-audit tests ──────────────────────────────────────────────────


class TestDecideRetention:
    def _make_section(self, adr_id: str, status: str, supersedes: str = "none") -> dict:
        return {
            "adr_id": adr_id,
            "title": adr_id,
            "status": status,
            "supersedes": supersedes,
            "date": "2026-01-01",
            "context": "c",
            "decision": "d",
            "rationale": "r",
            "alternatives": "a",
            "consequences": "con",
            "revisit_trigger": "rt",
        }

    def test_rejected_no_inbound_dropped(self):
        sections = [self._make_section("ADR-0001", "rejected")]
        decisions = decide_retention(sections)
        assert decisions["ADR-0001"] is False

    def test_deprecated_no_inbound_dropped(self):
        sections = [self._make_section("ADR-0001", "deprecated")]
        decisions = decide_retention(sections)
        assert decisions["ADR-0001"] is False

    def test_superseded_always_retained(self):
        sections = [self._make_section("ADR-0001", "superseded")]
        decisions = decide_retention(sections)
        assert decisions["ADR-0001"] is True

    def test_deprecated_with_inbound_retained(self):
        s1 = self._make_section("ADR-0001", "deprecated")
        # ADR-0002 supersedes ADR-0001 → ADR-0001 has an inbound ref → RETAIN
        s2 = self._make_section("ADR-0002", "accepted", supersedes="ADR-0001")
        sections = [s1, s2]
        decisions = decide_retention(sections)
        assert decisions["ADR-0001"] is True
        assert decisions["ADR-0002"] is True

    def test_rejected_with_inbound_retained(self):
        s1 = self._make_section("ADR-0001", "rejected")
        s2 = self._make_section("ADR-0002", "accepted", supersedes="ADR-0001")
        decisions = decide_retention([s1, s2])
        assert decisions["ADR-0001"] is True

    def test_open_accepted_always_retained(self):
        sections = [
            self._make_section("ADR-0001", "open"),
            self._make_section("ADR-0002", "accepted"),
        ]
        decisions = decide_retention(sections)
        assert decisions["ADR-0001"] is True
        assert decisions["ADR-0002"] is True

    def test_compute_inbound_refs(self):
        s1 = self._make_section("ADR-0001", "accepted")
        s2 = self._make_section("ADR-0002", "accepted", supersedes="ADR-0001")
        inbound = compute_inbound_refs([s1, s2])
        assert "ADR-0001" in inbound
        assert "ADR-0002" in inbound["ADR-0001"]
        assert "ADR-0002" not in inbound  # has no inbound refs


# ── 3. Full migration (end-to-end) ─────────────────────────────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestMigrateAllProjects:
    """End-to-end migration against the embedded store.

    Patches _resolve_project_root and seeds monolith pages directly into storage
    so _migrate_project can exercise the real write + read paths.
    """

    def _run_migrate_project(
        self,
        project_name: str,
        directory: str,
        content: str,
        dry_run: bool = False,
        delete_monolith: bool = False,
        branch: str | None = None,
    ) -> dict:
        """Seed monolith + call _migrate_project; returns result dict."""
        from migrate_adr_monolith import _migrate_project

        storage = server._get_storage()
        pid = _seed_monolith(storage, f"{project_name}-adr-log", content, directory, branch=branch)

        with patch(
            "migrate_adr_monolith.wiki_read",
            wraps=__import__("yadgar.core.server.tools.wiki", fromlist=["wiki_read"]).wiki_read,
        ):
            return _migrate_project(
                storage=storage,
                project_name=project_name,
                monolith_page_id=pid,
                content=content,
                directory_context=directory,
                dry_run=dry_run,
                delete_monolith=delete_monolith,
            )

    def test_per_project_directory_context_threaded(self, tmp_path):
        """Each project's directory_context is used for per-ADR pages."""
        from yadgar.core.server.tools.wiki import wiki_read

        proj_dir = str(tmp_path / "dirproj")
        os.makedirs(proj_dir, exist_ok=True)
        content = _make_monolith("0001", "0002", "dirproj")

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=proj_dir):
            from migrate_adr_monolith import _migrate_project

            storage = server._get_storage()
            pid = _seed_monolith(storage, "dirproj-adr-log", content, proj_dir)
            result = _migrate_project(
                storage=storage,
                project_name="dirproj",
                monolith_page_id=pid,
                content=content,
                directory_context=proj_dir,
                dry_run=False,
                delete_monolith=False,
            )

        assert result["errors"] == [], result["errors"]
        # The per-ADR page must be readable with the project's directory as context.
        page = wiki_read("dirproj-adr-0001", directory=proj_dir)
        assert "error" not in page, f"Page not readable: {page}"
        assert page.get("directory_context") == proj_dir

    def test_id_gaps_preserved_no_renumber(self, tmp_path):
        """ADR-0001 and ADR-0003 (gap) migrate without renumbering."""
        from yadgar.core.server.tools.adr import parse_index_rows
        from yadgar.core.server.tools.wiki import wiki_read

        proj_dir = str(tmp_path / "gapproj")
        os.makedirs(proj_dir, exist_ok=True)

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=proj_dir):
            from migrate_adr_monolith import _migrate_project

            storage = server._get_storage()
            pid = _seed_monolith(storage, "gapproj-adr-log", _MONOLITH_GAP, proj_dir)
            result = _migrate_project(
                storage=storage,
                project_name="gapproj",
                monolith_page_id=pid,
                content=_MONOLITH_GAP,
                directory_context=proj_dir,
                dry_run=False,
                delete_monolith=False,
            )

        assert result["errors"] == [], result["errors"]
        # ADR-0002 must NOT exist (gap must be preserved).
        missing = wiki_read("gapproj-adr-0002", directory=proj_dir)
        assert "error" in missing, "ADR-0002 should not exist (gap)"

        # ADR-0001 and ADR-0003 must exist.
        p1 = wiki_read("gapproj-adr-0001", directory=proj_dir)
        p3 = wiki_read("gapproj-adr-0003", directory=proj_dir)
        assert "error" not in p1
        assert "error" not in p3

        # Index has exactly 2 rows with the correct IDs.
        idx = wiki_read("gapproj-adr-index", directory=proj_dir)
        rows = parse_index_rows(idx.get("content") or "")
        assert [r["adr_id"] for r in rows] == ["ADR-0001", "ADR-0003"]

    def test_idempotent_second_run_creates_zero(self, tmp_path):
        """Running migration twice: second pass creates 0 new pages."""
        proj_dir = str(tmp_path / "idemproj")
        os.makedirs(proj_dir, exist_ok=True)
        content = _make_monolith("0001", "0002", "idemproj")

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=proj_dir):
            from migrate_adr_monolith import _migrate_project

            storage = server._get_storage()
            pid1 = _seed_monolith(storage, "idemproj-adr-log", content, proj_dir)
            # First run.
            r1 = _migrate_project(
                storage=storage,
                project_name="idemproj",
                monolith_page_id=pid1,
                content=content,
                directory_context=proj_dir,
                dry_run=False,
                delete_monolith=False,
            )
            # Second run (same monolith page_id, pages already exist).
            r2 = _migrate_project(
                storage=storage,
                project_name="idemproj",
                monolith_page_id=pid1,
                content=content,
                directory_context=proj_dir,
                dry_run=False,
                delete_monolith=False,
            )

        assert r1["errors"] == [], r1["errors"]
        assert r2["created"] == 0, f"Second run created {r2['created']} pages (should be 0)"
        assert r2["errors"] == [], r2["errors"]

    def test_monolith_deleted_only_after_verify_passes(self, tmp_path):
        """Monolith page is deleted iff verify passes and --delete-monolith set."""
        proj_dir = str(tmp_path / "delproj")
        os.makedirs(proj_dir, exist_ok=True)
        content = _make_monolith("0001", "0002", "delproj")

        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=proj_dir):
            from migrate_adr_monolith import _migrate_project

            storage = server._get_storage()
            pid = _seed_monolith(storage, "delproj-adr-log", content, proj_dir)
            result = _migrate_project(
                storage=storage,
                project_name="delproj",
                monolith_page_id=pid,
                content=content,
                directory_context=proj_dir,
                dry_run=False,
                delete_monolith=True,
            )

        assert result["errors"] == [], result["errors"]
        assert result["verify_ok"] is True
        assert result["monolith_deleted"] is True

        # Monolith page must be gone.
        gone = storage.get_wiki_page(pid)
        assert gone is None, "Monolith page was not deleted"


# ── 4. Non-git mis-pin (aws-work-adr-log style) ────────────────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestNonGitMisPin:
    """aws-work-adr-log was mis-pinned to branch='master' in a non-git dir.

    The migration reads the monolith directly by page_id (bypassing branch
    resolution), then writes canonical (branch IS NULL) pages that resolve from
    any caller context — no branch_hint required.
    """

    def test_mispin_relanded_canonical(self, tmp_path):
        """Monolith with branch='master' on a non-git dir → ADR pages are canonical."""
        from yadgar.core.server.tools.wiki import wiki_read

        proj_dir = "/nonexistent/aws-work"
        content = _make_monolith("0001", "0002", "aws-work")

        from migrate_adr_monolith import _migrate_project

        storage = server._get_storage()
        # Simulate the mis-pin: insert the monolith with an explicit branch.
        pid = _seed_monolith(storage, "aws-work-adr-log", content, proj_dir, branch="master")

        # Patch _resolve_project_root to return the non-git dir string unchanged
        # (as happens for /nonexistent paths that aren't real git trees).
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=proj_dir):
            result = _migrate_project(
                storage=storage,
                project_name="aws-work",
                monolith_page_id=pid,
                content=content,
                directory_context=proj_dir,
                dry_run=False,
                delete_monolith=False,
            )

        assert result["errors"] == [], result["errors"]

        # Pages must be canonical (branch IS NULL) — readable without branch_hint.
        page = wiki_read("aws-work-adr-0001", directory=proj_dir)
        assert "error" not in page, f"ADR page not found: {page}"
        assert page.get("branch") is None, (
            f"Expected branch=None (canonical), got {page.get('branch')!r}"
        )


# ── 5. Dry-run mutates nothing ─────────────────────────────────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestDryRunMutatesNothing:
    def test_dry_run_creates_no_pages(self, tmp_path):
        """dry_run=True must not create any per-ADR or index pages."""
        from yadgar.core.server.tools.wiki import wiki_read

        proj_dir = str(tmp_path / "dryproj")
        os.makedirs(proj_dir, exist_ok=True)
        content = _make_monolith("0001", "0002", "dryproj")

        from migrate_adr_monolith import _migrate_project

        storage = server._get_storage()
        pid = _seed_monolith(storage, "dryproj-adr-log", content, proj_dir)
        result = _migrate_project(
            storage=storage,
            project_name="dryproj",
            monolith_page_id=pid,
            content=content,
            directory_context=proj_dir,
            dry_run=True,
            delete_monolith=True,  # should be ignored in dry_run
        )

        # No pages created.
        assert result["created"] == 0
        assert result["index_written"] is False
        assert result["monolith_deleted"] is False

        # Pages do not exist.
        p = wiki_read("dryproj-adr-0001", directory=proj_dir)
        assert "error" in p, "Dry-run must not write pages"

        # Monolith still exists.
        mono = storage.get_wiki_page(pid)
        assert mono is not None, "Dry-run must not delete the monolith"


# ── 6. Deprecated-audit full integration ──────────────────────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestDeprecatedAuditIntegration:
    """Integration: deprecated/rejected ADRs are dropped or kept per §C.6.1."""

    def _make_audit_monolith(self) -> str:
        return (
            "## ADR-0001: Accepted baseline\n"
            "- status: accepted\n"
            "- date: 2026-01-01\n"
            "- context: c\n- decision: d\n- rationale: r\n"
            "- alternatives: a\n- consequences: con\n"
            "- revisit_trigger: rt\n- supersedes: none\n\n"
            "## ADR-0002: Rejected no inbound\n"
            "- status: rejected\n"
            "- date: 2026-01-02\n"
            "- context: c\n- decision: d\n- rationale: r\n"
            "- alternatives: a\n- consequences: con\n"
            "- revisit_trigger: rt\n- supersedes: none\n\n"
            "## ADR-0003: Superseded target\n"
            "- status: superseded\n"
            "- date: 2026-01-03\n"
            "- context: c\n- decision: d\n- rationale: r\n"
            "- alternatives: a\n- consequences: con\n"
            "- revisit_trigger: rt\n- supersedes: none\n\n"
            "## ADR-0004: Deprecated WITH inbound\n"
            "- status: deprecated\n"
            "- date: 2026-01-04\n"
            "- context: c\n- decision: d\n- rationale: r\n"
            "- alternatives: a\n- consequences: con\n"
            "- revisit_trigger: rt\n- supersedes: none\n\n"
            "## ADR-0005: Supersedes ADR-0004\n"
            "- status: accepted\n"
            "- date: 2026-01-05\n"
            "- context: c\n- decision: d\n- rationale: r\n"
            "- alternatives: a\n- consequences: con\n"
            "- revisit_trigger: rt\n- supersedes: ADR-0004\n"
        )

    def test_audit_rules_applied(self, tmp_path):
        """rejected-no-inbound DROPPED; superseded RETAINED; deprecated-with-inbound RETAINED."""
        from yadgar.core.server.tools.wiki import wiki_read

        proj_dir = str(tmp_path / "auditproj")
        os.makedirs(proj_dir, exist_ok=True)
        content = self._make_audit_monolith()

        from migrate_adr_monolith import _migrate_project

        storage = server._get_storage()
        pid = _seed_monolith(storage, "auditproj-adr-log", content, proj_dir)
        with patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=proj_dir):
            result = _migrate_project(
                storage=storage,
                project_name="auditproj",
                monolith_page_id=pid,
                content=content,
                directory_context=proj_dir,
                dry_run=False,
                delete_monolith=False,
            )

        assert result["errors"] == [], result["errors"]
        assert result["total"] == 5
        assert result["dropped_deprecated"] == 1  # ADR-0002 rejected-no-inbound
        assert result["retained"] == 4  # ADR-0001, 0003, 0004, 0005

        # ADR-0002 (rejected, no inbound) must NOT exist.
        p2 = wiki_read("auditproj-adr-0002", directory=proj_dir)
        assert "error" in p2, "Rejected-no-inbound ADR must be dropped"

        # ADR-0003 (superseded) must exist.
        p3 = wiki_read("auditproj-adr-0003", directory=proj_dir)
        assert "error" not in p3, "Superseded ADR must be retained"

        # ADR-0004 (deprecated WITH inbound from ADR-0005) must exist.
        p4 = wiki_read("auditproj-adr-0004", directory=proj_dir)
        assert "error" not in p4, "Deprecated-with-inbound ADR must be retained"
