# PLAN — v5.10.5: SESSION_END_CAPTURE sentinel-marker pattern + SessionStart extraction

**Status:** drafted + advisor-reviewed 2026-05-29. Ready for implementation. Renumbered v5.10.5 on 2026-05-29 evening per all-drafts-get-versions rule (v5.10.2 shipped; v5.10.3 scan-fix in flight; v5.10.4 reserved for nightly cycle remaining bugs).

**Master at draft time:** bbd50fb (initial draft) → master is now post-v5.10.2 ship (f42e4d8).

**Sequencing:** v5.10.5 patch. Independent of v5.10.3 (scan script) and v5.10.4 (nightly cycle remaining bugs). No file overlap. Ships when prior patches clear.

---

## Why

Verbatim friction observed in session 2026-05-29:

1. **Short sessions never trigger the stop hook.** Stop hook fires every 25 human messages
   (`INTERVAL=25`, `stop-memory-checkpoint.py:22`). Sessions under 25 messages produce
   zero checkpoints, zero signal evaluation, zero memorize calls.
2. **`checkpoint()` isn't cross-project searchable.** Its `key_decisions` field lives in
   the `checkpoint` table keyed by directory. `recall("...")` does not search it. A
   post-session `recall("what did I decide last time")` returns nothing from checkpoints.
3. **Post-hook tail loss.** Even when stop hook fires at message 25, if the user asks one
   more question and then closes, that tail is uncaptured.
4. **Typing burden.** `update_active_work()` is the correct verb but users don't type it at
   exit time. Default verb is `memorize` but users don't think of it as a closing action.
5. **No exit affordance.** Nothing prompts the user to capture before closing.

The existing stop hook is a DUMB PIPE (`stop-memory-checkpoint.py:8`) that injects a
prompt. SessionEnd cannot do that — it is observational-only. A different pattern is needed.

---

## SessionEnd hook contract (verified 2026-05-29)

- Fires when user types `exit` or Ctrl+D.
- **Distinct from Stop hook.** Stop fires mid-session when Claude finishes responding.
  SessionEnd fires once at process termination.
- **JSON stdin:** `{"end_reason": "clear|resume|logout|other", "session_id": "...",
  "transcript_path": "...", "cwd": "..."}`.
- **Observational only.** Cannot block or inject prompts (unlike Stop which can
  `{"decision": "block", "reason": "..."}` to inject text).
- Hook script can call MCP tools synchronously before Claude Code exits.
- Transcript file path is available from stdin — the hook script can read it.
- **Bash/Python cannot do LLM synthesis** of "what was important" — no model access
  from hook context. Any synthesis must happen in a subsequent Claude session.

---

## Alternatives considered

| Option | Description | Verdict |
|--------|-------------|---------|
| **A. Sentinel marker (RECOMMENDED)** | SessionEnd writes a lightweight memory row tagging transcript path + timestamp. Next session's `restore()` surfaces it as a `recommended_action`. LLM in next session does synthesis. | Best: zero user burden, defers synthesis to LLM, no data loss. |
| B. Last-N-turns grep heuristic | SessionEnd bash script greps last N lines of transcript for patterns (tool calls, key phrases) and calls `memorize()` directly. | Brittle: stores junk, misses nuance, false positives, can't assess importance without LLM. |
| C. Lower INTERVAL 25→5 | More frequent stop hook fires. | Doesn't solve the fundamental problem: tail loss between last fire and exit. Also increases noise. |
| D. Manual `/save` slash command | User invokes before exit. | Requires behavior change. Discoverability is poor. Doesn't address friction point 4/5. |
| E. Stop-hook "save before exit?" prompt | Complex stop-hook logic that detects proximity to exit. | Unreliable heuristic for "proximity to exit". Can't distinguish pre-exit from mid-session. Complexity risk. |

