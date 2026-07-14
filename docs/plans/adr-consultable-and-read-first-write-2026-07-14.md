# Canonical-Write Foundation + Task-List Fix + ADR-Consultable (3-Car Train)

**Status:** AUDITED-ready — implementable by a builder
**Date:** 2026-07-14 (re-planned 2026-07-14 after live-daemon empirical findings; verified against `origin/master` @ `aed223c9`, v5.139.1)
**Scope:** core (`yadgar/core/server/tools/{adr,wiki,project,memorize,admin_other,dispatch_helper}.py`, `yadgar/core/hooks/{session-start-context,subagent_stop}.py`, `yadgar/core/hooks/templates/stop_checkpoint_prompt.md`, `yadgar/core/install/install_hooks_lib.py`, `yadgar/core/server/http.py`), backend (`yadgar/backend/queue_drainer/dlq.py`, `yadgar/backend/retrieval/recall_pipeline.py`, `yadgar/backend/write_exec/memorize_impl.py`, `yadgar/backend/admin_exec/memory.py`), config (`yadgar/_shared/config/{config,config_registry}.py`), seed (`yadgar/core/seed/materials/agent_prompts.yaml`), migration script (`scripts/`), tests.
**Tracks:** task #76 (ADRs consultable) + the read-first-write discipline + the branch-model bug surfaced live (task-list mirror hard-reject + non-git default-branch-pin).
**Rollout:** ALL projects. Machinery is project-agnostic (`<project>-adr-*`, `<project>-task-list`).

---

## BLUF

A 3-car train. New empirical findings on the live 5.139.1 daemon reshaped this: what was "make ADRs recall-native" is now grounded on a **canonical-write foundation** that the just-shipped task-list mirror needs too.

**Car 0 — canonical-write + branch-model FOUNDATION (blocks Car 1 and Car 2).** Today you CANNOT write a canonical (branch=NULL) wiki page through `wiki_add`: with `YADGAR_BRANCH_ENFORCEMENT=true` (default), a `wiki_add` missing `branch`/`branch_hint` is hard-rejected `missing_branch` at BOTH the MCP boundary (`wiki.py:36`) and the drainer (`dlq.py:137`). The only carve-out is `_internal=True` — a payload flag NOT in the `wiki_add` MCP signature and STRIPPED before DB write (`dlq.py:248,254`), so a model can never set it. Car 0 makes canonical a **first-class, git-aware, SERVER-SIDE-only write path** (sets `branch=None` + `_internal` internally), reached by `adr_add` and the task-list writer — never by a model-settable flag. It adds **git-aware gating**: non-git dirs default to canonical (there are no branches to isolate); git dirs get canonical ONLY through the allowlisted system paths; missing-hint-with-no-non-git-signal stays hard-rejected (the corruption guard the v5.42.3 reject exists for).

**Car 1 — task-list mirror FIX (depends Car 0).** The shipped stop-hook template step 4c (`stop_checkpoint_prompt.md:101-132`) instructs a canonical task-list write "WITHOUT branch_hint" — but that write is **hard-rejected by `missing_branch`** as deployed (no `task_list` carve-out exists). The mirror is BROKEN in the field. Car 1 routes the write through Car 0's canonical path and adds the MISSING test: the real task-list write THROUGH the daemon gate (Car 1's original tests covered schema + template text + the read-nudge, never the gated write — the coverage hole that let it ship broken).

**Car 2 — ADR-consultable (depends Car 0).** Kill the write-only ADR monolith. One recall-native wiki page per ADR (`<project>-adr-NNNN`) + a thin index, using Car 0's canonical write (per-ADR pages must be readable cross-branch AND in non-git projects — the same reason as the task-list). The all-projects migration ALSO corrects mis-pinned ADR logs (`aws-work-adr-log` is pinned to a bogus "master" and unreadable in that non-git dir — the live 531352 bug). Plus the memorize soft-gate (Part B) and the `agent-discipline-adr-consult` read-path discipline.

**Superseding note:** the memory-531352 "pin the ADR-log to the default branch" decision is REVERSED by canonical (Car 2). A superseding ADR records this.

---

## Empirical findings (live daemon 5.139.1, re-verified against `origin/master`@`aed223c9`)

