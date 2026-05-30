# PLAN — v5.10.7.1: SessionEnd sentinel filter — skip local-command tags

## BUNDLED WITH viz lighting fix

v5.10.7.2 (viz Lambert→Basic lighting fix) was absorbed into this v5.10.7.1 release. Both fixes shipped together as a single bundled hotfix. See `docs/PLAN_V5_10_7_2_VIZ_LIGHTING_FIX.md` for the absorbed plan.

---

**Status:** drafted 2026-05-30. Plan-first per I27. Patch-level (v5.10.7.1) for v5.10.7 release train. Hotfix follows v5.10.7 viz fixes ship.

**Master at draft time:** v5.10.6 LIVE; v5.10.7 viz-fixes in flight on `feat/v5.10.7-viz-fixes` (not merged yet).

**Sequencing:** v5.10.7.1 ships AFTER v5.10.7. Small isolated patch — half-hour at most. Don't fold into v5.10.7 to keep that release scope clean.

---

## Why

v5.10.6 shipped SessionEnd capture (sentinel-marker pattern). First post-ship session (2026-05-30) wrote 2 valid sentinels at 13:09 + 13:10 with `end_reason=other`, `message_count=7`, transcript path + session_id intact. **Mechanism works.**

BUT `last_human_turns` filter (the heuristic that grabs the last few user-message snippets to embed in the sentinel) leaks slash-command output into the human-turn stream:

- ✅ Skipped today: `<system-reminder>`, `<command-message>`
- ❌ NOT skipped today: `<local-command-caveat>`, `<local-command-stdout>`

Effect: real human turns like `"resotre"` (verbatim user typo) survive but get buried among `<local-command-caveat>` and `<local-command-stdout>` text blocks that Claude Code emits as part of slash-command processing (e.g. `/mcp`, `/model`, etc.). The sentinel's "last human turns" field becomes ~80% noise, ~20% real signal.

Downstream impact:
- `extract_last_session_findings` returns noisy snippets — LLM (Claude in next session) has to wade through unrelated `MCP dialog dismissed` / `local-command-stdout` text
- LLM may misinterpret slash-command output as user intent and call wrong follow-up tools
- The whole point of `last_human_turns` is to give the next session a fast clue about what the user was doing; noisy turns defeat that

## Goals

1. Extend the skip set in `yadgar/hooks/session-end-capture.py` `last_human_turns` filter to cover ALL Claude Code slash-command output tags.
2. Re-test post-fix that real human turns now dominate the sentinel's `last_human_turns` field.
3. Add unit test pinning the skip behavior so future regressions are caught.

## Non-goals

- Re-processing existing sentinels (the noisy ones in `~/.yadgar/session-ends/` from 2026-05-30). User can `forget` them manually if desired. No backfill / re-extraction tool needed.
- Tuning `INTERVAL` (message count threshold) — separate concern.
- Changing the sentinel schema. Pure filter logic change.

## Approach

### Identify slash-command tags Claude Code emits

Known from observation (this session + others):
- `<system-reminder>` ← already skipped
- `<command-message>` ← already skipped
- `<command-name>` ← also slash-command, likely needs skip
- `<command-args>` ← slash-command, likely needs skip
- `<local-command-caveat>` ← **missing, leaks today**
- `<local-command-stdout>` ← **missing, leaks today**
- `<local-command-stderr>` ← potential (not observed today but symmetric with stdout)

Plan: extend skip set to cover all `<command-*>` / `<local-command-*>` variants generically. Simplest robust approach: regex match `<(local-)?command(-[a-z]+)?>...</(local-)?command(-[a-z]+)?>` blocks → strip before scanning for human content. OR explicit allow-list of tags to skip:
```python
SKIP_TAGS = {
    "system-reminder",
    "command-message",
    "command-name",
    "command-args",
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
}
```

Recommend the allow-list approach (explicit, greppable, no regex surprises). Files dropped from skip set later if proven needed.

### File + line

`yadgar/hooks/session-end-capture.py` lines ~92-93 (per user's report). Verify exact line during impl. The skip set should be a module-level constant for testability.

### Tests (red-first per TDD rule)

1. `test_last_human_turns_skips_local_command_caveat` — fixture transcript with `<local-command-caveat>` block; assert it's filtered out.
2. `test_last_human_turns_skips_local_command_stdout` — same for stdout.
3. `test_last_human_turns_skips_local_command_stderr` — same for stderr.
4. `test_last_human_turns_skips_command_name_and_args` — same for `<command-name>` and `<command-args>`.
5. `test_last_human_turns_preserves_typo_human_message` — fixture has `"resotre"` between slash-command blocks; assert "resotre" survives.
6. `test_last_human_turns_count_unchanged_when_no_slash_commands` — fixture without slash-command tags returns same number of turns.

## Acceptance

- All 6 tests green
- Existing v5.10.6 SessionEnd tests still pass
- Manual smoke: write a sentinel after slash-command-heavy session → `last_human_turns` contains real prompts, not slash-command noise
- Pre-commit hooks pass
- CHANGELOG.md + MIGRATION_NOTES.md v5.10.7.1 sections added

## Open questions

1. Should the regex generic approach be used instead of allow-list? Lean allow-list. If Claude Code ever adds a new `<*-command-*>` tag pattern, we add it. Easier to audit than regex surprises.
2. Should `<command-name>` + `<command-args>` be skipped even though they weren't reported leaking today? Lean YES — symmetric with `<command-message>` (already skipped), known slash-command meta-tags.
3. Should we offer a `--reprocess-sentinels` CLI to fix already-written noisy sentinels in `~/.yadgar/session-ends/`? Skip in v5.10.7.1 scope; not enough volume to justify. If user accumulates dozens of bad sentinels, add later.

## Dependencies

- v5.10.6 shipped (provides sentinel mechanism)
- v5.10.7 shipped (sequencing — don't disturb in-flight viz release)
- No backend changes
- No schema changes

## Risk + rollback

| Risk | Mitigation |
|---|---|
| Skip set over-broad → silently drops a real human turn | Tests pin behavior on each tag explicitly |
| Existing noisy sentinels in user's queue confuse next extract_last_session_findings | Document in MIGRATION_NOTES that user can manually forget noisy sentinels; future smoke-test confirms quality |

Rollback: revert the hook script change. Sentinel file format unchanged.

## Files to modify

- `yadgar/hooks/session-end-capture.py` — extend SKIP_TAGS set (or equivalent)
- `yadgar/tests/test_session_end_capture.py` — add 6 tests
- `CHANGELOG.md` — v5.10.7.1 entry
- `MIGRATION_NOTES.md` — v5.10.7.1 section
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — 5.10.7 → 5.10.7.1

## Effort

~½ hour code + tests. Hook copy still runs at `nix-apply` so deploy chain is same as any v5.10.x patch.
