"""Tests for v5.39.0 wiki_add similarity gate.

Design note: wiki_page stores ONE combined embedding (title + content[:2000]).
Separate title-only and content-only embeddings would require a schema change
(violates §4 non-goals). Gate therefore uses a single combined cosine similarity
threshold. Plan's dual threshold (TITLE/CONTENT) is collapsed to one effective
threshold: the lower of the two (CONTENT_THRESHOLD default 0.80) — conservative.

Tests use REAL embeddings via all-MiniLM-L6-v2 (no mocked scores).

Phase 1: find_similar_wiki_pages helper + wiki_check_duplicate MCP tool.
Phase 2: gate enforcement in wiki_add (force + replace_slug bypass).
Phase 3: config knobs.
Phase 4: calibration.

v5.41.5 note: gate moved from MCP handler to drainer pre-apply stage (I9 fix).
TestWikiAddSimilarityGate tests the drainer-internal _sim_gate_for_drainer()
helper via the is_draining=True sync path, which exercises the same gate logic.
For drainer async-path tests, see test_wiki_sim_gate_drainer.py.
"""

from __future__ import annotations

import pytest

from yadgar import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Isolated temp DB with real embedding model per test."""
    server.init_engines(
        db_path=str(tmp_path / "simgate_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _add(title: str, content: str, **kwargs) -> dict:
    """Direct WikiStore.add — bypasses async queue."""
    return _wiki().add(title, content, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures: content samples
# ---------------------------------------------------------------------------

# Near-duplicate pair (reproduces 2026-05-30 incident class):
# Page A: yadgar-roadmap-future-improvements (original)
# Page B: yadgar-future-roadmap (regen with different slug, near-identical content)
_ROADMAP_CONTENT_A = """# Yadgar Roadmap: Future Improvements

## Short-term (next 2 months)
- Implement wiki versioning (v5.41) to track page history
- Add similarity gate to wiki_add to prevent duplicate pages
- Improve embedding model to mpnet for better semantic search

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation
- Automated anchor hygiene with consolidation pass

## Long-term (6+ months)
- LLM-based duplicate resolution and wiki curation
- Retroactive deduplication of existing pages
- Distributed SurrealDB for large-scale deployment

## Architecture principles
Yadgar follows a thin-request-path invariant: all heavy computation deferred
to background consolidation. Wiki operations must complete in <100ms.
"""

_ROADMAP_CONTENT_B = """# Yadgar Future Roadmap

## Near-term (next 2 months)
- Wiki versioning (v5.41) — track page history and enable rollback
- Similarity gate in wiki_add — block near-duplicate page creation
- Better embedding model (mpnet) for semantic search quality

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation across workspaces
- Automated anchor hygiene during consolidation cycles

## Long-term (6+ months)
- LLM-based wiki curation and duplicate resolution
- Retroactive dedup of existing pages (v5.45+)
- Distributed SurrealDB for large deployments

## Core principles
Thin request path: heavy work deferred to consolidation background loop.
All wiki ops target <100ms latency.
"""

# Distinct pages (control group — should NOT trigger gate)
_ARCH_CONTENT = """# Yadgar Architecture

## Core components
StorageEngine: SurrealDB wrapper. Mixins: _WikiMixin, _VectorMixin, _MemoryMixin.
WikiStore: hybrid FTS + vector search over wiki_page table.
EmbeddingsService: sentence-transformers, all-MiniLM-L6-v2 default.

## Data flow
memorize() -> WriteGate -> StorageEngine.insert_memory() -> EmbeddingsService.encode_document()
wiki_add() -> WikiStore.add() -> StorageEngine.insert_wiki_page()

## Invariants
I1: request path thin (no ML in handler).
I3: opt-in features short-circuit on disabled.
I25: all knobs registered three-way (config.py + config_registry.py + config_yaml.py).
"""

_HOOKS_CONTENT = """# Yadgar Hook System

## Hook types
PreToolUse: fires before every Claude tool call. Captures action_stream.
PostToolUse: fires after tool call completion.
SessionStart: fires on session init. Loads project context.
SessionEnd: fires on shutdown. Captures session summary.

## Hook installation
install_hooks() writes .claude/settings.json hooks block.
Hooks execute as HTTP POSTs to the yadgar daemon.

