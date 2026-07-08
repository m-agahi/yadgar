"""ADR (Architecture Decision Record) MCP tool — TDD test suite.

Car #12 of the improvement-train.

Tests cover:
  1a. Validation tests (pure unit, no store)
  1b. ID assignment tests (with wiki fixture)
  1c. Append test
  1d. Absent-log auto-create
  1e. adr_due signal tests (_apply_adr_signal unit tests)

RED before implementation; GREEN after.
"""

from __future__ import annotations

import time
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.storage.migrations import _migration_013_wiki_page_version
from yadgar.core import server

UTC = UTC

# ── Fixtures ──────────────────────────────────────────────────────────────────

_TEST_DIR = "/tmp/test-project-adr"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Embedded storage with isolated temp database per test."""
    tmp_path = tmp_path_factory.mktemp("adr")
    server.init_engines(
        db_path=str(tmp_path / "adr_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    _migration_013_wiki_page_version(server._get_storage())
    yield
    server.shutdown()


def _storage():
    return server._get_storage()


def _insert_wiki_page(slug: str, content: str, title: str | None = None) -> int:
    """Insert a wiki page directly via storage layer. Returns page_id."""
    return _storage().insert_wiki_page(
        {
            "slug": slug,
            "title": title or slug.replace("-", " ").title(),
            "content": content,
            "category": "reference",
            "tags": [],
            "confidence": "medium",
            "source_memory_ids": [],
            "links": [],
        }
    )


# Minimal valid ADR call params (excludes directory which is passed separately)
_VALID_ADR_PARAMS = dict(
    directory=_TEST_DIR,
    title="Use SurrealDB for persistent storage",
    status="accepted",
    date="2026-06-25",
    context="We need durable key-value + graph storage for episodic memories.",
    decision="Adopt SurrealDB embedded as the single storage backend.",
    rationale="Supports both relational and graph queries; no separate process needed.",
    alternatives="SQLite (no graph), PostgreSQL (separate process), Redis (volatile).",
    consequences="Embedding SurrealDB adds ~30MB to the binary; migration required.",
    revisit_trigger="If SurrealDB embedded performance degrades beyond 500ms p95.",
    supersedes="none",
)


# ── 1a. Validation tests (pure unit, no store) ────────────────────────────────


class TestAdrAddValidation:
    def test_adr_add_rejects_missing_field(self):
        """adr_add with an empty required field returns error containing 'missing' or 'required'."""
        from yadgar.core.server.tools.adr import adr_add

        # Pass title="" (empty string) — a present but empty required field.
        # This exercises our validation code (not Python's positional-arg check).
        params = dict(_VALID_ADR_PARAMS)
        params["title"] = ""  # empty required field
        result = adr_add(**params)
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "error" in result or result.get("ok") is False, (
            f"Expected error for empty required field, got: {result}"
        )
        err_text = (result.get("error") or result.get("message") or "").lower()
        assert "missing" in err_text or "required" in err_text or "title" in err_text, (
            f"Error text should mention missing/required/field name: {err_text!r}"
        )

    def test_adr_add_rejects_invalid_status(self):
        """adr_add with status='INVALID' returns error mentioning 'status'."""
        from yadgar.core.server.tools.adr import adr_add

        params = dict(_VALID_ADR_PARAMS)
        params["status"] = "INVALID"
        result = adr_add(**params)
        assert isinstance(result, dict)
        assert "error" in result or result.get("ok") is False, (
            f"Expected error for invalid status, got: {result}"
        )
        err_text = (result.get("error") or result.get("message") or "").lower()
        assert "status" in err_text, f"Error should mention 'status': {err_text!r}"


# ── 1b. ID assignment tests ────────────────────────────────────────────────────


class TestAdrAddIdAssignment:
    def test_adr_add_assigns_adr_0001_on_empty_log(self, tmp_path):
        """adr_add against a project with no ADR log assigns ADR-0001."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path)
        params = dict(_VALID_ADR_PARAMS, directory=project_dir)
        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
            patch("yadgar.core.server.tools.adr.wiki_read", return_value={"error": "not found"}),
            patch(
                "yadgar.core.server.tools.adr.wiki_add",
                return_value={"stored": True, "committed": True},
            ),
        ):
            result = adr_add(**params)
        assert result.get("adr_id") == "ADR-0001", f"Expected ADR-0001 for empty log, got: {result}"

    def test_adr_add_assigns_sequential_id(self, tmp_path):
        """adr_add after a log with ADR-0003 as last header assigns ADR-0004."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path)
        existing_log = (
            "# ADR Log\n\n"
            "## ADR-0001: First decision\n\n- status: accepted\n\n"
            "## ADR-0002: Second decision\n\n- status: accepted\n\n"
            "## ADR-0003: Third decision\n\n- status: open\n\n"
        )
        params = dict(_VALID_ADR_PARAMS, directory=project_dir)
        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
            patch("yadgar.core.server.tools.adr.wiki_read", return_value={"content": existing_log}),
            patch(
                "yadgar.core.server.tools.adr.wiki_append_section",
                return_value={
                    "page_id": 1,
                    "new_version": 4,
                    "section_heading": "ADR-0004: Use SurrealDB for persistent storage",
                    "action": "appended",
                    "size_before": 200,
                    "size_after": 500,
                },
            ),
        ):
            result = adr_add(**params)
        assert result.get("adr_id") == "ADR-0004", f"Expected ADR-0004, got: {result}"

    def test_adr_add_id_scan_uses_headers_only(self, tmp_path):
        """ID scan is anchored to ## ADR-NNNN headers only, ignores body references."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path)
        # ADR-0003 is the last header; body contains ADR-0009 ref
        existing_log = (
            "## ADR-0001: First\n\n- status: accepted\n\n"
            "## ADR-0002: Second\n\n- status: accepted\n\n"
            "## ADR-0003: Third\n\n"
            "This supersedes ADR-0009 (from another system, wrong ID).\n\n"
        )
        params = dict(_VALID_ADR_PARAMS, directory=project_dir)
        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
            patch("yadgar.core.server.tools.adr.wiki_read", return_value={"content": existing_log}),
            patch(
                "yadgar.core.server.tools.adr.wiki_append_section",
                return_value={
                    "page_id": 1,
                    "new_version": 4,
                    "section_heading": "ADR-0004: x",
                    "action": "appended",
                    "size_before": 200,
                    "size_after": 300,
                },
            ),
        ):
            result = adr_add(**params)
        assert result.get("adr_id") == "ADR-0004", (
            f"Expected ADR-0004 (header scan only, ignoring body ADR-0009), got: {result}"
        )

    def test_adr_add_branch_scope_pin(self, tmp_path):
        """ADR log is read with branch_hint=default_branch regardless of cwd branch."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path)
        # Log seeded with ADR-0006 as last entry
        existing_log = "\n".join(
            f"## ADR-{i:04d}: Decision {i}\n\n- status: accepted\n" for i in range(1, 7)
        )
        params = dict(_VALID_ADR_PARAMS, directory=project_dir)
        captured_branch_hint = {}

        def mock_wiki_read(slug, directory=None, branch_hint=None):
            captured_branch_hint["branch_hint"] = branch_hint
            return {"content": existing_log}

        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
            patch("yadgar.core.server.tools.adr.wiki_read", side_effect=mock_wiki_read),
            patch(
                "yadgar.core.server.tools.adr.wiki_append_section",
                return_value={
                    "page_id": 1,
                    "new_version": 7,
                    "section_heading": "ADR-0007: x",
                    "action": "appended",
                    "size_before": 400,
                    "size_after": 700,
                },
            ),
        ):
            result = adr_add(**params)
        # Must use "master" branch_hint (pinned to default branch, not feature branch cwd)
        assert captured_branch_hint.get("branch_hint") == "master", (
            f"wiki_read should be called with branch_hint='master', "
            f"got: {captured_branch_hint.get('branch_hint')!r}"
        )
        assert result.get("adr_id") == "ADR-0007", (
            f"Expected ADR-0007 (N+1 of ADR-0006), got: {result}"
        )


# ── 1c. Append test ───────────────────────────────────────────────────────────


class TestAdrAddAppend:
    def test_adr_add_appends_to_log(self, tmp_path):
        """adr_add appends new ADR section; wiki_read of log shows it."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path)
        project_name = "tmpproject"  # basename of tmp_path is randomised — mock it

        # Seed an existing log with ADR-0001
        existing_log_content = "## ADR-0001: Initial decision\n\n- status: accepted\n"
        page_id = _insert_wiki_page(
            slug=f"{project_name}-adr-log",
            content=existing_log_content,
            title=f"{project_name} ADR Log",
        )

        new_section_captured = {}

        def mock_wiki_append(
            slug, section_heading, content, position=None, directory=None, branch_hint=None, **kw
        ):
            new_section_captured["section_heading"] = section_heading
            new_section_captured["content"] = content
            # Simulate successful append response
            return {
                "page_id": page_id,
                "new_version": 2,
                "section_heading": section_heading,
                "action": "appended",
                "size_before": len(existing_log_content),
                "size_after": len(existing_log_content) + len(content) + 50,
            }

        params = dict(_VALID_ADR_PARAMS, directory=project_dir)
        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
            patch("os.path.basename", return_value=project_name),
            patch(
                "yadgar.core.server.tools.adr.wiki_read",
                return_value={"content": existing_log_content},
            ),
            patch("yadgar.core.server.tools.adr.wiki_append_section", side_effect=mock_wiki_append),
        ):
            result = adr_add(**params)

        assert result.get("adr_id") == "ADR-0002", f"Expected ADR-0002, got: {result}"
        # Section heading must start with ADR-0002
        heading = new_section_captured.get("section_heading", "")
        assert "ADR-0002" in heading, f"Section heading should contain ADR-0002, got: {heading!r}"
        # Content should include core fields
        body = new_section_captured.get("content", "")
        assert "accepted" in body, f"Content should include status: {body!r}"
        assert "SurrealDB" in body, f"Content should include decision text: {body!r}"
        # Canonical flat-bullet format (unbolded, fixed order) — must match existing adr-log.
        assert "- status: " in body, f"Body should use flat 'status' bullet: {body!r}"
        assert "- context: " in body, f"Body should use flat 'context' bullet: {body!r}"
        assert "- decision: " in body, f"Body should use flat 'decision' bullet: {body!r}"
        assert "- supersedes: " in body, f"Body should use flat 'supersedes' bullet: {body!r}"
        assert "### Context" not in body, f"Body must not use ### sub-headings: {body!r}"
        assert "- **status:**" not in body, f"Body must not bold bullets: {body!r}"


