"""Car B (task #74): stop-hook emits a short pointer reason instead of the full protocol.

The full capture/maintenance protocol stays in the packaged template file at
yadgar/core/hooks/templates/stop_checkpoint_prompt.md.  main() emits only:

    [yadgar] Checkpoint due. Read <path> and follow all the instructions in it.

where <path> is the on-disk path resolved via importlib.resources.

Pinned here:
- template file exists and content matches the pre-extraction pin (with header)
- _resolve_prompt_template_path() returns an existing on-disk path
- _PROMPT_TEMPLATE_PATH (module-level) is an existing on-disk path
- template file contains the expected protocol content (adr_add, wiki_add,
  project_brief, {directory}/{project}/{default_branch} placeholders, substitution header)
- main() reason is the short pointer line, NOT the full protocol
- decision is still "block" (hook remains blocking)
- missing/unresolvable template fails LOUD (RuntimeError), never a silent broken pointer
- install_hooks' copied standalone script emits the short pointer end-to-end,
  and the path in the pointer resolves to a real file
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

# Byte-exact pin of the checkpoint protocol template.
# Deliberately duplicated here, NOT read from the template file — reading the
# file back would make the assertion circular.  Update this pin ONLY when the
# protocol text intentionally changes.
_EXPECTED_TEMPLATE = """<!-- YADGAR CHECKPOINT PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory}      = your current working directory (absolute path; the project root)
       {project}        = basename of {directory}
       {default_branch} = last segment of `git -C {directory} symbolic-ref refs/remotes/origin/HEAD`;
                          fall back to "master" for non-git projects or on any git error.
-->

Yadgar checkpoint. CAPTURE FIRST (steps 1-3), then maintenance (steps 4-5).
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
   implement, etc.). Skip one-offs and trivial prompts.
   - Read existing patterns FIRST: recall(type="wiki", tags=["agent-prompt"]) (or
     check the agent-prompt-toc page). See which task-shapes already have a pattern.
   - If an EXISTING pattern already covers this task-shape, IMPROVE/extend it:
     agent_prompt_save the SAME pattern slug — agent_prompt_save versions it.
   - Only create a NEW slug when no existing pattern fits. NEVER mint a
     near-duplicate: a differently-named clone of an existing shape.
   - Call agent_prompt_save(directory="{directory}", pattern=<kebab-task-shape>,
     content=<the prompt>, purpose=<one line>) — same slug to extend a match,
     a new slug only when genuinely new.

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

# The short pointer reason emitted by main() — the only thing that changes in
# the block reason (Car B, task #74).  The actual path is determined at runtime
# by _resolve_prompt_template_path(); the format string here is a pattern test.
_REASON_PREFIX = "[yadgar] Checkpoint due. Read "
_REASON_SUFFIX = " and follow all the instructions in it."


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
    """The template file content is byte-equal to the pre-extraction pin."""
    assert _TEMPLATE_PATH.read_text(encoding="utf-8") == _EXPECTED_TEMPLATE


def test_loader_resolves_to_existing_path():
    """_resolve_prompt_template_path() returns a str pointing at an existing file."""
    mod = _load_module()
    resolved = mod._resolve_prompt_template_path()
    assert isinstance(resolved, str), "resolved path must be a str"
    assert Path(resolved).is_file(), f"Resolved path does not exist: {resolved}"


def test_module_path_is_existing_file():
    """_PROMPT_TEMPLATE_PATH (module-level) points at an existing file."""
    mod = _load_module()
    assert Path(mod._PROMPT_TEMPLATE_PATH).is_file(), (
        f"_PROMPT_TEMPLATE_PATH does not exist: {mod._PROMPT_TEMPLATE_PATH}"
    )


def test_template_has_protocol_content():
    """Template file contains the expected protocol calls and placeholders."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    # Substitution header present
    assert "{directory}" in content
    assert "{project}" in content
    assert "{default_branch}" in content
    # Protocol steps present
    assert "adr_add(" in content
    assert "wiki_add(" in content
    assert "project_brief(" in content
    # Hand-rolled append must be gone
    assert "wiki_append_section(" not in content


def test_template_has_substitution_header():
    """Template file has the substitution-key header block so the instance can
    derive {directory}, {project}, {default_branch} without rendering."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "YADGAR CHECKPOINT PROTOCOL" in content
    assert "Substitute these placeholders" in content
    assert "basename of {directory}" in content
    assert "symbolic-ref refs/remotes/origin/HEAD" in content


