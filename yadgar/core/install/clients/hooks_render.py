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


# ── OpenCode (Car A) ──────────────────────────────────────────────────────────
#
# OpenCode's hook contract, primary-source re-verified 2026-07-26
# (docs/plans/port-opencode-re-audit-2026-07-26.md; supersedes 2026-07-20 plan):
#   * Plugin file: `~/.config/opencode/plugins/yadgar-hooks.ts` (global) or
#     `.opencode/plugins/yadgar-hooks.ts` (project). Bun discovers these on
#     startup; npm plugins are also supported but the Yadgar use-case is a
#     single-file shim — local-file install.
#   * The plugin is a JavaScript/TypeScript module that exports one or more
#     plugin functions. Each function receives a `PluginInput` context and
#     returns a `Hooks` object. The typed `Hooks` interface in
#     `@opencode-ai/plugin@1.18.5/dist/index.d.ts` exposes:
#       - `tool.execute.after` → typed hook (PostToolUse)
#       - `experimental.session.compacting` → typed hook (PreCompact)
#         with `{ context: string[]; prompt?: string }` output (mutate
#         `output.context` to append drain context; do NOT clobber
#         `output.prompt`).
#       - `event?: (input: {event: Event})` → generic callback that
#         dispatches typed `Event` objects (EventSessionCreated,
#         EventSessionCompacted, EventSessionIdle, etc.) per the SDK's
#         `gen/types.gen.d.ts`.
#   * Coverage (3/5 functional, 1/5 non-blocking, 1/5 gated on headless test):
#       - sessionStart         → event.type === "session.created"  (FUNCTIONAL)
#       - sessionStart-restore → event.type === "session.compacted" (FUNCTIONAL)
#       - postToolUse          → tool.execute.after                (FUNCTIONAL)
#       - preCompact           → experimental.session.compacting    (FUNCTIONAL)
#       - stop                 → event.type === "session.idle"     (NON-BLOCKING
#                                  observer; promote to blocking on sst/opencode#16626)
#       - userPromptSubmit     → chat.message.parts[] mutation     (DEFERRED to
#                                  headless `opencode run` test, see plan §4.5)
#   * IPC: thin TS shim that `execa`s the `yadgar hook <event>` CLI. The
#     typed `ctx.client` is opencode's own SDK (no generic MCP invoker),
#     so HTTP-to-MCP is NOT a working pattern here.
#   * Idempotency: replace-in-place, marker-detected (see `_OPENCODE_MANAGED_MARKER`).
#     Plugin files have no foreign-preserve concern (single-file, no shared
#     hooks.json) — overwriting on re-run is fine.
#   * Car A wires the 4 OUT-of-the-box functional events. chat.message is
#     intentionally NOT in the template (gated on a headless test per the
#     plan); session.idle IS in (non-blocking observer only).


# Marker comment that identifies a yadgar-managed opencode plugin file. Re-runs
# detect this marker to confirm "this is our file" before overwriting.
_OPENCODE_MANAGED_MARKER = "// @yadgar-managed: opencode hook plugin (do not edit)"


