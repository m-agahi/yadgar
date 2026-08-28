"""TDD tests for prelude contract wiki-sourcing (v5.122.0).

Covers:
1. test_contract_from_wiki_normal       — contract from wiki page, no ## Purpose in output
2. test_contract_cache_invalidation     — edited page → epoch bump → new text served
3. test_contract_reseed_on_delete       — page deleted → seed-on-miss → serve genesis
4. test_contract_seed_write_failure     — seeder raises → genesis fallback, no crash
5. test_contract_budget_contract_intact — huge pattern body truncated, NOT the contract
6. test_contract_rule4_present          — rule 4 (plan-executing-build) in contract text
7. test_contract_present_kill_gate_off  — AGENT_PROMPT_LIBRARY_ENABLED=False → contract present
"""

from __future__ import annotations

import pytest

from yadgar.tests.core.conftest import TEST_PROJECT_ID

# R3 Car 3c: agent_prompt_save forwards to backend /admin.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine wired into _st._storage."""
    import yadgar._shared.runtime.state as _st

    _prev = _st._storage
    _st._storage = module_storage
    yield module_storage
    _st._storage = _prev


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _flush_prompt_cache() -> None:
    """Invalidate the agent_prompt_prelude cache (simulates epoch bump in tests)."""
    from yadgar.core.server.tools.dispatch_helper import _prompt_cache  # noqa: PLC0415

    try:
        _prompt_cache.clear()
    except AttributeError:
        # Fallback: clear by draining (some Cache implementations may not have .clear)
        pass


# ---------------------------------------------------------------------------
# 1. Normal path: contract from wiki, no ## Purpose leakage
# ---------------------------------------------------------------------------


class TestContractFromWikiNormal:
    def test_contract_section_present(self, storage):
        """Prelude contains '## Yadgar subagent contract' section."""
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        prelude = agent_dispatch_prelude("", "test topic", storage=storage, project=TEST_PROJECT_ID)
        assert "## Yadgar subagent contract" in prelude

    def test_no_purpose_header_in_prelude(self, storage):
        """agent_prompt_save wraps content with '## Purpose'; the prelude must unwrap it."""
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        prelude = agent_dispatch_prelude("", "test topic", storage=storage, project=TEST_PROJECT_ID)
        # The Purpose/Prompt wrapper must NOT appear in the injected contract section
        assert "## Purpose" not in prelude

    def test_contract_contains_recall_directive(self, storage):
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        prelude = agent_dispatch_prelude("", "test topic", storage=storage, project=TEST_PROJECT_ID)
        assert "recall" in prelude.lower()

    def test_contract_contains_findings_footer(self, storage):
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        prelude = agent_dispatch_prelude("", "test topic", storage=storage, project=TEST_PROJECT_ID)
        assert "Yadgar findings" in prelude

    def test_contract_footer_memorization_note_in_genesis(self):
        """ADR-0093: the findings-footer paragraph explains bullets ARE memorized
        verbatim (one memory per bullet, standalone + context-complete). Pinned
        so genesis and the live wiki page stay in sync."""
        from yadgar.core.server.tools.agent_prompts import CONTRACT_GENESIS

        _, _, content = CONTRACT_GENESIS
        assert (
            "This footer IS memorized verbatim — one memory per bullet. Write each "
            "bullet as a standalone, context-complete memory (durable fact, not "
            "status); note candidates as you go and finalize at the end." in content
        ), "ADR-0093 footer-memorization sentence missing from CONTRACT_GENESIS"

    def test_contract_footer_memorization_note_in_seeded_prelude(self, storage):
        """ADR-0093 sentence survives seeding + prelude assembly."""
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        prelude = agent_dispatch_prelude("", "test topic", storage=storage, project=TEST_PROJECT_ID)
        assert "memorized verbatim — one memory per bullet" in prelude


# ---------------------------------------------------------------------------
# 2. Cache invalidation: edited page → new text served
# ---------------------------------------------------------------------------


