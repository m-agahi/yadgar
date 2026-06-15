# PLAN — v5.99.0: Roadmap freshness mechanism (DEFERRED)

**Renumbered:** v5.30.0 → v5.99.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30. Far-future deferred slot bumped to v5.99.0 to maintain clear gap ahead of the active pipeline (now extending to v5.37.0) and to signal indefinite deferral.

**Status:** DEFERRED on 2026-05-30. Originally drafted + implemented as v5.10.4 on 2026-05-29; deferred to v5.30.0 after encountering fundamental design issues with yadgar's async wiki write queue; renumbered to v5.99.0 under skip-1 convention. Discussion required before resuming.

**Update note (2026-06-02 post-opus-review):** v5.41.0 `wiki_append_section` MCP tool + v5.41.4 `roadmap_update_lag` signal partially close the original drift problem. v5.99.0 deferral remains correct; resume ONLY if drift persists after v5.41.4 ships and the new convention is exercised across multiple ships.

**Master at deferral time:** core v5.10.3 (unchanged — no v5.10.4 release shipped).

**Sequencing:** v5.99.0 slot — far future / indefinite. Pick up after active pipeline (v5.11.0–v5.37.0) clears and the issues below are resolved by either (a) new yadgar primitive `flush_only`, (b) explicit design choice on splice-vs-full-render, or (c) different architecture entirely.

---

## Why this exists

Roadmap wiki body was drifting silently across releases — metadata could be tag-touched without rewriting body. v5.10.4 was an attempt to enforce freshness via 4-part defense (regenerator + pre-commit + nightly + CI `--check`).

The mechanism works on the FILE artifact (`docs/ROADMAP_GENERATED.md`). The wiki publish layer hit fundamental issues with yadgar's storage model.

---

## What was built (preserved for reference)

Branch (NOT pushed, NOT merged): `chore/v5.10.4-roadmap-freshness-renumber`

Commits on that branch:
- `73f0f79` — renumber v5.10.4–7 → v5.10.5–8 + new freshness plan
- `2626ac6` — agent implementation: script + 18 tests + pre-commit + nightly step + CI workflow
- `6f5a12d` — MCP transport hotfix (JSON-RPC envelope + SSE Accept + sse parse + wiki_add overwrite)
- `bdd45d2` — WIP splice (Option B): 7 additional tests pass but async read-after-write unreliable

Files added on that branch:
- `scripts/refresh_roadmap_pipeline.py` — 3-mode regen (default / `--check` / `--dry-run`)
- `docs/ROADMAP_GENERATED.md` — tracked autogen artifact, sentinel-bounded
- `yadgar/tests/test_refresh_roadmap_pipeline.py` — 25 tests (18 unit + 7 splice)
- `yadgar/tests/fixtures/plan_v5_99_99_example.md`, `yadgar/tests/fixtures/changelog_example.md`, `yadgar/tests/golden/roadmap_pipeline_section.md`
- `.pre-commit-config.yaml` extension (`roadmap-freshness` hook)
- `.forgejo/workflows/ci.yaml` extension (`Check roadmap freshness` step)
- `yadgar/scripts/nightly_cycle.py` extension (step 8, `_step_roadmap_freshness`)
- Version bump 5.10.3 → 5.10.4 across `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock`

To resume: `git checkout chore/v5.10.4-roadmap-freshness-renumber` (still exists locally as of 2026-05-30; may need rebase onto then-current master).

---

## Design (original 4-part defense)

1. **`scripts/refresh_roadmap_pipeline.py`** — idempotent regenerator. Reads `docs/PLAN_V5_*.md` headers (`# PLAN — vX.Y.Z: title`, `**Status:**`, `**Sequencing:**`) + `CHANGELOG.md` head + `git tag --sort=-creatordate` + `pyproject.toml` version. Writes between sentinels.
2. **Pre-commit hook** — fires on staged `docs/PLAN_V5_*.md` / `CHANGELOG.md` / `pyproject.toml` version change. Runs `--check`; blocks commit on drift.
3. **Nightly cycle step 8** — runs regen post-vacuum as backstop for manual edits outside commits.
4. **CI `--check` mode** — semantic diff (gofmt-style, NOT mtime). Server-side gate against bypassed pre-commits.