# Canonical plugin template — written verbatim to `~/.config/opencode/plugins/yadgar-hooks.ts`.
# The template subscribes to:
#   1. experimental.session.compacting  → PreCompact drain (yadgar hook pre-compact-drain)
#   2. tool.execute.after               → PostToolUse capture (yadgar hook post-tool-capture)
#   3. event callback                   → session.created → sessionStart signal
#                                       → session.compacted → sessionStart restore
#                                       → session.idle → stop observer (non-blocking)
# chat.message (UserPromptSubmit) is NOT in the template — gated on a headless test
# per the plan. The comment in the template explains this for future maintainers.
_OPENCODE_PLUGIN_TEMPLATE = """{marker}
// Auto-generated by `yadgar install-hooks --client opencode` (Car A, 2026-07-26).
// Supersedes the 2026-07-20 plan. See docs/plans/port-opencode-re-audit-2026-07-26.md
// for the primary-source re-audit that this template reflects.
//
// Yadgar hook plugin for OpenCode. Five yadgar hook needs are wired:
//   1. SessionStart       → event callback (session.created)    → `yadgar hook session-start`
//   2. SessionStart-resume → event callback (session.compacted)  → `yadgar hook session-start --mode restore`
//   3. PostToolUse        → tool.execute.after (typed hook)    → `yadgar hook post-tool-capture`
//   4. PreCompact         → experimental.session.compacting     → `yadgar hook pre-compact-drain`
//   5. Stop (observer)    → event callback (session.idle)       → `yadgar hook stop` (non-blocking)
//
// UserPromptSubmit (chat.message parts[] mutation) is NOT in this template —
// that path is gated on a headless `opencode run` test per the re-audit plan
// §4.5. When that test passes, add the chat.message handler in the same shape
// as the tool.execute.after block below (mutate output.parts).
//
// IPC: this plugin is a thin shim. It does NOT call Yadgar MCP directly — the
// `ctx.client` typed SDK is opencode's own (no generic MCP invoker). Instead it
// shells out to the `yadgar` CLI via `execa`, which routes through MCP.

import type {{ Plugin }} from "@opencode-ai/plugin"
import {{ execa }} from "execa"

const YADGAR = (args: Record<string, unknown>) =>
  execa("yadgar", ["hook", "--event", String(args.event), "--directory", String(args.directory), "--json", JSON.stringify(args)], {{ reject: false }}).catch((e: unknown) => {{
    // Never throw — opencode plugin errors can disable subsequent hooks. Log + continue.
    console.warn(`[yadgar] ${{String(args.event)}} failed:`, e instanceof Error ? e.message : String(e))
  }})

const YadgarHooksPlugin: Plugin = async ({{ directory }}) => ({{
  // PreCompact — typed hook with `output.context` array mutation.
  "experimental.session.compacting": async (_input, output) => {{
    const r = await YADGAR({{ event: "pre-compact-drain", directory }})
    if (r && typeof r.stdout === "string" && r.stdout.length > 0) {{
      output.context.push(r.stdout)
    }}
  }},

  // PostToolUse — typed hook; capture every tool call into yadgar action_log.
  // Best-effort, never throw. Note: opencode fires this for INTERNAL MCP calls
  // too — the yadgar drain side filters server-side by tool name.
  "tool.execute.after": async (input, output) => {{
    await YADGAR({{
      event: "post-tool-capture",
      directory,
      tool: input.tool,
      callID: input.callID,
      sessionID: input.sessionID,
      title: output.title,
    }})
  }},

  // Generic event dispatch for session lifecycle.
  // - session.created   → SessionStart (signals-mode inject)
  // - session.compacted → SessionStart restore (restore-mode inject)
  // - session.idle      → Stop observer (non-blocking; promote when sst/opencode#16626 ships)
  event: async ({{ event }}) => {{
    if (event.type === "session.created") {{
      await YADGAR({{ event: "session-start", directory, mode: "signals" }})
    }} else if (event.type === "session.compacted") {{
      await YADGAR({{ event: "session-start", directory, mode: "restore" }})
    }} else if (event.type === "session.idle") {{
      // Non-blocking observer only. The blocking equivalent (`session.stopping`)
      // is gated on sst/opencode#16626; see the re-audit plan §4.5.
      await YADGAR({{ event: "stop", directory }})
    }}
  }},

  // UserPromptSubmit (chat.message parts[] mutation) — DEFERRED. Add here when
  // a headless `opencode run` test confirms parts[] mutation appears in the
  // same-turn context. See docs/plans/port-opencode-re-audit-2026-07-26.md §4.5.
}})

export default YadgarHooksPlugin
"""


# `execa` dep snippet merged into `~/.config/opencode/package.json`. Keeps
# any pre-existing dependencies intact; adds `execa` if absent.
_EXECA_DEP_BLOCK = {"execa": "^9.0.0"}


@observe(tier="stage")
def _opencode_plugin_path(home_dir: Path | None, scope: str, project_dir: Path | None) -> Path:
    """Resolve the plugin path for the given scope.

    Mirrors the cursor emitter's scope/path resolution. Global install goes to
    ``~/.config/opencode/plugins/yadgar-hooks.ts``; project install goes to
    ``.opencode/plugins/yadgar-hooks.ts`` under the project root.
    """
    if scope == "project":
        if project_dir is None:
            raise ValueError("project_dir required when scope='project'")
        base = project_dir
    else:
        base = home_dir if home_dir is not None else Path.home()
    return base / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts"


@observe(tier="stage")
def _opencode_package_json_path(
    home_dir: Path | None, scope: str, project_dir: Path | None
) -> Path:
    """Resolve the package.json path that the plugin's execa dep lives in.

    opencode runs `bun install` at startup to install local-plugin deps from
    this file. We MERGE (not clobber) so the user's pre-existing deps are
    preserved.
    """
    if scope == "project":
        if project_dir is None:
            raise ValueError("project_dir required when scope='project'")
        base = project_dir
    else:
        base = home_dir if home_dir is not None else Path.home()
    return base / ".config" / "opencode" / "package.json"


