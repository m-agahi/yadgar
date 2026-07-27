"""Tests for agent-prompt one-page-per-pattern (S2 rework, v5.85).

One wiki page per pattern; wiki versioning carries history.
"""

from __future__ import annotations

import pytest

# R3 Car 3c: agent_prompt_save forwards its DB write to the backend /admin endpoint.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(scope="module")
def storage(module_storage, embeddings):
    """Module-scoped storage wired into the _st engine set (R3 Car 3c).

    agent_prompt_save now forwards its DB write to the backend /admin op; under
    admin_backend_bypass that runs run_admin_op → the backend impl, which uses the
    ambient _st engines (_st._storage + _st._wiki + _st._embeddings) — NOT a
    detached engine passed via storage=. So we wire all three onto the registered
    module_storage (+ session-cached module_embeddings): _st._storage +
    _st._embeddings + a local WikiStore over them. Assertions read the same
    _st._storage the impl writes to. Uses module_storage (not init_engines) so the
    conftest surreal lifecycle + per-test data-wipe stay intact — no per-class
    daemon re-init. Prior _st engines restored at module teardown.
    """
    import yadgar._shared.runtime.state as _st
    from yadgar._shared.wiki import WikiStore

    _prev_storage = _st._storage
    _prev_embeddings = _st._embeddings
    _prev_wiki = _st._wiki

    _st._storage = module_storage
    _st._embeddings = embeddings
    _st._wiki = WikiStore(module_storage, embeddings)
    try:
        yield module_storage
    finally:
        _st._storage = _prev_storage
        _st._embeddings = _prev_embeddings
        _st._wiki = _prev_wiki


