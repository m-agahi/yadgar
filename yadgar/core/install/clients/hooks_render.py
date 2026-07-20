"""Hook-emitter generator — Car 0 (the hook-layer seam).

Makes the ``ClientDescriptor.hooks_kind`` field LIVE. Mirrors the two existing
per-client generators — ``mcp_register.register_mcp`` and
``rules_render.write_rules`` — with the same dispatch shape: one public
``register_hooks(descriptor, …)`` entrypoint that selects a per-kind
``_emit_<hooks_kind>()`` serializer from the ``_EMITTERS`` table.

Every ported client's emitted hook artifact ultimately shells out to the shared
``yadgar hook <event>`` CLI (``yadgar.core.cli.hook``) — the ONE implementation
of the auth + branch-detection + ``/hooks/*`` HTTP logic. So a per-client car
stays thin: one ``_emit_<kind>()`` that writes the client's native
hook-registration artifact (a TS plugin for OpenCode, a ``hooks.json`` / TOML
entry for Codex, …) invoking ``yadgar hook <event>``.

Car 0 scope — the seam + Claude Code only:
  * ``claude_json`` (Claude Code) routes through the shared, already-idempotent
    ``install_hooks_impl`` so there is one settings.json-writing code path.
  * ``None`` (Gemini advisory-only) emits nothing.
  * The other 7 kinds are explicit ``NotImplementedError`` stubs that Cars A–G
    fill (``_emit_opencode_plugin`` = Car A, ``_emit_cursor_hooks`` = Car B, …).
    A ``NotImplementedError`` (not a silent no-op) means a half-wired client
    fails loud rather than pretending to install hooks it didn't.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yadgar._shared.observability.observe import observe
from yadgar.core.install.clients.merge import _atomic_write_text, _load_json
from yadgar.core.install.install_hooks_lib import install_hooks_impl

if TYPE_CHECKING:
    from yadgar.core.install.clients.descriptor import ClientDescriptor


# ── Per-kind emitters (one per hooks_kind value) ──────────────────────────────


@observe(tier="stage")
def _emit_claude_json(
    descriptor: ClientDescriptor,
    home_dir: Path | None,
    scope: str,
    project_dir: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Claude Code — delegate to the shared ``install_hooks_impl`` (one path).

    Claude Code's hook wiring already lives in ``install_hooks_impl`` (writes the
    core-hook entries into ``settings.json``, wires the ``hook_runner.py`` shim
    which now delegates to ``yadgar hook <event>``). Routing ``claude_json``
    through it keeps ONE settings.json writer and inherits its idempotency +
    dry-run support for free.
    """
    resolved_home = home_dir if home_dir is not None else Path.home()
    project_directory = str(project_dir) if project_dir is not None else None
    return install_hooks_impl(
        home_dir=resolved_home,
        scope=scope,
        project_directory=project_directory,
        dry_run=dry_run,
    )


# ── Cursor (Car B) ────────────────────────────────────────────────────────────
#
# Cursor's hook contract, primary-source re-verified 2026-07-20 (ADR-0143 gate;
# corrects the ADR-0145 2026-07-18 snapshot):
#   * Config file: ``.cursor/hooks.json`` (project) / ``~/.cursor/hooks.json``
#     (global). Schema: ``{"version": 1, "hooks": {"<eventName>": [ {…} ]}}``.
#     A separate file from the MCP ``mcp.json`` — hence its own path resolution.
#   * Cursor runs EVERY command registered for an event → append (don't clobber)
#     yadgar's entry alongside any the user already has.
#   * Inject is BROKEN upstream: ``additional_context`` on ``sessionStart`` /
#     ``postToolUse`` is accepted+merged but never surfaced to the model, and
#     ``beforeSubmitPrompt`` output is not respected at all (open forum bugs,
#     mid-2026). ``stop`` is observation-only (``followup_message`` auto-continues,
#     it does NOT block). So the only hooks that actually DO something are the two
#     FIRE-AND-POST ones (capture + drain) that need no model-surfaced return.
#   * We therefore wire ONLY ``postToolUse`` and ``preCompact``; emitting
#     sessionStart/beforeSubmitPrompt would FAKE a broken inject (plan R7 forbids).
#
# Each command shells out to the shared ``yadgar hook <event>`` CLI — Cursor's
# native stdin JSON (``tool_name`` / ``tool_input`` / ``cwd`` / ``transcript_path``)
# already matches the keys the shared handlers read, so no per-client shim.

