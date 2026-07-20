<!-- YADGAR ANCHOR-AUDIT PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory}      = your current working directory (absolute path; the project root)
       {project}        = basename of {directory}
       {default_branch} = last segment of `git -C {directory} symbolic-ref refs/remotes/origin/HEAD`;
                          fall back to "master" for non-git projects or on any git error.
-->

Yadgar anchor-audit maintenance. This is a periodic HYGIENE pass over the
project's anchor memories — retire the ones that are no longer worth surfacing
forever, so the anchor set stays small and every anchor still earns its
compaction-proof slot. This is NOT the checkpoint; do NOT run capture steps here.

READ FOR REAL: every "call X" below means CALL the named tool THIS turn and act
on the CONTENT IT RETURNS. Do not reconstruct anchor state from memory of an
earlier turn.

1. GATHER CANDIDATES.
   - Call project_brief("{directory}", mode="signals") and note anchor_count.
   - Call recall("_audit_anchors sentinel anchor hygiene", directory="{directory}",
     tags=["_anchor"], max_results=25, type="memory") to list this project's
     `_anchor` memories with their id, content, tags, age (created_at) and
     access_count.

2. EMPTY-LIST GATE — NO NAG. If there are NO `_anchor` memories for this project
   (recall returns none, or anchor_count is 0), this pass is INAPPLICABLE:
   STOP NOW, do nothing, and simply continue the conversation. Never invent
   candidates, never ask the user to review an empty list, never nag. An empty
   anchor set is the healthy state, not a task.

3. JUDGE EACH CANDIDATE (semantic). For each `_anchor` memory, decide its fate
   from its CONTENT + AGE + access_count:
   - KEEP: still true AND still useful as an always-on fact/rule (workflow rules,
     account IDs, hard constraints, recurring procedures). Recently accessed
     (high access_count) is evidence it still earns its slot.
   - RETIRE (de_anchor): stale, superseded, one-off task notes that outlived
     their task, or facts now captured better in the wiki / an ADR. Old +
     never-accessed is the strongest retire signal. Retiring does NOT delete the
     memory — it clears is_protected + resets importance so the row ages out of
     the surfacing channels naturally over months.

4. SHOW THE USER + CONFIRM. Present a short table of the RETIRE candidates only
   (id · one-line summary · why retire). Ask the user to confirm before you
   change anything. Do NOT de_anchor anything the user has not confirmed. If the
   user says keep-all, stop — nothing to do.

5. APPLY (confirmed only). For each confirmed retire candidate call
   `de_anchor(memory_id)`. That clears is_protected, resets importance to 0.5,
   and strips the `_anchor` / `anchor:*` tags so the row re-enters normal decay.
   - HARD-DELETE is a separate, stronger action: only if the user EXPLICITLY asks
     to delete a memory outright (not just retire it) call `forget(memory_id)`.
     Default to de_anchor (gentle retire); forget is destructive and irreversible.

6. WRAP UP. Report which memories you de-anchored (or that the user kept all).
   Then continue the conversation — if you were mid-thought, repeat your last
   question so the flow resumes.
