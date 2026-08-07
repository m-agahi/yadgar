"""Engine-#2 car F follow-up: the ``sql_engine_status`` backend admin op.

WHY THIS OP EXISTS
------------------
The nightly cycle runs HOST-SIDE and cannot see engine #2 for itself: car C's
``client.cnf`` carries a CONTAINER-ABSOLUTE socket path, ``MariaStorageEngine``
construction is connectionless (so a host-built handle fails silently), and
mariadbd runs ``--skip-networking``. The backend process is the only one that
knows whether engine #2 was composed, so step 5b has to ASK rather than infer.

WHY IT IS A PURE SLOT READ AND NOT A PROBE
------------------------------------------
The op answers exactly one question — did ``_init_sql_storage`` populate the
slot — and never opens a connection. That is the load-bearing choice, not an
optimisation. A ``SELECT 1`` probe would report ABSENT for an engine that is
merely momentarily unreachable, and the caller turns ABSENT into "skip the
backup". Conflating those two states is precisely how a backup silently stops
happening while every log line still reads green.
"""

from __future__ import annotations

from unittest.mock import patch

from yadgar.backend.admin_exec import admin_ops, engine_status


def test_sql_engine_status_op_registered():
    """The op is on the /admin dispatch table — the route validates against it.

    Load-bearing beyond the usual registry hygiene: the host-side caller reads a
    400 ``unknown admin op`` as PROOF that the backend predates engine #2 and
    therefore skips the backup. A rename or a dropped registration would make a
    HEALTHY engine look absent and silently disable the nightly backup — the
    same silent-disable this whole arm exists to prevent, arriving by typo
    rather than by logic.
    """
    assert "sql_engine_status" in admin_ops()


def test_reports_present_when_the_slot_is_populated():
    with patch.object(engine_status, "_get_sql_engine", return_value=object()):
        result = engine_status.sql_engine_status({})

    assert result["present"] is True
    assert result["engine"] == "mariadb"


def test_reports_absent_when_the_slot_is_empty():
    """``_get_sql_storage`` returns None on core, and on a backend where MariaDB
    did not come up (``entrypoint-backend.sh`` treats that as a WARNING)."""
    with patch.object(engine_status, "_get_sql_engine", return_value=None):
        result = engine_status.sql_engine_status({})

    assert result["present"] is False


def test_never_opens_a_connection():
    """The op must not touch the engine handle beyond an is-None test.

    A handle that raises on ANY attribute access still yields ``present: True``.
    This is the anti-probe assertion: an implementation that grew a ``SELECT 1``
    (or any other liveness check) fails here, which is what keeps "present but
    momentarily unreachable" from being reported as absent.
    """

    class _Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"the status op touched the engine handle: .{name}")

    with patch.object(engine_status, "_get_sql_engine", return_value=_Exploding()):
        result = engine_status.sql_engine_status({})

    assert result["present"] is True


def test_ignores_its_payload():
    """No payload key can change the answer — the slot is the only input."""
    with patch.object(engine_status, "_get_sql_engine", return_value=None):
        assert engine_status.sql_engine_status({"present": True, "force": True})["present"] is False
