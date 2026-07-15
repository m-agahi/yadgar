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

Yadgar checkpoint. CAPTURE FIRST (steps 1-4), THEN maintenance (steps 5-6).
This is an ORDERING, not a licence to skip: run ALL six steps. Capture goes
first only because decisions and findings scroll out of context and are lost
forever, while maintenance signals re-fire next checkpoint. Ordering is NOT
permission to drop maintenance — you may not skip steps 5-6 to save length,
time, or effort. The ONLY legitimate skips are the closed allowed-skip list
spelled out in step 6, each of which means maintenance is INAPPLICABLE this
checkpoint (nothing to do), never that you chose to defer real work.

WHEN A STEP SAYS "READ" — read for real. Every "read the ADR log / wiki page /
task-list page / checkpoint" instruction below means: actually CALL the named
tool THIS turn and act on the CONTENT IT RETURNS. Do NOT paraphrase, summarise,
or reconstruct a page from memory of an earlier turn — the page may have changed,
and a checkpoint built on a remembered pointer instead of the live bytes is the
exact failure this protocol exists to prevent. On-disk paths → the Read tool;
wiki slugs → wiki_read; the tagged agent-prompt library → recall.

1. ADR CAPTURE (always run; the Yadgar wiki is the source of truth — no file,
   works for non-git projects too).
   Page: slug "{project}-adr-log", tag "adr", scoped to this directory.
   - Read existing ADRs FIRST — actually CALL it now and dedup against the
     RETURNED content, not your memory of it: wiki_read("{project}-adr-log",
     directory="{directory}", branch_hint="{default_branch}"). If the page is
     absent the log is empty — no prior ADRs to dedup against. Do NOT create the
     log manually; adr_add handles creation automatically.
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
   the topic (wiki_list → slug → wiki_read the slug NOW and edit the content it
   RETURNS — do NOT rewrite a page from memory; update via
   wiki_add(replace_slug=<slug>, ..., directory="{directory}",
   branch_hint="{default_branch}", wait=True); no near-duplicate pages). If no
   page fits, create one with wiki_add(tags=[...], directory="{directory}",
   branch_hint="{default_branch}", wait=True).
   Verify wiki_history. Facts/structure only — decisions go in step 1.

3. AGENT-PROMPT CAPTURE (only if the library is enabled — skip silently otherwise).
   Scan THIS session for a reusable SUBAGENT DISPATCH PROMPT you crafted or
   refined — one worth reusing for a recurring task shape (review, debug, explore,
   implement, etc.). Skip one-offs and trivial prompts.
   - Read existing patterns FIRST — actually CALL recall now and judge from the
     RETURNED patterns, not memory: recall(type="wiki", tags=["agent-prompt"]) (or
     wiki_read the agent-prompt-toc page). See which task-shapes already have a
     pattern.
   - If an EXISTING pattern already covers this task-shape, IMPROVE/extend it:
     agent_prompt_save the SAME pattern slug — agent_prompt_save versions it.
   - Only create a NEW slug when no existing pattern fits. NEVER mint a
     near-duplicate: a differently-named clone of an existing shape.
   - Call agent_prompt_save(directory="{directory}", pattern=<kebab-task-shape>,
     content=<the prompt>, purpose=<one line>) — same slug to extend a match,
     a new slug only when genuinely new.

