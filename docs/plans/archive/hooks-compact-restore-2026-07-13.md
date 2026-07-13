# HOOKS Train — Car 2: Compact/Restore Enrichment + Correctness Fixes

**Status:** AUDITED-ready — one user decision open (car split, see `### User decisions required`). Audit fixes folded into the body: in-flight algorithm corrected (strict `async_launched` launched-set minus terminal-status completion-set), blast radius reduced to 3 required sites (no migration), HTTP threading edit added, dedup target corrected to the brief catalog, versions bumped. Bug1 + Bug2-hardening + dedup are land-ready and unit-testable now; the enrichment is validatable only by a live mid-compaction round-trip. The `## AUDIT (2026-07-13)` section is kept verbatim as the review record.
**Date:** 2026-07-13
**Scope:** core (`5.134.0`) + hook scripts; backend (`5.42.0`) touched for the transcript-parse fork. Sequence after #195.
**Author dispatch:** Explore-verified against observed source (see file:line citations). No code changed; this is a design doc only.

---

## BLUF

Post-compact restore already exists (SessionStart `matcher="compact"` → restore). This car does **not** add a new hook event. It (a) fixes two correctness bugs in the restore-inject path, (b) dedups the double catalog injection on `source="compact"`, and (c) **enriches** what compaction captures: a PreCompact transcript-parse that records in-flight orchestration state (dispatched background agent IDs, `run_in_background` shell IDs, Monitor task IDs, git worktrees) into the checkpoint, then surfaces it in `restore()`. Today that orchestration state is silently lost across every compact.

The headline enrichment's **mechanism is feasible** but its **yield is unproven on real data**. The Claude Code transcript JSONL carries a stable join key (`agentId` on the launch ack == `<task-id>` on the completion notification), so an in-flight set is *computable*. The corrected algorithm is: `in_flight = launched − terminal`, where `launched` = the strict set of `agentId`s that carry a `toolUseResult.status == "async_launched"` result (background dispatches only) and `terminal` = the set of `<task-id>`s from every `<task-notification>` block with a terminal `<status>` ∈ {`completed`, `killed`, …}. The earlier "323 launched / 303 completed / 20 in-flight" number was an **artifact of a loose `agentId`-anytext regex** that counted foreground/synchronous agents (which echo `agentId:` in a completed non-async result, emit no `async_launched`, and emit no task-notification) — all 20 of those "in-flight" IDs are foreground agents. Under the strict definition the same transcript yields launched = 303 = terminal → **in_flight = 0 by construction**: it was a session that ran to completion, so every background agent had finished before the transcript froze. That means the enrichment **cannot be unit-validated post-hoc from a completed transcript** — the only true validator is a live mid-compaction round-trip with a genuinely-live background agent (see Acceptance `[manual]`, elevated to a feasibility gate). Honest limit stands: "in-flight" means "dispatched, no terminal notification observed in the frozen transcript" — not guaranteed still-running. We surface it as "were in flight — verify," never as fact.

Enrichment stays IN scope — it is the user's headline ask (capture in-flight agent IDs / worktrees / background shells so a post-compact session can resume orchestration). Only *how it is validated* changed. **Car-split note:** the AUDIT below recommends splitting the enrichment out of the bug/dedup PR. This plan author chooses **one car** — the enrichment is the headline ask, and gating it on a passing manual mid-compaction round-trip is a sufficient guard without a second PR. The split remains the one open user decision (see `### User decisions required`, item 1); the recommendation here is one car.

Two bugs: **BUG 1 confirmed** (HTTP restore path reads wrong response key → empty post-compact injection). **BUG 2 partially confirmed** — the in-repo script is correct-ish; the *global nix* variant the original report flagged lives in a different repo and could not be verified here (open question / MIGRATION_NOTES).

---

## Current drain/restore mechanics (verified, both paths)

There are **two divergent drain/restore paths**. They must both be considered; the plan picks a canonical one for the enrichment.

### Path A — canonical, registered by `install_hooks` (HTTP via hook_runner)

This is what `install_hooks` actually writes into `settings.json`:

- `yadgar/core/install/install_hooks_lib.py:377` — `hooks_config["PreCompact"] = [_runner_entry("pre-compact-drain")]`
- `yadgar/core/install/install_hooks_lib.py:378-380` — `SessionStart = [ _runner_entry("session-start-context"), _runner_entry("post-compact-rehydrate", matcher="compact") ]`

`_runner_entry(hook_type)` = `python hook_runner.py <hook_type>`. So PreCompact and both SessionStart entries route through **`hook_runner.py` → HTTP** to the running daemon.

- **PreCompact** → `hook_runner.py:162-168` `hook_pre_compact_drain()` → reads full stdin JSON → `_http_post("/hooks/pre-compact", data)`.
  - Backend `yadgar/core/server/http.py:651-683` `hook_pre_compact()` → `body.get("cwd", os.getcwd())` → `_forward_admin("pre_compact_drain", {"directory": directory})`.
  - `yadgar/backend/admin_exec/restoration.py:18-27` → `yadgar/backend/restoration/checkpoint_restore.py:209-241` `pre_compact_drain(directory)`: increments epoch, upserts an auto-checkpoint (`session_id="auto-drain"`), returns `{status, epoch, auto_checkpoint_created}`. **Receives only `directory` — no transcript access today.**
- **SessionStart(matcher="compact")** → `hook_runner.py:147-159` `hook_post_compact_rehydrate()` → `directory = data.get("cwd", os.getcwd())` → `_http_get("/hooks/post-compact", {"directory": directory})`.
  - Backend `http.py:686-702` `hook_post_compact()` → `_forward_restore(directory)` → backend `POST /restore` → `CheckpointRestore.restore()` → returns dict with key **`formatted`** (the restore markdown).
