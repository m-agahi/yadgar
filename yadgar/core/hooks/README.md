# `core/hooks/` — Claude Code hook scripts

The hook scripts `install_hooks` copies into `~/.claude/hooks/` (or project
`.claude/`): PreCompact drain, SessionStart context injection, post-compact
rehydrate, PostToolUse capture, UserPromptSubmit recall, Stop checkpoint.

These run OUTSIDE the daemon as short-lived host processes — keep them
stdlib-only, fast (latency budgets are tested), and talking to the daemon
over HTTP only. They are packaged as assets; changing filenames breaks
`install_hooks_lib` expectations and existing installs.
