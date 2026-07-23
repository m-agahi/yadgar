"""Car G3 — core HTTP route GET /api/runtime-config/{key} (ADR-0163).

The host-side fail-open client hits this route. It resolves the value core-side
via the G2 resolver (per-dir → global → default) and returns JSON:
    {"key": ..., "directory": ..., "value": <resolved>}

The resolver is already fail-safe (storage None / error → default), so a
daemon-with-no-DB returns {"value": null} rather than a 5xx — that IS the
fail-open end-to-end contract.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch


def _fire(key: str, params: dict, resolver):
    """Run api_runtime_config with the resolver patched; return parsed JSON body."""
    import yadgar.core.server.http as _http

    mock_request = MagicMock()
    mock_request.path_params = MagicMock()
    mock_request.path_params.get = MagicMock(side_effect=lambda k, d="": {"key": key}.get(k, d))
    mock_request.query_params = MagicMock()
    mock_request.query_params.get = MagicMock(side_effect=lambda k, d=None: params.get(k, d))

    async def _run():
        with patch("yadgar.core.server.tools._runtime_config.config_get", side_effect=resolver):
            return await _http.api_runtime_config(mock_request)

    resp = asyncio.run(_run())
    return json.loads(resp.body.decode())


class TestRuntimeConfigRoute:
    def test_returns_resolved_value(self):
        body = _fire(
            "code_graph.enabled",
            {"directory": "/proj"},
            resolver=lambda key, directory=None, default=None: True,
        )
        assert body == {"key": "code_graph.enabled", "directory": "/proj", "value": True}

    def test_directory_omitted_is_none(self):
        seen: dict = {}

        def _resolver(key, directory=None, default=None):
            seen["directory"] = directory
            return "x"

        body = _fire("k", {}, resolver=_resolver)
        assert seen["directory"] is None
        assert body["directory"] is None
        assert body["value"] == "x"

    def test_missing_value_is_null(self):
        body = _fire("k", {}, resolver=lambda key, directory=None, default=None: default)
        assert body["value"] is None


# ---------------------------------------------------------------------------
# Car G5 — WRITE route POST /api/runtime-config/{key}
# ---------------------------------------------------------------------------


def _fire_post(key: str, json_body: dict, apply_fn):
    """Run api_runtime_config_set with the apply helper patched; return (status, body)."""
    import yadgar.core.server.http as _http

    mock_request = MagicMock()
    mock_request.path_params = MagicMock()
    mock_request.path_params.get = MagicMock(side_effect=lambda k, d="": {"key": key}.get(k, d))

    async def _json():
        return json_body

    mock_request.json = _json

    async def _run():
        with patch(
            "yadgar.core.server.tools.runtime_config._apply_config_set", side_effect=apply_fn
        ):
            return await _http.api_runtime_config_set(mock_request)

    resp = asyncio.run(_run())
    return resp.status_code, json.loads(resp.body.decode())


class TestRuntimeConfigSetRoute:
    def test_success_returns_200_and_row(self):
        def _apply(key, value, scope, directory):
            return {"key": key, "directory": directory, "value": value}

        status, body = _fire_post(
            "code_graph.enabled",
            {"value": True, "scope": "global"},
            apply_fn=_apply,
        )
        assert status == 200
        assert body["value"] is True

    def test_passes_body_through_to_apply(self):
        seen: dict = {}

        def _apply(key, value, scope, directory):
            seen.update(key=key, value=value, scope=scope, directory=directory)
            return {"key": key, "value": value, "directory": directory}

        _fire_post(
            "k",
            {"value": 7, "scope": "project", "directory": "/proj"},
            apply_fn=_apply,
        )
        assert seen == {"key": "k", "value": 7, "scope": "project", "directory": "/proj"}

    def test_validation_error_returns_400(self):
        def _apply(key, value, scope, directory):
            return {"ok": False, "error": "invalid_scope"}

        status, body = _fire_post("k", {"value": 1, "scope": "bogus"}, apply_fn=_apply)
        assert status == 400
        assert body["ok"] is False

    def test_invalid_json_returns_400(self):
        import yadgar.core.server.http as _http

        mock_request = MagicMock()
        mock_request.path_params = MagicMock()
        mock_request.path_params.get = MagicMock(side_effect=lambda k, d="": {"key": "k"}.get(k, d))

        async def _json():
            raise ValueError("bad json")

        mock_request.json = _json

        resp = asyncio.run(_http.api_runtime_config_set(mock_request))
        assert resp.status_code == 400
