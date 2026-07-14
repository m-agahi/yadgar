> ARCHIVED 2026-07-14 — executing on car/tasklist-mirror, ships with this PR.

# Harness Task-List Mirror (persist TaskCreate list across instances)

**Status:** DRAFT — awaiting audit
**Date:** 2026-07-14
**Scope:** core template (`yadgar/core/hooks/templates/stop_checkpoint_prompt.md`), page-type schema (`yadgar/_shared/schemas/wiki_page_types.yaml` — add `task_list`), backend endpoint (`yadgar/core/server/http.py` — `hook_session_context`), tests (`yadgar/tests/core/`), version bump. NO `project_brief` change + NO new write tool (deliberate — see Design edit 3 + Schema enforcement).
**Tracks:** user-reported loss of the Claude Code harness task list on session exit.
**Target version:** core next-minor (design only — no version bump in this doc).

---

## BLUF

The Claude Code harness task list (`TaskCreate`/`TaskList`/`TaskUpdate`) is **session-only**. On exit / `/clear` it is lost — the user hit this. Yadgar's `checkpoint(next_steps=…)` is freeform prose, NOT the structured harness list (`id`/`subject`/`status`/`blocks`/`blockedBy`), so it is not a substitute.

**Locked direction (user decision):** ONE **flat** per-project wiki page `<project>-task-list`, **read-before-write**, **instruction-driven**. Explicitly REJECTED: hook-writes-harness-files (AUTO), full-content session-start injection (INJECT), and per-`session_id` sectioning. The list is restored by the instance calling `TaskCreate`, prompted by an always-present-but-existence-checked session-start line.

**Three edits, one behavioral, main-thread-only:**

1. **Reconcile-then-mirror (write), a 4-case state machine.** New step in `stop_checkpoint_prompt.md`. The instance FIRST reconciles its OWN live harness list (`TaskUpdate` done/blocked, `TaskCreate` discovered — automates the manual "update your task list" nudge), then branches on {have-tasks?} × {page-exists?} (see Design edit 1). Read-before-write always; **full-rewrite** the flat page on write (surgical tools optional, see Refinement-3 analysis).

2. **Restore-nudge (surface, MAIN-ONLY, existence-checked server-side).** In `hook_session_context` (`http.py:~953`, beside the checkpoint-resume hint, gated `source != "compact"`): the endpoint does a **cheap server-side existence check** for `<project>-task-list` and injects the restore line ONLY if the page exists. No dead nudge, no wasted `wiki_read`. Placed at the endpoint layer, NOT inside `project_brief` — proven main-thread-only.

3. **NO `project_brief` change** (deliberate). `project_brief` is in subagent tool allowlists (`general-purpose.md`, `cavecrew-*.md`) — a subagent can call it directly; an instruction in its return would leak into agent context. The nudge lives ONLY at the endpoint layer.

**Mechanism: wiki page, not memory block.** User accepts minor agent content-noise. Wiki = full fidelity (no `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT=2000` cap that would truncate descriptions + related-context pointers). The *instruction* stays main-only via endpoint placement (edit 2) → no behavioral interference in agents; only benign ranked-recall content-noise, accepted.

---

## Concurrency posture (user downgraded it — flat page, no sectioning)

The user chose a flat page and rejected `session_id` sectioning, accepting that concurrent same-project instances are rare (single-user-usually-one-instance) and that read-before-write + the case-3 catch-up-sync (below) is "good enough". Consequences, honestly stated:

