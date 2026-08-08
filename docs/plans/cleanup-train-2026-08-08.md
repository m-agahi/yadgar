# Cleanup train — 5 small cars against v5.181.0 (2026-08-08)

Followup train against the freshly-merged PR #36 (branch-scoping removal,
`cdd7e0a4`). After the 2026-08-08 wiki + live-DB pass, six open task-list
items are real, small, and independent of the spine rewrite (0047) and the
install/diagnostic cluster. All six are kept small enough that each car is
one worktree + one or two files of production code; the bulk of the work
is regression tests.

## Status

Open. Owner: m-agahi. Branch: `feat/cleanup-train-2026-08-08` (to be cut
at Car 1; Car 0 is wiki-only and works on master).

## Pre-train sweeps (already done on master 2026-08-08)

- **0149 (bulk anchor_renew)** — RESOLVED WITHOUT a bulk path. 98 rows
  renewed individually via the per-row `anchor_renew` tool, staggered
  150-240 days by directory group so the next expiry does not rebuild a
  synchronised cliff. `memory:490141` and `memory:491179` restored to
  full anchor status, both renewed. `518764/518775/518850` retired via
  `de_anchor` as in-flight/TODO-class rows — meant to lapse at
  2026-08-26. Live-verified `is_protected` and `migration_grace` state.
  Recorded as `status: completed` in the wiki task list.
- **0153 (BC-G11 dangling pointer)** — RESOLVED in PR #36. Car 10
  re-pointed BC-G11 at
  `tests/e2e/test_phase1_db_layer.py::TestBCB2_WikiDirectoryFilter::test_aws_wiki_excluded_from_yadgar_recall`.
  BC-I32 (the second dangling pointer found in the same pass) also fixed.
  `check_contract_coverage.py` exits 0.

## Cars (proposed)

### Car 0 — task list sweep + close 0144 (wiki only, on master)

The wiki task list was updated by the 2026-08-08 pass to mark 0149 and
0153 completed, but the meta block still says "branch-scoping removal
train IN FLIGHT" (stale) and 0144 itself is still `status: in_progress`
even though the train merged at `cdd7e0a4`.

**Scope:** two `wiki_replace_text` operations on
`yadgar-task-list`:
1. Replace the "BRANCH-SCOPING REMOVAL TRAIN IN FLIGHT" bullet with a
   "MERGED" bullet pointing at the new plan page.
2. Flip 0144 from `in_progress` to `completed` (or delete the row per the
   2026-08-02 policy; the body is long enough to keep as a completed
   record).

**Acceptance:** `recall("branch-scoping removal status", type="wiki")`
does not surface "IN FLIGHT" in its top 5; the meta line is
"MERGED". `wiki_lint` clean.

### Car 1 — anchor_audit_prompt.md gathers wrong rows (task 0148)

`yadgar/core/hooks/templates/anchor_audit_prompt.md:18-21` calls
`recall(tags=["_anchor"], type="memory")` expecting it to return anchor
memories. The `tags=` filter on `recall()` is applied to the WIKI
provider only (`yadgar/backend/retrieval/recall_pipeline.py:436`), not
the memory provider (`MemoryProvider` is constructed at line 426 with no
`tags=` arg). Under `type="memory"` the wiki provider is not even
constructed, so the filter is structurally unreachable. Every
anchor-audit pass has run an unfiltered semantic search over arbitrary
rows.

Companion guard test
`yadgar/tests/hooks/test_v5_158_anchor_audit_scheduler.py:151-165` is
substring-only and passed for the bug's entire lifetime — vacuous-pass
family.

**Fix (two halves):**

1. `MemoryProvider.__init__` accepts `tags: list[str] | None = None`;
   the provider's `candidates()` query passes it through to the storage
   layer's tag filter (already exists in
   `memory.py::get_memories_by_tag` / `get_memories_by_tags`). The
   pipeline passes `tags=` to the memory provider the same way it does
   to the wiki provider today.
2. The byte-pin test at `test_stop_hook_template.py:42,289` is the
   house pattern for guards; port the anchor-audit test to that shape:
   read the template, locate the `recall(` call, assert the first
   argument is a memory-only filter (e.g. `type="memory"`) AND that the
   gathering is post-grepped to anchor rows. The byte-pin kills the
   substring loophole.