**Option A wins.** It fits the existing CLAUDE.md behavioral contract ("in chat, you/Claude
only call `memorize`. Hooks fire everything else."). The sentinel adds nothing to the user's
typing burden. Synthesis happens where synthesis belongs: in the LLM during the next session.

---

## Recommended design: sentinel-marker pattern

### Overview

```
SessionEnd hook fires (daemon may be down; don't depend on it)
  → reads transcript_path, cwd, end_reason from stdin
  → if end_reason in ("clear", "resume"): exits 0 — skip
  → if message_count < SESSION_END_MIN_MESSAGES: exits 0 — skip
  → writes ~/.yadgar/session-ends/<session_id>.json  ← filesystem, no daemon
  → exits

Next session starts
  → session-start-context.py fires → calls GET /hooks/session-context
  → server scans ~/.yadgar/session-ends/*.json for unprocessed markers
  → imports each as memorize(_session_end_sentinel) → deletes file
  → project_brief(mode="signals") queries memory for _session_end_sentinel rows
  → emits recommended_action "extract_last_session_findings" per sentinel
  → Claude in new session reads transcript / last_human_turns, synthesizes findings
  → calls memorize(findings) + forget(sentinel_id) — sentinel consumed
```

### Sentinel storage: filesystem-first design

Two-stage: **SessionEnd writes filesystem marker → SessionStart imports into memory row**.

```
SessionEnd hook fires
  → writes ~/.yadgar/session-ends/<session_id>.json    ← no daemon dependency
  → exits

SessionStart hook fires (daemon is up by definition)
  → calls /hooks/session-context
  → server scans ~/.yadgar/session-ends/*.json for unprocessed markers
  → imports each as memorize() with tags=["_session_end_sentinel", "session_end"]
  → deletes the file (consumed)
```

**Why filesystem-first, not memory-row-at-exit:**

SessionEnd fires when the host process is dying — exactly when daemon stability is lowest.
SurrealDB instability is a documented operational reality (anchor [360391]: DB bloat / GC
pressure). An HTTP call to `POST /hooks/session-end` at exit-time can fail silently with no
retry path. The user loses the sentinel with no indication.

Filesystem write (`open(path, 'w')`) is atomic-by-rename, daemon-independent, survives DB
rebuild, and trivially debuggable (`ls ~/.yadgar/session-ends/`).

**The memory row is still created** — by the SessionStart import step, which runs after the
daemon is confirmed up (it's the same daemon that serves session-start-context.py). Existing
`project_brief(mode="signals")` query path is unchanged; it reads from the memory table as
designed.

### Sentinel file schema

Path: `~/.yadgar/session-ends/<session_id>.json`

**Content:**
```json
{
  "type": "session_end_sentinel",
  "version": 1,
  "cwd": "/home/max/git/yadgar",
  "end_reason": "logout",
  "ended_at": "2026-05-29T21:14:00Z",
  "transcript_path": "/home/max/.claude/projects/-home-max-git-yadgar/abc123.jsonl",
  "session_id": "abc123",
  "message_count": 7,
  "last_human_turns": ["turn text...", "..."],
  "last_touched_files": ["/home/max/git/yadgar/yadgar/server/tools/project.py", "..."]
}
```

Directory `~/.yadgar/session-ends/` created by hook if absent. Marker file is read-once:
deleted by the import step in SessionStart. If SessionStart never fires (e.g. repo never
re-opened), manual cleanup: `rm ~/.yadgar/session-ends/*.json`.

**Tags on imported memory row:** `["_session_end_sentinel", "session_end"]`

### New hook: `yadgar-session-end-capture.py`

Path: `yadgar/hooks/session-end-capture.py`
Installed: `~/.claude/hooks/yadgar-session-end-capture.py`

Hook type: `SessionEnd` in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/yadgar-session-end-capture.py"
          }
        ]
      }
    ]
  }
}
```

**`end_reason` gating:** MUST skip sentinel write for `end_reason in ("clear", "resume")`.

- `clear` = mid-session context wipe. Writing a sentinel here creates a self-referential loop:
  the same session that cleared would see "extract last session findings" on next restore,
  pointing at its own in-progress transcript. Wrong.
- `resume` = session resumed from compaction. Not a true exit; session is continuing.
- `logout` and `other` = true exits. Sentinel SHOULD be written.

```python
SKIP_REASONS = frozenset({"clear", "resume"})
if end_reason in SKIP_REASONS:
    sys.exit(0)
```

**Transcript snippet for rotation resilience:** the sentinel embeds the last N user message
bodies (default N=3) as `last_human_turns` AND the last N recently-touched file paths as
`last_touched_files` (default N=3). This survives transcript rotation — if the file
is gone when the next session opens, the next-session LLM still has partial context to
synthesize from. Bounded by `SESSION_END_SNIPPET_TURNS` env knob (default 3).

Snippet design rationale (advisor Q3): human turns have highest semantic density per byte
(encode intent). File paths are cheap and high-signal (encode "we were working on X.py").
Tool call names and assistant turns are low entropy / verbose — excluded.

**Size cap:** each turn truncated to 500 bytes; total `last_human_turns` capped at 4KB
(filesystem marker; no DB row size constraint applies). Cap implemented in
`_extract_last_human_turns()`:
```python
MAX_PER_TURN = 500   # bytes
MAX_TOTAL = 4096     # bytes total for last_human_turns field
turns_out = []
total = 0
for turn in reversed(last_n_turns):
    chunk = turn[:MAX_PER_TURN]
    if total + len(chunk.encode()) > MAX_TOTAL:
        break
    turns_out.insert(0, chunk)
    total += len(chunk.encode())
