# Plan: harness task-list inbound seeder (yadgar → Claude Code) — DECISION + design

Date: 2026-07-17 · Status: PROPOSED (decision required before build) · CC verified ~v2.1.142+

## Goal
Make the Claude Code harness task list populate from the yadgar `{project}-task-list`
wiki page at SessionStart. The Stop-hook OUTBOUND leg (harness → yadgar
`wiki_write_task_list`) works. The INBOUND leg (yadgar → harness) is the missing half:
today the SessionStart restore-nudge is advisory text and gets ignored — tasks live in
yadgar, harness `TaskList` stays empty.

## Verified facts (main thread checked THIS live install)
- Harness store: `~/.claude/tasks/<session_id>/<N>.json` + `.lock` (+ sometimes `.highwatermark`).
- On-disk schema (read from live files): `{id, subject, description, activeForm, status,
  blocks[], blockedBy[]}`.
- `.highwatermark` is NOT the id allocator (observed 47 tasks / max id 51 / highwatermark 4).
  Id allocation = `max(existing ids)+1` (scan-based). Do not write `.highwatermark`.
- Nudge emit site: `yadgar/core/server/http.py:905-955` (daemon builds the restore text +
  ALREADY has the open-task list). Client hook: `yadgar/core/hooks/session-start-context.py`
  (installed as `~/.claude/hooks/yadgar-session-start-context.py`) — has `session_id` + local
  `~/.claude` FS access. **Any mechanical file-write must live in the CLIENT hook, not the daemon.**

## Authoritative caution (claude-code-guide, cited docs)
- The on-disk task store is **UNDOCUMENTED / internal** — "can change at any release without
  warning." Writing it bypasses the tool contract and breaks on format churn.
- `.lock` / `.highwatermark` semantics are **undocumented** → race risk if we write while the
  harness holds the lock.
- **No sanctioned pre-populate mechanism exists.** SessionStart `hookSpecificOutput` supports
  `additionalContext` / `initialUserMessage` / `sessionTitle` / `watchPaths` / `reloadSkills` —
  but NO `tasks` field. The only documented path is `additionalContext` → model → `TaskCreate`
  (the unreliable path we're trying to escape). A `tasks` hook field is the correct fix — it is
  a FEATURE REQUEST, not shipped.
- The DOCUMENTED task-tool spec DOES include a `metadata` field — so metadata-keyed idempotency
  is plausible (our on-disk samples just didn't set it). Description-prefix is still the safer key.
- SessionStart stdin (documented): `{session_id, transcript_path, cwd, hook_event_name, source
  (startup|resume|clear|compact), model?, agent_type?, session_title?}`.

## DECISION (pick before build)
- **Option A — mechanical file writer (what the forwarded spec asked).** Guarantees tasks appear.
  Cost: writes an undocumented, race-prone store; breaks on CC releases; must be version-pinned +
  re-tested each upgrade. Bounded by a feature-flag + schema-sniff + forcing-nudge fallback.
- **Option B — forcing nudge (documented, safe).** Move the nudge to FIRST + imperative +
  enumerate the exact open tasks as ready-to-paste `TaskCreate` calls. Still advisory — the model
  can ignore it — but it's the documented surface and cannot corrupt harness state.
- **Option C — hybrid + feature request (RECOMMENDED).** Ship B now (cheap, safe). File the
  `tasks`-in-`hookSpecificOutput` feature request (the real fix). Add A LATER as an opt-in,
  feature-flagged writer ONLY if B measurably still fails. Don't lead with the fragile write.

Recommendation: **C**. A is buildable but the claude-code-guide verdict (undocumented + race +
breaks-on-release + no sanctioned path) makes it a maintenance liability to lead with. B is a
1-file, zero-risk improvement that may close most of the gap; measure before betting on A.

## Design — IF Option A (mechanical) is chosen
Client hook `session-start-context.py` (the daemon stays the open-task provider):
- Read stdin `session_id` + `cwd`; skip on `source` where inappropriate (decide: seed on
  startup/resume/clear; on compact the restore path already runs).
- Get the open tasks from the daemon (http.py already computes them at :947) — extend that
  response to return structured `{yadgar_id, subject, description, status}` per open task, not
  just the nudge text.
- `mkdir -p ~/.claude/tasks/<session_id>/` (may be absent at SessionStart).
- For each open task not already present: write `<N>.json`, `N = max(existing ids)+1`, id
  monotonic; **atomic** (`tempfile` in the same dir + `os.replace`). Prepend
  `[yadgar task NNNN] ` to `description` as the idempotency key. NEVER overwrite/renumber an
  existing file. Do NOT touch `.highwatermark`.
- IDEMPOTENCY: SessionStart fires on startup/resume/clear/compact — before writing, scan existing
  files' descriptions for `[yadgar task NNNN]`; skip any yadgar id already present. Zero dups.
- CONCURRENCY: `.lock` semantics unknown → write atomically + best-effort (if a `.lock` is held,
  either honor it or skip-and-fall-back-to-nudge; investigate its format first). Never block.
- FEATURE FLAG + FALLBACK: gate the writer behind a config flag (default OFF until proven).
  Before writing, SNIFF: does the dir/schema look as expected (read one existing file if any)?
  If not → skip the writer, emit the FORCING nudge (Option B) FIRST in `additionalContext`.
- ROUND-TRIP (Stop hook, `wiki_write_task_list` path): parse `[yadgar task NNNN]` from each
  harness task's description → map to the yadgar task by that id → UPDATE it (status), do NOT
  create a new yadgar task. Only create a fresh yadgar task for harness tasks with NO prefix.
  Prevents the seed → writeback-as-new → re-seed infinite-dup loop.
- Shared helper: one module both hooks import for the `<N>.json` schema + the `[yadgar task NNNN]`
  parse/format, so seeder and writeback agree.

### Build cars (Option A)
- Car 1: daemon returns structured open-task list (extend http.py:947 payload).
- Car 2: shared schema/id helper (format + parse `[yadgar task NNNN]`, atomic write).
- Car 3: SessionStart mechanical seeder (flag-gated, idempotent, sniff+fallback).
- Car 4: Stop-hook round-trip mapping (update-by-yadgarId, not create).

### Tests (Option A)
- empty dir → seeds ids 1..N from open yadgar tasks;
- dir with existing harness tasks → appends, no collision, no renumber;
- re-fire → zero dups (prefix dedup);
- completed yadgar tasks → not seeded;
- round-trip: seed → model marks one completed → Stop writes back by yadgarId as UPDATE →
  next SessionStart does not re-create;
- missing dir / wrong schema → writer skipped, forcing nudge emitted FIRST.

## Design — Option B (always worth doing, low-risk)
Reword the daemon nudge (http.py:947-955) to: (1) lead the SessionStart `additionalContext`
(FIRST, not buried), (2) imperative ("CALL TaskCreate now for each open task below before any
other work"), (3) enumerate each open task as a copy-ready line. Ship regardless of A.

## Open questions / risks
- `.lock` format + how the harness uses it (must resolve before A writes anything).
- Whether the harness preserves an unknown-but-tool-supported `metadata` field on disk (would
  make metadata a cleaner idempotency key than the description prefix).
- CC version pin: verified ~v2.1.142+; A must be re-tested on every CC minor upgrade.
- File the `tasks`-in-`hookSpecificOutput` feature request (the durable fix) either way.
