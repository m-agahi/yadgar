"""Car 3 (task #34): stop-hook checkpoint prompt lives in an external template.

The big capture/maintenance prompt emitted by the Stop hook
(yadgar/core/hooks/stop-memory-checkpoint.py) is extracted to package data:
yadgar/core/hooks/templates/stop_checkpoint_prompt.md, loaded at runtime via
importlib.resources.

Pinned here:
- template file exists and is byte-equal to the pre-extraction literal
- module loader resolves it via package resources (works for the standalone
  copy under ~/.claude/hooks too — that copy already imports yadgar._shared)
- rendered output (main() reason) byte-equal to the pre-extraction render
- missing/empty template fails LOUD (RuntimeError), never a silent broken prompt
- install_hooks' copied standalone script still renders the prompt end-to-end
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).parent.parent.parent
_HOOK_PATH = _REPO / "core" / "hooks" / "stop-memory-checkpoint.py"
_TEMPLATE_PATH = _REPO / "core" / "hooks" / "templates" / "stop_checkpoint_prompt.md"

# Byte-exact pin of the checkpoint prompt template (pre-extraction literal).
# Deliberately duplicated here, NOT read from the template file — reading the
# file back would make the assertion circular. Update this pin ONLY when the
# prompt text intentionally changes.
_EXPECTED_TEMPLATE = """Yadgar checkpoint. CAPTURE FIRST (steps 1-3), then maintenance (steps 4-5).
Decisions and findings scroll out of context and are lost forever; maintenance
signals re-fire next checkpoint. Capture is the irreplaceable work — if you must
triage anything away under length pressure, drop maintenance, NEVER capture.

1. ADR CAPTURE (always run; the Yadgar wiki is the source of truth — no file,
   works for non-git projects too).
   Page: slug "{project}-adr-log", tag "adr", scoped to this directory.
   - Read existing ADRs FIRST: wiki_read("{project}-adr-log", directory="{directory}",
     branch_hint="{default_branch}"). If the page is absent the log is empty — no
     prior ADRs to dedup against. Do NOT create the log manually; adr_add handles
     creation automatically.
   - Scan THIS session for durable decisions since the last checkpoint.
     KEEP (precision over recall): a clear durable decision — architecture, a
     tool/config choice, an approach committed-to, a scope cut; a conclusion we
     commit to and stop investing in (NOT a passing status report); a fix that
     changes an approach or contract. A user "record this" ALWAYS qualifies.
     SKIP: routine work (git push, branch cleanup, progress/status checks),
     in-flux or abandoned ideas, pure status ("tests pass"), routine corrections
     (typos, lint).
   - Dedup by decision, NOT by wording: if the substance of a decision is already
     logged in the ADRs you read above, SKIP it — even if the wording differs.
     Only call adr_add for genuinely new decisions.
   - For each new decision call:
       adr_add(
           directory="{directory}",
           title=<short human-readable title>,
           status=<open|accepted|superseded|rejected|deprecated>,
           date=<ISO date>,
           context=<what triggered this decision>,
           decision=<what was decided>,
           rationale=<why — the reasoning>,
           alternatives=<options considered + why rejected; "none" if none>,
           consequences=<trade-offs / costs / caveats / flags; "none" if none>,
           revisit_trigger=<condition to reconsider; "none" if none>,
           supersedes=<ADR-NNNN or "none">,
       )
     adr_add assigns the ADR-NNNN id, formats, and branch-pins the entry.
     ALL fields mandatory — write "none" if truly empty (keeps it machine-parseable).
     A decision still unresolved this session → status: open, revisit_trigger = pending question.

2. STRUCTURAL WRITE-BACK (always consider). Durable repo-structure / convention /
   module-purpose findings from THIS session → the EXISTING wiki page that owns
   the topic (wiki_list → slug → wiki_read; update via wiki_add(replace_slug=<slug>,
   ..., directory="{directory}", branch_hint="{default_branch}", wait=True); no
   near-duplicate pages). If no page fits, create one with wiki_add(tags=[...],
   directory="{directory}", branch_hint="{default_branch}", wait=True).
   Verify wiki_history. Facts/structure only — decisions go in step 1.

3. AGENT-PROMPT CAPTURE (only if the library is enabled — skip silently otherwise).
   Scan THIS session for a reusable SUBAGENT DISPATCH PROMPT you crafted or
   refined — one worth reusing for a recurring task shape (review, debug, explore,
   implement, etc.). If genuinely reusable (NOT a one-off, NOT trivial), call
   agent_prompt_save(directory="{directory}", pattern=<kebab-task-shape>,
   content=<the prompt>, purpose=<one line>). Skip one-offs and trivial prompts.

4. Call project_brief("{directory}", mode="signals").

5. MAINTENANCE — for each entry in recommended_actions:
   - ANCHOR HYGIENE: if audit_anchors appears, run it once:
     audit_anchors("{directory}", dry_run=True) → review actions list →
     audit_anchors("{directory}", dry_run=False) to apply forget/merge. The tool
     self-guards (never drops semantic_immortal or protected-legacy anchors). For
     any promote draft it returns, wiki_add it only if wiki-worthy (step-2 rules),
     else skip. Run this flow at most once.
   - Else if the action has a `suggested_call`: run it verbatim, supplying content
     from THIS session for placeholders (content='...', key_decisions=[...]) — the
     suggested_call is the exact shape; supply only the content, don't invent it.
     (Covers refresh_active_work, consider_refresh_active_work, refresh_checkpoint,
     consider_refresh_checkpoint, extract_last_session_findings, update_roadmap,
     review_rejections.)
   - bootstrap_project (no suggested_call): propose a <=1500-char project-summary
     memory, then bootstrap_project("{directory}", content).
   - Any action type NOT covered above AND with no suggested_call → SKIP and flag
     it in your reply (do not improvise the mechanics).