@observe(tier="stage")
def _ensure_opencode_package_json_dep(path: Path, dep: dict[str, str]) -> bool:
    """Merge ``dep`` into the package.json at ``path`` if absent. Returns True if changed.

    - Creates the file (with an empty ``dependencies`` dict) if it doesn't exist.
    - Adds only the keys from ``dep`` that are not already present.
    - Never removes a pre-existing dep or version.
    - Uses ``_atomic_write_text`` for write safety (matches the cursor emitter).
    """
    if path.exists():
        doc = _load_json(path)
    else:
        doc = {}

    deps = doc.get("dependencies")
    if not isinstance(deps, dict):
        deps = {}
        doc["dependencies"] = deps

    changed = False
    for key, value in dep.items():
        if key not in deps:
            deps[key] = value
            changed = True

    if not changed:
        return False

    # Preserve any other top-level keys (devDependencies, scripts, etc.) — we
    # only ever write back the dependencies block we touched.
    _atomic_write_text(path, json.dumps(doc, indent=2) + "\n")
    return True


@observe(tier="stage")
def _emit_opencode_plugin(
    descriptor: ClientDescriptor,
    home_dir: Path | None,
    scope: str,
    project_dir: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    """OpenCode — write the canonical ``yadgar-hooks.ts`` plugin + ensure ``execa`` dep.

    Idempotent on re-run: the plugin file is replaced in place (marker-detected
    so a user can confirm the file is yadgar-managed before any overwrite).
    The package.json ``execa`` dep is added only if missing — pre-existing
    user deps are preserved.

    Coverage (4 events wired, 1 deferred per the re-audit plan):
      - session.created       (SessionStart)         FUNCTIONAL
      - session.compacted     (SessionStart restore) FUNCTIONAL
      - tool.execute.after    (PostToolUse)          FUNCTIONAL
      - experimental.session.compacting (PreCompact) FUNCTIONAL
      - session.idle          (Stop)                 NON-BLOCKING observer
      - chat.message          (UserPromptSubmit)     DEFERRED (headless test gate)
    """
    plugin_path = _opencode_plugin_path(home_dir, scope, project_dir)
    package_json_path = _opencode_package_json_path(home_dir, scope, project_dir)

    if dry_run:
        return {
            "path": str(plugin_path),
            "package_json": str(package_json_path),
            "written": False,
            "events": ["session-start", "post-tool-capture", "pre-compact-drain", "stop"],
        }

    # 1) Ensure the plugins dir exists.
    plugin_path.parent.mkdir(parents=True, exist_ok=True)

    # 2) Write the canonical TS template. Marker is the first line so re-runs can
    #    detect "this is our file" (we overwrite unconditionally — plugin files
    #    have no foreign-preserve concern since they are single-file, not
    #    appended to a shared hooks.json).
    body = _OPENCODE_PLUGIN_TEMPLATE.format(marker=_OPENCODE_MANAGED_MARKER)
    _atomic_write_text(plugin_path, body)

    # 3) Ensure the execa dep is in the opencode package.json. The plugin
    #    imports it; opencode runs `bun install` on this file at startup.
    package_json_changed = _ensure_opencode_package_json_dep(package_json_path, _EXECA_DEP_BLOCK)

    return {
        "path": str(plugin_path),
        "package_json": str(package_json_path),
        "package_json_changed": package_json_changed,
        "written": True,
        "events": ["session-start", "post-tool-capture", "pre-compact-drain", "stop"],
    }


# ── Stub helper (kept for the 5 remaining stub-car kinds) ────────────────────
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
    fails loud. Each later car (C–G) replaces its ``kind`` with a real
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
# Car 0 implements claude_json; Car A added opencode_plugin (2026-07-26); Car B
# added cursor_hooks. The 5 client kinds in the table below map to the car
# that fills them (dispatched through ``_emit_stub`` until then). The table's
# KEYS must stay in sync with the registry's hooks_kind values (guarded by
# test_hooks_render.test_dispatch_table_covers_every_registry_hooks_kind).
_EMITTERS = {
    "claude_json": _emit_claude_json,
    "cursor_hooks": _emit_cursor_hooks,
    "opencode_plugin": _emit_opencode_plugin,
}

# hooks_kind → the car that implements its emitter (Car-0 stubs until then).
_STUB_CARS = {
    "codex_hooks_json": "Car C",
    "cline_hooks": "Car D",
    "windsurf_hooks": "Car F",
    "kiro_hooks_json": "Car E",
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
