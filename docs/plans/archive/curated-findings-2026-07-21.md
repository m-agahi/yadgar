# LLM-Curated Subagent Findings (ADR-0156)

- status: **APPROVED — building** (user GO 2026-07-21)
- version: core **5.159.0**, core-only (no backend bump)
- branch: `feat/curated-findings` off origin/master @ v5.158.0
- supersedes: #87's mechanical auto-store (shipped v5.158.0 but inert — SubagentStop never fires + the #96 branch bug meant 0 stored, so nothing to clean up)
- ADR: **ADR-0156**. Tasks: #98 (this build), #97 (exit-hook audit owns straggler consumption).

## Problem
#87 made a script (`_run_subagent_sweep`) sweep subagent `.output` transcripts, extract the `## Yadgar findings` footer, and POST bullets to `/hooks/subagent-stop` → `memorize()` each VERBATIM. User rejected: verbatim auto-store of agent bullets piles up garbage. "No raw data in the DB."

## Design (ADR-0156)
The Stop-hook **checkpoint** cadence (existing ~25-human-msg prompt) becomes the curation vehicle — a DUMB PIPE that injects a prompt → the MAIN INSTANCE reads pending subagent findings, CURATES them with judgment via its own MCP tools, then CLEANS UP the consumed `.out` files. No script writes raw findings to the DB. Mechanical raw writes allowed ONLY at pre-compact drain + SessionEnd exit (no LLM turn there).

## Verified facts (v5.158.0)
- `.out` files are **symlinks**: `/tmp/claude-<uid>/<slug>/<session-uuid>/tasks/<agentId>.output` → `~/.claude/projects/<slug>/<session-uuid>/subagents/agent-<id>.jsonl` (canonical). Cleanup = unlink the **/tmp symlink only**, never the target.
- Glob `/tmp/claude-*/*/<session-uuid>/tasks/*.output` (findings_capture.py:341) matches the real layout.
- Pre-compact drain is **independent** of `/hooks/subagent-stop` (uses `/hooks/pre-compact` → backend). So the endpoint + #30 counters can be fully ripped.
- The collector is **host-side disk I/O** (globs `/tmp/claude-*`, reads `~/.claude/...`). The daemon runs in a container that can't see those paths → the read surface MUST be a host-side CLI, NOT an MCP tool.

## 1. Read surface — CLI `yadgar pending-findings`
New `yadgar/core/cli/pending_findings.py`, registered in `yadgar/__main__.py`.
```
yadgar pending-findings --transcript-path <path> [--cwd <dir>] [--json] [--advance-state]
```
- Calls salvaged collector `collect_pending_findings(transcript_path, cwd, state_path) -> list[dict]` in `findings_capture.py` (repurpose of `sweep_subagent_transcripts`, read-only, no POST).
- Returns `[{agent_type, findings[], transcript_path}]` — one per new/changed transcript with a `## Yadgar findings` footer, filtered by the dedup state (path→mtime).
- Collector does NOT advance state; `--advance-state` (batch, advances all just-listed paths) does — so a crash between LIST and CLEANUP re-surfaces next cadence.
- Salvage: `extract_findings`, `last_assistant_text` (+ `_parse_transcript_line`/`_extract_content_from_msg`), `_load/_save_sweep_state`, `_session_uuid_from_transcript`, `_tasks_root_default`, the glob loop — minus `post_findings`.

