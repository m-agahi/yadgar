"""Car D (#83, ADR-0162) — stop-hook code_graph-refresh cadence.

The priority-2 maintenance slot is owned outright by ``code_graph_refresh``,
gated on ``CODE_GRAPH_ENABLED`` (dir-aware, ADR-0163):

  CODE_GRAPH_ENABLED true  (for this repo) → priority-2 slot fires ``code_graph_refresh``.
  CODE_GRAPH_ENABLED false (for this repo) → nothing fires at priority 2.

The gate lives INSIDE ``_code_graph_refresh_is_due`` via ``config.is_enabled()`` at
RUNTIME (attribute lookup on the imported module, so it stays monkeypatchable).
FIRST-DUE-WINS + only-injected-counter-advances still hold, so a due checkpoint /
anchor-audit PREEMPTS the priority-2 item.

(This slot formerly ran a GATED SWAP against ``repo_wiki_refresh`` — mutually
exclusive, no double-fire. repo_wiki was decommissioned #33/ADR-0162, so
code_graph now owns the slot unconditionally.)

The ``code_graph_refresh_prompt.md`` template is content-linted separately (a lint,
not a byte pin).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys as _sys
from pathlib import Path

import pytest

_HOOK_PATH = Path(__file__).resolve().parents[2] / "core" / "hooks" / "stop-memory-checkpoint.py"
_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "core"
    / "hooks"
    / "templates"
    / "code_graph_refresh_prompt.md"
)


def _load_hook_module(name: str):
    spec = importlib.util.spec_from_file_location(name, str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, payload: dict) -> dict:
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
    p = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"message": {"role": "user", "content": f"msg {i}"}})
        for i in range(n_user_turns)
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


@pytest.fixture()
def hook(tmp_path, monkeypatch):
    mod = _load_hook_module("stop_hook_cg_" + tmp_path.name)
    state_file = tmp_path / "stop-hook-state.json"
    monkeypatch.setattr(mod, "_state_file_path", lambda: state_file)
    return mod


def _set_enabled(monkeypatch, hook, value: bool) -> None:
    """Patch config.is_enabled() on the code_graph config module the hook reads.

    ADR-0163: ``is_enabled`` is now dir-aware (``is_enabled(directory)``), so the
    stub accepts (and ignores) the ``cwd`` the hook threads through.
    """
    from yadgar.core.code_graph import config as cg_config

    monkeypatch.setattr(cg_config, "is_enabled", lambda *a, **k: value)


class TestCodeGraphRefreshRegistration:
    def test_knob_exists_and_is_slowest(self, hook):
        assert hook.CODE_GRAPH_REFRESH_STOP_INTERVAL > hook.ANCHOR_AUDIT_STOP_INTERVAL
        assert hook.CODE_GRAPH_REFRESH_STOP_INTERVAL > hook.INTERVAL

    def test_template_path_resolves(self, hook):
        assert Path(hook._CODE_GRAPH_REFRESH_TEMPLATE_PATH).is_file()

    def test_code_graph_item_registered_priority_2(self, hook):
        item = next(it for it in hook._MAINTENANCE_ITEMS if it["name"] == "code_graph_refresh")
        assert item["priority"] == 2
        assert item["state_key"] == "last_code_graph_refresh"

    def test_repo_wiki_item_not_registered(self, hook):
        """repo_wiki was decommissioned (#33/ADR-0162) — must NOT be registered."""
        names = {it["name"] for it in hook._MAINTENANCE_ITEMS}
        assert "repo_wiki_refresh" not in names
        assert "code_graph_refresh" in names


class TestCodeGraphCadence:
    def _due_state(self, count: int, recent: int) -> dict:
        # checkpoint + anchor-audit just-saved (not due); refresh watermark at 0.
        return {
            "s1": {
                "last_save": recent,
                "last_anchor_audit": recent,
                "last_code_graph_refresh": 0,
            }
        }

    def test_enabled_fires_code_graph(self, hook, tmp_path, monkeypatch):
        """ENABLED → code_graph fires; its counter advances."""
        _set_enabled(monkeypatch, hook, True)
        interval = hook.CODE_GRAPH_REFRESH_STOP_INTERVAL
        count = interval + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        state_file.write_text(json.dumps(self._due_state(count, count - 1)), encoding="utf-8")

        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out.get("decision") == "block"
        assert "code_graph" in out["reason"].lower() or "code-graph" in out["reason"].lower()
        assert hook._CODE_GRAPH_REFRESH_TEMPLATE_PATH in out["reason"]

        saved = json.loads(state_file.read_text())["s1"]
        assert saved["last_code_graph_refresh"] == count

    def test_disabled_nothing_fires_at_priority_2(self, hook, tmp_path, monkeypatch):
        """DISABLED → priority-2 slot is inert (repo_wiki, its former co-tenant, is gone)."""
        _set_enabled(monkeypatch, hook, False)
        interval = hook.CODE_GRAPH_REFRESH_STOP_INTERVAL
        count = interval + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        state_file.write_text(json.dumps(self._due_state(count, count - 1)), encoding="utf-8")

        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out == {}

        saved = json.loads(state_file.read_text())["s1"]
        assert saved["last_code_graph_refresh"] == 0, "code_graph must stay inert when disabled"

    def test_dir_aware_opt_out_not_due(self, hook, tmp_path, monkeypatch):
        """ADR-0163: cwd is threaded into is_due; a per-repo opt-out → NOT due there.

        Enable globally but return False for the specific opted-out cwd — the
        code_graph refresh must not fire for that repo (no wasted nudge), so
        nothing fires at priority 2.
        """
        from yadgar.core.code_graph import config as cg_config

        opted_out = "/repo/opted-out"

        def _is_enabled(directory=None, *a, **k):
            return directory != opted_out  # global on, this dir off

        monkeypatch.setattr(cg_config, "is_enabled", _is_enabled)
        count = hook.CODE_GRAPH_REFRESH_STOP_INTERVAL + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        state_file.write_text(json.dumps(self._due_state(count, count - 1)), encoding="utf-8")

        out = _run_main(
            hook,
            {
                "session_id": "s1",
                "transcript_path": transcript,
                "stop_hook_active": False,
                "cwd": opted_out,
            },
        )
        assert out == {}
        saved = json.loads(state_file.read_text())["s1"]
        assert saved["last_code_graph_refresh"] == 0, "opted-out cwd must not fire code_graph"

    def test_dir_aware_enabled_dir_fires(self, hook, tmp_path, monkeypatch):
        """A cwd that is NOT opted out (dir-enabled) → code_graph refresh fires."""
        from yadgar.core.code_graph import config as cg_config

        monkeypatch.setattr(cg_config, "is_enabled", lambda *a, **k: True)
        count = hook.CODE_GRAPH_REFRESH_STOP_INTERVAL + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        state_file.write_text(json.dumps(self._due_state(count, count - 1)), encoding="utf-8")

        out = _run_main(
            hook,
            {
                "session_id": "s1",
                "transcript_path": transcript,
                "stop_hook_active": False,
                "cwd": "/repo/enabled",
            },
        )
        assert out.get("decision") == "block"
        assert "code_graph" in out["reason"].lower() or "code-graph" in out["reason"].lower()

    def test_is_due_toggles_with_enabled_flag(self, hook, monkeypatch):
        """_code_graph_refresh_is_due tracks CODE_GRAPH_ENABLED directly."""
        count = hook.CODE_GRAPH_REFRESH_STOP_INTERVAL + hook.INTERVAL
        session_state = {
            "last_save": count - 1,
            "last_anchor_audit": count - 1,
            "last_code_graph_refresh": 0,
        }
        _set_enabled(monkeypatch, hook, True)
        assert hook._code_graph_refresh_is_due(count, session_state) is True
        _set_enabled(monkeypatch, hook, False)
        assert hook._code_graph_refresh_is_due(count, session_state) is False


class TestCodeGraphRefreshPreemption:
    def test_checkpoint_preempts_code_graph(self, hook, tmp_path, monkeypatch):
        _set_enabled(monkeypatch, hook, True)
        count = hook.CODE_GRAPH_REFRESH_STOP_INTERVAL + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        state_file.write_text(
            json.dumps(
                {
                    "s1": {
                        "last_save": 0,
                        "last_anchor_audit": 0,
                        "last_code_graph_refresh": 0,
                    }
                }
            ),
            encoding="utf-8",
        )
        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out.get("decision") == "block"
        assert "Checkpoint due" in out["reason"]
        st = json.loads(state_file.read_text())["s1"]
        assert st["last_save"] == count
        assert st["last_code_graph_refresh"] == 0, "checkpoint must not consume code_graph turn"

    def test_nothing_due_allows_stop(self, hook, tmp_path, monkeypatch):
        _set_enabled(monkeypatch, hook, True)
        transcript = _write_transcript(tmp_path, 3)
        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out == {}


class TestCodeGraphRefreshTemplate:
    def test_template_file_exists(self):
        assert _TEMPLATE_PATH.exists(), f"template missing at {_TEMPLATE_PATH}"

    def test_template_has_substitution_header(self):
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "{directory}" in content
        assert "{project}" in content
        assert "Substitute these placeholders" in content

    def test_template_runs_cli_refresh(self):
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "yadgar code-graph refresh" in content

    def test_template_handles_skipped(self):
        """skipped:true → do nothing (a silent no-op)."""
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        normalized = " ".join(content.split()).lower()
        assert "skipped" in content
        assert "no-op" in normalized or "nothing" in normalized

    def test_template_writes_block_create_or_update(self):
        """else → block_update(name=<payload.block_name>); on not-found → block_create.

        The name MUST come from the payload, never a hardcoded "code_graph": a
        monorepo emits one payload per leaf (``code_graph_<subdir>``), so a
        hardcoded name makes every leaf overwrite the same block. The template
        was re-pointed onto ``<payload.block_name>`` by 0047 C10 (51f19805) but
        this assertion still pinned the hardcoded form, so it had been failing
        against the shipped template ever since — it asserted the exact bug the
        template tells the agent to avoid.
        """
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "block_update(" in content
        assert "block_create(" in content
        assert "name=<payload.block_name>" in content
        assert 'never hardcode `"code_graph"`' in content
        assert 'scope="project"' in content
        assert "directory=" in content