## Hook data
Each hook receives tool name, input args, output result.
PreToolUse receives only name + args (result not yet available).
"""

_BENCHMARK_CONTENT = """# Yadgar Benchmark Results v5.26.0

## LongMemEval-s 500 questions
Model: claude-sonnet-4-6
Score: Adopt-1 (headline result)
Methodology: 500 questions from LongMemEval-s benchmark suite.

## Latency metrics
p50 recall: 45ms, p99 recall: 180ms
wiki_add: p50 12ms, p99 45ms
memorize: p50 8ms, p99 30ms

## Comparison to baseline
Sonnet 4.6 outperforms Sonnet 4.5 on episodic recall by 12%.
No regression on structured knowledge retrieval.
"""


# ---------------------------------------------------------------------------
# Phase 1 tests: find_similar_wiki_pages helper
# ---------------------------------------------------------------------------


class TestFindSimilarWikiPages:
    """Tests for WikiStore.find_similar_wiki_pages()."""

    def test_empty_db_returns_empty(self):
        """No existing pages -> no candidates."""
        result = _wiki().find_similar_wiki_pages(
            title="Yadgar Roadmap Future Improvements",
            content=_ROADMAP_CONTENT_A,
            branch=None,
            threshold=0.80,
        )
        assert result == []

    def test_near_duplicate_detected(self):
        """Near-clone of 2026-05-30 incident: different slugs, >= 0.80 combined similarity."""
        # Insert page A
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        # Check page B (different title/slug, same content with minor edits)
        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            branch=None,
            threshold=0.80,
        )
        assert len(candidates) >= 1, (
            "Expected near-duplicate detected, got 0 candidates. "
            "This indicates the threshold 0.80 is too high for this embedding model. "
            "Calibration needed."
        )
        # Verify the flagged candidate is the right page
        slugs = [c["slug"] for c in candidates]
        assert "yadgar-roadmap-future-improvements" in slugs

    def test_distinct_pages_not_flagged(self):
        """Distinct pages (architecture vs hooks vs benchmark) are NOT flagged as duplicates."""
        _add("Yadgar Architecture", _ARCH_CONTENT)
        _add("Yadgar Hook System", _HOOKS_CONTENT)
        _add("Yadgar Benchmark Results v5.26.0", _BENCHMARK_CONTENT)

        # New distinct page: should not be caught by gate
        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar Configuration Guide",
            content="""# Yadgar Configuration Guide

## Config file location
~/.yadgar/config.yaml -- overrides defaults, overridden by env vars.

## Priority order
1. Environment variables (YADGAR_*)
2. ~/.yadgar/config.yaml
3. Built-in defaults in config.py

