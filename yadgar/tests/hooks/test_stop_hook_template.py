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
  project_brief, adr_list, task_list, task_write, {directory}/{project}
  placeholders, substitution header)
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
# Car E (0047 §16) and Car G (0047 §7) rewrote the task-list and ADR sections:
# the task-list mirror now reaches the SQL task ledger (task_list / task_write),
# and the ADR dedup now reaches the SQL ADR ledger (adr_list). The legacy wiki
# slug writes for tasks no longer exist and have no corresponding assertions.
_EXPECTED_TEMPLATE = """<!-- YADGAR CHECKPOINT PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory}      = your current working directory (absolute path; the project root)
       {project}        = the session's minted project_id — the `owner/repo` value
                          emitted at SessionStart as `yadgar: project_id=<owner/repo>`.
                          It is NOT the basename of {directory}: the basename is not an
                          identity (two checkouts named `yadgar` are two projects), and
                          the task ledger is keyed on the minted value (ADR-0227).
                          If you cannot find that line, scroll for the `current_project`
                          memory block, which carries the same value.

     IDENTITY MISSING OR REFUSED — two distinct modes. Tell them apart, then ASK
     THE USER. Never invent, derive or guess a key (ADR-0227); asking is the
     sanctioned path precisely BECAUSE deriving one is forbidden.
       MODE 1 — no key at all (neither the banner nor the `current_project` block).
         The SessionStart mint failed: no git `origin` remote, or no repo. SKIP
         the ledger steps below rather than inventing a key.
         Tell the user which yadgar writes are blocked and hand over the fix —
         add an `origin` remote, or pin the key explicitly with
         `mkdir -p .yadgar && echo owner/repo > .yadgar/project-id` — then a NEW
         session, because the mint runs only at SessionStart.
       MODE 2 — key present, but a scoped call answers `unknown project_id:
         '<key>'` (reason `unknown_project`). You cannot detect this at header
         time: it surfaces on the FIRST scoped call below, so when it arrives
         come back here and treat it as this mode. It is not a transient tool
         error and retrying will not clear it. The identity is fine; the
         `project` registry has no row for it. Diagnose with
         `yadgar project list` (read-only). An `ERROR:` line means the backend is
         unreachable — that is UNDETERMINED, so report it and conclude nothing.
         If it lists rows and yours is absent, ask the user to append one
         5-column TAB-separated row (source_directory, project_id, 0, 0, note) to
         `{directory}/.yadgar/project-id-map.tsv` and run `yadgar project seed
         --map {directory}/.yadgar/project-id-map.tsv` — the only registration
         path, and idempotent (existing rows come back `skipped`). The map path
         must be absolute: `project seed` resolves a relative default against the
         shell's cwd, not the project root. Do not run `seed` yourself; it writes
         a shared registry.
-->

Yadgar checkpoint. CAPTURE FIRST (steps 1-5), THEN maintenance (steps 6-7).
This is an ORDERING, not a licence to skip: run ALL seven steps. Capture goes
first only because decisions and findings scroll out of context and are lost
forever, while maintenance signals re-fire next checkpoint. Ordering is NOT
permission to drop maintenance — you may not skip steps 6-7 to save length,
time, or effort. The ONLY legitimate skips are the closed allowed-skip list
spelled out in step 7, each of which means maintenance is INAPPLICABLE this
checkpoint (nothing to do), never that you chose to defer real work.

WHEN A STEP SAYS "READ" — read for real. Every "read the ADR log / wiki page /
task-list page / checkpoint" instruction below means: actually CALL the named
tool THIS turn and act on the CONTENT IT RETURNS. Do NOT paraphrase, summarise,
or reconstruct a page from memory of an earlier turn — the page may have changed,
and a checkpoint built on a remembered pointer instead of the live bytes is the
exact failure this protocol exists to prevent. On-disk paths → the Read tool;
wiki slugs → wiki_read; the tagged agent-prompt library → recall.

1. ADR CAPTURE (always run; the Yadgar ADR ledger is the source of truth —
   no file, works for non-git projects too).
   - Read existing ADRs FIRST — actually CALL it now and dedup against the
     RETURNED content, not your memory of it: adr_list(directory="{directory}",
     project="{project}", status="open"). If the list is empty there are no
     open ADRs to dedup
     against. Do NOT create the log manually; adr_add handles creation
     automatically.
   <!-- Car G (0047 §7): step 1's read-first-dedup now reaches the SQL ADR
        ledger via adr_list(directory=...) above. Pre-G the instruction
        pointed at a wiki page whose slug followed the deleted-monolith
        shape; that read path is gone. adr_list reads the ledger, NOT a
        wiki page. Historical note only — do not re-introduce the legacy
        wiki_read call. -->
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
           project="{project}",
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
     adr_add assigns the ADR-NNNN id and formats the entry.
     ALL fields mandatory — write "none" if truly empty (keeps it machine-parseable).
     A decision still unresolved this session → status: open, revisit_trigger = pending question.
     Same when YOU have settled it but the user has not ruled on it: `accepted`
     means chosen AND ratified, so a decision whose own text says proposed /
     pending / recommended takes status: open. Never file a status that
     disagrees with the text under it — both statuses are binding tier, so
     the status is all that tells a later reader whether anyone agreed.

2. STRUCTURAL WRITE-BACK (always consider). Durable repo-structure / convention /
   module-purpose findings from THIS session → the EXISTING wiki page that owns
   the topic (wiki_list → slug → wiki_read the slug NOW and edit the content it
   RETURNS — do NOT rewrite a page from memory; update via
   wiki_add(replace_slug=<slug>, ..., directory="{directory}",
   project="{project}", wait=True); no near-duplicate pages). If no
   page fits, create one with wiki_add(tags=[...], directory="{directory}",
   project="{project}", wait=True).
   EVERY scoped call in this protocol carries project="{project}". Since C5 an
   identity is never derived, so a call that omits it is REJECTED with
   {{"error": "unresolved_project"}} rather than scoped to a guess.
   Verify wiki_history. Facts/structure only — decisions go in step 1.

3. AGENT-PROMPT CAPTURE (only if the library is enabled — skip silently otherwise).
   Scan THIS session for a reusable SUBAGENT DISPATCH PROMPT you crafted or
   refined — one worth reusing for a recurring task shape (review, debug, explore,
   implement, etc.). Skip one-offs and trivial prompts.
   - Read existing patterns FIRST — actually CALL recall now and judge from the
     RETURNED patterns, not memory: recall(type="wiki", tags=["agent-prompt"],
     directory="{directory}", project="{project}") (or
     wiki_read the agent-prompt-toc page). See which task-shapes already have a
     pattern.
   - If an EXISTING pattern already covers this task-shape, IMPROVE/extend it:
     agent_prompt_save the SAME pattern slug — agent_prompt_save versions it.
   - Only create a NEW slug when no existing pattern fits. NEVER mint a
     near-duplicate: a differently-named clone of an existing shape.
   - Call agent_prompt_save(directory="{directory}", project="{project}",
     pattern=<kebab-task-shape>, content=<the prompt>, purpose=<one line>) — same slug to extend a match,
     a new slug only when genuinely new.

4. SUBAGENT FINDINGS CURATION (always run; findings sit in on-disk
   transcripts until you curate — nothing is auto-stored).
   - LIST (Bash): yadgar pending-findings --transcript-path "<session transcript_path>" --cwd "{directory}"
     Returns per completed subagent: agent_type + "## Yadgar findings" bullets + transcript_path.
     Empty output → nothing pending; skip the rest.
   - JUDGE each bullet (precision over recall, same bar as ADR/wiki capture). Exactly one of:
       durable decision            → adr_add (step 1 rules)
       repo structure/convention   → wiki_add / update owning page (step 2)
       reusable dispatch prompt     → agent_prompt_save (step 3)
       useful working fact          → memorize(content=REWRITTEN in your words,
                                        context="{directory}", project="{project}",
                                        tags=[...])
                                        — never store the raw bullet verbatim
       noise/status/one-off/dup     → DISCARD (do nothing)
     Dedup vs what you wrote this checkpoint + existing ADRs/wiki. Storing nothing is valid + common.
   - CLEANUP: for EACH listed transcript_path:  rm -f -- "<transcript_path>"   (the /tmp .output SYMLINK
     only — never rm under ~/.claude/projects). Then: yadgar pending-findings --advance-state
     --transcript-path "<session transcript_path>" (batch-advance all just-listed).

5. TASK-LIST MIRROR (always). Persist your Claude Code harness task list to
   the task ledger so it survives session exit / /clear. The ledger is the
   source of truth (0047 spine train Car E); the legacy wiki task-list page
   is read-only marker-only.
   - Step 5a — RECONCILE YOUR OWN LIST FIRST. Call TaskList. TaskUpdate anything
     completed or blocked this session. Do NOT TaskCreate a follow-up you
     discovered — TaskCreate cannot be told which id to use, so a harness-first
     create never reconciles with its ledger row; new work is created in the
     ledger instead, at item 4 below.
     This is the every-checkpoint "update your task list" pass — do it before you
     mirror, so the ledger reflects reality.
   - Step 5b — WRITE BACK ONLY WHAT YOU TOUCHED. Other instances may be working
     this same ledger right now, so a blind diff-and-push clobbers their work.
     Only you know which rows you touched this session.
     1. READ THE LEDGER FOR REAL: CALL task_list(project_id="{project}",
        status=["pending", "in_progress"]) NOW and reconcile against what it
        RETURNS — never against a remembered copy. It answers with id, title
        and status only; that is all this step needs. D37 default is open-only
        (status ∈ {pending, in_progress}); closed/archived require an explicit
        filter. Absent = no saved ledger yet.
     2. Compare it against your harness TaskList.
     3. For EACH task whose status YOU changed this session:
          task_write(project_id="{project}", id=<ledger id>, title=<title>,
                     status=<new status>)
        Send only what changed. Fields you omit are left unchanged by the
        backend — do NOT read or re-send state, active_form, plan_path or
        body_slug to "preserve" them. project_id is a caller parameter
        (ADR-0202) — the same {project} on every call this step.
     4. For EACH follow-up you discovered this session that is NOT already in
        the list you read at item 1, create it in the LEDGER ONLY — never
        TaskCreate it first:
          task_write(project_id="{project}", title=<title>, status="pending")
        No id — the id it RETURNS is the ledger id, and the task comes back
        under that id next session. Harness-first is what this replaces:
        TaskCreate mints its own number, a subject cannot carry a ledger id that
        does not exist yet, and next checkpoint the row still reads as newly
        created and gets mirrored AGAIN — a duplicate ledger row per checkpoint
        unless you dedup by title, which nothing here tells you to do. COST,
        plainly: a task created here does NOT show in your harness list until
        the next session opens; you carry it in context until then. That is the
        price of the two ids agreeing.
     5. A row that changed in the ledger but NOT in your harness list is another
        instance's work. LEAVE IT ALONE.
   - Step 5c — TASK CONTEXT MAINTENANCE, same rule: only tasks YOU worked on.
     A task row is a pointer; the substance lives in the wiki body page named by
     its body_slug. When the row moves and the body does not, the context rots.
     The step-5b list does not carry body_slug or plan_path — read the full row
     with task_get(project_id="{project}", id=<id>), one call per task you
     worked on. Never widen the LIST to fetch them for rows you did not touch.
     - body_slug set → append what CHANGED THE APPROACH (root cause found,
       decision taken, dead end ruled out, new dependency) via
       wiki_append_section(slug=<body_slug>, ...). APPEND — never rewrite a body
       page from memory of it.
     - body_slug null AND the task now has real substance → write the page at
       {project with / as _}_task-<id>, then task_write(project_id="{project}",
       id=<id>, title=<title>, body_slug="<that slug>").
     - A design doc was written for the task → task_write(..., plan_path=<path>).
     - The doc at plan_path was deleted, superseded, or no longer governs →
       task_write(..., clear_plan_path=True). Never leave a path pointing at a
       file that is not the plan, and never "clear" a column by passing "" —
       that stores an empty string, and the body_slug form of it violates a
       unique index the second time you do it.
     - The title no longer describes the task → send the corrected one; title is
       written on every update.
     Do NOT audit or repair rows you did not work on.
   - Step 5d — CATCH-UP SYNC, and ONLY when your harness list is EMPTY while the
     ledger has open rows (a missed session-start seed, or a concurrent
     session's work). This is the one branch that needs the wide read: call
     task_list(project_id="{project}", status=["pending", "in_progress"],
     verbose=True) so the rows carry updated_at. Adopt its OPEN tasks into your
     harness via TaskCreate. GUARD: never adopt a completed task; if ALL ledger
     tasks are completed, OR the ledger's updated_at is older than 14 days, do
     NOT adopt — note "stale/finished saved list" and leave it. That age gate is
     NOT about completed rows (the status filter already excludes those): it
     catches the row that was finished but never marked completed — one that
     simply went quiet. Adopt by judgment: open tasks relevant to the work you
     are about to do; skip ones clearly from a finished or unrelated effort.
   - A harness subject carries the CURRENT ledger id and nothing else:
     `41: title`. The SessionStart seeder writes the ledger id AS the harness
     id, so there is nothing to reconcile — no `[N]` wrapper, no retired
     pre-migration id (`0047`, `task:0080`), no re-sequenced number of your own.

6. Call project_brief("{directory}", mode="signals", project="{project}").
   UNCONDITIONAL — this call is how you LEARN whether maintenance applies; it is
   cheap and you may never skip it. Its recommended_actions list drives step 7.
   `project=` is not optional decoration: the session-end sentinel signal is
   keyed on the project_id, so a call that omits it silently loses that action
   (yadgar will not derive one from the directory — ADR-0227).

7. MAINTENANCE — MANDATORY. You MUST work the recommended_actions list from
   step 6 to completion. It is NOT optional and NOT droppable under length, time,
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
     memory, then bootstrap_project("{directory}", content, project="{project}").
     (C5b: bootstrap_project raises without project= — this line is the one
     place the protocol used to omit it, against its own rule at the top.)
   - Any action type NOT covered above AND with no suggested_call → SKIP and flag
     it in your reply (do not improvise the mechanics).

[yadgar] Checkpoint cadence reached — capture, then continue. If you were
mid-thought, repeat your last question so the conversation continues. Resume after
/clear or session end: restore(directory="{directory}", project="{project}").
"""

