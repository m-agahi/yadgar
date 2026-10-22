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

Yadgar checkpoint. CAPTURE FIRST (steps 1-4), THEN maintenance (steps 5-6).
This is an ORDERING, not a licence to skip: run ALL six steps. Capture goes
first only because decisions and findings scroll out of context and are lost
forever, while maintenance signals re-fire next checkpoint. Ordering is NOT
permission to drop maintenance — you may not skip steps 5-6 to save length,
time, or effort. The ONLY legitimate skips are the closed allowed-skip list
spelled out in step 6, each of which means maintenance is INAPPLICABLE this
checkpoint (nothing to do), never that you chose to defer real work.

TASKS: this project's tasks live in yaadgaar (mcp__yaadgaar__*), not in the
yadgar task ledger, so this protocol has no task-list mirror step and you do
not call task_write. Track tasks with the yaadgaar tools instead.

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
     FORM: `decision` states the RULE — what holds from now on. A car, a train
     or a ledger row belongs in `context` as the incident that prompted it; it
     may be REFERENCED inside the rule but is never its SUBJECT. Title states
     the rule while `decision` restates the work → rewrite `decision`.
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
     directory="{directory}", project="{project}") AND
     agent_prompt_list(project="{project}", limit=200) — the ledger listing (the
     agent-prompt-toc page is retired). See which task-shapes already have a
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

5. Call project_brief("{directory}", mode="signals", project="{project}").
   UNCONDITIONAL — this call is how you LEARN whether maintenance applies; it is
   cheap and you may never skip it. Its recommended_actions list drives step 6.
   `project=` is not optional decoration: the session-end sentinel signal is
   keyed on the project_id, so a call that omits it silently loses that action
   (yadgar will not derive one from the directory — ADR-0227).

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
     audit_anchors("{directory}", project="{project}", dry_run=True) → review the
     actions list → audit_anchors("{directory}", project="{project}",
     dry_run=False) to apply forget/merge. (Without project= the coverage block
     falls back to the SessionStart contextvar, which is unbound on the ordinary
     stateless_http path — `coverage.scope_keys.project_id` then comes back null
     and the report loses its project_id arm. Same rule as every other call in
     this protocol.) The tool
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
