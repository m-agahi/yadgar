"""Car 0 — pin the ``hook_runner.py`` shim contract.

After Car 0 collapsed the two hook code paths into ``yadgar.core.cli.hook``,
``hook_runner.py`` is a thin re-export shim. External importers + the installed
settings.json entry (`hook_runner.py <event>`) depend on:

1. The public + private handler surface staying importable from the shim path
   (`from yadgar.core.scripts.hook_runner import hook_db_lockdown_check`, etc.).
2. The shim's ``main()`` delegating to the shared dispatcher IN-PROCESS (same
   interpreter, same stdin) — not a subprocess.
3. Byte-identical dispatch behaviour: unknown event → exit 1; each of the 6
   events routes to the same handler object the impl module exposes.
"""

from __future__ import annotations

import pytest

import yadgar.core.cli.hook as impl
import yadgar.core.scripts.hook_runner as hr

# The surface the characterization suite + external importers reach through the shim.
_REEXPORTED = [
    "_AUTH_TOKEN",
    "_PORT",
    "_HOOKS",
    "_auth_headers",
    "_http_get",
    "_http_post",
    "_capture_in_flight_host",
    "_log_hook_error",
    "dispatch",
    "hook_post_tool_capture",
    "hook_session_start_context",
    "hook_post_compact_rehydrate",
    "hook_pre_compact_drain",
    "hook_prompt_recall",
    "hook_block_reflect",
    "hook_db_lockdown_check",
]


@pytest.mark.parametrize("name", _REEXPORTED)
def test_shim_reexports_impl_object(name):
    """Every re-exported name IS the same object the impl module exposes."""
    assert getattr(hr, name) is getattr(impl, name)


def test_shim_hooks_table_has_six_events():
    """The 6 dispatch keys (not 5) — observed code, reported in Car 0 findings."""
    assert set(hr._HOOKS) == {
        "post-tool-capture",
        "session-start-context",
        "post-compact-rehydrate",
        "pre-compact-drain",
        "prompt-recall",
        "block-reflect",
    }


def test_shim_main_unknown_event_exits_1(monkeypatch):
    """`hook_runner.py <bad-event>` exits 1 (unchanged from pre-Car-0)."""
    monkeypatch.setattr("sys.argv", ["hook_runner.py", "no-such-event"])
    with pytest.raises(SystemExit) as exc:
        hr.main()
    assert exc.value.code == 1


def test_shim_main_no_args_exits_1(monkeypatch):
    """No hook_type → usage + exit 1 (unchanged)."""
    monkeypatch.setattr("sys.argv", ["hook_runner.py"])
    with pytest.raises(SystemExit) as exc:
        hr.main()
    assert exc.value.code == 1


def test_shim_main_delegates_in_process(monkeypatch):
    """`main()` runs the handler in-process via the shared ``_HOOKS`` table."""
    calls = []
    patched = dict(impl._HOOKS)
    patched["prompt-recall"] = lambda: calls.append("ran")
    monkeypatch.setattr(impl, "_HOOKS", patched)
    monkeypatch.setattr("sys.argv", ["hook_runner.py", "prompt-recall"])
    with pytest.raises(SystemExit) as exc:
        hr.main()
    assert exc.value.code == 0
    assert calls == ["ran"]
