"""Car A — OpenCode hook emitter (``_emit_opencode_plugin``).

OpenCode's hook contract was primary-source re-verified 2026-07-26
(docs/plans/archive/port-opencode-re-audit-2026-07-26.md; supersedes the
2026-07-20 plan). The load-bearing findings:

* OpenCode has NO Claude-Code-style hooks; it has a PLUGINS system
  (JavaScript/TypeScript modules in `~/.config/opencode/plugins/`).
* The typed `Hooks` interface in `@opencode-ai/plugin@1.18.5` exposes
  `tool.execute.after` (typed) + `experimental.session.compacting` (typed)
  + a generic `event` callback that dispatches `EventSessionCreated` /
  `EventSessionCompacted` / `EventSessionIdle` / etc. per the SDK.
* Coverage (4/5 functional, 1/5 dropped, 1/5 deferred) — CONTRACT-FIX
  2026-07-27: the first version of this emitter shipped a broken
  `execa`->CLI contract (flags instead of positional-event+stdin, plus
  invalid event names `session-start`/`stop`) that made every wired event
  exit 2 silently; see the CONTRACT-FIX note in `hooks_render.py`:
    - session.created       → SessionStart          → yadgar hook session-start-context   (FUNCTIONAL)
    - session.compacted     → SessionStart restore   → yadgar hook post-compact-rehydrate  (FUNCTIONAL)
    - tool.execute.after    → PostToolUse            → yadgar hook post-tool-capture       (FUNCTIONAL)
    - experimental.session.compacting → PreCompact   → yadgar hook pre-compact-drain       (FUNCTIONAL)
    - session.idle          → Stop                   NOT WIRED (no yadgar hook event exists yet, task F2)
    - chat.message          → UserPromptSubmit       DEFERRED (headless test, task F1/F3)

The emitter writes `yadgar-hooks.ts` (a thin TS shim) to
`~/.config/opencode/plugins/` (global) or `.opencode/plugins/` (project).
It also ensures the `execa` dep is in `~/.config/opencode/package.json`
(Bun installs it at opencode startup).
"""

from __future__ import annotations

import json
import subprocess
import sys

from yadgar.core.cli import hook as hook_cli
from yadgar.core.install.clients import hooks_render
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

_OPENCODE = CLIENT_REGISTRY["opencode"]

# Events the emitter wires (4; session.idle dropped, chat.message deferred).
_WIRED_EVENTS = {
    "session-start-context",
    "post-compact-rehydrate",
    "post-tool-capture",
    "pre-compact-drain",
}
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


def test_plugin_wires_4_functional_events(tmp_path):
    """The 4 functional events are wired: session.created, session.compacted,
    tool.execute.after, experimental.session.compacting. session.idle is
    deliberately NOT dispatched (no yadgar hook event exists for Stop yet)."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    # sessionStart + restore — via session.created + session.compacted
    assert 'event.type === "session.created"' in body
    assert 'event.type === "session.compacted"' in body
    assert '"session-start-context"' in body
    assert '"post-compact-rehydrate"' in body
    # PostToolUse — typed hook
    assert '"tool.execute.after"' in body
    assert '"post-tool-capture"' in body
    # PreCompact — typed hook
    assert '"experimental.session.compacting"' in body
    assert '"pre-compact-drain"' in body
    # Stop (session.idle) is NOT dispatched — no yadgar hook event exists for
    # it yet (task F2, gated on sst/opencode#16626). chat.message still deferred.
    assert 'event.type === "session.idle"' not in body
    assert '"chat.message"' not in body, (
        "chat.message is gated on a headless opencode run test per the re-audit plan §4.5"
    )


def test_execa_call_uses_positional_event_and_stdin_payload(tmp_path):
    """CONTRACT-FIX regression guard: the emitted execa call must pass the
    event name as a positional CLI argument and the payload via stdin
    (`input:`), never as `--event`/`--directory`/`--json` flags. The original
    flag-based shape made every wired event exit 2 (argparse rejects unknown
    flags) and never delivered a payload — silently swallowed by
    `{ reject: false }`. See the CONTRACT-FIX note in hooks_render.py."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    assert "--event" not in body
    assert "--directory" not in body
    assert "--json" not in body
    assert "input: JSON.stringify(payload)" in body
    assert 'execa("yadgar", ["hook", event, directory]' in body


def test_every_wired_event_is_a_real_hook_cli_event(tmp_path):
    """Mechanical guard: every event name the template emits must be a real
    `yadgar.core.cli.hook._HOOKS` dispatch key. Catches the class of bug where
    the emitter invents an event name (`session-start`, `stop`) that the CLI's
    argparse `choices=` immediately rejects."""
    hooks_render.register_hooks(_OPENCODE, home_dir=tmp_path, scope="global")
    body = _read_plugin(tmp_path)
    for event in _WIRED_EVENTS:
        assert f'"{event}"' in body, f"expected event {event!r} to appear in the emitted plugin"
        assert event in hook_cli._HOOKS, f"{event!r} is not a real yadgar hook CLI event"
    # session-start / stop are NOT valid _HOOKS keys and must never reappear.
    assert "session-start" not in hook_cli._HOOKS
    assert "stop" not in hook_cli._HOOKS


def test_yadgar_hook_cli_accepts_the_emitted_argv_shape(tmp_path):
    """End-to-end regression guard for the exact bug this PR review found:
    run the REAL installed `yadgar hook <event> <directory>` CLI with a
    stdin payload shaped exactly like the plugin's `YADGAR()` helper sends,
    and assert it does NOT exit 2 (argparse rejection). This is the check
    the original PR's test suite lacked entirely — every prior test only
    string-matched the emitted TypeScript source, so a 100%-non-functional
    emitter (verified: all 4 events exited 2 against the real CLI) still
    passed the full suite. Runs the real subprocess; each handler
    gracefully degrades to a no-op when the yadgar daemon isn't reachable
    (best-effort HTTP calls swallow connection errors), so this is safe to
    run without a live daemon and still proves the CLI CONTRACT is honored.
    """
    directory = str(tmp_path)
    payloads = {
        "session-start-context": {"cwd": directory, "source": "startup"},
        "post-compact-rehydrate": {"cwd": directory},
        "post-tool-capture": {
            "cwd": directory,
            "tool_name": "Bash",
            "session_id": "test-session",
            "tool_input": {"command": "ls"},
        },
        "pre-compact-drain": {"cwd": directory},
    }
    assert set(payloads) == _WIRED_EVENTS, "payload fixture must cover every wired event"
    for event, payload in payloads.items():
        proc = subprocess.run(
            [sys.executable, "-m", "yadgar", "hook", event, directory],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, (
            f"yadgar hook {event!r} exited {proc.returncode} (expected 0) — "
            f"stderr: {proc.stderr!r}. This is the exact argparse-rejection "
            f"failure mode the emitter's original --event/--directory/--json "
            f"flag contract produced."
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
