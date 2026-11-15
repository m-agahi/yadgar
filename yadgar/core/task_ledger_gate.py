"""Per-project external task ledger gate (ledger 1791, yadgarhq/docs ADR-0992).

Some projects keep their tasks in a ledger outside yadgar (yaadgaar, the
gateway task module, reached as ``mcp__yaadgaar__*``). For those projects the
yadgar task ledger is FROZEN: SessionStart does not nudge or seed from it, the
stop-hook checkpoint has no ledger-mirror step, and ``task_write`` refuses.
``task_list`` / ``task_get`` stay readable, because the frozen rows are history.

Which projects (ledger 1803, yadgarhq/docs ADR-1000): the yaadgaar gateway's
project registry decides. Its answer reaches the gate as a per-project record
the yaadgaar client writes, mirrored into the yadgar data dir by yadgar's host
SessionStart hook (``core.task_ledger_registry`` holds the contract). Only when
the registry has no answer for a project does the FALLBACK apply.

Fallback: the GLOBAL runtime config key ``tasks.external_ledger_prefixes``
(a list of project_id prefixes, default ``[]``). A project matches when its
project_id starts with any listed prefix. The key is read through the existing
runtime config store, never a second config system:

  * daemon-side callers (SessionStart, ``task_write``) inject the in-process
    resolver ``core.server.tools._runtime_config.config_get``;
  * host-side callers (the stop hook) inject the fail-open host client
    ``core.runtime_config_client.get``.

Both satisfy ``(key, directory, default) -> value``. The read always uses the
GLOBAL scope (``directory=None``): the prefixes name projects, so a per-directory
override of the list would mean nothing. Prefixes match case-insensitively.

Fail-open to TODAY'S behaviour: an absent or malformed value is the empty list,
and the empty list matches nothing. A malformed value logs one warning per
process. A FAILED read (the daemon resolver's backend forward raised) also
matches nothing for that call, logs a warning, and is not cached, so the next
read retries: an external-ledger project falls back to the yadgar ledger only
while the read keeps failing. A project that does not match takes exactly the
code path it took before this module existed.

Kill switch (yadgarhq/docs yaadgaar #699, Max 2026-11-15): the GLOBAL runtime
config key ``tasks.ledger_disabled`` (bool, default ``false``). When it is
``true`` the yadgar task ledger is off for EVERY project: the gate answers
"external" before it consults the registry or the prefixes, so every consumer
above (SessionStart nudge and seed, the stop-hook template, ``task_write``)
follows it with no change of its own. Only a JSON ``true`` turns it on. An
absent, ``false``, malformed or unreadable value is off and falls through to
the registry and the prefix fallback exactly as before; a malformed value
warns once per process, a failed read warns and is not cached.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from yadgar._shared.observability.observe import observe

#: Runtime config key (global scope) — a list of project_id prefixes.
EXTERNAL_LEDGER_PREFIXES_KEY = "tasks.external_ledger_prefixes"

#: Runtime config key (global scope) — bool; ``true`` turns the yadgar task
#: ledger off for every project (yaadgaar #699).
LEDGER_DISABLED_KEY = "tasks.ledger_disabled"

#: The error code ``task_write`` returns for a project whose ledger is external.
EXTERNAL_LEDGER_ERROR = "external_task_ledger"

#: Resolver signature: ``(key, directory, default) -> value``. The gate always
#: passes ``None`` as the second argument: it reads the GLOBAL scope only.
Resolver = Callable[[str, "str | None", Any], Any]

#: Registry lookup: ``project_id -> True | False | None``. ``None`` means "no
#: answer" and sends the gate to the prefix fallback.
RegistryLookup = Callable[[str], "bool | None"]

logger = logging.getLogger(__name__)

#: Set once a malformed value has been reported, so a bad row warns once per
#: process instead of on every SessionStart and ``task_write``.
_malformed_warned = False

#: Same once-per-process guard for a malformed ``tasks.ledger_disabled`` value.
_disabled_malformed_warned = False


@observe(tier="hot", metric="core.task_ledger_gate.daemon_resolver")
def daemon_resolver(key: str, _scope: str | None, default: Any) -> Any:
    """The in-process (daemon-side) resolver, global scope, imported at call time.

    Imported lazily so the host-side stop hook, which never runs in the
    daemon, does not pull in the server package, and so a test that patches
    ``_runtime_config.config_get`` is seen by every daemon-side gate.

    ``raise_on_error=True``: a failed backend forward raises
    ``ConfigReadError`` (nothing cached) instead of answering ``default``, so
    the gate can log that the read failed rather than read it as "unset".
    """
    from yadgar.core.server.tools import _runtime_config

    return _runtime_config.config_get(key, None, default, raise_on_error=True)


@observe(tier="stage", metric="core.task_ledger_gate.prefixes")
def external_ledger_prefixes(resolver: Resolver) -> tuple[str, ...]:
    """Return the configured prefixes; ``()`` when unset, malformed or unreadable.

    The default passed to the resolver is ``[]``, not ``None``: the daemon
    resolver caches a resolved value, and a ``None`` result reads as a cache
    miss, which would cost two backend forwards on every call.

    Only non-blank strings survive, casefolded. A blank prefix would match
    every project, so it is dropped rather than honoured. A value that is not
    a list, or a list holding a non-string, logs one warning per process.
    """
    try:
        raw = resolver(EXTERNAL_LEDGER_PREFIXES_KEY, None, [])
    except Exception:  # noqa: BLE001 — a config read must never break SessionStart, the stop hook or task_write; a failed read matches nothing for this call only (not cached)
        logger.warning(
            "%s read failed; no external task ledger for this call (not cached, "
            "the next read retries)",
            EXTERNAL_LEDGER_PREFIXES_KEY,
            exc_info=True,
        )
        return ()
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        _warn_malformed(raw)
        if not isinstance(raw, list):
            return ()
    return tuple(p.casefold() for p in raw if isinstance(p, str) and p.strip())


@observe(tier="stage", metric="core.task_ledger_gate.warn_malformed")
def _warn_malformed(raw: Any) -> None:
    """Warn once per process that the key holds something other than a string list."""
    global _malformed_warned
    if _malformed_warned:
        return
    _malformed_warned = True
    logger.warning(
        "%s is malformed (want a list of project_id prefix strings, got %r); "
        "non-string entries are ignored",
        EXTERNAL_LEDGER_PREFIXES_KEY,
        raw,
    )


@observe(tier="stage", metric="core.task_ledger_gate.ledger_disabled")
def ledger_disabled(resolver: Resolver) -> bool:
    """True only when ``tasks.ledger_disabled`` reads as the JSON value ``true``.

    The default passed to the resolver is ``False``, not ``None``, for the same
    cache reason as the prefixes read. A failed read is off for this call only
    (logged, not cached); a value that is neither a bool nor unset warns once
    per process and is off.
    """
    try:
        raw = resolver(LEDGER_DISABLED_KEY, None, False)
    except Exception:  # noqa: BLE001 — a config read must never break SessionStart, the stop hook or task_write; a failed read is "off" for this call only (not cached)
        logger.warning(
            "%s read failed; the yadgar task ledger stays on for this call (not "
            "cached, the next read retries)",
            LEDGER_DISABLED_KEY,
            exc_info=True,
        )
        return False
    if raw is None or isinstance(raw, bool):
        return raw is True
    _warn_disabled_malformed(raw)
    return False


@observe(tier="stage", metric="core.task_ledger_gate.warn_disabled_malformed")
def _warn_disabled_malformed(raw: Any) -> None:
    """Warn once per process that ``tasks.ledger_disabled`` is not a bool."""
    global _disabled_malformed_warned
    if _disabled_malformed_warned:
        return
    _disabled_malformed_warned = True
    logger.warning(
        "%s is malformed (want true or false, got %r); the yadgar task ledger stays on",
        LEDGER_DISABLED_KEY,
        raw,
    )


@observe(tier="stage", metric="core.task_ledger_gate.mirror_registry")
def mirror_registry(project_id: str) -> bool | None:
    """The default registry lookup: the mirror the host SessionStart hook wrote.

    Imported at call time so importing the gate stays cheap for the stop hook.
    """
    from yadgar.core.task_ledger_registry import read_mirror

    return read_mirror(project_id)


@observe(tier="stage", metric="core.task_ledger_gate.registry_answer")
def _registry_answer(project_id: str, registry: RegistryLookup) -> bool | None:
    """The registry's answer, or None when it has none or the lookup fails."""
    try:
        answer = registry(project_id)
    except Exception:  # noqa: BLE001 — a registry lookup must never break SessionStart, the stop hook or task_write; no answer sends the gate to the prefix fallback
        logger.warning(
            "yaadgaar registry lookup failed for %s; using the prefix fallback",
            project_id,
            exc_info=True,
        )
        return None
    return answer if isinstance(answer, bool) else None