**Acceptance:**

- `recall(tags=["_anchor"], type="memory", directory=<this>)` returns
  only anchor-tagged memories (verified by 5-row fixture).
- The existing scheduler test byte-pins the template's recall call.
- Mutation: temporarily revert the pipeline's `tags=` pass-through →
  test goes RED on the fixture, not just on a message string.

**Touched files:**
`yadgar/backend/retrieval/providers/memory.py`,
`yadgar/backend/retrieval/recall_pipeline.py`,
`yadgar/tests/hooks/test_v5_158_anchor_audit_scheduler.py`,
plus a new fixture under `yadgar/tests/fixtures/anchor_audit/`.

**Risk:** low. The existing `tags=` path is dead code today; widening
it to the memory provider only adds readers for a path that was
previously unused. Surface area: one constructor signature, one query
parameter.

### Car 2 — `tags=` byte-pin + the relationship-dangling repair (tasks 0155)

`check_invariants` auto-fixes three sibling classes (1 dangling
`wiki_crossref`, 1 phantom `memory:<N>` entity row, 29 over-occupied
engram-slot rebalances) but the `relationship_dangling_other` class is
violation-only with no repair path. The 2026-08-08 vacuum surfaced a
`relationship_dangling_other: 1` violation; the vacuum correctly KEPT
its swap (per-table counts verified identically pre/post).

**Fix:** in
`yadgar/backend/admin_exec/invariants.py::_repair_dangling_caused_by`
(around :215-254), add a sibling
`_repair_dangling_other` function. For each non-`caused_by` relationship
row where `source_entity_id` or `target_entity_id` is missing:

- if `source_entity_id` is missing → delete (a relationship with no
  source is unreachable; `caused_by` got the same treatment);
- if `source_entity_id` exists and `target_entity_id` is missing →
  this is a real-world case ("deleted-target ghost") and the right
  action is the same as caused_by: delete and log. If there is
  signal that the target was a `wiki_page` / `memory` / etc., record
  the type in the `fixed` message so dashboards see the shape.

The audit log entry should make the violation auto-repairable on the
next `check_invariants` call. Mirror the existing `caused_by` shape
exactly: count key, fix list, log line, idempotent re-run returns zero.

**Acceptance:**

- A fixture with 1 non-`caused_by` dangling row + 1 caused-by dangling
  row → both repaired; counts return
  `{caused_by_dangling: 0, relationship_dangling_other: 0,
  fixed: ["...", "..."]}`; second run returns
  `{caused_by_dangling: 0, relationship_dangling_other: 0, fixed: []}`
  (idempotent).
- Mutation: revert the delete → `relationship_dangling_other` count
  stays 1, fixed list stays empty, test goes RED on the count, not on
  a string.

**Touched files:** `yadgar/backend/admin_exec/invariants.py`,
`yadgar/tests/backend/test_invariants_repair.py` (extend).

**Risk:** low. Purely additive; the existing caused_by path is
unchanged.

### Car 3 — TOC dead patterns (task 0156)

`agent-prompt-toc` lists 9 pattern slugs whose pages do not exist:
`dispatch-flux-overlay-patch-pr`,
`dispatch-build-flux-convergence-pr`,
`dispatch-readonly-infra-audit`,
`dispatch-review-terraform-plan`,
`locate-config-monorepo`,
`build-and-open-pr`,
`dispatch-flux-adoption-audit`,
`dispatch-flux-post-merge-verify`,
`install-opencode-yadgar-plugins`. A guessed pattern slug returns
an EMPTY fallback contract (per `agent_dispatch_prelude`) — but a dead
TOC entry lands on the same empty fallback anyway. The TOC is the
designated cure; a stale TOC is the worst possible state of the cure.

**Fix (two halves):**

1. Remove the 9 dead entries from the TOC. The patterns themselves can
   stay in `agent-prompt-save` history; the TOC is a curated table of
   contents, not a registry of all ever-written prompts.
2. Add `scripts/check_agent_prompt_toc.py` (pre-commit + CI
   `invariant-checks`) that reads the TOC, extracts every backtick-
   wrapped slug of the form `agent-prompt-<pattern>`, asserts
   `wiki_read(slug)` returns content for each, fails on any miss.
   This is the same shape as
   `scripts/check_contract_coverage.py`'s dangling-test-reference
   rule.

