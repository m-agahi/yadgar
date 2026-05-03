"""Visualization server — serves the knowledge graph UI at http://localhost:42069."""

import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Timer

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


class _Handler(BaseHTTPRequestHandler):
    """Serve index.html for every path (single-page app)."""

    def do_GET(self) -> None:
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

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ANN001
        pass  # silence request logs


def run_viz_server(
    port: int = 42069,
    daemon_url: str = "http://127.0.0.1:8765",
    open_browser: bool = False,
    host: str = "0.0.0.0",
) -> None:
    """Start the visualization server. Blocks until interrupted."""
    if not INDEX_HTML.exists():
        raise FileNotFoundError(f"Visualization UI not found: {INDEX_HTML}")

    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    url = f"http://{display_host}:{port}"
    print(f"Yadgar Viz  →  {url}")
    print(f"Daemon API  →  {daemon_url}/api/graph")
    print("Press Ctrl+C to stop.")

    if open_browser:
        Timer(0.5, lambda: webbrowser.open(url)).start()

    HTTPServer((host, port), _Handler).serve_forever()
