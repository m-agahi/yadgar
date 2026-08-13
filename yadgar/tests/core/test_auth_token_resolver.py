"""Single shared bearer-token resolver (2026-07-29, third-instance fix).

``YADGAR_MCP_AUTH_TOKEN`` was resolved by THREE hand-rolled copies:
``mcp_register.resolve_mcp_auth_token`` (env → secrets.env),
``cli/seed.py::_read_auth_token`` (the same pattern re-typed), and
``runtime_config_client`` (``os.environ`` ONLY — the copy that was WRONG).

Because ``/api/`` is auth-gated (``auth_middleware:34``) and no installer
sources ``secrets.env``, every host-side runtime-config WRITE silently returned
False. Benign on the default path (``code_graph.enabled`` already defaults
true), but it made ``--no-code-graph`` a no-op: the ``false`` row never landed.

These tests pin ONE resolver (``core.install.auth_token``) and that all three
call sites route through it.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from yadgar.core.install.auth_token import parse_secrets_env_token, resolve_auth_token

# ── the resolver itself ──────────────────────────────────────────────────────


class TestResolveAuthToken:
    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=file-token\n")
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "  env-token  ")
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets))
        assert resolve_auth_token() == "env-token"

    def test_falls_back_to_secrets_env(self, tmp_path, monkeypatch):
        """The whole point: an unsourced shell still resolves the token."""
        secrets = tmp_path / "secrets.env"
        secrets.write_text("OTHER=1\nYADGAR_MCP_AUTH_TOKEN=file-token-abc\nTRAIL=x\n")
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets))
        assert resolve_auth_token() == "file-token-abc"

    def test_empty_env_falls_back(self, tmp_path, monkeypatch):
        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=file-token-2\n")
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "")
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets))
        assert resolve_auth_token() == "file-token-2"

    def test_both_absent_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(tmp_path / "nope" / "secrets.env"))
        assert resolve_auth_token() == ""


class TestParseSecretsEnvToken:
    def test_missing_file_never_raises(self, tmp_path):
        assert parse_secrets_env_token(tmp_path / "absent.env") == ""

    def test_no_matching_line_returns_empty(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text("LEGACY=yes\nNO_TOKEN_HERE=1\n")
        assert parse_secrets_env_token(secrets) == ""

    def test_commented_line_is_not_a_match(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text("# YADGAR_MCP_AUTH_TOKEN=not-real\n")
        assert parse_secrets_env_token(secrets) == ""

    def test_quoted_value_is_unwrapped(self, tmp_path):
        """Superset of the two merged parsers — ``seed.py`` stripped quotes,
        ``mcp_register`` did not. Tokens are urlsafe-base64 so stripping is
        inert for the latter's callers."""
        secrets = tmp_path / "secrets.env"
        secrets.write_text('YADGAR_MCP_AUTH_TOKEN="quoted-token"\n')
        assert parse_secrets_env_token(secrets) == "quoted-token"


# ── call site 1: runtime_config_client (the bug) ─────────────────────────────


def _fake_2xx(status: int = 200):
    class _Resp(io.BytesIO):
        def __init__(self):
            super().__init__(b"{}")
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def _fake_get_response(payload: dict):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(json.dumps(payload).encode())


class TestRuntimeConfigClientTokenFallback:
    """The acceptance criterion that makes ``--no-code-graph`` real."""

    def _capture_auth(self, monkeypatch, tmp_path, call):
        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=from-secrets-file\n")
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets))

        from yadgar.core import runtime_config_client as rcc

        seen: dict = {}

        def _capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return _fake_2xx(200) if req.get_method() != "GET" else _fake_get_response({"value": 1})

        with patch.object(rcc._req, "urlopen", side_effect=_capture):
            call(rcc)
        return seen.get("auth")

    def test_set_attaches_bearer_from_secrets_env(self, tmp_path, monkeypatch):
        auth = self._capture_auth(
            monkeypatch, tmp_path, lambda rcc: rcc.set("k", True, scope="global")
        )
        assert auth == "Bearer from-secrets-file"

    def test_delete_attaches_bearer_from_secrets_env(self, tmp_path, monkeypatch):
        auth = self._capture_auth(monkeypatch, tmp_path, lambda rcc: rcc.delete("k"))
        assert auth == "Bearer from-secrets-file"

    def test_get_attaches_bearer_from_secrets_env(self, tmp_path, monkeypatch):
        """``get`` had its OWN inline env-only read (line 57) — /api/ is auth-gated,
        so an unsourced stop-hook read 401'd and silently fail-opened to the
        caller's default, making a stored per-repo opt-out unreadable."""
        auth = self._capture_auth(monkeypatch, tmp_path, lambda rcc: rcc.get("k", default=0))
        assert auth == "Bearer from-secrets-file"

    def test_no_token_anywhere_sends_no_header(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(tmp_path / "absent.env"))

        from yadgar.core import runtime_config_client as rcc

        seen: dict = {}

        def _capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return _fake_2xx(200)

        with patch.object(rcc._req, "urlopen", side_effect=_capture):
            rcc.set("k", True, scope="global")
        assert seen["auth"] is None


# ── call sites 2 + 3: no fourth copy ─────────────────────────────────────────


class TestNoDuplicateResolvers:
    def test_mcp_register_delegates(self, tmp_path, monkeypatch):
        from yadgar.core.install.clients import mcp_register as mr

        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=shared-impl\n")
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets))
        assert mr.resolve_mcp_auth_token() == "shared-impl"
        assert mr.resolve_mcp_auth_token is resolve_auth_token

    def test_seed_delegates(self, tmp_path, monkeypatch):
        from yadgar.core.cli.seed import _read_auth_token

        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=shared-impl\n")
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets))
        assert _read_auth_token() == "shared-impl"

    def test_version_delegates(self, tmp_path, monkeypatch):
        """Car 9 (bug train): version.py used to carry its own hand-rolled
        ``_read_auth_token`` copy — the exact anti-pattern this module's
        docstring forbids. It now imports the shared resolver directly."""
        from yadgar.core.cli.version import resolve_auth_token as version_resolve_auth_token

        assert version_resolve_auth_token is resolve_auth_token
