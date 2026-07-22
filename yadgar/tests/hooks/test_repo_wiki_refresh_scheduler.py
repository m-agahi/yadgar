"""Car D (#83) — stop-hook repo-wiki-refresh maintenance cadence item.

Adds a third ``MaintenanceItem`` (priority 2) to the stop-hook scheduler:
  priority 0 — checkpoint    (INTERVAL=25)
  priority 1 — anchor-audit  (ANCHOR_AUDIT_STOP_INTERVAL=100)
  priority 2 — repo-wiki-refresh (REPO_WIKI_REFRESH_STOP_INTERVAL~200)

FIRST DUE WINS + only-the-injected-item's-counter-advances still hold, so a due
checkpoint or anchor-audit PREEMPTS the (slower) repo-wiki-refresh, which then
fires on the next eligible stop without having consumed its turn.

The template test content-lints ``repo_wiki_refresh_prompt.md`` for the three
state branches (existence-check / ENABLED-refresh / opt-out ASK) — a content
lint, not a byte pin (the prompt is tweakable; only the litigated checkpoint
template earns a byte pin per ADR-0122).
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
    / "repo_wiki_refresh_prompt.md"
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
    mod = _load_hook_module("stop_hook_rwr_" + tmp_path.name)
    state_file = tmp_path / "stop-hook-state.json"
    monkeypatch.setattr(mod, "_state_file_path", lambda: state_file)
    return mod


class TestRepoWikiRefreshCadence:
    def test_knob_exists_and_is_slowest(self, hook):
        """The repo-wiki-refresh cadence knob exists and is slower than the
        checkpoint (25) and anchor-audit (~100) intervals."""
        assert hook.REPO_WIKI_REFRESH_STOP_INTERVAL > hook.ANCHOR_AUDIT_STOP_INTERVAL
        assert hook.REPO_WIKI_REFRESH_STOP_INTERVAL > hook.INTERVAL

    def test_template_path_resolves(self, hook):
        assert Path(hook._REPO_WIKI_REFRESH_TEMPLATE_PATH).is_file()

    def test_repo_wiki_item_registered_priority_2(self, hook):
        item = next(it for it in hook._MAINTENANCE_ITEMS if it["name"] == "repo_wiki_refresh")
        assert item["priority"] == 2
        assert item["state_key"] == "last_repo_wiki_refresh"

    def test_fires_when_only_repo_wiki_due(self, hook, tmp_path):
        """Checkpoint + anchor-audit both just-saved, repo-wiki interval passed →
        repo-wiki-refresh injected; its counter advances, the others' do not."""
        rwr = hook.REPO_WIKI_REFRESH_STOP_INTERVAL
        count = rwr + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        # last_save + last_anchor_audit both recent (their intervals NOT elapsed);
        # last_repo_wiki_refresh at 0 so only repo-wiki is due.
        recent = count - 1
        state_file.write_text(
            json.dumps(
                {
                    "s1": {
                        "last_save": recent,
                        "last_anchor_audit": recent,
                        "last_repo_wiki_refresh": 0,
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
        assert "repo-wiki" in out["reason"].lower()
        assert hook._REPO_WIKI_REFRESH_TEMPLATE_PATH in out["reason"]

        saved = json.loads(state_file.read_text())["s1"]
        assert saved["last_save"] == recent, "repo-wiki inject must NOT advance last_save"
        assert saved["last_anchor_audit"] == recent, (
            "repo-wiki inject must NOT advance last_anchor_audit"
        )
        assert saved["last_repo_wiki_refresh"] == count, (
            "repo-wiki inject must advance its own counter"
        )


class TestRepoWikiRefreshPreemption:
    def test_checkpoint_preempts_repo_wiki(self, hook, tmp_path):
        """All three due → checkpoint wins (priority 0); last_repo_wiki_refresh NOT
        advanced (its turn is not consumed)."""
        count = hook.REPO_WIKI_REFRESH_STOP_INTERVAL + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        state_file.write_text(
            json.dumps(
                {"s1": {"last_save": 0, "last_anchor_audit": 0, "last_repo_wiki_refresh": 0}}
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
        assert st["last_anchor_audit"] == 0, "checkpoint must not consume audit turn"
        assert st["last_repo_wiki_refresh"] == 0, "checkpoint must not consume repo-wiki turn"

    def test_anchor_audit_preempts_repo_wiki(self, hook, tmp_path):
        """Audit + repo-wiki due, checkpoint just-saved → audit wins (priority 1);
        last_repo_wiki_refresh NOT advanced."""
        count = hook.REPO_WIKI_REFRESH_STOP_INTERVAL + hook.INTERVAL
        transcript = _write_transcript(tmp_path, count)
        state_file = tmp_path / "stop-hook-state.json"
        recent = count - 1  # checkpoint not due
        state_file.write_text(
            json.dumps(
                {
                    "s1": {
                        "last_save": recent,
                        "last_anchor_audit": 0,
                        "last_repo_wiki_refresh": 0,
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
        assert "anchor" in out["reason"].lower() or "audit" in out["reason"].lower()
        st = json.loads(state_file.read_text())["s1"]
        assert st["last_anchor_audit"] == count
        assert st["last_repo_wiki_refresh"] == 0, "audit must not consume repo-wiki turn"

    def test_nothing_due_allows_stop(self, hook, tmp_path):
        transcript = _write_transcript(tmp_path, 3)  # below all three intervals
        out = _run_main(
            hook,
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": False},
        )
        assert out == {}


class TestRepoWikiRefreshTemplate:
    def test_template_file_exists(self):
        assert _TEMPLATE_PATH.exists(), f"template missing at {_TEMPLATE_PATH}"

    def test_template_has_substitution_header(self):
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "{directory}" in content
        assert "{project}" in content
        assert "Substitute these placeholders" in content

    def test_template_has_existence_check_branch(self):
        """The prompt checks for the TOC page to decide enabled-vs-unset."""
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "{project}-repo-wiki-index" in content
        assert "wiki_read(" in content

    def test_template_has_enabled_refresh_branch(self):
        """ENABLED → run the host CLI stale-diff + write drifted pages back.

        Car D (#83): prompt uses single upsert form (upsert=True + slug=) instead
        of the old EXISTING/NEW branching (replace_slug= / force=True).
        """
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        normalized = " ".join(content.split())
        assert "--stale-only" in content
        assert "--stored-hashes" in content
        assert "wiki_list(" in content
        # Car D: single upsert form — no replace_slug / force=True branching
        assert "upsert=True" in content
        assert "slug=<page.slug>" in content or "slug=" in content
        assert "replace_slug" not in content, (
            "Car D prompt must not use replace_slug — use upsert=True instead"
        )
        assert "force=True" not in content, (
            "Car D prompt must not use force=True — use upsert=True instead"
        )
        assert "wiki_delete(" in content
        assert "toc_stale" in content
        # Silent no-op when nothing drifted.
        assert "nothing to do" in normalized.lower() or "nothing drifted" in normalized.lower()

    def test_template_has_optout_ask_branch(self):
        """UNSET → check opt-out marker; else ASK; on NO record the opt-out."""
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        normalized = " ".join(content.split())
        # opt-out marker mechanism: a protected tagged memory, recalled per-project.
        assert "repo-wiki-optout" in content
        assert "recall(" in content
        assert "memorize(" in content
        assert "is_protected=True" in content
        # The ASK itself (yes/no) + the no-op-on-opted-out branch.
        assert "yes" in normalized.lower() and "no" in normalized.lower()
        # YES → background agent bulk-regen + pointer-anchor.
        assert "background agent" in normalized.lower()
        assert "pointer-anchor" in normalized.lower() or "pointer anchor" in normalized.lower()

    def test_template_is_no_nag_when_optout(self):
        """Opted-out project → silent no-op, never re-ask (no nag)."""
        content = _TEMPLATE_PATH.read_text(encoding="utf-8")
        normalized = " ".join(content.split()).lower()
        assert "no-op" in normalized or "no op" in normalized or "skip silently" in normalized