def test_agent_prompt_step_is_read_first():
    """Step 3 (AGENT-PROMPT CAPTURE) enforces read-first — recall existing
    patterns, extend a matching slug, never mint a near-duplicate. This keeps
    step 3 consistent with the read-first shape of steps 1 (ADR) + 2 (wiki)."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    # Read-existing-first: recall the tagged agent-prompt library before saving.
    assert 'recall(type="wiki", tags=["agent-prompt"])' in content
    # Extend a match on the SAME slug (versioning) rather than clone it.
    assert "SAME pattern slug" in content
    # Explicit no-near-duplicate guard.
    assert "near-duplicate" in content
    # New slug only when nothing fits.
    assert "NEW slug when no existing pattern fits" in content
    # The save call itself must still be present.
    assert "agent_prompt_save(" in content


# ---------------------------------------------------------------------------
# main() emits the short pointer reason
# ---------------------------------------------------------------------------


def test_main_emits_short_pointer_reason(tmp_path, capsys):
    """main() reason is the short pointer, NOT the full protocol text."""
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
    with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
        with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
            mod.main()
    result = json.loads(capsys.readouterr().out)

    assert result.get("decision") == "block"
    reason = result["reason"]

    # Reason is the short pointer — not the full protocol
    assert reason.startswith(_REASON_PREFIX), (
        f"Reason must start with pointer prefix. Got: {reason[:100]}"
    )
    assert reason.endswith(_REASON_SUFFIX), (
        f"Reason must end with pointer suffix. Got: {reason[-100:]}"
    )

    # The path in the reason must point at the real template file
    path_in_reason = reason[len(_REASON_PREFIX) : -len(_REASON_SUFFIX)]
    assert Path(path_in_reason).is_file(), f"Path in reason does not exist: {path_in_reason}"

    # The reason must NOT contain the full protocol content
    assert "CAPTURE FIRST" not in reason, "Reason must not contain full protocol text"
    assert "adr_add(" not in reason, "Reason must not contain protocol step content"


def test_main_reason_path_points_at_correct_template(tmp_path, capsys):
    """The path embedded in the reason points at the file containing the protocol."""
    state_path = tmp_path / "state.json"
    transcript = tmp_path / "t.jsonl"
    lines = [json.dumps({"role": "user", "content": f"msg {i}"}) for i in range(25)]
    transcript.write_text("\n".join(lines))

    mod = _load_module()
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    stdin_data = {
        "session_id": "s-path",
        "transcript_path": str(transcript),
        "stop_hook_active": False,
        "cwd": str(project_dir),
    }
    with patch("sys.stdin", io.StringIO(json.dumps(stdin_data))):
        with patch.object(mod._paths, "STOP_HOOK_STATE_PATH", state_path):
            mod.main()
    result = json.loads(capsys.readouterr().out)
    reason = result["reason"]

    path_in_reason = reason[len(_REASON_PREFIX) : -len(_REASON_SUFFIX)]
    template_content = Path(path_in_reason).read_text(encoding="utf-8")
    # The file at the path must be the actual protocol template
    assert "YADGAR CHECKPOINT PROTOCOL" in template_content
    assert "adr_add(" in template_content
    assert "project_brief(" in template_content


# ---------------------------------------------------------------------------
# Missing / unresolvable template → fail loud
# ---------------------------------------------------------------------------


def test_missing_template_fails_loud():
    """Unresolvable package resource → RuntimeError naming the template."""
    import pytest

    mod = _load_module()
    with patch("importlib.resources.files", side_effect=FileNotFoundError("gone")):
        with pytest.raises(RuntimeError, match="stop_checkpoint_prompt.md"):
            mod._resolve_prompt_template_path()


def test_empty_template_fails_loud():
    """Empty template content → RuntimeError (never a blank checkpoint prompt)."""
    import pytest

    mod = _load_module()
    import pathlib
    from contextlib import contextmanager

    @contextmanager
    def _fake_as_file(ref):
        # Write an empty file to a tmp location and yield its path
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            f.write(b"   \n")
            name = f.name
        yield pathlib.Path(name)

    class _Node:
        def joinpath(self, *_a):
            return self

    with patch("importlib.resources.files", return_value=_Node()):
        with patch("importlib.resources.as_file", _fake_as_file):
            with pytest.raises(RuntimeError, match="empty"):
                mod._resolve_prompt_template_path()


# ---------------------------------------------------------------------------
# Installer: the copied standalone script emits the short pointer end-to-end
# ---------------------------------------------------------------------------


def test_installed_copy_emits_short_pointer_end_to_end(tmp_path):
    """install_hooks copies a SINGLE standalone script; the template is NOT
    copied alongside — it resolves from the installed yadgar package, which the
    copy already imports (yadgar._shared). Prove the copied script emits the
    short pointer and that the path in the pointer resolves to a real file."""
    from yadgar.core.install.install_hooks_lib import install_hooks_impl

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

    reason = out["reason"]
    assert reason.startswith(_REASON_PREFIX), (
        f"reason must start with pointer prefix: {reason[:120]}"
    )
    assert reason.endswith(_REASON_SUFFIX), f"reason must end with pointer suffix: {reason[-120:]}"

    # Proof that the path resolves post-install (req 2)
    path_in_reason = reason[len(_REASON_PREFIX) : -len(_REASON_SUFFIX)]
    assert Path(path_in_reason).is_file(), (
        f"Path in reason does not exist post-install: {path_in_reason}"
    )