4. TASK-LIST MIRROR (always). Persist your Claude Code harness task list to the
   wiki so it survives session exit / /clear. Page: slug "{project}-task-list",
   tag "task-list", scoped to this directory. Page format = the SCHEMA block at
   the bottom of this step; each task is a UNIQUE "## task:<id>" section.
   - Step 4a — RECONCILE YOUR OWN LIST FIRST. Call TaskList. TaskUpdate anything
     completed or blocked this session; TaskCreate any follow-ups you discovered.
     This is the every-checkpoint "update your task list" pass — do it before you
     mirror, so the page reflects reality.
   - Step 4b — READ THE PAGE FOR REAL: CALL wiki_read("{project}-task-list",
     directory="{directory}") NOW and reconcile against the tasks + updated_at it
     RETURNS — never against a remembered copy of the page. Absent = no saved list
     yet.
   - Step 4c — BRANCH on {have open tasks after reconcile?} × {page exists?}:
     (The task-list page is CANONICAL — one call lands it on the project-canonical
     / branch-NULL slot via the sanctioned wiki_write_task_list writer, so the
     session-start restore-nudge resolves it from ANY branch and from a non-git
     project. You do NOT craft a wiki_add / choose a branch — the writer sets the
     canonical branch server-side.)
     - have tasks · NO page → CREATE. wiki_write_task_list(project="{project}",
       content=<full page: ## Meta + one ## task:<id> section each>,
       directory="{directory}").
     - NO tasks · NO page → SKIP. Nothing to do.
     - NO tasks · page EXISTS → CATCH-UP SYNC. The page has tasks you don't.
       Adopt its OPEN tasks (status ∈ {pending, in_progress}) into your harness
       via TaskCreate — recovers a missed session-start restore or a concurrent
       session's work. GUARD: never adopt a completed task; if ALL page tasks are
       completed, OR the page's DB updated_at (from the wiki_read metadata) is
       older than 14 days, do NOT adopt — note "stale/finished saved list" and
       leave it. Adopt by judgment: open tasks relevant to the work you are about
       to do; skip ones clearly from a finished or unrelated effort.
     - have tasks · page EXISTS → MERGE + WRITE BACK. Reconcile the page's open
       tasks with yours (union; your live status wins for tasks you own; keep
       page-only open tasks). Default write = FULL REWRITE:
       wiki_write_task_list(project="{project}", content=<merged full page>,
       directory="{directory}"). OPTIONAL surgical path (only when your change is
       confined to ONE task): wiki_append_section(slug="{project}-task-list",
       section_heading="task:<id>", position="replace_section", heading_type="h2",
       content=<that task's body>, directory="{directory}") — the "## task:<id>"
       heading is UNIQUE so this is section-atomic and will not clobber a
       concurrent edit to a DIFFERENT task's section.
   - PER-TASK BODY: render fields as "- key: value" flat bullets UNDER the
     "## task:<id>" heading — subject, status, active_form (optional),
     description, context, blockedBy, blocks, modified. Multi-line values
     (description, context) MUST indent continuation lines 2 spaces so an
     embedded "##" line or ``` fence cannot be mis-parsed as a section boundary
     (same discipline as the ADR to_markdown_body renderer). The "context" field
     carries related-context pointers to where the task's work lives: file paths ·
     [[wiki-slug]] · docs/plans/*.md · mem:<id>. The "modified" field is
     ISO-8601 UTC, bumped ONLY on a real change to that task (per-task freshness).
     Do NOT hand-write a page-level "updated:" stamp — the age gate reads the DB
     updated_at column. Verify wiki_history after writing.
   - SCHEMA (page body):
     ```markdown
     <!-- yadgar task-list page — schema v1. One "## task:<id>" section per task.
          Fields are "- key: value" bullets; multi-line values indent 2 spaces.
          status ∈ {pending, in_progress, completed}. Restore: recreate open
          tasks via TaskCreate. -->

     # {project} task list

     ## Meta
     - project: {project}
     - open: <N> · completed: <M>

     ## task:0003
     - subject: <one line>
     - status: in_progress
     - active_form: <present-tense label>
     - description: <text; continuation lines indent 2 spaces>
     - context: src/foo.py · [[some-wiki-slug]] · docs/plans/x.md · mem:4821
     - blockedBy: 0005
     - blocks:
     - modified: 2026-07-14T18:20:32Z
     ```
     Zero-pad each <id> to 4 digits ("task:0001" ≠ "task:0012") so the section
     matcher is exact. status is the harness value VERBATIM, enum
     {pending, in_progress, completed} — there is NO "blocked" status (blocking
     is the blockedBy array).

5. Call project_brief("{directory}", mode="signals"). UNCONDITIONAL — this call
   is how you LEARN whether maintenance applies; it is cheap and you may never
   skip it. Its recommended_actions list drives step 6.

6. MAINTENANCE — MANDATORY. You MUST work the recommended_actions list from
   step 5 to completion. It is NOT optional and NOT droppable under length, time,
   or effort pressure. Skip the maintenance pass ONLY IF one of the following
   allowed-skip conditions holds — this is the complete, closed list:
     (a) project_brief returned recommended_actions EMPTY (nothing to do);
     (b) every recommended_action was already handled earlier THIS checkpoint;
     (c) the session did no writes and made no state changes at all (a pure
         read-only session), so no active_work / checkpoint refresh is warranted.
   If NONE of (a)-(c) holds you MUST run the pass below. "I am running low on
   length" / "this feels minor" are NOT on the list and do NOT authorize a skip.
   For each entry in recommended_actions:
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
    # wiki_append_section reappears in the TASK-LIST MIRROR step (step 4) as the
    # OPTIONAL surgical single-task path (replace_section on a unique
    # "## task:<id>" heading). Car B (#74) had removed a hand-rolled ADR append;
    # this is a different, deliberate reintroduction — assert it is present.
    assert "wiki_append_section(" in content


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


def test_task_list_mirror_step_present():
    """Step 4 (TASK-LIST MIRROR) persists the harness task list to the wiki.

    Asserts the step names the harness tools (TaskList/TaskUpdate/TaskCreate),
    the four state-machine cases, the catch-up guard, the schema shape, the
    status enum, the related-context pointer, the 2-space continuation-indent
    rule, and the optional surgical wiki_append_section(replace_section) path.
    """
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    # Named step + slug + the sanctioned canonical writer (Car 1). The tag +
    # page_type are baked into wiki_write_task_list, not named in the template.
    assert "TASK-LIST MIRROR" in content
    assert "{project}-task-list" in content
    assert 'wiki_write_task_list(project="{project}"' in content
    # Reconcile own list FIRST via the harness tools.
    assert "TaskList" in content
    assert "TaskUpdate" in content
    assert "TaskCreate" in content
    assert "RECONCILE YOUR OWN LIST FIRST" in content
    # Read-before-write.
    assert 'wiki_read("{project}-task-list"' in content
    # The FOUR cases of the state machine.
    assert "CREATE" in content
    assert "SKIP" in content
    assert "CATCH-UP SYNC" in content
    assert "MERGE + WRITE BACK" in content
    # Catch-up guard: skip completed, all-done→skip, 14-day age gate on DB updated_at.
    assert "never adopt a completed task" in content
    assert "ALL page tasks are\n     completed" in content or "ALL page tasks are completed" in (
        " ".join(content.split())
    )
    assert "14 days" in content
    assert "updated_at" in content
    # Section-per-task schema + status enum + verbatim.
    assert "## task:<id>" in content
    assert "{pending, in_progress, completed}" in content
    assert 'no "blocked" status' in " ".join(content.split()).replace("NO", "no")
    # key: value fields under the heading.
    for field in ("subject", "status", "description", "context", "blockedBy", "blocks", "modified"):
        assert field in content, f"per-task field {field!r} missing from schema"
    # Related-context pointers clause.
    assert "related-context pointers" in content
    assert "mem:<id>" in content
    # 2-space continuation-indent rule (section-boundary poisoning defence).
    assert "indent continuation lines 2 spaces" in content
    assert "mis-parsed as a section boundary" in content
    # Zero-pad discipline for exact section matching.
    assert "Zero-pad each <id> to 4 digits" in content
    assert '"task:0001" ≠ "task:0012"' in content
    # Optional surgical single-task path.
    assert "replace_section" in content
    assert 'section_heading="task:<id>"' in content
    # The full-rewrite default write goes through the sanctioned canonical writer
    # (Car 1) — replace_slug + branch-NULL are baked in server-side, so the
    # template no longer instructs a raw wiki_add(replace_slug=...).
    assert 'wiki_write_task_list(project="{project}", content=<merged full page>' in content


def test_maintenance_step_is_mandatory_with_closed_allowed_skip_list():
    """Issue 2 (Car 3): the maintenance pass (steps 5-6) is MANDATORY. The
    header no longer pre-authorizes dropping maintenance under length pressure,
    step 5 is UNCONDITIONAL, and step 6 defines a closed allowed-skip list."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    # Header no longer licenses dropping maintenance under length pressure.
    assert "drop maintenance, NEVER capture" not in content, (
        "header must not pre-authorize dropping the maintenance pass"
    )
    assert "run ALL six steps" in normalized

    # Step 5 (project_brief signals) is explicitly unconditional.
    assert "UNCONDITIONAL" in content

    # Step 6 is MANDATORY and states it is not droppable under length pressure.
    assert "MAINTENANCE — MANDATORY" in content
    assert "NOT optional and NOT droppable" in normalized

    # Closed allowed-skip list: the imperative phrasing + all three conditions.
    assert "Skip the maintenance pass ONLY IF" in normalized
    assert "complete, closed list" in normalized
    assert "recommended_actions EMPTY" in normalized
    assert "already handled earlier THIS checkpoint" in normalized
    assert "pure\n         read-only session" in content or "pure read-only session" in normalized
    # Length/effort rationalizations are explicitly NOT on the list.
    assert "you MUST run the pass" in normalized
    assert "do NOT authorize a skip" in normalized

    # The pre-existing per-action "SKIP and flag" escape (uncovered action type)
    # is a DIFFERENT legitimate skip and must survive.
    assert "SKIP and flag" in content


def test_read_instructions_are_strict_live_reads():
    """Issue 3 (Car 3): every read-a-file/slug/checkpoint instruction strictly
    tells the model to CALL the tool and act on the RETURNED content, not
    paraphrase from memory. Failure mode: model improvises off a short pointer."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    # Global strict-read preamble present, and it routes each retrieval to the
    # correct tool (Read for on-disk paths, wiki_read for slugs, recall for the
    # tagged agent-prompt library).
    assert 'WHEN A STEP SAYS "READ"' in content
    assert "act on the CONTENT IT RETURNS" in normalized
    assert "not paraphrase" in normalized.lower() or "Do NOT paraphrase" in normalized
    assert "On-disk paths → the Read tool" in normalized
    assert "wiki slugs → wiki_read" in normalized
    assert "agent-prompt library → recall" in normalized

    # Per-step strict-read reinforcement.
    # Step 1 ADR read.
    assert "dedup against the\n     RETURNED content" in content or (
        "dedup against the RETURNED content" in normalized
    )
    # Step 2 wiki read.
    assert (
        "edit the content it\n   RETURNS" in content or "edit the content it RETURNS" in normalized
    )
    # Step 3 agent-prompt recall.
    assert "judge from the\n     RETURNED patterns" in content or (
        "judge from the RETURNED patterns" in normalized
    )
    # Step 4b task-list read.
    assert "READ THE PAGE FOR REAL" in content
    assert "never against a remembered copy" in normalized


def test_task_list_mirror_status_enum_has_no_blocked():
    """The status enum is EXACTLY {pending, in_progress, completed} — no 'blocked'
    status (blocking is expressed via the blockedBy array, per the verified live
    harness output)."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "{pending, in_progress, completed}" in content
    # The schema must state blocking is the blockedBy array, not a status value.
    normalized = " ".join(content.split())
    assert "blockedBy array" in normalized
    # There must be no "status: blocked" example anywhere in the schema.
    assert "status: blocked" not in content


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
