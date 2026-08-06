"""TDD (RED-first) — discipline_save MCP tool + ADR-0208 asymmetric removal guard.

Context: _save_discipline_page (agent_prompts.py) was already a working
upsert, but had NO MCP exposure — its only caller was the seeder
(_seed_discipline_pages, create-if-absent). This is the write-path
prerequisite ADR-0208 calls out ("The whole flow depends on exposing a
discipline write path on the MCP surface").

Guard under test (ADR-0208, precise definition): compare the '## Prompt'
body of the existing page against the incoming one. If every non-empty
existing line survives in the new content, it's additions-only -> allow.
If any non-empty existing line is absent -> REMOVAL -> reject unless
confirm_removal=True; the rejection must name the removed line(s).
Creating a page that does not exist yet is never a removal.

Explicitly OUT of scope here (later car per ADR-0209): baseline_hash,
content_hash, drift detection, three-way merge.

Tests:
  1. test_create_new_discipline_works       — fresh name -> page created, retrievable
  2. test_additions_only_update_allowed     — appending a line needs no ratification
  3. test_removal_rejected_without_confirmation — dropping a line is rejected,
                                                    names the removed line, no write happens
  4. test_removal_with_confirmation_succeeds — same removal + confirm_removal=True -> succeeds
  5. test_seeder_still_create_if_absent     — seed_agent_prompts never overwrites a
                                                discipline page that already exists
  6. registration                           — discipline_save is a live MCP tool
"""

from __future__ import annotations

import pytest

from yadgar.core import server

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("discipline_save")
    server.init_engines(
        db_path=str(tmp_path / "test_discipline_save.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def storage():
    import yadgar._shared.runtime.state as _st

    return _st._storage


class TestDisciplineSaveCreate:
    def test_create_new_discipline_works(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, discipline_save

        result = discipline_save(
            "zz-probe-create",
            "Rule one.\nRule two.",
            purpose="A probe discipline.",
        )
        assert result["saved"] is True
        assert result["version"] == 1

        page = _read_agent_prompt("agent-discipline-zz-probe-create", storage=storage)
        assert page is not None, "discipline page not retrievable after create"
        assert "Rule one." in page["content"]
        assert "Rule two." in page["content"]
        assert "agent-discipline" in page["tags"]


class TestDisciplineSaveAdditionsOnly:
    def test_additions_only_update_allowed(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, discipline_save

        discipline_save("zz-probe-additions", "Rule one.\nRule two.", purpose="probe")
        result = discipline_save(
            "zz-probe-additions",
            "Rule one.\nRule two.\nRule three.",
            purpose="probe",
        )
        assert result.get("error") is None, f"additions-only update rejected: {result}"
        assert result["saved"] is True
        assert result["version"] == 2

        page = _read_agent_prompt("agent-discipline-zz-probe-additions", storage=storage)
        assert "Rule one." in page["content"]
        assert "Rule two." in page["content"]
        assert "Rule three." in page["content"]

    def test_omitted_purpose_on_update_reuses_stored_purpose(self, storage):
        """discipline_save is the write path — it must not silently clobber
        the stored purpose when the caller updates content without passing
        purpose= again."""
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, discipline_save

        discipline_save(
            "zz-probe-purpose-reuse",
            "Rule one.",
            purpose="A specific, hand-written purpose.",
        )
        result = discipline_save(
            "zz-probe-purpose-reuse",
            "Rule one.\nRule two.",
        )
        assert result.get("error") is None, f"additions-only update rejected: {result}"
        assert result["saved"] is True

        page = _read_agent_prompt("agent-discipline-zz-probe-purpose-reuse", storage=storage)
        assert "A specific, hand-written purpose." in page["content"], (
            f"purpose was clobbered by the generic default:\n{page['content']}"
        )


class TestDisciplineSaveRemovalGuard:
    def test_removal_rejected_without_confirmation(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, discipline_save

        discipline_save(
            "zz-probe-removal",
            "Rule one.\nRule two.\nRule three.",
            purpose="probe",
        )
        result = discipline_save(
            "zz-probe-removal",
            "Rule one.\nRule three.",
            purpose="probe",
        )
        assert result.get("saved") is False
        assert result.get("error") == "removal_requires_confirmation"
        assert "Rule two." in result.get("removed_lines", []), (
            f"rejection must name the removed line: {result}"
        )

        # No write happened — page must still be at version 1 with the original content.
        page = _read_agent_prompt("agent-discipline-zz-probe-removal", storage=storage)
        assert page["version"] == 1
        assert "Rule two." in page["content"]

    def test_removal_with_confirmation_succeeds(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, discipline_save

        discipline_save(
            "zz-probe-removal-confirmed",
            "Rule one.\nRule two.\nRule three.",
            purpose="probe",
        )
        result = discipline_save(
            "zz-probe-removal-confirmed",
            "Rule one.\nRule three.",
            purpose="probe",
            confirm_removal=True,
        )
        assert result.get("saved") is True, f"ratified removal must succeed: {result}"
        assert result["version"] == 2

        page = _read_agent_prompt("agent-discipline-zz-probe-removal-confirmed", storage=storage)
        assert "Rule two." not in page["content"]
        assert "Rule one." in page["content"]
        assert "Rule three." in page["content"]


class TestSeederStillCreateIfAbsent:
    def test_seeder_does_not_overwrite_existing_discipline(self, storage):
        """A discipline page written via discipline_save (even a genesis name)
        must survive a subsequent seed_agent_prompts call untouched."""
        from yadgar.core.server.tools.agent_prompts import (
            _read_agent_prompt,
            discipline_save,
            seed_agent_prompts,
        )

        discipline_save(
            "recall-first",
            "Custom recall-first rule one.\nCustom recall-first rule two.",
            purpose="custom override",
        )
        seed_agent_prompts(storage=storage)

        page = _read_agent_prompt("agent-discipline-recall-first", storage=storage)
        assert page is not None
        assert page["version"] == 1, "seeding must not have written a new version"
        assert "Custom recall-first rule one." in page["content"]


class TestDisciplineSaveRegistered:
    def test_discipline_save_in_all(self):
        from yadgar.core.server import tools

        assert "discipline_save" in tools.__all__, (
            "discipline_save missing from yadgar.server.tools.__all__ — "
            "tools/__init__.py has no autodiscovery, a decorated function is "
            "not a live tool until it is listed here"
        )

    def test_discipline_save_on_module(self):
        import yadgar.core.server.tools.agent_prompts as m

        assert hasattr(m, "discipline_save")