[yadgar] Checkpoint cadence reached — capture, then continue. If you were
mid-thought, repeat your last question so the conversation continues. Resume after
/clear or session end: restore(directory="{directory}").
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("stop_memory_checkpoint_t", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Template file + loader
# ---------------------------------------------------------------------------


def test_template_file_exists():
    assert _TEMPLATE_PATH.exists(), f"Template not found at {_TEMPLATE_PATH}"


def test_template_file_byte_equal_pin():
    """The template file content is byte-equal to the pre-extraction literal."""
    assert _TEMPLATE_PATH.read_text(encoding="utf-8") == _EXPECTED_TEMPLATE


def test_loader_resolves_via_package_resources():
    """_load_prompt_template() reads the packaged template (importlib.resources)."""
    mod = _load_module()
    assert mod._load_prompt_template() == _EXPECTED_TEMPLATE


def test_module_template_byte_equal_pin():
    """_PROMPT_TEMPLATE (module-level, used by main()) matches the pin."""
    mod = _load_module()
    assert mod._PROMPT_TEMPLATE == _EXPECTED_TEMPLATE


def test_template_has_all_placeholders_and_no_stray_braces():
    """format() placeholders survive extraction; no accidental literal braces."""
    for ph in ("{directory}", "{project}", "{default_branch}"):
        assert ph in _EXPECTED_TEMPLATE
    # .format() must succeed without KeyError/IndexError from stray braces.
    rendered = _EXPECTED_TEMPLATE.format(
        directory="/tmp/proj", project="proj", default_branch="main"
    )
    assert "{directory}" not in rendered


# ---------------------------------------------------------------------------
# Rendering byte-equal to current output (end-to-end via main())
# ---------------------------------------------------------------------------


def test_main_renders_byte_equal(tmp_path, capsys):
    """main()'s block reason == pinned template rendered with the same fields."""
    state_path = tmp_path / "state.json"
    transcript = tmp_path / "t.jsonl"
    lines = [json.dumps({"role": "user", "content": f"msg {i}"}) for i in range(25)]
    transcript.write_text("\n".join(lines))

    mod = _load_module()
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    stdin_data = {
        "session_id": "s-tmpl",
        "transcript_path": str(transcript),
        "stop_hook_active": False,
        "cwd": str(project_dir),
    }
    with patch.object(mod, "_default_branch", return_value="master"):
        with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
            with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
                mod.main()
    reason = json.loads(capsys.readouterr().out)["reason"]

    expected = _EXPECTED_TEMPLATE.format(
        directory=str(project_dir), project="myproj", default_branch="master"
    )
    assert reason == expected


# ---------------------------------------------------------------------------
# Missing / empty template → fail loud
# ---------------------------------------------------------------------------


def test_missing_template_fails_loud():
    """Unresolvable package resource → RuntimeError naming the template."""
    import pytest

    mod = _load_module()
    with patch("importlib.resources.files", side_effect=FileNotFoundError("gone")):
        with pytest.raises(RuntimeError, match="stop_checkpoint_prompt.md"):
            mod._load_prompt_template()


def test_empty_template_fails_loud():
    """Empty template content → RuntimeError (never a blank checkpoint prompt)."""
    import pytest

    mod = _load_module()

    class _Node:
        def joinpath(self, *_a):
            return self

        def read_text(self, encoding="utf-8"):
            return "   \n"

    with patch("importlib.resources.files", return_value=_Node()):
        with pytest.raises(RuntimeError, match="empty"):
            mod._load_prompt_template()


# ---------------------------------------------------------------------------
# Installer: the copied standalone script still renders the prompt
# ---------------------------------------------------------------------------


def test_installed_copy_renders_prompt_end_to_end(tmp_path):
    """install_hooks copies a SINGLE standalone script; the template is NOT
    copied alongside — it resolves from the installed yadgar package, which the
    copy already imports (yadgar._shared). Prove the copied script renders."""
    from yadgar.core.install_hooks_lib import install_hooks_impl

    home = tmp_path / "home"
    home.mkdir()
    result = install_hooks_impl(home_dir=home, scope="global", project_directory=str(tmp_path))
    assert result["status"] == "installed"

    copied = home / ".claude" / "hooks" / "yadgar-stop-memory-checkpoint.py"
    assert copied.exists(), "installer did not copy the stop hook"
    # Installer must NOT need to copy the template next to the script.
    assert not (home / ".claude" / "hooks" / "templates").exists()

    transcript = tmp_path / "t.jsonl"
    lines = [json.dumps({"role": "user", "content": f"msg {i}"}) for i in range(25)]
    transcript.write_text("\n".join(lines))

    project_dir = tmp_path / "endproj"
    project_dir.mkdir()
    env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(tmp_path / "xdg-state")}
    proc = subprocess.run(
        [sys.executable, str(copied)],
        input=json.dumps(
            {
                "session_id": "s-copy",
                "transcript_path": str(transcript),
                "stop_hook_active": False,
                "cwd": str(project_dir),
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"copied hook crashed: {proc.stderr[-500:]}"
    out = json.loads(proc.stdout.strip())
    assert out.get("decision") == "block"
    expected = _EXPECTED_TEMPLATE.format(
        directory=str(project_dir), project="endproj", default_branch="master"
    )
    assert out["reason"] == expected