**Acceptance:**

- `python scripts/check_agent_prompt_toc.py` exits 0.
- 9 dead entries are gone from the TOC; the kept entries still resolve.
- New entry in `.pre-commit-config.yaml` registers the script under
  `always_run: true` (matches `check-changelog-unreleased-versions`).

**Touched files:** `agent-prompt-toc` (wiki page),
`scripts/check_agent_prompt_toc.py` (new),
`.pre-commit-config.yaml` (one new hook entry).

**Risk:** low. Removing 9 lines from a curated doc + adding a
pre-commit guard. No production code change.

### Car 4 — `docs/CHANGELOG.md` stale second `## [Unreleased]` (task 0151)

`docs/CHANGELOG.md` contains two `## [Unreleased]` headers (lines 8
and 1329). A 2026-08-02 car promoted 55 releases out of `[Unreleased]`
but added a second header for the in-flight work, violating the Keep a
Changelog shape.

**Fix:** merge the second `[Unreleased]` body into the first. No
semantic content is lost. Cosmetic; the bug is "parsing breaks for any
tool that does a heading-anchored parse of the changelog".

**Acceptance:**

- `rg -c "^## \[Unreleased\]" docs/CHANGELOG.md` returns `1`.
- `python scripts/check_changelog_unreleased_versions.py` exits 0.

**Touched files:** `docs/CHANGELOG.md` only.

**Risk:** none. Trivial text merge.

### Car 5 — `_project_init` staleness threshold (task 0154)

`project.py::_build_recommended_actions` emits `bootstrap_project` on
PRESENCE only (`:1121`). The age is computed
(`init_memory_age_hours: float | None`, lines 1636/1718) but never
compared to a threshold — the other actions in the same block
(`active_work`, `checkpoint`) ARE warn- and stale-thresholded
(`:1131`, `:1139`, `:1150`, `:1158`). 81 days of silent drift on the
corpus.

**Fix:** mirror the active_work/checkpoint pattern. Add a configurable
threshold (default 30 days, name: `INIT_MEMORY_STALE_HOURS` in
`config.py`'s FIELD_META) and emit `bootstrap_project` with
`reason: "init_memory stale (N days)"` when the age exceeds it. The
suggestion text in `:135` already names `bootstrap_project`; the action
is correct, only the trigger condition needs the threshold.

**Acceptance:**

- A fixture with `init_memory_age_hours=900` and the new default 720h
  threshold emits the `bootstrap_project` action with the stale
  reason; the existing `init_memory absent` reason is unchanged.
- A fixture with `init_memory_age_hours=24` does NOT emit the action.
- A test pins both; mutation: revert the comparison → test goes RED
  on the fixture, not on a string match.

**Touched files:** `yadgar/core/server/tools/project.py`,
`yadgar/_shared/config/config.py` (one FIELD_META row), one new test.

**Risk:** low. The age field already exists; only the threshold +
action reason are new.

## Sequencing

Linear, no dependencies between cars beyond the merge order. Car 0
lands first (wiki-only, on master), Cars 1-5 each get their own
worktree off `feat/cleanup-train-2026-08-08`, the integration step
opens ONE PR at the end (per `agent-prompt-integrate-train-and-pr`).

## Out of scope (intentionally)

- 0035 (DB config migration) — blocked on 0095/0098
- 0047 (spine refactor) — own train
- 0043 (anchor cull surface) — depends on the 2026-08-08 anchor pass
  that already landed
- 0040, 0041, 0138 (viz/CLI) — never scoped
- 0120, 0122, 0124-0131, 0133, 0135, 0137, 0140, 0143, 0145-0147 —
  install/diagnostic cluster
- 0001-0025, 0028, 0057-0060, 0077, 0080, 0114-0118, 0093 — long-tail

## Acceptance gates

Per `agent-prompt-build-car`, each car's final report includes:

- commit hashes
- files/functions changed
- targeted red→green test evidence
- ONE final full-suite result with REAL exit code
- mutation test results for any guard
- backend-bump-needed flag (none of these touch `yadgar/backend/**`
  storage; Car 2 only adds a function in the existing
  `admin_exec/invariants.py`, no schema change → no bump)
- `## Yadgar findings` section