## Common knobs
YADGAR_EMBEDDING_MODEL: sentence-transformer model name
YADGAR_PORT: HTTP daemon port (default 8765)
YADGAR_DB_PATH: SurrealDB storage directory
""",
            branch=None,
            threshold=0.80,
        )
        # Should not match arch, hooks, or benchmark
        if candidates:
            sims = [c.get("similarity", 0) for c in candidates]
            pytest.fail(
                f"False positive: distinct config guide page flagged as duplicate of "
                f"{[c['slug'] for c in candidates]} with similarities {sims}. "
                f"Threshold 0.80 may be too low for this embedding model."
            )

    def test_same_slug_excluded_from_results(self):
        """Existing page with the SAME slug should be excluded from candidates.

        Same slug = overwrite, not duplicate. wiki_add upserts by slug.
        """
        _add("Yadgar Architecture", _ARCH_CONTENT)

        # Finding duplicates for "Yadgar Architecture" should exclude itself
        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar Architecture",
            content=_ARCH_CONTENT,
            branch=None,
            threshold=0.50,  # very low threshold to ensure we'd catch self if not excluded
            exclude_slug="yadgar-architecture",
        )
        slugs = [c["slug"] for c in candidates]
        assert "yadgar-architecture" not in slugs

    def test_top_k_respected(self):
        """Returns at most top_k candidates."""
        _add("Yadgar Architecture", _ARCH_CONTENT)
        _add("Yadgar Hook System", _HOOKS_CONTENT)
        _add("Yadgar Benchmark Results v5.26.0", _BENCHMARK_CONTENT)
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            branch=None,
            threshold=0.0,  # return everything
            top_k=2,
        )
        assert len(candidates) <= 2

    def test_similarity_field_in_result(self):
        """Each candidate includes 'similarity' field in [0, 1]."""
        _add("Yadgar Architecture", _ARCH_CONTENT)

        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar System Architecture Overview",
            content=_ARCH_CONTENT,
            branch=None,
            threshold=0.0,
        )
        for c in candidates:
            assert "similarity" in c
            assert 0.0 <= c["similarity"] <= 1.0

    def test_branch_scope_isolation(self):
        """Page on branch 'feat/x' is NOT a candidate when checking on different branch."""
        # Insert page on branch feat/x
        _wiki().add(
            "Yadgar Architecture",
            _ARCH_CONTENT,
            branch="feat/x",
        )
        # Check for duplicates on branch feat/y -- should not find feat/x page
        candidates = _wiki().find_similar_wiki_pages(
            title="Yadgar Architecture Overview",
            content=_ARCH_CONTENT,
            branch="feat/y",
            threshold=0.70,
        )
        # No candidates because the only existing page is on feat/x, not feat/y or None
        assert len(candidates) == 0, (
            f"Branch isolation failed: feat/x page returned as candidate for feat/y check. "
            f"Candidates: {[c['slug'] for c in candidates]}"
        )


# ---------------------------------------------------------------------------
# Phase 1 tests: wiki_check_duplicate MCP tool
# ---------------------------------------------------------------------------


class TestWikiCheckDuplicate:
    """wiki_check_duplicate is a dry-run: returns candidates, never writes."""

    def test_check_duplicate_returns_empty_when_no_pages(self, monkeypatch):
        """Empty DB -> no candidates."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        result = server.wiki_check_duplicate(
            title="Test Page",
            content="Some content about testing.",
        )
        assert isinstance(result, dict)
        assert result.get("candidates") == [] or result.get("candidates") is not None

    def test_check_duplicate_finds_near_clone(self, monkeypatch):
        """Reproduces the 2026-05-30 incident: near-clone detected."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)

        # Insert page A via wiki_add (sync path)
        server.wiki_add(
            title="Yadgar Roadmap Future Improvements",
            content=_ROADMAP_CONTENT_A,
        )

        # Check page B (different title, near-identical content)
        result = server.wiki_check_duplicate(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
        )
        assert isinstance(result, dict)
        candidates = result.get("candidates", [])
        assert len(candidates) >= 1, (
            f"wiki_check_duplicate found no candidates for near-duplicate roadmap page. "
            f"Result: {result}"
        )
        slugs = [c["slug"] for c in candidates]
        assert "yadgar-roadmap-future-improvements" in slugs

    def test_check_duplicate_does_not_write(self, monkeypatch):
        """wiki_check_duplicate never creates a wiki page."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)

        server.wiki_check_duplicate(
            title="Some New Page",
            content="Content about something new entirely.",
        )
        # Verify no page was created
        page = _wiki()._storage.get_wiki_page_by_slug("some-new-page")
        assert page is None, "wiki_check_duplicate must not write to DB"

    def test_check_duplicate_distinct_pages_empty(self, monkeypatch):
        """Distinct pages: check_duplicate returns empty candidates."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)

        server.wiki_add(
            title="Yadgar Architecture",
            content=_ARCH_CONTENT,
        )

        result = server.wiki_check_duplicate(
            title="Yadgar Benchmark Results",
            content=_BENCHMARK_CONTENT,
        )
        candidates = result.get("candidates", [])
        assert candidates == [], (
            f"False positive: distinct benchmark page flagged as dup of architecture. "
            f"Candidates: {candidates}"
        )


# ---------------------------------------------------------------------------
# Phase 2 tests: gate enforcement in wiki_add (sync path)
# ---------------------------------------------------------------------------


def _wiki_add_sync(monkeypatch, **kwargs) -> dict:
    """Call wiki_add on the sync (drain) path — bypasses queue and gate."""
    monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
    return server.wiki_add(**kwargs)


def _drainer_gate(payload: dict) -> dict | None:
    """Call _sim_gate_for_drainer() directly to test gate logic in isolation."""
    import tempfile

    from yadgar.file_queue import FileQueue, QueueDrainer

    with tempfile.TemporaryDirectory() as tmp:
        fq = FileQueue(tmp)
        drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
        return drainer._sim_gate_for_drainer(payload)


class TestWikiAddSimilarityGate:
    """Tests for gate enforcement.

    v5.41.5: gate moved to drainer. Tests exercise _sim_gate_for_drainer()
    directly for unit coverage. For integration tests via wait=True path see
    test_wiki_sim_gate_drainer.py::TestWaitTrueSyncRejection.
    """

    def test_gate_blocks_near_duplicate(self):
        """Near-clone of 2026-05-30 incident: _sim_gate_for_drainer returns rejection."""
        # Insert page A via sync path.
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        # Gate should fire for page B.
        payload = {
            "title": "Yadgar Future Roadmap",
            "content": _ROADMAP_CONTENT_B,
            "slug": "yadgar-future-roadmap",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is not None, (
            "_sim_gate_for_drainer did not block near-duplicate. "
            "This reproduces the 2026-05-30 incident class."
        )
        assert rejection.get("stored") is False
        assert rejection.get("reason") == "duplicate_detected"
        assert "candidates" in rejection
        slugs = [c["slug"] for c in rejection["candidates"]]
        assert "yadgar-roadmap-future-improvements" in slugs

    def test_force_bypass_allows_write(self):
        """force=True bypasses _sim_gate_for_drainer."""
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        payload = {
            "title": "Yadgar Future Roadmap",
            "content": _ROADMAP_CONTENT_B,
            "slug": "yadgar-future-roadmap",
            "branch": None,
            "force": True,  # bypass
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, f"force=True should bypass gate but gate returned: {rejection}"

    def test_replace_slug_bypasses_gate(self):
        """replace_slug bypasses _sim_gate_for_drainer."""
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        payload = {
            "title": "Yadgar Future Roadmap",
            "content": _ROADMAP_CONTENT_B,
            "slug": "yadgar-future-roadmap",
            "branch": None,
            "force": False,
            "replace_slug": "yadgar-roadmap-future-improvements",  # bypass
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, f"replace_slug should bypass gate but gate returned: {rejection}"

    def test_distinct_pages_allowed_through_gate(self):
        """False-positive control: distinct pages return None from gate."""
        _add("Yadgar Architecture", _ARCH_CONTENT)
        _add("Yadgar Hook System", _HOOKS_CONTENT)

        payload = {
            "title": "Yadgar Benchmark Results v5.26.0",
            "content": _BENCHMARK_CONTENT,
            "slug": "yadgar-benchmark-results-v5260",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, f"False positive: distinct page flagged. Got: {rejection}"

    def test_gate_disabled_by_env(self, monkeypatch):
        """WIKI_SIM_GATE_ENABLED=False skips _sim_gate_for_drainer."""
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        from yadgar.config import get_settings

        orig_settings = get_settings()

        class _DisabledSettings:
            def __getattr__(self, name):
                if name == "WIKI_SIM_GATE_ENABLED":
                    return False
                return getattr(orig_settings, name)

        import yadgar.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_settings", lambda: _DisabledSettings())

        payload = {
            "title": "Yadgar Future Roadmap",
            "content": _ROADMAP_CONTENT_B,
            "slug": "yadgar-future-roadmap",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, f"Gate ran despite WIKI_SIM_GATE_ENABLED=False. Got: {rejection}"

    def test_soft_mode_allows_with_warning(self, monkeypatch):
        """WIKI_SIM_MODE=soft returns None from _sim_gate_for_drainer."""
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        from yadgar.config import get_settings

        orig_settings = get_settings()

        class _SoftSettings:
            def __getattr__(self, name):
                if name == "WIKI_SIM_MODE":
                    return "soft"
                return getattr(orig_settings, name)

        import yadgar.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_settings", lambda: _SoftSettings())

        payload = {
            "title": "Yadgar Future Roadmap",
            "content": _ROADMAP_CONTENT_B,
            "slug": "yadgar-future-roadmap",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, f"Soft mode should allow write but gate returned: {rejection}"

    def test_append_skips_gate(self):
        """append=True bypasses _sim_gate_for_drainer."""
        _add("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        payload = {
            "title": "Yadgar Future Roadmap",
            "content": _ROADMAP_CONTENT_B,
            "slug": "yadgar-future-roadmap",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": True,  # bypass
        }
        rejection = _drainer_gate(payload)
        assert rejection is None, f"append=True should bypass gate but got: {rejection}"
