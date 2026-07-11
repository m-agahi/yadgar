"""Config file permission tests (S2 — v5.2.0 security cluster).

Verifies that cmd_config_set writes config.yaml with mode 0o600 (owner read/write only).
H-9: config written without chmod 600 — credentials potentially world-readable.
"""

import os

from yadgar._shared.config.config_yaml import cmd_config_set


class TestConfigYamlPermissions:
    def test_config_yaml_write_is_0o600(self, tmp_path, monkeypatch):
        """config.yaml is written with mode 0o600 after cmd_config_set."""
        monkeypatch.setenv("YADGAR_CONFIG", str(tmp_path / "config.yaml"))
        # get_config_path reads YADGAR_CONFIG env var when set
        monkeypatch.setattr(
            "yadgar._shared.config.config_yaml.get_config_path",
            lambda: tmp_path / "config.yaml",
        )

        class _FakeArgs:
            key = "db_path"
            value = "/tmp/test.db"

        cmd_config_set(_FakeArgs())

        path = tmp_path / "config.yaml"
        assert path.exists(), "config.yaml was not created"
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600, (
            f"config.yaml has mode {oct(mode)} — expected 0o600. Credentials may be world-readable."
        )

    def test_config_yaml_write_is_0o600_on_overwrite(self, tmp_path, monkeypatch):
        """chmod 0o600 is applied even when overwriting an existing config.yaml."""
        config_path = tmp_path / "config.yaml"
        # Create file with permissive mode first
        config_path.write_text("")
        os.chmod(config_path, 0o644)

        monkeypatch.setattr(
            "yadgar._shared.config.config_yaml.get_config_path",
            lambda: config_path,
        )

        class _FakeArgs:
            key = "db_path"
            value = "/tmp/other.db"

        cmd_config_set(_FakeArgs())

        mode = os.stat(config_path).st_mode & 0o777
        assert mode == 0o600, f"config.yaml overwrite left mode {oct(mode)} — expected 0o600."
