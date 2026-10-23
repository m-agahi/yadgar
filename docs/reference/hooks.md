# Yadgar Claude Code Hooks

Yadgar ships hook scripts for every major Claude Code lifecycle event. The
easiest way to register them is:

```sh
yadgar install --client claude-code --hooks --scope global
```

(Migrated from the legacy `yadgar install-hooks --scope global` command,
which was hard-removed in v5.166.0 — Car 7 of the opencode port train.)

This copies scripts to `~/.claude/hooks/` and writes the settings below into
`~/.claude/settings.json`.

---

## Ready-to-paste settings.json snippets

If you prefer manual registration, copy the relevant blocks into your
`~/.claude/settings.json` under the `"hooks"` key.

Replace `/HOOKS_DIR/` with the directory where you copied the hook scripts
(default: `~/.claude/hooks/`).

### All hooks combined

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/hook_runner.py' pre-compact-drain" }]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/hook_runner.py' session-start-context" }]
      },
      {
        "matcher": "compact",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/hook_runner.py' post-compact-rehydrate" }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/hook_runner.py' post-tool-capture" }]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/hook_runner.py' prompt-recall" }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/hook_runner.py' db-lockdown-check" }]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/yadgar-subagent-stop.py'" }]
      }
    ],
    "InstructionsLoaded": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/yadgar-instructions-loaded.py'" }]
      }
    ],
    "SubagentStart": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/yadgar-subagent-start.py'" }]
      }
    ],
    "FileChanged": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/yadgar-file-changed.py'" }]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [{ "type": "command", "command": "python3 '/HOOKS_DIR/yadgar-stop-memory-checkpoint.py'" }]
      }
    ]
  }
}
```

---

## Hook reference

| Event | Script | Purpose |
|-------|--------|---------|
| `SessionStart` | `hook_runner.py session-start-context` | Inject hot memories + checkpoint on session open |
| `SessionStart` (compact) | `hook_runner.py post-compact-rehydrate` | Restore context after conversation compaction |
| `PreCompact` | `hook_runner.py pre-compact-drain` | Drain pending context before compaction |
| `UserPromptSubmit` | `hook_runner.py prompt-recall` | Auto-recall relevant memories on every prompt |
| `PostToolUse` | `hook_runner.py post-tool-capture` | Capture Write/Edit/Bash/Agent actions to action log |
| `PreToolUse` | `hook_runner.py db-lockdown-check` | Block direct `docker exec` into yadgar DB containers |
| `SubagentStop` | `yadgar-subagent-stop.py` | Extract `## Yadgar findings` bullets from agent reports |
| `InstructionsLoaded` | `yadgar-instructions-loaded.py` | Inject recalled context when CLAUDE.md loads (v5.3.2) |
| `SubagentStart` | `yadgar-subagent-start.py` | Pre-populate subagent context via recall at dispatch (v5.3.2) |
| `FileChanged` | `yadgar-file-changed.py` | Mirror team_inbox JSONL → action_log; memorize PLAN_*.md changes (v5.3.6) |
| `Stop` | `yadgar-stop-memory-checkpoint.py` | Persist session checkpoint on Claude Code exit |

---

## SessionStart task-list seed

`hook_runner.py session-start-context` (implemented in
`yadgar/core/cli/hook.py`) writes the ledger's open tasks straight into the
Claude Code task store, so `TaskList` is populated at zero model cost. The
hook runs on the host, in the process Claude Code spawns, so it sees the
client's environment.

- **Trigger.** The payload carries `session_id`. The hook then sends `seed=1`.
  The daemon returns `task_nudge` and `tasks` beside `text`. The opencode plugin
  sends no `session_id`, so opencode gets the nudge inside `text` as before.
- **Store dir.** `<CLAUDE_CONFIG_DIR or ~/.claude>/tasks/<list_id>/`.
  `list_id` is `$CLAUDE_CODE_TASK_LIST_ID` when set, else `session_id`. This is
  the same rule Claude Code uses for a named, shared task list. A list id that
  is not a bare token (`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`) writes nothing.
  The hook then prints the nudge.
- **Idempotent.** A task id already on disk is left alone. A re-seed into a
  non-empty dir adds only the missing ledger ids. `.highwatermark` is raised to
  the top seeded id and never lowered.
- **Fallback.** Any guard trip (unknown file format, non-empty `.lock`, bad id,
  `YADGAR_HARNESS_SEED_DISABLED=1`) prints the hand-mirror nudge instead.
- **Compact skips the seed.** On `source=compact` the daemon returns before it
  builds the nudge or the rows. This is safe, because compaction keeps the
  session id and the task dir. Measured 2026-10-23 on local transcripts: 52 of
  52 `compact_boundary` records keep the same `sessionId` before and after, and
  156 `SessionStart:compact` records carry the transcript's own id. The
  Claude Code docs also say "Tasks persist across context compactions".
- **Resume and fork are seeded.** The daemon reads `resume` as a known source
  and coerces `fork` to `startup`. One measured `--resume` kept its session id
  and its task dir (n=1). A fork gets a new id and a fresh dir.