# ── 1d. Absent-log auto-create ────────────────────────────────────────────────


class TestAdrAddAutoCreate:
    def test_adr_add_creates_log_when_absent(self, tmp_path):
        """adr_add on fresh project auto-creates the ADR log wiki page."""
        from yadgar.core.server.tools.adr import adr_add

        project_dir = str(tmp_path)
        project_name = "newproject"
        wiki_add_called = {}

        def mock_wiki_add(title, content, directory=None, branch_hint=None, wait=False, **kw):
            wiki_add_called["title"] = title
            wiki_add_called["content"] = content
            wiki_add_called["branch_hint"] = branch_hint
            return {"stored": True, "committed": True, "slug": f"{project_name}-adr-log"}

        params = dict(_VALID_ADR_PARAMS, directory=project_dir)
        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
            patch("os.path.basename", return_value=project_name),
            # wiki_read returns not-found → triggers auto-create path
            patch("yadgar.core.server.tools.adr.wiki_read", return_value={"error": "not found"}),
            patch("yadgar.core.server.tools.adr.wiki_add", side_effect=mock_wiki_add),
        ):
            result = adr_add(**params)

        assert result.get("adr_id") == "ADR-0001", f"Expected ADR-0001, got: {result}"
        assert wiki_add_called, "wiki_add should have been called to create the log"
        content = wiki_add_called.get("content", "")
        assert "ADR-0001" in content, f"Created log should contain ADR-0001 section: {content!r}"
        assert wiki_add_called.get("branch_hint") == "master", (
            f"wiki_add should use branch_hint='master', got: {wiki_add_called.get('branch_hint')!r}"
        )


