"""Ledger 1791 — the stop-hook checkpoint for an external-ledger project.

A project whose project_id starts with a prefix in the global runtime config key
``tasks.external_ledger_prefixes`` keeps its tasks in yaadgaar. Its checkpoint
pointer names a VARIANT template with no task-list mirror step (base step 5),
the later steps renumbered. Every other project gets the base template and the
exact reason string it got before the gate.

The hook resolves the project from the session map the SessionStart hook wrote
(``lookup_project_for_directory(cwd)`` — no derivation, ADR-0227) and reads the
key through the fail-open host client. Both are stubbed here: these tests never
reach the live daemon or the real session map.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
_HOOK_PATH = _REPO / "core" / "hooks" / "stop-memory-checkpoint.py"
_TEMPLATES = _REPO / "core" / "hooks" / "templates"
_BASE = _TEMPLATES / "stop_checkpoint_prompt.md"
_VARIANT = _TEMPLATES / "stop_checkpoint_prompt_external_ledger.md"

_KEY = "tasks.external_ledger_prefixes"
_TASKS_NOTE = (
    "TASKS: this project's tasks live in yaadgaar (mcp__yaadgaar__*), not in the\n"
    "yadgar task ledger, so this protocol has no task-list mirror step and you do\n"
    "not call task_write. Track tasks with the yaadgaar tools instead.\n"
    "\n"
)


def _external_ledger_variant(base: str) -> str:
    """The variant, derived from the base: the drift guard for the shipped file.

    Drop step 5 whole (5a-5d), renumber 6→5 and 7→6, fix every cross-reference
    to those numbers, and add one note naming the active ledger. A base edit
    that this transform cannot carry fails ``test_variant_is_the_base_minus_step_5``.
    """
    start = base.index("5. TASK-LIST MIRROR")
    end = base.index("6. Call project_brief")
    text = base[:start] + base[end:]
    text = text.replace(
        "(steps 1-5), THEN maintenance (steps 6-7)", "(steps 1-4), THEN maintenance (steps 5-6)"
    )
    text = text.replace("run ALL seven steps", "run ALL six steps")
    text = text.replace("skip steps 6-7", "skip steps 5-6")
    text = re.sub(r"\bstep 6\b", "step 5", text)
    text = re.sub(r"\bstep 7\b", "step 6", text)
    text = re.sub(r"^6\. Call project_brief", "5. Call project_brief", text, flags=re.M)
    text = re.sub(r"^7\. MAINTENANCE", "6. MAINTENANCE", text, flags=re.M)
    marker = 'WHEN A STEP SAYS "READ"'
    return text.replace(marker, _TASKS_NOTE + marker, 1)


def _load_module():
    spec = importlib.util.spec_from_file_location("stop_memory_checkpoint_1791", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook(monkeypatch):
    """The hook module with the session map and the host config client stubbed."""
    import yadgar._shared.runtime.session_map as session_map
    import yadgar.core.runtime_config_client as rc_client

    state: dict = {"map": {}, "value": None, "lookups": [], "reads": []}

    def _lookup(directory):
        state["lookups"].append(directory)
        return state["map"].get(directory)

    def _get(key, directory=None, default=None):
        state["reads"].append((key, directory, default))
        if key == _KEY and state["value"] is not None:
            return state["value"]
        return default

    monkeypatch.setattr(session_map, "lookup_project_for_directory", _lookup)
    monkeypatch.setattr(rc_client, "get", _get)
    mod = _load_module()
    mod.state = state
    return mod


# ── the variant template ─────────────────────────────────────────────────────


def test_variant_is_the_base_minus_step_5() -> None:
    expected = _external_ledger_variant(_BASE.read_text(encoding="utf-8"))
    assert _VARIANT.read_text(encoding="utf-8") == expected, (
        "stop_checkpoint_prompt_external_ledger.md drifted from the base template; "
        "regenerate it with _external_ledger_variant()"
    )


def test_variant_has_no_task_ledger_mirror() -> None:
    text = _VARIANT.read_text(encoding="utf-8")
    for gone in ("TASK-LIST MIRROR", "task_write(", "task_list(", "TaskList", "Step 5a"):
        assert gone not in text, gone
    steps = re.findall(r"^(\d)\. ", text, flags=re.M)
    assert steps == ["1", "2", "3", "4", "5", "6"]
    assert "(steps 1-4), THEN maintenance (steps 5-6)" in text
    assert "step 7" not in text and "seven" not in text


def test_hook_resolves_the_variant_as_package_data(hook) -> None:
    assert Path(hook._EXTERNAL_LEDGER_TEMPLATE_PATH).is_file()


# ── the pointer reason ───────────────────────────────────────────────────────


def test_external_project_gets_the_variant(hook) -> None:
    hook.state["map"]["/w/docs"] = "yadgarhq/docs"
    hook.state["value"] = ["yadgarhq/"]

    reason = hook._checkpoint_reason(25, "/w/docs")

    assert reason == (
        f"[yadgar] Checkpoint due. Read {hook._EXTERNAL_LEDGER_TEMPLATE_PATH}"
        " and follow all the instructions in it."
    )
    assert hook.state["reads"] == [(_KEY, None, [])]


@pytest.mark.parametrize("cwd", ["/w/aws", "/w/unmapped", None])
def test_other_projects_get_the_identical_base_reason(hook, cwd) -> None:
    hook.state["map"]["/w/aws"] = "quinyx/aws-work"

    hook.state["value"] = None
    unset = hook._checkpoint_reason(25, cwd)
    hook.state["value"] = ["yadgarhq/"]
    gated = hook._checkpoint_reason(25, cwd)

    assert gated == unset == hook._checkpoint_reason(25)
    assert hook._PROMPT_TEMPLATE_PATH in gated


def test_no_project_means_no_config_read(hook) -> None:
    hook.state["value"] = ["yadgarhq/"]
    hook._checkpoint_reason(25, "/w/unmapped")
    hook._checkpoint_reason(25, None)
    assert hook.state["reads"] == []


def test_main_threads_cwd_into_the_checkpoint_reason(hook, monkeypatch, tmp_path) -> None:
    hook.state["map"]["/w/docs"] = "yadgarhq/docs"
    hook.state["value"] = ["yadgarhq/"]
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(json.dumps({"role": "user", "content": f"m{i}"}) for i in range(25))
    )
    monkeypatch.setattr(hook, "_load_state", lambda: {})
    monkeypatch.setattr(hook, "_save_state", lambda state: None)
    payload = {"session_id": "s1", "transcript_path": str(transcript), "cwd": "/w/docs"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    hook.main()

    body = json.loads(out.getvalue())
    assert body["decision"] == "block"
    assert hook._EXTERNAL_LEDGER_TEMPLATE_PATH in body["reason"]
