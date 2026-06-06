"""v5.46.15 TDD — seed anchors via daemon REST endpoint (RED).

Tests for the yadgar.cli.seed rewrite that drops the dead `yadgar.db` import
and replaces it with HTTP POST to /hooks/seed-anchor on the daemon.

Architecture note (DEVIATION from spec): spec said "POST to MCP memorize
endpoint". Actual MCP transport is streamable-HTTP at POST /mcp with JSON-RPC 2.0
envelope + SSE framing — no existing call-site to copy and fragile to parse.
Decision: add thin REST wrapper /hooks/seed-anchor (same pattern as
/hooks/subagent-stop) that internally calls _srv.memorize(). Single write path
preserved — daemon still owns all SurrealDB writes. Documented in CHANGELOG.

Runner note: T2/T3/T4 dynamic tests load seed.py via spec_from_file_location to
avoid yadgar package __init__ dragging in yadgar.server (requires mcp module).
T1 and T5 are pure static (read source file, no import).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SEED_PY = REPO_ROOT / "yadgar" / "cli" / "seed.py"
SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"


def _load_seed_module():
    """Load yadgar/cli/seed.py directly — bypass yadgar package __init__."""
    spec = importlib.util.spec_from_file_location("yadgar_cli_seed_isolated", SEED_PY)
    mod = importlib.util.module_from_spec(spec)
    # Inject into sys.modules under isolated name to avoid caching conflicts
    sys.modules["yadgar_cli_seed_isolated"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── T1: No dead import ────────────────────────────────────────────────────────


class TestNoDeadImport:
    """T1: _seed_anchors must not import yadgar.db (source regex check)."""

    def test_seed_py_does_not_import_yadgar_db(self):
        """Regex check on source: no 'from yadgar.db' or 'import yadgar.db'."""
        content = SEED_PY.read_text()
        forbidden = re.compile(r"\bfrom\s+yadgar\.db\b|\bimport\s+yadgar\.db\b")
        matches = forbidden.findall(content)
        assert matches == [], f"seed.py still contains dead yadgar.db import(s): {matches}"

    def test_seed_py_does_not_call_get_db(self):
        """No get_db() call site remaining in seed.py."""
        content = SEED_PY.read_text()
        assert "get_db" not in content, "seed.py still references get_db — dead code not removed"


# ── T2: HTTP POST to daemon endpoint ─────────────────────────────────────────


class TestSeedAnchorsHTTP:
    """T2: _seed_anchors posts each anchor to /hooks/seed-anchor via urllib."""

    def test_seed_anchors_posts_to_daemon(self):
        """_seed_anchors calls urlopen at least twice: /health probe + anchor POSTs."""
        mod = _load_seed_module()
        anchors = [
            {"content": "Anchor one", "tags": ["wiki", "setup"]},
            {"content": "Anchor two", "tags": ["config"]},
        ]

        health_resp = MagicMock()
        health_resp.status = 200
        health_resp.read.return_value = b'{"status": "ok"}'
        health_resp.__enter__ = lambda s: s
        health_resp.__exit__ = MagicMock(return_value=False)

        post_resp = MagicMock()
        post_resp.read.return_value = b'{"status": "ok", "created": 1}'
        post_resp.__enter__ = lambda s: s
        post_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [health_resp, post_resp, post_resp]
            result = mod._seed_anchors(anchors, db_path=None, dry_run=False)

        # at least health probe + 2 anchor POSTs
        assert mock_urlopen.call_count >= 2, (
            f"Expected ≥2 urlopen calls (health + anchors), got {mock_urlopen.call_count}"
        )
        assert result["loaded"] == 2

    def test_seed_anchors_sends_is_protected_true(self):
        """Each anchor POST body includes is_protected=True."""
        import json as _json

        mod = _load_seed_module()
        anchors = [{"content": "Protected anchor", "tags": ["_anchor"]}]

        captured_bodies: list[dict] = []

        health_resp = MagicMock()
        health_resp.status = 200
        health_resp.read.return_value = b'{"status": "ok"}'
        health_resp.__enter__ = lambda s: s
        health_resp.__exit__ = MagicMock(return_value=False)

        post_resp = MagicMock()
        post_resp.read.return_value = b'{"status": "ok", "created": 1}'
        post_resp.__enter__ = lambda s: s
        post_resp.__exit__ = MagicMock(return_value=False)

        import urllib.request as _ur

        original_request_cls = _ur.Request

        def capture_request(url, data=None, headers=None):
            if data and b"content" in data:
                try:
                    captured_bodies.append(_json.loads(data.decode()))
                except Exception:
                    pass
            return original_request_cls(url, data=data, headers=headers or {})

        with patch("urllib.request.urlopen", side_effect=[health_resp, post_resp]):
            with patch("urllib.request.Request", side_effect=capture_request):
                mod._seed_anchors(anchors, db_path=None, dry_run=False)

        anchor_posts = [b for b in captured_bodies if "is_protected" in b]
        assert len(anchor_posts) >= 1, (
            f"No anchor POST with is_protected captured. Bodies: {captured_bodies}"
        )
        assert anchor_posts[0]["is_protected"] is True, (
            f"is_protected not True in POST body: {anchor_posts[0]}"
        )

    def test_seed_anchors_includes_anchor_tag(self):
        """Each anchor POST body includes '_anchor' in tags."""
        import json as _json

        mod = _load_seed_module()
        anchors = [{"content": "Anchor content", "tags": ["wiki"]}]

        captured_bodies: list[dict] = []

        health_resp = MagicMock()
        health_resp.status = 200
        health_resp.read.return_value = b'{"status": "ok"}'
        health_resp.__enter__ = lambda s: s
        health_resp.__exit__ = MagicMock(return_value=False)

        post_resp = MagicMock()
        post_resp.read.return_value = b'{"status": "ok", "created": 1}'
        post_resp.__enter__ = lambda s: s
        post_resp.__exit__ = MagicMock(return_value=False)

        import urllib.request as _ur

        original_request_cls = _ur.Request

        def capture_request(url, data=None, headers=None):
            if data and b"content" in data:
                try:
                    captured_bodies.append(_json.loads(data.decode()))
                except Exception:
                    pass
            return original_request_cls(url, data=data, headers=headers or {})

        with patch("urllib.request.urlopen", side_effect=[health_resp, post_resp]):
            with patch("urllib.request.Request", side_effect=capture_request):
                mod._seed_anchors(anchors, db_path=None, dry_run=False)

        anchor_posts = [b for b in captured_bodies if "tags" in b]
        assert len(anchor_posts) >= 1, f"No anchor POST with tags captured: {captured_bodies}"
        tags = anchor_posts[0].get("tags", [])
        assert "_anchor" in tags, f"_anchor not in POST tags: {tags}"

    def test_seed_anchors_sends_content_from_yaml(self):
        """POST body content matches the anchor entry content."""
        import json as _json

        mod = _load_seed_module()
        anchors = [{"content": "The canonical content text", "tags": ["test"]}]

        captured_bodies: list[dict] = []

        health_resp = MagicMock()
        health_resp.status = 200
        health_resp.read.return_value = b'{"status": "ok"}'
        health_resp.__enter__ = lambda s: s
        health_resp.__exit__ = MagicMock(return_value=False)

        post_resp = MagicMock()
        post_resp.read.return_value = b'{"status": "ok", "created": 1}'
        post_resp.__enter__ = lambda s: s
        post_resp.__exit__ = MagicMock(return_value=False)

        import urllib.request as _ur

        original_request_cls = _ur.Request

        def capture_request(url, data=None, headers=None):
            if data and b"content" in data:
                try:
                    captured_bodies.append(_json.loads(data.decode()))
                except Exception:
                    pass
            return original_request_cls(url, data=data, headers=headers or {})

        with patch("urllib.request.urlopen", side_effect=[health_resp, post_resp]):
            with patch("urllib.request.Request", side_effect=capture_request):
                mod._seed_anchors(anchors, db_path=None, dry_run=False)

        anchor_posts = [b for b in captured_bodies if "content" in b]
        assert len(anchor_posts) >= 1
        assert anchor_posts[0]["content"] == "The canonical content text", (
            f"Content mismatch: {anchor_posts[0].get('content')!r}"
        )


# ── T3: Daemon unreachable → graceful exit ────────────────────────────────────


class TestDaemonUnreachable:
    """T3: URLError on /health probe → skipped result with reason='daemon_unreachable'."""

    def test_daemon_unreachable_returns_skipped_result(self):
        """urllib.error.URLError on /health → result has skipped=N, reason='daemon_unreachable'."""
        import urllib.error

        mod = _load_seed_module()
        anchors = [
            {"content": "A", "tags": ["t1"]},
            {"content": "B", "tags": ["t2"]},
        ]

        with patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")
        ):
            result = mod._seed_anchors(anchors, db_path=None, dry_run=False)

        assert result.get("reason") == "daemon_unreachable", (
            f"Expected reason='daemon_unreachable', got: {result}"
        )
        assert result.get("skipped") == 2, f"Expected skipped=2, got: {result}"

    def test_daemon_unreachable_does_not_raise(self):
        """_seed_anchors must not raise when daemon is unreachable."""
        import urllib.error

        mod = _load_seed_module()
        anchors = [{"content": "X", "tags": []}]

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            result = mod._seed_anchors(anchors, db_path=None, dry_run=False)

        assert isinstance(result, dict), "Expected dict result even on daemon down"

    def test_daemon_unreachable_logs_instructional_message(self, capsys):
        """Instructional message printed to stderr when daemon is unreachable."""
        import urllib.error

        mod = _load_seed_module()
        anchors = [{"content": "X", "tags": []}]

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            mod._seed_anchors(anchors, db_path=None, dry_run=False)

        stderr = capsys.readouterr().err
        assert "Daemon not running" in stderr or "systemctl" in stderr or "yadgar seed" in stderr, (
            f"No instructional message in stderr: {stderr!r}"
        )


# ── T4: Dry-run preserves current behavior ───────────────────────────────────


class TestDryRun:
    """T4: dry_run=True logs to stderr, returns result, makes zero HTTP calls."""

    def test_dry_run_no_http_calls(self):
        """dry_run=True must make zero urlopen calls."""
        mod = _load_seed_module()
        anchors = [{"content": "X", "tags": ["a"]}, {"content": "Y", "tags": ["b"]}]

        with patch("urllib.request.urlopen") as mock_urlopen:
            mod._seed_anchors(anchors, db_path=None, dry_run=True)

        assert mock_urlopen.call_count == 0, (
            f"dry_run=True must not call urlopen, called {mock_urlopen.call_count} times"
        )

    def test_dry_run_returns_result_dict(self):
        """dry_run=True returns result with dry_run=True and loaded count."""
        mod = _load_seed_module()
        anchors = [{"content": "X", "tags": ["a"]}]

        result = mod._seed_anchors(anchors, db_path=None, dry_run=True)

        assert result.get("dry_run") is True
        assert result.get("loaded") == 1

    def test_dry_run_logs_to_stderr(self, capsys):
        """dry_run=True prints DRY RUN indicator to stderr."""
        mod = _load_seed_module()
        anchors = [{"content": "Test content", "tags": ["a"]}]

        mod._seed_anchors(anchors, db_path=None, dry_run=True)

        stderr = capsys.readouterr().err
        assert "DRY RUN" in stderr or "dry" in stderr.lower(), (
            f"No dry-run indicator in stderr: {stderr!r}"
        )


# ── T5: setup.sh _wait_for_daemon static checks ──────────────────────────────


class TestSetupShWaitForDaemon:
    """T5: setup.sh _step_seed_anchors polls /health and handles daemon down gracefully."""

    def test_setup_sh_has_wait_for_daemon_function(self):
        """_wait_for_daemon function defined in yadgar-setup.sh."""
        content = SETUP_SH.read_text()
        assert "_wait_for_daemon()" in content, (
            "_wait_for_daemon function not found in yadgar-setup.sh"
        )

    def test_setup_sh_step_seed_anchors_calls_wait_for_daemon(self):
        """_step_seed_anchors calls _wait_for_daemon before yadgar seed."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_step_seed_anchors\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None, "_step_seed_anchors function not found"
        body = m.group(1)
        assert "_wait_for_daemon" in body, (
            f"_step_seed_anchors body does not call _wait_for_daemon:\n{body}"
        )

    def test_setup_sh_wait_for_daemon_probes_health_endpoint(self):
        """_wait_for_daemon probes localhost:8765/health."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_wait_for_daemon\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None, "_wait_for_daemon function not found"
        body = m.group(1)
        assert "8765/health" in body or "localhost:8765" in body, (
            f"_wait_for_daemon does not probe /health endpoint:\n{body}"
        )

    def test_setup_sh_step_seed_anchors_skips_on_timeout(self):
        """_step_seed_anchors returns 0 and prints instructional message when daemon not ready."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_step_seed_anchors\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None
        body = m.group(1)
        assert "return 0" in body, (
            "_step_seed_anchors must return 0 (not fail) when daemon not ready"
        )
        assert "yadgar seed" in body, (
            "_step_seed_anchors must print instructional 'yadgar seed' command on timeout"
        )

    def test_setup_sh_wait_for_daemon_attempts_systemctl_start_on_linux(self):
        """_wait_for_daemon attempts systemctl --user start yadgar.target."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_wait_for_daemon\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None
        body = m.group(1)
        assert "systemctl" in body, "_wait_for_daemon must try to start daemon via systemctl"

    def test_setup_sh_wait_for_daemon_appears_at_least_twice(self):
        """_wait_for_daemon defined once and called at least once → ≥2 occurrences."""
        content = SETUP_SH.read_text()
        count = content.count("_wait_for_daemon")
        assert count >= 2, (
            f"_wait_for_daemon appears {count} times; expected ≥2 (definition + call)"
        )