# The short pointer reason emitted by main() — the only thing that changes in
# the block reason (Car B, task #74).  The actual path is determined at runtime
# by _resolve_prompt_template_path(); the format string here is a pattern test.
_REASON_PREFIX = "[yadgar] Checkpoint due. Read "
_REASON_SUFFIX = " and follow all the instructions in it."

# Compile-time assertion: the byte-equal pin must match the on-disk template.
# This is a defensive check at import time so a drift between the template and
# the pin surfaces as a MODULE-LOAD error rather than a single test failure.
assert _EXPECTED_TEMPLATE == _TEMPLATE_PATH.read_text(encoding="utf-8"), (
    "_EXPECTED_TEMPLATE does not match the on-disk template — regen required"
)


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
    # Protocol calls present (step 1 ADR capture adr_add + step 2 wiki)
    assert "adr_add(" in content
    assert "wiki_add(" in content
    # project_brief (step 6 — drives the maintenance pass)
    assert "project_brief(" in content
    # Car G (0047 §7): ADR read-first-dedup now reaches the SQL ADR ledger.
    assert "adr_list(" in content
    # Car E (0047 §16): task-list mirror now reaches the SQL task ledger.
    assert "task_list(project_id=" in content
    assert "task_write(project_id=" in content
    # Car E removed wiki_append_section from the TASK-LIST path: the ledger is
    # row-based, so section-atomic markdown edits stopped being how a task's
    # STATUS is written. Car C part 2 brings the call back for a different
    # object — the per-task wiki BODY page, whose substance is still prose and
    # must be appended to rather than rewritten. Assert the target, which is
    # what separates the two, instead of the bare tool name.
    assert "wiki_append_section(slug=<body_slug>" in content


