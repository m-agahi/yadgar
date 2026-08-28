<!-- YADGAR CHECKPOINT PROTOCOL
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