class TestContractCacheInvalidation:
    def test_edited_contract_served_after_resave(self, storage):
        """Re-save the contract page; epoch bump ensures stale content is NOT served."""
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page, agent_prompt_save
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        # First read — baseline
        prelude_before = agent_dispatch_prelude(
            "", "epoch test", storage=storage, project=TEST_PROJECT_ID
        )
        assert "Yadgar subagent contract" in prelude_before

        # Edit the contract page via agent_prompt_save (bumps wiki epoch)
        agent_prompt_save(
            "contract",
            "UPDATED_SENTINEL_TEXT unique_marker_xyz",
            directory="global",
            storage=storage,
            project=TEST_PROJECT_ID,
        )
        _flush_prompt_cache()

        prelude_after = agent_dispatch_prelude(
            "", "epoch test", storage=storage, project=TEST_PROJECT_ID
        )
        assert "UPDATED_SENTINEL_TEXT" in prelude_after, (
            "Cache invalidation failed: stale contract content served after resave"
        )


# ---------------------------------------------------------------------------
# 3. Seed-on-miss: page absent → reseed + serve
# ---------------------------------------------------------------------------


class TestContractReseedOnDelete:
    def test_deleted_contract_triggers_reseed(self, storage):
        """If agent-prompt-contract page is absent, reseed fires and genesis is served."""
        from yadgar.core.server.tools.agent_prompts import (
            CONTRACT_GENESIS,
            CONTRACT_SLUG,
            _seed_contract_page,
        )
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        # Ensure contract exists first
        _seed_contract_page(storage=storage)

        # Delete the contract page by wiping (simulate absent page via storage delete)
        try:
            page = storage.get_wiki_page_by_slug(CONTRACT_SLUG)
            if page is not None:
                page_id = storage._extract_id(page.get("id"))
                if page_id is not None:
                    storage._q(f"DELETE wiki_page:{page_id}")
        # `get_wiki_page_by_slug` / `_extract_id` / `_q` all reach the storage
        # driver, whose error surface differs between the httpx and embedded
        # backends — which is precisely the "not available in this fixture"
        # condition the skip names.
        except Exception:  # noqa: BLE001 — `_q` error surface is backend-dependent
            pytest.skip("Storage delete not available in this fixture — skip delete test")

        _flush_prompt_cache()

        # Call prelude — should trigger seed-on-miss and return genesis content
        prelude = agent_dispatch_prelude(
            "", "reseed test", storage=storage, project=TEST_PROJECT_ID
        )
        _, _, genesis_content = CONTRACT_GENESIS
        # Genesis must be in the prelude (possibly from in-memory fallback)
        assert "Yadgar subagent contract" in prelude
        # The prelude must not be empty
        assert len(prelude) > 100


# ---------------------------------------------------------------------------
# 4. Seed-write-failure → genesis fallback, no crash
# ---------------------------------------------------------------------------


class TestContractSeedWriteFailure:
    def test_seed_failure_uses_genesis_fallback(self, storage, monkeypatch):
        """If _seed_contract_page raises, genesis is returned; prelude never crashes.

        _get_contract_text uses lazy imports from agent_prompts, so we patch
        _seed_contract_page at its home module (agent_prompts) and _cached_agent_prompt
        at dispatch_helper (it IS a module-level function there).
        """
        import yadgar.core.server.tools.agent_prompts as ap_mod
        from yadgar.core.server.tools import dispatch_helper as dh
        from yadgar.core.server.tools.agent_prompts import CONTRACT_GENESIS

        # Force cache miss (so _get_contract_text reaches the seed path)
        _flush_prompt_cache()

        def _raise_on_seed(*args, **kwargs):  # noqa: ARG001
            raise RuntimeError("simulated seed failure")

        # Patch at the home module (dispatch_helper imports it lazily from there)
        monkeypatch.setattr(ap_mod, "_seed_contract_page", _raise_on_seed)
        # Patch _cached_agent_prompt at dispatch_helper (it IS a module attribute)
        monkeypatch.setattr(dh, "_cached_agent_prompt", lambda *_a, **_kw: None)

        prelude = dh.agent_dispatch_prelude(
            "", "failure test", storage=storage, project=TEST_PROJECT_ID
        )
        # Must not crash; must include genesis contract header
        assert "## Yadgar subagent contract" in prelude
        _, _, genesis_content = CONTRACT_GENESIS
        # Some part of the genesis content must appear
        assert "recall" in prelude.lower()


# ---------------------------------------------------------------------------
# 5. Budget: contract intact even with huge pattern body
# ---------------------------------------------------------------------------