Sentinels: `<!-- AUTO:ROADMAP:CURRENT START/END -->`, `<!-- AUTO:ROADMAP:PIPELINE START/END -->`, `<!-- AUTO:ROADMAP:SHIPPED START/END -->`.

Source-of-truth precedence (high → low): `pyproject.toml` → `git tag` → `CHANGELOG.md` → `docs/PLAN_V5_*.md`.

The file-artifact side (`docs/ROADMAP_GENERATED.md`) works correctly. CI `--check` works. Pre-commit works. Nightly works. Drift detection works.

**The problem is wiki publish.**

---

## Issues encountered

Numbered for traceability — each one is a real bug or design limitation surfaced during v5.10.4 development.

### Issue 1: Agent shipped script with wrong MCP transport (FIXED in 6f5a12d)
Agent's initial implementation POST'd to `http://127.0.0.1:8765/mcp` with naive `{method, params}` payload. Every wiki publish silently failed with `wiki_update failed: Wiki page 'yadgar-roadmap-future-improvements' not found via wiki_list`. Four sub-bugs:
- Missing JSON-RPC 2.0 envelope (`jsonrpc: "2.0"`, `id`).
- Missing `Accept: application/json, text/event-stream` header (yadgar replies `400 Not Acceptable` otherwise).
- Response is SSE-framed (`event: message\ndata: {payload}\n\n`) — `json.loads(resp.read())` fails; must parse last `data:` line.
- `_mcp_call` returned whole envelope (including `id`/`error`) instead of unwrapping `result` field.

**Resolution:** rewrite `_mcp_call`. Committed in `6f5a12d`. Tested end-to-end.

### Issue 2: `wiki_list` does NOT expose page `id`
`wiki_update(page_id, fields)` requires numeric ID. `wiki_list` returns `{slug, title, category, tags, confidence, created_at, updated_at, source_count}` — no `id` field.

**Workaround applied:** switched to `wiki_add(title, content, append=False)` — overwrites by title-derived slug, no ID needed. Dead code (`_find_wiki_page_id`) deleted.

**Open question:** should yadgar surface `id` from `wiki_list`? Or document `wiki_add(append=False)` as the canonical overwrite path?

### Issue 3: `wiki_list.content` is one entry per page (NOT a list of all pages)
Agent assumed `result.content[0].text` was JSON-of-all-pages. Actually `result.content` is a list of N entries, each `{type: "text", text: <JSON-of-one-page>}`. Reading `content[0]` returns only the first page.

**Workaround applied:** prefer `result.structuredContent.result` (canonical `list[dict]`) with content-fallback that iterates all entries.

**Open question:** is the N-entries-with-text shape intentional or a bug in yadgar's MCP content packaging? Could simplify if yadgar always populated `structuredContent.result` consistently.

### Issue 4: `wiki_add` overwrite wipes manual prose
`wiki_add(append=False)` replaces entire page content. The autogen artifact only contains 3 sections (Currently deployed / Pipeline / Recently shipped). Manual prose elsewhere on the wiki (Invariants list, Parked items, design notes) is silently dropped on every publish.

**Two attempted solutions (both incomplete):**
- **Option A (REJECTED earlier):** track `docs/ROADMAP_GENERATED.md` artifact and let humans edit wiki freely. Wiki and artifact diverge — defeats the freshness goal.
- **Option B (committed as WIP in `bdd45d2`):** splice between sentinels. Read existing wiki via `wiki_read` → splice new autogen sections between sentinels → `wiki_add` overwrite. Preserves manual prose mechanically. 7 splice tests pass. End-to-end blocked by Issue 5.

