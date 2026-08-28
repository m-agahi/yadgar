"""TDD (RED-first) — S6: agent-prompt discovery surface + kill-gate rewire.

0047 Car I REWRITE: the discovery surface is now the ``agent_pattern`` ledger
table (D40) — ``agent_prompt_list`` / ``agent_prompt_get`` MCP tools reach
the table via ``list_agent_pattern_rows_uses_desc`` /
``get_agent_pattern_row`` backend ops. The wiki-TOC page and library anchor
are RETIRED (D35a — kept-ignored pointer slug for one cycle). The kill-gate
(AGENT_PROMPT_LIBRARY_ENABLED) is unchanged in shape: when False, all three
library surfaces go inert.

Coverage:
  BC-S6-LIST       agent_prompt_save persists a row in ``agent_pattern``;
                   agent_prompt_list returns it in uses-DESC order
  BC-S6-GET        agent_prompt_get returns the row + the wiki body in one
                   round-trip
  BC-S6-BRIEF      project_brief(mode='restore') surfaces ``agent_prompt_toc``
                   (slug + capped pattern list) sourced from the table
  BC-S6-KILLGATE   AGENT_PROMPT_LIBRARY_ENABLED=False makes the library INERT
                   across list/get/project_brief/dispatch_prelude
"""

from __future__ import annotations

import sys

import pytest

from yadgar.core import server  # noqa: E402
from yadgar.tests.core.conftest import TEST_PROJECT_ID