- **SessionStart(matcher="")** → `hook_runner.py:126-144` `hook_session_start_context()` → `_http_get("/hooks/session-context", ...)` → reads key `text` (`hook_runner.py:142`).

### Path B — global nix / dotfiles (CLI via `.sh`)

Deployed by the nix dotfiles module (`dotfiles/common/yadgar-hooks/`, per memory 993, `llm.nix`), separate from this repo. In-repo copies exist at:

- `.claude/hooks/pre-compact-drain.sh` (mirror of `yadgar/core/hooks/pre-compact-drain.sh`)
- CLI: `yadgar/core/cli/drain.py:10-19` `cmd_drain` → `forward_pre_compact_drain(directory)` → `_forward_admin("pre_compact_drain", {directory})`.
- CLI: `yadgar/core/cli/restore.py:9-17` `cmd_restore` → `forward_restore(directory)` → **reads `result.get("formatted", "")`** (line 15) — **correct**.

So the CLI restore path reads the right key; the HTTP hook_runner path does not (BUG 1, below).

---

## The two bugs (evidence)

### BUG 1 — CONFIRMED. Post-compact restore inject reads the wrong response key (HTTP path)

- **Where:** `yadgar/core/scripts/hook_runner.py:157` (post-compact-rehydrate handler):
  ```python
  text = result.get("text", result.get("context", ""))
  ```
- **What backend returns:** `hook_post_compact()` → `_forward_restore()` unwraps the `{"result": {...}}` envelope (`yadgar/core/server/tools/_forward.py:157`) and returns a dict whose restore-markdown key is **`formatted`** (never `text` or `context`).
- **Effect:** On the canonical registered path, `result.get("text", result.get("context",""))` = `""`. Post-compact injection is **empty** — restore silently no-ops for every install-hooks/HTTP user. (The CLI path at `restore.py:15` reads `formatted` and works, which is why this was masked.)
- **Priority / who is biting:** per memory 993, the nix/dotfiles deployment invokes the `yadgar restore` **CLI** (Path B), which reads `formatted` correctly — so for a user on the nix hooks, BUG 1 is **latent** (bites only users whose hooks route through `install_hooks` → `hook_runner.py` HTTP). Still a real correctness bug worth fixing, but confirm the target user's deployment path when prioritizing.
- **Fix:** `text = result.get("formatted", result.get("text", result.get("context", "")))` — prefer `formatted`, keep old keys as defensive fallback.

### BUG 2 — PARTIALLY CONFIRMED (wrong-directory restore). In-repo script is correct-ish; global variant unverified

- **Original claim (about the REHYDRATE / post-compact restore script):** the global `yadgar-post-compact-rehydrate.sh` uses `CWD=$(pwd)` instead of stdin `cwd` → restores wrong/empty directory.
- **Script-name clarity — the claim is about a *different* script than the one readable here.** BUG 2 concerns the **rehydrate** (post-compact restore) path. The only cwd-handling `.sh` present in this repo is the **drain** (PreCompact) script `.claude/hooks/pre-compact-drain.sh` — a mirror of `yadgar/core/hooks/pre-compact-drain.sh`. We inspect it as the closest available proxy for the pattern, but it is NOT the script BUG 2 names.
- **What the in-repo drain script actually does** (`.claude/hooks/pre-compact-drain.sh:10-13`): reads stdin `cwd` **first**, `$(pwd)` only as fallback:
  ```sh
  CWD=$(echo "$INPUT" | python3 -c "...json.load(sys.stdin).get('cwd','')" 2>/dev/null || echo "")
  if [ -z "$CWD" ]; then CWD=$(pwd); fi
  ```
  This is the milder, correct-ish pattern — **not the `$(pwd)`-only bug the report describes**.
- **In-repo rehydrate paths are also correct:** the HTTP rehydrate handler (`hook_runner.py:150`) reads `data.get("cwd", os.getcwd())` — stdin-first, correct. So no in-repo rehydrate path exhibits the `$(pwd)`-only bug either.
- **Honesty note:** the *global nix* `yadgar-post-compact-rehydrate.sh` the original claim points at lives in the **dotfiles/nix repo, not `/home/max/git/yadgar`**, and was **not read** by this investigation. We cannot confirm or refute BUG 2 as stated. It is out of scope for a yadgar-repo PR. → **Open question + MIGRATION_NOTES item**, not a fix landed here.
- **Residual real risk (in-repo):** the `$(pwd)` fallback fires when stdin JSON is missing/malformed. If a compact fires with an unexpected payload shape, restore targets the process CWD, which for a daemon-adjacent hook may not be the project dir → wrong/empty restore. Low-cost hardening: log a warning when the fallback triggers, and prefer failing silent over restoring a wrong directory.

---

## The schema gap

Checkpoint model (`yadgar/_shared/contracts/models.py:328-342`) has 13 contract fields:
`id, session_id, directory_context, current_task, files_being_edited, key_decisions, open_questions, next_steps, active_errors, custom_context, epoch, created_at, is_active`.

- `resume_hint` is written to the DB row (`yadgar/_shared/storage/ops.py:636`) but is **not** in the Pydantic model — DB-only, write-only helper.
- **No field** for: in-flight agent IDs, worktree paths, background shell IDs, Monitor task IDs. Orchestration state is **not round-trippable** through a checkpoint today. This is the gap this car closes.