- **No cross-instance clobber isolation.** Two instances writing the flat page near-simultaneously → lost-update (last full-rewrite wins). `wiki_add(wait=True)` has **no CAS / `expected_version` / optimistic lock** (`wiki.py` grepped — none); versioning (`wiki_history`) is audit-only. Read-before-write shrinks but does not eliminate the TOCTOU window.
- **Mitigation retained without sectioning:** case-3 **catch-up-sync** (a task-less session adopts the page's open tasks) means a session that lost the race can re-absorb the winner's list at its next checkpoint. Convergence-by-catch-up, not locking. Acceptable per the user's downgrade.
- **Optional softener (Refinement 3):** surgical per-task edits (`wiki_replace_text` on one status line) are line-atomic → two instances editing *different* tasks don't clobber each other. This recovers *some* concurrency safety without session_id — but only where the edits are genuinely disjoint, and at the cost of extra tool-calls. Recommended sparingly (see analysis), not as the default.

---

## Verification findings (verified against code, file:line)

### Instance can reconcile own list at checkpoint time
- The stop-hook `block` fires in the **main thread**, where `TaskList`/`TaskCreate`/`TaskUpdate` are available. Reconcile-own-list-first is unblocked. **This stays step 1.**

### Harness task list is file-backed (context, not used by this design)
- `~/.claude/tasks/<session_id>/<N>.json`, one file per task, written **live** (this session's files had mtimes marching 1-2s apart during active work — the harness writes each create/update through to disk). Format has already drifted across harness versions (e.g. an `activeForm` field present in newer task files, absent in older) — **direct evidence for why AUTO (hook writing these files) was correctly rejected: undocumented, drift-prone internals.** This design touches NONE of it; the list flows via `TaskList`/`TaskCreate` tools only.

### Stop hook is a dumb pipe — writes must be instance-driven
- `stop-memory-checkpoint.py` does no writes; emits a `block` pointing at the template (`:200-204`, `_PROMPT_TEMPLATE_PATH` resolves `stop_checkpoint_prompt.md`). Mirror = instance MCP calls. Adding the step = **template-only edit**; no `.py` change.

### Server-side existence check IS cheap + available at http.py:953 (edit-2 mechanism)
- The checkpoint-hint block already calls `_storage.get_active_checkpoint(directory)` server-side inside `if source != "compact":` (`http.py:957`). A wiki existence check of the SAME cost is available: `get_wiki_page_by_slug` / `get_wiki_page_by_slug_directory_branch` (`storage/wiki.py:390,459`) — a metadata row read, no embedding/recall. So the endpoint can pre-check page existence and inject the line only when present, at parity cost with the existing hint. **This makes server-side pre-check the recommended mechanism** (see Refinement 2).

### Session-context endpoint is MAIN-THREAD-ONLY (edit-2 safety proof)
- Distinct hook events: `.claude/settings.json` + global `~/.claude/settings.json` register `SessionStart` → `hook_runner.py session-start-context` and `SubagentStart` → `yadgar-subagent-start.py` **separately**. SessionStart fires top-level only; subagents fire SubagentStart.
- `GET /hooks/session-context` called from ONE Claude path: `hook_session_start_context()` (`hook_runner.py:126-142`). Other caller = `graph.html:71` (web UI, not a Claude path). No subagent path calls it.
- SubagentStart endpoint `hook_subagent_start` (`http.py:1690-1795`) = `recall()` only; `agent_dispatch_prelude._build_context_block` (`dispatch_helper.py:470-520`) = `recall()` + `wiki_query()` only; `project_brief` absent from `dispatch_helper.py`.
- Render gating: `source=="compact"` early-returns (`http.py:912`); block-render (`:930`) + checkpoint-hint (`:953`) both gated `source != "compact"`, both inside this one handler. Restore-nudge slots beside the checkpoint hint → same handler, same gate, same main-only reach. **VERDICT: TRUE** — unreachable by any subagent. Caveat (not a blocker): skipped on `source=="compact"`; `/hooks/post-compact`→`restore()` owns compact, page persists to next non-compact start.

### Wiki vs block — interference axis
- `recall` type enum `{"all","memory","wiki"}` (`recall.py:32`) — blocks NOT recall-indexed (separate `memory_block` table, migration 012). Blocks render main-thread-only (3 push-paths). Block would leak nowhere but caps at 2000/8000 chars; wiki has full fidelity but IS recall/wiki_query indexed → benign content-noise in agents. **User chose wiki.**

### Surgical wiki primitives (Refinement 3 inventory)
- `wiki_append_section(position=…)` — section-atomic, **synchronous** (no queue), `wait` no-op (`wiki.py:1022`). Add/replace a `##` block.
- `wiki_replace_text(old,new,occurrences)` — surgical anchor-text swap; single-line status flip. Rejects on ambiguous/absent match.
- `wiki_delete_text` — drop a completed task's text.
- `wiki_insert_after`/`wiki_insert_before` — add a task next to an anchor.
- `wiki_replace_markdown_block` — replace the Nth block of a type.
- `wiki_add(replace_slug=…, wait=True)` — full-rewrite; enqueue→drainer→poll (`_wiki_add_wait_path`, `wiki.py:74`).

---

## Refinement analyses (the three questions)

### R1 — case-3 (no-tasks + page-exists → sync): stale-resurrection guard

The catch-up-sync covers two legit cases: (a) session-start restore was skipped/missed; (b) a concurrent session wrote a list this session hasn't absorbed. The risk: a session that **genuinely finished all its work** re-adopting an OLD lingering page = zombie tasks.

**Proposed guard (per-task status filter) HOLDS but is INSUFFICIENT alone.** The page stores per-task `[status]`, so sync adopts ONLY tasks whose status is NOT completed. Necessary. But it misses the case where the page's open tasks were actually completed in a PRIOR session that forgot to update the page → those stale-open tasks get resurrected.

**Recommended discriminator stack (all three, cheap):**
1. **Status filter** — adopt only `[pending]`/`[in-progress]`/`[blocked]` tasks; never `[completed]`. If ALL page tasks are completed → SKIP (no adoption, optionally rewrite to prune the page).
2. **Age gate** — if the page's last-modified (`wiki_history` newest `created_at`, or a `updated:` line the mirror writes) is older than a freshness window (propose 14 days), do NOT auto-adopt; instead surface "stale saved list found — review before restoring". Bounds zombie risk to recently-active pages.
3. **Prompted, not blind** — adoption is an instruction step where the model judges relevance ("adopt open tasks that match the work you're about to do; skip ones clearly from unrelated/finished efforts"). Instruction-driven means a human-in-the-loop-ish judgment, not a mechanical replay.

No better single discriminator exists without stable cross-session identity (which the flat/no-session_id choice gave up). Status + age + judgment is the honest best. **FLAG:** case-3 is the softest part of the whole design — it trades determinism for the flat-page simplicity the user chose. Documented as accepted.

### R2 — session-start line: server-side pre-check vs self-checking instruction

**Recommend SERVER-SIDE PRE-CHECK.** Rationale: the check is cheap and already-shaped at the injection point — `get_wiki_page_by_slug_directory_branch` (`wiki.py:459`) is a metadata read at parity with the `get_active_checkpoint` call the checkpoint-hint block already makes (`http.py:957`). Injecting the line only when the page exists means: zero dead nudges, zero wasted model `wiki_read` round-trips, and no standing "IF exists…" noise on every fresh project that has never saved a list. The self-checking-instruction alternative works (model's `wiki_read` returns not-found → skips) but spends a model tool-call + tokens on every session start of every project, most of which have no page. Server-side pre-check is strictly better here because the check is already affordable server-side. (Fail-open: wrap in try/except like the sibling hint blocks `http.py:946,977` — any error → simply omit the line.)

### R3 — surgical per-task edits vs full-rewrite: is surgical worth it?

**Recommend KEEP-IT-SIMPLE: full-rewrite (`wiki_add(replace_slug, wait=True)`) as the default.** For a few-KB flat list, a single full-rewrite after the read-merge is the simplest correct write: one atomic call, no anchor-uniqueness/section-not-found failure modes, no multi-call orchestration. Surgical tools earn their complexity on LARGE pages or for TRUE concurrent-section isolation — but the user dropped sectioning, and a task list is small. The extra tool-calls + brittleness (e.g. `wiki_replace_text` rejects on non-unique status lines — two tasks both `[pending]` on their own lines can trip anchor ambiguity) are not worth it for the common path.

**Narrow exception where surgical clearly helps (corrected primitive):** updating ONE task on an existing page — `wiki_append_section(slug="{project}-task-list", section_heading="task:<id>", position="replace_section", heading_type="h2", content=<that task's body>)`. Because the schema makes each task a UNIQUE `## task:<id>` section, `replace_section` swaps exactly that task, section-atomic, with NO anchor-ambiguity (this is why the earlier `wiki_replace_text`-on-status-line idea is dropped — two `pending` tasks made the status line non-unique; a section heading is unique). It also won't clobber a concurrent instance editing a DIFFERENT task's section. Offer as an OPTIONAL optimization ("if your only change is one task, you MAY `wiki_append_section(replace_section, "task:<id>")` instead of a full rewrite"), not the default. **FLAG:** don't grow a surgical-tool decision tree — full-rewrite is the value path; the one section-replace is the single footnote. (Heading-match gotcha: the matcher must be exact-line so `task:1` ≠ `task:12` — zero-pad ids to `task:0001` or the schema uses the delimiter form; see Schema.)

---

## Schema — the `{project}-task-list` page

### House conventions surveyed (how yadgar gives a wiki page a schema)

- **ADR log** — the canonical typed-record-→-wiki-section pattern. `adr_add` takes 10 typed params, validates required-fields + a `status` enum `{open, accepted, superseded, rejected, deprecated}` (`adr.py:77,85`), builds an `ADR` Pydantic `BaseModel` (`models.py:247`), and writes ONE `## ADR-NNNN: <title>` section per record via `wiki_append_section` (`adr.py:162,176`). The body is `- key: value` flat bullets from `ADR.to_markdown_body()` (`models.py:290`), with **continuation lines indented 2 spaces** — mandatory so a multi-line value containing a `##` line or ``` fence can't be mis-parsed as a section boundary (poisoning the `^## ADR-NNNN` id-scan + `wiki_append_section` detection). **This is the exact hazard a section-per-task list inherits.**
- **Typed wiki pages** — `page_type` + `wiki_schema_version` on `wiki_add` (`wiki.py:155,257,271`). The registry is a **packaged YAML** `yadgar/_shared/schemas/wiki_page_types.yaml` loaded by `wiki_meta.py` (`_load_page_type_schemas`); each type lists `required:` `##` headings. `wiki_lint` → `check_page_type_format` (`wiki_meta.py`) warns on missing required sections — **ADVISORY only; `wiki_add` never rejects on mismatch** (`wiki.py:184`). Adding a new page_type is a **pure data edit** to the yaml, zero code.
- **repo-wiki fn/mod pages** — `page_type="code"` + DB-carried `source_file` + `hash` columns (migration 024, `wiki.py:196,202`) for hash-based staleness; `## MANUAL NOTES` never-overwrite convention.
- **agent-prompt pages** — `page_type="agent_prompt"`, `required: [Purpose, Prompt]`, `optional: [Preconditions, Failure modes, Verification, Composes]` (yaml). Written by `agent_prompt_save` from the `AgentPrompt` model (`models.py:308`).
- **Registry reality:** yadgar has NO single schema registry — typed records live in `models.py` (Pydantic), section-templates in the page-types YAML, DB shape in migrations. The consolidation recommendation (memory) is "typed-records in models.py + per-type tool-validation, ADR-style." **That path assumes a validating tool — which this instruction-driven feature deliberately does not have** (see Enforcement).

### Status enum (verified against live harness output, NOT invented)

The harness emits exactly three status values — `grep`ed across all live `~/.claude/tasks/*/*.json`: **`pending`** (90), **`in_progress`** (7), **`completed`** (214). There is **NO `blocked` status** — blocking is expressed via the `blockedBy` array, not a status. So the enum is `{pending, in_progress, completed}`; "blocked" is a *display* derivation from non-empty `blockedBy`. Store the harness value **verbatim** (no mapping layer). (The harness also emits an `activeForm` gerund field per task; we can carry it as optional `active_form` or drop it — recommend carry, it's free context.)

### Page format

Metadata that the `wiki_page` row ALREADY carries — `directory`, `branch`, `wiki_schema_version`, `updated_at` — is NOT duplicated in the body (single source of truth; the age gate reads DB `updated_at`, which is automatic and can't be forgotten). The body carries only what the DB lacks: the human-readable meta line + per-task records.

```markdown
<!-- yadgar task-list page — schema v1. One "## task:<id>" section per task.
     Fields are "- key: value" bullets; multi-line values indent 2 spaces.
     status ∈ {pending, in_progress, completed}. Restore: recreate open tasks via TaskCreate. -->

# myapp task list

## Meta
- project: myapp
- open: 2 · completed: 1

## task:0003
- subject: Wire the retry backoff into the client
- status: in_progress
- active_form: Wiring the retry backoff
- description: Exponential backoff on 5xx; cap at 30s.
    Note: see the ## Timeouts note in the design doc — indented so this line is not a heading.
- context: src/client/retry.py · [[myapp-http-client]] · docs/plans/retry-2026-07-10.md · mem:4821
- blockedBy: 0005
- blocks:
- modified: 2026-07-14T18:20:32Z

## task:0005
- subject: Define the backoff config knob
- status: pending
- description: RETRY_MAX_BACKOFF_S, default 30.
- context: src/config.py · mem:4822
- blockedBy:
- blocks: 0003
- modified: 2026-07-14T17:55:10Z

## task:0001
- subject: Audit existing retry call sites
- status: completed
- description: 4 call sites found + catalogued.
- context: src/client/*.py
- blockedBy:
- blocks:
- modified: 2026-07-13T22:41:00Z
```

Field definitions (per task):

| field | source | notes |
|-------|--------|-------|
| `<id>` in `## task:<id>` | harness task id | **zero-padded to 4 digits** so the section matcher is exact (`task:0001` ≠ `task:0012`); addressable unit for surgical edits |
| `subject` | harness | one line |
| `status` | harness verbatim | enum `{pending, in_progress, completed}` |
| `active_form` | harness `activeForm` | optional; present-tense label |
| `description` | harness | multi-line → indent continuation 2 spaces |
| `context` | mirror-authored | related-context pointers: file paths · `[[wiki-slug]]` · `docs/plans/*.md` · `mem:<id>` — where the task's work lives |
| `blockedBy` / `blocks` | harness | space-separated padded ids; empty allowed |
| `modified` | mirror-authored | ISO-8601 UTC; bumped only on a real change to that task (per-task freshness, distinct from page-level DB `updated_at`) |

### How the schema enables surgical edits

Each task is a UNIQUE `## task:<id>` section, so:
- **Update one task** → `wiki_append_section(replace_section, "task:<id>")` — section-atomic, no anchor ambiguity, no clobber of sibling tasks.
- **Add a task** → `wiki_append_section(position="new_section_bottom", "task:<id>")`.
- **Remove a completed task** → surgical section delete (or full-rewrite prune).
- **Full rewrite** (create / bulk merge) → `wiki_add(replace_slug, page_type="task_list", wait=True)`.
The zero-pad + `##`-continuation-indent discipline is what makes section boundaries reliable — without it a task description containing a `##` line silently corrupts neighboring sections (the 2026-05-31 pattern `wiki_append_section` exists to prevent).

### Enforcement level — recommendation: **(a) + (b)**

Honest framing: the OTHER typed pages (ADR, agent_prompt, repo-wiki) get their schema from a **validating tool** that builds a `models.py` record and renders it. This feature is **instruction-driven — there is no tool**. So "a schema like the others" does not transfer cleanly; the fork:

- **(a) documented markdown-format convention** — the page format above, plus the HTML header comment on the page itself (self-documenting) + this Schema section. Carries the **per-task** shape (which nothing can lint — see below). **Adopt.**
- **(b) `page_type="task_list"` in `wiki_page_types.yaml`** — pure data edit (`required: [Meta]`, `optional: [Tasks]`). Gives the page `page_type` + `wiki_schema_version`, groups it in the catalog "like the others," and lets `wiki_lint` advisory-check the **page-level** `## Meta` heading. **Adopt** — cheap, and it's the honest reading of "a proper schema like the others." **BUT state plainly: (b) validates nothing per-task** — dynamic `## task:<id>` headings cannot sit in a fixed `required:` list, so lint covers only the fixed page-level section(s), not task structure.
- **(c) full typed-record (`TaskList`/`Task` in models.py) + a dedicated `task_list_add` validating tool** — this is the ONLY way to actually enforce the per-task schema, but it **requires a write tool, which reverses the locked instruction-driven decision.** A `models.py` record with no caller is cargo-cult. **Reject for v1**; note it as the follow-up IF the user later wants tool-enforced task-list writes (that revisits the instruction-driven lock).

Net: (a) carries the real shape, (b) makes it a first-class typed page cheaply and advisory-lints the page-level frame. Tool-enforcement (c) is deferred and explicitly gated on reversing the no-tool decision.

---

## Design

### Edit 1 — `stop_checkpoint_prompt.md` (4-case reconcile-then-mirror step)

Add a step matching the read-first shape of steps 1-3. `{project}`=`Path(directory).name`.

> **TASK-LIST MIRROR** (always).
> **Step 1 — reconcile your OWN list first.** `TaskList`; `TaskUpdate` anything completed/blocked this session; `TaskCreate` follow-ups you discovered. (This is the every-checkpoint "update your task list" pass.)
> **Step 2 — read the page:** `wiki_read("{project}-task-list", directory="{directory}", branch_hint="{default_branch}")`.
> **Step 3 — branch on {have open tasks after reconcile?} × {page exists?}:** (page format = the **Schema** section below; each task is a `## task:<id>` section)
> - **have tasks · NO page → CREATE.** `wiki_add(title="{project} task list", content=<full page: ## Meta + one ## task:<id> section each>, replace_slug="{project}-task-list", tags=["task-list"], page_type="task_list", directory="{directory}", branch_hint="{default_branch}", wait=True)`.
> - **NO tasks · NO page → SKIP.** Nothing to do.
> - **NO tasks · page EXISTS → CATCH-UP SYNC.** The page has tasks you don't. Adopt its **open** tasks (`status` ∈ {pending, in_progress}) into your harness via `TaskCreate` — recovers a missed session-start restore or a concurrent session's work. GUARD: skip `completed` tasks; if ALL page tasks are completed, or the page's DB `updated_at` (from `wiki_read`/metadata) is older than 14 days, do NOT adopt — note "stale/finished saved list" and leave it. Adopt by judgment: open tasks relevant to your work; skip ones clearly from a finished/unrelated effort.
> - **have tasks · page EXISTS → MERGE + WRITE BACK.** Reconcile the page's open tasks with yours (union; your live status wins for tasks you own; keep page-only open tasks). Default write = **full rewrite** `wiki_add(replace_slug="{project}-task-list", content=<merged full page>, page_type="task_list", …, wait=True)`. OPTIONAL surgical path (when your change is confined to one task): `wiki_append_section(slug="{project}-task-list", section_heading="task:<id>", position="replace_section", heading_type="h2", content=<that task's body>, …)` — the `## task:<id>` heading is UNIQUE so this is section-atomic and does not clobber a concurrent edit to a *different* task. (This replaces the earlier `wiki_replace_text`-on-status idea, which broke on non-unique status lines — section-per-task removes that ambiguity.)
> **Per-task body:** render fields as `- key: value` flat bullets (subject, status, description, context, blockedBy, blocks, modified) UNDER the `## task:<id>` heading — see Schema. Multi-line values (description, context) MUST indent continuation lines 2 spaces so an embedded `##`/``` ``` ``` cannot be mis-parsed as a section boundary (same discipline as ADR `to_markdown_body`, `models.py:290`). Do NOT hand-write an `updated:` stamp — the age gate reads the DB `updated_at` column. Verify `wiki_history`.

### Edit 2 — `hook_session_context` restore-nudge (existence-checked, main-only)

In `http.py`, inside `if source != "compact":` (the checkpoint-hint block, `~953`), after the `_hint` append, add:
- Server-side existence check: `get_wiki_page_by_slug_directory_branch("{project}-task-list", directory, branch)` (metadata read, parity cost with the adjacent `get_active_checkpoint`). Wrap in try/except → on any error, omit the line (fail-open).
- If the page exists, append to `render`:
  `\n[yadgar] Saved task list found ({project}-task-list). To restore: wiki_read("{project}-task-list", directory="{directory}"), then recreate the open tasks (status pending / in_progress) with TaskCreate before proceeding (skip completed).\n`
- Gated `source != "compact"` (inherits enclosing block). Slug `{project}-task-list`, `{project}`=`Path(directory).name`.

### Edit 3 — NO `project_brief` change

Documented deliberate omission so a future contributor doesn't move the nudge into `project_brief` (subagent-callable → leak). Nudge lives ONLY at the endpoint.

---

## Test plan (TDD — red → green)

Mirror `test_stop_hook_template.py`, `test_v5_53_1_curation_loop.py`.

1. **Endpoint existence-check test (load-bearing).**
   - Seed `<project>-task-list` for a dir → `GET /hooks/session-context?…&source=startup` output CONTAINS the restore-nudge.
   - No page seeded → output does NOT contain it (existence check false — the key R2 assertion).
   - `source=compact` → nudge ABSENT (early-return).
   - **Isolation:** `hook_subagent_start` + `agent_dispatch_prelude` for same dir/topic → nudge ABSENT from both (locks main-only).
   - **Fail-open:** existence check raising → endpoint still returns the rest of the render (no 500).
2. **Template test.** Assert `stop_checkpoint_prompt.md` contains: reconcile-own-list-first (`TaskList`/`TaskUpdate`/`TaskCreate`), the FOUR named cases (create / skip / catch-up-sync / merge-write-back), the catch-up guard (skip `completed`, all-done→skip, 14-day age gate on DB `updated_at`), the `## task:<id>` section format + `- key: value` fields + status enum `{pending, in_progress, completed}`, the `context:` related-pointers clause, the 2-space continuation-indent rule, and the optional `wiki_append_section(replace_section, "task:<id>")` surgical note. Assert placeholder tokens present.
3. **Schema / page-type test.** Assert `wiki_page_types.yaml` gains a `task_list` entry (`required: [Meta]`); assert `check_page_type_format` on a well-formed task-list page returns zero issues and on a page missing `## Meta` returns a `missing_section` warning. Assert a `wiki_add(page_type="task_list", …)` round-trips `page_type` + `wiki_schema_version` (metadata read).
4. **Section-boundary robustness test (load-bearing for surgical safety).** Build a task-list page where one task's `description` contains a `## Foo` line and a ``` fence; assert (via the same section-parser `wiki_append_section` uses) that task-section boundaries are still detected correctly (the 2-space indent keeps the embedded `##` off column 0) and that a `replace_section("task:<other-id>")` leaves the poisoned task's section byte-identical.
5. **Catch-up-guard test (behavioral, if a wiki-seed fixture exists).** Seed a page whose tasks are ALL `completed` → assert the prompt's guard text instructs SKIP (test the instruction phrasing; adoption is model-driven). If no fixture, mark manual.

No application-logic module changes beyond the endpoint edit + the yaml data-edit — mirror is prompt-driven; template + endpoint + schema tests carry the load.

---

## Failure modes

- **Empty list, no page** — SKIP; no write. Nudge existence-conditional (server pre-check → absent).
- **Stale resurrection (case 3)** — guarded by status-filter (skip `[completed]`) + 14-day age gate + model judgment. Softest part of the design; residual = a recently-updated page with genuinely-finished-but-unmarked open tasks could resurrect. Accepted cost of the flat/no-session_id simplicity.
- **Concurrent same-project instances** — flat page, last-full-rewrite-wins (no CAS in `wiki.py`). Read-before-write narrows the window; case-3 catch-up-sync converges the loser next checkpoint. Optional per-task `wiki_append_section(replace_section, "task:<id>")` gives section-atomic disjoint-edit safety where used. Rare (single-user-usually-one-instance).
- **Stale within a live list** — completed snapshot persists until next checkpoint rewrites. Bounded = one checkpoint interval. DB `updated_at` (page) + per-task `modified` make staleness legible.
- **Section-boundary poisoning** — a task `description`/`context` containing a column-0 `##` or ``` fence could be mis-parsed as a section boundary, corrupting a surgical edit. MITIGATED by the mandatory 2-space continuation-indent (schema), same discipline as ADR `to_markdown_body` (`models.py:290`). Test #4 locks it.
- **Section id-collision** — `task:1` vs `task:12` if the heading matcher is prefix-based. MITIGATED by zero-padding ids to `task:0001` (schema). Confirm the matcher is exact-line at implementation.
- **Soft-nudge, not enforced** — restore/adopt require the model to act on injected text; not guaranteed. Same reliability class as existing `restore()`/checkpoint hints (`http.py:953`).
- **Compact path** — nudge skipped on `source=compact` (early return). Page persists; next non-compact start surfaces it.
- **Lint is advisory** — `page_type="task_list"` lint warns on a missing `## Meta` but NEVER rejects a write, and canNOT validate per-task structure (dynamic headings). Per-task correctness rests on the prompt (a) + tests, not lint.

---

## Rollout / scope

- Core: template edit (`stop_checkpoint_prompt.md`).
- Schema (data): add `task_list` block to `yadgar/_shared/schemas/wiki_page_types.yaml` (`required: [Meta]`) — enforcement tier (b). Pure data, zero code.
- Backend: `http.py` `hook_session_context` edit (existence check + line).
- Freshness window (14 days) + slug: hardcode in prompt + endpoint (prompt-driven feature — avoid a config knob unless the audit wants one).
- Tests: endpoint existence/isolation/fail-open + template + schema/page-type + section-boundary robustness + optional catch-up-guard.
- Version bump on ship (core next-minor; assigned at ship per ROADMAP convention).
- Register in `ROADMAP.md` open-plans (this doc) + append to `yadgar-roadmap-future-improvements` wiki backlog (left for main thread — durable MCP write, outside this read-only design scope).

No migration, no new store, no harness-internal writes, no new tool (tier (c) deferred). Three files (template + yaml + http.py) + tests.
