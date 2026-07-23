"""Car D (#83, ADR-0162) — stop-hook code_graph-refresh gated cadence swap.

The priority-2 maintenance slot is a GATED swap (transition, not hard cutover):

  CODE_GRAPH_ENABLED true  → priority-2 slot runs ``code_graph_refresh`` INSTEAD OF
                             ``repo_wiki_refresh`` (mutually exclusive, no double-fire).
  CODE_GRAPH_ENABLED false → ``repo_wiki_refresh`` runs as today; code_graph inert.

Both items stay registered in ``_MAINTENANCE_ITEMS`` (repo_wiki NOT deleted —
decommission is task #33). The gate lives INSIDE each is_due function via
``config.is_enabled()`` at RUNTIME (attribute lookup on the imported module, so it
stays monkeypatchable). FIRST-DUE-WINS + only-injected-counter-advances still hold,
so a due checkpoint / anchor-audit PREEMPTS the priority-2 item.

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

    def test_repo_wiki_item_still_registered(self, hook):
        """Decommission is #33 — repo_wiki item MUST stay registered."""
        names = {it["name"] for it in hook._MAINTENANCE_ITEMS}
        assert "repo_wiki_refresh" in names
        assert "code_graph_refresh" in names


class TestGatedCadenceSwap:
    def _due_state(self, count: int, recent: int) -> dict:
        # checkpoint + anchor-audit just-saved (not due); both refresh watermarks at 0.
        return {
            "s1": {
                "last_save": recent,
                "last_anchor_audit": recent,
                "last_repo_wiki_refresh": 0,
                "last_code_graph_refresh": 0,
            }
        }

    def test_enabled_fires_code_graph_not_repo_wiki(self, hook, tmp_path, monkeypatch):
        """ENABLED → code_graph fires, repo_wiki inert. Only code_graph counter advances."""
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
        assert "repo-wiki" not in out["reason"].lower()

        saved = json.loads(state_file.read_text())["s1"]
        assert saved["last_code_graph_refresh"] == count
        assert saved["last_repo_wiki_refresh"] == 0, "repo_wiki must stay inert when enabled"

    def test_disabled_fires_repo_wiki_not_code_graph(self, hook, tmp_path, monkeypatch):
        """DISABLED → repo_wiki fires (as today), code_graph inert."""
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
        assert out.get("decision") == "block"
        assert "repo-wiki" in out["reason"].lower()
        assert hook._REPO_WIKI_REFRESH_TEMPLATE_PATH in out["reason"]

        saved = json.loads(state_file.read_text())["s1"]
        assert saved["last_repo_wiki_refresh"] == count
        assert saved["last_code_graph_refresh"] == 0, "code_graph must stay inert when disabled"

    def test_dir_aware_opt_out_not_due(self, hook, tmp_path, monkeypatch):
        """ADR-0163: cwd is threaded into is_due; a per-repo opt-out → NOT due there.

        Enable globally but return False for the specific opted-out cwd — the
        code_graph refresh must not fire for that repo (no wasted nudge), and since
        code_graph owns the priority-2 slot when globally enabled, repo_wiki stays
        inert too (nothing due at priority 2).
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
        # code_graph is dir-off here → not due; repo_wiki yields to code_graph
        # (globally enabled) → nothing at priority 2 fires.
        assert (
            out == {}
            or out.get("decision") != "block"
            or "code" not in out.get("reason", "").lower()
        )
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

    def test_no_double_fire_priority_2(self, hook, tmp_path, monkeypatch):
        """Exactly one priority-2 item may be due at a time (mutual exclusion)."""
        _set_enabled(monkeypatch, hook, True)
        count = hook.CODE_GRAPH_REFRESH_STOP_INTERVAL + hook.INTERVAL
        session_state = {
            "last_save": count - 1,
            "last_anchor_audit": count - 1,
            "last_repo_wiki_refresh": 0,
            "last_code_graph_refresh": 0,
        }
        cg_due = hook._code_graph_refresh_is_due(count, session_state)
        rw_due = hook._repo_wiki_refresh_is_due(count, session_state)
        assert cg_due is True
        assert rw_due is False
        # Flip the flag: repo_wiki due, code_graph not.
        _set_enabled(monkeypatch, hook, False)
        assert hook._code_graph_refresh_is_due(count, session_state) is False
        assert hook._repo_wiki_refresh_is_due(count, session_state) is True


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
                        "last_repo_wiki_refresh": 0,
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
        """else → block_update(name=code_graph); on not-found → block_create."""
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "block_update(" in content
        assert "block_create(" in content
        assert 'name="code_graph"' in content
        assert 'scope="project"' in content
        assert "directory=" in content
