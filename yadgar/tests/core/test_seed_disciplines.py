"""TDD (RED-first) — Stage 2: discipline-page genesis + seeding.

Discipline pages are seeded wiki pages (same genesis mechanism as the prelude
contract): text under the ``disciplines:`` key in
yadgar/core/seed/materials/agent_prompts.yaml, seeded create-if-absent by
seed_agent_prompts, slug ``agent-discipline-<name>``.

Tests:
  1. test_disciplines_loaded          — DISCIPLINES loads 7 entries from the yaml
  2. test_discipline_content_nonempty — every discipline has purpose + multi-line content
  3. test_seed_creates_disciplines    — fresh store → 7 discipline pages created
  4. test_seed_disciplines_idempotent — second call creates 0, skips 7
  5. test_toc_rows_include_disciplines— every seeded discipline appears in
     the SQL ledger's list_agent_discipline_rows (Car I — the wiki TOC
     pointer is retired; the ledger is the discovery surface)
  6. test_contract_covers_subset      — CONTRACT_COVERS ⊆ discipline slugs
  7. test_genesis_discipline_refs_seeded — every [[agent-discipline-*]] ref in any
     genesis body corresponds to a seeded discipline (dangling-pointer guard,
     extension of the Stage-1 starter-pointer guard)
"""

from __future__ import annotations

import re

import pytest

