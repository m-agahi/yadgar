# Yadgar Claude Code Hooks

Yadgar ships hook scripts for every major Claude Code lifecycle event. The
easiest way to register them is:

```sh
yadgar install-hooks --scope global
```

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
| `Stop` | `yadgar-stop-memory-checkpoint.py` | Persist session checkpoint on Claude Code exit |

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

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `YADGAR_PORT` | `8765` | Daemon HTTP port |
| `YADGAR_MCP_AUTH_TOKEN` | (empty) | Bearer token for daemon auth |
