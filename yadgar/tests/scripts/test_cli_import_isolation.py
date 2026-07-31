"""Regression guard — the CLI hook path must NOT import the MCP server package.

Car 0031. ``yadgar restore`` / ``yadgar drain`` are the two live Claude Code hook
paths. Both are thin HTTP forwarders (``yadgar/core/cli/_shared.py``), yet they
used to reach the forwarder via ``yadgar.core.server.tools._forward``. Importing
anything under ``yadgar.core.server`` runs ``yadgar/core/server/__init__.py``,
which eagerly imports ``_app``, which calls ``setup_tracing("yadgar-core")`` at
module scope — so a 40-line HTTP POST dragged in the whole MCP server *and* a
live OTLP exporter. Measured cost on the host: ``yadgar restore`` 8.2s vs 1.2s
with export disabled (the configured collector hostname,
``host.containers.internal``, does not resolve host-side, so every export burns
the full 10s exporter deadline and the SDK's own atexit shutdown joins on it).

These tests are what stops the edge being re-introduced. They run the probe in a
SUBPROCESS on purpose: ``sys.modules`` is process-global, so an in-process
assertion would be polluted by any earlier test in the same xdist worker that
imported the server, and would false-fail under ``-n auto`` while passing at
``-n0``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# The forward helpers raise RuntimeError when YADGAR_EMBED_URL is unset (they are
# forward-only). That fail-fast path still executes the module import we care
# about, and makes zero network calls — exactly what we want to probe.
_PROBE = """
import os, sys

os.environ.pop("YADGAR_EMBED_URL", None)

from yadgar.core.cli._shared import {func}

try:
    {func}({args})
except RuntimeError:
    pass  # expected: forward-only, YADGAR_EMBED_URL unset

leaked = sorted(
    m for m in sys.modules
    if m == "yadgar.core.server" or m.startswith("yadgar.core.server.")
)
assert not leaked, "CLI hook path imported the MCP server: " + repr(leaked)
"""


@pytest.mark.parametrize(
    ("func", "args"),
    [
        ("forward_restore", '"/tmp"'),
        ("forward_pre_compact_drain", '"/tmp"'),
    ],
)
def test_cli_forward_does_not_import_mcp_server(func: str, args: str) -> None:
    """Calling a CLI forward helper must not pull in ``yadgar.core.server``."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(func=func, args=args)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_cli_entry_modules_do_not_import_mcp_server() -> None:
    """Importing the restore/drain subcommand modules must not pull in the server."""
    probe = (
        "import sys\n"
        "import yadgar.core.cli.restore, yadgar.core.cli.drain, yadgar.core.cli._shared\n"
        "leaked = sorted(m for m in sys.modules "
        "if m == 'yadgar.core.server' or m.startswith('yadgar.core.server.'))\n"
        "assert not leaked, leaked\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr


def test_tracer_provider_does_not_register_sdk_atexit_shutdown() -> None:
    """Safety net: ``setup_tracing`` must build the provider with ``shutdown_on_exit=False``.

    The SDK's default ``atexit.register(provider.shutdown)`` joins the
    BatchSpanProcessor for up to 30s against an unreachable collector, and it
    fires even after our own bounded ``shutdown_tracing(timeout_sec=3.0)``
    abandoned its worker thread (the unregister only happens *after* the inner
    shutdown returns, which is precisely what hangs). Span RECORDING is
    untouched — ``LogSpanProcessor`` is still registered unconditionally, and
    ``OTEL_SDK_DISABLED`` is never set (ADR-0037).
    """
    probe = (
        "from opentelemetry import trace\n"
        "from yadgar._shared.observability.tracing import setup_tracing\n"
        "setup_tracing('yadgar-test-atexit')\n"
        "p = trace.get_tracer_provider()\n"
        "h = getattr(p, '_atexit_handler', 'MISSING')\n"
        "assert h is None, 'SDK atexit shutdown still registered: %r' % (h,)\n"
        "assert p._active_span_processor is not None\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