---

## InstructionsLoaded hook (v5.3.2)

Fires when Claude Code loads a CLAUDE.md file. Throttled: only fires on
`load_reason ∈ {session_start, compact}`. Suppresses `nested_traversal`,
`path_glob_match`, and `include` to avoid spam on every nested include.

The hook calls `/hooks/instructions-loaded` on the daemon, which runs a
lightweight recall (~3 results) derived from the filename. The daemon
response text is printed to stdout — Claude Code injects it into the model
context.

**Payload fields used:**
- `file_path` — path of the loaded instructions file
- `load_reason` — controls throttle gate

---

## SubagentStart hook (v5.3.2)

Fires when Claude Code dispatches a subagent. Reads `agent_type`, `cwd`, and
`description` (or `prompt` as fallback) from the payload. POSTs to
`/hooks/subagent-start` on the daemon, which runs `recall(description)` and
returns relevant memories + anchors.

The injected context reaches the subagent AT DISPATCH TIME — the orchestrator
does not need to prepend context manually.

**Empirical note:** SubagentStart payload schema was not verified against a
live Claude Code event as of v5.3.2. The script handles missing fields with
safe defaults. Verify the actual field names (`description` vs `prompt`,
presence of `agent_id`) from a real Claude Code run and update if needed.

**Payload fields used:**
- `agent_type` — used for context header and daemon query param
- `cwd` — project directory
- `description` (fallback: `prompt`) — used as recall query

---

## FileChanged hook (v5.3.6)

Fires when a watched file changes on disk. Registered with empty matcher (fires
on all changes); the hook script filters to two use cases:

### M1 — Agent Teams JSONL inbox mirror

Watches `~/.claude/team_inbox/**/*.jsonl`. When a peer agent appends a message,
the hook reads new lines since last call (position-tracked in daemon memory) and
writes one `action_log` entry per JSONL line with `tool=team_message` and tags
`["team-message", "from-subagent"]`. Gives the curator full visibility into
inter-agent communication.

**Payload fields used:**
- `file_path` — path to changed JSONL file
- `file_action` — only `created` / `modified` are processed; `deleted` is skipped

### Q4 — PLAN_*.md auto-memorize

Watches `**/docs/PLAN_*.md`. When a plan file is modified, reads its content
and calls `memorize()` with tags `["_plan", "plan-file"]`. Hash-deduped:
identical content on repeat calls is skipped.

**Payload fields used:**
- `file_path` — path to changed markdown file
- `file_action` — only `created` / `modified` are processed

### Matcher note

`FileChanged` uses literal filename matching per Claude Code 2026 docs (no
regex or glob). The empty matcher `""` is used — script-side `re` patterns
handle the team_inbox and docs/PLAN_* filtering.

---

## External task ledger: the yaadgaar project registry

Some projects keep their tasks in yaadgaar (`mcp__yaadgaar__*`). For them the
yadgar task ledger is frozen: SessionStart prints one ledger line and seeds
nothing, the stop-hook checkpoint has no task-list mirror step, and
`task_write` refuses. The gate is `yadgar/core/task_ledger_gate.py`.

The yaadgaar gateway's project registry decides which projects these are. The
answer travels in two files (`yadgar/core/task_ledger_registry.py`):

1. **Record (written by the yaadgaar client).** Its SessionStart hook asks the
   gateway about the session's project and writes
   `${YAADGAAR_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/yaadgaar}/registry/<key>.json`,
   where `<key>` is `urllib.parse.quote(project.casefold(), safe="")`
   (`acme/widget` → `acme%2Fwidget.json`). Body:
   `{"project": "<key sent>", "registered": true|false, "checked_at": "<ISO-8601 UTC>", "client_version": "<x.y.z>"}`.
   `registered: false` only on an explicit "the gateway registers no project"
   answer. On a network, auth or gateway fault the client writes nothing. The
   write is atomic (temp file + rename). The project key follows yadgar's mint
   rule: `.yadgar/project-id`, else the `origin` remote.
2. **Mirror (written by yadgar's host SessionStart hook).** After the mint, the
   hook copies the record's answer into `external_ledger_registry.json` in the
   yadgar data dir, which the daemon container mounts as `/data`. A missing
   record removes the mirror entry.

Every gate caller reads the mirror. A registry answer decides. No answer (no
record yet, an unreadable or malformed record) sends the gate to the fallback:
the global runtime config key `tasks.external_ledger_prefixes`. Each decision
logs one INFO line from `yadgar.core.task_ledger_gate` with the structured
field `ledger_source` set to `registry` or `fallback-prefix`.

Claude Code runs SessionStart hooks in parallel, so the yaadgaar record can
land after yadgar's hook has read it. A registry change can therefore reach the
gate one session late. On a project's first session that means the prefix
decides. The mirror file is read, changed and rewritten; two sessions that
start at the same moment can lose one entry, which falls back to the prefix
until the next session.

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `YADGAR_PORT` | `8765` | Daemon HTTP port |
| `YADGAR_MCP_AUTH_TOKEN` | (empty) | Bearer token for daemon auth |
