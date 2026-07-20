### Context Compaction Shield

Hooks are installed automatically on startup — no manual setup needed.

- During long sessions, call `checkpoint` periodically to snapshot your working state.
- Use `anchor` to mark critical facts/decisions that MUST survive context compaction.
- After context compaction, call `restore` to reconstruct your working context.
- `checkpoint` fields: `directory`, `current_task`, `files_being_edited`,
  `key_decisions`, `open_questions`, `next_steps`, `active_errors`, `custom_context`.
- `anchor` fields: `content`, `context`, `reason` — creates protected memories with max heat.
- `restore` returns: checkpoint + anchored memories + hot context + gap detection.
