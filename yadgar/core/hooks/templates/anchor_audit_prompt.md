<!-- YADGAR ANCHOR-AUDIT PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory}      = your current working directory (absolute path; the project root)
       {project}        = the session's minted project_id — the `owner/repo` value
                          emitted at SessionStart as `yadgar: project_id=<owner/repo>`.
                          It is NOT the basename of {directory}: the basename is not an
                          identity (two checkouts named `yadgar` are two projects), and
                          since C5 an identity is never derived (ADR-0227).
                          If you cannot find that line, scroll for the `current_project`
                          memory block, which carries the same value. If NEITHER exists,
                          the mint failed — say so and STOP rather than inventing a key.
-->

Yadgar anchor-audit maintenance. This is a periodic HYGIENE pass over the
project's anchor memories — retire the ones that are no longer worth surfacing
forever, so the anchor set stays small and every anchor still earns its
compaction-proof slot. This is NOT the checkpoint; do NOT run capture steps here.

READ FOR REAL: every "call X" below means CALL the named tool THIS turn and act
on the CONTENT IT RETURNS. Do not reconstruct anchor state from memory of an
earlier turn.

1. GATHER CANDIDATES — dry-run audit.
   - Call audit_anchors("{directory}", dry_run=True, *, project="{project}"). The
     return shape is:
       {
         "scanned": int,                       # how many _anchor rows were considered
         "actions": [
           {
             "id": int | [int, int],          # memory id, or [keep_id, forget_id] for merges
             "type": "forget_expired" | "merge" | "promote" | "verify_grace_expired_anchor",
             "tags": [str, ...],              # anchor tags on the row
             "rationale": str,                # why this action was proposed
             # merge-only: "similarity": float
             # promote-only: "draft": {...}  (NEVER auto-applied — user-gated)
             # optional: "skipped": True, "skip_reason": "..."
           },
           ...
         ],
         "dry_run": true,
         "applied": [],                        # empty on dry_run=True
         "cross_project_redundancy_candidates": [...],   # surfaced only, never mutated
         "anchored_by_prose_only": {"count": int, ...},
         "_truncated": false,                  # True when the MAX_ACTIONS_PER_RUN cap was hit
       }
     Note that audit_anchors is a CORE-side helper that handles the per-directory
     scan, the cosine-thresholded merge detection, the promote-candidate draft
     build, the verify_grace_expired_anchor surfacing, and the prose-only-archive
     risk census — all in one tool, all read-only when dry_run=True. The de_anchor
     primitive still exists for fine-grained, single-row retirement outside this
     protocol; the dry-run-then-apply flow below uses audit_anchors's dry_run=False
     as its single apply entry point.

2. EMPTY-LIST GATE — NO NAG. If audit_anchors returned no actions AND
   ``anchored_by_prose_only.count == 0``, this pass is INAPPLICABLE: STOP NOW,
   do nothing, and simply continue the conversation. Never invent candidates,
   never ask the user to review an empty list, never nag. An empty action set
   with no prose-only-archive risk is the healthy state, not a task.

3. JUDGE EACH CANDIDATE (semantic). For each entry in ``actions`` and each
   ``cross_project_redundancy_candidates`` entry, decide its fate from its
   ``rationale`` + ``tags`` + the anchor's content you fetched for review:
   - KEEP: the rationale is wrong or no longer applies — keep the anchor.
     Recently-accessed anchors (high access_count on the underlying row) are
     evidence it still earns its slot.
   - RETIRE: the rationale still holds — stale, superseded, one-off task notes
     that outlived their task, or facts now captured better in the wiki / an
     ADR. Old + never-accessed is the strongest retire signal.
   - PROMOTE: a "promote" action proposes a draft only. NEVER auto-applies.
     Show the user the ``draft`` (slug, title, category, suggested_tags, body,
     rationale) and ask whether to call ``wiki_add(...)`` and then ``forget(id)``.
     Hard-stop after a promote draft: the user has to opt in.
   - VERIFY_GRACE_EXPIRED: always user-gated, NEVER auto-applied — see
     ``skip_reason == "user_verification_required"``. Surface for review only.

4. SHOW THE USER + CONFIRM. Present a short table of the RETIRE candidates only
   (id · one-line summary from rationale · why retire). Ask the user to confirm
   before you change anything. Do NOT apply any action the user has not
   confirmed. If the user says keep-all, stop — nothing to do. Promote drafts
   are NOT a "RETIRE" — show them separately under their own header.

5. APPLY (confirmed only) — single audit_anchors dry_run=False call.
   Re-call audit_anchors("{directory}", dry_run=False, *, project="{project}")
   with the SAME arguments as Step 1. The tool re-scans, applies the
   forget_expired + merge deletions it deems safe (tier=semantic_immortal and
   legacy is_protected=True rows stay untouched), writes the action_log,
   bumps the epoch, and returns ``{"scanned", "actions", "applied": [...]}``.
   ONE call, not a per-id de_anchor loop. de_anchor remains as the lower-level
   primitive the audit_anchors apply path delegates to internally; if the user
   explicitly asks to retire a SINGLE anchor outside this audit flow, call
   de_anchor(memory_id) directly — it clears is_protected, resets importance
   to 0.5, and strips the _anchor / anchor:* tags so the row re-enters normal
   decay.
   - HARD-DELETE is a separate, stronger action: only if the user EXPLICITLY
     asks to delete a memory outright (not just retire it) call ``forget(memory_id)``.
     Default to the audit_anchors dry_run=False apply (gentle retire, with the
     apply-time guards); forget is destructive and irreversible.

6. TRUNCATION CHECK + RE-RUN. If the dry-run result carried ``"_truncated": True``,
   the action list was capped by ``ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN``. The first
   apply handles the visible candidates; after it returns, re-call
   audit_anchors("{directory}", dry_run=True, *, project="{project}") once more
   to confirm ``_truncated`` is now False (or to surface the next slice for the
   user's review). Repeat Step 4-5 for any new candidates until the cap is no
   longer hit. A truncated apply that exits without re-running is an INCOMPLETE
   audit — the report the user reads must not lie about completion.

7. WRAP UP. Report which memories were retired / merged (or that the user kept
   all) and how many prose-only archives remain at risk. Then continue the
   conversation — if you were mid-thought, repeat your last question so the
   flow resumes.
