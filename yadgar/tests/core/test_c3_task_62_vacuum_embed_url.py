"""Targeted tests for task 62 / C3 — vacuum service sets YADGAR_EMBED_URL.

The bug: ``yadgar-vacuum.service`` did not export ``YADGAR_EMBED_URL``. Car
0113's queue-drain nudge (``yadgar/core/vacuum/__init__.py:1796``) calls
``_forward_admin("drain_now", {})``, which refuses to forward without that
env var (``yadgar/core/forward.py:115-120``). The drain is wrapped in a
WARN-and-proceeds at ``vacuum/__init__.py:1812`` — so every systemd-fired
vacuum silently no-op'd the safety mechanism. The literal ``8001`` mirrors
``build_nightly_service`` (maintenance_units.py:413) and the backend's
loopback publish (units.py:306).
"""

from __future__ import annotations

from yadgar.core.daemon.maintenance_units import build_vacuum_service
from yadgar.core.daemon.unit_model import render_unit

EMBED_URL = "Environment=YADGAR_EMBED_URL=http://127.0.0.1:8001"


def _rendered_vacuum() -> str:
    """Render the vacuum unit with the minimal kwargs the renderer needs."""
    return render_unit(
        build_vacuum_service(
            data_dir="/data-host",
            secrets_env_file="/etc/yadgar/secrets.env",
            surreal_port=8000,
            vacuum_exec="/usr/local/bin/yadgar",
        )
    )


def test_vacuum_service_emits_embed_url():
    """The fix: yadgar-vacuum.service MUST export YADGAR_EMBED_URL so the
    queue-drain nudge (Car 0113) can reach the backend admin endpoint."""
    text = _rendered_vacuum()
    assert EMBED_URL in text, (
        f"yadgar-vacuum.service must export YADGAR_EMBED_URL=http://127.0.0.1:8001 "
        f"so the queue-drain nudge (_forward_admin('drain_now')) can reach the "
        f"backend (task 62, C3).\nGot:\n{text}"
    )


def test_vacuum_embed_url_positioned_with_other_environment_vars():
    """The new line must sit alongside YADGAR_DB_URL, not at the tail of the
    unit. This guards against a future refactor that places Environment= at
    the wrong section — systemd reads these top-down and a [Service]-tail
    position is unusual for the existing pair."""
    text = _rendered_vacuum()
    db_pos = text.index("Environment=YADGAR_DB_URL=")
    embed_pos = text.index(EMBED_URL)
    data_pos = text.index("Environment=YADGAR_DATA_DIR=")
    assert db_pos < embed_pos < data_pos, (
        "YADGAR_EMBED_URL must sit between YADGAR_DB_URL and YADGAR_DATA_DIR "
        "to mirror build_nightly_service's shape (maintenance_units.py:411-413)."
    )


def test_vacuum_embed_url_value_is_loopback_literal():
    """The literal 127.0.0.1:8001 must match nightly-cycle's literal AND the
    backend's loopback publish. A typo here silently breaks the drain on
    every host; this test pins the exact value the cross-generator suite
    also pins."""
    text = _rendered_vacuum()
    assert "YADGAR_EMBED_URL=http://127.0.0.1:8001" in text, (
        "loopback literal must match nightly-cycle + backend publish (task 62, C3)."
    )