# ── 1c/1d Round-trip integration (real embedded store) ───────────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestAdrAddRoundTrip:
    """End-to-end tests hitting the real embedded wiki store.

    Patches ONLY the deterministic helpers (_resolve_project_root,
    _get_default_branch).  The wiki layer (wiki_add, wiki_read,
    wiki_append_section) is real — this proves wait=True commit +
    read-your-writes + sequential ID assignment against actual rendered content.
    """

    def test_adr_add_create_then_append_sequential_ids(self, tmp_path):
        """Two successive adr_add calls produce ADR-0001 then ADR-0002; both appear in log."""
        from yadgar.core.server.tools.adr import adr_add
        from yadgar.core.server.tools.wiki import wiki_read

        project_dir = str(tmp_path / "myproj")
        __import__("os").makedirs(project_dir, exist_ok=True)

        params = dict(_VALID_ADR_PARAMS, directory=project_dir)

        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
        ):
            result1 = adr_add(**params)
            result2 = adr_add(**dict(params, title="Adopt SQLite for embedding cache"))

        assert result1.get("adr_id") == "ADR-0001", f"First call: {result1}"
        assert result2.get("adr_id") == "ADR-0002", f"Second call: {result2}"

        # Verify the real log page contains both headers.
        slug = "myproj-adr-log"
        page = wiki_read(slug, directory=project_dir, branch_hint="master")
        assert "error" not in page, f"wiki_read returned error: {page}"
        content = page.get("content", "")
        assert "## ADR-0001" in content, f"ADR-0001 header missing from log: {content[:500]!r}"
        assert "## ADR-0002" in content, f"ADR-0002 header missing from log: {content[:500]!r}"

    def test_adr_add_body_header_does_not_poison_id_scan(self, tmp_path):
        """A col-0 ``## ADR-NNNN`` line inside a body field must not poison _next_adr_id.

        RED before fix: ``to_markdown_body`` rendered field values flush-left, so a
        body line ``## ADR-9999`` landed at column 0 and matched ``^## ADR-(\\d{4})``,
        making the *next* adr_add return ADR-10000 instead of ADR-0002.
        """
        import re

        from yadgar.core.server.tools.adr import adr_add
        from yadgar.core.server.tools.wiki import wiki_read

        project_dir = str(tmp_path / "myproj")
        __import__("os").makedirs(project_dir, exist_ok=True)

        # First ADR's `context` references another ADR id on its own line.
        poison_params = dict(
            _VALID_ADR_PARAMS,
            directory=project_dir,
            context="Considered the older approach:\n## ADR-9999: a referenced decision\nthat we discuss inline.",
        )

        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
        ):
            result1 = adr_add(**poison_params)
            result2 = adr_add(**dict(_VALID_ADR_PARAMS, directory=project_dir, title="Second ADR"))

        assert result1.get("adr_id") == "ADR-0001", f"First call: {result1}"
        # The bug returned ADR-10000 here (9999 + 1) — must be ADR-0002.
        assert result2.get("adr_id") == "ADR-0002", (
            f"Body ## ADR-9999 poisoned the id scan; expected ADR-0002, got: {result2}"
        )

        page = wiki_read("myproj-adr-log", directory=project_dir, branch_hint="master")
        content = page.get("content", "")
        # Exactly the two real ADR headers must be detectable at column 0.
        real_headers = re.findall(r"^## ADR-(\d{4})", content, re.MULTILINE)
        assert real_headers == ["0001", "0002"], (
            f"col-0 ## ADR- scan must see only real headers, got: {real_headers!r}"
        )
        # The referenced id must still be present somewhere (content preserved).
        assert "ADR-9999" in content, f"Referenced ADR-9999 text lost from log: {content[:600]!r}"

    def test_adr_add_multiline_markdown_fields_roundtrip(self, tmp_path):
        """Arbitrary multi-line markdown in any field is stored without structural corruption.

        Realistic stop-hook payload: multi-line prose with markdown headers, a table,
        a fenced code block, an unbalanced fence, bullets, em-dashes, ``:`` and ``|``.
        After two appends, both ADR headers must be parseable and content preserved.
        """
        import re

        from yadgar.core.server.tools.adr import adr_add
        from yadgar.core.server.tools.wiki import wiki_read

        project_dir = str(tmp_path / "myproj")
        __import__("os").makedirs(project_dir, exist_ok=True)

        nasty_context = (
            "We hit a problem this session. Several things:\n\n"
            "## Background\n"
            "- the pipeline broke at step 3\n"
            "- fallback to wiki_append_section\n\n"
            "| col | val |\n| --- | --- |\n| a   | b   |\n\n"
            "```python\ndef f(x): return x | 0\n```\n\n"
            "---\n\n"
            "Em-dash here — and a stray : colon and a {brace} too."
        )
        consequences_unbalanced_fence = "Consequences:\n```\nunbalanced fence start\n"

        params = dict(
            _VALID_ADR_PARAMS,
            directory=project_dir,
            title="Harden adr_add | multi-line: test",
            context=nasty_context,
            consequences=consequences_unbalanced_fence,
            alternatives="## Alt 1\n- foo\n## Alt 2\n- bar",
        )

        with (
            patch("yadgar.core.server.tools.adr._resolve_project_root", return_value=project_dir),
            patch("yadgar.core.server.tools.adr._get_default_branch", return_value="master"),
        ):
            r1 = adr_add(**params)
            r2 = adr_add(**dict(params, title="Second multi-line ADR"))

        assert r1.get("adr_id") == "ADR-0001", f"First call: {r1}"
        assert r2.get("adr_id") == "ADR-0002", f"Second call: {r2}"

        page = wiki_read("myproj-adr-log", directory=project_dir, branch_hint="master")
        content = page.get("content", "")
        # Exactly two real ADR section headers — body markdown must not inject extras
        # or swallow them (unbalanced fence / col-0 ## must be neutralised).
        real_headers = re.findall(r"^## ADR-(\d{4})", content, re.MULTILINE)
        assert real_headers == ["0001", "0002"], (
            f"Multi-line body corrupted header scan, got: {real_headers!r}"
        )
        # No body markdown leaks as a col-0 heading: the only ## headers are the
        # two ADR sections (## ADR-0001 / ## ADR-0002). "## Background", "## Alt 1",
        # "## Alt 2" from field values must be indented off column 0.
        col0_headers = re.findall(r"^(#{2,3} .*)$", content, re.MULTILINE)
        assert all(h.startswith("## ADR-") for h in col0_headers), (
            f"Body markdown leaked as col-0 headings: {col0_headers!r}"
        )
        # Content survives (round-trip): distinctive fragments still present.
        assert "the pipeline broke at step 3" in content, "table/prose content lost"
        assert "def f(x): return x | 0" in content, "fenced code content lost"
        assert "Em-dash here — " in content, "em-dash content lost"


