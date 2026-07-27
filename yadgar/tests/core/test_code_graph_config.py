"""TDD tests for code_graph config (Car B, ADR-0162; enable migrated in Car G4, ADR-0163).

Coverage
--------
1. is_enabled reads the ``code_graph.enabled`` runtime-config row via a resolver
   (default: the fail-open host client). ON by default (opt-out, ADR-0163 flip
   2026-07-27): no row / daemon down (resolver returns default) → True. Global
   true + per-dir false → that dir disabled.
2. is_opted_out follows is_enabled (per-dir false OR global false → opted out;
   no row → NOT opted out, on by default). The old ``.code-graph-disable``
   marker is GONE — never read.
3. cache_dir lives under yadgar's CACHE_DIR (not the user tree).
4. Car C / Car D constant keys defined (digest budget, refresh cadence).
5. session_suggest_line reads the store via the injected resolver.

The env var ``CODE_GRAPH_ENABLED`` and the ``OPT_OUT_MARKER`` / ``.code-graph-disable``
file are NO LONGER the runtime mechanism — tests inject a fake resolver instead.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _resolver(mapping: dict, default_key_value=False):
    """Build a fake ``(key, directory, default) -> value`` resolver.

    ``mapping`` maps ``directory`` (or ``None`` for global) → the value returned for
    ``code_graph.enabled``. Falls back to the caller's ``default`` when the requested
    directory has no entry AND no global entry — mirroring the real store's per-dir →
    global → default resolution.
    """

    def _r(key, directory=None, default=None):
        assert key == "code_graph.enabled"
        if directory in mapping:
            return mapping[directory]
        if None in mapping:
            return mapping[None]
        return default

    return _r


class TestEnabledFlag:
    def test_default_on_when_no_row(self):
        """ADR-0163 flip (2026-07-27): on by default — opt-out, not opt-in."""
        from yadgar.core.code_graph import config

        # Resolver returns the caller default (True) — nothing stored.
        r = _resolver({})
        assert config.is_enabled("/repo/a", resolver=r) is True

    def test_global_true(self):
        from yadgar.core.code_graph import config

        r = _resolver({None: True})
        assert config.is_enabled("/repo/a", resolver=r) is True
        assert config.is_enabled(None, resolver=r) is True

    def test_global_false_disables(self):
        from yadgar.core.code_graph import config

        r = _resolver({None: False})
        assert config.is_enabled("/repo/a", resolver=r) is False

    def test_per_dir_false_overrides_global_true(self):
        from yadgar.core.code_graph import config

        # global on, but /repo/b explicitly opted out.
        r = _resolver({None: True, "/repo/b": False})
        assert config.is_enabled("/repo/a", resolver=r) is True
        assert config.is_enabled("/repo/b", resolver=r) is False

    def test_daemon_down_returns_true(self):
        """Fail-open resolver (any lookup yields the caller default) → on by default."""
        from yadgar.core.code_graph import config

        def _down(key, directory=None, default=None):
            return default

        assert config.is_enabled("/repo/a", resolver=_down) is True

    def test_default_resolver_is_host_client(self):
        """No resolver → uses runtime_config_client.get with the right key/default."""
        from yadgar.core.code_graph import config

        seen = {}

        def _fake_get(key, directory=None, default=None):
            seen["key"] = key
            seen["directory"] = directory
            seen["default"] = default
            return True

        with patch("yadgar.core.runtime_config_client.get", _fake_get):
            assert config.is_enabled("/repo/x") is True
        assert seen == {"key": "code_graph.enabled", "directory": "/repo/x", "default": True}


class TestOptOut:
    def test_not_opted_out_when_no_row(self):
        """ADR-0163 flip: nothing stored → default True → NOT opted out."""
        from yadgar.core.code_graph import config

        r = _resolver({})
        assert config.is_opted_out("/repo/a", resolver=r) is False

    def test_opted_out_when_explicitly_disabled(self):
        from yadgar.core.code_graph import config

        r = _resolver({None: False})  # explicit global false → opted out
        assert config.is_opted_out("/repo/a", resolver=r) is True

    def test_not_opted_out_when_enabled(self):
        from yadgar.core.code_graph import config

        r = _resolver({None: True})
        assert config.is_opted_out("/repo/a", resolver=r) is False

    def test_opted_out_when_per_dir_false_even_if_global_true(self):
        from yadgar.core.code_graph import config

        r = _resolver({None: True, "/repo/b": False})
        assert config.is_opted_out("/repo/b", resolver=r) is True

    def test_marker_symbol_is_gone(self):
        """The ``.code-graph-disable`` marker mechanism was removed (ADR-0163)."""
        from yadgar.core.code_graph import config

        assert not hasattr(config, "OPT_OUT_MARKER")
        assert not hasattr(config, "ENABLE_ENV")


class TestCacheDir:
    def test_cache_dir_under_yadgar_cache(self):
        from yadgar._shared.paths.paths import CACHE_DIR
        from yadgar.core.code_graph import config

        cache = config.cache_dir()
        assert isinstance(cache, Path)
        # yadgar-owned, not polluting the user tree
        assert str(cache).startswith(str(CACHE_DIR))


class TestForwardConstants:
    def test_digest_char_budget_defined(self):
        from yadgar.core.code_graph import config

        assert isinstance(config.DIGEST_CHAR_BUDGET, int)
        assert 0 < config.DIGEST_CHAR_BUDGET <= 8000

    def test_refresh_cadence_key_defined(self):
        from yadgar.core.code_graph import config

        assert isinstance(config.CODE_GRAPH_REFRESH_STOP_INTERVAL, int)
        assert config.CODE_GRAPH_REFRESH_STOP_INTERVAL > 0

    def test_refresh_cadence_matches_shared_default(self):
        """The module constant mirrors the shared-config default (one 200)."""
        from yadgar._shared.config import get_settings
        from yadgar.core.code_graph import config

        assert (
            config.CODE_GRAPH_REFRESH_STOP_INTERVAL
            == get_settings().CODE_GRAPH_REFRESH_STOP_INTERVAL
        )


class TestSessionSuggestPredicate:
    """Car D SessionStart soft-suggest predicate — NOTHING forced.

    enabled + no code_graph block + not opted-out → suggestion present.
    block exists → absent (already injected). opted-out → absent.
    Enable state comes from the injected resolver (runtime config store).
    """

    def test_suggest_when_enabled_no_block(self, tmp_path):
        from yadgar.core.code_graph import config

        r = _resolver({None: True})
        line = config.session_suggest_line(tmp_path, blocks=[], resolver=r)
        assert line is not None
        assert "code_graph" in line
        assert tmp_path.name in line
        assert "yadgar code-graph refresh" in line

    def test_no_suggest_when_block_exists(self, tmp_path):
        from yadgar.core.code_graph import config

        blocks = [{"name": "code_graph", "scope": "project", "content": "digest"}]
        r = _resolver({None: True})
        assert config.session_suggest_line(tmp_path, blocks=blocks, resolver=r) is None

    def test_suggest_when_no_row_defaults_enabled(self, tmp_path):
        """ADR-0163 flip: nothing stored → on by default → suggestion present."""
        from yadgar.core.code_graph import config

        r = _resolver({})
        line = config.session_suggest_line(tmp_path, blocks=[], resolver=r)
        assert line is not None
        assert "code_graph" in line

    def test_no_suggest_when_explicitly_disabled(self, tmp_path):
        from yadgar.core.code_graph import config

        r = _resolver({None: False})  # explicit global false → disabled
        assert config.session_suggest_line(tmp_path, blocks=[], resolver=r) is None

    def test_no_suggest_when_per_dir_opted_out(self, tmp_path):
        from yadgar.core.code_graph import config

        # global on but this dir explicitly false → no suggestion here.
        r = _resolver({None: True, str(tmp_path): False})
        assert config.session_suggest_line(tmp_path, blocks=[], resolver=r) is None

    def test_other_blocks_do_not_suppress(self, tmp_path):
        """A non-code_graph block present must NOT suppress the suggestion."""
        from yadgar.core.code_graph import config

        blocks = [{"name": "current_task", "scope": "project", "content": "x"}]
        r = _resolver({None: True})
        assert config.session_suggest_line(tmp_path, blocks=blocks, resolver=r) is not None
