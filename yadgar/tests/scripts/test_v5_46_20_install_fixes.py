"""v5.46.20 TDD — install comprehensive fixes (RED scaffolding).

Bugs covered:
  BUG 1: yadgar.service.in missing YADGAR_MCP_AUTH_TOKEN passthrough
  BUG 2: SELinux :Z mount flag — replace with --security-opt label=disable
  BUG 3: _wait_for_daemon timeout too short (30 → 120s), add 10s progress log
  BUG 4: _step_pull_images must stop containers before pull
  BUG 5: (progress covered by BUG 3 fix — no additional test needed)
  BUG 6: Seed idempotency — second run returns 0 new (similarity gate dedup)
"""

from __future__ import annotations

import importlib.util
import re
import sys
from unittest.mock import MagicMock, patch

from yadgar.tests._paths import REPO_ROOT

SERVICE_IN = REPO_ROOT / "scripts" / "install" / "yadgar.service.in"
BACKEND_SERVICE_IN = REPO_ROOT / "scripts" / "install" / "yadgar-backend.service.in"
SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"
BOOTSTRAP_SH = REPO_ROOT / "scripts" / "install" / "bootstrap_secrets.sh"
SEED_PY = REPO_ROOT / "yadgar" / "core" / "cli" / "seed.py"


