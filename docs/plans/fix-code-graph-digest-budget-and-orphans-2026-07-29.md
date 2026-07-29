# code_graph follow-ups: the stale marker is truncated away, and the index cache has orphans — 2026-07-29

**Status:** PARTIALLY SHIPPED (v5.169.0, m-agahi/yadgar#15) — NOT archived, per ADR-0081.
- **Car 1 (digest budget reserve): SHIPPED.** The `stale @ <sha>` marker is now a budget-reserved
  preamble on line 2 and survives truncation — verified absent-before / present-after on a
  budget-filling render, and again at `budget=200`, with `len(out) <= budget` intact.
- **Car 2 (orphan index cache): NOT BUILT.** Deliberately doc-only; no prune subcommand was written.
  The residual — 746 MB across 37 files in `~/.cache/yadgar/code_graph`, plus the wrong path recorded
  at `CAPABILITY_REGISTRY.md:1935` (it names `~/.cache/codebase-memory-mcp`, which has never existed) —
  is tracked as **task:0087**, together with the section-starvation finding below.
- **Known limit, restated so a green Car 1 is not misread:** this fixes the MARKER, not digest
  completeness. `endpoints:` is still entirely absent on large-repo digests because `layers:` +
  `hotspots:` alone exceed `DIGEST_CHAR_BUDGET = 2000`.
**Verdict: TWO cars.** They share the `code_graph` subsystem and nothing else — disjoint files,
disjoint mechanisms, disjoint acceptance shapes. Car 1 is a renderer correctness bug with unit ACs;
Car 2 is a paragraph in `MIGRATION_NOTES.md` plus a tracked follow-up. Fusing them would produce one
incoherent car whose "done" means two unrelated things.

**Investigated:** 2026-07-29 against branch `feat/v5.169-install-runtime-fixes`, HEAD `4d21ce80`
(the merge of Car C — `stale @ <sha>` wiring + deterministic project key). Every claim below is
re-verified against the current tree, not against master and not against the task description.

**Builds on:** [[yadgar-adr-0162]] (code_graph via codebase-memory-mcp),
`docs/plans/fix-code-graph-stale-line-wiring-2026-07-29.md` (the just-merged car), BC-CODEGRAPH-3/7.

---

# Car 1 — the `stale @ <sha>` marker is truncated away on any real repo

## Context — what is actually broken

`render_digest` concatenates six sections in a fixed priority order and then tail-cuts the joined
string to the budget:

```python
# yadgar/core/code_graph/digest.py:456-475
sections: list[list[str]] = [
    [_header_line(architecture, identity)],
    _layers_section(architecture),
    _hotspots_section(architecture),
    _entry_points_section(architecture),
    _endpoints_section(endpoints),
    _stale_line(identity),          # ← LAST
]
...
text = _defang_secret_shaped_runs("\n".join(line for line in lines if line))
return _truncate(text, limit)       # ← naive tail cut, digest.py:417-426
```

`_truncate` (`digest.py:417-426`) is a plain `text[:keep] + "…"`. There is no per-section
reservation. So **whichever section is last dies first**, and `_stale_line` (`digest.py:399-413`) is
last.

`DIGEST_CHAR_BUDGET = 2000` (`config.py:118`).

### Measured — it is not hypothetical

Rendered with a realistic single Java service shape (20 layers → 12 rendered by `_MAX_LAYERS`,
20 hotspots → 10 by `_MAX_HOTSPOTS`, 40 endpoints → 20 by `_MAX_ENDPOINTS`; package-qualified
Java names, `stale=True`, 40-hex `head_sha`):

| section | chars | cumulative |
|---|---:|---:|
| header | 37 | 38 |
| layers | 1133 | 1172 |
| hotspots | 959 | **2132 — budget already blown** |
| entry-points | 0 | 2133 |
| endpoints | 1467 | 3601 |
| **stale** | **20** | **3622** |

Untruncated render = **3780 chars**; emitted = **2000**. Sections present in the emitted digest:
`layers:`, `hotspots:`. **`endpoints:` and `stale @` are both entirely absent.**

**Overflow threshold:** the budget covers the header (~37) plus 2 section-label lines plus the
22 capped rows → **~88 chars per row on average**. A Java/Go/Python repo whose layer names are
package-qualified (`com.acme.svc.Component`, `internal/http/handler`) and whose hotspots are
`pkg.Class.method` clears 88 chars per row trivially. The failure is not an edge case; it is the
default on any non-toy repo. This repo's own digest is small enough to survive, which is precisely
why nobody noticed.

### Why it stayed dead — the same disease as the merged car, one layer up

The merged car fixed a marker that **no producer populated**. This one is a marker that **renders
correctly and is then thrown away**. Both are "present in code, passes its tests, never reaches the
consumer."

Two pieces of evidence that this was known and worked around rather than fixed:

- `yadgar/tests/core/test_code_graph_digest.py:210-225` (`TestStale`) renders the marker against the
  small `_java_arch()` fixture — the isolated-render pattern.
- `yadgar/tests/core/test_code_graph_cli.py:29-32`, verbatim:

  > *"(b) small enough that the LAST-priority stale line is never truncated away by
  > `DIGEST_CHAR_BUDGET`."*

  The fixture was **deliberately sized to dodge the bug**. The constraint is documented; the bug is
  not filed. That comment must be deleted as part of the fix — leaving a rationale comment for a
  thing that no longer exists is the same disease.

Nothing anywhere renders a budget-filling digest and asserts the marker survives.

## Decision — reserve inside the budget; do NOT exempt

**Chosen: a reserved preamble.** `header` + `stale` are rendered as a preamble; the remaining
sections are truncated to `budget − len(preamble) − 1`. The marker moves from last to **second**,
and its survival becomes **structural** rather than positional.

```
── code_graph: globalrouter (Java) ──
stale @ a1b2c3d4e5f6
layers:
  …
```

**Degenerate case — specify it, do not let the builder invent it.** When `budget <= len(preamble)`
(pathological: a budget smaller than the header, or an absurdly long `repo_id`/language list), fall
back to truncating the whole assembled text exactly as today. Do **not** compute a negative body
budget and slice with it. `_truncate`'s existing `max(0, ...)` clamp (`digest.py:425`) already
handles the whole-text path; the new code just must not construct a negative before reaching it.

**Rejected: budget-exempt** (render the marker after truncation, letting output exceed `budget`).

1. **One invariant beats two.** `len(content) <= budget` is a single, testable, downstream-relied-on
   property (`payload["chars"]`, the memory-block `char_limit`). Exempting mints a second, weaker
   contract — "≤ budget, except for the bits that aren't" — for a 20-char saving. Reserving costs
   nothing and keeps the contract intact.
2. **Top position is semantically correct independently of truncation.** The marker is a *qualifier
   on the whole digest*: "everything below describes commit X and may be out of date." A reader (and
   an LLM reading an always-injected block) parses top-down. A marker at the bottom is the wrong
   place even in the case where it survives. So reordering is the right change on its own merits,
   and the budget fix falls out of it for free.
3. Corroborating, not load-bearing: BC-CODEGRAPH-3 states the digest SHALL be "bounded
   (≤ `DIGEST_CHAR_BUDGET`)". That contract is amendable (this car already amends BC-CODEGRAPH-7),
   so it is not the argument — but it does mean exempting is a contract change where reserving is not.

### Is the existing priority order principled?

**No — it is accidental.** `header > layers > hotspots > entry-points > endpoints > stale` is
declared in the module docstring (`digest.py:11-12`) and in `render_digest`'s docstring
(`digest.py:444-446`) and was never validated against a budget-filling input. Two tells:

- **`entry-points` outranks `endpoints`.** Entry-points is *derived from* `layers`
  (`_entry_points_section`, `digest.py:302-316`) — those names are already on screen in the `layers:`
  section. It is the most redundant section in the digest and it sits above the least redundant one.
- **A metadata qualifier sits below bulk content.** Nothing that qualifies how to read the whole
  block should rank below the block's rows.

**This car changes the order in exactly one way** (stale → preamble). It does **not** re-rank
layers/hotspots/entry-points/endpoints — see Open decision (b), which carries the measured numbers.

## File seam

| File | Change |
|---|---|
| `yadgar/core/code_graph/digest.py` | `render_digest` — split preamble (`_header_line` + `_stale_line`) from body; truncate the body against the remaining budget; join. Update the module docstring (`:11-12`) and `render_digest`'s docstring (`:444-446`) to state the new order **and** the reservation. |
| `yadgar/tests/core/test_code_graph_digest.py` | New `TestStaleSurvivesBudget` (AC-1/AC-2). Existing tests unchanged — see blast radius. |
| `yadgar/tests/core/test_code_graph_cli.py` | **Delete the now-false constraint (b)** at `:29-32`. The fixture may stay as-is; only the obsolete rationale goes. |
| `docs/contracts/BEHAVIOR_CONTRACT.md` | BC-CODEGRAPH-7 says "a **trailing** `stale @ <12-char sha>` marker" (`:375`) — amend "trailing". Add BC-CODEGRAPH-8 (below). Append-only; do not edit BC-CODEGRAPH-1..6. |
| `yadgar/core/hooks/templates/code_graph_refresh_prompt.md:34` | Same wording: *"…and a **trailing** `stale @ <sha>` marker"*. Amend. This is the prose an agent reads at hook time — leaving it wrong is the merged car's `AC-8 [manual]` failing one car later. |
| `yadgar/core/cli/code_graph.py:87` | `_cmd_refresh` docstring: *"a **trailing** `stale @ <12-char sha>`"*. Amend. Docstring only — **no code change in this file**. |

**Repo-wide sweep done** (`grep -rn "stale @"` over `*.md` + `*.py` + `*.sh`): those three are the
only "trailing"-position claims, and **nothing parses the digest positionally** — every other match
is a docstring, a prose comment, or a substring-containment test assertion. No consumer breaks.

### Blast radius — measured, not estimated

Every existing digest assertion was read (`test_code_graph_digest.py:75-225, 381-405`). **Zero golden
/ exact-equality assertions exist in the file.** Position-sensitive assertions:

- `:86` `assert out.startswith("── code_graph:")` — **survives**: the header stays first, and this
  fixture is fresh (`_stale_line` returns `[]`, so nothing is inserted at all).
- `:90-93` relative index `layers < hotspots < endpoints` — **survives**: their relative order is
  unchanged.

Everything else is substring containment. **Expected diff to existing tests: zero lines**, beyond the
obsolete comment in `test_code_graph_cli.py`. If the builder finds themselves editing an existing
assertion, that is a signal the refactor did more than intended — stop and re-read.

### Two regressions the builder must not introduce

1. **`_defang_secret_shaped_runs` must still cover BOTH halves.** Today it runs once over the whole
   joined text *before* `_truncate` (`digest.py:474`). Splitting preamble/body and defanging only the
   body silently narrows the #30 secret-gate FP guard. Cleanest: build the full text, defang it, then
   split — or defang each half. `TestSecretGateFalsePositive` (`:455-487`) must pass unmodified.
2. **Keep `_stale_line`'s 12-char sha cut** (`digest.py:411`). A builder "simplifying" the preamble to
   emit the raw `head_sha` reintroduces the exactly-40-hex AWS-secret shape that
   `test_git_sha_stale_line_passes_gate` (`:466-474`) exists to prevent.

## Acceptance criteria

**AC-1 [unit] — THE criterion this car exists for: the marker survives a budget-filling digest.**
Render at the real `config.DIGEST_CHAR_BUDGET` with an architecture whose untruncated render exceeds
it (the realistic-Java shape above; untruncated ≈ 3780). Assert **all three**:
- **`"…" in out`** — the load-bearing assertion. `_truncate` returns the text unchanged when it fits
  (`digest.py:423-424`), so the ellipsis appears **if and only if** truncation actually happened.
  Without it the test passes on a digest that never truncated — exactly the false-green the original
  `TestStale` gave. **Relaxing or dropping this assertion is what makes the test meaningless; that is
  the line the builder must not cross.**
- `"stale @ " in out`.
- The marker is in the first two lines (`out.splitlines()[1].startswith("stale @ ")`).

> Assert `len(out) <= budget` (the AC-4 invariant), **not** `== budget`. Exact equality depends on
> the builder's join-separator arithmetic landing perfectly; one off-by-one reds the test, and the
> natural "fix" is to relax it to `<=` — which, if the ellipsis assertion were the thing relaxed
> instead, silently deletes the truncation proof. Keep the two concerns separate: `"…"` proves
> truncation, `<= budget` proves the bound.

**AC-2 [unit] — the guarantee is structural, not "2000 happens to be roomy."**
Same input at an explicit tiny `budget=200`. Truncation is severe; the marker still renders and is
still on line 2. Pins that the reservation is computed, not that the constant is generous.

**AC-3 [unit] — no marker on a fresh digest, and no blank line where it would go.**
`stale` absent/False → `"stale @" not in out` **and** `out.splitlines()[1].startswith("layers:")`.
Guards against the preamble emitting an empty line when `_stale_line` returns `[]`.

**AC-4 [unit] — budget invariant intact (BC-CODEGRAPH-3).**
`len(render_digest(...)) <= budget` across: fresh/stale × under-budget/over-budget × tiny budget.
`build_block_payload(...)["chars"] == len(content) <= budget` in the stale-and-truncated case.

**AC-5 [unit] — secret-gate guard not narrowed.**
`TestSecretGateFalsePositive` and `TestSecretGateAdversarialStillRejected`
(`test_code_graph_digest.py:455-526`) pass **unmodified**. Plus one new case: a keyword-armed
40-char run in the **body** of a digest that also carries a stale preamble is still defanged.

**AC-6 [unit] — zero regression in the existing suite.**
`test_code_graph_digest.py` and `test_code_graph_cli.py` pass with no assertion edited. The only
permitted test-file change besides new tests is deleting the obsolete constraint comment at
`test_code_graph_cli.py:29-32`.

**AC-7 [manual] — docs match behaviour.**
`digest.py`'s module docstring (`:11-12`) and `render_digest`'s (`:444-446`) state the new order and
the reservation. BC-CODEGRAPH-7's "trailing" wording is corrected. BC-CODEGRAPH-8 added:

> BC-CODEGRAPH-8 — the `stale @ <sha>` freshness marker SHALL be rendered in a budget-reserved
> preamble immediately after the header, so it survives truncation on any digest that fills
> `DIGEST_CHAR_BUDGET`; the total digest SHALL remain ≤ `DIGEST_CHAR_BUDGET` (BC-CODEGRAPH-3
> unaffected). ⏳ [u] `tests/core/test_code_graph_digest.py::TestStaleSurvivesBudget::test_marker_survives_budget_filling_digest` P2

## What Car 1 does NOT fix — state this so a green AC is not misread

**Car 1 makes the marker survive. It does not make the digest complete.** On the measured realistic
Java shape, `endpoints:` (1467 chars) is still entirely absent after the fix, because
`layers:` + `hotspots:` alone exceed the budget and the tail cut starves everything after them. A
green AC-1 means "the freshness signal reaches the reader," **not** "the digest is healthy on large
repos." The endpoints starvation is Open decision (b).

## Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Preamble split narrows `_defang_secret_shaped_runs` coverage (#30 regression). | AC-5; defang before splitting, or defang both halves. Named explicitly in the file seam. |
| R2 | Builder emits a raw 40-hex sha in the preamble, reintroducing the AWS-40 shape. | Keep `_stale_line` as the single marker producer (12-char cut at `digest.py:411`). AC-5. |
| R3 | An empty preamble line when `_stale_line` returns `[]` shifts every fresh digest by one blank line. | AC-3 pins line 2 on the fresh path. The existing `line for line in lines if line` filter already drops empties — do not remove it. |
| R4 | BC-CODEGRAPH-7's test ref (`test_refresh_reemits_stale_marked_digest_on_fetch_failure`) asserts `f"stale @ {sha[:12]}" in payload["content"]` — substring, position-agnostic. | Survives unchanged. Verified at `test_code_graph_cli.py:265`. |
| R5 | Doc collision on `BEHAVIOR_CONTRACT.md` — already edited by the merged install car and the merged stale-wiring car. | Append-only after BC-CODEGRAPH-7; rebase on the train before pushing. |

---

# Car 2 — orphan index caches (doc-only; barely a car)

## The premise needs correcting first

**`~/.cache/codebase-memory-mcp` does not exist.** The orphans live in
**`~/.cache/yadgar/code_graph`** — which is **yadgar's own cache dir**:

- `config.cache_dir()` returns `CACHE_DIR / "code_graph"` (`config.py:99-111`).
- `runner._build_env` **creates it** (`cache.mkdir(parents=True, exist_ok=True)`, `config.py:114` via
  `runner.py:113-115`) and points `CBM_CACHE_DIR` at it (`runner.py:115`) — explicitly so "SQLite
  never lands in the user tree" (`runner.py:19-20`).
- The orphan filenames derive from **yadgar's own** `tempfile.mkdtemp(prefix="yadgar-code-graph-")`
  (`default_branch.py:241`).

So the framing "outside yadgar's data dir, belongs to a third-party binary, is owning cleanup even
appropriate?" **does not apply**. Yadgar chose this directory, creates it, named the files, and made
the orphans. Ownership is not in question.

## Measured state (read-only `ls`/`du`; nothing deleted)

**12 orphan `.db` files (+ `-shm`/`-wal` siblings), 746 MB**, dated 24–28 Jul 2026:

```
tmp-yadgar-code-graph-<mkdtemp-suffix>-wt.db                              ← whole-repo index
tmp-yadgar-code-graph-<mkdtemp-suffix>-wt-services-core-eventprocessor.db ← monorepo leaf
```

Nine are large-repo indexes at 122 MB, 122 MB, 122 MB, 122 MB, 122 MB, 9 MB, 9 MB, 9 MB, 12 MB;
three are monorepo leaves (3–12 MB).

**Identification is exact, not heuristic.** Every orphan carries the `tmp-` prefix and the literal
`yadgar-code-graph-` fragment that yadgar's own `mkdtemp` prefix put there. Post-fix project names
are `<safe-leaf>-<12-hex>` (`_project_name`, `default_branch.py:100-123`) — structurally distinct: no
`tmp-` prefix, no `yadgar-code-graph-` fragment.

> ### ⚠️ Load-bearing ASSUMPTION — verify before running the `rm`
>
> Every filename observed on disk derives from the **indexed path**
> (`tmp-yadgar-code-graph-<mkdtemp>-wt[-<subdir>]`), not visibly from `--name`. Whether the
> codebase-memory-mcp binary keys its SQLite **filename** off `--name` or off the path is
> third-party behaviour we cannot see from here, and the fix merged *today* — so no post-fix file
> exists on disk to check. Not verified deliberately: confirming it needs the 259 MB binary and a
> real cache write.
>
> **If the filename follows `--name` (assumed):** the per-refresh leak is closed and the
> `tmp-yadgar-code-graph-*` glob matches only dead files. Plan holds.
>
> **If the filename still follows the temp path:** (a) the per-refresh leak is **not** closed — every
> refresh still mints a new file — and (b) the glob would match **live** indexes, making the `rm`
> unsafe. That flips this car's recommendation and reopens open decision (d).
>
> Resolution is one observation, not an investigation — it is written into the MIGRATION_NOTES entry
> below as a check Max performs before deleting anything.

## Decision — document it. Do not build a prune subcommand.

**Recommendation: a `MIGRATION_NOTES.md` entry that hands Max the one-liner. No code, no CLI
subcommand, no automated prune step.** Plainly: this is a one-off, and it should be documented rather
than featurised.

Why:

- **It is a one-time cost from a bug that is now fixed.** `_project_name` (merged in `1b42567d`)
  makes names deterministic, so the *unbounded-per-refresh* leak — a fresh orphan on every single
  refresh — is closed. What remains on disk is a finite historical set.
- **Blast radius is tiny.** Only installs that ran `code-graph refresh` before 2026-07-28 have any.
  In practice: dogfooding users.
- **A prune subcommand is code + tests + docs + a destructive-by-design surface for a sweep that
  happens once.** That is inventing a feature to avoid writing a paragraph.
- Yadgar has no existing cache-prune CLI family to piggyback on (`yadgar vacuum` is SurrealKV DB
  compaction — a different concern, `yadgar/core/cli/vacuum.py`), so this would be a new surface, not
  one more predicate on an existing one.

**No car executes the `rm`.** Per the standing apply/import rule, the command goes into
`MIGRATION_NOTES.md` for Max to run.

### `MIGRATION_NOTES.md` entry (content to write; the command is for Max, not for a car)

> **code_graph — one-off orphan index cleanup (post `1b42567d`)**
>
> Before the deterministic-project-name fix, every `yadgar code-graph refresh` named its index after
> a random `tempfile.mkdtemp` worktree path, so each refresh minted a new project and left the old
> SQLite behind in `~/.cache/yadgar/code_graph`. The fix stops new orphans; it does not remove the
> old ones. They are identifiable by the `tmp-yadgar-code-graph-` prefix, which the post-fix naming
> scheme never produces.
>
> Inspect first:
> ```bash
> du -sh ~/.cache/yadgar/code_graph
> ls -la ~/.cache/yadgar/code_graph/tmp-yadgar-code-graph-*
> ```
>
> **Safety check — do this before deleting.** Run one `yadgar code-graph refresh <repo>` and look at
> what appears in the cache dir:
> ```bash
> ls -lat ~/.cache/yadgar/code_graph | head
> ```
> - A **new `.db` WITHOUT** the `tmp-yadgar-code-graph-` prefix ⇒ expected; the glob below matches
>   only dead files. Proceed.
> - A new `.db` **WITH** that prefix ⇒ the indexer still names its SQLite after the temp path.
>   **Do NOT run the `rm`** — the glob would delete live indexes, and the per-refresh leak is not
>   actually closed. Report it; the plan's open decision (d) reopens.
>
> Then, if the check passed:
> ```bash
> rm -f ~/.cache/yadgar/code_graph/tmp-yadgar-code-graph-*
> ```
> Nothing is lost that a `yadgar code-graph refresh` will not rebuild. Observed here: 12 files,
> 746 MB.

Also add a one-line pointer in the code_graph section of `docs/CHANGELOG.md` under the
deterministic-naming entry.

### Correct the wrong path already recorded in the capability registry

`docs/contracts/CAPABILITY_REGISTRY.md:1935` (CAP-CODEGRAPH-001, task:0067 addendum) ends:

> *"Pre-existing orphan projects under old temp-derived names remain in `~/.cache/codebase-memory-mcp`
> until cleared manually."*

That path **does not exist** — it is the same wrong premise, already propagated into a contract
document. Amend it to `~/.cache/yadgar/code_graph` and point at the MIGRATION_NOTES entry. This is
the actual reason Car 2 is worth doing at all: a contract file currently tells a future maintainer to
clean a directory that was never there.

## Acceptance criteria

**AC-8 [manual]** — `MIGRATION_NOTES.md` carries the entry above, including the pre-delete safety
check that resolves the `--name`-vs-path filename assumption.
**AC-9 [manual]** — `docs/contracts/CAPABILITY_REGISTRY.md:1935` no longer says
`~/.cache/codebase-memory-mcp`; it says `~/.cache/yadgar/code_graph` and points at MIGRATION_NOTES.
**AC-10 [manual]** — `docs/CHANGELOG.md` points at the entry from the deterministic-naming line.
**AC-11 [manual]** — Max runs the safety check, then the `rm`, at his discretion.
**Not a car deliverable** — no car executes a destructive command (standing apply/import rule).

## What Car 2 does NOT fix

**The cache dir has no eviction policy at all, and the deterministic-naming fix does not give it
one.** Post-fix there is one `.db` per `(canonical_root, subdir)` **forever**. A new permanent orphan
appears whenever `canonical_root` changes — repo moved, repo renamed, second clone at another path,
or a monorepo leaf indexed once during an exploration and never again. Three of the twelve files
found here are exactly that last case: they are permanent orphans **under the new scheme too**. At
~122 MB per large-repo index the dir grows monotonically, just slower.

That is a real, separate, unfixed issue and it is **out of scope for this plan** — it was not created
by the merged car and it is not what Finding 2 asked about. Recommend filing it as a tracked
follow-up: *"`~/.cache/yadgar/code_graph` has no size bound or eviction policy."*

---

## Open decisions for the user (one recommendation each)

**(a) Reserve the marker inside the budget, or exempt it from the budget?**
- **Recommend: reserve** (Car 1's decision section). One invariant instead of two; top position is
  correct on its own merits; no contract amendment needed for BC-CODEGRAPH-3.
- Rejected: exempt. Buys 20 chars, costs the single clean `len(content) <= budget` property that
  `payload["chars"]` and the block `char_limit` both lean on.

**(b) Re-rank the remaining sections / give each a reserved share.**
- **Recommend: NOT in Car 1 — but do file it, with these numbers.** The measured table shows
  `layers` (1133) + `hotspots` (959) exceed the 2000 budget on their own, so `endpoints` (1467) is
  entirely absent from every large-repo digest, and `entry-points` — which is *derived from* `layers`
  and therefore the most redundant section — outranks it in the declared order.
- Kept separate because the severity differs in kind: a missing stale marker **misleads** (the reader
  sees a fresh-looking digest of an aged index); missing endpoints merely **omits** (a lossy summary
  is lossier). Different bug class, different fix (per-section reservation or proportional
  allocation in `_truncate`), different ACs. Two small honest cars.
- Sub-question for that follow-up: raise `DIGEST_CHAR_BUDGET` above 2000? Note the coupling —
  2000 is also `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT`, so raising the budget requires raising the block's
  `char_limit` at create time (hard max 8000). Raising alone does not fix ordering; a big enough repo
  overflows any budget.

**(c) Surface `head_sha` on FRESH digests too, now that there is a preamble to put it in.**
- **Recommend: still defer.** This is open decision (b) of the merged stale-wiring car, deferred
  there for the same reason. Car 1 makes it *cheaper* (the preamble exists), which is an argument for
  the follow-up, not for widening this car. Flagged so the fresh-path asymmetry stays explained.

**(d) Build `yadgar code-graph prune` after all?**
- **Recommend: no** — see Car 2. Revisit only if the "no eviction policy" follow-up is picked up, in
  which case a prune surface is the natural home for *both* the legacy glob and an age/size policy,
  and building it once for both is better than building it twice.

**(e) Wire `runner.list_projects` (`runner.py:233-236`) — still zero callers on this tree
(re-verified by repo-wide grep; only docstrings and one test comment reference it).**
- **Recommend: no.** It could enumerate indexed projects for a prune UI, but since (d) is "no prune",
  wiring it now would create a caller for a feature we decided not to build. It stays dormant, as the
  merged car's open decision (d) already recorded.

---

## Integration notes for the train

- **Car 1 branch:** `fix/code-graph-digest-budget-reservation`, rebased onto
  `feat/v5.169-install-runtime-fixes`.
- **Car 2 branch:** `docs/code-graph-orphan-cache-note` — or fold into Car 1's commit if the train is
  being kept short; it is a single `MIGRATION_NOTES.md` block plus a CHANGELOG line.
- **Code seam is disjoint** from every other car in the train. `yadgar/core/code_graph/digest.py` is
  the only product file with a **behaviour** change; `cli/code_graph.py` gets a docstring-only edit
  (`:87`). `default_branch.py`, `runner.py` and `config.py` are **untouched**.
- **Doc collisions** — all already carry edits from merged cars, so keep every edit append-only or
  in-place-minimal, and rebase before pushing:
  - Car 1: `docs/contracts/BEHAVIOR_CONTRACT.md` (BC-CODEGRAPH-7 amend + BC-CODEGRAPH-8 add),
    `yadgar/core/hooks/templates/code_graph_refresh_prompt.md:34`.
  - Car 2: `MIGRATION_NOTES.md`, `docs/contracts/CAPABILITY_REGISTRY.md:1935`, `docs/CHANGELOG.md`.
- **Sequence Car 1 after `4d21ce80`** (already in the train): BC-CODEGRAPH-7 must exist before this
  car amends its wording.
