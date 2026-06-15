"""Tests for FileChanged hook endpoint — v5.3.6 (M1b + Q4).

Covers:
1. PLAN_*.md modification → memorize call with _plan tag.
2. Other markdown file → no memorize.
3. Unchanged PLAN file (same hash) → no duplicate memorize.
4. install_hooks registers FileChanged with append-if-absent semantics.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_PLAN_RE = re.compile(r"[/\\]docs[/\\]plans[/\\]([^/\\]+\.md)$")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_plan_file(
    tmp_path: Path,
    name: str = "viz-data-fidelity.md",
    content: str = "# Plan\n\nSome content.",
) -> Path:
    docs = tmp_path / "docs" / "plans"
    docs.mkdir(parents=True, exist_ok=True)
    p = docs / name
    p.write_text(content, encoding="utf-8")
    return p


def _run_plan_handler(plan_path, match, storage, memorize_fn=None):
    """Run _handle_plan_file with an optional fake memorize override."""
    from yadgar.server.http import _handle_plan_file

    if memorize_fn is not None:
        fake_srv = MagicMock()
        fake_srv.memorize = memorize_fn
        with patch.dict(sys.modules, {"yadgar.server": fake_srv}):
            return asyncio.run(_handle_plan_file(str(plan_path), match, storage))
    return asyncio.run(_handle_plan_file(str(plan_path), match, storage))


# ── Test 1: PLAN_*.md modification → memorize with _plan tag ────────────────


class TestPlanFileMemorize:
    """PLAN_*.md changes are memorized with _plan tag."""

    def test_plan_file_triggers_memorize(self, tmp_path):
        """PLAN_V5_3.md change → memorize called, response ok."""
        import yadgar.server._state as _st

        plan_path = _make_plan_file(tmp_path, name="viz-data-fidelity.md")
        _st._plan_file_hashes.clear()

        mock_storage = MagicMock()
        memorize_calls = []

        def capturing_memorize(**kwargs):
            memorize_calls.append(kwargs)
            return {"stored": True}

        match = _PLAN_RE.search(str(plan_path))
        result = json.loads(
            _run_plan_handler(plan_path, match, mock_storage, memorize_fn=capturing_memorize).body
        )

        assert result["status"] == "ok"
        assert result["memorized"] is True
        assert result["file"] == "viz-data-fidelity.md"
        assert len(memorize_calls) == 1

    def test_plan_file_memorize_includes_plan_tag(self, tmp_path):
        """Memorize call includes _plan tag."""
        import yadgar.server._state as _st

        plan_path = _make_plan_file(tmp_path, name="viz-data-fidelity.md")
        _st._plan_file_hashes.clear()

        mock_storage = MagicMock()
        memorize_calls = []

        def capturing_memorize(**kwargs):
            memorize_calls.append(kwargs)
            return {"stored": True}

        match = _PLAN_RE.search(str(plan_path))
        _run_plan_handler(plan_path, match, mock_storage, memorize_fn=capturing_memorize)

        assert len(memorize_calls) == 1
        tags = memorize_calls[0].get("tags", [])
        assert "_plan" in tags


# ── Test 2: Other markdown file → no memorize ───────────────────────────────


class TestPlanPathFilter:
    """Only PLAN_*.md files under docs/ trigger memorize."""

    def test_plan_file_path_detected(self, tmp_path):
        from yadgar.hooks.file_changed import is_plan_file_path

        plan_path = _make_plan_file(tmp_path)
        assert is_plan_file_path(str(plan_path)) is True

    def test_regular_markdown_not_detected(self, tmp_path):
        from yadgar.hooks.file_changed import is_plan_file_path

        other_md = tmp_path / "docs" / "README.md"
        other_md.parent.mkdir(exist_ok=True)
        other_md.write_text("hello")
        assert is_plan_file_path(str(other_md)) is False

    def test_non_docs_plan_file_not_detected(self, tmp_path):
        from yadgar.hooks.file_changed import is_plan_file_path

        wrong_dir = tmp_path / "other" / "plan.md"
        wrong_dir.parent.mkdir(parents=True, exist_ok=True)
        wrong_dir.write_text("content")
        assert is_plan_file_path(str(wrong_dir)) is False

    def test_archived_plan_not_detected(self, tmp_path):
        # docs/plans/archive/ holds shipped/dead plans — frozen, must NOT re-memorize.
        from yadgar.hooks.file_changed import is_plan_file_path

        archived = tmp_path / "docs" / "plans" / "archive" / "PLAN_V5_3.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text("old plan")
        assert is_plan_file_path(str(archived)) is False

    def test_legacy_top_level_plan_not_detected(self, tmp_path):
        # Pre-migration docs/PLAN_*.md location is no longer the convention.
        from yadgar.hooks.file_changed import is_plan_file_path

        legacy = tmp_path / "docs" / "PLAN_V5_3.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("legacy")
        assert is_plan_file_path(str(legacy)) is False

    def test_main_posts_for_plan_file(self, tmp_path, monkeypatch):
        """main() calls _post_file_changed for a docs/PLAN_*.md path."""
        import io

        from yadgar.hooks import file_changed

        posted = []
        monkeypatch.setattr(file_changed, "_post_file_changed", lambda *a: posted.append(a))

        plan_path = str(_make_plan_file(tmp_path))
        payload = json.dumps({"file_path": plan_path, "file_action": "modified"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        file_changed.main()
        assert len(posted) == 1

    def test_main_skips_other_markdown(self, tmp_path, monkeypatch):
        """main() does NOT post for non-PLAN markdown files."""
        import io

        from yadgar.hooks import file_changed

        posted = []
        monkeypatch.setattr(file_changed, "_post_file_changed", lambda *a: posted.append(a))

        other = tmp_path / "docs" / "CHANGELOG.md"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("content")
        payload = json.dumps({"file_path": str(other), "file_action": "modified"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        file_changed.main()
        assert posted == []


# ── Test 3: unchanged PLAN file → no duplicate memorize ─────────────────────


class TestPlanHashDedup:
    """Same-hash PLAN file → skipped, no duplicate memorize."""

    def test_unchanged_plan_skipped(self, tmp_path):
        """Second call with same content → status=skipped."""
        import yadgar.server._state as _st

        plan_path = _make_plan_file(tmp_path, name="PLAN_V5_3.md")
        _st._plan_file_hashes.clear()

        mock_storage = MagicMock()
        memorize_fn = MagicMock(return_value={"stored": True})
        match = _PLAN_RE.search(str(plan_path))

        r1 = json.loads(
            _run_plan_handler(plan_path, match, mock_storage, memorize_fn=memorize_fn).body
        )
        assert r1["status"] == "ok"

        # Second call — same content, same hash
        r2 = json.loads(
            _run_plan_handler(plan_path, match, mock_storage, memorize_fn=memorize_fn).body
        )
        assert r2["status"] == "skipped"
        assert r2["reason"] == "unchanged"

    def test_changed_content_reruns_memorize(self, tmp_path):
        """Content change clears hash cache → memorize runs again."""
        import yadgar.server._state as _st

        plan_path = _make_plan_file(tmp_path, name="PLAN_V5_3.md", content="# Version A")
        _st._plan_file_hashes.clear()

        mock_storage = MagicMock()
        memorize_fn = MagicMock(return_value={"stored": True})
        match = _PLAN_RE.search(str(plan_path))

        r1 = json.loads(
            _run_plan_handler(plan_path, match, mock_storage, memorize_fn=memorize_fn).body
        )
        assert r1["status"] == "ok"

        # Update file content
        plan_path.write_text("# Version B — updated content", encoding="utf-8")

        r2 = json.loads(
            _run_plan_handler(plan_path, match, mock_storage, memorize_fn=memorize_fn).body
        )
        assert r2["status"] == "ok"
        assert r2["memorized"] is True


# ── Test 4: install_hooks registers FileChanged ──────────────────────────────


class TestInstallHooksFileChanged:
    """install_hooks registers FileChanged event with append-if-absent semantics."""

    def test_file_changed_registered(self, tmp_path):
        """install_hooks writes FileChanged hook to settings.json."""
        from yadgar.install_hooks_lib import install_hooks_impl

        result = install_hooks_impl(tmp_path, "global", str(tmp_path / "proj"), dry_run=True)
        preview = result["preview"]
        hooks = preview.get("hooks", {})
        assert "FileChanged" in hooks
        entries = hooks["FileChanged"]
        assert len(entries) >= 1
        cmd = entries[0]["hooks"][0]["command"]
        assert "yadgar-file-changed.py" in cmd

    def test_file_changed_not_duplicated_on_rerun(self, tmp_path):
        """Second install_hooks call does not add duplicate FileChanged entry."""
        from yadgar.install_hooks_lib import install_hooks_impl

        r1 = install_hooks_impl(tmp_path, "global", str(tmp_path / "proj"), dry_run=True)
        preview1 = r1["preview"]
        fc_count_1 = len(preview1["hooks"]["FileChanged"])

        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(preview1))

        r2 = install_hooks_impl(tmp_path, "global", str(tmp_path / "proj"), dry_run=True)
        preview2 = r2["preview"]
        fc_count_2 = len(preview2["hooks"]["FileChanged"])

        assert fc_count_2 == fc_count_1
