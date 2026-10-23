"""Ledger 1791 — the stop-hook checkpoint for an external-ledger project.

A project whose project_id starts with a prefix in the global runtime config key
``tasks.external_ledger_prefixes`` keeps its tasks in yaadgaar. Its checkpoint
pointer names a VARIANT template with no task-list mirror step (base step 5),
the later steps renumbered. Every other project gets the base template and the
exact reason string it got before the gate.

The hook mints the project host-side with the same ``resolve_session_project``
SessionStart uses (ADR-0227: the mint lives in host-side hook entry points), so
a ``cd`` into a subdirectory or a git worktree still finds it, and falls back to
the session map SessionStart wrote. It reads the key through the fail-open host
client. The config client and the session map are always stubbed: these tests
never reach the live daemon or the real session map. The mint is stubbed too,
except in the real-git tests at the end, which run it against a throwaway repo.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
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
    # The base's MODE 1 skip and identity note name the task ledger among the
    # project-scoped steps; the variant has only the ADR ledger step left.
    text = text.replace(
        "the task ledger is keyed on the minted value",
        "the ADR ledger is keyed on the minted value",
    )
    text = text.replace(
        "SKIP\n         the ledger steps below", "SKIP\n         the ADR ledger step below (step 1)"
    )
    marker = 'WHEN A STEP SAYS "READ"'
    return text.replace(marker, _TASKS_NOTE + marker, 1)


def _load_module():
    spec = importlib.util.spec_from_file_location("stop_memory_checkpoint_1791", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook(monkeypatch):
    """The hook module with the mint, the session map and the config client stubbed.

    ``state["mint"]`` maps cwd → minted project (absent = the mint fails);
    ``state["real_mint"]`` is the shipped ``resolve_session_project``.
    """
    import yadgar._shared.runtime.session_map as session_map
    import yadgar.core.hooks._identity_mint as identity_mint
    import yadgar.core.runtime_config_client as rc_client

    state: dict = {
        "map": {},
        "mint": {},
        "value": None,
        "lookups": [],
        "reads": [],
        "real_mint": identity_mint.resolve_session_project,
    }

    def _mint(cwd):
        project = state["mint"].get(cwd)
        return project, "banner" if project else "notice"

    def _lookup(directory):
        state["lookups"].append(directory)
        return state["map"].get(directory)

    def _get(key, directory=None, default=None):
        state["reads"].append((key, directory, default))
        if key == _KEY and state["value"] is not None:
            return state["value"]
        return default

    monkeypatch.setattr(session_map, "lookup_project_for_directory", _lookup)
    monkeypatch.setattr(identity_mint, "resolve_session_project", _mint)
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


def _bare_step_5_refs_outside_the_mirror_block(base: str) -> list[str]:
    """Every bare ``step 5`` (any case) in the base outside the removed block.

    The transform drops step 5 and renumbers 6→5, so a bare ``step 5`` left
    elsewhere in the base would silently point at the wrong step in the
    variant. ``Step 5a``-``5d`` are not bare and live inside the block.
    """
    start = base.index("5. TASK-LIST MIRROR")
    end = base.index("6. Call project_brief")
    outside = base[:start] + base[end:]
    return re.findall(r"(?i)\bsteps? 5\b", outside)


def test_base_has_no_bare_step_5_reference_outside_the_removed_block() -> None:
    base = _BASE.read_text(encoding="utf-8")
    assert _bare_step_5_refs_outside_the_mirror_block(base) == []
    # The check itself bites: a bare reference planted in step 1 is caught.
    planted = base.replace("1. ADR CAPTURE", "1. ADR CAPTURE (see Step 5)", 1)
    assert _bare_step_5_refs_outside_the_mirror_block(planted) == ["Step 5"]


def test_variant_names_no_task_ledger_step_to_skip() -> None:
    text = _VARIANT.read_text(encoding="utf-8")
    assert "the ledger steps below" not in text
    assert "the task ledger is keyed" not in text
    assert "SKIP\n         the ADR ledger step below (step 1)" in text


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


def _mirror(project: str, registered: bool) -> None:
    """Record a yaadgaar registry answer and mirror it, as SessionStart does (ledger 1803)."""
    from yadgar.core import task_ledger_registry as reg

    path = reg.record_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"project": project, "registered": registered}))
    assert reg.mirror_registry_answer(project) is registered


def test_registered_project_outside_the_prefix_gets_the_variant(hook) -> None:
    """Ledger 1803: the yaadgaar registry decides; the prefix is not read."""
    hook.state["map"]["/w/widget"] = "acme/widget"
    hook.state["value"] = ["yadgarhq/"]
    _mirror("acme/widget", True)

    reason = hook._checkpoint_reason(25, "/w/widget")

    assert str(hook._EXTERNAL_LEDGER_TEMPLATE_PATH) in reason
    assert hook.state["reads"] == []


def test_unregistered_yadgarhq_project_gets_the_base_template(hook) -> None:
    hook.state["map"]["/w/new"] = "yadgarhq/newrepo"
    hook.state["value"] = ["yadgarhq/"]
    _mirror("yadgarhq/newrepo", False)

    reason = hook._checkpoint_reason(25, "/w/new")

    assert str(hook._PROMPT_TEMPLATE_PATH) in reason
    assert hook.state["reads"] == []


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


def test_minted_project_beats_the_session_map(hook) -> None:
    """The mint is preferred: an unmapped cwd that mints to a gated project is gated."""
    hook.state["mint"]["/w/docs/sub"] = "yadgarhq/docs"
    hook.state["value"] = ["yadgarhq/"]

    assert hook._EXTERNAL_LEDGER_TEMPLATE_PATH in hook._checkpoint_reason(25, "/w/docs/sub")
    assert hook.state["lookups"] == []


# ── real git: a cd into a subdirectory, and a worktree ──────────────────────


@pytest.fixture
def docs_repo(tmp_path, monkeypatch):
    """A throwaway repo whose origin mints to ``yadgarhq/docs``, plus a worktree.

    Global and system git config are cut off so no insteadOf rule or signing
    setting from the machine running the tests leaks in.
    """
    from yadgar._shared.server_helpers.server_helpers import _resolve_project_root
    from yadgar.core import identity

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    identity._insteadof_rules.cache_clear()
    _resolve_project_root.cache_clear()

    repo = tmp_path / "docs"
    (repo / "sub" / "deeper").mkdir(parents=True)

    def git(*args, cwd=repo):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    git("init", "-q")
    git("remote", "add", "origin", "https://example.invalid/yadgarhq/docs.git")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    worktree = tmp_path / "docs-wt"
    git("worktree", "add", "-q", str(worktree))
    yield {"repo": repo, "worktree": worktree}
    identity._insteadof_rules.cache_clear()
    _resolve_project_root.cache_clear()


@pytest.mark.parametrize("where", ["sub", "worktree"])
def test_subdir_and_worktree_get_the_variant(hook, monkeypatch, docs_repo, where) -> None:
    import yadgar.core.hooks._identity_mint as identity_mint

    monkeypatch.setattr(identity_mint, "resolve_session_project", hook.state["real_mint"])
    hook.state["value"] = ["yadgarhq/"]
    cwd = docs_repo["repo"] / "sub" / "deeper" if where == "sub" else docs_repo["worktree"]

    reason = hook._checkpoint_reason(25, str(cwd))

    assert hook._EXTERNAL_LEDGER_TEMPLATE_PATH in reason
    assert hook.state["lookups"] == []  # no session-map entry was needed
    assert hook.state["reads"] == [(_KEY, None, [])]
