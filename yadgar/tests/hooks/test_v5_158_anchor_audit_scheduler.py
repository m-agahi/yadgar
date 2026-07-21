"""Car #85 — stop-hook maintenance scheduler (checkpoint + anchor-audit).

The single Stop hook now runs an ordered ``MaintenanceItem`` registry.
On each stop (past the loop/transcript guards) it evaluates items by priority
and injects exactly ONE ``{decision: block}`` — FIRST DUE WINS. It advances
ONLY the counter of the item it injected, so a checkpoint that preempts a due
anchor-audit does not consume the audit's turn — the audit fires on the next
eligible stop.

Items:
  priority 0 — checkpoint  (existing behavior; due when count-last_save >= INTERVAL)
  priority 1 — anchor-audit (due when count-last_anchor_audit >= ANCHOR_AUDIT_STOP_INTERVAL)

Also asserts the #87 subagent sweep still runs unconditionally at the top of
main() (must not regress) and the anchor-audit template has the empty-list
no-nag gate language.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys as _sys
from pathlib import Path

import pytest

_HOOK_PATH = Path(__file__).resolve().parents[2] / "core" / "hooks" / "stop-memory-checkpoint.py"


def _load_hook_module(name: str):
    spec = importlib.util.spec_from_file_location(name, str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, payload: dict) -> dict:
    """Drive main() with a JSON stdin payload; return the parsed stdout JSON."""
    buf = io.StringIO()
    old_in, old_out = _sys.stdin, _sys.stdout
    _sys.stdin = io.StringIO(json.dumps(payload))
    _sys.stdout = buf
    try:
        mod.main()
    finally:
        _sys.stdin, _sys.stdout = old_in, old_out
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _write_transcript(tmp_path: Path, n_user_turns: int) -> str:
    """Write a JSONL transcript with n_user_turns human turns."""
    p = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"message": {"role": "user", "content": f"msg {i}"}})
        for i in range(n_user_turns)
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


@pytest.fixture()
def hook(tmp_path, monkeypatch):
    """Load a fresh hook module with an isolated state file and no-op sweep."""
    mod = _load_hook_module("stop_hook_sched_" + tmp_path.name)
    state_file = tmp_path / "stop-hook-state.json"
    monkeypatch.setattr(mod, "_state_file_path", lambda: state_file)
    return mod


class TestSchedulerCadence:
    def test_checkpoint_fires_at_interval(self, hook, tmp_path):
        transcript = _write_transcript(tmp_path, hook.INTERVAL)
        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out.get("decision") == "block"
        assert "Checkpoint due" in out["reason"]
        assert hook._PROMPT_TEMPLATE_PATH in out["reason"]

    def test_anchor_audit_fires_when_only_audit_due(self, hook, tmp_path, monkeypatch):
        """Audit interval passed but within checkpoint interval → audit injected,
        last_save unchanged (cadence isolation)."""
        interval = hook.ANCHOR_AUDIT_STOP_INTERVAL
        # count high enough for audit but the checkpoint was just saved.
        transcript = _write_transcript(tmp_path, interval + hook.INTERVAL)
        state_file = tmp_path / "stop-hook-state.json"
        # last_save is recent (within INTERVAL) so checkpoint is NOT due; audit IS.
        recent_save = (interval + hook.INTERVAL) - (hook.INTERVAL - 1)
        state_file.write_text(
            json.dumps({"s1": {"last_save": recent_save, "last_anchor_audit": 0}}),
            encoding="utf-8",
        )
        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out.get("decision") == "block"
        assert "anchor" in out["reason"].lower() or "audit" in out["reason"].lower()

        saved = json.loads(state_file.read_text())["s1"]
        assert saved["last_save"] == recent_save, "audit injection must NOT advance last_save"
        assert saved["last_anchor_audit"] > 0, "audit injection must advance last_anchor_audit"


class TestSchedulerPreemption:
    def test_checkpoint_preempts_audit_then_audit_fires_next(self, hook, tmp_path):
        """Both due → checkpoint injected (priority 0), last_anchor_audit NOT advanced.
        Next stop → audit injected."""
        interval = hook.ANCHOR_AUDIT_STOP_INTERVAL
        count = interval + hook.INTERVAL  # both checkpoint and audit due
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        state_file.write_text(
            json.dumps({"s1": {"last_save": 0, "last_anchor_audit": 0}}), encoding="utf-8"
        )

        # First stop: checkpoint wins.
        out1 = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out1.get("decision") == "block"
        assert "Checkpoint due" in out1["reason"]
        st = json.loads(state_file.read_text())["s1"]
        assert st["last_save"] == count, "checkpoint advanced last_save"
        assert st["last_anchor_audit"] == 0, "checkpoint must NOT consume the audit's turn"

        # Second stop: same count, checkpoint no longer due → audit fires.
        out2 = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out2.get("decision") == "block"
        assert "anchor" in out2["reason"].lower() or "audit" in out2["reason"].lower()
        st2 = json.loads(state_file.read_text())["s1"]
        assert st2["last_anchor_audit"] == count, "audit now advanced its own counter"

    def test_nothing_due_allows_stop(self, hook, tmp_path):
        transcript = _write_transcript(tmp_path, 3)  # below both intervals
        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out == {}


class TestAnchorAuditTemplate:
    def test_template_has_no_nag_gate(self):
        """The injected anchor-audit prompt must instruct STOP/no-op on an empty
        candidate list — never nag the user when there is nothing to audit."""
        tmpl = (_HOOK_PATH.parent / "templates" / "anchor_audit_prompt.md").read_text(
            encoding="utf-8"
        )
        low = tmpl.lower()
        assert "de_anchor" in low, "template must reference the de_anchor tool"
        # Empty-list no-nag gate language present.
        assert ("no candidate" in low or "empty" in low) and (
            "stop" in low or "no-op" in low or "nothing" in low
        ), "template must gate on an empty candidate list (no-nag)"