@observe(tier="stage", metric="core.task_ledger_gate.is_external")
def is_external_ledger_project(
    project_id: str | None, resolver: Resolver, registry: RegistryLookup = mirror_registry
) -> bool:
    """True when ``project_id``'s tasks live in yaadgaar (ledger 1803, ADR-1000).

    ``tasks.ledger_disabled`` = ``true`` decides first, for every project
    (yaadgaar #699). Otherwise the yaadgaar project registry decides, and the
    configured prefixes decide only when the registry has no answer for this
    project (no record yet, a fault). Each decision logs one INFO line naming
    its source: ``disabled``, ``registry`` or ``fallback-prefix``.

    No project, no read: an empty or absent project_id never matches, and
    neither the registry nor the resolver is called for it.
    """
    if not isinstance(project_id, str) or not project_id:
        return False
    if ledger_disabled(resolver):
        _log_decision(project_id, "disabled", True)
        return True
    answer = _registry_answer(project_id, registry)
    if answer is not None:
        _log_decision(project_id, "registry", answer)
        return answer
    project = project_id.casefold()
    external = any(project.startswith(p) for p in external_ledger_prefixes(resolver))
    _log_decision(project_id, "fallback-prefix", external)
    return external


@observe(tier="stage", metric="core.task_ledger_gate.log_decision")
def _log_decision(project_id: str, source: str, external: bool) -> None:
    """One INFO line per decision, with the source as a structured field."""
    logger.info(
        "task ledger for %s: %s (decided by %s)",
        project_id,
        "yaadgaar" if external else "yadgar",
        source,
        extra={"ledger_project": project_id, "ledger_source": source, "external": external},
    )


@observe(exempt="pure f-string render of one constant line; no I/O, no branch, no error path")
def session_start_line(project_id: str) -> str:
    """The one SessionStart line that names the active ledger."""
    return (
        f"[yadgar] Tasks for {project_id} live in yaadgaar (mcp__yaadgaar__*); "
        "the yadgar task ledger is frozen for this project.\n"
    )


@observe(tier="stage", metric="core.task_ledger_gate.brief_task_line")
def brief_task_line(resolver: Resolver = daemon_resolver) -> str:
    """``project_brief``'s empty-state task hint; names yaadgaar when the ledger is off.

    Keyed on ``tasks.ledger_disabled`` ALONE, not the per-project gate, so a
    false key keeps the pre-#699 line for every project (yaadgaar #699).
    """
    if ledger_disabled(resolver):
        return (
            "*Tasks live in yaadgaar (mcp__yaadgaar__*): the yadgar task ledger is"
            " disabled (tasks.ledger_disabled); its task_list and task_get read"
            " history only.*"
        )
    return (
        "*To track TODOs/tasks use the harness task list (TaskCreate)"
        " and the yadgar `task_write` tool — the SQL ledger is the source"
        " of truth, the wiki `{project}-task-list` page is a derived mirror.*"
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
