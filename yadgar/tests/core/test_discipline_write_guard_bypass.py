"""Task 23 — ADR-0208's removal guard must cover every write path, not one door.

Car 8 added ``discipline_save`` with the asymmetric guard (additions flow, net
removals need ``confirm_removal=True``). But that guard protected only its own
front door: ``wiki_delete_text``, ``wiki_replace_text``, ``wiki_append_section``
and the positional edit family all resolve ``agent-discipline-*`` slugs like any
other page and could strip rule lines with ZERO ratification — which makes the
front-door guard theatre, since the same instance holds the bypass tools.

ADR-0209's page-type split is what gives the write path something to gate on:
the guard keys on ``page_type == agent_discipline``, not a slug prefix.

Scope pinned here, and deliberately:
- ADDITIONS still flow (ADR-0208 is asymmetric, not a ban).
- ``wiki_restore`` is EXEMPT. ADR-0208's own consequences lean on it as the
  recovery path for auto-applied merges ("every apply creates a version, so
  wiki_restore is one call away"); reverting to a previously-ratified version
  is not an unratified weakening.
- Non-discipline pages are untouched.
"""

from __future__ import annotations

import pytest

from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_AGENT_DISCIPLINE
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

# Rule lines are deliberately long: the positional ops enforce anchor_hint >= 20
# chars, so a short rule cannot be addressed by replace_at / delete_at at all.
_RULE_1 = "Rule one: write the failing test before the implementation."
_RULE_2 = "Rule two: never bypass a pre-commit hook when it fails."
_RULE_3 = "Rule three: never add co-author trailers to a commit."
_BODY = f"## Purpose\n\nGuard probe.\n\n## Prompt\n\n{_RULE_1}\n{_RULE_2}\n{_RULE_3}\n"
#: 1-indexed line of _RULE_2 in _BODY (## Purpose, blank, prose, blank,
#: ## Prompt, blank, rule 1, rule 2).
_RULE_2_LINE = 8


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("discipline_write_guard")
    server.init_engines(
        db_path=str(tmp_path / "test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def wiki():
    import yadgar._shared.runtime.state as _st

    return _st._wiki


def _storage():
    """The live StorageEngine, narrowed — the module fixture guarantees it."""
    import yadgar._shared.runtime.state as _st

    storage = _st._storage
    assert storage is not None, "init_engines did not register a storage engine"
    return storage


def _make_page(slug: str, page_type: str | None) -> int:
    """Insert a page directly so the test controls page_type exactly."""
    storage = _storage()
    existing = storage.get_wiki_page_by_slug(slug)
    if existing is not None:
        page_id = storage._extract_id(existing.get("id"))
        storage.update_wiki_page(page_id, {"content": _BODY})
        return page_id
    row = {
        "slug": slug,
        "title": slug,
        "content": _BODY,
        "tags": ["agent-prompt", "agent-discipline"],
        "links": [],
        "category": "reference",
        "confidence": "high",
        "source_memory_ids": [],
        "directory_context": "global",
        "project_id": TEST_PROJECT_ID,
        "wiki_schema_version": 1,
    }
    if page_type is not None:
        row["page_type"] = page_type
    return storage.insert_wiki_page(row)


def _content(page_id: int) -> str:
    page = _storage().get_wiki_page(page_id)
    assert page is not None
    return page.get("content", "")


class TestRemovalsAreBlocked:
    """Every content-mutating WikiStore op refuses a net removal."""

    def test_delete_text_blocked(self, wiki):
        pid = _make_page("agent-discipline-guard-delete", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.delete_text(pid, _RULE_2 + "\n")
        assert result["ok"] is False
        assert result["error"] == "discipline_removal_requires_confirmation"
        assert _RULE_2 in result["removed_lines"]
        assert _content(pid) == _BODY, "rejected write must not touch the page"

    def test_replace_text_blocked(self, wiki):
        pid = _make_page("agent-discipline-guard-replace", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.replace_text(pid, _RULE_2, "Rule two: bypass hooks freely.")
        assert result["ok"] is False
        assert result["error"] == "discipline_removal_requires_confirmation"
        assert _content(pid) == _BODY

    def test_replace_at_blocked(self, wiki):
        pid = _make_page("agent-discipline-guard-replace-at", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.replace_at(
            pid, _RULE_2_LINE, 1, len(_RULE_2), "Rule two: bypass freely.", _RULE_2
        )
        assert result["ok"] is False
        assert result["error"] == "discipline_removal_requires_confirmation"
        assert _content(pid) == _BODY

    def test_delete_at_blocked(self, wiki):
        pid = _make_page("agent-discipline-guard-delete-at", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.delete_at(pid, _RULE_2_LINE, 1, len(_RULE_2), _RULE_2)
        assert result["ok"] is False
        assert result["error"] == "discipline_removal_requires_confirmation"
        assert _content(pid) == _BODY

    def test_append_section_replace_blocked(self, wiki):
        """replace_section is the append-family position that CAN remove."""
        pid = _make_page("agent-discipline-guard-append-replace", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.append_section(pid, "Prompt", _RULE_1 + "\n", "replace_section")
        assert result.get("error") == "discipline_removal_requires_confirmation"
        assert _content(pid) == _BODY


class TestWikiUpdateCovered:
    """wiki_update never enters WikiStore, so it needs its own pre-check.

    ``backend/admin_exec/wiki.py::wiki_update`` calls ``storage.update_wiki_page``
    directly — the store-level chokepoint cannot see it, and ``content`` is in
    the tool's allowed-keys list, so one call could strip every rule line. The
    guard therefore also sits in the ``@_tool`` shell. That shell is a disjoint
    entry point from ``discipline_save`` (which goes ``_save_discipline_page`` →
    ``_forward_admin("agent_prompt_save")`` → ``wiki.add``), so this cannot
    double-gate the sanctioned path.
    """

    def test_content_removal_blocked(self):
        from yadgar.core.server.tools.admin_other import wiki_update

        pid = _make_page("agent-discipline-guard-update", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki_update(pid, {"content": "## Prompt\n\n" + _RULE_1 + "\n"})
        assert result.get("error") == "discipline_removal_requires_confirmation"
        assert _RULE_2 in result["removed_lines"]
        assert _content(pid) == _BODY, "rejected write must not touch the page"

    def test_content_addition_allowed(self):
        from yadgar.core.server.tools.admin_other import wiki_update

        pid = _make_page("agent-discipline-guard-update-add", PAGE_TYPE_AGENT_DISCIPLINE)
        wiki_update(pid, {"content": _BODY + "Rule four: and one more.\n"})
        assert "Rule four: and one more." in _content(pid)

    def test_non_content_fields_unaffected(self):
        """Only `content` can remove a rule — a tag edit must still work."""
        from yadgar.core.server.tools.admin_other import wiki_update

        pid = _make_page("agent-discipline-guard-update-tags", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki_update(pid, {"tags": ["agent-prompt", "agent-discipline", "extra"]})
        assert result.get("error") != "discipline_removal_requires_confirmation"

    def test_non_discipline_page_unaffected(self):
        from yadgar.core.server.tools.admin_other import wiki_update

        pid = _make_page("plain-guard-update-control", None)
        wiki_update(pid, {"content": "wiped\n"})
        assert _content(pid) == "wiped\n"


class TestAdditionsFlow:
    """ADR-0208 is asymmetric — the guard must not become a write ban."""

    def test_append_section_end_allowed(self, wiki):
        pid = _make_page("agent-discipline-guard-append-ok", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.append_section(pid, "Prompt", "Rule four: keep edits surgical.\n")
        assert result.get("error") is None
        assert "Rule four: keep edits surgical." in _content(pid)
        assert _RULE_2 in _content(pid)

    def test_insert_after_allowed(self, wiki):
        pid = _make_page("agent-discipline-guard-insert", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.insert_after(pid, _RULE_2, "\nRule two-and-a-half: and mean it.")
        assert result["ok"] is True
        assert "Rule two-and-a-half: and mean it." in _content(pid)

    def test_replace_text_that_only_adds_allowed(self, wiki):
        """A replacement whose old line survives verbatim is an addition."""
        pid = _make_page("agent-discipline-guard-replace-add", PAGE_TYPE_AGENT_DISCIPLINE)
        result = wiki.replace_text(pid, _RULE_2, _RULE_2 + "\nRule two-bis: also this.")
        assert result["ok"] is True
        assert "Rule two-bis: also this." in _content(pid)


class TestScope:
    def test_non_discipline_page_unguarded(self, wiki):
        """A plain page keeps its ordinary edit semantics."""
        pid = _make_page("plain-guard-control", None)
        result = wiki.delete_text(pid, _RULE_2 + "\n")
        assert result["ok"] is True
        assert _RULE_2 not in _content(pid)

    def test_pattern_page_unguarded(self, wiki):
        """Only DISCIPLINES carry the guard — ADR-0208 scopes it to rule sets."""
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_AGENT_PATTERN

        pid = _make_page("agent-prompt-guard-control", PAGE_TYPE_AGENT_PATTERN)
        result = wiki.delete_text(pid, _RULE_2 + "\n")
        assert result["ok"] is True

    def test_restore_version_exempt(self, wiki):
        """ADR-0208 names wiki_restore as the recovery path — it must not block.

        Restoring reverts to content that was already ratified when written,
        so it is not an unratified weakening. Blocking it would break the
        mitigation ADR-0208 explicitly relies on for auto-applied merges.
        """
        pid = _make_page("agent-discipline-guard-restore", PAGE_TYPE_AGENT_DISCIPLINE)
        wiki.insert_after(pid, _RULE_3, "\nRule four: transient addition.")
        result = wiki.restore_version(pid, 1)
        assert result.get("error") is None
        assert "Rule four: transient addition." not in _content(pid)
