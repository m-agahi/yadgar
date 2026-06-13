"""Central helpers for spawning and reaping SurrealDB test subprocesses.

v5.25.1: Implementation moved to yadgar/_surreal_runner.py so benchmarks can
import it without pulling in test-only modules.  This file is now a re-export
shim so existing test imports continue to work unchanged.
"""

from yadgar._surreal_runner import (  # noqa: F401
    _DEFAULT_PORT_BASE,
    _RETRY_BACKOFF_MS,
    _SPAWNED_SURREAL_PIDS,
    _port_in_use,
    _worker_index,
    allocate_port,
    allocate_port_with_retry,
    kill_all_spawned_surreal,
    reap_stale_surreal,
    spawn_surreal,
    teardown_surreal_proc,
)