## 2. `stop_checkpoint_prompt.md` — new CURATE step (folded into checkpoint)
Insert as step 3.5 (after AGENT-PROMPT CAPTURE, before TASK-LIST MIRROR); bump the "CAPTURE FIRST (steps 1-4)"/"steps 5-6" counters accordingly.
```
3.5. SUBAGENT FINDINGS CURATION (always run; findings sit in on-disk
     transcripts until you curate — nothing is auto-stored).
   - LIST (Bash): yadgar pending-findings --transcript-path "<session transcript_path>" --cwd "{directory}"
     Returns per completed subagent: agent_type + "## Yadgar findings" bullets + transcript_path.
     Empty output → nothing pending; skip the rest.
   - JUDGE each bullet (precision over recall, same bar as ADR/wiki capture). Exactly one of:
       durable decision            → adr_add (step 1 rules)
       repo structure/convention   → wiki_add / update owning page (step 2)
       reusable dispatch prompt     → agent_prompt_save (step 3)
       useful working fact          → memorize(content=REWRITTEN in your words,
                                        context="{directory}", tags=[...], branch_hint="{default_branch}")
                                        — never store the raw bullet verbatim
       noise/status/one-off/dup     → DISCARD (do nothing)
     Dedup vs what you wrote this checkpoint + existing ADRs/wiki. Storing nothing is valid + common.
   - CLEANUP: for EACH listed transcript_path:  rm -f -- "<transcript_path>"   (the /tmp .output SYMLINK
     only — never rm under ~/.claude/projects). Then: yadgar pending-findings --advance-state
     --transcript-path "<session transcript_path>" (batch-advance all just-listed).
```

## 3. Rip list
- **findings_capture.py:** delete `post_findings`; repurpose `sweep_subagent_transcripts` → `collect_pending_findings` (return list, no POST/state-write); drop `detect_branch_from_cwd`/`_sidechain_git_branch` from the collector path (delete unless a retained test imports).
- **stop-memory-checkpoint.py:** delete `_run_subagent_sweep` + its call in `main()`. KEEP `_MAINTENANCE_ITEMS` scheduler + the `checkpoint` item (curation folds into it). `_subagent_sweep_state_path` stays (reused as the CLI dedup state; consider moving to `_shared/paths.py`).
- **server/http.py:** remove `hook_subagent_stop` endpoint + registration (`server/__init__.py`) + route. Obviates #96. Drain uses `/hooks/pre-compact` — unaffected.
- **#30 counters:** DROP (`yadgar_subagent_stop_posts_total`, `yadgar_subagent_captures_total`, capture-rate gauge, `_record_subagent_post`/`_record_subagent_captures`). No observability lost — curation goes through memorize/wiki/adr which have their own metrics.
- **Legacy SubagentStop:** remove `subagent_stop.py` + `subagent-stop.py` scripts + the installer SubagentStop entry (`_hook_scripts.py`, `_settings.py`) — inert dead weight re-exporting ripped code.

## 4. Straggler safety-net (ties #97)
Collector scopes to current session-uuid → a crash before a checkpoint orphans that session's pending `.output` (next session's uuid differs). Fix (this build, ~15 lines): `session-end-capture.py` runs the collector against the exiting transcript + records `pending_findings: [{agent_type, findings, transcript_path}]` into its sentinel (mechanical, ADR-permitted for the exit hook; writes to a sentinel FILE, not the DB). **Consumption** (next-session surfacing) → deferred to #97.

## 5. Cars
- **Car A (additive):** `collect_pending_findings` + `yadgar pending-findings` CLI + tests. Green without touching the live path.
- **Car B (atomic swap):** checkpoint-prompt edit + rip auto-store + endpoint/metrics removal + straggler sentinel-record + remove dead SubagentStop scripts + update/delete affected tests. Must land atomically (no window auto-stores).

## TDD
Collector returns `[{agent_type,findings,transcript_path}]` (footer / no-footer / dedup); no network I/O (assert `post_findings` gone); CLI `--json` valid + `--advance-state` writes state; symlink-unlink leaves target intact; stop-hook `main()` no longer sweeps; endpoint 404/unregistered; session-end sentinel includes `pending_findings`; prompt template contains CURATE+CLEANUP text. Repurpose `test_v5_158_subagent_sweep_capture.py`, update `test_v5_158_anchor_audit_scheduler.py`, delete/adjust `test_subagent_stop_hook.py` + `test_v5_158_subagent_capture_metric.py`.

## Integration flow (corrected, per user 2026-07-21)
Cars merge into the feature branch `--no-verify` (assembly); the FINAL feature-branch push is a NORMAL push (pre-push hook runs `make e2e`); CI re-gates. `--no-verify` only for assembly, never elsewhere.

## Open (decided)
batch `--advance-state`; remove legacy SubagentStop scripts; #97 owns straggler consumption.
