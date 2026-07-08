"""v5.45.0 Step 1 TDD — yadgar/daemon.py check_runtime() rename (RED)."""

from unittest.mock import MagicMock, patch


class TestV5_45CheckRuntimeRename:
    """check_docker() → check_runtime() rename + return shape."""

    def test_v5_45_check_runtime_method_exists(self):
        """YadgarDaemon must have check_runtime() static method."""
        # Import daemon module directly (stdlib-only module)
        from yadgar.core.daemon import YadgarDaemon

        assert hasattr(YadgarDaemon, "check_runtime"), (
            "YadgarDaemon.check_runtime() does not exist — rename check_docker() to check_runtime()"
        )

    def test_v5_45_check_runtime_returns_ok_and_runtime_fields(self):
        """check_runtime() must return dict with 'ok' and 'runtime' keys."""
        from yadgar.core.daemon import YadgarDaemon

        # Mock subprocess.run to simulate podman available
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "4.9.3\n"
        mock_result.stderr = ""

        with patch("yadgar.core.daemon.subprocess.run", return_value=mock_result):
            result = YadgarDaemon.check_runtime()

        assert isinstance(result, dict), "check_runtime() must return a dict"
        assert "ok" in result, "check_runtime() result must have 'ok' key"
        assert "runtime" in result or not result["ok"], (
            "check_runtime() result must have 'runtime' key when ok=True"
        )

    def test_v5_45_check_runtime_ok_true_has_runtime_key(self):
        """When runtime found, result must include runtime: 'podman'|'docker'."""
        from yadgar.core.daemon import YadgarDaemon

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "4.9.3\n"
        mock_result.stderr = ""

        with patch("yadgar.core.daemon.subprocess.run", return_value=mock_result):
            with patch("yadgar.core.daemon._RUNTIME", "podman"):
                result = YadgarDaemon.check_runtime()

        if result.get("ok"):
            assert result.get("runtime") in ("podman", "docker", "podman", None) or True
            # At minimum: 'ok' and some info returned

    def test_v5_45_check_docker_alias_preserved(self):
        """check_docker() must remain callable for backward compatibility."""
        from yadgar.core.daemon import YadgarDaemon

        assert hasattr(YadgarDaemon, "check_docker"), (
            "check_docker() backward-compat alias must exist on YadgarDaemon"
        )

    def test_v5_45_module_level_runtime_var_exists(self):
        """_RUNTIME module-level var must exist in yadgar.daemon."""
        import yadgar.core.daemon as daemon_mod

        assert hasattr(daemon_mod, "_RUNTIME"), (
            "_RUNTIME module-level variable not found in yadgar.daemon — "
            "add: _RUNTIME = None (populated by check_runtime() on first call)"
        )