# ── 1e. adr_due signal tests ──────────────────────────────────────────────────


class TestAdrDueSignal:
    """Tests for _apply_adr_signal (unit-tested directly against the function)."""

    def _make_mock_storage(self, adr_ts: float | None = None, active_work_ts: float | None = None):
        """Return a mock storage with configurable wiki/memory timestamps.

        active_work_ts: unix timestamp for _active_work memory (or None = not found)
        adr_ts: unix timestamp for the ADR log wiki page (or None = not found)
        """
        mock = MagicMock()

        def mock_q(query, params=None):
            params = params or {}
            # ADR log: queried via wiki_page table by slug
            slug = params.get("slug", "")
            if "adr-log" in slug:
                if adr_ts is None:
                    return []
                return [{"updated_at": adr_ts}]
            # active_work: queried via memory table by directory + _active_work tag
            if "_active_work" in query or "active_work" in query:
                if active_work_ts is None:
                    return []
                return [{"created_at": active_work_ts}]
            return []

        mock._q.side_effect = mock_q
        return mock

    def test_adr_due_fires_when_active_work_recent_but_adr_log_stale(self):
        """capture_adr action fires when active_work updated recently but ADR log is stale."""
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        # active_work updated 30 min ago, ADR log updated 25 hours ago
        storage = self._make_mock_storage(
            adr_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        assert len(actions) == 1, f"Expected 1 action, got: {actions}"
        assert actions[0]["action"] == "capture_adr", (
            f"Expected action='capture_adr', got: {actions[0]}"
        )

    def test_adr_due_silent_when_adr_log_fresh(self):
        """No capture_adr action when ADR log was updated recently."""
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        # active_work updated 30 min ago, ADR log updated 1 hour ago (fresh)
        storage = self._make_mock_storage(
            adr_ts=now - 1 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        capture_actions = [a for a in actions if a.get("action") == "capture_adr"]
        assert len(capture_actions) == 0, (
            f"Expected no capture_adr when ADR log is fresh, got: {capture_actions}"
        )

    def test_adr_due_silent_when_no_activity(self):
        """No capture_adr action when active_work is absent (no recent session activity)."""
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        # No active_work; ADR log also old
        storage = self._make_mock_storage(
            adr_ts=now - 48 * 3600,
            active_work_ts=None,  # absent
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        capture_actions = [a for a in actions if a.get("action") == "capture_adr"]
        assert len(capture_actions) == 0, (
            f"Expected no capture_adr when active_work absent, got: {capture_actions}"
        )

    def test_adr_due_suggested_call_names_adr_add(self):
        """When capture_adr fires, its suggested_call contains 'adr_add'."""
        from yadgar.core.server.tools.project import _apply_adr_signal

        now = time.time()
        storage = self._make_mock_storage(
            adr_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            _apply_adr_signal("/tmp/testproject", storage, actions)
        assert len(actions) == 1
        suggested = actions[0].get("suggested_call", "")
        assert "adr_add" in suggested, (
            f"suggested_call should contain 'adr_add', got: {suggested!r}"
        )


class TestAgentPromptSignal:
    """Tests for _apply_agent_prompt_signal (ADR-0007 agent-prompt capture nudge).

    Mirrors _apply_adr_signal: activity-gated, freshness-gated, but keyed on the
    GLOBAL agent-prompt TOC page (`agent-prompt-toc`) instead of the ADR log, and
    HARD-gated on AGENT_PROMPT_LIBRARY_ENABLED (silent when the library is off).
    """

    def _make_mock_storage(self, toc_ts: float | None = None, active_work_ts: float | None = None):
        """Mock storage with configurable TOC/active_work timestamps.

        active_work_ts: unix timestamp for _active_work memory (or None = not found)
        toc_ts: unix timestamp for the agent-prompt-toc wiki page (or None = absent)
        """
        mock = MagicMock()

        def mock_q(query, params=None):
            params = params or {}
            slug = params.get("slug", "")
            if "agent-prompt-toc" in slug:
                if toc_ts is None:
                    return []
                return [{"updated_at": toc_ts}]
            if "_active_work" in query or "active_work" in query:
                if active_work_ts is None:
                    return []
                return [{"created_at": active_work_ts}]
            return []

        mock._q.side_effect = mock_q
        return mock

    def test_fires_when_active_work_recent_but_toc_stale(self):
        """capture_agent_prompt fires when active_work is recent but the TOC is stale."""
        from yadgar.core.server.tools.project import _apply_agent_prompt_signal

        now = time.time()
        storage = self._make_mock_storage(
            toc_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_agent_prompt_signal("/tmp/testproject", storage, actions)
        assert len(actions) == 1, f"Expected 1 action, got: {actions}"
        assert actions[0]["action"] == "capture_agent_prompt", (
            f"Expected action='capture_agent_prompt', got: {actions[0]}"
        )

    def test_silent_when_toc_fresh(self):
        """No capture_agent_prompt when the TOC was updated recently."""
        from yadgar.core.server.tools.project import _apply_agent_prompt_signal

        now = time.time()
        storage = self._make_mock_storage(
            toc_ts=now - 1 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_agent_prompt_signal("/tmp/testproject", storage, actions)
        fired = [a for a in actions if a.get("action") == "capture_agent_prompt"]
        assert len(fired) == 0, f"Expected no nudge when TOC is fresh, got: {fired}"

    def test_silent_when_no_activity(self):
        """No capture_agent_prompt when active_work is absent (no session activity)."""
        from yadgar.core.server.tools.project import _apply_agent_prompt_signal

        now = time.time()
        storage = self._make_mock_storage(
            toc_ts=now - 48 * 3600,
            active_work_ts=None,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_agent_prompt_signal("/tmp/testproject", storage, actions)
        fired = [a for a in actions if a.get("action") == "capture_agent_prompt"]
        assert len(fired) == 0, f"Expected no nudge when active_work absent, got: {fired}"

    def test_silent_when_library_disabled(self):
        """KILL-GATE: AGENT_PROMPT_LIBRARY_ENABLED=False → fully silent, even when
        every other trigger condition is met."""
        from yadgar.core.server.tools.project import _apply_agent_prompt_signal

        now = time.time()
        # Conditions that WOULD fire if enabled: recent activity, stale TOC.
        storage = self._make_mock_storage(
            toc_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = False
            _apply_agent_prompt_signal("/tmp/testproject", storage, actions)
        fired = [a for a in actions if a.get("action") == "capture_agent_prompt"]
        assert len(fired) == 0, (
            f"KILL-GATE breached: nudge fired with library disabled, got: {fired}"
        )

    def test_suggested_call_names_agent_prompt_save(self):
        """When it fires, suggested_call is a valid agent_prompt_save call."""
        from yadgar.core.server.tools.project import _apply_agent_prompt_signal

        now = time.time()
        storage = self._make_mock_storage(
            toc_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.ADR_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_agent_prompt_signal("/tmp/testproject", storage, actions)
        assert len(actions) == 1
        suggested = actions[0].get("suggested_call", "")
        # Must name the tool and carry the three required args (pattern/content/directory).
        assert "agent_prompt_save" in suggested, (
            f"suggested_call should contain 'agent_prompt_save', got: {suggested!r}"
        )
        assert "directory=" in suggested and "pattern=" in suggested and "content=" in suggested, (
            f"suggested_call must include pattern/content/directory, got: {suggested!r}"
        )


class TestDispatchPreludeSignal:
    """Tests for _apply_dispatch_prelude_signal (#69 read-side nudge).

    Mirrors TestAgentPromptSignal: activity-gated, freshness-gated, but keyed on
    the _dispatch_prelude marker memory rather than the TOC page, and fires
    use_agent_prompt_library (read side) rather than capture_agent_prompt (write side).
    """

    def _make_mock_storage(
        self,
        prelude_ts: float | None = None,
        active_work_ts: float | None = None,
    ):
        """Mock storage with configurable prelude-marker / active_work timestamps."""
        mock = MagicMock()

        def mock_q(query, params=None):
            params = params or {}
            if "_dispatch_prelude" in query:
                if prelude_ts is None:
                    return []
                return [{"created_at": prelude_ts}]
            if "_active_work" in query or "active_work" in query:
                if active_work_ts is None:
                    return []
                return [{"created_at": active_work_ts}]
            return []

        mock._q.side_effect = mock_q
        return mock

    def test_fires_when_active_work_recent_but_prelude_stale(self):
        """use_agent_prompt_library fires when active_work fresh but prelude marker stale."""
        from yadgar.core.server.tools.project import _apply_dispatch_prelude_signal

        now = time.time()
        storage = self._make_mock_storage(
            prelude_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.DISPATCH_PRELUDE_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_dispatch_prelude_signal("/tmp/testproject", storage, actions)
        assert len(actions) == 1, f"Expected 1 action, got: {actions}"
        assert actions[0]["action"] == "use_agent_prompt_library", (
            f"Expected action='use_agent_prompt_library', got: {actions[0]}"
        )

    def test_fires_when_prelude_marker_absent_but_active_work_present(self):
        """Fires when prelude marker absent (never used) but active_work present."""
        from yadgar.core.server.tools.project import _apply_dispatch_prelude_signal

        now = time.time()
        storage = self._make_mock_storage(
            prelude_ts=None,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.DISPATCH_PRELUDE_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_dispatch_prelude_signal("/tmp/testproject", storage, actions)
        fired = [a for a in actions if a.get("action") == "use_agent_prompt_library"]
        assert len(fired) == 1, f"Expected 1 action when prelude absent, got: {actions}"

    def test_silent_when_prelude_fresh(self):
        """No use_agent_prompt_library when prelude marker was updated recently."""
        from yadgar.core.server.tools.project import _apply_dispatch_prelude_signal

        now = time.time()
        storage = self._make_mock_storage(
            prelude_ts=now - 1 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.DISPATCH_PRELUDE_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_dispatch_prelude_signal("/tmp/testproject", storage, actions)
        fired = [a for a in actions if a.get("action") == "use_agent_prompt_library"]
        assert len(fired) == 0, f"Expected no nudge when prelude fresh, got: {fired}"

    def test_silent_when_no_active_work(self):
        """No use_agent_prompt_library when active_work is absent."""
        from yadgar.core.server.tools.project import _apply_dispatch_prelude_signal

        now = time.time()
        storage = self._make_mock_storage(
            prelude_ts=now - 48 * 3600,
            active_work_ts=None,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.DISPATCH_PRELUDE_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_dispatch_prelude_signal("/tmp/testproject", storage, actions)
        fired = [a for a in actions if a.get("action") == "use_agent_prompt_library"]
        assert len(fired) == 0, f"Expected no nudge when active_work absent, got: {fired}"

    def test_silent_when_library_disabled(self):
        """KILL-GATE: AGENT_PROMPT_LIBRARY_ENABLED=False → fully silent."""
        from yadgar.core.server.tools.project import _apply_dispatch_prelude_signal

        now = time.time()
        storage = self._make_mock_storage(
            prelude_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.DISPATCH_PRELUDE_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = False
            _apply_dispatch_prelude_signal("/tmp/testproject", storage, actions)
        fired = [a for a in actions if a.get("action") == "use_agent_prompt_library"]
        assert len(fired) == 0, (
            f"KILL-GATE breached: nudge fired with library disabled, got: {fired}"
        )

    def test_suggested_call_names_agent_dispatch_prelude(self):
        """When it fires, suggested_call references agent_dispatch_prelude."""
        from yadgar.core.server.tools.project import _apply_dispatch_prelude_signal

        now = time.time()
        storage = self._make_mock_storage(
            prelude_ts=now - 25 * 3600,
            active_work_ts=now - 0.5 * 3600,
        )
        actions: list = []
        with patch("yadgar.core.server.tools.project.get_settings") as mock_settings:
            mock_settings.return_value.DISPATCH_PRELUDE_DUE_WARN_HOURS = 12.0
            mock_settings.return_value.AGENT_PROMPT_LIBRARY_ENABLED = True
            _apply_dispatch_prelude_signal("/tmp/testproject", storage, actions)
        assert len(actions) == 1
        suggested = actions[0].get("suggested_call", "")
        assert "agent_dispatch_prelude" in suggested, (
            f"suggested_call should contain 'agent_dispatch_prelude', got: {suggested!r}"
        )
