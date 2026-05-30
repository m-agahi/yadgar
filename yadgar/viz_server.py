"""Visualization server — serves the knowledge graph UI at http://localhost:42069/.

For /api/* paths the server reverse-proxies to the yadgar backend (port 8765)
and injects the bearer token server-side so the browser never needs credentials.
The proxy is ON by default: auth is always required on /api/* (REQUIRE_AUTH=True
is the production default) so without proxy the UI is silently broken.
Set YADGAR_VIZ_PROXY=0 to disable proxy (e.g. in rare setups with auth disabled).
"""

from __future__ import annotations

import os
import socketserver
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Timer
from typing import Any

import httpx

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

_MIME_MAP: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _mime_type(path: Path) -> str:
    """Return MIME type string for *path* based on suffix."""
    return _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")


def _proxy_enabled() -> bool:
    """Return True unless YADGAR_VIZ_PROXY is explicitly set to '0'."""
    return os.environ.get("YADGAR_VIZ_PROXY", "1") != "0"


def _proxy_request(
    method: str,
    upstream_url: str,
    headers: dict[str, str],
    body: bytes,
    token: str,
    client_factory: Callable[[], Any] | None = None,
) -> httpx.Response:
    """Forward *method* + *upstream_url* to the backend, injecting bearer auth.

    Args:
        method: HTTP verb (GET, POST, …).
        upstream_url: Full upstream URL including path + query string.
        headers: Request headers from the browser (pass-through).
        body: Raw request body bytes.
        token: Bearer token to inject.  Empty string → no Authorization header.
        client_factory: Optional callable returning an httpx.Client-like object.
            Injected for testing; defaults to a real httpx.Client.

    Returns:
        httpx.Response with status, headers, and content from the backend.
    """
    proxy_headers = dict(headers)
    # Strip hop-by-hop headers that must not be forwarded.
    for hop in ("host", "connection", "transfer-encoding", "te", "trailer", "upgrade"):
        proxy_headers.pop(hop, None)

    if token:
        proxy_headers["Authorization"] = f"Bearer {token}"

    if client_factory is None:
        # Default: generous timeout for large /api/graph payloads (2k+ nodes
        # with semantic-edge cosine compute can exceed the httpx default 5s).
        client_factory = lambda: httpx.Client(  # type: ignore[assignment]  # noqa: E731
            timeout=httpx.Timeout(60.0, connect=5.0)
        )

    with client_factory() as client:
        resp = client.request(
            method=method,
            url=upstream_url,
            headers=proxy_headers,
            content=body,
        )
    return resp


class _Handler(BaseHTTPRequestHandler):
    """Serve index.html for SPA paths; reverse-proxy /api/* to the daemon."""

    # Injected by run_viz_server so each request can resolve daemon_url + token.
    _daemon_url: str = "http://127.0.0.1:8765"

    def _handle_proxy(self) -> None:
        """Proxy /api/* to the daemon backend with bearer token injected."""
        from yadgar.config import get_settings  # read at request time — supports late env setup

        token: str = get_settings().MCP_AUTH_TOKEN

        # Build upstream URL: daemon_url + raw path (includes query string).
        upstream = self._daemon_url.rstrip("/") + self.path

        # Read request body (needed for POST/PUT).
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        # Forward browser headers.
        fwd_headers: dict[str, str] = {k: v for k, v in self.headers.items()}

        try:
            resp = _proxy_request(
                method=self.command,
                upstream_url=upstream,
                headers=fwd_headers,
                body=body,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            self.send_error(502, f"Bad Gateway: {exc}")
            return

        ct = resp.headers.get("content-type", "application/octet-stream")
        data = resp.content

        self.send_response(resp.status_code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        # Forward cache-control and CORS headers if present.
        for hdr in ("cache-control", "access-control-allow-origin"):
            val = resp.headers.get(hdr)
            if val:
                self.send_header(hdr.title(), val)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        # Strip query string for path prefix check.
        raw_path = self.path.split("?", 1)[0]

        if _proxy_enabled() and raw_path.startswith("/api/"):
            self._handle_proxy()
            return

        # Serve static files (bookmarks.html, bookmarks.css, bookmarks.js, lib/*).
        # Any path that maps to an existing file under STATIC_DIR is served directly.
        # Everything else falls back to index.html (SPA behaviour).
        if raw_path not in ("", "/"):
            candidate = STATIC_DIR / raw_path.lstrip("/")
            # Resolve to prevent path traversal outside STATIC_DIR.
            try:
                resolved = candidate.resolve()
                static_resolved = STATIC_DIR.resolve()
            except Exception:
                self.send_error(400, "Bad Request")
                return
            if resolved.is_file() and str(resolved).startswith(str(static_resolved)):
                ct = _mime_type(resolved)
                try:
                    data = resolved.read_bytes()
                except OSError:
                    self.send_error(404, "File Not Found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        try:
            data = INDEX_HTML.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "Visualization UI not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        raw_path = self.path.split("?", 1)[0]
        if _proxy_enabled() and raw_path.startswith("/api/"):
            self._handle_proxy()
        else:
            self.send_error(405, "Method Not Allowed")

    def do_DELETE(self) -> None:
        """Proxy DELETE /api/* to the daemon (e.g. DELETE /api/bookmarks/{slug})."""
        raw_path = self.path.split("?", 1)[0]
        if _proxy_enabled() and raw_path.startswith("/api/"):
            self._handle_proxy()
        else:
            self.send_error(405, "Method Not Allowed")

    def do_PUT(self) -> None:
        """Proxy PUT /api/* to the daemon (e.g. PUT /api/bookmarks/{slug}/position)."""
        raw_path = self.path.split("?", 1)[0]
        if _proxy_enabled() and raw_path.startswith("/api/"):
            self._handle_proxy()
        else:
            self.send_error(405, "Method Not Allowed")

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ANN001
        pass  # silence request logs


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Thread-per-request server so SSE connections don't block other requests."""

    daemon_threads = True


def run_viz_server(
    port: int = 42069,
    daemon_url: str = "http://127.0.0.1:8765",
    open_browser: bool = False,
    host: str = "127.0.0.1",
) -> None:
    """Start the visualization server. Blocks until interrupted."""
    if not INDEX_HTML.exists():
        raise FileNotFoundError(f"Visualization UI not found: {INDEX_HTML}")

    # Propagate daemon_url to the handler class so request-time code can use it.
    _Handler._daemon_url = daemon_url

    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    url = f"http://{display_host}:{port}"
    print(f"Yadgar Viz  →  {url}")
    print(f"Daemon API  →  {daemon_url}/api/graph")
    if _proxy_enabled():
        print(f"Proxy mode  →  /api/* → {daemon_url} (YADGAR_VIZ_PROXY=0 to disable)")
    else:
        print("Proxy mode  →  disabled (YADGAR_VIZ_PROXY=0)")
    print("Press Ctrl+C to stop.")

    if open_browser:
        Timer(0.5, lambda: webbrowser.open(url)).start()

    _ThreadingHTTPServer((host, port), _Handler).serve_forever()