class TestAgentPromptSave:
    """agent_prompt_save upserts one page per pattern."""

    def test_first_save_creates_page(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        result = agent_prompt_save(
            "dispatch-fix-bug", "Dispatch a bug-fix agent.", directory="global", storage=storage
        )
        assert result["saved"] is True
        assert result["version"] == 1
        assert result["slug"] == "agent-prompt-dispatch-fix-bug"

    def test_second_save_updates_page_not_creates_new(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "First version.", directory="global", storage=storage)
        result = agent_prompt_save(
            "dispatch-fix-bug", "Second version.", directory="global", storage=storage
        )
        assert result["saved"] is True
        assert result["version"] == 2
        assert result["slug"] == "agent-prompt-dispatch-fix-bug"
        # Only one page should exist (not two)
        pages = storage.list_wiki_pages()
        ap_pages = [
            p for p in pages if p.get("slug", "").startswith("agent-prompt-dispatch-fix-bug")
        ]
        assert len(ap_pages) == 1, (
            f"Expected 1 page, found {len(ap_pages)}: {[p['slug'] for p in ap_pages]}"
        )

    def test_save_different_patterns_are_independent(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        r1 = agent_prompt_save(
            "dispatch-fix-bug", "Bug fix prompt.", directory="global", storage=storage
        )
        r2 = agent_prompt_save(
            "dispatch-research", "Research prompt.", directory="global", storage=storage
        )
        assert r1["version"] == 1
        assert r2["version"] == 1
        assert r1["slug"] == "agent-prompt-dispatch-fix-bug"
        assert r2["slug"] == "agent-prompt-dispatch-research"
        assert r1["slug"] != r2["slug"]

    def test_saved_page_has_correct_page_type(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "Fix bugs.", directory="global", storage=storage)
        page = storage.get_wiki_page_by_slug("agent-prompt-dispatch-fix-bug")
        assert page is not None
        assert page.get("page_type") == "agent_prompt"

    def test_storage_scope_override_global_when_project_dir_supplied(self, storage):
        """C2 (#83): agent_prompt_save with a project directory must store global scope.

        storage_scope="global" enforcement in WikiStore.add overrides the caller's
        directory_context when page_type == "agent_prompt" — the type, not the
        caller, decides the scope.
        """
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save(
            "zz-probe-scope",
            "Probe prompt for storage_scope enforcement.",
            directory="/tmp/some-project",
            storage=storage,
        )
        page = storage.get_wiki_page_by_slug("agent-prompt-zz-probe-scope")
        assert page is not None, "page must be stored"
        assert page.get("directory_context") == "global", (
            f"expected 'global', got {page.get('directory_context')!r} — "
            "storage_scope enforcement did not fire"
        )


class TestWikiAddStorageScopeEnforcement:
    """C2 (#83): raw wiki_add with page_type='agent_prompt' also stores global scope.

    The enforcement lives in WikiStore.add (the shared write chokepoint used by
    both agent_prompt_save and wiki_add's replay path), so the type — not the
    caller — decides directory_context.
    """

    def test_wiki_add_agent_prompt_page_type_stored_global(self, storage):
        """wiki_add(page_type='agent_prompt', directory='/tmp/some-project') → global."""
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.wiki import WikiStore
        from yadgar._shared.wiki.contract import WikiAddOptions

        # Use the module-scoped storage already wired into _st by the fixture.
        wiki = WikiStore(storage, _st._embeddings)
        result = wiki.add(
            title="Storage Scope Probe",
            content="Probe content for storage_scope test.",
            category="reference",
            tags=["agent-prompt", "task:storage-scope-probe"],
            opts=WikiAddOptions(
                directory_context="/tmp/some-project",
                page_type="agent_prompt",
            ),
        )
        slug = result.get("slug")
        assert slug is not None
        page = storage.get_wiki_page_by_slug(slug)
        assert page is not None
        assert page.get("directory_context") == "global", (
            f"expected 'global', got {page.get('directory_context')!r}"
        )

    def test_wiki_add_plain_page_type_stays_project_scoped(self, storage):
        """wiki_add with page_type=None keeps caller's directory_context (control case)."""
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.wiki import WikiStore
        from yadgar._shared.wiki.contract import WikiAddOptions

        wiki = WikiStore(storage, _st._embeddings)
        project_dir = "/tmp/control-project"
        result = wiki.add(
            title="Storage Scope Control Plain",
            content="Control content — no page_type.",
            category="reference",
            tags=["plain-page"],
            opts=WikiAddOptions(
                directory_context=project_dir,
                page_type=None,
            ),
        )
        slug = result.get("slug")
        assert slug is not None
        page = storage.get_wiki_page_by_slug(slug)
        assert page is not None
        assert page.get("directory_context") == project_dir, (
            f"expected {project_dir!r}, got {page.get('directory_context')!r}"
        )

    def test_wiki_add_repo_wiki_page_type_stays_project_scoped(self, storage):
        """wiki_add with page_type='repo_wiki' keeps caller's directory_context (project storage_scope)."""
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.wiki import WikiStore
        from yadgar._shared.wiki.contract import WikiAddOptions

        wiki = WikiStore(storage, _st._embeddings)
        project_dir = "/tmp/repo-wiki-project"
        result = wiki.add(
            title="Storage Scope Control Repo Wiki",
            content="repo_wiki control — should stay project scoped.",
            category="reference",
            tags=["repo-wiki-control"],
            opts=WikiAddOptions(
                directory_context=project_dir,
                page_type="repo_wiki",
            ),
        )
        slug = result.get("slug")
        assert slug is not None
        page = storage.get_wiki_page_by_slug(slug)
        assert page is not None
        assert page.get("directory_context") == project_dir, (
            f"expected {project_dir!r}, got {page.get('directory_context')!r}"
        )


class TestGlobalScopeBranchCanonicalization:
    """A global-scoped page must land in the canonical branch slot (branch IS NULL).

    Regression for the agent-prompt wiki_read 404 drift: storage_scope='global'
    pages inserted with a caller branch_hint (e.g. the SessionStart hook passing
    branch_hint='master') were stamped global+branch='master'. §25 read resolution
    reaches a global page ONLY via step 3 (directory='global' AND branch IS NONE),
    so those rows 404'd through wiki_read while still resolving via the plain-slug
    prelude path. WikiStore.add now forces branch=None alongside the
    directory_context='global' override so global pages are always canonical.
    """

    def _add_agent_prompt_with_branch(self, storage, slug_suffix, branch):
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.wiki import WikiStore
        from yadgar._shared.wiki.contract import WikiAddOptions

        wiki = WikiStore(storage, _st._embeddings)
        return wiki.add(
            title=f"Branch Canonicalization Probe {slug_suffix}",
            content="Probe content for global-scope branch canonicalization.",
            category="reference",
            tags=["agent-prompt", f"task:{slug_suffix}"],
            opts=WikiAddOptions(
                directory_context="global",
                page_type="agent_prompt",
                branch=branch,
            ),
        )

    def test_global_page_inserted_with_branch_is_stored_canonical(self, storage):
        """add(page_type='agent_prompt', branch='feature-x') stores branch=None."""
        result = self._add_agent_prompt_with_branch(storage, "branch-canon-probe", "feature-x")
        slug = result.get("slug")
        assert slug is not None
        page = storage.get_wiki_page_by_slug(slug)
        assert page is not None
        assert page.get("branch") is None, (
            f"expected canonical branch=None, got {page.get('branch')!r} — "
            "global-scope branch canonicalization did not fire"
        )

    def test_global_page_resolves_via_directory_branch_from_project_caller(self, storage):
        """The user-visible bug: resolve a global agent_prompt page from a PROJECT dir.

        caller_directory MUST be a project path (not 'global'), so resolution falls
        through step 1/2 to step 3 (directory='global' AND branch IS NONE) — the only
        step that discriminates a stranded branch='master' row (unresolvable) from a
        canonical branch=None row (found). Reading with caller_dir='global' would
        false-pass via step 1 and NOT encode the bug.
        """
        result = self._add_agent_prompt_with_branch(storage, "branch-resolve-probe", "master")
        slug = result.get("slug")
        assert slug is not None

        page = storage.get_wiki_page_by_slug_directory_branch(
            slug, "/home/some/project", "some-feature-branch"
        )
        assert page is not None, (
            "global agent_prompt page must resolve via §25 step-3 (global + branch IS NONE) "
            "from an arbitrary project caller; a stranded global+branch='master' row 404s here"
        )
        assert page.get("slug") == slug


class TestReadAgentPrompt:
    """_read_agent_prompt (internal slug-read) returns the page by deterministic slug.

    v5.85 S4/S5: the agent_prompt_get MCP tool was removed; the exact-key lookup
    logic lives in the internal helper _read_agent_prompt(slug, storage).
    """

    def test_read_returns_latest_content(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "First version.", directory="global", storage=storage)
        agent_prompt_save(
            "dispatch-fix-bug", "Second version — updated.", directory="global", storage=storage
        )
        result = _read_agent_prompt("agent-prompt-dispatch-fix-bug", storage=storage)
        assert result is not None
        assert result["version"] == 2
        assert "Second version" in result["content"]
        assert result["slug"] == "agent-prompt-dispatch-fix-bug"

    def test_read_unknown_slug_returns_none(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt

        result = _read_agent_prompt("agent-prompt-nonexistent-pattern-xyz", storage=storage)
        assert result is None or result == {}

    def test_read_returns_version_1_when_only_one_save(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, agent_prompt_save

        agent_prompt_save(
            "dispatch-review-code", "Review code carefully.", directory="global", storage=storage
        )
        result = _read_agent_prompt("agent-prompt-dispatch-review-code", storage=storage)
        assert result is not None
        assert result["version"] == 1
        assert "Review code" in result["content"]

    def test_read_with_many_saves_returns_latest_version(self, storage):
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt, agent_prompt_save

        for i in range(1, 6):
            agent_prompt_save(
                "multi-version-test", f"Prompt version {i}.", directory="global", storage=storage
            )
        result = _read_agent_prompt("agent-prompt-multi-version-test", storage=storage)
        assert result is not None
        assert result["version"] == 5
        assert "Prompt version 5" in result["content"]


class TestAgentPromptDoubleWrap:
    """#68: agent_prompt_save must not double-wrap when content already has Purpose/Prompt headers."""

    def test_pre_wrapped_content_produces_single_wrap(self, storage):
        """RED: saving already-wrapped content currently double-wraps (## Purpose appears twice)."""
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        pre_wrapped = "## Purpose\n\nSome purpose\n\n## Prompt\n\nDO THE THING"
        agent_prompt_save(
            "double-wrap-test",
            pre_wrapped,
            directory="global",
            purpose="Test purpose",
            storage=storage,
        )
        page = storage.get_wiki_page_by_slug("agent-prompt-double-wrap-test")
        assert page is not None
        content = page["content"]
        # Exactly one ## Purpose and one ## Prompt header
        assert content.count("## Purpose") == 1, (
            f"Expected exactly 1 '## Purpose', got {content.count('## Purpose')}:\n{content}"
        )
        assert content.count("## Prompt") == 1, (
            f"Expected exactly 1 '## Prompt', got {content.count('## Prompt')}:\n{content}"
        )
        # Body text is intact
        assert "DO THE THING" in content, f"Body text missing from:\n{content}"

    def test_bare_content_wraps_normally(self, storage):
        """Passthrough: bare content gets wrapped once (no change in behaviour)."""
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save(
            "bare-content-test",
            "DO THE THING",
            directory="global",
            purpose="A test purpose",
            storage=storage,
        )
        page = storage.get_wiki_page_by_slug("agent-prompt-bare-content-test")
        assert page is not None
        content = page["content"]
        assert content.count("## Purpose") == 1
        assert content.count("## Prompt") == 1
        assert "DO THE THING" in content

    def test_unwrap_helper_strips_wrapper(self):
        """Unit-test _unwrap_purpose_prompt directly."""
        from yadgar.core.server.tools.agent_prompts import _unwrap_purpose_prompt

        wrapped = "## Purpose\n\nSome purpose\n\n## Prompt\n\nDO THE THING"
        assert _unwrap_purpose_prompt(wrapped) == "DO THE THING"

    def test_unwrap_helper_passthrough_bare(self):
        """_unwrap_purpose_prompt returns bare content unchanged."""
        from yadgar.core.server.tools.agent_prompts import _unwrap_purpose_prompt

        bare = "DO THE THING"
        assert _unwrap_purpose_prompt(bare) == bare


class TestAgentPromptToolSurface:
    """v5.85 S4 (I32): bespoke get/search tools removed; save stays a tool."""

    def test_get_and_search_tools_removed_from_all(self):
        from yadgar.core.server import tools

        assert "agent_prompt_get" not in tools.__all__
        assert "agent_prompt_search" not in tools.__all__

    def test_save_tool_still_exported(self):
        from yadgar.core.server import tools

        assert "agent_prompt_save" in tools.__all__

    def test_removed_tools_not_importable(self):
        import yadgar.core.server.tools.agent_prompts as ap_mod

        assert not hasattr(ap_mod, "agent_prompt_get")
        assert not hasattr(ap_mod, "agent_prompt_search")
        assert hasattr(ap_mod, "agent_prompt_save")