```

`last_touched_files` is extracted from `ToolUse` entries with `name in ("Read", "Edit", "Write")` —
last 3 unique file paths, newest-first. Cheap: O(N scan) of last 100 transcript entries.

Pseudocode:
```python
#!/usr/bin/env python3
"""Yadgar SessionEnd hook — writes session_end_sentinel memory.

Fires on exit/Ctrl-D. Observational only — no blocking, no prompts.
If SESSION_END_CAPTURE_ENABLED=false, exits immediately.
Skips on end_reason=clear/resume.
"""
import json, os, sys, datetime
from pathlib import Path

ENABLED = os.environ.get("SESSION_END_CAPTURE_ENABLED", "true").lower() not in ("false","0","no")
if not ENABLED:
    sys.exit(0)

data = json.loads(sys.stdin.read() or "{}")
cwd = data.get("cwd", os.getcwd())
transcript_path = data.get("transcript_path", "")
end_reason = data.get("end_reason", "other")
session_id = data.get("session_id", "unknown")

SKIP_REASONS = frozenset({"clear", "resume"})
if end_reason in SKIP_REASONS:
    sys.exit(0)

message_count = _count_human_messages(transcript_path) if transcript_path else 0
snippet_turns = int(os.environ.get("SESSION_END_SNIPPET_TURNS", "5"))
last_human_turns = _extract_last_human_turns(transcript_path, n=snippet_turns)

sentinel_dir = Path.home() / ".yadgar" / "session-ends"
sentinel_dir.mkdir(parents=True, exist_ok=True)

last_touched_files = _extract_last_touched_files(transcript_path, n=3)

record = {
    "type": "session_end_sentinel",
    "version": 1,
    "cwd": cwd,
    "end_reason": end_reason,
    "ended_at": datetime.datetime.utcnow().isoformat() + "Z",
    "transcript_path": transcript_path,
    "session_id": session_id,
    "message_count": message_count,
    "last_human_turns": last_human_turns,   # list of str, ≤4KB total
    "last_touched_files": last_touched_files,  # list of str, last 3 unique paths
}

