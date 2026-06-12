"""Tests for v5.53.1 Phase C — Live curation loop.

Covers:
1. stale_wiki_count reflects real hash-drift (mock drifted page → count rises;
   no drift → 0); signals call stays cheap (TTL cache, no full rescan per call).
2. wiki_refresh_stale returns stale slugs in its result (stale_count + suggested_calls).
3. Dedup gate: near-duplicate wiki_add (no force) returns suggested_update_slug
   pointing at the match; force=True still bypasses; non-duplicate stores normally.
4. Write-back nudge: stop hook prompt includes consolidate-onto-existing step.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

ROADMAP_A = """\
# Yadgar Roadmap 2026

## Planned features

- Semantic search improvements via better embedding models
- Wiki deduplication via similarity gate
- Performance improvements to the signals hot path
- TTL-cached stale count to avoid rescans
"""

ROADMAP_B = """\
# Yadgar 2026 Roadmap

## Features Planned

- Better embedding model for semantic search
- Similarity gate for wiki deduplication
- Signals hot-path performance via TTL cache
- Stale wiki count caching
"""


# ---------------------------------------------------------------------------
# 1. stale_wiki_count via _compute_stale_wiki_count (TTL-cached)
# ---------------------------------------------------------------------------


class TestStaleWikiCount:
    """_compute_stale_wiki_count returns real drift count, not hardcoded 0."""

    def _write_wiki_page(
        self, wiki_dir: Path, slug: str, source_files: list[str], source_hash: str
    ) -> None:
        """Write a .md file with frontmatter referencing source_files + hash."""
        content = (
            "---\n"
            f"hash: {source_hash}\n"
            f"source_files:\n" + "".join(f"  - {f}\n" for f in source_files) + "---\n\n# Page\n"
        )
        (wiki_dir / f"{slug}.md").write_text(content)

    def test_no_drift_returns_zero(self, tmp_path):
        """No hash drift → count == 0."""
        from yadgar.server.tools.project import _compute_stale_wiki_count

        # Fresh dir with no wiki subdir → 0
        assert _compute_stale_wiki_count(str(tmp_path)) == 0

    def test_drifted_page_count_rises(self, tmp_path):
        """A page whose source file changed → count == 1."""
        from yadgar.server.tools.project import (
            _compute_stale_wiki_count,
            _stale_count_cache,
        )

        wiki_dir = tmp_path / ".local-review" / "wiki"
        wiki_dir.mkdir(parents=True)

        source = tmp_path / "module.py"
        source.write_text("def foo(): pass")
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        self._write_wiki_page(wiki_dir, "mod-module", [str(source)], original_hash)

        # No drift yet
        _stale_count_cache.clear()
        assert _compute_stale_wiki_count(str(tmp_path)) == 0

        # Simulate drift: change source file content
        source.write_text("def foo(): return 42")
        _stale_count_cache.clear()  # Bust TTL to force rescan

        count = _compute_stale_wiki_count(str(tmp_path))
        assert count == 1, f"Expected 1 stale page, got {count}"

    def test_multiple_drifted_pages(self, tmp_path):
        """Two pages drift → count == 2."""
        from yadgar.server.tools.project import (
            _compute_stale_wiki_count,
            _stale_count_cache,
        )

        wiki_dir = tmp_path / ".local-review" / "wiki"
        wiki_dir.mkdir(parents=True)

        for i in range(2):
            src = tmp_path / f"module{i}.py"
            src.write_text(f"original_{i}")
            h = hashlib.sha256(src.read_bytes()).hexdigest()
            self._write_wiki_page(wiki_dir, f"mod-module{i}", [str(src)], h)
            # Now change the source
            src.write_text(f"changed_{i}")

        _stale_count_cache.clear()
        assert _compute_stale_wiki_count(str(tmp_path)) == 2

    def test_ttl_cache_prevents_rescan(self, tmp_path, monkeypatch):
        """Within TTL window, _scan_stale_wiki_slugs is NOT called a second time."""
        from yadgar.server.tools import project as _proj

        call_count = [0]
        original_scan = _proj._scan_stale_wiki_slugs

        def counting_scan(directory: str):
            call_count[0] += 1
            return original_scan(directory)

        monkeypatch.setattr(_proj, "_scan_stale_wiki_slugs", counting_scan)
        _proj._stale_count_cache.clear()

        # First call — should hit the scan
        _proj._compute_stale_wiki_count(str(tmp_path))
        # Second call — should hit the cache (within default 300s TTL)
        _proj._compute_stale_wiki_count(str(tmp_path))

        assert call_count[0] == 1, f"Expected 1 scan call (second hit cache), got {call_count[0]}"

    def test_ttl_zero_disables_cache(self, tmp_path, monkeypatch):
        """TTL=0 disables caching: each call triggers a scan."""
        from yadgar.config import Settings
        from yadgar.server.tools import project as _proj

        call_count = [0]
        original_scan = _proj._scan_stale_wiki_slugs

        def counting_scan(directory: str):
            call_count[0] += 1
            return original_scan(directory)

        monkeypatch.setattr(_proj, "_scan_stale_wiki_slugs", counting_scan)
        monkeypatch.setattr(_proj, "get_settings", lambda: Settings(STALE_COUNT_CACHE_TTL_S=0))
        _proj._stale_count_cache.clear()

        _proj._compute_stale_wiki_count(str(tmp_path))
        _proj._compute_stale_wiki_count(str(tmp_path))

        assert call_count[0] == 2, f"TTL=0 should scan twice, got {call_count[0]}"

    def test_stale_wiki_count_in_signals_not_hardcoded(self, tmp_path, monkeypatch):
        """project_brief signals mode includes stale_wiki_count from real compute."""
        from yadgar.server.tools import project as _proj

        # Patch compute to return a known value
        monkeypatch.setattr(_proj, "_compute_stale_wiki_count", lambda resolved: 3)

        # Also patch all the other stuff needed for signals to not explode
        mock_storage = MagicMock()
        mock_storage._q.return_value = []
        mock_storage.get_anchors.return_value = []

        with patch.object(_proj, "_get_storage", return_value=mock_storage):
            result = _proj._project_brief_signals(
                resolved=str(tmp_path),
                mode="signals",
                init_memory_present=True,
                active_work_present=True,
                init_memory_age_hours=1.0,
                active_work_age_hours=1.0,
                stale_checkpoint_hours=1.0,
                storage=None,
            )

        assert result.get("stale_wiki_count") == 3, (
            f"signals stale_wiki_count should be 3, got {result.get('stale_wiki_count')}"
        )


# ---------------------------------------------------------------------------
# 2. wiki_refresh_stale returns stale slugs prominently
# ---------------------------------------------------------------------------


class TestWikiRefreshStaleReturn:
    """wiki_refresh_stale returns stale_count + suggested_calls."""

    def _write_wiki_page(
        self, wiki_dir: Path, slug: str, source_files: list[str], source_hash: str
    ) -> None:
        content = (
            "---\n"
            f"hash: {source_hash}\n"
            f"source_files:\n" + "".join(f"  - {f}\n" for f in source_files) + "---\n\n# Page\n"
        )
        (wiki_dir / f"{slug}.md").write_text(content)

    def test_stale_slugs_in_return(self, tmp_path):
        """Stale page → stale list contains its slug, stale_count == 1."""
        from yadgar.server.tools.project import _wiki_refresh_stale_impl

        wiki_dir = tmp_path / ".local-review" / "wiki"
        wiki_dir.mkdir(parents=True)

        src = tmp_path / "service.py"
        src.write_text("original")
        h = hashlib.sha256(src.read_bytes()).hexdigest()
        self._write_wiki_page(wiki_dir, "mod-service", [str(src)], h)

        # Drift
        src.write_text("changed")

        result = _wiki_refresh_stale_impl(str(tmp_path), slugs=None, force_branch=True)

        assert "stale" in result
        assert "mod-service" in result["stale"], f"Expected mod-service in stale: {result['stale']}"
        assert result.get("stale_count") == 1
        assert "suggested_calls" in result
        assert len(result["suggested_calls"]) == 1

    def test_no_stale_returns_empty_lists(self, tmp_path):
        """No drift → stale=[], stale_count=0, suggested_calls=[]."""
        from yadgar.server.tools.project import _wiki_refresh_stale_impl

        wiki_dir = tmp_path / ".local-review" / "wiki"
        wiki_dir.mkdir(parents=True)

        src = tmp_path / "utils.py"
        src.write_text("stable content")
        h = hashlib.sha256(src.read_bytes()).hexdigest()
        self._write_wiki_page(wiki_dir, "mod-utils", [str(src)], h)

        result = _wiki_refresh_stale_impl(str(tmp_path), slugs=None, force_branch=True)

        assert result["stale"] == []
        assert result.get("stale_count") == 0
        assert result.get("suggested_calls") == []

    def test_suggested_calls_mention_repo_wiki(self, tmp_path):
        """suggested_calls contain repo-wiki reference for each stale slug."""
        from yadgar.server.tools.project import _wiki_refresh_stale_impl

        wiki_dir = tmp_path / ".local-review" / "wiki"
        wiki_dir.mkdir(parents=True)

        src = tmp_path / "core.py"
        src.write_text("v1")
        h = hashlib.sha256(src.read_bytes()).hexdigest()
        self._write_wiki_page(wiki_dir, "core-module", [str(src)], h)
        src.write_text("v2")

        result = _wiki_refresh_stale_impl(str(tmp_path), slugs=None, force_branch=True)

        calls = result.get("suggested_calls", [])
        assert len(calls) == 1
        assert "repo-wiki" in calls[0]


# ---------------------------------------------------------------------------
# 3. Dedup gate — suggested_update_slug + bypass paths
#
# These tests mock find_similar_wiki_pages to avoid sentence-transformers
# dependency (not installed in this env). Gate logic is unit-tested in
# isolation via _sim_gate_for_drainer directly.
# ---------------------------------------------------------------------------


def _make_drainer_with_mock_wiki(mock_wiki):
    """Build a QueueDrainer and patch _st._wiki to mock_wiki."""
    import tempfile

    import yadgar.server._state as _st
    from yadgar.file_queue import FileQueue, QueueDrainer

    tmp = tempfile.mkdtemp()
    fq = FileQueue(tmp)
    drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
    original_wiki = _st._wiki
    _st._wiki = mock_wiki
    return drainer, _st, original_wiki, tmp


class TestDedupGateSuggestedSlug:
    """v5.53.1: gate returns suggested_update_slug on near-dup reject.

    Uses mocked find_similar_wiki_pages to avoid sentence-transformers dep.
    """

    def _gate(self, payload: dict, candidates: list[dict]) -> dict | None:
        """Call _sim_gate_for_drainer with a mocked wiki returning given candidates."""
        import tempfile

        import yadgar.server._state as _st
        from yadgar.file_queue import FileQueue, QueueDrainer

        mock_wiki = MagicMock()
        mock_wiki.find_similar_wiki_pages.return_value = candidates

        with tempfile.TemporaryDirectory() as tmp:
            fq = FileQueue(tmp)
            drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
            orig = _st._wiki
            _st._wiki = mock_wiki
            try:
                return drainer._sim_gate_for_drainer(payload)
            finally:
                _st._wiki = orig

    def test_near_dup_returns_suggested_update_slug(self):
        """Near-duplicate → rejection includes suggested_update_slug pointing at best match."""
        candidates = [
            {
                "slug": "yadgar-roadmap-future-improvements",
                "title": "Yadgar Roadmap Future Improvements",
                "similarity": 0.93,
                "branch": None,
            },
        ]
        payload = {
            "title": "Yadgar 2026 Roadmap",
            "content": ROADMAP_B,
            "slug": "yadgar-2026-roadmap",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = self._gate(payload, candidates)

        assert rejection is not None, "Gate should have fired for near-dup"
        assert rejection.get("stored") is False
        assert rejection.get("reason") == "duplicate_detected"
        assert "suggested_update_slug" in rejection, (
            f"Missing suggested_update_slug in rejection: {rejection}"
        )
        assert rejection["suggested_update_slug"] == "yadgar-roadmap-future-improvements", (
            f"suggested_update_slug should point at the best candidate, "
            f"got: {rejection['suggested_update_slug']}"
        )

    def test_force_true_bypasses_gate(self):
        """force=True → gate returns None (bypass) regardless of candidates."""
        payload = {
            "title": "Yadgar 2026 Roadmap",
            "content": ROADMAP_B,
            "slug": "yadgar-2026-roadmap",
            "branch": None,
            "force": True,
            "replace_slug": None,
            "append": False,
        }
        # Even with candidates that would trigger rejection, force=True returns None
        # (early return before find_similar_wiki_pages is even called)
        import tempfile

        from yadgar.file_queue import FileQueue, QueueDrainer

        with tempfile.TemporaryDirectory() as tmp:
            fq = FileQueue(tmp)
            drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
            result = drainer._sim_gate_for_drainer(payload)
        assert result is None, f"force=True should bypass gate (return None), got: {result}"

    def test_replace_slug_bypasses_gate(self):
        """replace_slug set → gate returns None (bypass) before find_similar called."""
        import tempfile

        from yadgar.file_queue import FileQueue, QueueDrainer

        payload = {
            "title": "Yadgar 2026 Roadmap",
            "content": ROADMAP_B,
            "slug": "yadgar-2026-roadmap",
            "branch": None,
            "force": False,
            "replace_slug": "yadgar-roadmap-future-improvements",
            "append": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            fq = FileQueue(tmp)
            drainer = QueueDrainer(queue=fq, storage_factory=lambda: None, drain_interval=9999)
            result = drainer._sim_gate_for_drainer(payload)
        assert result is None, f"replace_slug should bypass gate (return None), got: {result}"

    def test_happy_path_no_candidates_returns_none(self):
        """No near-dup candidates → gate returns None (happy path, page stores normally)."""
        payload = {
            "title": "PostgreSQL Configuration Guide",
            "content": "Completely unrelated content about PostgreSQL setup.",
            "slug": "postgresql-config-guide",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        # Mock returns empty list → no near-dup → gate returns None
        result = self._gate(payload, [])
        assert result is None, (
            f"Empty candidates → happy path, gate should return None, got: {result}"
        )

    def test_suggested_update_slug_is_best_candidate(self):
        """suggested_update_slug equals candidates[0]['slug'] (best match by similarity)."""
        candidates = [
            {"slug": "best-match-slug", "title": "Best Match", "similarity": 0.95, "branch": None},
            {
                "slug": "second-match-slug",
                "title": "Second Match",
                "similarity": 0.82,
                "branch": None,
            },
        ]
        payload = {
            "title": "Near Dup Title",
            "content": "Some near-dup content",
            "slug": "near-dup-title",
            "branch": None,
            "force": False,
            "replace_slug": None,
            "append": False,
        }
        rejection = self._gate(payload, candidates)

        assert rejection is not None
        assert rejection["suggested_update_slug"] == "best-match-slug", (
            "suggested_update_slug must be candidates[0]['slug'] (highest similarity)"
        )
        # Verify it's the first candidate (highest similarity)
        assert rejection["candidates"][0]["slug"] == rejection["suggested_update_slug"]


# ---------------------------------------------------------------------------
# 4. Write-back nudge in stop hook prompt
# ---------------------------------------------------------------------------


class TestWriteBackNudgeInStopHook:
    """Stop hook prompt includes write-back consolidation nudge."""

    _HOOK_PATH = Path(__file__).parent.parent / "hooks" / "stop-memory-checkpoint.py"

    def _run_hook(self, hook_path: Path, stdin_data: dict, env: dict | None = None) -> dict:
        """Run the hook script in a subprocess, return parsed stdout."""
        import subprocess

        env_full = {**os.environ, **(env or {})}
        result = subprocess.run(
            [sys.executable, str(hook_path)],
            input=json.dumps(stdin_data),
            capture_output=True,
            text=True,
            env=env_full,
            timeout=10,
        )
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return {"raw_stdout": result.stdout}

    def _make_transcript(self, tmp_path: Path, human_count: int) -> Path:
        lines = []
        for i in range(human_count):
            entry = {"message": {"role": "user", "content": f"Human message {i}"}}
            lines.append(json.dumps(entry))
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("\n".join(lines))
        return transcript

    def test_prompt_contains_wiki_refresh_stale(self, tmp_path):
        """Block reason must mention wiki_refresh_stale (stale regen path)."""
        transcript = self._make_transcript(tmp_path, 25)
        state_dir = tmp_path / ".yadgar"
        state_dir.mkdir()

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = self._run_hook(
                self._HOOK_PATH,
                {
                    "session_id": "sess-writeback-1",
                    "transcript_path": str(transcript),
                    "stop_hook_active": False,
                },
            )

        assert result.get("decision") == "block"
        reason = result.get("reason", "")
        assert "wiki_refresh_stale" in reason, (
            f"Prompt must mention wiki_refresh_stale, got: {reason[:300]}"
        )

    def test_prompt_contains_write_back_consolidate_step(self, tmp_path):
        """Block reason must include write-back / consolidation nudge."""
        transcript = self._make_transcript(tmp_path, 25)
        state_dir = tmp_path / ".yadgar"
        state_dir.mkdir()

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = self._run_hook(
                self._HOOK_PATH,
                {
                    "session_id": "sess-writeback-2",
                    "transcript_path": str(transcript),
                    "stop_hook_active": False,
                },
            )

        assert result.get("decision") == "block"
        reason = result.get("reason", "")
        # Look for the write-back nudge keywords
        assert "consolidate" in reason.lower() or "replace_slug" in reason, (
            f"Prompt must mention consolidation/replace_slug for write-back, got: {reason[:400]}"
        )

    def test_prompt_contains_wiki_history_verification(self, tmp_path):
        """Block reason must mention wiki_history or wiki_diff for post-write verification."""
        transcript = self._make_transcript(tmp_path, 25)
        state_dir = tmp_path / ".yadgar"
        state_dir.mkdir()

        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = self._run_hook(
                self._HOOK_PATH,
                {
                    "session_id": "sess-writeback-3",
                    "transcript_path": str(transcript),
                    "stop_hook_active": False,
                },
            )

        assert result.get("decision") == "block"
        reason = result.get("reason", "")
        assert "wiki_history" in reason or "wiki_diff" in reason, (
            f"Prompt must mention wiki_history or wiki_diff for verification, got: {reason[:400]}"
        )
