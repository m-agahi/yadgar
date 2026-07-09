> ARCHIVED 2026-07-09 — SHIPPED: all phases shipped — P0 #121 (eeaec40), P1 adr_add tool + P2 project_brief ADR surfacing + P3 models.py ADR shape + #19 read-side branch_hint all in #124 (6e1629cb); B5 anchor-signal shipped #177 (a4390c4e).

# PLAN — ADR capture system (Yadgar wiki as source of truth)

Status: **PHASED — P0 SHIPPED (PR #121, `eeaec40`); P1–P3 + #19-read-side open.** Designed 2026-06-24 (human + instance, item-by-item review + opus design agent + advisor).

> **AUDIT 2026-06-25 (improvement-train #29, group B).** Verified against current code:
> - **P0 is DONE — shipped in PR #121 (`eeaec40` / `ea83d70`), deployed.** The
>   stop-hook `_PROMPT_TEMPLATE` (`yadgar/hooks/stop-memory-checkpoint.py:26`) is
>   capture-first (ADR step 1 → structural step 2 → project_brief step 3 →
>   maintenance step 4; preamble line 27), uses `<project>-adr-log` + tag `adr`
>   (line 34), `wiki_add`/`wiki_append_section` with the explicit "do NOT
>   wiki_approve" note (lines 36-40, 65), mandatory 11-field schema (lines 51-62),
>   `branch_hint="{default_branch}"` WRITE-side (lines 37/66/72/74, computed by
>   `_default_branch()` at line 150, substituted in `main()` at 242-245), dead
>   `refresh_stale_wiki` removed, footer "Checkpoint cadence reached" (line 98).
>   Test `test_stop_memory_checkpoint_module.py` green — now **20 tests** (plan
>   said 17). Installed copy via `install_hooks_lib.py:129-130`. **The "P0 shipping
>   in THIS PR" header was stale → flipped to SHIPPED above.**
> - **#19 (stop-hook ADR READ-side branch_hint) added below** as P0.5 — write-side
>   shipped, read-side gap is real (see new §P0.5).
> - **Bug A line drift:** the dead gate is now at `project.py:1862` (not 1865-66) and
>   reads `fm.get("source_files") or fm.get("sources")` — BOTH wrong; disk frontmatter
>   key is `source_file` (singular). Fix must add `source_file`. Detail corrected in
>   §Bug fixes below.
> - **Bug B nuance:** the IN-REPO generator `sync_instructions()`
>   (`yadgar/server/tools/misc.py:517`) is ALREADY CORRECT (says `wiki_add`, no
>   `wiki_approve`). The stale "wiki_add + wiki_approve for new pages" text survives
>   ONLY in Max's nix dotfiles (`~/git/nix`, OUT OF THIS REPO — unverifiable here)
>   and one archived plan. So Bug B's in-repo surface is effectively closed; the real
>   fix is the out-of-repo nix file. Detail corrected in §Bug fixes.
> - **P1/P2/P3 still unbuilt** (no `adr_add`/`adr_due`/`decide` in code).
>   `yadgar/models.py` exists, **234 lines, 16 record classes** — already a
>   substantial de-facto schema home, so P3 (1) "consolidate scattered shapes into
>   models.py" is partly done; the ADR shape is the missing piece. (An earlier
>   verification pass mis-measured it at 56 lines; the real figure is 234/16.)
>
> ---
>
> > **AUDIT 2026-06-26 (v5.85 train, car #37) — STALE-CLAIM FLIP. The 2026-06-25
> > note above is now WRONG on the load-bearing point.** Verified directly against
> > current `master`:
> > - **P1 IS SHIPPED.** `adr_add` is a live MCP tool — `yadgar/server/tools/adr.py`
> >   (285 lines), registered at `yadgar/server/tools/__init__.py:128` (import) +
> >   `:204` (`__all__`). `adr_due` nudge is wired into `project_brief` via
> >   `_apply_adr_signal(...)` (`project.py:1428`, called at `:1570`). Landed in
> >   commits `0940ed4` (feat) + `a61f445` (test) → merged in `6e1629c` (#124). The
> >   2026-06-25 audit's "no `adr_add`/`adr_due`/`decide` in code" claim is FALSE on
> >   today's tree. **This is the exact C1/C2 trap from the previous train — a plan
> >   claim that went stale between writing and the next train. P2 (project_brief
> >   first-class ADR surface) is partly done via the `adr_due` signal; P3 schema
> >   home unchanged.**
> > - **What car #37 ACTUALLY is now:** the *prompt-migration* — move the stop-hook
> >   ADR capture from manual `wiki_read`+`wiki_append_section` (still present at
> >   `yadgar/hooks/stop-memory-checkpoint.py:35` read / `:65` append) onto the
> >   now-shipped `adr_add` tool. P1's tool is done; the hook still hand-rolls the
> >   capture. See the new §P1-migration below for the concrete, dedup-preserving spec.
> > - **`adr_add` dedup behavior (verified):** `adr_add` reads the existing log
> >   first (`adr.py:215`, `branch_hint=default_branch`) and assigns the next
> >   sequential id via `_next_adr_id` (`adr.py:92`) — i.e. **ID-collision dedup
> >   ONLY**, NOT decision-level dedup. The current prompt does "dedup by decision,
> >   not wording" (`stop-memory-checkpoint.py:49`). A naive "lean: if adr_due call
> >   adr_add" migration would therefore **REGRESS** the decision-dedup the task
> >   requires us to keep. The migration MUST preserve it — see §P1-migration.
>
> ## P1-migration — stop-hook ADR capture → `adr_add` (car #37, v5.85)
> Replace the hand-rolled ADR capture in `_PROMPT_TEMPLATE`
> (`stop-memory-checkpoint.py:34-67`) with a call to the shipped `adr_add` tool,
> **without losing decision-level dedup or the read-first behavior.**
>
> **The dedup constraint is load-bearing (task requirement, not a flag).** `adr_add`
> only prevents ID collisions; it does NOT check "is this decision already logged".
> So one of these MUST hold in the migrated prompt:
> - **(A, recommended for car #37 — keep dedup in the prompt):** the lean prompt
>   still instructs *read the existing ADR log first, skip decisions already
>   recorded (by decision, not wording), then call `adr_add` only for genuinely new
>   ones.* `adr_add` handles ID assignment + schema validation + branch-pinned
>   append; the prompt keeps the KEEP/SKIP + decision-dedup judgement it has today.
>   This is the smallest correct change and matches "extraction needs no daemon LLM —
>   the in-session instance does the dedup judgement."
> - **(B, larger — push dedup into the tool):** add a decision-similarity check to
>   `adr_add` (e.g. embed the `decision` field, compare to existing entries, return
>   `{"skipped": "duplicate of ADR-NNNN"}` above a threshold). Heavier; touches
>   `adr.py` + tests + a new threshold knob. Defer unless the prompt-side dedup
>   proves unreliable.
>
> **TDD outline (failing first):**
> 1. *Template test* (`test_stop_memory_checkpoint_module.py`): assert the rendered
>    prompt (a) calls `adr_add(...)` for ADR capture, (b) STILL contains a
>    read-existing-first + dedup-by-decision instruction (guards against the
>    regression), (c) no longer hand-rolls `wiki_append_section` for ADRs. Red today.
> 2. *Tool test* (already green, `test_adr.py`): `adr_add` round-trip — confirms the
>    tool the prompt now calls behaves (read-first, next-id, branch-pinned append).
>
> **Effort: S.** ~30-line template rewrite (net reduction) + 1 template test edit;
> `adr_add` already exists and is tested. **Risk: LOW** if approach (A); the only way
> this goes wrong like C1/C2 is shipping the *lean* version (B-style "just call
> adr_add") and silently dropping decision-dedup → duplicate ADRs accumulate. The
> template test in step 1.b is the guard. **User-decision flag:** approach A vs B
> (recommend A; revisit B only if prompt-side dedup is observed failing).

theme: memory / decisions-log / stop-hook
priority: high (decisions + rationale are the fastest-rotting, highest-value artifact; lost when context scrolls)

## Problem
The "why" behind decisions (rationale, rejected alternatives, revisit triggers) lives only in chat → evaporates. Code shows *what*, git shows *when*, but *why* is lost. The stop hook's old steps 4/5 ("Otherwise capture key decisions") **empirically under-captured** — e.g. the COMET keep/retire decision (PD-50) was only logged when the user explicitly asked. Goal: reliably capture **ADRs (Architecture Decision Records)** into Yadgar, where **the Yadgar wiki is the source of truth** (one ADR page per project; NO file required — works for non-git projects like aws-work; file export only on request).

Key insight: extraction needs **no v6 daemon LLM** — the stop hook already invokes the in-session instance LLM every 25 turns, which has the transcript + judgment (proven: PD-50 captured in-session).

## Mandatory ADR schema (canonical)
Every entry MUST contain ALL fields (write "none"/"n/a" if empty, never omit — stays machine-parseable):
`id` (ADR-NNNN, 4-digit zero-padded, project-sequential) | `title` | `status` (open|accepted|superseded|rejected|deprecated) | `date` (ISO) | `context` | `decision` | `rationale` | `alternatives` (considered + why rejected) | `consequences` (trade-offs/costs/caveats) | `revisit_trigger` | `supersedes` (ADR-NNNN|none). Superset of Nygard/MADR + yadgar extensions (rationale/alternatives/revisit_trigger/supersedes). Mandatory `status` + `revisit_trigger` drive the OPEN/provisional lifecycle + future contradiction-detection.

## P0 — stop-hook prompt redesign (THIS PR)
`yadgar/hooks/stop-memory-checkpoint.py` `_PROMPT_TEMPLATE` rewritten:
- **Capture-first** (steps 1 ADR + 2 structural, then 3 project_brief + 4 maintenance). Rationale baked in: decisions are irreplaceable, maintenance re-fires → under triage pressure, drop maintenance, never capture. This is the real fix for the triage-skip failure.
- **ADR step**: per-project wiki page `<project>-adr-log` (tag `adr`); `wiki_add` to create (NOT `wiki_add+wiki_approve` — `wiki_add` commits directly, `wiki_approve` errors on a live page); precision-biased KEEP/SKIP with two load-bearing discriminators (commitment-vs-status; approach-fix-vs-routine-fix); read-existing-then-append dedup; **mandatory 11-field / ADR-NNNN schema**; unresolved → `status: open`.
- **Maintenance** reframed around `suggested_call` (server supplies the call shape; instance supplies only content — no invention) + anchor actions consolidated into one `audit_anchors` flow + skip-unknown-and-flag guard.
- Dead `refresh_stale_wiki` path **removed** (see Bug A); false "Checkpoint saved" footer fixed to "cadence reached".
- `{project}` derived in `main()` via `os.path.basename`.
- Test `test_stop_memory_checkpoint_module.py` green (17).
- **Deploy:** installed copy `~/.claude/hooks/yadgar-stop-memory-checkpoint.py` refreshes on next session start / `install_hooks`.

## P0.5 — #19: stop-hook ADR prompt READ-side branch_hint (write-side shipped)
**Problem (verified 2026-06-25).** P0 shipped the WRITE-side: ADR `wiki_add` /
`wiki_append_section` calls pin `branch_hint="{default_branch}"` so decisions land
on the project-canonical default branch. But the prompt's two READ calls —
`wiki_read("{project}-adr-log", directory="{directory}")` at
`stop-memory-checkpoint.py:35` and `wiki_history(...)` at line 67 — **omit
`branch_hint`**. Wiki reads ARE branch-filtered (§25). **`wiki_read` (`yadgar/server/tools/wiki.py:686`)
and `wiki_history` (`wiki.py:1013`) both take `branch_hint` and resolve in order:
(1) `directory + branch=$effective_branch`, (2) `directory + branch IS NULL`
(project-canonical), (3) `global`.** The P0 write pins the ADR page to
`branch_hint="{default_branch}"` (e.g. `master`), so the page is stored under
**`branch=master`, NOT NULL**. On a **feature branch**, a `wiki_read` WITHOUT
`branch_hint` detects the feature branch → step (1) looks for `branch=feature` (miss)
→ step (2) `branch IS NULL` (miss — the page is under `master`, not NULL) → step (3)
global (miss). So the dedup-read **can fail to find the canonical ADR page** and the
instance re-creates it / appends a duplicate ADR-NNNN. Write-default / read-current
asymmetry. (Reads are deliberately "more permissive" than writes per §25, but that
permissiveness covers the NULL-canonical case, not a default-branch-pinned page.)

**Fix.** Add `branch_hint="{default_branch}"` to the `wiki_read` (line 35) and
`wiki_history` (line 67) calls in `_PROMPT_TEMPLATE`, so the read targets the same
branch the write pins to. One-template edit; `{default_branch}` is already computed
and substituted in `main()`.

**TDD outline (failing first).** Two layers — the string test alone is insufficient
(it passes regardless of whether the tools honor the kwarg):
1. *Template test* (`test_stop_memory_checkpoint_module.py`): assert the rendered
   prompt's ADR `wiki_read`/`wiki_history` lines contain `branch_hint="{default_branch}"`
   (red today → green after the edit). Cheap guard against regression.
2. *Mechanism test* (the load-bearing one, live/seeded wiki — mirror existing wiki
   branch-scope tests): `wiki_add` an ADR page with `branch_hint="master"` from a
   non-master cwd; assert `wiki_read(slug, directory=…)` WITHOUT `branch_hint` from a
   feature-branch context **misses** it (codifies the bug), and `wiki_read(...,
   branch_hint="master")` **finds** it (codifies the fix). This proves the §25
   resolution behaves as the fix assumes — the string test cannot.

**Contracts.** Touches the §25 branch-filter contract on the wiki read path (no new
BC; the fix makes ADR read+write branch-consistent). No I25 config change.

**Adjacent (separate, do NOT fold here):** `restore()` / `project_brief(mode=restore|signals)`
do not surface the *stored* checkpoint `branch_hint` back to a resuming session
(`project.py:1654/1666` restore+signals payloads omit branch; catalog/full include it
at 1679). That is a resume-ergonomics gap, not the ADR-dedup bug. Track separately if
the user wants it — out of #19 scope.

## P1 — `adr_add` MCP tool + `adr_due` nudge (the robust version)
Move ADR capture off the prompt so the user never formulates it:
- **`adr_add` / `decide` MCP tool** — the 11-field schema as **validated typed params** → server validates + appends to `<project>-adr-log`. This is **storage-level schema enforcement** (not markdown discipline) and makes ADRs machine-structured for v6 reasoning.
- **`adr_due` nudge** surfaced in `project_brief(signals)` / `memorize` / `checkpoint` responses → instance reminded without a prompt. Note: `checkpoint` already takes `key_decisions=[...]` (instance already extracts decisions) — route those to the ADR log / flag for `adr_add`.
- Stop hook then goes **lean**: "if `adr_due`, call `adr_add` for this session's decisions."

## P2 — project_brief surfaces the ADR page first-class
The ADR page must be **promoted in `project_brief`** (read-first, like anchors) so a new session loads prior decisions before acting. Page-existing isn't enough; it must be surfaced.

## P3 — schema home (pragmatic, not boil-the-ocean)
Verified: yadgar has **no canonical schema registry**; SurrealDB is SCHEMALESS by design; record shapes scattered (`models.py` + per-module dataclasses + 54 `DEFINE FIELD` migrations + `config_registry` + `FIELD_META` + `CAPABILITY_REGISTRY` + `EDGE_CONTRACT`).
- (1) Consolidate scattered record shapes into `yadgar/models.py` as the de-facto schema home (cheap, high value).
- (2) Typed-record + tool-validation per NEW type (ADR via `adr_add` first).
- (3) A full DB-level schema-registry seeded at setup = its own v6 design doc/bet (pros: write-validation, versioned schemas, typed data for v6 reasoning; cons: fights SurrealDB flexibility, big migration). Natural generalization of the existing EDGE_CONTRACT/CAPABILITY_REGISTRY declare-the-contract pattern.

## Bug fixes (separate tasks) — see group C of improvement-train.md
- **Bug A (task #9):** `stale_wiki_count` dead. **Current code (verified 2026-06-25):**
  `project.py:1862` reads `fm.get("source_files") or fm.get("sources") or []`, and the
  stale-scan repeats the same check at `~2055-2058` (`_scan_stale_wiki_slugs` /
  `_compute_stale_wiki_count`). The on-disk wiki frontmatter key is **`source_file`
  (singular)** — neither `source_files` nor `sources` matches → always `[]` →
  `stale_wiki_count` always 0 → no staleness signal ever fires. Fix: read
  `fm.get("source_file") or fm.get("source_files") or fm.get("sources")` (or remove the
  dead feature). `project_brief` confirmed `stale_wiki_count: 0` live. Code-only edit
  in `project.py` — planned here, NOT made (scope wall: docs/plans only).
- **Bug B (task #10):** "wiki_add + wiki_approve for new pages" convention is wrong
  (`wiki_add` commits directly: `server/tools/wiki.py`; `wiki_approve` looks up a draft
  and errors on a live slug: `wiki.py:841-879`; the v5.39 similarity-gate DRAFT path
  still exists — drafts created only on a similarity collision via `wiki_check_duplicate`,
  so the common path has no draft to approve). **In-repo status (verified 2026-06-25):**
  the in-repo generator `sync_instructions()` (`server/tools/misc.py:517`) is **already
  correct** — it emits `wiki_add(title, content, append=False)` with no `wiki_approve`.
  The stale text survives ONLY in (a) Max's nix dotfiles (`~/git/nix`, e.g.
  `dotfiles/common/claude.md` — OUT OF THIS REPO, **unverifiable here, hand to user**)
  and (b) one archived plan (`archive/PLAN_V5_45_0_SETUP_FOUNDATION.md:158`, history —
  do not edit). So the real Bug-B fix is the out-of-repo nix file; the in-repo memory
  rules need no change. The "similarity-gate draft-path check still pending" item in the
  global memory rules can be closed: draft path exists + is similarity-collision-only.

## v6 — contradiction-detection
Once ADRs are structured (P1) + read-first (P2), the in-session instance can check "does this new decision contradict ADR-NNNN?" at log time; the v6 daemon LLM does periodic full-corpus contradiction sweeps. Decisions get heat/decay/supersede lifecycle.

## Migration
yadgar's own `docs/DECISIONS.md` (PD-46..PD-50) → migrate to `yadgar-adr-log` wiki page under the new schema; file becomes an on-demand export.

## Risks
- Triage-skip persists even capture-first if the prompt bloats — keep it tight; P1 tool is the durable fix.
- ADR page balloons at scale (100s) → eventual sectioning/archival (`<project>-adr-archive`). Defer.
- ADR-NNNN numbering race across parallel sessions — rare for single-user; accept.

## Related
- `docs/DECISIONS.md` (PD-50 = COMET, the worked example), wiki `comet-enrichment-keep-or-retire-analysis-en2a-2026-06-24`, wiki `…deferred-architecture-ideas` Idea 7, [[yadgar-roadmap-future-improvements]], `PLAN_V6_QUALITY_FOUNDATION`.
