# PLAN — ADR capture system (Yadgar wiki as source of truth)

Status: **PHASED — P0 shipping in this PR (stop-hook prompt); P1–P3 planned.** Designed 2026-06-24 (human + instance, item-by-item review + opus design agent + advisor).

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

## Bug fixes (separate tasks)
- **Bug A (task #9):** `stale_wiki_count` dead — `project.py:1865-66` checks `source_files` (plural) but disk has `source_file` (singular) → always 0 → `refresh_stale_wiki` never fires. Fix gate or remove feature.
- **Bug B (task #10):** CLAUDE.md says "wiki_add + wiki_approve for new pages" but `wiki_add` commits directly + `wiki_approve` errors on live pages. Verify the v5.39 similarity-gate draft path, then fix the memory-rules text in BOTH places CLAUDE.md is produced: normal-user (setup script / repo nix flake) AND Max's `yadgar.nix`.

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