# Cursor event name → shared ``yadgar hook`` event (the fire-and-POST subset).
_CURSOR_EVENT_MAP = {
    "postToolUse": "post-tool-capture",
    "preCompact": "pre-compact-drain",
}

# Marker identifying a yadgar-owned command entry inside an event's array (for
# idempotent replace-in-place instead of duplicate-append on re-run).
_YADGAR_HOOK_MARKER = "yadgar hook "


@observe(tier="stage")
def _cursor_hooks_path(home_dir: Path | None, scope: str, project_dir: Path | None) -> Path:
    """Resolve ``.cursor/hooks.json`` for the given scope.

    Cursor's hooks file is distinct from its ``mcp.json`` (which the descriptor's
    ``mcp_config_path`` points at), so it is resolved here rather than off the
    descriptor.
    """
    if scope == "project":
        if project_dir is None:
            raise ValueError("project_dir required when scope='project'")
        base = project_dir
    else:
        base = home_dir if home_dir is not None else Path.home()
    return base / ".cursor" / "hooks.json"


@observe(tier="stage")
def _merge_cursor_hook_entry(hooks: dict[str, Any], event: str, command: str) -> None:
    """Idempotently register ``command`` under ``hooks[event]``, preserving foreign entries.

    Cursor executes every command registered for an event, so we append yadgar's
    entry alongside the user's. Re-running replaces the existing yadgar entry in
    place (matched by the ``yadgar hook`` marker) instead of duplicating it.
    """
    entry = {"command": command, "type": "command"}
    existing = hooks.get(event)
    if not isinstance(existing, list):
        hooks[event] = [entry]
        return
    kept = [
        e
        for e in existing
        if not (isinstance(e, dict) and _YADGAR_HOOK_MARKER in str(e.get("command", "")))
    ]
    kept.append(entry)
    hooks[event] = kept


