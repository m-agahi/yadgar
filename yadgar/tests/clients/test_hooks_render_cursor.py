"""Car B — Cursor hook emitter (``_emit_cursor_hooks``).

Cursor's hook contract was primary-source re-verified 2026-07-20 (ADR-0143 gate).
The load-bearing finding: Cursor's inject path (``additional_context`` on
``sessionStart`` / ``postToolUse``, and any output from ``beforeSubmitPrompt``)
is DOCUMENTED but currently NON-FUNCTIONAL — accepted/merged by Cursor yet never
surfaced to the model (open upstream bugs, forum threads mid-2026). Cursor's
``stop`` hook is observation-only (``followup_message`` auto-continues, does not
block). So the honest, working subset is the two FIRE-AND-POST hooks that need no
model-surfaced return:

  * ``postToolUse`` → ``yadgar hook post-tool-capture`` (POST /hooks/auto-capture)
  * ``preCompact``  → ``yadgar hook pre-compact-drain`` (POST /hooks/pre-compact)

The emitter writes ``.cursor/hooks.json`` (``{"version": 1, "hooks": {...}}``),
idempotently, preserving any foreign hooks. It does NOT emit sessionStart /
beforeSubmitPrompt (would fake a broken inject — plan R7 forbids faking a hook).
"""

from __future__ import annotations

import json

import pytest

from yadgar.core.install.clients import hooks_render
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

_CURSOR = CLIENT_REGISTRY["cursor"]

# The two Cursor events the emitter wires (fire-and-POST; inject-free).
_WIRED_EVENTS = {"postToolUse", "preCompact"}
# The events deliberately NOT wired (inject broken upstream / observe-only).
_UNWIRED_EVENTS = {"sessionStart", "beforeSubmitPrompt", "stop"}


def _read_hooks_json(home_dir):
    path = home_dir / ".cursor" / "hooks.json"
    assert path.exists(), f"expected {path} to exist"
    return json.loads(path.read_text())


def test_cursor_is_a_real_emitter_not_a_stub(tmp_path):
    """cursor_hooks dispatches to a real emitter (no NotImplementedError)."""
    result = hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    assert result["emitted"] is True
    assert result["hooks_kind"] == "cursor_hooks"


def test_writes_hooks_json_with_version_and_hooks_keys(tmp_path):
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    doc = _read_hooks_json(tmp_path)
    assert doc["version"] == 1
    assert "hooks" in doc and isinstance(doc["hooks"], dict)


def test_wires_only_the_two_working_fire_and_post_events(tmp_path):
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    doc = _read_hooks_json(tmp_path)
    assert set(doc["hooks"]) == _WIRED_EVENTS


def test_does_not_fake_the_broken_inject_events(tmp_path):
    """sessionStart / beforeSubmitPrompt inject is non-functional upstream — never faked."""
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    doc = _read_hooks_json(tmp_path)
    for event in _UNWIRED_EVENTS:
        assert event not in doc["hooks"], f"{event} must not be emitted (broken/observe-only)"


def test_events_shell_out_to_the_shared_yadgar_hook_cli(tmp_path):
    """Each wired event's command invokes ``yadgar hook <event>`` (the shared body)."""
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    doc = _read_hooks_json(tmp_path)

    post = doc["hooks"]["postToolUse"]
    assert isinstance(post, list) and len(post) == 1
    assert "yadgar hook post-tool-capture" in post[0]["command"]
    assert post[0]["type"] == "command"

    pre = doc["hooks"]["preCompact"]
    assert isinstance(pre, list) and len(pre) == 1
    assert "yadgar hook pre-compact-drain" in pre[0]["command"]
    assert pre[0]["type"] == "command"


def test_idempotent_second_run_byte_identical(tmp_path):
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    path = tmp_path / ".cursor" / "hooks.json"
    first = path.read_text()
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    assert path.read_text() == first


def test_preserves_foreign_hooks_and_version(tmp_path):
    """A user's own hooks (and their version) survive the merge; only yadgar events are added."""
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "beforeShellExecution": [{"command": "./audit.sh", "type": "command"}],
                    "postToolUse": [{"command": "./my-own-post.sh", "type": "command"}],
                },
            }
        )
    )
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    doc = _read_hooks_json(tmp_path)

    # Foreign event untouched.
    assert doc["hooks"]["beforeShellExecution"] == [{"command": "./audit.sh", "type": "command"}]
    # The user's own postToolUse entry is preserved AND yadgar's is added (append,
    # not clobber) — Cursor runs every command registered for an event.
    post_cmds = [e["command"] for e in doc["hooks"]["postToolUse"]]
    assert "./my-own-post.sh" in post_cmds
    assert any("yadgar hook post-tool-capture" in c for c in post_cmds)


def test_re_run_does_not_duplicate_yadgar_entry(tmp_path):
    """Re-emitting must not append a second yadgar command to an event array."""
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    doc = _read_hooks_json(tmp_path)
    yadgar_post = [e for e in doc["hooks"]["postToolUse"] if "yadgar hook" in e.get("command", "")]
    assert len(yadgar_post) == 1


def test_project_scope_writes_under_project_dir(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="project", project_dir=project)
    path = project / ".cursor" / "hooks.json"
    assert path.exists()
    doc = json.loads(path.read_text())
    assert set(doc["hooks"]) == _WIRED_EVENTS


def test_dry_run_writes_nothing(tmp_path):
    result = hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global", dry_run=True)
    assert not (tmp_path / ".cursor" / "hooks.json").exists()
    # dry_run still reports what WOULD be emitted (the events).
    assert result["emitted"] is True
    assert set(result["result"]["events"]) == _WIRED_EVENTS


def test_capability_row_reflects_verified_reality():
    """Registry row must be corrected to the 2026-07-20 primary-source reality."""
    from yadgar.core.install.clients.descriptor import StopMechanism

    cap = _CURSOR.hook_capability
    # Inject broken upstream → session-start + prompt-recall are non-functional.
    assert cap.session_start is False
    assert cap.user_prompt_submit is False
    # Fire-and-POST hooks work.
    assert cap.post_tool_use is True
    assert cap.pre_compact is True
    # Cursor's stop is observation-only (NOT blocking) — corrects ADR-0145.
    assert cap.stop is StopMechanism.NONE
    assert cap.verified_date == "2026-07-20"


@pytest.mark.parametrize("event", sorted(_WIRED_EVENTS))
def test_matcher_absent_for_fire_and_post_events(tmp_path, event):
    """No ``matcher`` needed — capture/drain apply to every tool / every compaction."""
    hooks_render.register_hooks(_CURSOR, home_dir=tmp_path, scope="global")
    doc = _read_hooks_json(tmp_path)
    entry = doc["hooks"][event][0]
    assert "matcher" not in entry