class TestContractBudgetIntact:
    def test_contract_intact_when_pattern_truncated(self, storage):
        """With a 5 000-char pattern, total ≤2 000 and contract section is NOT truncated."""
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page, agent_prompt_save
        from yadgar.core.server.tools.dispatch_helper import (
            _TOTAL_BUDGET,
            agent_dispatch_prelude,
        )

        _seed_contract_page(storage=storage)
        agent_prompt_save(
            "huge-pattern-contract-test",
            "y" * 5000,
            directory="global",
            storage=storage,
            project=TEST_PROJECT_ID,
        )
        _flush_prompt_cache()

        prelude = agent_dispatch_prelude(
            "huge-pattern-contract-test", "budget test", storage=storage, project=TEST_PROJECT_ID
        )
        assert len(prelude) <= _TOTAL_BUDGET
        # Full contract header must survive truncation (it's sections[0])
        assert "## Yadgar subagent contract" in prelude
        assert "Yadgar findings" in prelude


# ---------------------------------------------------------------------------
# 6. Rule 4 present in genesis contract
# ---------------------------------------------------------------------------


class TestContractRule4:
    def test_rule4_plan_executing_build_in_genesis(self):
        """Rule 4 (plan-executing-build reference) is present in the genesis contract."""
        from yadgar.core.server.tools.agent_prompts import CONTRACT_GENESIS

        _, _, content = CONTRACT_GENESIS
        assert "plan-executing-build" in content, (
            "Rule 4 (plan-executing-build reference) missing from CONTRACT_GENESIS content"
        )

    def test_rule4_in_seeded_prelude(self, storage):
        """Rule 4 present in a real prelude after seeding."""
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        prelude = agent_dispatch_prelude("", "plan test", storage=storage, project=TEST_PROJECT_ID)
        assert "plan-executing-build" in prelude

    def test_contract_wiki_pointers_match_seeded_starters(self):
        """Dangling-pointer regression guard: every agent-prompt-<pattern> slug the
        contract genesis references MUST correspond to a pattern in STARTER_PROMPTS,
        so fresh installs (seeded via seed_agent_prompts) can resolve the pointer."""
        import re

        from yadgar.core.server.tools.agent_prompts import CONTRACT_GENESIS, STARTER_PROMPTS

        _, _, content = CONTRACT_GENESIS
        seeded_patterns = {p for p, _, _ in STARTER_PROMPTS}
        referenced = re.findall(r"agent-prompt-([a-z0-9-]+)", content)
        assert referenced, "expected at least one agent-prompt-<pattern> reference in genesis"
        for pattern in referenced:
            assert pattern in seeded_patterns, (
                f"contract genesis references agent-prompt-{pattern} but {pattern!r} is "
                f"not in STARTER_PROMPTS {sorted(seeded_patterns)} — dangling pointer on "
                "fresh installs"
            )


# ---------------------------------------------------------------------------
# 7. Kill-gate: contract present even when AGENT_PROMPT_LIBRARY_ENABLED=False
# ---------------------------------------------------------------------------


class TestContractPresentKillGateOff:
    def test_contract_present_when_library_disabled(self, storage, monkeypatch):
        """AGENT_PROMPT_LIBRARY_ENABLED=False disables pattern lookup but NOT the contract.

        get_settings is imported lazily inside agent_dispatch_prelude (PLC0415),
        so we patch it at the source module (yadgar._shared.config) where the
        lazy import resolves to.
        """
        import yadgar._shared.config as config_mod
        from yadgar.core.server.tools.agent_prompts import _seed_contract_page
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_contract_page(storage=storage)
        _flush_prompt_cache()

        original_get_settings = config_mod.get_settings
        original_settings = original_get_settings()

        class _FakeSettings:
            AGENT_PROMPT_LIBRARY_ENABLED = False

            def __getattr__(self, name):
                return getattr(original_settings, name)

        monkeypatch.setattr(config_mod, "get_settings", lambda: _FakeSettings())

        prelude = agent_dispatch_prelude(
            "some-pattern", "killgate test", storage=storage, project=TEST_PROJECT_ID
        )
        # Contract MUST be present even with kill-gate off
        assert "## Yadgar subagent contract" in prelude
        assert "Yadgar findings" in prelude
