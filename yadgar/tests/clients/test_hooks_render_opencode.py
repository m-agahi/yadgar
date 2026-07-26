"""Car A — OpenCode hook emitter (``_emit_opencode_plugin``).

OpenCode's hook contract was primary-source re-verified 2026-07-26
(docs/plans/port-opencode-re-audit-2026-07-26.md; supersedes the 2026-07-20
plan). The load-bearing findings:

* OpenCode has NO Claude-Code-style hooks; it has a PLUGINS system
  (JavaScript/TypeScript modules in `~/.config/opencode/plugins/`).
* The typed `Hooks` interface in `@opencode-ai/plugin@1.18.5` exposes
  `tool.execute.after` (typed) + `experimental.session.compacting` (typed)
  + a generic `event` callback that dispatches `EventSessionCreated` /
  `EventSessionCompacted` / `EventSessionIdle` / etc. per the SDK.
* Coverage (3/5 functional, 1/5 non-blocking, 1/5 deferred):
    - session.created       → SessionStart          (FUNCTIONAL)
    - session.compacted     → SessionStart restore  (FUNCTIONAL)
    - tool.execute.after    → PostToolUse           (FUNCTIONAL)
    - experimental.session.compacting → PreCompact  (FUNCTIONAL)
    - session.idle          → Stop                  (NON-BLOCKING observer)
    - chat.message          → UserPromptSubmit      (DEFERRED, headless test)

The emitter writes `yadgar-hooks.ts` (a thin TS shim) to
`~/.config/opencode/plugins/` (global) or `.opencode/plugins/` (project).
It also ensures the `execa` dep is in `~/.config/opencode/package.json`
(Bun installs it at opencode startup).
"""

from __future__ import annotations

import json

from yadgar.core.install.clients import hooks_render
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

_OPENCODE = CLIENT_REGISTRY["opencode"]

# Events the emitter wires (4 of 5; chat.message deferred to a headless test).
_WIRED_EVENTS = {"session-start", "post-tool-capture", "pre-compact-drain", "stop"}
# The event deliberately NOT wired (gated on a headless `opencode run` test).
_UNWIRED_EVENT = "user-prompt-submit"


def _read_plugin(home_dir):
    path = home_dir / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts"
    assert path.exists(), f"expected {path} to exist"
    return path.read_text()


def _read_package_json(home_dir):
    path = home_dir / ".config" / "opencode" / "package.json"
    return json.loads(path.read_text())


def test_opencode_is_a_real_emitter_not_a_stub(tmp_path):
    """opencode_plugin dispatches to a real emitter (no NotImplementedError)."""
    result = hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    assert result["emitted"] is True
    assert result["hooks_kind"] == "opencode_plugin"


def test_writes_plugin_file_under_global_config(tmp_path):
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    path = tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts"
    assert path.exists()


def test_plugin_file_has_yadgar_managed_marker(tmp_path):
    """Marker comment on the first line so re-runs can confirm 'this is our file'."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    assert body.startswith(hooks_render._OPENCODE_MANAGED_MARKER)


def test_plugin_imports_from_opencode_plugin_sdk(tmp_path):
    """The template subscribes via the @opencode-ai/plugin typed Hooks interface."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    assert 'from "@opencode-ai/plugin"' in body
    assert 'import { execa } from "execa"' in body or 'import {execa} from "execa"' in body


def test_plugin_wires_4_of_5_working_events(tmp_path):
    """The 4 functional events are wired: session.created, session.compacted,
    tool.execute.after, experimental.session.compacting."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    # sessionStart (signals + restore modes) — both come through session.created + session.compacted
    assert 'event.type === "session.created"' in body
    assert 'event.type === "session.compacted"' in body
    # PostToolUse — typed hook
    assert '"tool.execute.after"' in body
    assert '"post-tool-capture"' in body
    # PreCompact — typed hook
    assert '"experimental.session.compacting"' in body
    assert '"pre-compact-drain"' in body
    # Stop — non-blocking observer (chat.message still NOT in template)
    assert 'event.type === "session.idle"' in body
    assert '"chat.message"' not in body, (
        "chat.message is gated on a headless opencode run test per the re-audit plan §4.5"
    )


def test_plugin_does_not_fake_broken_inject(tmp_path):
    """`tui.prompt.append` is TUI-internal (wrong tool for user-prompt inject) — never used."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    assert "tui.prompt.append" not in body
    # The wrong-for-this-purpose system.transform is also not in the template
    # (it injects into system prompt, not session lifecycle; #34321 makes it
    # buggy on OpenAI-compat providers).
    assert "system.transform" not in body


