"""Car B — core `_runtime_config.py` config reads forward over HTTP.

§15.4 / ADR-0078 / ADR-0200: config reads stay core but go through HTTP forward
to the backend ``get_config_row`` / ``list_config_rows`` op bodies, NOT the
in-process ``_get_storage()`` (which is the existing violation Car B closes).

Fail-safe: backend-down + cold core PTC falls back to ``Settings`` code default
(never raises out of a config read). Backend-up + warm core PTC hits without
an HTTP call (cache fast path).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_cache():
    """Drop the runtime_config core PTC between tests (no cross-bleed)."""
    from yadgar.core.cache.cache import _REGISTRY
    from yadgar.core.server.tools import _runtime_config as rc

    _REGISTRY.pop("runtime_config", None)
    rc._cache = None
    yield
    _REGISTRY.pop("runtime_config", None)
    rc._cache = None


class TestForwardReads:
    def test_config_get_forwards_to_backend_get_config_row(self) -> None:
        from yadgar.core.server.tools import _runtime_config as rc

        captured: dict = {}

        def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
            captured["op"] = op
            captured["payload"] = payload
            return {"row": {"key": "k", "value": 42, "directory": None}}

        with patch.object(rc, "_forward_admin", side_effect=fake_forward):
            value = rc.config_get("k", directory=None, default=None)

        assert value == 42
        assert captured["op"] == "get_config_row"
        assert captured["payload"] == {"key": "k", "directory": None}

    def test_config_get_warm_cache_skips_forward(self) -> None:
        """A warm core PTC hits WITHOUT an HTTP call (cache fast path)."""
        from yadgar.core.server.tools import _runtime_config as rc

        # Pre-warm via config_get.
        with patch.object(rc, "_forward_admin") as fake_forward:
            fake_forward.return_value = {"row": {"key": "k", "value": "first"}}
            rc.config_get("k", directory=None, default=None)

        # Second call: no HTTP.
        with patch.object(rc, "_forward_admin") as fake_forward:
            value = rc.config_get("k", directory=None, default=None)

        assert value == "first"
        fake_forward.assert_not_called()

    def test_config_get_falls_back_to_default_when_backend_down(self) -> None:
        """Backend-down + cold PTC: must return ``default``, never raise."""
        from yadgar.core.server.tools import _runtime_config as rc

        with patch.object(rc, "_forward_admin", side_effect=RuntimeError("backend down")):
            value = rc.config_get("missing_key", directory=None, default="DEFAULT")

        assert value == "DEFAULT"

    def test_config_get_returns_none_when_backend_returns_none_row(self) -> None:
        """Backend ``get_config_row`` returns ``{row: None}`` for an absent key;
        the resolver treats that as 'no row → default'."""
        from yadgar.core.server.tools import _runtime_config as rc

        with patch.object(rc, "_forward_admin") as fake_forward:
            fake_forward.return_value = {"row": None}
            value = rc.config_get("absent", directory=None, default="FALLBACK")

        assert value == "FALLBACK"

    def test_config_get_per_dir_resolution(self) -> None:
        """Per-dir → global → default resolution through forward."""
        from yadgar.core.server.tools import _runtime_config as rc

        responses: list[dict] = [
            {"row": None},  # per-dir miss
            {"row": {"key": "k", "value": 7, "directory": None}},  # global hit
        ]
        responses_iter = iter(responses)

        def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
            return next(responses_iter)

        with patch.object(rc, "_forward_admin", side_effect=fake_forward):
            value = rc.config_get("k", directory="/proj", default=None)

        assert value == 7


class TestInProcessStorageNoLongerUsed:
    """The forwarding path must not touch ``_get_storage()`` for the read —
    ADR-0078 forbids core reading the DB directly."""

    def test_module_no_longer_imports_get_storage(self) -> None:
        """After Car B the resolver must not even import ``_get_storage``
        from ``yadgar._shared.runtime.lifecycle`` — the only read path is
        ``_forward_admin``. (Sanity-check via module source: a forbidden
        import would trip the chokepoint lint.)"""
        from yadgar.core.server.tools import _runtime_config as rc  # noqa: PLC0415

        assert not hasattr(rc, "_get_storage"), (
            "Car B closes the _get_storage() read violation; the symbol must "
            "not exist on the resolver module anymore."
        )

    def test_config_get_does_not_call_get_storage(self) -> None:
        """Defence-in-depth: even if a future change re-introduces a
        ``_get_storage`` symbol, config_get must not call it."""
        from yadgar.core.server.tools import _runtime_config as rc  # noqa: PLC0415

        sentinel = MagicMock()

        # Inject a sentinel that would scream if invoked.
        rc._get_storage = sentinel  # type: ignore[attr-defined]

        with patch.object(rc, "_forward_admin") as fake_forward:
            fake_forward.return_value = {"row": {"key": "k", "value": 1}}
            rc.config_get("k", directory=None, default=None)
            rc.config_get("k", directory=None, default=None)  # cache hit

        assert not sentinel.called, (
            "config_get must not call _get_storage(); ADR-0078 / ADR-0200 "
            "forbid core reading the DB directly."
        )


class TestWarmupForwardOverHttp:
    """`warmup_runtime_config_cache` must seed via ``_forward_admin``, NOT
    ``_get_storage()`` (would defeat ADR-0078 / ADR-0200)."""

    def test_warmup_uses_forward(self) -> None:
        from yadgar.core.server.tools import _runtime_config as rc  # noqa: PLC0415

        with patch.object(rc, "_forward_admin") as fake_forward:
            fake_forward.return_value = {"rows": [{"key": "k", "value": 1, "directory": None}]}
            rc.warmup_runtime_config_cache(object())  # truthy storage arg ignored

        fake_forward.assert_called_once_with("list_config_rows", {})