@observe(tier="stage")
def _emit_cursor_hooks(
    descriptor: ClientDescriptor,
    home_dir: Path | None,
    scope: str,
    project_dir: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Cursor — write ``.cursor/hooks.json`` wiring the fire-and-POST hook subset.

    Emits ONLY ``postToolUse`` (→ ``yadgar hook post-tool-capture``) and
    ``preCompact`` (→ ``yadgar hook pre-compact-drain``): the two Cursor hooks
    whose contract is FUNCTIONAL (fire-and-POST, no model-surfaced return). Cursor's
    inject path is broken upstream and ``stop`` is observation-only, so
    session-start / prompt-recall / checkpoint hooks are deliberately NOT emitted
    (never fake a broken hook — plan R7).

    Idempotent + format-preserving: foreign hooks and the user's own per-event
    commands survive; a re-run replaces yadgar's entry in place.
    """
    path = _cursor_hooks_path(home_dir, scope, project_dir)
    events = sorted(_CURSOR_EVENT_MAP)

    if dry_run:
        return {"path": str(path), "events": events, "written": False}

    doc = _load_json(path)
    doc["version"] = 1
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        doc["hooks"] = hooks

    for cursor_event, cli_event in _CURSOR_EVENT_MAP.items():
        _merge_cursor_hook_entry(hooks, cursor_event, f"yadgar hook {cli_event}")

    _atomic_write_text(path, json.dumps(doc, indent=2) + "\n")
    return {"path": str(path), "events": events, "written": True}


@observe(tier="stage")
def _emit_stub(
    kind: str,
    car: str,
    descriptor: ClientDescriptor,
    home_dir: Path | None,
    scope: str,
    project_dir: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Typed placeholder for a not-yet-implemented client hook kind.

    Raises ``NotImplementedError`` (not a silent no-op) so a half-wired client
    fails loud. Each later car (A–G) replaces its ``kind`` with a real
    ``_emit_<kind>`` that writes the client's native hook-registration artifact
    (which shells out to ``yadgar hook <event>``).
    """
    raise NotImplementedError(
        f"hooks_kind={kind!r} emitter is a Car-0 stub — implemented in {car}. "
        f"Every emitted artifact shells out to `yadgar hook <event>`; write "
        f"{descriptor.name!r}'s native hook-registration file here."
    )


# ── Dispatch table (hooks_kind → emitter | stub-car) ──────────────────────────
#
# Car 0 implements claude_json; the 7 client kinds map to the car that fills
# them (dispatched through ``_emit_stub`` until then). The table's KEYS must
# stay in sync with the registry's hooks_kind values (guarded by
# test_hooks_render.test_dispatch_table_covers_every_registry_hooks_kind).
_EMITTERS = {
    "claude_json": _emit_claude_json,
    "cursor_hooks": _emit_cursor_hooks,
}

# hooks_kind → the car that implements its emitter (Car-0 stubs until then).
_STUB_CARS = {
    "opencode_plugin": "Car A",
    "codex_hooks_json": "Car C",
    "cline_hooks": "Car D",
    "kiro_hooks_json": "Car E",
    "windsurf_hooks": "Car F",
    "amp_hooks": "Car G",
}


@observe(tier="boundary")
def register_hooks(
    descriptor: ClientDescriptor,
    home_dir: Path | None = None,
    scope: str = "global",
    project_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Emit *descriptor*'s hook wiring, dispatching on ``hooks_kind``.

    The public entrypoint mirroring ``register_mcp`` / ``write_rules``.

    Args:
        descriptor: the client descriptor (drives the emitter via ``hooks_kind``).
        home_dir:   user home (tests pass a temp dir); ``None`` → ``Path.home()``.
        scope:      ``"global"`` (default) or ``"project"``.
        project_dir: project root (required by some kinds when scope=``"project"``).
        dry_run:    compute but write nothing.

    Returns:
        ``{"client": str, "hooks_kind": str | None, "emitted": bool,
           "result": <emitter return> | None}``. ``emitted`` is False only for
        ``hooks_kind is None`` (advisory-only clients).

    Raises:
        NotImplementedError: for a ``hooks_kind`` whose emitter is still a stub
            (the 7 later-car client kinds).
        KeyError: for a ``hooks_kind`` with no dispatch entry (registry drift —
            guarded by the completeness test).
    """
    kind = descriptor.hooks_kind
    if kind is None:
        # Advisory-only client (Gemini): no hook surface to emit.
        return {
            "client": descriptor.name,
            "hooks_kind": None,
            "emitted": False,
            "result": None,
        }

    if kind in _EMITTERS:
        result = _EMITTERS[kind](descriptor, home_dir, scope, project_dir, dry_run)
    elif kind in _STUB_CARS:
        result = _emit_stub(
            kind, _STUB_CARS[kind], descriptor, home_dir, scope, project_dir, dry_run
        )
    else:
        raise KeyError(f"no hook emitter registered for hooks_kind={kind!r}")

    return {
        "client": descriptor.name,
        "hooks_kind": kind,
        "emitted": True,
        "result": result,
    }


# All hook kinds this module dispatches (real emitters + Car-0 stubs). Kept as a
# frozenset for the completeness test (registry hooks_kind ⊆ this set).
_DISPATCHED_KINDS = frozenset(_EMITTERS) | frozenset(_STUB_CARS)
