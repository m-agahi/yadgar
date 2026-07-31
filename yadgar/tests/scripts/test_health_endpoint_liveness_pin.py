"""Pin: Python call sites that only need PROCESS-UP confirmation must probe
``/health/live`` (liveness), not ``/health`` (readiness, backend-dependent).

Car 0091: the v5.169 train split the health endpoints (ADR-0019) and pinned the
THREE non-Python surfaces (flake.nix, Dockerfile, docker-compose.yml — see
``test_core_health_probe_liveness_pin.py``), but that pin was config-file-only
and could not see Python call sites. Three Python call sites were still hitting
``/health`` where the caller only ever needed "is the process up and
responding", not "is the db/embed dependency usable":

  1. ``YadgarDaemon._health_ok`` (core-port gate for sd_notify READY=1)
  2. ``orchestrator._default_health_check`` (post-restart upgrade gate)
  3. ``update._probe_daemon_version`` (post-upgrade/rollback version probe)

A busy-but-fine backend can 503 the readiness endpoint; hitting it from these
three gates risks exactly the conflation ADR-0019 fixed at the container layer
— a transiently-degraded dependency incorrectly failing a liveness-shaped gate
(missed READY=1, a spurious upgrade rollback, "manual recovery needed" on a
healthy daemon).

Genuine readiness callers (``YadgarDaemon.status``, ``YadgarDaemon.
_embed_health_ok``, ``seed.py``, ``version.py``, vacuum's finalize wait) are
deliberately OUT of scope here — they read db/embed payload fields or gate a DB
write, so ``/health`` is the correct endpoint for them. See
``.health-endpoint-allowlist.json`` for the governed list.
"""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock, patch

# ── YadgarDaemon._health_ok — core liveness gate ─────────────────────────────


def test_health_ok_probes_liveness_endpoint():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    mock_resp = MagicMock()
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        assert d._health_ok(8765) is True
    requested_url = mock_open.call_args[0][0]
    assert requested_url == "http://127.0.0.1:8765/health/live", (
        f"_health_ok must probe /health/live (liveness), got {requested_url!r}"
    )


def test_health_ok_still_tolerates_503_degraded():
    """Liveness tolerance for a 503 predates ADR-0019 (ADR-0002) and must survive
    the endpoint switch — a degraded-but-alive pool must not fail sd_notify."""
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    err = urllib.error.HTTPError(
        "http://127.0.0.1:8765/health/live",
        503,
        "degraded",
        {},
        io.BytesIO(b'{"status":"degraded"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        assert d._health_ok(8765) is True
    err.close()


# ── YadgarDaemon._embed_health_ok — backend embed readiness (unchanged) ──────


def test_embed_health_ok_still_probes_readiness_endpoint():
    """The backend embed service exposes no /health/live liveness variant
    (ADR-0019 scope: core only) — this probe must stay on bare /health."""
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    mock_resp = MagicMock()
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        assert d._embed_health_ok(8001) is True
    requested_url = mock_open.call_args[0][0]
    assert requested_url == "http://127.0.0.1:8001/health"


# ── orchestrator._default_health_check — post-restart upgrade gate ──────────


def test_default_health_check_probes_liveness_endpoint():
    from yadgar.core.update.orchestrator import _default_health_check

    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value = MagicMock(status=200)
        assert _default_health_check() is True
    requested_url = mock_open.call_args[0][0]
    if hasattr(requested_url, "full_url"):
        requested_url = requested_url.full_url
    assert requested_url.endswith("/health/live"), (
        f"_default_health_check must probe /health/live, got {requested_url!r}"
    )


# ── update._probe_daemon_version — post-upgrade/rollback version probe ──────


def test_probe_daemon_version_probes_liveness_endpoint():
    import json as _json

    from yadgar.core.cli.update import _probe_daemon_version

    body = _json.dumps({"version": "5.169.2"}).encode()

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    with patch("urllib.request.urlopen", return_value=_FakeResp()) as mock_open:
        assert _probe_daemon_version() == "5.169.2"
    requested_url = mock_open.call_args[0][0]
    if hasattr(requested_url, "full_url"):
        requested_url = requested_url.full_url
    assert requested_url.endswith("/health/live"), (
        f"_probe_daemon_version must probe /health/live, got {requested_url!r}"
    )
