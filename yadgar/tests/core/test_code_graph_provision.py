"""code_graph provisioning — the shared home for the binary+flag pair.

Phase 1 of docs/plans/fix-shell-installer-code-graph-gap-2026-07-29.md: the
provisioning helpers moved out of ``cli/setup.py`` so the shell installer can
reach them through ``yadgar code-graph install``. The move is behavior-
preserving; the ONE deliberate behavior change is read-before-write on the
enable path (plan open decision 7).

Why read-before-write is not optional: ``_persist_code_graph_enable`` wrote
``true`` at GLOBAL scope unconditionally. That was inert only because the write
almost always failed (no token — fixed in the preceding commit; and
``yadgar setup`` runs before the daemon). Once the write actually lands, every
re-run of the deliberately idempotent installer would silently resurrect
code_graph for a user who ran ``config_set("code_graph.enabled", false,
scope="global")`` — turning an idempotent installer into a destructive one.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar.core.install.code_graph_provision import (
    _CODE_GRAPH_KEY,
    provision_code_graph,
)


@pytest.fixture(autouse=True)
def _never_touch_a_live_daemon(monkeypatch):
    """Hard guard: an unstubbed store call must ERROR, not reach a real daemon.

    Not hypothetical. The first run of this module had one test missing the
    ``set`` stub; it POSTed to the author's live daemon and flipped the real
    global ``code_graph.enabled`` row. ``runtime_config_client`` resolves its
    bearer token from ``secrets.env`` now, so on any developer machine these
    calls SUCCEED — the previous "no token, so it 401s" accident-absorber is
    gone. Every test here must stub both halves explicitly.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError(
            "unstubbed runtime_config_client call — stub set/get in the test; "
            "an unstubbed call mutates a developer's real runtime-config store"
        )

    monkeypatch.setattr("yadgar.core.runtime_config_client.set", _boom)
    monkeypatch.setattr("yadgar.core.runtime_config_client.get", _boom)


class _Harness:
    """Drive ``provision_code_graph`` with the binary installer + store stubbed.

    ``runtime_config_client.set`` AND ``.get`` are BOTH stubbed in every case:
    an unstubbed call reaches a developer's LIVE daemon (the write mutates its
    real runtime-config store; the read makes the outcome depend on whatever
    row that machine happens to carry). Same argument
    ``test_codebase_memory_mcp_install.py`` already makes for ``set``, applied
    to the read side added by read-before-write.
    """

    def __init__(self, *, install_ok=True, set_ok=True, stored=None):
        self.install_calls: list = []
        self.set_calls: list = []
        self.get_calls: list = []
        self._install_ok = install_ok
        self._set_ok = set_ok
        self._stored = stored  # None → "no row"

    def _fake_install(self, skip_if_exists=False):
        self.install_calls.append(skip_if_exists)
        if not self._install_ok:
            raise RuntimeError("no network: download failed")
        return "/home/x/.local/bin/codebase-memory-mcp"

    def _fake_set(self, key, value, *, scope="global", directory=None):
        self.set_calls.append((key, value, scope, directory))
        return self._set_ok

    def _fake_get(self, key, directory=None, default=None):
        self.get_calls.append((key, directory))
        return default if self._stored is None else self._stored

    def run(self, *, opt_out=False):
        with (
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
                side_effect=self._fake_install,
            ),
            patch("yadgar.core.install.codebase_memory_mcp.BINARY_NAME", "codebase-memory-mcp"),
            patch("yadgar.core.install.codebase_memory_mcp.VERSION", "v0.9.0"),
            patch("yadgar.core.runtime_config_client.set", side_effect=self._fake_set),
            patch("yadgar.core.runtime_config_client.get", side_effect=self._fake_get),
        ):
            return provision_code_graph(opt_out=opt_out)


# ── the three coherent outcomes ──────────────────────────────────────────────