def test_idempotent_second_run_byte_identical(tmp_path):
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    first = _read_plugin(tmp_path)
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    assert _read_plugin(tmp_path) == first


def test_dry_run_writes_nothing(tmp_path):
    result = hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global", dry_run=True)
    assert not (tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts").exists()
    assert not (tmp_path / ".config" / "opencode" / "package.json").exists()
    # dry_run still reports the events the emitter WOULD wire.
    assert result["emitted"] is True
    assert set(result["result"]["events"]) == _WIRED_EVENTS


def test_ensure_opencode_package_json_dep_creates_file(tmp_path):
    """If package.json doesn't exist, emitter creates it with execa + @opencode-ai/plugin deps.

    F7 (2026-07-26 followup): the @opencode-ai/plugin dep is DOCUMENTARY —
    the plugin template uses a type-only import that gets erased at
    strip-types, so there's no runtime dep. Adding it to package.json
    makes the contract explicit: anyone reading package.json sees the
    dep, even though it's resolved at type-check time only.
    """
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    pkg = _read_package_json(tmp_path)
    assert "execa" in pkg["dependencies"]
    assert "@opencode-ai/plugin" in pkg["dependencies"]


def test_ensure_opencode_package_json_dep_preserves_existing(tmp_path):
    """If package.json already has deps, emitter does NOT clobber them."""
    pkg_path = tmp_path / ".config" / "opencode"
    pkg_path.mkdir(parents=True)
    (pkg_path / "package.json").write_text(
        json.dumps(
            {
                "name": "opencode",
                "dependencies": {"some-user-dep": "^1.0.0"},
                "scripts": {"dev": "echo dev"},
            }
        )
    )
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    pkg = _read_package_json(tmp_path)
    # Existing dep preserved.
    assert pkg["dependencies"]["some-user-dep"] == "^1.0.0"
    # Yadgar deps added.
    assert pkg["dependencies"]["execa"] == "^9.0.0"
    assert pkg["dependencies"]["@opencode-ai/plugin"] == "^1.0.0"
    # Other top-level keys (scripts, name) preserved.
    assert pkg["name"] == "opencode"
    assert pkg["scripts"]["dev"] == "echo dev"


def test_ensure_opencode_package_json_dep_idempotent(tmp_path):
    """Re-running does not change the file when execa is already present."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    first = (tmp_path / ".config" / "opencode" / "package.json").read_text()
    result2 = hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    second = (tmp_path / ".config" / "opencode" / "package.json").read_text()
    assert first == second
    assert result2["result"]["package_json_changed"] is False


def test_project_scope_writes_under_project_dir(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="project", project_dir=project)
    path = project / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts"
    assert path.exists()
    pkg_path = project / ".config" / "opencode" / "package.json"
    assert pkg_path.exists()


def test_opencode_plugin_uses_execa_not_mcp_rpc(tmp_path):
    """The plugin MUST use execa shell-out to `yadgar`, NOT a fabricated
    ctx.client MCP RPC. Per the re-audit §1.1, ctx.client is opencode's own
    typed SDK (no generic MCP invoker)."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    assert "execa" in body
    assert '"yadgar"' in body
    # No ctx.client.app.<MCP_NAMESPACE>.call pattern (the fabricated pattern from
    # the botched 2026-07-26 dispatch prompt).
    assert "ctx.client.app" not in body
    assert "MCP_NAMESPACE" not in body


def test_opencode_capability_row_reflects_re_audit():
    """Registry row session_start is True (FUNCTIONAL via event callback), stop
    is NONE (no blocking), per the 2026-07-26 re-audit.

    Note: the ``verified_date`` field is bumped in CAR 4 (alongside the
    CAPABILITY_REGISTRY.md I32 update) — not asserted here to keep this
    test passing on the unchanged registry row.
    """
    from yadgar.core.install.clients.descriptor import StopMechanism

    cap = _OPENCODE.hook_capability
    # The 4 functional events
    assert cap.session_start is True
    assert cap.post_tool_use is True
    assert cap.pre_compact is True
    # Stop remains NONE (no blocking surface) per the verified plan
    assert cap.stop is StopMechanism.NONE
    # userPromptSubmit is True in the registry (the surface exists) but
    # NOT wired in the emitter (gated on headless test)
    assert cap.user_prompt_submit is True
    # F6 (2026-07-26 followup): per-row override of verified_date. The
    # shared _VERIFIED constant (2026-07-18) covers the other 8 clients;
    # the opencode row was re-verified during the re-audit
    # (docs/plans/port-opencode-re-audit-2026-07-26.md, 2026-07-26) so
    # it gets its own date. Bumping the shared constant would falsely
    # re-stamp 8 unrelated rows.
    assert cap.verified_date == "2026-07-26"
