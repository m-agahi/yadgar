"""Fail-open host-side client for the runtime_config store (ADR-0163, Car G3).

Importable by hook scripts and the host ``yadgar`` CLI — it runs ON THE HOST,
outside the daemon container, so it uses ONLY the stdlib (``urllib``): no httpx,
no ``yadgar.*`` runtime deps beyond observability.

``get(key, directory=None, default=None)`` hits the core daemon's
``GET /api/runtime-config/{key}?directory=...`` route and returns the resolved
value. It is FAIL-OPEN: the daemon being down, a timeout, a non-200, or malformed
JSON all yield the caller's ``default`` — it NEVER raises. The stop-hook opt-out
path depends on this (mirrors ``session-start-context.py``'s
``except Exception: pass`` daemon-down handling).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error as _err
import urllib.parse as _parse
import urllib.request as _req
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# Short request budget — the caller (e.g. a stop-hook) must not block on a slow
# or dead daemon. Matches the 2s budget the session-start-context hook uses.
_TIMEOUT_S = 2


@observe(tier="boundary")
def get(key: str, directory: str | None = None, default: Any = None) -> Any:
    """Resolve a runtime config value via the core daemon; fail open to ``default``.

    Args:
        key: Config key (e.g. ``code_graph.enabled``).
        directory: Absolute project path for a per-dir lookup; ``None`` = global.
        default: Returned on a null/missing value OR ANY error (daemon down,
            timeout, non-200, malformed JSON). Never raises.

    Returns:
        The resolved value, or ``default``.
    """
    port = os.environ.get("YADGAR_PORT", "8765")
    try:
        params = {}
        if directory:
            params["directory"] = directory
        query = f"?{_parse.urlencode(params)}" if params else ""
        # quote the key into the path (a key may contain '.' or '/'-like chars).
        url = f"http://127.0.0.1:{port}/api/runtime-config/{_parse.quote(key, safe='')}{query}"

        req_obj = _req.Request(url)
        token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        if token:
            req_obj.add_header("Authorization", f"Bearer {token}")

        with _req.urlopen(req_obj, timeout=_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode())
        value = body.get("value")
        return default if value is None else value
    except _err.HTTPError as http_exc:
        # An HTTPError IS a response object holding a file wrapper (a
        # tempfile._TemporaryFileWrapper via addbase on py3.14). If we let it be
        # GC'd unclosed, its deallocator fires a spurious ResourceWarning at an
        # arbitrary later moment — under pytest-xdist that unraisable warning is
        # mis-attributed to whatever test is running, failing unrelated cases.
        # Close it deterministically here. Still fail-open → default.
        _close_quietly(http_exc)
        logger.debug("runtime_config_client.get HTTP error for key=%s; using default", key)
        return default
    except Exception:  # noqa: BLE001 — fail-open: daemon down / any error → default
        logger.debug("runtime_config_client.get failed for key=%s; using default", key)
        return default


@observe(tier="stage")
def _close_quietly(http_exc: _err.HTTPError) -> None:
    """Close an HTTPError's file wrapper; never re-raise (py3.14 ResourceWarning guard)."""
    try:
        http_exc.close()
    except Exception:  # noqa: BLE001 — defensive; close must never re-raise
        pass


@observe(tier="stage")
def _base_url(key: str, params: dict) -> str:
    """Build the ``/api/runtime-config/{key}`` URL with an optional query string."""
    port = os.environ.get("YADGAR_PORT", "8765")
    query = f"?{_parse.urlencode(params)}" if params else ""
    # quote the key into the path (a key may contain '.' or '/'-like chars).
    return f"http://127.0.0.1:{port}/api/runtime-config/{_parse.quote(key, safe='')}{query}"


@observe(tier="stage")
def _apply_auth(req_obj: _req.Request) -> None:
    """Attach the Bearer header when ``YADGAR_MCP_AUTH_TOKEN`` is set."""
    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    if token:
        req_obj.add_header("Authorization", f"Bearer {token}")


@observe(tier="boundary")
def set(key: str, value: Any, *, scope: str = "global", directory: str | None = None) -> bool:
    """Persist a runtime config value via the core daemon; return success.

    UNLIKE :func:`get`, this is NOT silently fail-open: a daemon-down / timeout /
    non-2xx / any error yields ``False`` (NEVER raises) so the caller can report
    "couldn't enable" rather than assuming the write landed. A 2xx yields ``True``.

    Args:
        key: Config key (e.g. ``code_graph.enabled``).
        value: JSON-serializable value (bool / int / str / list / dict).
        scope: ``"global"`` (the global row) or ``"project"`` (per-dir override —
            ``directory`` required, validated server-side).
        directory: Absolute project path when ``scope="project"``.

    Returns:
        ``True`` on a 2xx write, ``False`` on any failure.
    """
    url = _base_url(key, {})
    body = json.dumps({"value": value, "scope": scope, "directory": directory}).encode()
    try:
        req_obj = _req.Request(url, data=body, method="POST")
        req_obj.add_header("Content-Type", "application/json")
        _apply_auth(req_obj)
        with _req.urlopen(req_obj, timeout=_TIMEOUT_S) as resp:
            status = getattr(resp, "status", 200)
        return 200 <= int(status) < 300
    except _err.HTTPError as http_exc:
        # Non-2xx (e.g. 400 validation, 401 auth) — close the wrapper (py3.14
        # ResourceWarning guard) and report failure so the caller knows the write
        # did not land.
        _close_quietly(http_exc)
        logger.debug("runtime_config_client.set HTTP error for key=%s → False", key)
        return False
    except Exception:  # noqa: BLE001 — NOT fail-open: any error → False (caller reports)
        logger.debug("runtime_config_client.set failed for key=%s → False", key)
        return False


@observe(tier="boundary")
def delete(key: str, *, scope: str = "global", directory: str | None = None) -> bool:
    """Delete a runtime config row via the core daemon; return success.

    Mirrors :func:`set` — NOT fail-open: any failure yields ``False`` (never raises).

    Args:
        key: Config key to remove.
        scope: ``"global"`` or ``"project"`` (``directory`` required for project).
        directory: Absolute project path when ``scope="project"``.

    Returns:
        ``True`` on a 2xx delete, ``False`` on any failure.
    """
    params: dict = {"scope": scope}
    if directory:
        params["directory"] = directory
    url = _base_url(key, params)
    try:
        req_obj = _req.Request(url, method="DELETE")
        _apply_auth(req_obj)
        with _req.urlopen(req_obj, timeout=_TIMEOUT_S) as resp:
            status = getattr(resp, "status", 200)
        return 200 <= int(status) < 300
    except _err.HTTPError as http_exc:
        _close_quietly(http_exc)
        logger.debug("runtime_config_client.delete HTTP error for key=%s → False", key)
        return False
    except Exception:  # noqa: BLE001 — NOT fail-open: any error → False (caller reports)
        logger.debug("runtime_config_client.delete failed for key=%s → False", key)
        return False