### Issue 5: yadgar wiki writes are async + eventually-consistent (FUNDAMENTAL)
yadgar's storage queues `wiki_add`/`wiki_update` writes for consolidation. Read-after-write races: script writes content X, then 2 seconds later reads via `wiki_read`, gets pre-X content. Splice operates on stale base → on subsequent runs, manual prose silently disappears.

Observed empirically: published rich content, slept 2s, ran script, observed wiki ended up autogen-only. Re-ran after `consolidate_now` (13 min) — same result, because the script ran BEFORE the previous consolidate completed.

**No clean fix in v5.10.4 scope.** The fundamental Read-Your-Own-Writes property is not provided by yadgar storage at the MCP level.

### Issue 6: `consolidate_now` is NOT a queue-flush primitive (13-min sleep cycle)
First instinct to fix Issue 5: call `consolidate_now()` to force flush. Reality: `consolidate_now()` runs the FULL sleep cycle:
- memify reweight (~700 memories)
- 100 similarity links
- 76 memify-derived
- 40+ community clusters
- dream replay (20 pairs)
- causal DAG edges
- anchor audit
- narrative generation across 52 directories (each = LLM call)
- re-embed pass

Total: ~13 minutes. Per v5.7.0 design, meant for NIGHTLY 19:00 UTC run only. Calling on-demand for "force flush" is wildly inappropriate.

**Implication for any future yadgar-write-then-read script.** Same gotcha will hit any author who needs synchronous behavior. A dedicated `flush_only` MCP tool (commit queued writes without sleep cycle) would unblock both v5.99.0 and other future tooling.

### Issue 7: `ruff format` strips parens from `except (A, B):`
`ruff format` (v0.15.x in repo) auto-rewrites `except (json.JSONDecodeError, TypeError):` to invalid `except json.JSONDecodeError, TypeError:` (Python-2 era syntax, raises `SyntaxError` in Python 3).

**Workaround applied:** use single `except Exception:` with `# noqa: BLE001` comment, OR avoid tuple-of-exceptions in this codebase.

**Open question:** is this a known upstream ruff bug or a config-specific issue? File a yadgar-internal lint preset to forbid `except (A, B):` patterns until ruff fixes it?

### Issue 8: Renumber chain when inserting in pipeline middle is high-touch
Inserting v5.10.4 = freshness pushed prior chain by +1 (nightly → v5.10.5, etc.). Each plan file had multiple version-string references (title, sequencing, version-bump notes, intra-plan tables). Mechanical but error-prone. Renumber commit `73f0f79` got 8 edits across 4 files plus a new file plus a rename of recently-added plan.

**Implication:** if v5.20.0 (or its successor) lands as a re-insertion, plan to do this renumber as a single atomic commit with both the new plan + all renumbers. OR avoid pipeline-middle insertions; always add at the tail and document sequencing in prose.

---

## Learnings

1. **yadgar MCP HTTP transport is non-trivial.** JSON-RPC 2.0 + dual Accept + SSE response framing + JSON-RPC unwrap. Document in a yadgar wiki page (already memorized as anchor 2026-05-29 — see `yadgar-mcp-transport-gotchas`).

2. **Tests can pass while end-to-end is broken.** Agent's 18 unit tests mocked MCP integration. None exercised real HTTP. Discovered transport bug only by running the script live. Always include at least ONE integration test that hits a real daemon (gated by env knob if daemon may be down in CI).

