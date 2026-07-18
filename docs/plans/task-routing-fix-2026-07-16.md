# Plan: fix "add a task" misrouting to yadgar memory instead of harness task list

Date: 2026-07-16 · Status: PROPOSED · yadgar-core (project_brief + sync_instructions).
Own PR recommended (unrelated to viz PRs); user may fold into the current train.

## Contract (Fix #1 — confirmed, state it)
- Harness task list (TaskCreate/TaskUpdate/TaskList) = **source of truth**.
- yadgar `{project}-task-list` wiki (page_type task_list, canonical) = **derived mirror**,
  overwritten each checkpoint by the stop-hook via `wiki_write_task_list` (NOT read-only —
  rewritten, not hand-edited).
- `update_active_work` stores **working-state / checkpoint context** (what you're doing
  now), NOT a todo store. `memorize`/`wiki_add` = durable knowledge, not task items.

## Fix #2 — reword the project_brief nudge (HIGHEST LEVERAGE, yadgar-owned)
Root: session-start project_brief is the only task-adjacent nudge and it points at
`update_active_work` ("call update_active_work once you start a session") → reads as
"capture what you're about to do" = the exact "add a task" moment.

- `yadgar/_shared/.../project.py:216-218` (catalog/full empty-state nudge): reword. Clarify
  `_active_work` = working-state/checkpoint context; ADD a sibling line: "to track
  TODOs/tasks use the harness task list (TaskCreate) — yadgar mirrors it via the stop-hook."
- **DO NOT** globally repoint `_aw_call` (`project.py:1747`, the signals `suggested_call`)
  — it is shared by the legit stale-`_active_work` refresh action (`:1752-1753`) where
  `update_active_work` IS correct. Only the empty-state framing is wrong.
- Tests asserting nudge text (update the ~2-4 that break): `test_project_brief.py`,
  `test_project_brief_modes.py`, `test_project_brief_catalog_v5530.py`,
  `test_roadmap_update_signal.py`.

## Fix (bonus) — sync_instructions overload
`yadgar/.../misc.py:459+` line 24: "After completing any significant task, call `memorize`"
literally primes "task"→memorize. Reword to "significant work / decision / discovery"
(remove the "task" trigger word). Byte-pin test for sync_instructions body if one exists —
update it.

## Fix #3 — task-like-content soft hint: SKIP
False-positive rate too high — `_active_work`/`checkpoint.next_steps`/`memorize` content is
legitimately imperative/checkbox-shaped by design ("next: fix X", "task #19 …"). A heuristic
would fire on the majority of legit writes → alert fatigue. Fix #2 removes the cause; a
per-write hint treats the symptom + nags. Do not build.

## Fix #4 — CLAUDE.md rule (DRAFT — user maintains in nix, not a repo change)
> **HARD RULE — "task"/"todo" → harness, not yadgar.** "Add a task", "task list", "todo",
> "track this" → use the harness task tools (TaskCreate/TaskUpdate/TaskList — the in-terminal
> ☐/■ rows), NEVER `memorize` or `update_active_work`. The harness list is the source of
> truth; yadgar's `{project}-task-list` wiki page is a stop-hook-derived mirror (auto-written
> at session end, restored at session start). `update_active_work` stores working-state /
> checkpoint context (what you're doing now), NOT a todo store. `memorize`/`wiki_add` = durable
> knowledge (decisions, discoveries), not task items.

## Deferred harness Task tools (user ask — separate lever, NOT yadgar code)
Root cause #4: harness TaskCreate/List/Update are deferred behind ToolSearch while yadgar
tools are live (least-resistance = yadgar). yadgar's `anthropic/alwaysLoad` (ADR-0047) marks
ONLY yadgar's own MCP tools — it CANNOT un-defer harness built-ins. Un-deferring Task* is a
Claude Code config/harness matter (pending claude-code-guide finding). If it turns out to be
a user settings.json action, hand it over via MIGRATION_NOTES — not a yadgar code change.

## Test / version
Core-only change (project.py + misc.py + tests). Core version bump; no backend bump. I33 if
any new fn (unlikely — text edits). Run touched project_brief + sync_instructions suites
(`YADGAR_OTLP_ENDPOINT=''`, `HF_HOME` set, pytest via script file).

## Scope
Small. Own PR = cleanest (yadgar-core, no viz overlap). If bundled into the viz-rest PR per
user, land it as a distinct commit + a separate CHANGELOG line so the diff stays legible.