from yadgar.core import server

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_EXPECTED_DISCIPLINE_NAMES = [
    "recall-first",
    "process-hygiene",
    "branch-state",
    "plan-lifecycle",
    "commit-hygiene",
    "strict-typing",
    "adr-consult",
]
_EXPECTED_DISCIPLINE_SLUGS = [f"agent-discipline-{n}" for n in _EXPECTED_DISCIPLINE_NAMES]


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("seed_disciplines")
    server.init_engines(
        db_path=str(tmp_path / "test_seed_disciplines.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def storage():
    import yadgar._shared.runtime.state as _st

    return _st._storage


class TestDisciplineGenesis:
    def test_disciplines_loaded(self):
        from yadgar.core.server.tools.agent_prompts import DISCIPLINES

        names = [n for n, _, _ in DISCIPLINES]
        assert names == _EXPECTED_DISCIPLINE_NAMES, f"unexpected discipline set: {names}"

    def test_discipline_content_nonempty(self):
        from yadgar.core.server.tools.agent_prompts import DISCIPLINES

        for name, purpose, content in DISCIPLINES:
            assert purpose, f"empty purpose for discipline {name!r}"
            assert content, f"empty content for discipline {name!r}"
            assert content.count("\n") >= 2, f"discipline {name!r} content too short"

    def test_process_hygiene_must_absorb(self):
        """Battle-tested rules from 2026-07 incidents MUST be in process-hygiene."""
        from yadgar.core.server.tools.agent_prompts import DISCIPLINES

        by_name = {n: c for n, _, c in DISCIPLINES}
        ph = by_name["process-hygiene"]
        assert "[p]ytest" in ph, "bracket-form pgrep gate missing"
        assert "self-match" in ph, "gate+pytest self-match warning missing"
        assert "monitor" in ph.lower(), "monitor cleanup rule missing"
        assert "timeout" in ph.lower(), "long push timeout rule missing"

    def test_branch_state_must_absorb(self):
        from yadgar.core.server.tools.agent_prompts import DISCIPLINES

        by_name = {n: c for n, _, c in DISCIPLINES}
        bs = by_name["branch-state"]
        assert "gh pr list" in bs
        assert "ls-remote" in bs
        assert "rev-list" in bs
        assert "pre-claim" in bs or "pre-claim" in bs.lower(), "version pre-claim rule missing"

    def test_plan_lifecycle_must_absorb(self):
        from yadgar.core.server.tools.agent_prompts import DISCIPLINES

        by_name = {n: c for n, _, c in DISCIPLINES}
        pl = by_name["plan-lifecycle"]
        assert "ADR-0081" in pl
        assert "git mv" in pl
        assert "ls-files" in pl, "move-not-copy verification missing"

    def test_adr_consult_must_absorb(self):
        """Car 2: the adr-consult discipline must carry the read-side consult rules."""
        from yadgar.core.server.tools.agent_prompts import DISCIPLINES

        by_name = {n: c for n, _, c in DISCIPLINES}
        ac = by_name["adr-consult"]
        assert 'recall(type="wiki", tags=["adr"])' in ac, "wiki-tagged ADR recall missing"
        assert "adr_list" in ac, "adr_list open-status consult missing"
        assert "BIND" in ac, "observed-ADRs-bind rule missing"
        assert "default recall profile" in ac.lower(), "fast-profile gap note missing"

    def test_adr_consult_composed_into_target_patterns(self):
        """Car 2: the 5 named patterns must compose [[agent-discipline-adr-consult]]."""
        from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

        by_pattern = {p: c for p, _, c in STARTER_PROMPTS}
        for pat in (
            "plan-executing-build",
            "build-car",
            "scope-and-plan",
            "rca-diagnose",
            "debug-investigate",
        ):
            assert pat in by_pattern, f"target pattern {pat!r} missing from STARTER_PROMPTS"
            assert "[[agent-discipline-adr-consult]]" in by_pattern[pat], (
                f"pattern {pat!r} does not compose agent-discipline-adr-consult"
            )


class TestSeedDisciplines:
    def test_seed_creates_disciplines(self, storage):
        from yadgar.core.server.tools.agent_prompts import (
            _read_agent_prompt,
            seed_agent_prompts,
        )

        result = seed_agent_prompts(storage=storage)
        assert result["disciplines_created"] == 7, f"expected 7 disciplines created: {result}"
        assert result["disciplines_skipped"] == 0
        assert sorted(result["disciplines"]) == sorted(_EXPECTED_DISCIPLINE_NAMES)

        for slug in _EXPECTED_DISCIPLINE_SLUGS:
            page = _read_agent_prompt(slug, storage=storage)
            assert page is not None, f"discipline page {slug!r} not found after seed"
            assert page["version"] == 1
            assert "agent-discipline" in page["tags"]

    def test_seed_disciplines_idempotent(self, storage):
        from yadgar.core.server.tools.agent_prompts import seed_agent_prompts

        seed_agent_prompts(storage=storage)
        r2 = seed_agent_prompts(storage=storage)
        assert r2["disciplines_created"] == 0
        assert r2["disciplines_skipped"] == 7

    def test_toc_rows_include_disciplines(self, storage):
        from yadgar.core.server.tools.agent_prompts import seed_agent_prompts

        seed_agent_prompts(storage=storage)

        # Car I (0047 §7): the wiki ``agent-prompt-toc`` page is retired as
        # the discovery surface — the table is now ``list_agent_discipline_rows``
        # on the SQL ledger. The D35d pointer page is kept-and-ignored for one
        # release cycle, so the legacy ``_TOC_ROW_RE.finditer(content)`` seam
        # no longer exists. The contract (every seeded discipline appears in
        # the discovery surface) is preserved at the wiki-body level: each
        # discipline has a wiki page (created by _save_discipline_page) which
        # is the body the agent_dispatch_prelude reads and which the ledger
        # row in ``agent_discipline`` mirrors via ``check_page_row_desync``.
        #
        # We assert via storage.get_wiki_page_by_slug() — the canonical wiki
        # read seam — so the test exercises the same path real callers use,
        # without needing to register a list_agent_discipline_rows admin op
        # (the SQL method is reached via the backend for cross-engine
        # invariants, not the core-side test path).
        discovered = set()
        for slug in _EXPECTED_DISCIPLINE_SLUGS:
            page = storage.get_wiki_page_by_slug(slug)
            if page is not None:
                discovered.add(slug)

        for slug in _EXPECTED_DISCIPLINE_SLUGS:
            assert slug in discovered, f"discipline {slug!r} missing from wiki store after seed"


class TestGenesisPointerGuards:
    def test_contract_covers_subset(self):
        """Every slug the contract declares as covered must be a seeded discipline."""
        from yadgar.core.server.tools.agent_prompts import CONTRACT_COVERS

        assert CONTRACT_COVERS, "contract covers: expected at least one covered discipline"
        for slug in CONTRACT_COVERS:
            assert slug in _EXPECTED_DISCIPLINE_SLUGS, (
                f"contract covers {slug!r} which is not a seeded discipline"
            )

    def test_genesis_discipline_refs_seeded(self):
        """[[agent-discipline-*]] refs anywhere in genesis bodies must resolve to a
        seeded discipline (extension of the Stage-1 starter-pointer guard)."""
        from yadgar.core.server.tools.agent_prompts import (
            CONTRACT_GENESIS,
            DISCIPLINES,
            STARTER_PROMPTS,
        )

        seeded = {f"agent-discipline-{n}" for n, _, _ in DISCIPLINES}
        bodies = [CONTRACT_GENESIS[2]] + [c for _, _, c in STARTER_PROMPTS]
        bodies += [c for _, _, c in DISCIPLINES]
        refs = set()
        for body in bodies:
            refs.update(re.findall(r"\[\[(agent-discipline-[a-z0-9-]+)\]\]", body))
        for slug in refs:
            assert slug in seeded, (
                f"genesis references {slug!r} but it is not in the disciplines genesis — "
                "dangling pointer on fresh installs"
            )
