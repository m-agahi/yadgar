"""Central helpers for spawning and reaping SurrealDB test subprocesses.

v5.25.1: Implementation moved to yadgar/_surreal_runner.py so benchmarks can
import it without pulling in test-only modules.  This file is now a re-export
shim so existing test imports continue to work unchanged.
"""

from yadgar.core._surreal_runner import (  # noqa: F401
    _DEFAULT_PORT_BASE,
    _RETRY_BACKOFF_MS,
    _SPAWNED_SURREAL_DATA_DIRS,
    _SPAWNED_SURREAL_PIDS,
    _port_in_use,
    _worker_index,
    allocate_port,
    allocate_port_with_retry,
    kill_all_spawned_surreal,
    purge_registered_test_data_dirs,
    reap_stale_surreal,
    register_test_data_dir,
    remove_test_data_dir,
    remove_test_tmp_dir,
    spawn_surreal,
    sweep_orphan_surreal_data_dirs,
    sweep_orphan_test_tmp_dirs,
    teardown_surreal_proc,
)
