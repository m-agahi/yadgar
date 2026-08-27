"""yadgar.core._surreal_runner — surreal test/ops process-runner package.

T2 Car D (D3, layer-boundary train): the flat ``_surreal_runner.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.core._surreal_runner`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.core._surreal_runner._surreal_runner``
directly.

  _surreal_runner.py — spawn/reap local surreal processes (host-ops next to vacuum)
"""

from typing import Final

_EXPORTS: Final = {
    "Any": "yadgar.core._surreal_runner._surreal_runner",
    "_DEFAULT_PORT_BASE": "yadgar.core._surreal_runner._surreal_runner",
    "_RETRY_BACKOFF_MS": "yadgar.core._surreal_runner._surreal_runner",
    "_SPAWNED_SURREAL_DATA_DIRS": "yadgar.core._surreal_runner._surreal_runner",
    "_SPAWNED_SURREAL_PIDS": "yadgar.core._surreal_runner._surreal_runner",
    "_SWEEP_MIN_AGE_S": "yadgar.core._surreal_runner._surreal_runner",
    "_TEST_DATA_DIR_PREFIXES": "yadgar.core._surreal_runner._surreal_runner",
    "_is_test_data_dir": "yadgar.core._surreal_runner._surreal_runner",
    "_kill_all_spawned_surreal_atexit": "yadgar.core._surreal_runner._surreal_runner",
    "_live_surreal_cmdlines": "yadgar.core._surreal_runner._surreal_runner",
    "_port_in_use": "yadgar.core._surreal_runner._surreal_runner",
    "_resolve_db_creds": "yadgar.core._surreal_runner._surreal_runner",
    "_session_tmp_base": "yadgar.core._surreal_runner._surreal_runner",
    "_sweep_one": "yadgar.core._surreal_runner._surreal_runner",
    "_worker_index": "yadgar.core._surreal_runner._surreal_runner",
    "allocate_port": "yadgar.core._surreal_runner._surreal_runner",
    "allocate_port_with_retry": "yadgar.core._surreal_runner._surreal_runner",
    "annotations": "yadgar.core._surreal_runner._surreal_runner",
    "atexit": "yadgar.core._surreal_runner._surreal_runner",
    "kill_all_spawned_surreal": "yadgar.core._surreal_runner._surreal_runner",
    "logger": "yadgar.core._surreal_runner._surreal_runner",
    "logging": "yadgar.core._surreal_runner._surreal_runner",
    "observe": "yadgar.core._surreal_runner._surreal_runner",
    "os": "yadgar.core._surreal_runner._surreal_runner",
    "purge_registered_test_data_dirs": "yadgar.core._surreal_runner._surreal_runner",
    "reap_stale_surreal": "yadgar.core._surreal_runner._surreal_runner",
    "register_test_data_dir": "yadgar.core._surreal_runner._surreal_runner",
    "remove_test_data_dir": "yadgar.core._surreal_runner._surreal_runner",
    "signal": "yadgar.core._surreal_runner._surreal_runner",
    "socket": "yadgar.core._surreal_runner._surreal_runner",
    "spawn_surreal": "yadgar.core._surreal_runner._surreal_runner",
    "subprocess": "yadgar.core._surreal_runner._surreal_runner",
    "sweep_orphan_surreal_data_dirs": "yadgar.core._surreal_runner._surreal_runner",
    "teardown_surreal_proc": "yadgar.core._surreal_runner._surreal_runner",
    "time": "yadgar.core._surreal_runner._surreal_runner",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
