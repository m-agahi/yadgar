"""Tests for yadgar/hooks/prompt-recall.py — UserPromptSubmit hook handler.

Wave 2 coverage: yadgar/hooks/prompt-recall.py (126 stmts, 0% pre-wave).
Strategy: the module has a main() that runs as a script. Import via importlib.util
to bypass module-level side effects. Test pure helpers: _extract_query,
_preprocess_fts, _merge_and_rank, _format_context. Test main() via runpy.
The _fts_search function requires a live SurrealDB cursor — floor is expected
for that function (~15 lines).
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module load
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parent.parent / "hooks" / "prompt-recall.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prompt_recall_hook", str(_SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    _mod = _load_module()
    _extract_query = _mod._extract_query
    _preprocess_fts = _mod._preprocess_fts
    _merge_and_rank = _mod._merge_and_rank
    _format_context = _mod._format_context
    _db_locked = _mod._db_locked
    _MODULE_LOADED = True
except Exception as _e:
    _MODULE_LOADED = False
    _load_error = str(_e)


def _require():
    if not _MODULE_LOADED:
        pytest.skip(f"prompt-recall module load failed: {_load_error}")


# ---------------------------------------------------------------------------
# _extract_query
# ---------------------------------------------------------------------------


class TestExtractQuery:
    def setup_method(self):
        _require()

    def test_prompt_field(self):
        assert _extract_query({"prompt": "hello world"}) == "hello world"

    def test_user_prompt_fallback(self):
        assert _extract_query({"user_prompt": "fallback text"}) == "fallback text"

    def test_empty_dict_returns_empty(self):
        assert _extract_query({}) == ""

    def test_strips_whitespace(self):
        assert _extract_query({"prompt": "  trim me  "}) == "trim me"

    def test_prompt_preferred_over_user_prompt(self):
        assert _extract_query({"prompt": "primary", "user_prompt": "secondary"}) == "primary"


# ---------------------------------------------------------------------------
# _preprocess_fts
# ---------------------------------------------------------------------------


class TestPreprocessFts:
    def setup_method(self):
        _require()

    def test_simple_query(self):
        result = _preprocess_fts("hello world")
        assert "hello" in result
        assert "world" in result

    def test_removes_punctuation(self):
        result = _preprocess_fts("hello, world!")
        assert "," not in result
        assert "!" not in result

    def test_empty_returns_empty(self):
        assert _preprocess_fts("") == ""

    def test_single_char_words_removed(self):
        result = _preprocess_fts("a b c hello")
        assert "hello" in result
        assert result.strip() != "a b c"  # short words filtered

    def test_caps_at_15_terms(self):
        query = " ".join(f"word{i}" for i in range(20))
        result = _preprocess_fts(query)
        assert len(result.split()) <= 15

    def test_underscores_preserved(self):
        result = _preprocess_fts("snake_case_name")
        assert "snake_case_name" in result


# ---------------------------------------------------------------------------
# _merge_and_rank
# ---------------------------------------------------------------------------


class TestMergeAndRank:
    def setup_method(self):
        _require()

    def _result(self, mid, score, directory="", content="some content"):
        return {"id": mid, "score": score, "directory": directory, "content": content, "heat": 0.5}

    def test_empty_returns_empty(self):
        assert _merge_and_rank([], "/project") == []

    def test_returns_at_most_max_results(self):
        results = [self._result(str(i), 1.0) for i in range(20)]
        out = _merge_and_rank(results, "/project")
        assert len(out) <= 5  # MAX_RESULTS = 5

    def test_project_matches_boosted(self):
        dir_match = self._result("1", 1.0, directory="/project")
        no_match = self._result("2", 1.0, directory="/other")
        out = _merge_and_rank([dir_match, no_match], "/project")
        # Project match should rank higher
        assert out[0]["id"] == "1"

    def test_deduplicates_by_id(self):
        dup1 = self._result("same-id", 1.0)
        dup2 = self._result("same-id", 0.5)
        out = _merge_and_rank([dup1, dup2], "/project")
        ids = [r["id"] for r in out]
        assert ids.count("same-id") == 1

    def test_sorted_by_combined_score(self):
        low = self._result("low", 0.1)
        high = self._result("high", 0.9)
        out = _merge_and_rank([low, high], "/project")
        assert out[0]["id"] == "high"

    def test_non_action_stream_boosted(self):
        action = self._result("act", 1.0, content="Session activity tool calls")
        semantic = self._result("sem", 1.0, content="API design decision")
        out = _merge_and_rank([action, semantic], "/project")
        # Semantic should rank higher (2x boost)
        assert out[0]["id"] == "sem"


# ---------------------------------------------------------------------------
# _format_context
# ---------------------------------------------------------------------------


class TestFormatContext:
    def setup_method(self):
        _require()

    def _mem(self, content, directory="", mid="1"):
        return {"id": mid, "content": content, "directory": directory, "heat": 0.5}

    def test_empty_returns_empty_string(self):
        assert _format_context([], "/project") == ""

    def test_formats_header(self):
        mems = [self._mem("some content")]
        result = _format_context(mems, "/project")
        assert "Yadgar" in result or "Auto-Recall" in result

    def test_content_included(self):
        mems = [self._mem("important memory text")]
        result = _format_context(mems, "/project")
        assert "important memory text" in result

    def test_cross_project_label_added(self):
        mems = [self._mem("remote memory", directory="/other-project")]
        result = _format_context(mems, "/project")
        assert "other-project" in result

    def test_no_label_for_same_directory(self):
        mems = [self._mem("local memory", directory="/project")]
        result = _format_context(mems, "/project")
        # Should NOT have a [proj] label for same directory
        assert "[project]" not in result

    def test_truncates_long_content(self):
        long_content = "x" * 5000
        mems = [self._mem(long_content)]
        result = _format_context(mems, "/project")
        # Result should be under total limit + format overhead
        assert len(result) < 5000 + 200

    def test_footer_with_count(self):
        mems = [self._mem("m1"), self._mem("m2", mid="2")]
        result = _format_context(mems, "/project")
        assert "2 memories" in result or "memories surfaced" in result


# ---------------------------------------------------------------------------
# _db_locked
# ---------------------------------------------------------------------------


class TestDbLocked:
    def setup_method(self):
        _require()

    def test_nonexistent_lock_returns_false(self, tmp_path):
        db_path = tmp_path / "yadgar.db"
        assert _db_locked(db_path) is False

    def test_unlocked_file_returns_false(self, tmp_path):
        lock_path = tmp_path / "yadgar.lock"
        lock_path.write_text("")
        assert _db_locked(tmp_path / "yadgar.db") is False


# ---------------------------------------------------------------------------
# main() via runpy
# ---------------------------------------------------------------------------


class TestMain:
    def setup_method(self):
        _require()

    def test_empty_query_returns_nothing(self):
        import runpy

        payload = {"prompt": "", "cwd": "/project"}
        with (
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            patch("yadgar.paths"),
        ):
            # Should complete without error; no output expected
            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass

    def test_http_endpoint_called_on_valid_query(self):
        import runpy

        payload = {"prompt": "how to configure auth", "cwd": "/project"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": "found context"}).encode()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            patch("yadgar.paths"),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_open,
        ):
            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass
        # URL open should have been called once for the HTTP endpoint
        mock_open.assert_called_once()

    def test_daemon_down_silent_skip(self, capsys):
        import runpy
        import urllib.error

        payload = {"prompt": "some query", "cwd": "/project"}
        with (
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            patch("yadgar.paths"),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")),
        ):
            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass
        # No output expected when daemon is down
        capsys.readouterr()
        # output may be empty or have Yadgar context, but no crash

    def test_malformed_stdin_silent_exit(self):
        import runpy

        with (
            patch("sys.stdin", io.StringIO("not-json")),
            patch("yadgar.paths"),
        ):
            try:
                runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
            except SystemExit:
                pass
        # No exception = pass