marker_path = sentinel_dir / f"{session_id}.json"
tmp_path = marker_path.with_suffix(".json.tmp")
tmp_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
tmp_path.rename(marker_path)  # atomic on POSIX
```

### Server-side import: sentinel scan in `/hooks/session-context`

No new HTTP endpoint is needed for the hook-write path (filesystem is daemon-independent).

The existing `GET /hooks/session-context` endpoint (which fires at SessionStart) gains one
new responsibility: **scan and import** any unprocessed `~/.yadgar/session-ends/*.json` files.

Implementation in `yadgar/server/http.py` inside `hook_session_context()`:

```python
# Import any pending session-end sentinel files
sentinel_dir = Path.home() / ".yadgar" / "session-ends"
if sentinel_dir.exists():
    for marker in sorted(sentinel_dir.glob("*.json")):
        try:
            record = json.loads(marker.read_text())
            # Import into memory
            await storage.memorize(
                content=json.dumps(record),
                directory_context=record.get("cwd", "global"),
                tags=["_session_end_sentinel", "session_end"],
                importance=0.5,
            )
            marker.unlink()  # consumed
        except Exception as e:
            logger.warning(f"sentinel import failed for {marker}: {e}")
            # Leave file in place — will retry next session
```

~25 LOC addition to existing function. Retry semantics: import errors leave the file; it
will be retried at next SessionStart. After 3 failed imports (counted in a `<session_id>.retries`
sidecar or in the JSON itself), the file is moved to `~/.yadgar/session-ends/failed/` for
manual inspection. (Simple: increment a `retries` field in the JSON before re-writing.)

### SessionStart integration: `extract_last_session_findings` action

`session-start-context.py` already calls `/hooks/session-context` which calls
`project_brief(mode="signals")`. The signals path in `project.py` needs one new check:

**In `_project_brief_signals()`** (after existing checks):

```python
# Session-end sentinel check
sentinel_rows = storage._q(
    "SELECT id, content, created_at FROM memory "
    "WHERE '_session_end_sentinel' INSIDE tags "
    "AND directory_context = $dir "
    "ORDER BY created_at DESC LIMIT 1",
    {"dir": resolved},
)
if sentinel_rows:
    row = sentinel_rows[0]
    sentinel_data = json.loads(row.get("content", "{}"))
    transcript_path = sentinel_data.get("transcript_path", "")
    ended_at = sentinel_data.get("ended_at", "")
    msg_count = sentinel_data.get("message_count", 0)
    sentinel_id = storage._extract_id(row.get("id"))
    recommended_actions.append({
        "action": "extract_last_session_findings",
        "reason": f"sentinel found: ended_at={ended_at}, msg_count={msg_count}",
        "suggested_call": (
            f"# Read transcript at {transcript_path!r}, extract key decisions/findings,\n"
            f"# then call: memorize(content='...', context={resolved!r}, tags=['session-finding'])\n"
            f"# and: forget(memory_id={sentinel_id})"
        ),
        "transcript_path": transcript_path,
        "sentinel_id": sentinel_id,
    })
```

**Rotation resilience:** sentinel includes `last_human_turns` snippet (see hook pseudocode).
Even with a vanished transcript, the next-session LLM has partial context.

**Retention / stale transcript handling:**

If `transcript_path` does not exist (file rotated/deleted by Claude Code), `restore()` must
handle gracefully:

```python
# In restore() or session-start-context rendering:
if transcript_path and not Path(transcript_path).exists():
    # Transcript gone — emit degraded action with tombstone note
    action["reason"] += " [transcript_not_found — extract from memory only]"
    action["suggested_call"] = (
        f"# Transcript at {transcript_path!r} no longer exists.\n"
        f"# Call: forget(memory_id={sentinel_id})  # clean up stale sentinel"
    )
```

Auto-prune: if `SESSION_END_RETENTION_DAYS` is set (default 30), sentinels older than
this threshold are auto-deleted during vacuum. Implemented in `vacuum_now()` alongside
existing vacuum logic.

### New `recommended_actions` action type

**`extract_last_session_findings`**

| Field | Value |
|-------|-------|
| `action` | `"extract_last_session_findings"` |
| `reason` | `"sentinel found: ended_at=<iso>, msg_count=<n>"` |
| `suggested_call` | multiline instructions: read transcript → memorize findings → forget sentinel |
| `transcript_path` | absolute path to `.jsonl` transcript |
| `sentinel_id` | memory row ID for cleanup |

Claude in the new session reads this action, reads the transcript (if available), synthesizes
key decisions and findings via `memorize()`, then calls `forget(sentinel_id)` to consume the
sentinel. The sentinel is a one-shot signal: once consumed, it does not re-fire.

---

## Env knobs (I25-registered)

| Knob | Default | Type | Purpose |
|------|---------|------|---------|
| `SESSION_END_CAPTURE_ENABLED` | `true` | bool | Kill switch for entire feature |
| `SESSION_END_RETENTION_DAYS` | `30` | int | Auto-prune sentinels older than N days during vacuum |
| `SESSION_END_SNIPPET_TURNS` | `5` | int | Last N human turns to embed in sentinel for rotation resilience |
| `SESSION_END_MIN_MESSAGES` | `2` | int | Skip sentinel if session had fewer than N human messages |

All registered in `yadgar/config.py` I25 section alongside existing session knobs.

---

## Implementation slices

1. **TDD scaffolding** — `yadgar/tests/test_session_end_capture.py`:
   - `project_brief(mode="signals")` on dir with imported sentinel row emits
     `extract_last_session_findings` action.
   - Action includes `transcript_path`, `sentinel_id`, `last_human_turns`,
     `last_touched_files`.
   - No sentinel row → action absent.
   - Sentinel with missing transcript → action present with tombstone note; `suggested_call`
     is `forget(sentinel_id)` cleanup.
   - `end_reason=clear` → hook writes no filesystem marker.
   - `end_reason=resume` → hook writes no filesystem marker.
   - `message_count < SESSION_END_MIN_MESSAGES` → hook writes no marker.
   - `SESSION_END_CAPTURE_ENABLED=false` → hook exits 0, no file written.
   - Sentinel older than `SESSION_END_RETENTION_DAYS` → vacuum deletes memory row.
   - SessionStart import: unprocessed `~/.yadgar/session-ends/*.json` is imported into
     memory and file deleted.
   - Import failure leaves file for retry (retries field incremented); after 3 failures,
     moved to `~/.yadgar/session-ends/failed/`.

2. **`_build_recommended_actions()` extension** — add sentinel check. ~25 LOC.
   File: `yadgar/server/tools/project.py`.

3. **`vacuum_now()` extension** — prune expired sentinel memory rows. ~10 LOC.
   File: wherever `vacuum_now` lives (check `admin.py`).

4. **4 env knobs I25-registered** — yaml + Settings + registry.

5. **Hook script** — `yadgar/hooks/session-end-capture.py`. ~100 LOC.
   - Writes `~/.yadgar/session-ends/<session_id>.json` atomically.
   - Extract `_count_human_messages`, `_extract_last_human_turns`,
     `_extract_last_touched_files` from `stop-memory-checkpoint.py` into shared
     `yadgar/hooks/_utils.py`. Both stop hook and session-end hook import from `_utils.py`.

6. **SessionStart import step** — extend `hook_session_context()` in
   `yadgar/server/http.py`. ~25 LOC. Scans `~/.yadgar/session-ends/*.json`, imports each
   as `memorize()` with `_session_end_sentinel` tag, deletes file on success.
   (No new HTTP endpoint required — no /mcp generic endpoint exists; existing
   `/hooks/session-context` GET is the natural place.)

7. **`install_hooks` update** — add `SessionEnd` hook entry to `settings.json` injection.
   File: `yadgar/server/tools/misc.py` (or `server.py` — wherever `install_hooks` writes).

8. **MIGRATION_NOTES.md** — v5.10.3 section:
   - `install_hooks` re-run required to add SessionEnd entry.
   - New `~/.yadgar/session-ends/` directory (auto-created by hook).
   - New env knobs + defaults.
   - Note: `end_reason=clear` intentionally skipped.
   - Manual cleanup if needed: `rm ~/.yadgar/session-ends/*.json`.

9. **CHANGELOG.md** — terse entry.

---

## Acceptance criteria

- `pytest yadgar/tests/test_session_end_capture.py` green.
- `yadgar-session-end-capture.py` writes a sentinel row when `SESSION_END_CAPTURE_ENABLED=true`
  and `end_reason in ("logout", "other")`.
- Hook is a no-op for `end_reason in ("clear", "resume")`.
- Hook is a no-op when `message_count < SESSION_END_MIN_MESSAGES`.
- `project_brief(mode="signals")` on directory with pending sentinel emits
  `extract_last_session_findings` with correct `transcript_path`, `sentinel_id`,
  and `last_human_turns`.
- Missing transcript → action emits tombstone note, `suggested_call` is `forget(sentinel_id)`;
  `last_human_turns` still present (populated at write time, before rotation).
- `vacuum_now()` deletes sentinels older than `SESSION_END_RETENTION_DAYS`.
- `SESSION_END_CAPTURE_ENABLED=false` → hook is a no-op.
- `POST /hooks/session-end` returns `{"status": "ok", "stored": true}` on success.
- I13 + I23 + I24 + I25 lints green.

---

## Risks + mitigations

| Risk | Mitigation |
|------|------------|
| Transcript rotated before next session opens | Degrade gracefully: tombstone note in action, `suggested_call` directs `forget()` to clean up |
| Sentinel accumulates if user never opens a new session in that directory | `SESSION_END_RETENTION_DAYS` auto-prune in `vacuum_now()`. Default 30 days |
| Hook fires on every exit including trivial no-op sessions | `message_count` in sentinel; `extract_last_session_findings` action could gate on `message_count >= SESSION_END_MIN_MESSAGES` (default 2, new optional knob) to skip truly empty sessions |
| `end_reason=clear` self-referential loop | Gate sentinel write: skip on `end_reason in ("clear", "resume")`. First guard in hook. |
| User closes session while hook runs (daemon slow/down) | Hook writes to filesystem only — no daemon dependency. Atomic POSIX rename: either file exists (complete) or doesn't (hook killed mid-write). Even if hook is killed, no partial write. |
| SessionStart import fails (DB down, serialization error) | File left in place; `retries` counter incremented. After 3 failures, moved to `~/.yadgar/session-ends/failed/` for manual inspection. Never silently dropped. |
| Sentinel tag `_session_end_sentinel` collides with future internal tags | Leading `_` convention is reserved for internal system tags (established in v5). Fine. |
| heat-decay prunes sentinel before next session starts (unlikely — heat=0.3 + low decay) | Set `is_protected=False` but use `importance=0.3` which biases heat. If 30-day vacuum is implemented, heat decay is a non-issue for practical session gaps |
| Multiple sessions in same directory: sentinel points at wrong session | Sentinel is per-session (includes `session_id`). `project_brief` query fetches `LIMIT 1 ORDER BY created_at DESC` — always surfaces most-recent. Prior sentinels are overwritten (or accumulate until vacuum). |

---

## Estimate

~170 LOC implementation (`project.py` + `admin.py` + `http.py` new endpoint) +
~100 LOC tests + ~100 LOC hook script + shared `_utils.py` extraction + docs/migration notes.
Small train. Single agent dispatch.

---

## Sequencing relative to v5.10.2

v5.10.2 (Secret-Gate Architecture, `PLAN_V5_10_2_SECRET_GATE_ARCHITECTURE.md`) modifies
`memorize.py`, `storage.py`, and `config.py`. SESSION_END_CAPTURE modifies `project.py`,
`admin.py`, and `config.py`.

**Overlap:** `config.py` (both add I25 knobs). Not a merge conflict risk — each adds its
own section. Coordinate variable-ordering in the YAML schema section.

**Recommended order:** ship v5.10.2 first (security-adjacent), then SESSION_END_CAPTURE
as v5.10.3. Can be parallel-branched if v5.10.2 review takes >1 week.

---

## Out of scope

- LLM synthesis in the SessionEnd hook itself (no model access from hook context).
- Retroactive extraction of transcripts from sessions before this feature ships.
- Cross-session "what did I work on this week" rollup (separate feature, v5.12 candidate).
- CLI command `yadgar session-extract <transcript>` for manual extraction (v5.12 candidate).
- Lowering `INTERVAL=25` in the stop hook (separate concern; addressed in v5.10.1 plan).

---

## Advisor Consult (2026-05-29)

Three questions put to advisor after initial draft:

**Q1: Is sentinel-marker the right design vs alternatives A-E? What failure mode am I missing?**

**Q2: Sentinel as memory row vs filesystem marker — trade-off?**

**Q3: Vanished transcript failure mode — sentinel points at deleted file, weeks later.**

### Advisor response (2026-05-29)

**Q1: Sentinel-marker vs alternatives A-E — what failure mode am I missing?**

> Advisor: Can't compare A-E without seeing the table. Discriminating constraint: which
> alternatives survive daemon-down at SessionEnd? Apply that filter. Sentinel-marker (A)
> wins by elimination if it's the only daemon-independent option in the table.

**Q2: Memory row vs filesystem marker — trade-off?**

> Advisor: Not a trade-off to weigh — daemon-down at exit is a real constraint, not
> theoretical (anchor [360391]: SurrealDB instability active operational issue). HTTP POST
> at exit-time can fail silently with no retry path. Recommended: **filesystem-first, then
> import into memory at SessionStart** (which runs after daemon is confirmed up). You get
> daemon-independence for the write AND the existing memory query path for the read.
> This was the correct call. The plan was leaning the wrong way.

**Q3: Snippet richness — human turns only, or include assistant turns / tool call names?**

> Advisor: 3 human turns + last 3 touched file paths. Human turns = highest semantic density
> per byte. File paths = cheap, high-signal (encode "we were working on X.py"). Tool call
> names = low entropy, repetitive. Assistant turns = verbose, low density. If filesystem
> design adopted (Q2), 1KB cap was a DB row constraint and no longer applies — 4KB is fine.

**Q4: `end_reason=resume` skip — correct if long session compacts then exits?**

> Anchor [491682] (verified 2026-05-19): `PostCompact` fires AFTER compaction completes.
> `SessionEnd` is a separate event. A session that compacts (`PostCompact`) then truly
> exits would produce `SessionEnd` with `end_reason=logout` or `end_reason=other`, NOT
> `end_reason=resume`. Skip of `resume` is correct and conservative — `resume` means the
> session is being resumed/continued, not terminated.

### Plan updates from advisor

1. **Filesystem-first design adopted** (Q2): hook writes `~/.yadgar/session-ends/<session_id>.json`;
   server imports at SessionStart. Full §Sentinel memory schema and §New HTTP endpoint
   sections rewritten. `/hooks/session-end` POST endpoint dropped (not needed).
2. **Snippet expanded** (Q3): 3 human turns + 3 last-touched file paths; cap relaxed from
   1KB to 4KB; `last_touched_files` field added to sentinel schema.
3. **Q4 end_reason gating confirmed correct.** No plan change needed.
4. **Risk table updated**: daemon-down risk removed from write path; import retry risk added.