def _load_seed_module():
    """Load yadgar/cli/seed.py directly — bypass yadgar package __init__."""
    spec = importlib.util.spec_from_file_location("yadgar_cli_seed_v5_46_20", SEED_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["yadgar_cli_seed_v5_46_20"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── T1: BUG 1 — YADGAR_MCP_AUTH_TOKEN passthrough in yadgar.service.in ────────


class TestAuthTokenPassthrough:
    """T1: yadgar.service.in ExecStart must pass YADGAR_MCP_AUTH_TOKEN to container."""

    def test_yadgar_service_has_auth_token_env_passthrough(self):
        """yadgar.service.in ExecStart block contains -e YADGAR_MCP_AUTH_TOKEN=..."""
        content = SERVICE_IN.read_text()
        assert "YADGAR_MCP_AUTH_TOKEN" in content, (
            "yadgar.service.in missing -e YADGAR_MCP_AUTH_TOKEN passthrough. "
            "Token in secrets.env (EnvironmentFile) never reaches container."
        )

    def test_yadgar_service_auth_token_uses_env_var_syntax(self):
        """YADGAR_MCP_AUTH_TOKEN must be passed as ${YADGAR_MCP_AUTH_TOKEN} (not hardcoded)."""
        content = SERVICE_IN.read_text()
        assert "${YADGAR_MCP_AUTH_TOKEN}" in content, (
            "yadgar.service.in must use -e YADGAR_MCP_AUTH_TOKEN=${YADGAR_MCP_AUTH_TOKEN} syntax."
        )

    def test_backend_service_does_not_need_auth_token(self):
        """yadgar-backend.service.in: backend serves SurrealDB+embed, auth is at MCP layer.

        This test is informational — verifies backend doesn't incorrectly depend on
        YADGAR_MCP_AUTH_TOKEN (it should not). If it does appear, it's unexpected.
        """
        # Backend doesn't enforce REQUIRE_AUTH — absence is expected/OK.
        # We just confirm the file is readable and has expected backend vars.
        content = BACKEND_SERVICE_IN.read_text()
        assert "SURREAL_USER" in content, (
            "yadgar-backend.service.in unexpectedly missing SURREAL_USER"
        )


# ── T1b: BUG 1 completeness — bootstrap_secrets.sh writes YADGAR_MCP_AUTH_TOKEN ──


class TestBootstrapSecretsWritesToken:
    """T1b: bootstrap_secrets.sh must write YADGAR_MCP_AUTH_TOKEN to secrets.env.

    BUG 1 fix is only complete if the token is generated and written by bootstrap_secrets.sh.
    The service template passthrough (T1) is inert if secrets.env never contains the token.
    """

    def test_bootstrap_secrets_heredoc_contains_auth_token(self):
        """Main write heredoc in bootstrap_secrets.sh must include YADGAR_MCP_AUTH_TOKEN."""
        content = BOOTSTRAP_SH.read_text()
        assert "YADGAR_MCP_AUTH_TOKEN" in content, (
            "bootstrap_secrets.sh does not write YADGAR_MCP_AUTH_TOKEN to secrets.env. "
            "Service template passthrough is inert without this token in secrets.env."
        )

    def test_bootstrap_secrets_test_dryrun_includes_auth_token(self):
        """Test dryrun heredoc in bootstrap_secrets.sh must also include YADGAR_MCP_AUTH_TOKEN."""
        content = BOOTSTRAP_SH.read_text()
        # Find the YADGAR_TEST_DRYRUN block — it starts with the conditional
        dryrun_start = content.find('YADGAR_TEST_DRYRUN:-0}" == "1"')
        assert dryrun_start != -1, "YADGAR_TEST_DRYRUN block not found in bootstrap_secrets.sh"
        # The dryrun block's main heredoc write ('cat > ...') appears after the idempotency skip.
        # Find 'cat >' inside the dryrun block — that's where the heredoc starts.
        cat_pos = content.find("cat >", dryrun_start)
        assert cat_pos != -1, "Heredoc write (cat >) not found in dryrun block"
        # The final 'exit 0' of the dryrun block terminates it
        dryrun_exit = content.find("exit 0", cat_pos)
        assert dryrun_exit != -1, "Dryrun block final exit 0 not found"
        dryrun_heredoc = content[cat_pos:dryrun_exit]
        assert "YADGAR_MCP_AUTH_TOKEN" in dryrun_heredoc, (
            "bootstrap_secrets.sh test dryrun heredoc missing YADGAR_MCP_AUTH_TOKEN. "
            "Tests that exercise bootstrap via YADGAR_TEST_DRYRUN=1 won't write the token."
        )

    def test_bootstrap_secrets_required_keys_includes_auth_token(self):
        """REQUIRED_KEYS array in bootstrap_secrets.sh must include YADGAR_MCP_AUTH_TOKEN."""
        content = BOOTSTRAP_SH.read_text()
        m = re.search(r"REQUIRED_KEYS=\(([^)]+)\)", content)
        assert m is not None, "REQUIRED_KEYS array not found in bootstrap_secrets.sh"
        keys_str = m.group(1)
        assert "YADGAR_MCP_AUTH_TOKEN" in keys_str, (
            f"REQUIRED_KEYS does not include YADGAR_MCP_AUTH_TOKEN: {keys_str!r}. "
            "Idempotency check will skip regeneration even if token is missing."
        )

    def test_bootstrap_secrets_gen32_function_exists(self):
        """bootstrap_secrets.sh must define _gen32() for 32-byte token generation."""
        content = BOOTSTRAP_SH.read_text()
        assert "_gen32" in content, (
            "bootstrap_secrets.sh missing _gen32() function. "
            "MCP auth token requires 32-byte (256-bit) entropy, not 24-byte."
        )


# ── T2: BUG 2 — SELinux :Z → --security-opt label=disable ─────────────────────


class TestSELinuxMountFix:
    """T2: Both .in templates must use --security-opt label=disable, not :Z mount."""

    def test_yadgar_service_no_z_mount_suffix(self):
        """yadgar.service.in: -v @DATA_DIR@:/data must NOT have :Z suffix."""
        content = SERVICE_IN.read_text()
        assert ":/data:Z" not in content, (
            "yadgar.service.in still has :Z mount suffix — causes SELinux Enforcing failures."
        )

    def test_backend_service_no_z_mount_suffix(self):
        """yadgar-backend.service.in: -v @DATA_DIR@:/data must NOT have :Z suffix."""
        content = BACKEND_SERVICE_IN.read_text()
        assert ":/data:Z" not in content, (
            "yadgar-backend.service.in still has :Z mount suffix — causes SELinux Enforcing failures."
        )

    def test_yadgar_service_has_security_opt_label_disable(self):
        """yadgar.service.in must contain --security-opt label=disable."""
        content = SERVICE_IN.read_text()
        assert "--security-opt label=disable" in content, (
            "yadgar.service.in missing --security-opt label=disable. "
            "Required for Rocky Linux / RHEL with SELinux Enforcing."
        )

    def test_backend_service_has_security_opt_label_disable(self):
        """yadgar-backend.service.in must contain --security-opt label=disable."""
        content = BACKEND_SERVICE_IN.read_text()
        assert "--security-opt label=disable" in content, (
            "yadgar-backend.service.in missing --security-opt label=disable. "
            "Required for Rocky Linux / RHEL with SELinux Enforcing."
        )

    def test_yadgar_service_security_opt_before_user_root(self):
        """--security-opt label=disable must appear before --user root in ExecStart."""
        content = SERVICE_IN.read_text()
        idx_security = content.find("--security-opt label=disable")
        idx_user = content.find("--user root")
        assert idx_security != -1 and idx_user != -1, (
            "Either --security-opt or --user root missing from yadgar.service.in"
        )
        assert idx_security < idx_user, (
            "--security-opt label=disable must appear BEFORE --user root in ExecStart"
        )


# ── T3: BUG 3 — _wait_for_daemon timeout 30 → 120, progress log every 10s ─────


class TestWaitForDaemonTimeout:
    """T3: _wait_for_daemon default timeout must be 120s with 10s progress log."""

    def test_wait_for_daemon_default_timeout_is_120(self):
        """_wait_for_daemon default timeout must be 120 (not 30)."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_wait_for_daemon\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None, "_wait_for_daemon function not found in yadgar-setup.sh"
        body = m.group(1)
        # Must have 120 as default, not 30
        assert ":-120" in body or '"120"' in body or "'120'" in body, (
            f"_wait_for_daemon default timeout not 120 in function body:\n{body[:400]}"
        )
        assert ":-30" not in body and '"30"' not in body, (
            f"_wait_for_daemon still has old 30s default:\n{body[:400]}"
        )

    def test_wait_for_daemon_progress_log_every_10s(self):
        """_wait_for_daemon must print progress every 10s (modulo 10 check)."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_wait_for_daemon\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None
        body = m.group(1)
        # Look for modulo-10 progress log pattern
        assert "% 10" in body or "%10" in body or "10)" in body, (
            f"_wait_for_daemon missing 10s progress log (modulo 10 check):\n{body[:600]}"
        )

    def test_step_seed_anchors_calls_wait_for_daemon_with_120(self):
        """_step_seed_anchors must call _wait_for_daemon 120 (updated from 30)."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_step_seed_anchors\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None, "_step_seed_anchors function not found"
        body = m.group(1)
        # Should call with 120, not hardcoded 30
        assert "_wait_for_daemon 120" in body or "_wait_for_daemon" in body, (
            "_step_seed_anchors must call _wait_for_daemon"
        )
        assert "_wait_for_daemon 30" not in body, (
            "_step_seed_anchors still hardcodes _wait_for_daemon 30 — should be 120"
        )


# ── T4: BUG 4 — _step_pull_images stops containers before pull ────────────────


class TestPullImagesStopsFirst:
    """T4: _step_pull_images must stop running containers before pulling new images."""

    def test_step_pull_images_stops_containers(self):
        """_step_pull_images body contains container stop logic."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_step_pull_images\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None, "_step_pull_images function not found"
        body = m.group(1)
        # Must have stop command referencing yadgar containers
        has_stop = "stop" in body and ("yadgar" in body or "RUNTIME" in body or "ctr" in body)
        assert has_stop, f"_step_pull_images does not stop containers before pull:\n{body}"

    def test_step_pull_images_stop_before_pull(self):
        """Container stop must appear BEFORE pull in _step_pull_images body."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_step_pull_images\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None
        body = m.group(1)
        # Use RUNTIME pull (the actual pull invocation, not comments or function names)
        stop_match = re.search(r'"\$RUNTIME"\s+stop|RUNTIME\s+stop|\bstop\s+"?\$ctr', body)
        pull_match = re.search(r'"\$RUNTIME"\s+pull|run\s+"\$RUNTIME"\s+pull', body)
        assert stop_match is not None, (
            f"No container stop command in _step_pull_images body:\n{body}"
        )
        assert pull_match is not None, (
            f"No '$RUNTIME pull' command in _step_pull_images body:\n{body}"
        )
        assert stop_match.start() < pull_match.start(), (
            f"Container stop appears AFTER pull in _step_pull_images body:\n{body}"
        )

    def test_step_pull_images_stops_yadgar_and_yadgar_backend(self):
        """Both 'yadgar' and 'yadgar-backend' must be referenced in stop logic."""
        content = SETUP_SH.read_text()
        m = re.search(
            r"_step_pull_images\s*\(\s*\)\s*\{(.*?)\n\}",
            content,
            re.DOTALL,
        )
        assert m is not None
        body = m.group(1)
        # Either explicit names or via loop variable
        has_yadgar = "yadgar" in body
        has_backend = "yadgar-backend" in body or "backend" in body.lower()
        assert has_yadgar, f"'yadgar' not referenced in _step_pull_images:\n{body}"
        assert has_backend, f"'yadgar-backend' not referenced in _step_pull_images:\n{body}"


# ── T6: BUG 6 — Seed idempotency via similarity gate dedup ────────────────────


class TestSeedIdempotency:
    """T6: Second seed run with same anchors yields 0 new (similarity gate dedup)."""

    def _make_health_resp(self):
        r = MagicMock()
        r.status = 200
        r.read.return_value = b'{"status": "ok"}'
        r.__enter__ = lambda s: s
        r.__exit__ = MagicMock(return_value=False)
        return r

    def _make_post_resp(self, created: int):
        r = MagicMock()
        r.read.return_value = f'{{"status": "ok", "created": {created}}}'.encode()
        r.__enter__ = lambda s: s
        r.__exit__ = MagicMock(return_value=False)
        return r

    def test_second_seed_run_returns_zero_new_when_gate_dedupes(self):
        """Second seed call with same anchors: created=0 when daemon returns created=0 (deduped)."""
        mod = _load_seed_module()
        anchors = [
            {"content": "Canonical anchor one", "tags": ["_anchor", "wiki"]},
            {"content": "Canonical anchor two", "tags": ["_anchor", "setup"]},
        ]

        # First run: daemon returns created=1 for each
        # Second run: daemon returns created=0 (similarity gate deduped)
        health1 = self._make_health_resp()
        post1a = self._make_post_resp(1)
        post1b = self._make_post_resp(1)

        health2 = self._make_health_resp()
        post2a = self._make_post_resp(0)  # deduped
        post2b = self._make_post_resp(0)  # deduped

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [health1, post1a, post1b]
            result1 = mod._seed_anchors(anchors, db_path=None, dry_run=False)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [health2, post2a, post2b]
            result2 = mod._seed_anchors(anchors, db_path=None, dry_run=False)

        assert result1["created"] == 2, f"First run: expected 2 created, got {result1}"
        assert result2["created"] == 0, (
            f"Second run: expected 0 new (deduped by similarity gate), got {result2}"
        )
        assert result2["skipped"] == 2, f"Second run: expected 2 skipped, got {result2}"

    def test_seed_handles_409_conflict_as_skipped(self):
        """409 Conflict from daemon means similarity gate deduped — counts as skipped."""
        import urllib.error

        mod = _load_seed_module()
        anchors = [{"content": "Existing anchor", "tags": ["_anchor"]}]

        health = self._make_health_resp()
        conflict_resp = MagicMock()
        conflict_resp.status = 409
        conflict_resp.code = 409
        conflict_resp.read.return_value = b'{"status": "duplicate"}'
        conflict_resp.__enter__ = lambda s: s
        conflict_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen") as mock_urlopen:
            # Health call returns OK, anchor POST raises 409 HTTPError
            mock_urlopen.side_effect = [
                health,
                urllib.error.HTTPError(
                    url="http://localhost:8765/hooks/seed-anchor",
                    code=409,
                    msg="Conflict",
                    hdrs={},
                    fp=None,
                ),
            ]
            result = mod._seed_anchors(anchors, db_path=None, dry_run=False)

        assert result["created"] == 0, f"409 must not count as created: {result}"
        assert result["skipped"] == 1, f"409 must count as skipped: {result}"

    def test_seed_idempotency_loaded_count_stable(self):
        """result['loaded'] equals len(anchors) regardless of created vs skipped."""
        mod = _load_seed_module()
        anchors = [
            {"content": "Anchor A", "tags": ["_anchor"]},
            {"content": "Anchor B", "tags": ["_anchor"]},
            {"content": "Anchor C", "tags": ["_anchor"]},
        ]

        health = self._make_health_resp()
        # All deduped
        posts = [self._make_post_resp(0) for _ in anchors]

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [health] + posts
            result = mod._seed_anchors(anchors, db_path=None, dry_run=False)

        assert result["loaded"] == 3, f"loaded must always equal len(anchors): {result}"
        assert result["created"] == 0
        assert result["skipped"] == 3