`custom_context` (`str`, no size cap) is enriched at write time by `_enrich_checkpoint_context()` (`yadgar/backend/write_exec/checkpoint_impl.py:67`) which appends an action-buffer summary — **but only on the MCP checkpoint write path** (`run_checkpoint_replay` → `create_checkpoint`). The drain write that carries `in_flight` (`pre_compact_drain`) calls `insert_checkpoint` **directly** (`checkpoint_restore.py:224`), bypassing `checkpoint_impl.py` entirely. So there is **no collision** between the new `in_flight` field and `_enrich_checkpoint_context` on the drain path — the audit's original collision concern does not apply, and `checkpoint_impl.py` is **not** a required edit site.

---

## PreCompact transcript-parse design

### Feasibility — mechanism verified; yield unproven (validate by live round-trip)

**Premise — VERIFIED.** Claude Code passes `transcript_path` on the PreCompact stdin payload (confirmed against code.claude.com/docs/en/hooks, 2026-07-13). Full PreCompact payload: `session_id, prompt_id, transcript_path, cwd, permission_mode, effort, hook_event_name`. `transcript_path` points to the session's JSONL and is hook-readable. The in-repo PreCompact code (`hook_runner.py:162-168`, `pre-compact-drain.sh`) currently reads only `cwd` and discards the rest — but `transcript_path` is present in the payload to thread through. **Caveat (per docs):** the transcript file may lag in-memory state at hook-fire time — it may not include the current turn's most recent messages (see Risk 7). The JSONL carries:

- **Agent dispatch launch ack** (a `tool_result` entry): text `agentId: <id> (internal ID ...)` and/or top-level `agentId`; `status: "async_launched"` for background dispatches.
- **Completion notification** (later message): `<task-notification>...<task-id><id></task-id>...<status>completed</status>...`.
- **Join key:** the launch `agentId` **equals** the completion `<task-id>` (both `a` + hex, 17 chars).

**Corrected algorithm** (both defects below were folded in from the audit; the earlier version was WRONG on two counts — foreground pollution in `launched`, and a `completed`-only terminal filter):

```
launched   = { agentId  from every toolUseResult with status == "async_launched" }
                # STRICT: background dispatches ONLY. A bare `agentId:` token in a
                # completed non-async tool_result (foreground/synchronous agent) is
                # NOT a launch — foreground agents never emit async_launched and never
                # emit a <task-notification>, so counting them inflates the set.
terminal   = { task-id  from every <task-notification> block whose <status> is
                terminal ∈ {completed, killed, …} }
                # NOT completed-only: killed agents ARE done. A completed-only filter
                # falsely marks killed agents as still-in-flight.
in_flight  = launched − terminal
```

Two separable defects the earlier draft had, now fixed:
1. **Foreground pollution** — `launched` must be the strict `async_launched` set, not any `agentId` token. Foreground agents echo `agentId:` in their completed non-async result and are otherwise absent from the notification stream; counting them fabricates in-flight entries.
2. **`killed` mis-flagged** — `terminal` must include every terminal `<status>`, not just `completed`. Killed agents finished; excluding them counts them as never-completed.

**Empirical status — the yield is UNPROVEN, not "verified."** On a real completed repo transcript (`~/.claude/projects/-home-max-git-yadgar/<uuid>.jsonl`, 1640 notifications) the join key is exact (`agentId` ↔ `<task-id>`, both `a`+hex, 17 chars) — that much is verified. But under the strict algorithm `launched = 303 = terminal → in_flight = 0`. The earlier "323 launched / 20 in-flight" figure came from the loose `agentId`-anytext regex; all 20 of those extra IDs are foreground agents (verified at full n=20: each has a synchronous `toolUseResult` with `isAsync != True, status == "completed"`). On any session that ran to completion, in-flight is **0 by construction** — every background agent finished before the transcript froze. Therefore the enrichment **cannot be validated post-hoc from a completed transcript**; the sole real validator is a live mid-compaction round-trip (Acceptance `[manual]`, promoted to a feasibility gate). The mechanism is feasible; its practical yield is untested until that round-trip runs.

### Extraction scope

| Signal | Source in JSONL | Extractable? |
|---|---|---|
| Background subagent IDs | launch ack `agentId` ↔ completion `<task-id>` | **Yes** (verified) |
| Foreground subagent IDs | block; final output in `tool_result`, never in-flight | N/A — excluded by definition |
| `run_in_background` bash shell IDs | `tool_use` `name="Bash"`, `input.run_in_background=true`; shell/task id in result | **Likely** — same launched/completed pattern; VERIFY shell-id key on first impl (sample had no bg-bash instance) |
| Monitor task IDs | `tool_use` `name="Monitor"` | **UNVERIFIED** — Monitor is a deferred tool; no transcript sample exists. Parse defensively; emit nothing if absent |
| Git worktrees | not in transcript — from `git worktree list` at drain time | **Yes** (shell out) |

### Liveness honesty (bounds the design)

From the frozen transcript alone we cannot prove an agent is *still running* — only that no completion was observed **in the transcript at freeze time** (it may have finished after the last written line, or written completion outside the transcript). Therefore:

- Capture **all** dispatched IDs + worktrees.
- Restore surfaces them as **"were in flight at compaction — verify liveness"**, never "still running."
- No attempt to poll or confirm liveness from the script (would need a live process registry we don't have).

### Design fork — WHERE to parse (resolved: backend-side)

Two options; **the plan picks (B)** to satisfy the "one canonical path" constraint:

- **(A) Parse in the hook script.** Duplicates parse logic across `hook_runner.py` (HTTP), `.sh`/`yadgar drain` (CLI), and the global nix variant. Rejected — three copies drift.
- **(B) Thread `transcript_path` to the backend; parse once.** ✅
  - `hook_runner.py:168` already POSTs the full stdin `data` (includes `transcript_path`) to `/hooks/pre-compact`, so the **hook_runner script needs no change** to deliver it — **BUT `http.py:hook_pre_compact()` DOES need an edit.** Today `http.py:660` extracts only `directory = body.get("cwd", …)` and `:670` forwards `{"directory": directory}`, **dropping `transcript_path`**. The thread-through edit is required here: read `transcript_path = body.get("transcript_path")` and forward `{"directory": directory, "transcript_path": transcript_path}`.
  - `pre_compact_drain(directory, transcript_path=None)` (backend, `checkpoint_restore.py:209-241`) parses the JSONL, computes `in_flight`, runs `git worktree list`, writes the result into the checkpoint.
  - CLI parity: `cmd_drain` / `forward_pre_compact_drain(directory, transcript_path)` — add optional arg; the `.sh` must pass its stdin `transcript_path` too.

  **Blast radius (B):** HTTP body field (`http.py:hook_pre_compact` — read + forward `transcript_path`, REQUIRED), CLI signature (`drain.py`, `_shared.forward_pre_compact_drain`), backend fn signature (`checkpoint_restore.pre_compact_drain`), backend admin wrapper (`admin_exec/restoration.py`), and `.sh` stdin passthrough (Path B). All additive/optional-arg — `transcript_path=None` degrades to today's behavior.

### Storage — new field vs `custom_context` (resolved: new `in_flight` field)

- **`custom_context` (str):** zero schema change, but fragile — free-form text, no validation, restore would have to string-parse a JSON blob back out. Rejected for the structured payload.
- **New `in_flight` field (chosen):** structured, cleanly formatted in restore. **No DB migration** — SurrealDB `CREATE … SET` is schemaless; `resume_hint` already writes to the checkpoint row (`ops.py:625,636`) without being in the Pydantic model and without any migration, which proves a new field lands the same way. **Blast radius = 3 required + 2 optional:**

  **Required (3):**
  1. `checkpoint_restore.py:210` (`pre_compact_drain`) — parse the transcript + add `in_flight` to the dict passed to `insert_checkpoint`.
  2. `ops.py:630` — add `in_flight = $in_flight` to the `CREATE … SET` clause and `"in_flight": data.get("in_flight")` to the params dict.
  3. Restore formatter (`_format_restoration`, `checkpoint_restore.py:399`/`477`) — read `checkpoint.get("in_flight")` from the raw `SELECT *` dict (no model field needed to round-trip).

  **Optional (2, typing/validation only — NOT on the drain write path):**
  4. `models.py:328-342` — add `in_flight: dict | None = None` (or a typed submodel) to `Checkpoint`. Nice for typing; add only if a typed consumer appears.
  5. `checkpoint()` MCP signature (`core/server/tools/misc.py:141-210`) — optional param. This is the enqueue+secret-gate path, not the drain path, so it is genuinely optional.

  **Dropped:** the previously-listed `checkpoint_impl.py` edit is **unnecessary** — the drain write calls `insert_checkpoint` directly (`checkpoint_restore.py:224`), bypassing `_enrich_checkpoint_context` entirely, so there is nothing to thread through there.

  Payload shape (proposed):
  ```json
  {"agents": ["a6231fe…", "aa3236a…"], "bg_shells": [...], "monitors": [...],
   "worktrees": ["/path (branch)"], "captured_epoch": N, "note": "dispatched; liveness unverified"}
  ```

---

## Restore surfacing

`restore()` markdown is assembled by `_format_restoration()` (`yadgar/backend/restoration/checkpoint_restore.py:463-516`) with **explicit per-field section iteration** (not a `custom_context` verbatim dump — `custom_context` is appended as trailing text at `:435-436`). Clean insertion point: a new section handler **after the checkpoint section (`:477`), before "Critical Facts (Anchored)" (`:479`)**.

```
## In-Flight At Compaction (verify — not confirmed still running)
- Agents dispatched, no completion seen: a6231fe…, aa3236a…
- Background shells: <ids or "none">
- Monitors: <ids or "none">
- Worktrees: /home/max/git/yadgar/.claude/worktrees/foo (feat/x)
```

Only emit the block when `checkpoint.in_flight` is present and non-empty. Wording carries the liveness caveat verbatim.

---

## Dedup — double catalog injection on `source="compact"`

On a compact SessionStart **both** handlers fire: `session-start-context` (matcher `""`, always) **and** `post-compact-rehydrate` (matcher `"compact"`). Partial dedup already exists — `hook_session_context()` (`http.py:862-926`) suppresses the memory-block prepend (`:902 if source != "compact"`) and the checkpoint resume hint (`:926 if source != "compact"`) when `source="compact"`, because the compact handler already restored those via `restore()`.

- **The real unguarded double-inject is the `project_brief._render` catalog** (`http.py:888` `render = brief.get("_render", "")`, prefixed at `:897`) — it is **NOT** behind any `source != "compact"` check, so on compact `restore()` injects its markdown **and** `session-context` injects the brief catalog (~500 tok duplicate). The already-guarded block-prepend (`:902`) and resume-hint (`:926`) are NOT the residual duplicate — do not re-guard them.
- **Fix:** early-return the generic handler when `source=="compact"` (the compact handler owns the whole restore inject), OR guard the brief `render` assembly on `source != "compact"`. Prefer the early-return — simplest, removes the whole ~500-tok duplicate.
- **Placement (critical):** the guard/early-return MUST sit **after** the throttle-timestamp write (`_bounded_set(_st._last_session_context, …)`, `:869`) and the sentinel import (`_import_pending_sentinels`, `:875`) — i.e. just before the `project_brief` render assembly begins (`:879`+). An early-return at top-of-function would break sentinel ingestion and recall-throttle on every compact.
- **Measure first (OQ5):** confirm the brief catalog is the *sole* residual duplicate before adding the early-return.

---

## Acceptance criteria

**[unit]**
1. `test_hook_post_compact_rehydrate_reads_formatted` — given backend result `{"formatted": "MD"}`, hook_runner prints `MD` (not empty). Regression for BUG 1.
2. `test_transcript_parse_in_flight` — feed a fixture JSONL and assert the parser returns exactly the expected in-flight `agentId` set. Cover: strict `async_launched`∩terminal join, launched-only (in-flight), terminal-with-no-launch (ignore), malformed line (skip), and the two crux regressions: **(a) a foreground-completed agent** (echoes `agentId:` in a completed non-async `toolUseResult`, no `async_launched`, no `<task-notification>`) MUST be **excluded** from in-flight; **(b) a `killed`-status completion** MUST be counted as terminal (i.e. NOT in-flight).
3. `test_pre_compact_drain_writes_in_flight` — `pre_compact_drain(dir, transcript_path=fixture)` writes a checkpoint whose `in_flight.agents` matches the fixture's in-flight set; `transcript_path=None` writes `in_flight=None` (back-compat).
4. `test_restore_surfaces_in_flight` — checkpoint with `in_flight` → `formatted` markdown contains the "In-Flight At Compaction" header and the agent IDs; absent `in_flight` → no header.
5. `test_checkpoint_model_in_flight_roundtrip` — Pydantic model + `ops.insert_checkpoint` round-trip the `in_flight` dict.
6. `test_session_context_compact_dedup` — `source="compact"` request does not emit the catalog block already covered by restore.

**[manual] — real mid-compaction round-trip (THE feasibility gate for the enrichment, not just an acceptance check).**
Because in-flight is 0 by construction on any completed transcript, the unit tests validate only the *mechanics* against fixtures that encode our own assumptions — they cannot prove the feature surfaces a real live agent. The live round-trip is the only true validator, so it **gates the enrichment**: run it before treating the parser as done.
- In a live session with ≥1 **genuinely-live** background agent dispatched (uncompleted at compact time), trigger `/compact`. After compaction, confirm the injected context contains (a) the restore block (non-empty — BUG 1 gone) and (b) the "In-Flight At Compaction" section listing that dispatched agent ID. Confirm no doubled catalog. Confirm `git worktree list` entries appear.
- If the section is empty or wrong, the parser assumptions (or transcript-lag, Risk 7) are off — fix before merge. The bug/dedup fixes (unit-testable, independent) are unaffected either way.

---

## Test plan

- New fixture: `yadgar/tests/fixtures/transcript_in_flight.jsonl` — a hand-built minimal transcript covering every branch of the corrected algorithm: a background agent that is `async_launched` + `completed` (terminal, not in-flight), a background agent `async_launched` with **no** terminal notification (the one true in-flight), a background agent `async_launched` + `killed` (terminal, not in-flight), a **foreground/synchronous** agent that echoes `agentId:` in a completed non-async `toolUseResult` (MUST be excluded — the crux regression), one `run_in_background` bash, and one malformed line. Redact all content.
- Unit tests under `yadgar/tests/hooks/` (join `test_hook_runner_*` neighbors) + `yadgar/tests/restoration/` for the backend parse/format.
- Parser lives backend-side (`checkpoint_restore.py` or a new `yadgar/backend/restoration/transcript_parse.py`) → unit-testable without a live daemon.
- Regression: existing restore/drain tests must still pass with `transcript_path=None`.
- Manual round-trip logged in the PR description (screenshot/paste of the injected block).

---

## Risks

1. **Liveness overclaim.** If restore says "still running" and the agent finished, the user wastes a verify. Mitigated by wording ("verify — not confirmed") and never polling. **Accepted.**
2. **Transcript schema drift.** Claude Code could change the launch-ack / notification shape; the `agentId`↔`task-id` join is empirically verified today but is an undocumented internal contract. Mitigate: parser tolerant (skip unrecognized lines, emit `in_flight=None` on total parse failure) and never blocks the drain. Add a metric/log when parse yields 0 from a non-empty transcript (drift canary).
3. **Backend parse cost on large transcripts.** 1640-notification file parsed synchronously in `pre_compact_drain`. Cap: stream line-by-line, bounded regex, hard timeout; PreCompact is already best-effort. Measure on the manual round-trip.
4. **BUG 2 scope confusion.** Fixing only the in-repo script leaves the (unverified) global nix variant untouched. Mitigate: MIGRATION_NOTES entry for the user to check/patch the dotfiles script.
5. **Monitor IDs unverifiable.** Shipping a parser branch for a shape we've never seen risks silent wrong-matching. Mitigate: gate Monitor extraction behind an observed-shape check; ship agents+bg-bash+worktrees first, add Monitor when a real sample exists.
6. **Shared-worktree/version skew.** This worktree's git HEAD (#50) diverges from the session's (#170). Confirm the actual base branch + latest merged PR at PR-open time; sequence after #195 per directive.
7. **Transcript lag at hook-fire.** Per Claude Code docs, `transcript_path` may not yet reflect the current turn's most-recent messages when PreCompact fires. Effect: a very-recently-dispatched agent could be missing from the parse → under-report in-flight (never over-report). Accepted — under-report is the safe direction; the restore block says "verify," so a missing entry just means the user isn't reminded of that one agent. Do not treat the parse as exhaustive.

---

## Scope

**IN**
- Fix BUG 1 (`hook_runner.py:157` → prefer `formatted`).
- In-repo BUG 2 hardening (warn-on-fallback; prefer silent over wrong-dir). MIGRATION_NOTES for global nix variant.
- Dedup: eliminate residual double catalog on `source="compact"`.
- PreCompact transcript-parse (backend-side, option B): agents + bg-bash + worktrees.
- New `in_flight` checkpoint field (model + ops + impl + restore formatter).
- Restore surfacing block.
- Unit fixtures + tests; manual round-trip.

**OUT**
- New hook events (no PostCompact write-inject — it's read-only; no new lifecycle event).
- Monitor-ID extraction (deferred until a verified transcript sample exists).
- Global nix / dotfiles repo edits (different repo → MIGRATION_NOTES handoff).
- Model-authored ADR capture at compact time (PreCompact is a non-interactive script; rich model capture stays on the Stop hook — out of this car).
- Live liveness polling / process registry.

---

## Version impact

- **core `5.134.0` (post-#195) → next minor.** hook_runner (BUG 1 fix), `http.py:hook_pre_compact` (REQUIRED `transcript_path` thread-through), CLI drain/restore signatures, checkpoint ops (+ optional model), restore formatter, dedup guard. install_hooks_lib needs **no registration change** — the payload already carries `transcript_path`.
- **backend `5.42.0` → next minor.** `pre_compact_drain` signature + transcript parser + admin wrapper.
- **hook scripts:** versioned with core; `.sh` (Path B) gains `transcript_path` passthrough.
- **Sequence:** after #195 (per directive). Confirm actual latest-merged PR + base branch at open time (worktree skew noted in Risk 6).

---

## Open questions

1. **Global nix BUG 2 (unresolved).** Does `dotfiles/common/yadgar-hooks/yadgar-post-compact-rehydrate.sh` actually use `$(pwd)`-only? Not in this repo — needs a look at the nix dotfiles repo. If yes → MIGRATION_NOTES fix; if no → BUG 2 is a non-issue.
2. **`in_flight` field vs `custom_context`.** Plan chooses the new field — **3 required sites, no migration** (schemaless `CREATE … SET`; `resume_hint` proves it) — over the fragile `custom_context` string-parse route. Confirm the team is fine adding the field via the schemaless path (Pydantic model field is optional, add only if a typed consumer appears).
3. **Liveness limit acceptance.** Is "dispatched, unverified" surfacing useful enough, or does it need a follow-up (e.g. a live agent registry the daemon queries at restore) to be worth the parse? Ship the honest version first?
4. **Background-bash shell-id key.** The result-line key for a `run_in_background` bash shell ID was not in the sample. Verify on first impl before wiring that branch.
5. **Dedup shape.** The `project_brief._render` catalog (`:888/:897`) is unguarded and double-injects on compact; the block-prepend (`:902`) and resume-hint (`:926`) are already suppressed. Confirm the catalog is the *sole* residual duplicate (measure) before adding the early-return (placed after the `:869/:875` side effects).
6. **Parser home.** New `transcript_parse.py` module vs inline in `checkpoint_restore.py` — prefer a standalone module for unit isolation.

---

## User decisions required

The audit fixes have been folded into the body above (algorithm corrected, blast radius = 3 required, HTTP threading edit added, dedup target corrected, versions bumped). **One genuine decision remains open:**

1. **Car split (OPEN — the only blocker to a final go).** The AUDIT (kept below) recommends **splitting**: land BUG 1 + BUG 2-hardening + dedup now (independent, unit-testable, land-ready today) and ship the enrichment separately, gated on a passing live round-trip. This plan author recommends **one car (option a)**: keep the enrichment in the same PR because it is the user's headline ask, and gate it on a passing manual mid-compaction round-trip rather than a second PR. Two options for the user to pick:
   - **(a) One car [RECOMMENDED]** — bugs + dedup + enrichment in a single PR; the enrichment's merge is gated on a passing manual live round-trip (Acceptance `[manual]`). Simpler review history; the headline feature ships with its fixes.
   - **(b) Split** — bugs + dedup ship immediately as PR 1 (no live gate needed); the enrichment is PR 2, gated on the same round-trip. De-risks the land-ready fixes from the live-gated feature.

Everything else the audit raised (algorithm correction, 3-site schemaless schema route, HTTP threading, dedup placement, BUG 2 → MIGRATION_NOTES, script-name muddle) is **already resolved in the body** and needs no further user decision — they are correctness folds, not choices.

---

## AUDIT (2026-07-13)

Adversarial audit. Read-only. Every claim re-verified against observed source + a real transcript. Brutal-honesty verdict below.

### Verdict: DRAFT — CONDITIONAL. Bug fixes + dedup + schema mechanics are sound and land-ready. The **headline enrichment claim is WRONG as stated** — the "20 in-flight, algorithm works on real data" number is an artifact of a loose regex; the *mechanism* is feasible but the plan's operationalization and its empirical proof are both broken. Fix the algorithm definition, downgrade the empirical claim to "unproven — manual round-trip is the only validator," and the enrichment can proceed. The two bugs and the dedup fix can land independently and immediately.

### Per-claim verification table

| # | Plan claim | Verdict | Evidence (file:line) |
|---|---|---|---|
| 1 | BUG 1: `hook_runner.py:157` reads `text`/`context`, backend returns `formatted` → empty inject on HTTP path | **VERIFIED** | `hook_runner.py:157` `text = result.get("text", result.get("context", ""))`; restore return key is `formatted` at `checkpoint_restore.py:399` |
| 2 | CLI `restore.py:15` reads `formatted` correctly (nix/CLI users unaffected) | **VERIFIED** | `cli/restore.py:15` `formatted = result.get("formatted", "")` |
| 3 | Fix (prefer `formatted`, keep old keys fallback) safe for both paths | **VERIFIED — safe** | HTTP path currently gets `""`; adding `formatted` first is strictly additive. CLI path untouched (separate function). No shared code. |
| 4 | `hook_pre_compact()` at `http.py:651-683`, reads `body.get("cwd")`, forwards `{"directory"}` only | **VERIFIED** | `http.py:660` `directory = body.get("cwd", os.getcwd())`; `:670` forwards `{"directory": directory}` — **`transcript_path` is dropped HERE, not at hook_runner**. Plan line 137 ("no change needed on HTTP path to deliver it") is misleading: hook_runner POSTs full `data`, but `http.py:660-670` extracts only `cwd`. The threading edit IS required at `http.py`. (Plan line 138 does list this — but line 137's "no change needed" overstates.) |
| 5 | `pre_compact_drain(directory)` at `checkpoint_restore.py:209-241`, no transcript today | **VERIFIED** | exact `:209-241`; signature `pre_compact_drain(self, directory: str)`; body writes via `self._storage.insert_checkpoint({4 fields})` at `:224-231` |
| 6 | Checkpoint model `models.py:328-342`, 13 fields, no orchestration field | **VERIFIED** | exact `:328-342`, 13 fields as listed |
| 7 | `resume_hint` DB-only, not in Pydantic model | **VERIFIED** | `ops.py:625,636` writes `resume_hint = $hint`; absent from `models.Checkpoint`. **Proves schemaless write — see schema finding below.** |
| 8 | custom_context enriched by `_enrich_checkpoint_context()` at `checkpoint_impl.py:67`; new block must not collide | **WRONG (misdirected)** | `_enrich_checkpoint_context` is on the **MCP checkpoint** write path (`run_checkpoint_replay` → `create_checkpoint`). `pre_compact_drain` (the drain write that carries `in_flight`) calls `insert_checkpoint` DIRECTLY (`checkpoint_restore.py:224`) — bypasses `checkpoint_impl.py` entirely. The collision concern (plan lines 88, 150 item 3) is a **non-issue for the drain write**. Blast-radius item 3 is unnecessary. |
| 9 | Dedup: `:902` and `:926` suppress on `source != "compact"` | **VERIFIED** | `http.py:902` (memory-block prepend) + `:926` (checkpoint hint) both guarded. |
| 10 | Dedup GAP: brief catalog still double-injects on compact | **VERIFIED** | The `render` from `project_brief._render` (`http.py:888,897`) is **NOT** guarded by any `source != "compact"` check — it always injects. So on compact, restore() injects its markdown AND session-context injects the brief catalog. The plan's gap claim is correct; the field-level dedup does NOT cut the catalog. |
| 11 | Restore formatter `_format_restoration` at `:463-516`; insert after checkpoint (`:477`), before anchored (`:479`) | **VERIFIED** | exact. `checkpoint.get("in_flight")` readable there since `checkpoint` is the raw `SELECT *` dict from `get_active_checkpoint`. |
| 12 | MCP `checkpoint()` at `misc.py:141-210` | **VERIFIED** | exact; already `# noqa: PLR0913` at 9 args. Adding `in_flight` extends it further — this is the enqueue+secret-gate path, NOT the drain path, so it's genuinely optional (plan says so — correct). |
| 13 | Transcript join `agentId (launch) == <task-id> (completion)`, 17-char `a`+hex | **VERIFIED** | Real transcript `cf2c4f8b-….jsonl`: `<task-id>af2808a69a250738f</task-id>` == launch `agentId:"af2808a69a250738f"`. Join is exact. |
| 14 | 323 launched / 303 completed / **20 in-flight** — "algorithm works on real data" | **WRONG — artifact** | See crux below. 323 comes from an `agentId`-**anytext** regex that counts foreground agents. Strict `async_launched`-result launched = **303 = completed → 0 real in-flight**. All 20 "in-flight" verified (n=20, not n=1) as foreground agents with `isAsync!=True, status=completed`. |
| 15 | Algorithm: `completed = task-id from every …completed… block` | **WRONG (incomplete)** | Completion notifications carry `<status>` ∈ {`completed`:666, `killed`:24} in the same transcript. The plan's `…completed…`-only filter would mark the 24 `killed` agents as never-completed → **falsely in-flight**. Must count `killed` (and any terminal status) too. |
| 16 | BUG 2: in-repo `.sh` reads stdin `cwd` first, `$(pwd)` fallback (correct-ish); global nix variant flagged to MIGRATION_NOTES | **VERIFIED (with a naming muddle)** | `.claude/hooks/pre-compact-drain.sh:10-13` matches the quote exactly. BUT this is the **drain** (PreCompact) script; BUG 2 is about the **rehydrate** (post-compact restore) wrong-directory. The plan quotes the drain script as a proxy for a rehydrate-script bug. Scoping (in-repo-fine + dotfiles-flagged) is honest and correct; the script-name conflation should be fixed in prose. |
| 17 | Version: core `5.133.0` | **VERIFIED** | `pyproject.toml:7 version = "5.133.0"`. (Audit ran in worktree on `feat/obs-quickwins-train`@945ff954, ahead of the `git status` snapshot — plan's Risk 6 worktree-skew note is warranted; version literal is correct.) |

### Crux — transcript-parse in-flight heuristic is measuring the wrong thing (WRONG)

The plan's central empirical proof is an artifact. Two definitions of "launched" give opposite answers on the **same real transcript** (`cf2c4f8b-6003-483e-a8ae-d6583335162c.jsonl`):

- **Strict** (`toolUseResult.status == "async_launched"` only): launched = **303**, completed = 303, `in_flight = 0`.
- **Anytext** (`agentId` token anywhere, incl. `agentId: X (use SendMessage…)` echoed in completed tool_result text): launched = **323**, completed = 303, `in_flight = 20`. ← the plan's numbers.

Verified at full n (not n=1): **all 20** anytext-only IDs have a synchronous `toolUseResult` with `isAsync != True, status == "completed"`, carrying `<usage>…duration_ms…</usage>`. They are **foreground/synchronous agents** — dispatched inline, finished inline, returned output directly. They never emit an `async_launched` result and never emit a `<task-notification>…</task-notification>` block (those are background-only), so they are absent from `completed` and falsely counted as launched-not-completed.

The plan's own extraction table (line 118) says foreground agents are "excluded by definition." The regex the plan actually used to reach 323 does **not** exclude them — the 20 it celebrates as proof ARE the foreground agents it claims to exclude. On a session that ran to completion, real in-flight is 0 **by construction** (every background agent finished before the transcript ended).

Corrected algorithm (three separable defects):
1. `launched` = strict `async_launched` toolUseResult `agentId` set — NOT any `agentId` token. (foreground pollution)
2. `completed` = `<task-id>` from every `<task-notification>` block with terminal `<status>` ∈ {`completed`, `killed`, …} — NOT `completed`-only. (24 killed agents mis-flagged)
3. **Value is unproven, separate from correctness.** The only transcript that could validate a *true* in-flight positive is one frozen mid-compaction with genuinely-live background agents — which the plan never obtained. Compounded by Risk 7 (transcript lag drops the most-recent dispatches → under-report), the feature's real yield is **untested**. The `[manual]` round-trip (plan line 197-198) is the ONLY validator that matters — elevate it from "acceptance criterion" to "feasibility gate before building the parser."

Monitor-ID deferral (OUT): **agree** — no transcript sample exists; shipping a parser branch for an unseen shape risks silent wrong-matching (Risk 5 is right).

### Schema change — NO migration needed; 5-site blast radius is OVERSTATED

SurrealDB `CREATE … SET` is schemaless. `resume_hint` (claim 7) proves a field writes to the checkpoint row without being in the Pydantic model and without any migration. Therefore:

- **Required minimum = 3 sites**, not 5: (a) `pre_compact_drain` — parse + add `in_flight` to the `insert_checkpoint` dict; (b) `ops.insert_checkpoint` — add `in_flight = $in_flight` to the SET clause + params; (c) restore formatter — read `checkpoint.get("in_flight")`. Restore reads via `SELECT *` → dict, so no model field is needed to round-trip.
- **Optional** (plan items 1 + 4): Pydantic `models.Checkpoint` field and the MCP `checkpoint()` param. Nice for typing/validation but NOT on the drain write path. Item 3 (`checkpoint_impl.py`) is **unnecessary** — the drain bypasses it (claim 8).

So the "5-site blast radius" is really "3 required + 2 optional." No DB migration. Prefer `dict | None` payload as proposed; do NOT route it through `custom_context` (agree — the fragile-string-parse rejection is right).

### Dedup fix caution

The plan's preferred "early-return the generic handler on `source==compact`" (line 183) must sit **after** the throttle-timestamp write (`http.py:869`) and the sentinel import (`:875`) — an early-return at top-of-function would break sentinel ingestion and recall-throttle on every compact. Place the guard just before the `project_brief` render assembly (`:879`+). Note the block-prepend (`:902`) and hint (`:926`) are ALREADY suppressed, so the early-return only needs to also drop the brief `render` — confirm that's the sole residual duplicate before adding it (plan OQ5 is the right instinct: measure first).

### Acceptance criteria — testable? Mostly yes, one gap

- `[unit]` 1-6: all sound and cover the corrected mechanics — with two required additions to test #2 (`test_transcript_parse_in_flight`): (a) a fixture agent that is foreground-completed (echoed `agentId` in a completed non-async result) MUST be excluded from in-flight — this is the exact regression for the crux bug; (b) a `killed`-status completion MUST be treated as completed (not in-flight).
- `[manual]`: correctly identified as the real validator — but it's buried as one bullet. Given the crux, it should GATE the parser build: run one real mid-compaction round-trip with a live bg agent FIRST; if in-flight surfaces correctly, then build the full parser. Otherwise the unit tests pass against fixtures that encode the plan's own (possibly wrong) assumptions.

### STALE / WRONG count

- **WRONG: 3** — (14) headline "20 in-flight / works on real data" is an artifact; (15) `completed`-only filter mis-flags `killed` agents; (8) blast-radius item 3 misdirected (drain bypasses `_enrich_checkpoint_context`).
- **OVERSTATED: 2** — 5-site blast radius (really 3 required + 2 optional, no migration); plan line 137 "no change needed on HTTP path" (http.py DOES need the threading edit).
- **VERIFIED: 12** — all bug/dedup/schema-mechanic/version/join-shape claims.
- **Naming muddle: 1** — BUG 2 quotes the drain `.sh` as a proxy for a rehydrate-script bug.

### User decisions required

1. **Enrichment go/no-go given crux:** the in-flight number is unproven on any real data (0 by construction on completed sessions). Build the parser NOW with the corrected algorithm and gate on a manual mid-compaction round-trip — or DEFER the enrichment and land only BUG1 + BUG2-hardening + dedup (all independent, land-ready today)? Recommendation: **split the PR** — bugs+dedup ship immediately; enrichment gated behind a passing manual round-trip.
2. **Algorithm correction accepted?** strict `async_launched` launched-set + `{completed, killed, …}` completed-set. (Required for correctness regardless of go/no-go.)
3. **Schema route:** accept the 3-required-site schemaless approach (no migration, no Pydantic field) — or add the optional Pydantic/MCP surface for typing? Recommendation: 3-site schemaless first; add model field only if a typed consumer appears.
4. **BUG 2 global nix:** confirmed out-of-repo; MIGRATION_NOTES handoff is the right call. Also fix the drain-vs-rehydrate script-name conflation in the plan prose.
5. **Dedup:** measure the residual catalog double-inject before adding the early-return; if adding it, place the guard after the `:869`/`:875` side effects.