def _identity_recovery_header(content: str) -> str:
    """Return the header comment block only (everything before the closing --> )."""
    marker = "-->"
    assert marker in content, "template lost its header comment"
    return content.split(marker, 1)[0]


def test_header_names_both_identity_failure_modes():
    """Task 423: the header used to dead-end ("say so and stop / skip").

    Two DIFFERENT failures reach the same symptom-space and need opposite
    fixes: the mint failing (no key exists) versus the registry refusing a key
    that does exist. Prose that conflates them sends the user to the wrong
    remedy, so the header must name both.
    """
    header = _identity_recovery_header(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert "MODE 1" in header and "MODE 2" in header, (
        "the header must separate the mint failure from the registry refusal"
    )
    assert "mint failed" in header, "MODE 1 must name the mint failure"
    assert "unknown project_id" in header, "MODE 2 must name the error string that identifies it"


def test_header_instructs_asking_the_user():
    """The recovery is the USER's to make — the instance must surface it."""
    header = _identity_recovery_header(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert "ASK" in header.upper(), "the header must instruct asking the user"
    # The ADR-0227 prohibition stays intact alongside the new instruction.
    assert "invent" in header, "the never-invent-a-key prohibition must survive"
    assert "ADR-0227" in header


def test_header_carries_the_verified_recovery_commands():
    """Commands are pinned against the real parser (yadgar/core/cli/project.py).

    ``project`` registers exactly two subcommands — ``seed`` (with ``--map``)
    and ``list``. A header naming anything else would be inventing a flag.
    """
    header = _identity_recovery_header(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert ".yadgar/project-id" in header, "MODE 1 needs the override-file remedy"
    assert "yadgar project list" in header, "MODE 2 needs the diagnostic"
    assert "yadgar project seed" in header, "MODE 2 needs the registration path"
    # ``DEFAULT_MAP_PATH`` in yadgar/core/cli/project.py is ``Path.cwd()`` bound
    # at IMPORT time, so a relative ``--map`` resolves against the shell's cwd,
    # not the project root the append instruction targeted. The header must
    # anchor both halves on {directory} or the two lines disagree.
    assert "{directory}/.yadgar/project-id-map.tsv" in header, (
        "the map path must be anchored on {directory}, not left relative"
    )
    assert "--map {directory}/.yadgar/project-id-map.tsv" in header, (
        "the seed command must carry the same anchored path as the append step"
    )


def test_header_says_mode_2_surfaces_mid_protocol():
    """MODE 2 is invisible when the header is read.

    The key and the ``current_project`` block are both present, so the
    discriminator (``unknown project_id``) can only appear once a scoped call
    has been made — i.e. after the instance has scrolled past this header. Left
    unsaid, the refusal reads as a transient tool error and the instance
    retries or gives up instead of recognising the mode.
    """
    header = _identity_recovery_header(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    low = header.lower()
    assert "surfaces on the first scoped call" in low, (
        "the header must say WHEN the MODE 2 signal appears"
    )
    assert "transient" in low, (
        "the header must say the refusal is not a transient error to retry past"
    )


def test_header_guards_against_an_unreachable_backend():
    """``yadgar project list`` exits 1 with an ``ERROR:`` line when the backend
    is down (cmd_project_list). Reading that as "not registered" would send the
    user to seed a registry that cannot be reached, so the header must call the
    case undetermined rather than let it collapse into MODE 2."""
    header = _identity_recovery_header(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert "ERROR:" in header, "the header must name the failure marker to look for"
    assert "UNDETERMINED" in header.upper(), (
        "an unreachable backend must be reported as undetermined, not as MODE 2"
    )


def test_template_has_substitution_header():
    """Template file has the substitution-key header block so the instance can
    derive {directory} and {project} without rendering."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "YADGAR CHECKPOINT PROTOCOL" in content
    assert "Substitute these placeholders" in content
    assert "basename of {directory}" in content


def test_adr_status_must_agree_with_its_own_text():
    """Step 1's status rule covers the UNAPPROVED case, not only the unresolved one.

    Car D1 (task 215). The step-1 KEEP clause is defined largely by negation, so
    "a conclusion we commit to" reads as licence to file anything concluded —
    including a recommendation the user has not ruled on. Measured on the live
    corpus 2026-08-20: ADR-0033 is status=`accepted` while its own decision text
    reads "PROPOSED, pending user go-ahead" and its revisit_trigger is "User
    confirms the gate fix to implement". `accepted` and `open` are BOTH binding
    tier (seed_adr_tier_subsystem: accepted|open -> binding), so the status is
    the only thing separating a ratified decision from a pending proposal in the
    default adr_list.

    The pre-existing clause only named the epistemic case ("still unresolved this
    session"), which does not describe an instance that HAS worked the answer out
    and is waiting on permission. This pins the permission case alongside it.

    NOTE ON SCOPE: this is a structural guard only — it asserts the criterion is
    present in the template and reachable inside step 1. A prompt's effect on
    model behaviour is not unit-testable, and this test claims no such assurance.
    """
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    # The pre-existing epistemic case survives — the new clause extends it,
    # it does not replace it.
    assert "A decision still unresolved this session → status: open" in content
    # The permission case: settled by you, not ruled on by the user.
    assert "user has not ruled on it" in content
    # `accepted` carries ratification, not merely choice.
    assert "means chosen AND ratified" in content
    # The concrete tripwire — the wording that must force status: open.
    assert "pending / recommended takes status: open" in content
    # Why status is load-bearing here: `open` is not a lesser tier.
    assert "both statuses are binding tier" in content
    # Reachable inside step 1 (ADR CAPTURE), not stranded after a later step.
    step1 = content.index("1. ADR CAPTURE")
    step2 = content.index("2. STRUCTURAL WRITE-BACK")
    assert step1 < content.index("means chosen AND ratified") < step2


def test_agent_prompt_step_is_read_first():
    """Step 3 (AGENT-PROMPT CAPTURE) enforces read-first — recall existing
    patterns, extend a matching slug, never mint a near-duplicate. This keeps
    step 3 consistent with the read-first shape of steps 1 (ADR) + 2 (wiki)."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    # Read-existing-first: recall the tagged agent-prompt library before saving.
    # C5 (0047 PR#40 §5) split the literal across a line and added the now-required
    # scope arguments, so the pin is on the two load-bearing fragments rather than
    # on one contiguous string — a whitespace-sensitive pin would break on every
    # reflow while a substring-free pin would stop asserting read-first at all.
    assert 'recall(type="wiki", tags=["agent-prompt"]' in content
    assert 'project="{project}") (or' in content
    # Extend a match on the SAME slug (versioning) rather than clone it.
    assert "SAME pattern slug" in content
    # Explicit no-near-duplicate guard.
    assert "near-duplicate" in content
    # New slug only when nothing fits.
    assert "NEW slug when no existing pattern fits" in content
    # The save call itself must still be present.
    assert "agent_prompt_save(" in content


def test_task_list_mirror_step_present():
    """Step 5 (TASK-LIST MIRROR) persists the harness task list to the SQL
    task ledger (Car E, 0047 §16). The wiki task-list page is gone — it is
    read-only marker-only. Asserts the step names the harness tools
    (TaskList/TaskUpdate/TaskCreate), the touched-only write-back rule, the
    catch-up branch and its 14-day age gate, the ledger read/write call
    shapes, and the `<id>: title` subject rule."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    # Named step + ledger call shapes (Car E).
    assert "TASK-LIST MIRROR" in content
    assert 'task_list(project_id="{project}"' in content
    assert 'task_write(project_id="{project}"' in content
    # Reconcile own list FIRST via the harness tools.
    assert "TaskList" in content
    assert "TaskUpdate" in content
    assert "TaskCreate" in content
    assert "RECONCILE YOUR OWN LIST FIRST" in content
    # Read-before-write (live ledger call, not a remembered copy).
    assert "READ THE LEDGER FOR REAL" in content
    # Car C part 2: the write-back is scoped to what THIS instance touched.
    # The ledger is shared across concurrently running instances, so the old
    # "MERGE + WRITE BACK" union — which re-sent every ledger row it had read —
    # would clobber another session's rows. It is gone and must not come back;
    # so is the state/active_form re-send it carried, which duplicated work the
    # backend already does (omitted fields are left unchanged).
    assert "WRITE BACK ONLY WHAT YOU TOUCHED" in content
    assert "MERGE + WRITE BACK" not in content
    assert "another\n       instance's work. LEAVE IT ALONE" in content or (
        "another instance's work. LEAVE IT ALONE" in " ".join(content.split())
    )
    # Car C part 2, second arm: task context maintenance. A row is a pointer;
    # its substance lives in the body page, and nothing used to revisit either
    # that or plan_path.
    assert "TASK CONTEXT MAINTENANCE" in content
    assert "wiki_append_section(slug=<body_slug>" in content
    assert "clear_plan_path=True" in content
    # The catch-up branch survives as the seeder's fallback.
    assert "CATCH-UP SYNC" in content
    # Catch-up guard: skip completed, all-done→skip, 14-day age gate on ledger updated_at.
    assert "never adopt a completed task" in content
    assert "ALL\n       ledger tasks are completed" in content or (
        "ALL ledger tasks are completed" in " ".join(content.split())
    )
    assert "14\n       days" in content or "14 days" in " ".join(content.split())
    assert "updated_at" in content
    # Ledger two-value status enum (D37: open-only default; closed tasks stay
    # visible only via explicit filter). The full harness enum
    # {pending, in_progress, completed} is still referenced for the
    # CATCH-UP-SYNC adoption guard ("OPEN tasks (status ∈ {pending,
    # in_progress})").
    assert "status ∈ {pending,\n     in_progress}" in content or (
        "status ∈ {pending, in_progress}" in " ".join(content.split())
    )
    # Ledger read parity: D37 default is open-only.
    assert "D37 default is open-only" in content
    # Subject format: the CURRENT ledger id and nothing else (Car C part 1 made
    # the ledger id BE the harness id, so the `[N]` reconcile wrapper — and the
    # Crockford-base32 display form it came with — have nothing left to do).
    assert "`41: title`" in content
    assert "no `[N]` wrapper" in " ".join(content.split())
    assert "Crockford base32" not in content
    # project_id is a caller parameter (ADR-0202); the step must call it out.
    assert "project_id is a caller parameter" in content
    assert "ADR-0202" in content
    # The legacy wiki-page artifacts are gone — no slug, no sanctioned writer,
    # no per-task markdown schema, no zero-pad 4-digit id discipline.
    assert "{project}-task-list" not in content
    assert "wiki_write_task_list" not in content
    assert "## task:<id>" not in content
    assert "Zero-pad each <id>" not in content
    # NOTE: this list used to include `"wiki_append_section" not in content`.
    # The retired artifact was the wiki TASK-LIST PAGE — asserted above by the
    # {project}-task-list slug and wiki_write_task_list — and back then the
    # only thing step 5 could have appended to was that page. Car C part 2
    # calls wiki_append_section on per-task BODY pages, which are current, so
    # the tool-name assertion no longer separates legacy from live; the slug
    # assertions do.


def test_new_tasks_are_created_in_the_ledger_not_the_harness():
    """Step 5 creates new work in the LEDGER only — never harness-first.

    Car D1 (task 227). Step 5a used to say "TaskCreate any follow-ups you
    discovered" and 5b item 4 then mirrored "EACH task you CREATED in the
    harness". That order guarantees divergence: TaskCreate allocates the harness
    id and cannot be told which one to use, the ledger then allocates a
    different one, and the harness subject cannot carry a ledger id that did not
    exist at TaskCreate time. Observed 2026-08-20: harness #107 against ledger
    row 215, one piece of work.

    Worse than mismatched numbers, item 4's old predicate stayed TRUE for the
    rest of the session, so the same row was eligible to be mirrored again at
    every later checkpoint — a duplicate ledger row per checkpoint unless the
    model dedups by title unprompted. The new predicate is keyed to the item-1
    ledger read, which closes that.

    Only the SessionStart seeder can choose an id (it writes
    ~/.claude/tasks/<session>/<N>.json directly), and ADR-0007 verified that is
    safe: allocation is max(file ids, .highwatermark)+1 and never reuses gaps.
    ADR-0007 also fixes the direction split this relies on — downstream
    mechanical, upstream model-mediated — so the fix belongs in this template
    and not in a hook.

    NOTE ON SCOPE: structural guard only. It pins that both halves of the rule
    are present and land in step 5. A prompt's effect on model behaviour is not
    unit-testable, and this test claims no such assurance.
    """
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    flat = " ".join(content.split())

    # --- Site 1: step 5a carries the prohibition AND its reason locally.
    assert "Do NOT TaskCreate a follow-up you" in content
    assert "TaskCreate cannot be told which id to use" in flat
    # The old harness-first mandate is gone from 5a.
    assert "TaskCreate any follow-ups you discovered" not in flat

    # --- Site 2: 5b item 4 creates in the ledger and states the cost.
    assert "create it in the LEDGER ONLY — never" in content
    # Predicate is keyed to the item-1 read, so it cannot re-fire next checkpoint
    # on a row this session already created.
    assert "that is NOT already in the list you read at item 1" in flat
    # The duplicate-per-checkpoint failure is named, hedged on the dedup caveat.
    assert "a duplicate ledger row per checkpoint" in flat
    assert "unless you dedup by title" in flat
    # The downside is stated, not hidden.
    assert "does NOT show in your harness list until the next session" in flat
    # The old harness-first predicate is gone from item 4.
    assert "For EACH task you CREATED in the harness this session" not in flat

    # --- Both sites live inside step 5, not stranded in a neighbouring step.
    step5 = content.index("5. TASK-LIST MIRROR")
    step6 = content.index("6. Call project_brief(")
    assert step5 < content.index("Do NOT TaskCreate a follow-up you") < step6
    assert step5 < content.index("create it in the LEDGER ONLY — never") < step6

    # 5d still adopts EXISTING ledger rows via TaskCreate — those ids are
    # already known, so that path is untouched by this rule.
    assert "harness via TaskCreate" in content


def test_subagent_findings_curation_step_present():
    """Step 4 (SUBAGENT FINDINGS CURATION, ADR-0156) folds subagent-findings
    curation into the checkpoint: LIST via the `yadgar pending-findings` CLI,
    JUDGE each bullet with the same precision bar as ADR/wiki capture, then
    CLEANUP the consumed /tmp .output symlink + batch-advance state. No script
    auto-stores; the main instance curates with judgment."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    normalized = " ".join(content.split())
    # Named step + the read-surface CLI (LIST).
    assert "SUBAGENT FINDINGS CURATION" in content
    assert "yadgar pending-findings --transcript-path" in content
    # Nothing is auto-stored — findings sit in on-disk transcripts until curated.
    assert "nothing is auto-stored" in normalized
    # JUDGE routes each bullet to exactly one destination; rewrite, never verbatim.
    assert "JUDGE each bullet" in content
    assert "REWRITTEN in your words" in content
    assert "never store the raw bullet verbatim" in content
    assert "DISCARD" in content
    # CLEANUP: rm the /tmp .output SYMLINK only (never under ~/.claude/projects),
    # then batch-advance dedup state.
    assert "CLEANUP" in content
    assert "rm -f --" in content
    assert "never rm under ~/.claude/projects" in normalized
    assert "yadgar pending-findings --advance-state" in content


def test_maintenance_step_is_mandatory_with_closed_allowed_skip_list():
    """Issue 2 (Car 3): the maintenance pass (steps 6-7) is MANDATORY. The
    header no longer pre-authorizes dropping maintenance under length pressure,
    step 6 is UNCONDITIONAL, and step 7 defines a closed allowed-skip list."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    # Header no longer licenses dropping maintenance under length pressure.
    assert "drop maintenance, NEVER capture" not in content, (
        "header must not pre-authorize dropping the maintenance pass"
    )
    assert "run ALL seven steps" in normalized

    # Step 6 (project_brief signals) is explicitly unconditional.
    assert "UNCONDITIONAL" in content

    # Step 7 is MANDATORY and states it is not droppable under length pressure.
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
    paraphrase from memory. Failure mode: model improvises off a short pointer.

    Car E + Car G re-routed the task-list and ADR read instructions to the SQL
    ledgers; the per-step strict-read reinforcement must still fire on the
    NEW live-call shapes (task_list, adr_list), not on the deleted wiki slugs."""
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
    # Step 1 ADR read (Car G: now adr_list, not wiki_read).
    assert "adr_list(" in content
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
    # Step 5b task-list read (Car E: now task_list, not wiki_read; the old
    # wiki_read("{project}-task-list" path is gone).
    assert "READ THE LEDGER FOR REAL" in content
    assert "never against a remembered copy" in normalized
    assert 'wiki_read("{project}-task-list"' not in content


def test_task_list_mirror_status_enum_has_no_blocked():
    """The ledger task-status enum is {pending, in_progress} for the open-only
    default (D37); the closed/archived states require an explicit filter —
    there is no separate 'blocked' status (the legacy markdown SCHEMA block is
    gone in the Car E ledger rewrite; the {blockedBy, context, modified, ...}
    fields used to live there as per-task "- key: value" bullets, but the
    ledger is now row-based and stores those fields as native columns).
    Asserting on the surviving enum text + the absence of any "status:
    blocked" example is the strongest still-applicable guard against an
    accidental reintroduction of a blocked status value."""
    content = _TEMPLATE_PATH.read_text(encoding="utf-8")
    normalized = " ".join(content.split())
    # The ledger two-value open enum must appear (D37 default).
    assert "status ∈ {pending, in_progress}" in normalized
    # There must be no "status: blocked" example anywhere in the template.
    assert "status: blocked" not in content
    # The full legacy {pending, in_progress, completed} enum (which the
    # CATCH-UP-SYNC adoption guard still references) must NOT appear — the
    # ledger two-value open enum is the canonical set.
    assert "{pending, in_progress, completed}" not in normalized


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
