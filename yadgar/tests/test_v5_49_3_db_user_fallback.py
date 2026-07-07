"""v5.49.3 hotfix TDD — DB credential fallback + setup emits alias keys (Rocky VM iter-3).

Bug 14:
  Path A: storage layer raises KeyError when YADGAR_DB_USER absent — fix with fallback
          to YADGAR_RW_USER via _resolve_db_credentials() helper.
  Path B: yadgar setup secrets.env missing YADGAR_DB_USER / YADGAR_DB_PASS lines.
"""

import pytest

# ---------------------------------------------------------------------------
# Path A — _resolve_db_credentials() helper
# ---------------------------------------------------------------------------


class TestResolveDbCredentials:
    """Tests for yadgar.storage._resolve_db_credentials()."""

    def test_storage_engine_uses_db_user_when_set(self, monkeypatch):
        """Explicit YADGAR_DB_USER takes priority over YADGAR_RW_USER."""
        monkeypatch.setenv("YADGAR_DB_USER", "explicit_db_user")
        monkeypatch.setenv("YADGAR_DB_PASS", "explicit_db_pass")
        monkeypatch.setenv("YADGAR_RW_USER", "rw_user")
        monkeypatch.setenv("YADGAR_RW_PASS", "rw_pass")

        from yadgar._shared.storage import _resolve_db_credentials

        user, _pass = _resolve_db_credentials()
        assert user == "explicit_db_user"
        assert _pass == "explicit_db_pass"

    def test_storage_engine_falls_back_to_rw_user(self, monkeypatch):
        """Falls back to YADGAR_RW_USER / YADGAR_RW_PASS when DB_USER not set.

        No KeyError must be raised — this is the Rocky VM Bug 14 scenario.
        """
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_PASS", raising=False)
        monkeypatch.setenv("YADGAR_RW_USER", "rw_user")
        monkeypatch.setenv("YADGAR_RW_PASS", "rw_pass")

        from yadgar._shared.storage import _resolve_db_credentials

        user, _pass = _resolve_db_credentials()
        assert user == "rw_user"
        assert _pass == "rw_pass"

    def test_storage_engine_raises_clear_error_when_neither_set(self, monkeypatch):
        """Raises ValueError naming both var pairs when nothing is set."""
        monkeypatch.delenv("YADGAR_DB_USER", raising=False)
        monkeypatch.delenv("YADGAR_DB_PASS", raising=False)
        monkeypatch.delenv("YADGAR_RW_USER", raising=False)
        monkeypatch.delenv("YADGAR_RW_PASS", raising=False)

        from yadgar._shared.storage import _resolve_db_credentials

        with pytest.raises((ValueError, RuntimeError)) as exc_info:
            _resolve_db_credentials()

        msg = str(exc_info.value)
        assert "YADGAR_DB_USER" in msg
        assert "YADGAR_RW_USER" in msg


# ---------------------------------------------------------------------------
# Path B — _render_secrets_env() produces DB alias keys
# ---------------------------------------------------------------------------


class TestSecretsEnvAliasKeys:
    """Tests for yadgar.cli.setup._render_secrets_env()."""

    def _render(self) -> str:
        from yadgar.core.cli.setup import _render_secrets_env

        return _render_secrets_env(
            token="tok123",
            db_pass="dbpass456",
            rw_pass="rwpass789",
            ro_pass="ropass000",
        )

    def _parse_env(self, content: str) -> dict[str, str]:
        """Parse systemd EnvironmentFile format: KEY=VALUE lines, skip comments/blank."""
        result: dict[str, str] = {}
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key, _, value = stripped.partition("=")
                result[key.strip()] = value.strip()
        return result

    def test_setup_secrets_env_contains_db_user_alias(self):
        """Generated secrets.env contains YADGAR_DB_USER line."""
        content = self._render()
        env = self._parse_env(content)
        assert "YADGAR_DB_USER" in env, (
            "YADGAR_DB_USER missing from secrets.env — Bug 14 / Path B not fixed"
        )
        # Alias must match the RW user value (hardcoded 'yadgar' username)
        assert env["YADGAR_DB_USER"] == "yadgar"

    def test_setup_secrets_env_contains_db_pass_alias(self):
        """Generated secrets.env contains YADGAR_DB_PASS line with same value as YADGAR_RW_PASS."""
        content = self._render()
        env = self._parse_env(content)
        assert "YADGAR_DB_PASS" in env, (
            "YADGAR_DB_PASS missing from secrets.env — Bug 14 / Path B not fixed"
        )
        # Alias must match the RW pass value
        assert env["YADGAR_DB_PASS"] == env["YADGAR_RW_PASS"]

    def test_setup_secrets_env_no_shell_expansion(self):
        """No ${ or :- expressions — systemd EnvironmentFile is literal KEY=VALUE only."""
        content = self._render()
        assert "${" not in content, "Shell ${VAR} expansion found — breaks systemd EnvironmentFile"
        assert ":-" not in content, "Shell :- expansion found — breaks systemd EnvironmentFile"