3. **The `gofmt --check` idiom is correct, mtime is wrong.** Initial proposal to lint via `wiki_updated_at >= max(source_mtime)` was fragile (git clone breaks mtimes, whitespace edits trip refresh, doesn't catch semantic drift). Semantic diff via `--check` mode (regenerate-and-compare) is the right pattern.

4. **Async write queues conflict with read-modify-write scripts.** Any yadgar-write-then-read pattern needs to assume eventual consistency. Either: (a) make the write fully describe the new state (no read needed); (b) tolerate stale reads with a retry/settle loop; (c) require new primitive.

5. **`wiki_add(append=False)` is the canonical wiki-overwrite path.** Not `wiki_update`. Document this in MCP tool descriptions if not already.

6. **Don't call `consolidate_now()` outside nightly.** It's a synchronous heavy-ML operation, not a flush. Memorized as anchor.

7. **Agent dispatch needs verification.** Agent claimed clean ship; transport bug found in 5 minutes of manual verification. Always re-test the deliverable, not just the agent's report.

---

## Open questions (to resolve when picking up v5.99.0)

1. **Splice (B) vs full-render (C)?**
   - **B:** wiki = manual prose + sentinel-bounded autogen sections, spliced on publish. Pros: humans can edit non-autogen wiki sections freely. Cons: requires read-after-write (Issue 5).
   - **C:** wiki = whole regenerated content every time. Manual prose lives in script template or `docs/ROADMAP_STATIC.md` concatenated by script. Pros: no read-after-write, no async dependency. Cons: humans can't edit manual prose on wiki; must edit file + re-run.
   - Recommendation: **C** unless yadgar grows `flush_only` MCP primitive that resolves Issue 5.

2. **Should yadgar grow `flush_only` MCP tool?**
   - Cheap: commit queued wiki/memory writes to backing store without sleep cycle.
   - Unblocks Issue 5 generically (not just for this script).
   - Probably v5.13+ design point (separate from this plan).

3. **If C: where does static prose live?**
   - Inline in `scripts/refresh_roadmap_pipeline.py` (template constant)?
   - `docs/ROADMAP_STATIC.md` (separate file, script concatenates)?
   - `docs/ROADMAP_TEMPLATE.md` with `{{AUTO:CURRENT}}` placeholders for the autogen sections?
   - Lean: `docs/ROADMAP_STATIC.md` — easy editing, no code change to update prose.

4. **Is the renumber-on-pipeline-middle-insertion worth the churn?**
   - Could just append new plans at tail with clear sequencing notes (no renumber needed).
   - Trade-off: numeric version order no longer matches dispatch order.

5. **Should `--check` mode also verify wiki matches artifact?**
   - Currently `--check` only verifies `docs/ROADMAP_GENERATED.md` matches what would be regenerated. Wiki content drift goes uncaught.
   - But wiki content drift may be intentional (humans editing manual sections in B mode).
   - Re-examine after B-vs-C decision.

---

## When to resume

Pick up v5.99.0 after at least one of:
- **Trigger A:** yadgar ships `flush_only` MCP primitive (unblocks Option B).
- **Trigger B:** explicit decision in user discussion to go with Option C (then this becomes mechanical: pick prose location, rewrite `_publish_to_wiki`, drop splice code).
- **Trigger C:** another roadmap-drift incident bad enough to force the issue.

Before resuming, REVIEW THIS DOC and the feature branch:
```
git checkout chore/v5.10.4-roadmap-freshness-renumber
git log --oneline master..HEAD
```

---

## Dependencies + non-deps

Dependencies (must be in place):
- yadgar MCP HTTP transport stable (it is).
- `pyproject.toml` + `CHANGELOG.md` + `docs/PLAN_V5_*.md` continue to be canonical source.

Non-deps (this plan is independent of):
- v5.10.4 nightly cycle bugs (unrelated, ships under its current slot)
- v5.10.5 session-end capture
- v5.10.6 viz fixes
- v5.10.7 secret-gate context-awareness
- backend-v5.4.x CE perf
- v5.21.0 anchor cross-project

---

## Risks if NOT resumed

- Roadmap wiki keeps drifting silently on every release. Same drift bug that prompted this plan in the first place.
- File artifact (`docs/ROADMAP_GENERATED.md`) doesn't exist on master (lives only on the feature branch). So `--check` lint isn't running anywhere either.
- Each new release cycle reproduces the manual-rewrite-the-wiki ritual.

**Acceptable trade-off** for deferral: pipeline ahead has higher value (security fixes in v5.10.7, viz UX in v5.10.6, etc.). Manual roadmap refresh is a known cost we keep paying until v5.99.0 lands.