# R3 Car 3c: agent_prompt_save forwards its DB write to the backend /admin endpoint.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Real StorageEngine + WikiStore + replay wired via init_engines."""
    tmp_path = tmp_path_factory.mktemp("agent_prompt_discovery_s")
    server.init_engines(
        db_path=str(tmp_path / "test_agent_prompt_discovery_s6.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture(autouse=True)
def _ledger_row_mocks():
    """In-memory dict mocks for the 7 new ledger ops (no engine #2 in unit tests).

    The conftest's ``_unit_backend_harness`` autouse fixture routes
    ``_forward_admin`` to ``run_admin_op_blocking`` (in-process). The real
    ledger ops return ``{ok: False, error: 'engine #2 not composed...'}`` when
    no MariaDB is composed — which would block our ``save → list → get``
    round-trip. Patching the registry entries with in-memory equivalents
    preserves the round-trip and lets ``uses`` ordering assertions stand.
    """
    rows: dict[str, dict] = {}
    composes: dict[str, list[tuple[str, int]]] = {}

    def _save_agent_pattern_row(payload):
        name = payload["name"]
        rows[name] = {
            "name": name,
            "body_slug": payload.get("body_slug") or f"agent-prompt-{name}",
            "purpose": payload.get("purpose", ""),
            "status": payload.get("status", "active"),
            "content_hash": payload.get("content_hash", ""),
            "baseline_hash": payload.get("baseline_hash"),
            "uses": rows.get(name, {}).get("uses", 0),
        }
        return rows[name]

    def _save_agent_discipline_row(payload):
        return {"ok": True, "name": payload["name"]}

    def _get_agent_pattern_row(payload):
        return {"row": rows.get(payload["name"])}

    def _list_agent_pattern_rows_uses_desc(payload):
        sorted_rows = sorted(
            rows.values(),
            key=lambda r: (-r.get("uses", 0), r.get("name", "")),
        )
        return {"rows": sorted_rows[: int(payload.get("limit", 20))]}

    def _increment_agent_pattern_uses(payload):
        name = payload["pattern"]
        if name in rows:
            rows[name]["uses"] = rows[name].get("uses", 0) + 1
        return {"ok": True, "pattern": name}

    def _list_pattern_composes(payload):
        return {
            "rows": [
                {"pattern_name": payload["pattern_name"], "discipline_name": dn, "position": p}
                for dn, p in composes.get(payload["pattern_name"], [])
            ]
        }

    def _get_agent_prompt_toc_updated_at(payload):  # noqa: ARG001
        return {"timestamp": None}

    import yadgar.backend.admin_exec as _exec_module
    import yadgar.backend.admin_exec.ledger_agent as _ledger_module

    _originals = {
        "save_agent_pattern_row": _ledger_module.save_agent_pattern_row,
        "save_agent_discipline_row": _ledger_module.save_agent_discipline_row,
        "get_agent_pattern_row": _ledger_module.get_agent_pattern_row,
        "list_agent_pattern_rows_uses_desc": _ledger_module.list_agent_pattern_rows_uses_desc,
        "increment_agent_pattern_uses": _ledger_module.increment_agent_pattern_uses,
        "list_pattern_composes": _ledger_module.list_pattern_composes,
        "get_agent_prompt_toc_updated_at": _ledger_module.get_agent_prompt_toc_updated_at,
    }
    _exec_module._ADMIN_OPS["save_agent_pattern_row"] = _save_agent_pattern_row
    _exec_module._ADMIN_OPS["save_agent_discipline_row"] = _save_agent_discipline_row
    _exec_module._ADMIN_OPS["get_agent_pattern_row"] = _get_agent_pattern_row
    _exec_module._ADMIN_OPS["list_agent_pattern_rows_uses_desc"] = (
        _list_agent_pattern_rows_uses_desc
    )
    _exec_module._ADMIN_OPS["increment_agent_pattern_uses"] = _increment_agent_pattern_uses
    _exec_module._ADMIN_OPS["list_pattern_composes"] = _list_pattern_composes
    _exec_module._ADMIN_OPS["get_agent_prompt_toc_updated_at"] = _get_agent_prompt_toc_updated_at
    yield rows
    # Restore the original (real) op entries — other test files share the
    # module-level _ADMIN_OPS dict and depend on the real implementations.
    for name, op in _originals.items():
        _exec_module._ADMIN_OPS[name] = op


def _save(pattern: str, content: str, purpose: str | None = None) -> dict:
    from yadgar.core.server.tools.agent_prompts import agent_prompt_save

    res = agent_prompt_save(
        pattern, content, directory="global", purpose=purpose, project=TEST_PROJECT_ID
    )
    assert res.get("saved") is True, f"agent_prompt_save failed: {res}"
    return res


def _read_pattern_row(pattern: str) -> dict | None:
    """Return the agent_pattern ledger row, or None if absent."""

    from yadgar.core.server.tools.agent_prompts import agent_prompt_get

    res = agent_prompt_get(pattern, directory="global")
    if res.get("error") == "not_found":
        return None
    return res


# ---------------------------------------------------------------------------
# BC-S6-LIST — agent_prompt_list reaches the agent_pattern ledger table
# ---------------------------------------------------------------------------


class TestBC_S6_List:
    def test_save_persists_row(self):
        _save("review-diff", "Review the diff.", purpose="Severity-tagged diff review.")
        row = _read_pattern_row("review-diff")
        assert row is not None, "agent_pattern row was not created on save"
        assert row["name"] == "review-diff"
        assert row["purpose"] == "Severity-tagged diff review."
        assert row["body_slug"] == "agent-prompt-review-diff"

    def test_resave_updates_row_no_dupes(self):
        _save("review-diff", "v1 body", purpose="First purpose line.")
        _save("review-diff", "v2 body", purpose="Updated purpose line.")
        row = _read_pattern_row("review-diff")
        assert row is not None
        assert row["purpose"] == "Updated purpose line."
        assert row["purpose"] != "First purpose line."
        assert row["content"].endswith("v2 body")

    def test_multiple_patterns_listed(self):
        _save("review-diff", "a", purpose="Review.")
        _save("plan-feature", "b", purpose="Plan.")
        from yadgar.core.server.tools.agent_prompts import agent_prompt_list

        listing = agent_prompt_list(directory="global", limit=20)
        names = [p["name"] for p in listing["patterns"]]
        assert "review-diff" in names
        assert "plan-feature" in names

    def test_list_orders_by_uses_desc(self):
        # ``uses`` is bumped per dispatch, not per save (D40). Three
        # dispatch_helper calls against ``popular-pat`` move it to uses=3.
        _save("never-used-pat", "a", purpose="Never.")
        _save("popular-pat", "a", purpose="Popular.")
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        agent_dispatch_prelude(
            "popular-pat", "topic", storage=_st._storage, project=TEST_PROJECT_ID
        )
        agent_dispatch_prelude(
            "popular-pat", "topic", storage=_st._storage, project=TEST_PROJECT_ID
        )
        agent_dispatch_prelude(
            "popular-pat", "topic", storage=_st._storage, project=TEST_PROJECT_ID
        )
        from yadgar.core.server.tools.agent_prompts import agent_prompt_list

        listing = agent_prompt_list(directory="global", limit=20)
        by_name = {p["name"]: p["uses"] for p in listing["patterns"]}
        assert by_name.get("popular-pat", 0) >= 3
        assert by_name.get("never-used-pat", 0) == 0


# ---------------------------------------------------------------------------
# BC-S6-GET — agent_prompt_get returns row + body in one round-trip
# ---------------------------------------------------------------------------


class TestBC_S6_Get:
    def test_get_returns_row_and_body(self):
        _save("review-diff", "Review body.", purpose="Review.")
        row = _read_pattern_row("review-diff")
        assert row is not None
        assert row["name"] == "review-diff"
        assert "Review body." in row["content"]
        assert row["body_slug"] == "agent-prompt-review-diff"
        assert row["page_id"] is not None
        assert row["version"] >= 1
        assert row["content_hash"], "content_hash must be populated"

    def test_get_unknown_pattern_not_found(self):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_get

        res = agent_prompt_get("never-existed-pattern-xyz", directory="global")
        assert res.get("error") == "not_found"
        assert res["name"] == "never-existed-pattern-xyz"


# ---------------------------------------------------------------------------
# BC-S6-BRIEF — project_brief restore surfaces the agent_prompt_toc block
# sourced from the ledger table (pointer slug is the kept-ignored slug).
# ---------------------------------------------------------------------------


class TestBC_S6_Brief:
    def test_restore_surfaces_agent_prompt_toc(self):
        _save("review-diff", "a", purpose="Review.")
        _save("plan-feature", "b", purpose="Plan.")
        # Unrelated project dir — the library is global, must still surface.
        result = server.project_brief(
            "/tmp/some_unrelated_proj_s6", mode="restore", project=TEST_PROJECT_ID
        )
        assert "agent_prompt_toc" in result, (
            f"agent_prompt_toc missing from restore keys: {list(result.keys())}"
        )
        toc = result["agent_prompt_toc"]
        # Car I: the kept-ignored pointer slug (D35d); the patterns are table-sourced.
        assert toc["slug"] == "agent-prompt-toc"
        assert "review-diff" in toc["patterns"]
        assert "plan-feature" in toc["patterns"]
        # Cheap: no full body, only the pattern list.
        assert "body" not in toc

    def test_restore_toc_empty_when_no_library(self):
        result = server.project_brief(
            "/tmp/empty_lib_proj_s6", mode="restore", project=TEST_PROJECT_ID
        )
        assert "agent_prompt_toc" in result
        assert result["agent_prompt_toc"]["patterns"] == []


# ---------------------------------------------------------------------------
# BC-S6-KILLGATE — flag False => library inert across all surfaces
# ---------------------------------------------------------------------------


def _recall_fn():
    return sys.modules["yadgar.core.server.tools.recall"].recall


class TestBC_S6_KillGate:
    def _setup(self, monkeypatch):
        _save(
            "review-pr-security",
            "Review a pull request for security vulnerabilities. Check for injection, "
            "auth bypass, and secret leakage. Report findings with severity.",
            purpose="Security PR review.",
        )

    def _set_flag(self, monkeypatch, value: bool):
        """Flip AGENT_PROMPT_LIBRARY_ENABLED on the cached settings singleton."""
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "AGENT_PROMPT_LIBRARY_ENABLED", value)

    def test_flag_off_recall_include_inert(self, monkeypatch, recall_backend_bypass):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, False)
        results = _recall_fn()(
            query="audit this pull request for vulnerabilities",
            type="wiki",
            tags=["agent-prompt"],
            directory="global",
            project=TEST_PROJECT_ID,
        )
        assert results == [] or all("agent-prompt" not in (r.get("tags") or []) for r in results), (
            f"flag-off tagged recall must be inert, got: {results}"
        )

    def test_flag_on_recall_include_surfaces(self, monkeypatch, recall_backend_bypass):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, True)
        results = _recall_fn()(
            query="audit this pull request for vulnerabilities",
            type="wiki",
            tags=["agent-prompt"],
            directory="global",
            project=TEST_PROJECT_ID,
        )
        assert results, "flag-on tagged recall must return the agent-prompt page"
        assert any("agent-prompt" in (r.get("tags") or []) for r in results)

    def test_flag_off_project_brief_no_toc(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, False)
        result = server.project_brief(
            "/tmp/killgate_proj_s6", mode="restore", project=TEST_PROJECT_ID
        )
        assert "agent_prompt_toc" not in result, "flag-off restore must suppress agent_prompt_toc"

    def test_flag_on_project_brief_has_toc(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, True)
        result = server.project_brief(
            "/tmp/killgate_proj_s6", mode="restore", project=TEST_PROJECT_ID
        )
        assert "agent_prompt_toc" in result

    def test_flag_off_dispatch_prelude_no_prompt(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, False)
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "review-pr-security", "review task", storage=_st._storage, project=TEST_PROJECT_ID
        )
        assert "Agent-prompt" not in prelude, (
            f"flag-off dispatch prelude must inject no prompt, got:\n{prelude}"
        )

    def test_flag_on_dispatch_prelude_injects_prompt(self, monkeypatch):
        self._setup(monkeypatch)
        self._set_flag(monkeypatch, True)
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "review-pr-security", "review task", storage=_st._storage, project=TEST_PROJECT_ID
        )
        assert "Agent-prompt" in prelude
