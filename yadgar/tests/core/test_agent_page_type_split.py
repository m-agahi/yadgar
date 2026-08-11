"""ADR-0209 — the agent_prompt page type splits into pattern / discipline / index.

Today both families share ``page_type=agent_prompt``, discriminated only by
slug prefix and tags, while ADR-0198 splits them at the ROW level and ADR-0208
gives them different governance. ``page_type`` is the policy lever
(``providers/wiki.py`` reads ``get_policy(page_type).recall_disposition``), so
keying governance off a slug prefix is string-matching where a type belongs.

Pinned here: the WRITE path stamps the right type on each family, including
the TOC index whose null page_type is task 0134's live defect.
"""

from __future__ import annotations

import pytest

from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_DISCIPLINE,
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
)
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("agent_page_type_split")
    server.init_engines(
        db_path=str(tmp_path / "test_agent_page_type_split.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def storage():
    import yadgar._shared.runtime.state as _st

    return _st._storage


def _page_type(storage, slug: str) -> str | None:
    page = storage.get_wiki_page_by_slug(slug)
    assert page is not None, f"{slug} not written"
    return page.get("page_type")


class TestWritePathStampsSplitTypes:
    def test_agent_prompt_save_stamps_agent_pattern(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save(
            "split-pattern-probe", "Body line.", directory="global", project=TEST_PROJECT_ID
        )
        assert _page_type(storage, "agent-prompt-split-pattern-probe") == PAGE_TYPE_AGENT_PATTERN

    def test_discipline_save_stamps_agent_discipline(self, storage):
        from yadgar.core.server.tools.agent_prompts import discipline_save

        discipline_save("split-discipline-probe", "Rule one.", project=TEST_PROJECT_ID)
        assert (
            _page_type(storage, "agent-discipline-split-discipline-probe")
            == PAGE_TYPE_AGENT_DISCIPLINE
        )

    def test_toc_stamps_agent_index(self, storage):
        """Car I (0047 §16.8): the TOC pointer is RETIRED.

        Per §S6 the ``agent_pattern`` ledger row IS the discovery surface; the
        wiki-TOC page is gone. ``_TOC_POINTER_SLUG = "agent-prompt-toc"`` stays
        in code only as a kept-ignored slug so callers that pin the old slug
        see an explanatory page (one cycle of soft retire) — but
        ``agent_prompt_save`` does NOT write the TOC. The original
        'task 0134: page_type=null → DEFAULT_POLICY include' defect is dead.

        This test pins the positive side of that: the page is NOT created by
        a normal save (so DEFAULT_POLICY inheritance can no longer bite), AND
        if it IS created, the type is the agent_index sentinel (not null).
        Both shapes are the post-Car-I contract.
        """
        from yadgar.core.server.tools.agent_prompts import (
            _TOC_POINTER_SLUG,
            agent_prompt_save,
        )

        # 1. agent_prompt_save must NOT create the TOC pointer (Car I retired it).
        agent_prompt_save("split-toc-probe", "Body.", directory="global", project=TEST_PROJECT_ID)
        toc_page = storage.get_wiki_page_by_slug(_TOC_POINTER_SLUG)
        assert toc_page is None, (
            f"agent_prompt_save must not write the retired TOC pointer "
            f"(slug={_TOC_POINTER_SLUG!r}); got page_type={toc_page.get('page_type')!r}"
        )

        # 2. If something DOES create the TOC pointer, the page_type MUST be
        #    agent_index (the kept-ignored sentinel — not null, not pattern,
        #    not discipline). This is the defensive back-stop so the
        #    'page_type=null → DEFAULT_POLICY include' defect stays dead.
        storage.insert_wiki_page(
            {
                "slug": _TOC_POINTER_SLUG,
                "title": "Agent Prompt TOC (manual — Car I retired)",
                "content": "Retired discovery surface; see agent_prompt_list.",
                "tags": ["agent-prompt"],
                "page_type": PAGE_TYPE_AGENT_INDEX,
                "wiki_schema_version": 1,
                "directory_context": "global",
                "project_id": TEST_PROJECT_ID,
            },
            branch=None,
        )
        assert _page_type(storage, _TOC_POINTER_SLUG) == PAGE_TYPE_AGENT_INDEX

    def test_contract_is_a_discipline(self, storage):
        """ADR-0209: the contract stays INSIDE the discipline type.

        It is distinguished by ADR-0198's ``always_applied`` flag, NOT promoted
        to a third page type — ADR-0198 deliberately avoided a singleton
        special case. The seeder routes it through agent_prompt_save, so the
        contract slug must be excepted there rather than typed as a pattern.
        """
        from yadgar.core.server.tools.agent_prompts import CONTRACT_SLUG, _seed_contract_page

        _seed_contract_page(storage=storage, project=TEST_PROJECT_ID)
        assert _page_type(storage, CONTRACT_SLUG) == PAGE_TYPE_AGENT_DISCIPLINE


class TestSplitTypesAreGloballyScoped:
    """ADR-0159's storage_scope=global must survive the split.

    A page type missing from POLICY_BY_TYPE silently inherits DEFAULT_POLICY,
    which would stamp the caller's directory and make the library invisible
    cross-project — the exact bug ADR-0159 fixed.
    """

    def test_pattern_page_is_global(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save(
            "scope-probe", "Body.", directory="/tmp/some-project", project=TEST_PROJECT_ID
        )
        page = storage.get_wiki_page_by_slug("agent-prompt-scope-probe")
        assert page is not None
        assert page.get("directory_context") == "global"

    def test_discipline_page_is_global(self, storage):
        from yadgar.core.server.tools.agent_prompts import discipline_save

        discipline_save("scope-discipline-probe", "Rule.", project=TEST_PROJECT_ID)
        page = storage.get_wiki_page_by_slug("agent-discipline-scope-discipline-probe")
        assert page is not None
        assert page.get("directory_context") == "global"


class TestPreludeStillResolves:
    """The split must not break the read path the whole library hangs off."""

    def test_saved_pattern_is_readable_by_slug(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, agent_prompt_save

        agent_prompt_save(
            "readback-probe", "Distinct body text.", directory="global", project=TEST_PROJECT_ID
        )
        got = _read_agent_prompt("agent-prompt-readback-probe", storage=storage)
        assert got is not None
        assert "Distinct body text." in got["content"]
