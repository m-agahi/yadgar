"""Car A (smoke half) — light syntax + structure check for the emitted opencode plugin.

The re-audit plan §4.5/§4.6 calls for a headless ``opencode run`` test as
the gate for chat.message parts[] mutation. That test requires Bun + the
opencode runtime, neither of which are available in this train's env
(no Bun installed; opencode is the consumer of the plugin, not the
producer).

For the 3 functional events (sessionStart, postToolUse, preCompact)
the unit tests in ``test_hooks_render_opencode.py`` already cover the
structural contract: marker, event names, execa invocation, idempotency,
package.json merge, scope/project, dry_run. The deeper value of a
"smoke" — confirming the emitted TS file is syntactically valid and
the template's event handlers are wired to the right OpenCode plugin
events — comes from actually loading the file in a Node runtime that
can strip types. Node 24 has that built in.

This test:
  1. Emits the opencode plugin via the real emitter to a tmp path.
  2. Loads the emitted file through Node 24's ``--experimental-strip-types``
     mode via a small driver script (``_smoke/opencode_plugin_smoke.ts``).
  3. The driver reports a JSON shape covering: has the right handlers,
     dispatches the 3 lifecycle types, uses execa (not fabricated MCP
     RPC), has the yadgar-managed marker, has a default export, does
     NOT include chat.message / tui.prompt.append / system.transform.
  4. The Python test asserts on the JSON.

This catches template drift that the unit tests don't: if a future
edit to ``_OPENCODE_PLUGIN_TEMPLATE`` accidentally removes an event
handler or introduces a runtime import of @opencode-ai/plugin, this
test fires.

It does NOT catch: runtime behavior of opencode actually firing the
handlers. That gate is a real headless test, deferred to a follow-up
train per the plan.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from yadgar.core.install.clients import hooks_render
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

_OPENCODE = CLIENT_REGISTRY["opencode"]
_SMOKE_DIR = Path(__file__).parent / "_smoke"
_DRIVER = _SMOKE_DIR / "opencode_plugin_smoke.ts"


def _node_available() -> bool:
    """Skip the test if Node 24+ is not available in this env."""
    return shutil.which("node") is not None


def _emit_plugin(tmp_path: Path) -> Path:
    """Emit the opencode plugin to ``tmp_path/.config/opencode/plugins/yadgar-hooks.ts``."""
    result = hooks_render.register_hooks(
        _OPENCODE, home_dir=tmp_path, scope="global", dry_run=False
    )
    inner = result["result"]
    plugin_path = Path(inner["path"])
    assert plugin_path.exists(), f"emitter did not write the plugin: {plugin_path}"
    return plugin_path


def _run_driver(plugin_path: Path) -> dict:
    """Spawn the Node driver against the plugin file and parse its JSON report."""
    proc = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--no-warnings",
            str(_DRIVER),
            str(plugin_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"smoke driver failed (rc={proc.returncode}):\n"
            f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
    # Driver writes a single JSON line on stdout.
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_has_all_required_typed_handlers(tmp_path):
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["hasAllRequiredHandlers"], (
        f"plugin is missing a required typed handler; lineCount={report['lineCount']}"
    )


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_dispatches_all_three_lifecycle_events(tmp_path):
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["hasAllLifecycleDispatches"], (
        "plugin's event callback must dispatch on session.created, session.compacted, "
        "and session.idle per the re-audit plan §3.1"
    )


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_uses_execa_not_fabricated_mcp_rpc(tmp_path):
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["usesExeca"], "plugin must import execa to shell out to yadgar hook <event>"
    assert report["doesNotFakeMcpRpc"], (
        "plugin must NOT use ctx.client.app[...].call(...) — that API doesn't exist "
        "in @opencode-ai/plugin@1.18.5's PluginInput (client is opencode's own SDK)"
    )


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_has_default_export(tmp_path):
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["hasDefaultExport"], (
        "plugin must export default YadgarHooksPlugin for opencode to load"
    )


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_uses_output_context_push_for_precompact(tmp_path):
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["usesContextPush"], (
        "preCompact handler must use output.context.push() to append drain output; "
        "clobbering output.prompt would lose opencode's framing"
    )


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_does_not_wire_chat_message(tmp_path):
    """chat.message is gated on a real headless test per the re-audit plan §4.5."""
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["hasNoChatMessage"], "chat.message is intentionally NOT in the template yet"


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_does_not_fake_tui_or_system_transform(tmp_path):
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["hasNoFakeInject"], (
        "tui.prompt.append is TUI-internal (wrong tool); system.transform is buggy on "
        "OpenAI-compat providers per #34321 — neither belongs in the template"
    )


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_marker_on_first_line(tmp_path):
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert report["markerOnFirstLine"], (
        "@yadgar-managed marker must be the first line so re-runs can detect 'this is our file'"
    )


@pytest.mark.skipif(
    not _node_available(),
    reason="node not in PATH (opencode plugin smoke; loads the emitted yadgar-hooks.ts via Node 24 --experimental-strip-types)",
)
def test_emitted_plugin_does_not_emit_runtime_plugin_import(tmp_path):
    """The @opencode-ai/plugin import is type-only — strip-types erases it.

    A regression that adds a runtime import (e.g. to call ctx.client)
    would silently break load under Node (and add a hard dependency on
    the package being installed). This test catches that drift.
    """
    plugin = _emit_plugin(tmp_path)
    report = _run_driver(plugin)
    assert not report["hasRuntimePluginImport"], (
        "plugin must not runtime-import @opencode-ai/plugin; type-only imports are "
        "erased at strip-time. A runtime import would add a hard dep + break in "
        "environments without the package installed."
    )
