"""Per-project external task ledger gate (ledger 1791, yadgarhq/docs ADR-0992).

Some projects keep their tasks in a ledger outside yadgar (yaadgaar, the
gateway task module, reached as ``mcp__yaadgaar__*``). For those projects the
yadgar task ledger is FROZEN: SessionStart does not nudge or seed from it, the
stop-hook checkpoint has no ledger-mirror step, and ``task_write`` refuses.
``task_list`` / ``task_get`` stay readable, because the frozen rows are history.

Which projects: the GLOBAL runtime config key ``tasks.external_ledger_prefixes``
(a list of project_id prefixes, default ``[]``). A project matches when its
project_id starts with any listed prefix. The key is read through the existing
runtime config store, never a second config system:

  * daemon-side callers (SessionStart, ``task_write``) inject the in-process
    resolver ``core.server.tools._runtime_config.config_get``;
  * host-side callers (the stop hook) inject the fail-open host client
    ``core.runtime_config_client.get``.

Both satisfy ``(key, directory, default) -> value``. The read always uses the
GLOBAL scope (``directory=None``): the prefixes name projects, so a per-directory
override of the list would mean nothing.

Fail-open to TODAY'S behaviour: an unreadable, absent or malformed value is the
empty list, and the empty list matches nothing. A project that does not match
takes exactly the code path it took before this module existed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from yadgar._shared.observability.observe import observe

#: Runtime config key (global scope) — a list of project_id prefixes.
EXTERNAL_LEDGER_PREFIXES_KEY = "tasks.external_ledger_prefixes"

#: The error code ``task_write`` returns for a project whose ledger is external.
EXTERNAL_LEDGER_ERROR = "external_task_ledger"

#: Resolver signature: ``(key, directory, default) -> value``. The gate always
#: passes ``None`` as the second argument: it reads the GLOBAL scope only.
Resolver = Callable[[str, "str | None", Any], Any]

logger = logging.getLogger(__name__)


@observe(tier="hot", metric="core.task_ledger_gate.daemon_resolver")
def daemon_resolver(key: str, _scope: str | None, default: Any) -> Any:
    """The in-process (daemon-side) resolver, global scope, imported at call time.

    Imported lazily so the host-side stop hook, which never runs in the
    daemon, does not pull in the server package, and so a test that patches
    ``_runtime_config.config_get`` is seen by every daemon-side gate.
    """
    from yadgar.core.server.tools import _runtime_config

    return _runtime_config.config_get(key, None, default)


@observe(tier="stage", metric="core.task_ledger_gate.prefixes")
def external_ledger_prefixes(resolver: Resolver) -> tuple[str, ...]:
    """Return the configured prefixes; ``()`` when unset, malformed or unreadable.

    The default passed to the resolver is ``[]``, not ``None``: the daemon
    resolver caches a resolved value, and a ``None`` result reads as a cache
    miss, which would cost two backend forwards on every call.

    Only non-blank strings survive. A blank prefix would match every project,
    so it is dropped rather than honoured.
    """
    try:
        raw = resolver(EXTERNAL_LEDGER_PREFIXES_KEY, None, [])
    except Exception:  # noqa: BLE001 — a config read must never break SessionStart, the stop hook or task_write; unreadable means "no external ledger", i.e. today's behaviour
        logger.warning(
            "%s unreadable; treating it as unset (no external task ledger)",
            EXTERNAL_LEDGER_PREFIXES_KEY,
            exc_info=True,
        )
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(p for p in raw if isinstance(p, str) and p.strip())


@observe(tier="stage", metric="core.task_ledger_gate.is_external")
def is_external_ledger_project(project_id: str | None, resolver: Resolver) -> bool:
    """True when ``project_id`` starts with a configured external-ledger prefix.

    No project, no read: an empty or absent project_id never matches, and the
    resolver is not called for it.
    """
    if not isinstance(project_id, str) or not project_id:
        return False
    return any(project_id.startswith(p) for p in external_ledger_prefixes(resolver))


@observe(exempt="pure f-string render of one constant line; no I/O, no branch, no error path")
def session_start_line(project_id: str) -> str:
    """The one SessionStart line that names the active ledger."""
    return (
        f"[yadgar] Tasks for {project_id} live in yaadgaar (mcp__yaadgaar__*); "
        "the yadgar task ledger is frozen for this project.\n"
    )


@observe(exempt="pure dict literal render of a fixed refusal; no I/O, no branch, no error path")
def task_write_refusal(project_id: str) -> dict:
    """The ``task_write`` refusal for a project whose tasks live in yaadgaar."""
    return {
        "ok": False,
        "error": EXTERNAL_LEDGER_ERROR,
        "project_id": project_id,
        "ledger": "yaadgaar",
        "message": (
            f"Tasks for {project_id} live in yaadgaar: use mcp__yaadgaar__create_task, "
            "edit_task or transition_task. The yadgar task ledger is frozen for this "
            "project; task_list and task_get still read its history."
        ),
    }


@observe(tier="stage", metric="core.task_ledger_gate.write_refusal_for")
def write_refusal_for(project_id: str, resolver: Resolver = daemon_resolver) -> dict | None:
    """``task_write``'s gate: the refusal for an external-ledger project, else None."""
    if is_external_ledger_project(project_id, resolver):
        return task_write_refusal(project_id)
    return None


@observe(tier="stage", metric="core.task_ledger_gate.session_task_section")
async def session_task_section(
    project: str,
    read_nudge: Callable[[], Awaitable[tuple[str, list[dict]]]],
    resolver: Resolver = daemon_resolver,
) -> tuple[str, str, list[dict]]:
    """Return ``(render_prefix, task_nudge, task_rows)`` for SessionStart.

    External-ledger project → the one ledger line as the prefix, no nudge, no
    rows, and NO ledger read at all (the hook then neither seeds nor nudges).
    Any other project → ``("", *await read_nudge())``, which is the
    SessionStart behaviour from before this gate. ``read_nudge`` is the
    caller's bound ``_task_list_restore_nudge`` call.
    """
    if is_external_ledger_project(project, resolver):
        return session_start_line(project), "", []
    nudge, rows = await read_nudge()
    return "", nudge, rows
