"""Tests for vacuum URL env-read correctness (v5.10.5 Bug 1 regression guard).

Verifies that EVERY code path that resolves a backend_url for the vacuum
reads YADGAR_DB_URL from the environment rather than hard-coding :8080.

TDD: written before fix, confirmed red before green.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_NC_MODULE = "yadgar.core.scripts.nightly_cycle"
_VAC_MODULE = "yadgar.core.vacuum"


class TestVacuumImplEnvRead:
    """cmd_vacuum_impl must read YADGAR_DB_URL when args.backend_url is absent."""

    def test_cmd_vacuum_impl_uses_env_when_no_args_backend_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """cmd_vacuum_impl falls back to YADGAR_DB_URL env, not :8080, when arg missing."""
        monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:8000")

        captured_url: list[str] = []

        import yadgar.core.vacuum as vac_mod

        # Stub out the real HTTP call — we only care what URL is used
        def _fake_get(url, **_kw):
            captured_url.append(url)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        # Build minimal args WITHOUT backend_url attribute
        args = SimpleNamespace(service_mode="manual", db_path=None, yes=True)

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()

        with (
            patch("yadgar.core.vacuum.httpx.get", side_effect=_fake_get),
            patch("yadgar._shared.config.Settings") as mock_settings_cls,
        ):
            mock_settings = MagicMock()
            mock_settings.DB_PATH = str(db_dir)
            mock_settings.BACKEND_HTTP_TIMEOUT_SEC = "30"
            mock_settings_cls.return_value = mock_settings

            # Only testing URL resolution — early exit via a fake health check is fine
            # The 200 response lets it proceed to the next step, but we only need capture
            try:
                vac_mod.cmd_vacuum_impl(args)
            # The call runs against a deliberately partial mock set, so it fails
            # somewhere past the URL resolution this test asserts on. Which
            # unmocked step it reaches first is not this test's business.
            except Exception:
                pass  # expected — vacuum does more work we haven't mocked fully

        assert captured_url, "httpx.get was not called — test cannot validate URL"
        for url in captured_url:
            assert ":8080" not in url, (
                f"cmd_vacuum_impl used hard-coded :8080 in URL: {url!r}. "
                "Must read YADGAR_DB_URL env instead."
            )
            assert "8000" in url, f"Expected env URL (port 8000), got: {url!r}"

    def test_nightly_cycle_main_passes_env_url_to_vacuum(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """nightly_cycle.main resolves backend_url from YADGAR_DB_URL, not hard-coded :8080."""
        import importlib

        import yadgar.core.scripts.nightly_cycle as nc_mod

        importlib.reload(nc_mod)

        # Set env BEFORE args are built (simulates systemd unit invocation)
        monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:8000")

        captured_vac_url: list[str] = []

        def _fake_vac(args):
            captured_vac_url.append(getattr(args, "backend_url", "<missing>"))
            return 0

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()

        # Simulate systemd invocation: no backend_url on args namespace
        nc_args = SimpleNamespace(
            db_path=str(db_dir),
            service_mode=None,
            retention=3,
            # backend_url intentionally absent
        )

        with patch.multiple(
            _NC_MODULE,
            _run_systemctl=MagicMock(),
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=_fake_vac,
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=mock_sched.run_nightly_consolidation,
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            nc_mod.main(nc_args)

        assert captured_vac_url, "cmd_vacuum_impl was never called"
        url = captured_vac_url[0]
        assert ":8080" not in url, (
            f"nightly_cycle.main passed hard-coded :8080 URL to vacuum: {url!r}. "
            "Must read YADGAR_DB_URL env."
        )
        assert "8000" in url, f"Expected env URL (port 8000) forwarded to vacuum, got: {url!r}"


class TestNoHardCodedPort8080InProductionSources:
    """Structural check: production source files must not contain :8080 at all.

    v5.10.5 fixed the env-read bug (a bare ``getattr(..., "http://127.0.0.1:8080")``
    default that never consulted ``YADGAR_DB_URL``), but left the literal
    fallback-of-last-resort — used only when the env var is ALSO unset — at the
    wrong port. The real backend binds :8000 (``entrypoint-backend.sh --bind
    0.0.0.0:8000``, ``config_registry.py``'s ``YADGAR_DB_URL`` default, every
    systemd/launchd unit's ``Environment=YADGAR_DB_URL=...:8000``). task:0042
    corrected the literal to :8000, so no production vacuum/nightly-cycle
    source file should contain '8080' in any form any more.
    """

    def test_nightly_cycle_no_bare_8080_literal_as_default(self) -> None:
        """nightly_cycle.py must not use getattr(..., ':8080') without env fallback."""
        import inspect

        import yadgar.core.scripts.nightly_cycle as nc

        src = inspect.getsource(nc)
        # The bad pattern: getattr(args, "backend_url", "http://127.0.0.1:8080")
        # without reading os.environ right after — triggers when env var is the fix
        bad_pattern = 'getattr(args, "backend_url", "http://127.0.0.1:8080")'
        assert bad_pattern not in src, (
            "nightly_cycle.py contains bare getattr-fallback to :8080 literal. "
            "Must read YADGAR_DB_URL from environment instead."
        )

    def test_cmd_vacuum_impl_no_bare_8080_literal_as_default(self) -> None:
        """cmd_vacuum_impl must not use getattr(..., ':8080') without env fallback."""
        import inspect

        import yadgar.core.vacuum as vac

        src = inspect.getsource(vac)
        bad_pattern = 'getattr(args, "backend_url", "http://127.0.0.1:8080")'
        assert bad_pattern not in src, (
            "yadgar/vacuum/__init__.py contains bare getattr-fallback to :8080 literal. "
            "Must read YADGAR_DB_URL from environment instead."
        )

    def test_no_8080_literal_survives_anywhere_in_vacuum_sources(self) -> None:
        """task:0042: the :8080 fallback-of-last-resort was itself the wrong port.

        Neither ``nightly_cycle.py`` nor ``vacuum/__init__.py`` nor
        ``cli/vacuum.py`` have a directly-testable helper for their fallback
        literal the way ``cli.vacuum._default_backend_url`` does (see
        ``test_vacuum_viz_env_defaults.py``), so this is the structural guard
        that closes the loop for all three: no '8080' substring — in code,
        comments, or docstrings — should remain in any of them.
        """
        import inspect

        import yadgar.core.cli.vacuum as cli_vac
        import yadgar.core.scripts.nightly_cycle as nc
        import yadgar.core.vacuum as vac

        for module in (nc, vac, cli_vac):
            src = inspect.getsource(module)
            assert "8080" not in src, (
                f"{module.__name__} still contains an '8080' literal — the real "
                "backend port is 8000 (entrypoint-backend.sh --bind 0.0.0.0:8000)."
            )
