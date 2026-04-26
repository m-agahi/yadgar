"""Visualization server — serves the knowledge graph UI at http://localhost:42069."""

import webbrowser
from pathlib import Path
from threading import Timer

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


async def serve_index(request: Request) -> FileResponse:
    if not INDEX_HTML.exists():
        return JSONResponse({"error": "index.html not found"}, status_code=404)
    return FileResponse(INDEX_HTML)


app = Starlette(
    routes=[
        Route("/", serve_index),
        Route("/{path:path}", serve_index),
    ]
)


def run_viz_server(
    port: int = 42069,
    daemon_url: str = "http://127.0.0.1:8765",
    open_browser: bool = False,
) -> None:
    """Start the visualization server. Blocks until interrupted."""
    if not INDEX_HTML.exists():
        raise FileNotFoundError(f"Visualization UI not found: {INDEX_HTML}")

    url = f"http://localhost:{port}"
    print(f"Yadgar Viz  →  {url}")
    print(f"Daemon API  →  {daemon_url}/api/graph")
    print("Press Ctrl+C to stop.")

    if open_browser:
        Timer(0.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