> **Stale-base correction (audit process note).** The first audit pass ran against a worktree pinned at v5.136 and wrongly concluded "the task-list mirror does not exist." It does — the "Stop-hook / task-list train" (PR #200, `aed223c9`, v5.136.1→5.139.1) shipped it. The worktree was rebased onto `origin/master`; every claim below is verified against that tree. Observed-state-wins cuts both ways: a stale checkout is not authoritative about current master.

### Branch-enforcement + the missing_branch reject (Car 0 hinge)
- MCP boundary: `_check_wiki_add_context` (`wiki.py:25`) returns `missing_branch` when `not branch` AND `_enforcement_on("YADGAR_BRANCH_ENFORCEMENT")` (`wiki.py:36`).
- Drainer: `_validate_wiki_add` branch check (`dlq.py:137-152`), memory-op `_validate_branch_context` (`dlq.py:172-197`) — both skip when `_internal`.
- `_internal=True` is the ONLY carve-out (`dlq.py:138,175`); it is **stripped before the DB write** (`dlq.py:248,254`) and is NOT a `wiki_add` MCP parameter → **a model cannot set it.** This non-model-settable property is the security boundary Car 0 builds on.
- Defaults: `YADGAR_BRANCH_ENFORCEMENT=true` (`config_registry.py:374`, `config.py:345`), `YADGAR_DIRECTORY_ENFORCEMENT=true` (`config_registry.py:373`). `_enforcement_on` (`_shared/security/enforcement.py:17`) fails ON for unknown values.
- `wiki_add` docstring (`wiki.py:190-193`) already SPECIFIES both-omitted → `branch IS NULL` canonical — but enforcement blocks that path today. So Car 0 is "let the allowlisted system path pass NULL past enforcement," not new storage plumbing.

### The daemon cannot see host git — default-branch-pin is doubly wrong
- `_get_default_branch_cached` (`project.py:136`) runs `git symbolic-ref` and **unconditionally returns `"master"`** on any error / non-git (`project.py:154`). `_detect_branch` returns `None` on non-git.
- The daemon runs in a CONTAINER and cannot see the host `.git`; a daemon-side `_get_default_branch` therefore resolves `"master"` even for a git project whose real default is `main`. Branch context MUST be supplied by the HOST.
- Host-side compute: `session-start-context.py:42-49` runs `git branch --show-current`; non-git → `returncode != 0` → `_branch=""` → the `?branch=` param is OMITTED. `subagent_stop.py:262-272,295` runs `git symbolic-ref --short HEAD`. So the host ALREADY produces an empty branch for non-git — but conflates "non-git" with "detached-HEAD / git-error".
- **Live proof (531352 bug):** `aws-work-adr-log` (id 6822) is pinned `branch="master"`; `wiki_read(directory=aws-work)` with no `branch_hint` returns NOT FOUND, only `branch_hint="master"` finds it — and "master" is bogus for a non-git dir. Content pages in the same dir (id 6705, `branch=NULL`) read fine. Default-branch-pin fails for non-git; canonical(NULL) is correct.

### Task-list mirror — deployed but BROKEN (Car 1 target)
- Template `stop_checkpoint_prompt.md` (207 lines). Step 4 = TASK-LIST MIRROR (`:89-143`). Step 4c (`:101-132`): canonical write "WITHOUT branch_hint so it lands on the project-canonical / branch-NULL slot", via `wiki_add(page_type="task_list", replace_slug="{project}-task-list", tags=["task-list"], directory=..., wait=True)` — **no branch_hint, no `_internal`.**
- Under `BRANCH_ENFORCEMENT=true` this is hard-rejected `missing_branch`. **No `task_list` canonical carve-out exists** in `wiki_add`/`dlq` (verified). The template instructs a write the gate rejects → the mirror never lands. (Investigator draft marked this row "✅ correct"; that verdict is WRONG — observed-state-wins overturns it. The intent is canonical; the deployed path is hard-rejected.)
- `task_list` page_type WAS added by Car 1 of the train (`_shared/schemas/wiki_page_types.yaml:39-40`, `required: [Meta]`; `PAGE_TYPES` loads from it).
- The READ side works: `_task_list_restore_nudge` (`http.py:855`, MAIN-THREAD-ONLY, existence-checked) resolves the canonical page via §25 step-2 (dir + branch IS NULL) regardless of the caller's `branch_hint`. Its docstring (`http.py:869-873`) explicitly names the "memory 531352 / ADR-log branch-pin bug class" as the reason the page must be canonical. So the read is correct; only the WRITE is broken.

### ADR write path — the 531352 bug LIVE (Car 2 target)
- `adr.py`: `default_branch=_get_default_branch(resolved)` (`:251`); `wiki_read(..., branch_hint=default_branch)` (`:265`); `wiki_append_section(..., branch_hint=default_branch)` (`:296`); first-ADR `wiki_add(..., branch_hint=default_branch)` (`:320`). Both read AND write pin the default branch → for a non-git dir that is bogus "master" → the aws-work-adr-log bug.
- `_build_adr_log` (`project.py:1837`, restore mode) and `_get_adr_log_updated_at` (`project.py:1340`) read `<project>-adr-log` with `branch_hint=default_branch` → feature-branch / non-git sessions see an empty/absent ADR log. (Investigator "ADR-log asymmetry" ⚠ — real; RESOLVED by the canonical move in Car 2, not a separate fix.)

### Recall / read-path (Car 2; re-verified post-rebase, unchanged from prior audit)
- Wiki is a first-class recall source (WikiProvider fuses in the default profile). Wiki pages NEVER decay — `wiki_page` CREATE SET (`_shared/storage/wiki.py:180-211`) has no `heat`/`is_protected`/`valid_until`.
- `profile="fast"` SKIPS the wiki arm: `_should_skip_wiki` (`recall_pipeline.py:300`), `profile_def.get("wiki", True)` (`:329`), ADR-0077 (`:314-316`). The per-turn auto-recall hook uses `profile="fast"` → memory-only → does NOT surface ADR pages (the read-path gap, Car 2 §C.5.1).
- `wiki_exclude = None if tags else [...]` (`recall_pipeline.py:432`) — do NOT add `"adr"`; ADR pages surface in default-profile recall.
- `wiki_add force=True` bypasses the drainer sim gate (`dlq.py:279`); threshold `WIKI_SIM_CONTENT_THRESHOLD=0.80` (`config.py:325`).
- `memory_update` never re-embeds: backend `admin_exec/memory.py:79` → `update_memory_fields` (`storage/memory.py:964`) writes the row + busts cache, never re-encodes.
- `agent_dispatch_prelude` lives in `dispatch_helper.py`; reads the contract + agent-prompt pages global-scoped, propagates `branch_hint` to recall/wiki_query (branch-correct).

---

## Car 0 — Canonical-write + branch-model foundation

**Depends on:** nothing. **Blocks:** Car 1, Car 2.

### 0.1 The guardrail (why NOT a model-settable flag or a page_type allowlist-in-wiki_add)

The requirement: *a model must not be able to canonical-write arbitrary git pages* (canonical bypasses branch isolation; a model canonical-writing a feature-branch page corrupts the canonical slot). Grade the options against the ONE property that enforces this — is the canonical decision model-settable?

- **A bare `canonical=True` param on `wiki_add`** → model sets it on anything → guardrail gone. **Rejected.**
- **A `page_type ∈ {task_list, adr}` + required-tag allowlist evaluated inside `wiki_add`** (the literal spec) → BOTH `page_type` and `tags` are model-supplied → the allowlist blocks *accidents*, not a motivated caller. **It is NOT a real boundary — brutal-honesty: it is a soft accident-guard, not a security gate.** Do not present it as one.
- **Canonical reached ONLY through SERVER-SIDE system paths that set `branch=None` + `_internal` themselves** → the model never requests canonical; it calls `adr_add` / the task-list writer, which internally decide canonical. `_internal` stays non-model-settable (stripped before DB write). **Chosen.**

**Decision:** Car 0 adds an internal canonical-write helper (call it `_wiki_write_canonical(...)`, core-side) that sets `branch=None` and `_internal=True` on the enqueue payload. `adr_add` (Car 2) and the task-list writer (Car 1) call it. The `page_type ∈ {task_list, adr}` + required-tag check becomes a **server-side assertion INSIDE that helper** (defense-in-depth: refuse to canonical-write a page whose `page_type` is not allowlisted), NOT the primary gate. This still delivers the coordinator's "one-call canonical write" — `adr_add` and the task-list write are each a single call — and kills the two-step `wiki_add`+`wiki_set_metadata` workaround.

**Allowlist as a config-visible constant** (I32 capability-registry entry): `CANONICAL_PAGE_TYPES = frozenset({"task_list", "adr"})`. Register it so the capability audit catalogues it.

### 0.2 Git-aware gating (the three cases)

The canonical helper / enforcement path resolves target branch by case:

1. **Non-git dir → canonical (NULL) is the DEFAULT for ALL pages.** No branches to isolate. The HOST must signal non-git (the container can't see `.git`). See 0.3.
2. **Git dir + canonical via an allowlisted system path (`page_type ∈ {task_list, adr}` + tag) → write NULL.** The model cannot reach this except through `adr_add` / the task-list writer.
3. **Git dir + a regular page + missing/empty branch AND no non-git signal → PRESERVE the v5.42.3 hard-reject.** This still guards the git-dropped-hint corruption case the reject was created for. **`non-git-signal` ≠ `missing-hint`:** an explicit non-git signal permits NULL; a merely-absent hint on a git dir is still rejected.

### 0.3 The non-git host signal (design + files)

The host already produces an empty branch for non-git but conflates it with git-errors. Make it explicit:

- **Host-side detection:** in the hook scripts, run `git -C {dir} rev-parse --is-inside-work-tree` (or `--git-dir`); a non-zero exit = genuinely non-git (distinct from "git dir, detached HEAD"). Emit an explicit signal — a sentinel branch value `__canonical__` (or a `non_git=1` query param / payload field) — rather than silently omitting `branch`.
- **Daemon-side mapping:** the enforcement path (`_check_wiki_add_context` `wiki.py:25`, `_validate_wiki_add` `dlq.py:137`) treats the sentinel as "canonical permitted" → maps to `branch=None` and does NOT reject. A genuinely-absent hint (no sentinel) on a git dir stays rejected.
- **`_get_default_branch` fix:** stop manufacturing bogus `"master"` for non-git. When the host signals non-git, canonical is the target — the daemon must not invent a branch name it cannot verify.
- **Files touched:** `yadgar/core/hooks/session-start-context.py` (`:42-49` branch compute), `yadgar/core/hooks/subagent_stop.py` (`:262-295`), `yadgar/core/install/install_hooks_lib.py` (hook wiring / any `{default_branch}` substitution), the template `{default_branch}` placeholder logic (`stop_checkpoint_prompt.md:5-6`), `yadgar/core/server/tools/project.py` (`_get_default_branch` `:154`, `_detect_branch`), `yadgar/core/server/tools/misc.py` (the `_detect_branch → branch_hint → CI_BRANCH → hard-reject` chain), and the enforcement sites `wiki.py:25` / `dlq.py:137`.

### 0.4 page_type for ADR

`PAGE_TYPES` currently has `function, module, service, architecture, decision, analysis` — **no `adr`.** Two choices: (a) add an `adr` page_type to `_shared/schemas/wiki_page_types.yaml` (parallels `task_list` which Car 1 added), or (b) reuse `decision` and gate the allowlist on the `["adr"]` tag. **Recommend (a)** — a distinct `adr` page_type makes the allowlist assertion unambiguous and lets `wiki_lint` format-check ADR pages. Add `adr: {required: [Context, Decision, Consequences]}` (matching the existing `decision` schema shape).

### 0.5 Migration note (sequenced under Car 2)

Existing mis-pinned pages (`aws-work-adr-log` master→canonical, any other project's default-pinned log) are corrected — but that correction is part of Car 2's migration (§C.6), NOT Car 0. Car 0 only ships the write path + gating.

### 0.6 TDD (Car 0)

- non-git signal → NULL for ANY page_type (regular page in a non-git dir writes canonical).
- git dir + non-allowlisted page_type + canonical requested via the helper → rejected (assertion fires).
- git dir + `page_type ∈ {task_list, adr}` via the internal helper → writes NULL.
- git dir + regular page + missing hint + NO non-git signal → hard-reject UNCHANGED (v5.42.3 guard preserved).
- model calling `wiki_add` directly (no `_internal`) cannot reach canonical — the helper is the only NULL path; `wiki_add` still rejects. Documents that the allowlist is a server-side assertion, not a model-facing gate.
- `_get_default_branch` no longer returns "master" when the non-git signal is present.

---

## Car 1 — Task-list mirror fix

**Depends on:** Car 0.

### 1.1 The bug + fix
- **Bug:** template step 4c (`stop_checkpoint_prompt.md:101-132`) tells the stop-hook to `wiki_add(..., NO branch_hint)` to land canonical → hard-rejected `missing_branch` under `BRANCH_ENFORCEMENT=true`. The mirror never persists.
- **Fix:** route the task-list write through Car 0's canonical path — a system path (dedicated task-list writer, or `wiki_add` recognising the non-git signal / an internal canonical call) that sets `branch=None` + `_internal` server-side. NOT "tell the model to omit branch_hint" (still rejected) and NOT a model-settable canonical flag (breaks the guardrail).
- **Template edit:** step 4c drops the "write WITHOUT branch_hint" instruction (a rejected write) and points at the one-call canonical path Car 0 provides. The model-facing instruction becomes "call the task-list mirror," not "craft a canonical wiki_add".

### 1.2 The missing coverage
Add a test that exercises the REAL task-list write THROUGH the daemon gate — enqueue → drainer → assert the page lands with `branch IS NULL` and is NOT DLQ'd `missing_branch`. Car 1's original tests (`test_stop_hook_template.py`, `test_task_list_schema.py`) covered schema + template text + the read-nudge, never the gated write — the exact hole that let a hard-rejected write ship. This test is the regression guard.

### 1.3 Note
`yadgar-task-list` (id 6845) was already made canonical manually (via the two-step `wiki_add(branch_hint=default)` + `wiki_set_metadata(branch=null)` workaround). Car 1 makes the automated stop-hook path do it in one call; the workaround is retired.

### 1.4 TDD (Car 1)
- task-list write through the gate → lands `branch IS NULL`, not rejected (regression test).
- non-git project → task-list write lands canonical (Car 0 non-git signal).
- restore nudge still resolves the canonical page under a feature-branch caller (guards a Car 1 regression).

---

## Car 2 — ADR-consultable (recall-native records)

**Depends on:** Car 0. Body largely as the prior audit, revised for the branch model.

### C.1 Storage-model decision
Reuse the wiki store — one page per ADR + one thin index page. No new `store_type`, no memory-store migration. Wiki pages never decay (`_shared/storage/wiki.py:180-211`) → ADR immortality for free; wiki is a first-class recall source (default profile). A memory-store ADR would need `tier="semantic_immortal"` to get what wiki gives natively, and would fragment the record. ADR stays a DISTINCT TYPE via the `["adr"]` tag + `<project>-adr-NNNN` slug + (Car 0 §0.4) the `adr` page_type.

### C.2 Per-ADR record design
- **Slug:** `<project>-adr-NNNN` (zero-padded 4-digit). `<project>` derived per project from each `<project>-adr-log` slug (migration).
- **Title:** `ADR-NNNN: <title>`. **Content:** existing `_build_adr_body`/`to_markdown_body()` (`adr.py:144,176`) verbatim.
- **Tags:** `["adr","decisions","adr-status:<status>","adr-<NNNN>"]`. **page_type:** `adr` (Car 0 §0.4). **Category:** `decision`.
- **`directory_context`:** each ADR page carries its OWN project's directory_context (migration threads it per project) — NOT `global`, NOT hardcoded yadgar.
- **Branch:** **CANONICAL (branch=NULL) via Car 0's canonical write** — NOT default-branch-pinned. This closes the 531352 bug: canonical pages resolve via §25 step-2 (dir + branch IS NULL) from ANY caller branch AND in non-git dirs (where "master" is bogus). This REVERSES the memory-531352 "pin ADR-log to default branch" decision (record a superseding ADR).
- **Recall visibility:** `"adr"` NOT in `wiki_exclude` (`recall_pipeline.py:432`) → ADR pages surface in default-profile recall.

### C.3 Thin index design
One small wiki page per project `<project>-adr-index`, tagged `["adr","adr-index"]`, **canonical (NULL)**, carrying the project's directory_context. Metadata table only (`| ADR | Status | Date | Title | Supersedes | Superseded-by | Slug |`) so `wiki_read` never errors. Index is the ID source of truth (max+1, under the existing `_adr_log_lock` `adr.py:63`); supersede-chains flip the target's `adr-status:*` tag via `wiki_update(fields={"tags":[...]})` (NOT `wiki_set_metadata` — it rejects tags). Index version cost accepted (~150-char rows).

### C.4 Tools
- `adr_add` (rewrite): write per-ADR page **via Car 0's canonical helper** + append index row + patch supersede targets. Must still use `force=True` to bypass the 0.80 sim gate (`dlq.py:279`) — near-identical/reversing ADRs are legitimately distinct. Signature unchanged.
- `adr_list(status=None)` (new, thin): read the index; optional status filter. Deterministic "show all open".
- `adr_get(adr_id)` (new, thin): `wiki_read(<project>-adr-NNNN)` canonical — direct fetch, no branch footgun.

### C.5 Read-path surfacing (north star)
- `project_brief` `## Recent ADRs` = temporal (newest-N). Explicit `recall(query)` (default profile → wiki fanout) = semantic. Complementary; neither alone covers planning.
- **`_build_adr_log` + `_get_adr_log_updated_at` + the `## Recent ADRs` block ALL re-point to the canonical index page.** The monolith is DELETED in migration (§C.6); if `_build_adr_log` (`project.py:1837`) keeps reading `<project>-adr-log` with `branch_hint=default`, restore mode reads a deleted slug and silently returns empty. This resolves the "ADR-log asymmetry" ⚠ as a side effect of the canonical move — one fix, three call sites; a builder must not leave one dangling.

#### C.5.1 Auto-recall gap + fix (composed discipline)
The per-turn auto-recall hook uses `profile="fast"` → skips the wiki arm (`_should_skip_wiki` `recall_pipeline.py:300,329`) → ADRs do NOT auto-surface each turn. Fix = a **composed agent-prompt discipline** `agent-discipline-adr-consult` (NOT a prose convention — fires only if the model remembers). Content: *"Before planning / building / debugging a subsystem: `recall(type='wiki', tags=['adr'])` + `adr_list(status='open')` for the touched area. Observed ADR decisions BIND. Uses the default recall profile — fans out to wiki, unlike the fast-profile auto-recall hook."* Compose into `plan-executing-build`, `build-car`, `scope-and-plan`, `rca-diagnose`, `debug-investigate` so `agent_dispatch_prelude` injects it into every dispatch. `project_brief` Recent-ADRs kept. Honest residual gap: disciplines fire only for DISPATCHED agents; main-thread inline planning covered by (a) the HARD RULE that the main thread builds dispatches from `agent_dispatch_prelude` (sees the discipline) + (b) `project_brief` recency. Rejected: flipping the hot auto-recall hook to include wiki (latency regression, ADR-0077 made it memory-only deliberately).

#### C.5.2 project_brief wiring
`_build_recent_adrs(storage, resolved, limit=3)` (metadata read of the canonical index). Render `## Recent ADRs` after Hot Memories (`project.py:257`); include in catalog/restore/full, EXCLUDE from signals. Confirm during impl which mode the SessionStart hook requests (default `catalog`, `project.py:2027`); if `signals`, the block won't render at session start — either fit a compact line into the signals budget or accept catalog/restore/full-only + rely on the C.5.1 discipline.

### C.6 Migration (ALL projects: audit → migrate → DELETE monolith + fix mis-pins)
Project-agnostic idempotent `scripts/migrate_adr_monolith.py` (user-invoked; hand over via `MIGRATION_NOTES.md`; no auto-apply). Enumerate every `<project>-adr-log` page (`SELECT slug, directory_context, branch FROM wiki_page WHERE slug LIKE '%-adr-log'`); derive `<project>`; thread each page's OWN `directory_context` + canonical target. Per project:
1. Read the monolith (daemon returns full content; only the MCP client truncates — server-side migration unaffected).
2. Parse each `## ADR-NNNN` section (`_ADR_HEADER_RE` `adr.py:82`) → 9 bullets + title + status.
3. **Audit — drop deprecated (§C.6.1) BEFORE emitting.**
4. Emit one canonical per-ADR page per SURVIVING ADR (via Car 0's canonical write, `force=True`, original `date` + `NNNN` verbatim, never renumber).
5. Build the per-project canonical index; two-pass supersede back-links.
6. Verify: page count == surviving; index rows == pages; supersede links resolve; no stray `<project>-adr-*` on a feature branch (branch-drift scan).
7. **DELETE the monolith** (`wiki_delete(<project>-adr-log)`) after verify — gated behind `--delete-monolith` + successful verify. Removes the write-only page + version bloat. (Also removes the mis-pin: `aws-work-adr-log` master→gone; its ADRs re-land canonical, readable in the non-git dir.)
8. Idempotency: skip existing `<project>-adr-NNNN` slugs. `--dry-run` prints per project: "would migrate N (M dropped deprecated), create index, resolve K links, DELETE <project>-adr-log".

**Risks:** ID gaps preserved verbatim (never renumber); supersede two-pass; large read RESOLVED (daemon returns full content); branch-drift scan per project; cross-project scale (resumable via idempotent skip; isolate failures — one bad parse must not abort other projects).

#### C.6.1 Deprecated-ADR audit rule (chain-safe)
Decided by the ADR's own `status` with a supersede-chain safeguard:
- `status == superseded` → **RETAIN** (it is a supersede target — dropping dangles the chain; "why reversed" is debug gold).
- `status ∈ {rejected, deprecated}` AND no inbound `supersedes:` reference → **DROP**.
- `status ∈ {rejected, deprecated}` WITH an inbound reference → **RETAIN** (chain integrity wins).
- `status ∈ {open, accepted}` → **RETAIN**.
Compute inbound references in the same two-pass that builds `Superseded-by` back-links.

### C.7 Agent-prompt seed-sync work-item
`agent-discipline-adr-consult` must land in BOTH the live wiki AND the repo seed or they drift on re-seed. Seed source: `yadgar/core/seed/materials/agent_prompts.yaml` (keys `contract:`/`prompts:`/`disciplines:`; loaded by `agent_prompts.py` — `_load_genesis_yaml`, `seed_agent_prompts:355`, `_seed_discipline_pages:324`). Work-item: review both places for drift → add `- name: adr-consult` under `disciplines:` → add `[[agent-discipline-adr-consult]]` to the `## Composes` list + a body reference of the 5 patterns → sync the live wiki (seeder is create-if-absent, so live edits are a separate explicit step) → add a seed test so drift fails CI.

### C.8 Part B — memorize soft-gate + memory_update re-embed
- **memorize soft-gate (default ON):** a non-blocking similarity check for durable writes, triggered on caller-settable signals `tags ∩ {feedback, decision, _anchor}` OR `is_protected=True` OR any `tier` set (NOT `store_type` — it is "episodic" at gate-time, set by the CLS classifier post-gate). Embeds content (embed phase already runs), returns `near_duplicates: [{id, content, score}]` WITHOUT blocking. Config (register in `config_registry.py`+`config.py`): `YADGAR_MEMORIZE_SIM_GATE_ENABLED=true`, `YADGAR_MEMORIZE_SIM_THRESHOLD=0.85` (CONFIGURABLE knob, NOT hardcoded; calibrate before default-on, mirror the wiki-0.80 calibration at `config.py:319`), `YADGAR_MEMORIZE_SIM_TOP_K=3`.
- **memory_update re-embed (option a):** extend `memory_update` to re-embed ONLY when `content` actually changes (content-change guard; metadata-only patches stay cheap). Backend `admin_exec/memory.py:79` → `update_memory_fields` (`storage/memory.py:964`) never re-encodes today → a content patch keeps a stale vector. No new `memory_replace` tool.
- **Agent-discipline (write-side):** *"Before a DURABLE write, `recall` the topic. Near-duplicate → UPDATE-in-place (`memory_update` / wiki `replace_slug`). Contradicts observed state → mark stale (`memory_update is_stale=true`) or supersede (ADR). Episodic: skip."* Added to the stop-hook write-back prompt + `agent-instructions.md`. Read-side ADR-consult is the composed discipline (C.5.1), not a prose line.

### C.9 TDD (Car 2)
ID-sequential from index; recall-surfaces-ADR (default profile) + fast-profile does NOT (documents C.5.1); branch-pin-resolves (canonical read from a feature-branch AND a non-git cwd); index-integrity (supersede flips tag + back-link; `adr_list(status=open)` excludes superseded); migration-all-projects (per-project directory_context, IDs per project, idempotent, monolith deleted only after verify, `aws-work-adr-log` re-lands canonical); migration-deprecated-audit (rejected-no-inbound dropped, superseded retained, deprecated-with-inbound retained); `_build_adr_log` reads the index post-migration (not the deleted monolith); project_brief-render (Recent ADRs in catalog/restore/full, absent in signals); memorize-soft-gate (durable near-dup returns near_duplicates + still stores; episodic bypasses; threshold boundary; gate honors ENABLED=false); memory_update-reembed (content-change re-embeds; same-value + tags-only do not); adr-consult-discipline seeded + composed + in prelude + seed/wiki sync.

---

## Stop-hook / SessionStart / dispatch-prelude branch audit

Every wiki read/write in the three flows, under the NEW branch model. Verified against `origin/master`@`aed223c9`. (An investigator supplied a draft; the task-list-write row is CORRECTED here — it marked "✅ correct" but the deployed write is hard-rejected.)

| Site (file:line) | Slug / page | R/W | Branch handling today | Correct under new model? | Car |
|---|---|---|---|---|---|
| stop step 1 ADR read (`adr.py:265`) | `{proj}-adr-log` | R | `branch_hint=default` | **NO** — non-git = bogus "master"; becomes canonical index read | Car 2 |
| stop step 1 ADR write (`adr.py:296,320`) | `{proj}-adr-log` | W | `branch_hint=default` | **NO** — becomes canonical per-ADR write | Car 2 |
| stop step 2 structural (`template:67-70`) | topic page (caller) | W | `branch_hint=default` | OK for git; **non-git = bogus "master"** → needs the non-git canonical default | Car 0 |
| stop step 3 agent-prompt read (`template:78`) | `agent-prompt-*` | R | none (global-scoped) | OK — shared/global by design | — |
| stop step 3 agent-prompt write (`template:85`) | `agent-prompt-*` | W | none → `agent_prompt_save` | OK — global; confirm it writes canonical/global not caller-branch | Car 2 (confirm) |
| stop step 4b task-list read (`template:97`) | `{proj}-task-list` | R | none (canonical) | OK — reads canonical | — |
| **stop step 4c task-list WRITE (`template:109-132`)** | `{proj}-task-list` | W | **none (intended canonical) → HARD-REJECTED `missing_branch`** | **NO — BROKEN as deployed; routed through Car 0 canonical path** | **Car 1** |
| SessionStart nudge (`http.py:889`) | `{proj}-task-list` | R | `branch_hint` (resolves NULL via §25 step-2) | OK — canonical read works | — |
| project_brief `_build_adr_log` (`project.py:1837`) | `{proj}-adr-log` | R | `branch_hint=default` | **NO** — non-git/feature-branch sees empty; re-point to canonical index | Car 2 |
| project_brief `_get_adr_log_updated_at` (`project.py:1340`) | `{proj}-adr-log` | R | `branch_hint=default` | **NO** — same; re-point to canonical index | Car 2 |
| project_brief wiki catalog (`project.py:~402,460`) | all | R | directory-scoped | OK | — |
| dispatch_prelude contract/prompt read (`dispatch_helper.py`) | `agent-prompt-*` | R | none (global) | OK — global | — |
| dispatch_prelude context recall (`dispatch_helper.py`) | memories + wiki | R | `branch_hint` propagated | OK | — |

**Other default-branch-pin landmines (like 531352):** the ADR-log reads/writes above are the family. The `{default_branch}` → "master" fallback (`template:5-6`, `_get_default_branch:154`) is the ROOT — Car 0's non-git signal fixes it for all pages; Car 2 moves ADRs off default-pin entirely (canonical). Agent-prompt pages are global (not default-pinned) — safe. Task-list is canonical-by-design — the only remaining fix is Car 1's write path.

---

## Sequencing

| Car | Deliverable | Depends on |
|---|---|---|
| 0 | Canonical-write helper (`_internal` server-side) + git-aware gating + non-git host signal + `adr` page_type + I32 allowlist constant | — |
| 1 | Task-list write routed through Car 0 canonical path; template step 4c fixed; gated-write regression test | 0 |
| 2 | Per-ADR canonical pages + index; `adr_add`/`adr_list`/`adr_get`; migration (audit→migrate→DELETE→fix mis-pins); `_build_adr_log`+`_get_adr_log_updated_at`+`## Recent ADRs` re-point to index; `agent-discipline-adr-consult` (+ seed-sync); memorize soft-gate; memory_update re-embed; superseding ADR for 531352 | 0 |

Loop-until-clean: `pytest` on touched suites + lint/types after each car. Per-file `-n0`; no full sweeps.
