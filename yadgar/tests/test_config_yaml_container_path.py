"""Step A: container yaml loading tests (v5.7.10).

Tests that YADGAR_CONFIG_FILE env override works for the yaml loader used in
container deployments where ~/.yadgar/ doesn't exist (--user root, /data bind-mount).
"""

from pathlib import Path


class TestGetConfigPath:
    """get_config_path() respects YADGAR_CONFIG_FILE env override."""

    def test_default_path_when_env_unset(self, monkeypatch):
        """Env unset → returns ~/.config/yadgar/config.yaml expanded."""
        monkeypatch.delenv("YADGAR_CONFIG_FILE", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        from yadgar._shared.config_yaml import get_config_path

        result = get_config_path()
        assert result == Path("~/.config/yadgar/config.yaml").expanduser()

    def test_env_override_returned(self, monkeypatch, tmp_path):
        """YADGAR_CONFIG_FILE set → returns that exact path."""
        custom = tmp_path / "custom" / "config.yaml"
        monkeypatch.setenv("YADGAR_CONFIG_FILE", str(custom))
        from yadgar._shared.config_yaml import get_config_path

        result = get_config_path()
        assert result == custom

    def test_env_override_empty_string_uses_default(self, monkeypatch):
        """YADGAR_CONFIG_FILE='' (empty) → falls through to default."""
        monkeypatch.setenv("YADGAR_CONFIG_FILE", "")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        from yadgar._shared.config_yaml import get_config_path

        result = get_config_path()
        assert result == Path("~/.config/yadgar/config.yaml").expanduser()

    def test_env_override_whitespace_uses_default(self, monkeypatch):
        """YADGAR_CONFIG_FILE='   ' (whitespace) → falls through to default."""
        monkeypatch.setenv("YADGAR_CONFIG_FILE", "   ")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        from yadgar._shared.config_yaml import get_config_path

        result = get_config_path()
        assert result == Path("~/.config/yadgar/config.yaml").expanduser()


class TestYamlConfigSourceLoad:
    """YamlConfigSource._load() reads from YADGAR_CONFIG_FILE when set."""

    def test_env_override_path_is_read(self, monkeypatch, tmp_path):
        """When YADGAR_CONFIG_FILE points at a valid yaml, Settings picks it up."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("port: 9999\n")
        monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_file))

        # Re-import to pick up the patched env (import cache cleared via module reload)
        import importlib

        import yadgar._shared.config as cfg_mod

        importlib.reload(cfg_mod)
        source = cfg_mod.YamlConfigSource(cfg_mod.Settings)
        assert source._data.get("PORT") == 9999

    def test_nonexistent_env_override_returns_empty_no_crash(self, monkeypatch, tmp_path):
        """YADGAR_CONFIG_FILE pointing at a missing file → empty dict, no exception."""
        missing = tmp_path / "does_not_exist.yaml"
        monkeypatch.setenv("YADGAR_CONFIG_FILE", str(missing))

        import importlib

        import yadgar._shared.config as cfg_mod

        importlib.reload(cfg_mod)
        source = cfg_mod.YamlConfigSource(cfg_mod.Settings)
        assert source._data == {}

    def test_default_path_missing_returns_empty_no_crash(self, monkeypatch, tmp_path):
        """Env unset, default path missing → empty dict, no exception (regression guard)."""
        monkeypatch.delenv("YADGAR_CONFIG_FILE", raising=False)
        # Patch get_config_path to point at a non-existent file
        monkeypatch.setattr(
            "yadgar._shared.config_yaml.get_config_path",
            lambda: tmp_path / "nonexistent.yaml",
        )

        import importlib

        import yadgar._shared.config as cfg_mod

        importlib.reload(cfg_mod)
        source = cfg_mod.YamlConfigSource(cfg_mod.Settings)
        assert source._data == {}