class TestProvisionOutcomes:
    def test_default_installs_and_enables(self, capsys):
        h = _Harness()
        assert h.run() is True
        assert h.install_calls == [True], "must pass skip_if_exists=True (offline re-run)"
        assert h.set_calls == [(_CODE_GRAPH_KEY, True, "global", None)]
        assert "code_graph enabled globally" in capsys.readouterr().out

    def test_opt_out_skips_install_and_disables(self, capsys):
        h = _Harness()
        assert h.run(opt_out=True) is False
        assert h.install_calls == [], "--no-code-graph must not download anything"
        assert h.set_calls == [(_CODE_GRAPH_KEY, False, "global", None)]
        assert "--no-code-graph" in capsys.readouterr().out

    def test_failed_install_disables_and_never_raises(self, capsys):
        h = _Harness(install_ok=False)
        assert h.run() is False  # must NOT raise
        assert h.set_calls == [(_CODE_GRAPH_KEY, False, "global", None)]
        out = capsys.readouterr().out
        assert "binary install failed" in out
        assert "Setup CONTINUES" in out

    def test_failed_persist_prints_the_manual_remediation(self, capsys):
        """A failed DISABLE write is consequential — the flag defaults true."""
        h = _Harness(set_ok=False)
        h.run(opt_out=True)
        out = capsys.readouterr().out
        assert 'config_set("code_graph.enabled", false, scope="global")' in out


# ── read-before-write: the installer stays idempotent, not destructive ───────


class TestDoesNotClobberDeliberateOptOut:
    def test_existing_global_false_is_not_overwritten(self, capsys):
        h = _Harness(stored=False)
        h.run()
        assert h.get_calls == [(_CODE_GRAPH_KEY, None)], "must read the GLOBAL row"
        assert h.set_calls == [], (
            "a deliberate global opt-out must survive an installer re-run — "
            "without read-before-write every re-run silently re-enables it"
        )
        assert "opt-out" in capsys.readouterr().out.lower()

    def test_binary_is_still_installed_when_opted_out_globally(self):
        """Only the PERSIST is suppressed. Skipping the download too would make a
        later re-enable need the network again for no reason."""
        h = _Harness(stored=False)
        assert h.install_calls == []
        h.run()
        assert h.install_calls == [True]

    def test_existing_global_true_is_rewritten(self):
        """Idempotent: an explicit true row is not an opt-out."""
        h = _Harness(stored=True)
        h.run()
        assert h.set_calls == [(_CODE_GRAPH_KEY, True, "global", None)]

    def test_no_row_writes_true(self):
        h = _Harness(stored=None)
        h.run()
        assert h.set_calls == [(_CODE_GRAPH_KEY, True, "global", None)]

    def test_daemon_down_read_degrades_to_write(self):
        """``get`` is fail-open, so a daemon-down read yields the caller's
        sentinel — which must NOT be mistaken for a stored ``false``."""
        h = _Harness(stored=None)  # fail-open → returns our default sentinel
        h.run()
        assert h.set_calls, "an unreadable store must not suppress the enable"

    def test_opt_out_path_does_not_read(self):
        """``--no-code-graph`` is an explicit instruction — no read needed."""
        h = _Harness(stored=True)
        h.run(opt_out=True)
        assert h.get_calls == []


# ── the move is behavior-preserving for cli/setup.py ─────────────────────────


class TestSetupBackCompat:
    @pytest.mark.parametrize(
        "name",
        [
            "_CODE_GRAPH_KEY",
            "_resolve_code_graph_action",
            "_do_install_code_graph",
            "_persist_code_graph_enable",
            "_persist_code_graph_disable",
            "_maybe_install_code_graph",
        ],
    )
    def test_names_still_importable_from_setup(self, name):
        """Pure move: existing importers (and their tests) must not break."""
        from yadgar.core.cli import setup as _setup

        assert hasattr(_setup, name), f"cli/setup.py lost {name} in the move"

    def test_maybe_install_delegates_to_provision(self):
        """``cmd_setup``'s entry point must be a thin adapter, not a second copy.

        Patched on ``setup``'s own namespace: it imports the function by value,
        so patching the defining module would leave the bound name — and the
        real POST — in place.
        """
        import types

        from yadgar.core.cli import setup as _setup

        with patch.object(_setup, "provision_code_graph") as mock_provision:
            _setup._maybe_install_code_graph(types.SimpleNamespace(no_code_graph=True))
        mock_provision.assert_called_once_with(opt_out=True)
