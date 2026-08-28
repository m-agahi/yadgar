<!-- YADGAR ANCHOR-AUDIT PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory}      = your current working directory (absolute path; the project root)
       {project}        = the session's minted project_id — the `owner/repo` value
                          emitted at SessionStart as `yadgar: project_id=<owner/repo>`.
                          It is NOT the basename of {directory}: the basename is not an
                          identity (two checkouts named `yadgar` are two projects), and
                          since C5 an identity is never derived (ADR-0227).
                          If you cannot find that line, scroll for the `current_project`
                          memory block, which carries the same value.

     IDENTITY MISSING OR REFUSED — two distinct modes. Tell them apart, then ASK
     THE USER. Never invent, derive or guess a key (ADR-0227); asking is the
     sanctioned path precisely BECAUSE deriving one is forbidden.
       MODE 1 — no key at all (neither the banner nor the `current_project` block).
         The SessionStart mint failed: no git `origin` remote, or no repo. STOP
         this audit rather than inventing a key.
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

Yadgar anchor-audit maintenance. This is a periodic HYGIENE pass over the
project's anchor memories — retire the ones that are no longer worth surfacing
forever, so the anchor set stays small and every anchor still earns its
compaction-proof slot. This is NOT the checkpoint; do NOT run capture steps here.

READ FOR REAL: every "call X" below means CALL the named tool THIS turn and act
on the CONTENT IT RETURNS. Do not reconstruct anchor state from memory of an
earlier turn.

1. GATHER CANDIDATES — dry-run audit.
   - Call audit_anchors("{directory}", dry_run=True, project="{project}"). The
     return shape is:
       {
         "scanned": int,                       # how many _anchor rows were considered
         "coverage": {                         # task 391 — what the scan MISSED
           "scanned": int,                     # same number, restated in context
           "scanned_protected": int,
           "protected_total": int,             # protected rows this project owns
           "unscanned": int,                   # protected_total - scanned_protected
           "unscanned_reasons": {"no_anchor_tag": int,
                                 "directory_context_mismatch": int,
                                 "global_reach_not_scanned": int},
           "unscanned_sample": {<reason>: [id, ...]},   # <=10 ids per reason
           "scope_keys": {"directory_context": [str, ...], "project_id": str | None},
           # on a failed coverage query instead: {"error": str, "scope_keys": {...}}
         },
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

2. EMPTY-LIST GATE — NO NAG, BUT SAY WHICH EMPTY. If audit_anchors returned no
   actions AND ``anchored_by_prose_only.count == 0``, this pass is
   INAPPLICABLE. Before you stop, READ ``scanned`` — an empty action list has
   two causes and they are not the same fact:
   - ``scanned == 0`` — this project has NO anchor memories at all. Say
     exactly that once ("no anchors for {project} — nothing to audit"), then
     STOP. Nothing was examined, so "all healthy" would be a false report.
   - ``scanned > 0`` and ``actions`` is empty — the audit DID examine
     ``scanned`` anchors and every one of them is healthy. Say exactly that
     once ("{scanned} anchors for {project}, all healthy"), then STOP.
   Either way: do nothing, then simply continue the conversation. Never invent
   candidates, never ask the user to review an empty list, never nag, never
   repeat the line on a later turn. An empty action set with no
   prose-only-archive risk is the healthy state, not a task — but a silent
   stop that cannot distinguish "nothing to audit" from "audit ran clean" is
   the ambiguity this gate exists to remove.

2b. COVERAGE — ``scanned`` IS NOT THE WHOLE PROJECT. Whether or not you
   stopped at the gate above, read ``coverage``. ``scanned`` counts only rows
   the scan's own selector matches (``_anchor`` tag AND the audited
   directory); ``coverage.protected_total`` counts every protected row the
   project owns by either scope key. When ``coverage.unscanned > 0``, say so
   in ONE line alongside whatever else you report — the count, and the
   reasons verbatim, e.g.
       "95 of 102 protected rows scanned; 7 outside the scan
        (no_anchor_tag: 6, directory_context_mismatch: 1)".
   Do NOT act on unscanned rows — they are not audit candidates and this
   protocol proposes nothing for them. They are reported so a clean audit
   cannot be mistaken for a complete one. If ``coverage`` carries an
   ``error`` key, the coverage query failed: report that it is unknown, and
   do NOT report full coverage.

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
   Re-call audit_anchors("{directory}", dry_run=False, project="{project}")
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
   audit_anchors("{directory}", dry_run=True, project="{project}") once more
   to confirm ``_truncated`` is now False (or to surface the next slice for the
   user's review). Repeat Step 4-5 for any new candidates until the cap is no
   longer hit. A truncated apply that exits without re-running is an INCOMPLETE
   audit — the report the user reads must not lie about completion.

7. WRAP UP. Report which memories were retired / merged (or that the user kept
   all), how many prose-only archives remain at risk, and — if you have not
   already said it at Step 2b — the ``coverage.unscanned`` count. A wrap-up
   that names only what was audited reads as a statement about the whole
   project; it is not one. Then continue the
   conversation — if you were mid-thought, repeat your last question so the
   flow resumes.
