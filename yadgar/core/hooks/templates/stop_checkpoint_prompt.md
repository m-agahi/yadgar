<!-- YADGAR CHECKPOINT PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory}      = your current working directory (absolute path; the project root)
       {project}        = basename of {directory}
       {default_branch} = last segment of `git -C {directory} symbolic-ref refs/remotes/origin/HEAD`;
                          fall back to "master" for non-git projects or on any git error.
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
                                        context="{directory}", tags=[...], branch_hint="{default_branch}")
                                        — never store the raw bullet verbatim
       noise/status/one-off/dup     → DISCARD (do nothing)
     Dedup vs what you wrote this checkpoint + existing ADRs/wiki. Storing nothing is valid + common.
   - CLEANUP: for EACH listed transcript_path:  rm -f -- "<transcript_path>"   (the /tmp .output SYMLINK
     only — never rm under ~/.claude/projects). Then: yadgar pending-findings --advance-state
     --transcript-path "<session transcript_path>" (batch-advance all just-listed).

5. TASK-LIST MIRROR (always). Persist your Claude Code harness task list to the
   wiki so it survives session exit / /clear. Page: slug "{project}-task-list",
   tag "task-list", scoped to this directory. Page format = the SCHEMA block at
   the bottom of this step; each task is a UNIQUE "## task:<id>" section.
   - Step 5a — RECONCILE YOUR OWN LIST FIRST. Call TaskList. TaskUpdate anything
     completed or blocked this session; TaskCreate any follow-ups you discovered.
     This is the every-checkpoint "update your task list" pass — do it before you
     mirror, so the page reflects reality.
   - Step 5b — READ THE PAGE FOR REAL: CALL wiki_read("{project}-task-list",
     directory="{directory}") NOW and reconcile against the tasks + updated_at it
     RETURNS — never against a remembered copy of the page. Absent = no saved list
     yet.
   - Step 5c — BRANCH on {have open tasks after reconcile?} × {page exists?}:
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

6. Call project_brief("{directory}", mode="signals"). UNCONDITIONAL — this call
   is how you LEARN whether maintenance applies; it is cheap and you may never
   skip it. Its recommended_actions list drives step 7.

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
     memory, then bootstrap_project("{directory}", content).
   - Any action type NOT covered above AND with no suggested_call → SKIP and flag
     it in your reply (do not improvise the mechanics).

[yadgar] Checkpoint cadence reached — capture, then continue. If you were
mid-thought, repeat your last question so the conversation continues. Resume after
/clear or session end: restore(directory="{directory}").
