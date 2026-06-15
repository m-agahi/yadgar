# PLAN — v5.42.2: wiki branch-default fix (silent similarity gate, real root cause)

**Status:** drafted 2026-06-02 night. Critical hotfix — replaces refuted v5.42.2 HNSW-rebuild plan.

**Origin:** v5.39.0 / v5.41.5 / v5.42.0 / v5.42.1 all shipped passing unit tests but the live similarity gate never fires on real near-duplicates. Four consecutive fix attempts targeted the wrong layer (CREATE/store/threshold/backfill). The HNSW-rebuild hypothesis written into the prior v5.42.2 plan was empirically refuted: SurrealDB 3.0.5 HNSW auto-updates on UPDATE; REBUILD INDEX is a no-op.

The real root cause is a **branch-scope filter mismatch** confirmed by direct live probe 2026-06-02:

```
wiki_check_duplicate(near-clone content, branch=None  )  → candidates: []
wiki_check_duplicate(near-clone content, branch="master") → candidates: [{sim: 0.9055, slug: <existing>}]
```

Same content, only the `branch` parameter differs. Gate works when scope correct; silent when not.

**Mechanism:**

1. Pages are stored with `branch="master"` because the drainer's `_fill_wiki_add_defaults` (`yadgar/file_queue/dlq.py:127-134`) injects a hardcoded `"master"` when the payload omits `branch`. Since v5.41.5 moved the similarity gate to the drainer pre-apply stage, **every wiki write** flows through this path. Confirmed live: 19 of 20 sampled production pages have `branch="master"`.
2. `WikiStore.find_similar_wiki_pages` (`yadgar/wiki.py:490-524`) builds `allowed_branches = {None}` when called with `branch=None`. KNN candidates whose `page_branch not in allowed_branches` are dropped.
3. The `wiki_check_duplicate` MCP handler (`yadgar/server/tools/wiki.py:663-705`) defaults `branch: str | None = None` and passes it straight through. Unlike `wiki_query` and `wiki_read`, it does NOT auto-detect the current/default branch via `_detect_branch` / `_get_default_branch`.

Result: every production call to `wiki_check_duplicate` runs against scope `{None}`, excludes all `branch="master"` pages, returns zero candidates, gate stays silent.

**Why all 4 prior attempts missed it:** existing tests use the CREATE path with matching branch on both write and check sides. No test exercised "write via drainer (branch defaults to master) → check_duplicate without explicit branch (defaults to None)" — the exact production sequence.

**Branch:** `fix/v5.42.2-wiki-branch-default-fix` off master.

**Effort estimate:** 0.5 calendar day.

---

## 1. Problem state

| Layer | State |
|---|---|
| Drainer `_fill_wiki_add_defaults` | Injects hardcoded `branch="master"` when missing |
| MCP `wiki_add` direct handler | Stores `branch=None` when neither `branch` nor `branch_hint` supplied |
| MCP `wiki_query` | Auto-detects current+default branch via `_detect_branch`/`_get_default_branch` |
| MCP `wiki_read` | Auto-detects (§25 3-step resolution) |
| MCP `wiki_check_duplicate` | **Does NOT auto-detect** — passes raw `branch=None` |
| `find_similar_wiki_pages` | Builds `allowed_branches = {None}` when called with `branch=None`; excludes `branch="master"` pages |
| Result | Two incoherent "canonical" slots (None vs master) + silent gate |

Live sample (2026-06-02, 20 pages randomly sampled out of 200):
- 19 with `branch="master"`
- 1 with `branch=NULL`

Multi-branch wiki pages: **0 observed.** Branch-multiplexing exists in code but not in data.

## 2. Goal

Make the similarity gate fire on real near-duplicates in production, and normalize the canonical slot so writer paths agree.

## 3. Scope (minimum viable fix)

Two single-line changes plus one RED test. No schema migration. No data migration. No tool removal.

### 3.1 Drainer default → canonical (None)

File: `yadgar/file_queue/dlq.py:127-134` (`_fill_wiki_add_defaults`).

Change: when payload omits `branch`, store `None` instead of `"master"`. Matches the MCP `wiki_add` direct handler's behavior. Both writer paths now produce identical canonical-slot pages.

### 3.2 `wiki_check_duplicate` auto-detects branch

File: `yadgar/server/tools/wiki.py:663-705` (`wiki_check_duplicate` MCP handler).

Change: when `branch` is `None`, call `_detect_branch(os.getcwd())` and `_get_default_branch(os.getcwd())` and pass the resolved branch through to `find_similar_wiki_pages` — same pattern as `wiki_query` (lines 440-507). If detection fails (no git, no remote), fall back to `None` AND `find_similar_wiki_pages` already includes the default in scope, so the result is correct either way.

### 3.3 RED test that reproduces the production sequence

File: `yadgar/tests/test_v5_42_2_branch_default_e2e.py` (new).

Test sequence:
1. Initialize fresh test DB.
2. Submit `wiki_add(title=X, content=A, force=True)` — no `branch` param. Drainer applies it; with the fix, page lands with `branch=None`.
3. Call `wiki_check_duplicate(title=X, content=A')` against a near-clone — no `branch` param. With the fix, auto-detect runs; without the fix, scope `{None}` would exclude a `branch="master"`-tagged page.
4. Assert `candidates` includes the original slug with similarity ≥ 0.8.

Phase 1 (RED, pre-fix): test FAILS because drainer writes `branch="master"` and `wiki_check_duplicate` filters to `{None}`.

Phase 2 (GREEN, post-fix): test PASSES because drainer writes `branch=None` AND `wiki_check_duplicate` auto-resolves to a scope that includes the page.

The existing `yadgar/tests/test_v5_42_2_gate_fires_e2e.py` (written under the refuted HNSW hypothesis, uses MTREE-only embedded mode, fails at a test-design defect rather than the real bug) is removed.

## 4. Out of scope (deferred)

These items belong to v5.43.x or later, not v5.42.2:

- **Option C — drop branch field entirely.** Requires migration on 200 rows, removal of `wiki_cleanup_merged_branches` tool, removal of ~6 test files. Re-evaluate in v5.50+ after instrumentation confirms zero real users.
- **Hardcoded `"master"` cleanup in fallback paths.** Hits in `yadgar/server/tools/wiki.py:478,540,730`, `recall.py:86`, `project.py:185,1682,1844`, `export/views.sql:159,165`. These are exception-fallback strings — they trip only when `git symbolic-ref` fails. Real bug on `main`-default repos with no remote, but lower severity than the gate-silence bug. Schedule for v5.42.3 or v5.43.
- **Migration to move pre-v5 `branch="master"` rows to `branch=NULL`.** Backfill from migration 004 left old rows tagged `"master"`. They still resolve via step 2 on `master`-default repos; they go invisible on `main`-default repos. Schedule with the hardcoded-fallback cleanup.

## 5. Acceptance

- `pytest yadgar/tests/test_v5_42_2_branch_default_e2e.py -m integration` GREEN
- `pytest yadgar/tests/` overall — no regressions; in particular `test_wiki_read_resolution.py`, `test_branch_retrieval_filter.py`, `test_queue_drainer_validation.py`, `test_wiki_cleanup_merged_branches.py` continue to pass
- Live re-probe after deploy:
  - `wiki_add` an arbitrary near-clone of an existing prod page (via `force=True` to bypass the gate)
  - `wiki_check_duplicate` on the same near-clone content — must return the original slug with similarity ≥ 0.8
- `yadgar_wiki_add_rejected_total` becomes nonzero on real prod traffic within first deploy day

## 6. Risk

- `test_queue_drainer_validation.py:84-99` (`test_branch_filled_with_master_when_absent`) currently asserts the old `"master"` default. Will need update to assert `None` instead. **Confirm the test was guarding the existing behavior, not a contractual requirement** — i.e. there is no upstream consumer relying on drainer setting `"master"`.
- Pages written before this fix retain `branch="master"`. With Step 3.2 in place, `wiki_check_duplicate` auto-detects current branch (typically `master` or `main`); on a `master`-default repo the scope becomes `{None, "master"}` and those legacy pages remain reachable. On a `main`-default repo the legacy pages are invisible — but the hardcoded-fallback cleanup deferred to v5.42.3 covers that.
- `wiki_cleanup_merged_branches` continues to operate on `branch` values; behavior unchanged because the field still exists and pages with explicit non-default branches are still GC'd correctly.

## 7. Implementation order

1. Write RED test (`test_v5_42_2_branch_default_e2e.py`). Run it. Confirm RED.
2. Apply Step 3.1 (drainer default → None). Update `test_queue_drainer_validation.py` assertion.
3. Apply Step 3.2 (`wiki_check_duplicate` auto-detect).
4. Re-run RED test. Confirm GREEN.
5. Run full `yadgar/tests/` — assert no regressions.
6. Remove obsolete `yadgar/tests/test_v5_42_2_gate_fires_e2e.py`.
7. Version bump `5.42.1 → 5.42.2`.
8. CHANGELOG + MIGRATION_NOTES.
9. Local merge to master, podman build, codeberg push, nix bump.

## 8. References

- v5.42.2 ROOT CAUSE anchor (yadgar memory store 2026-06-02 night)
- v5.42.2 BRANCH FIELD AUDIT anchor (yadgar memory store 2026-06-02 night)
- Refuted prior plan: `docs/PLAN_V5_42_2_WIKI_HNSW_REBUILD.md` (delete in same commit as this file is added)
- v5.41.5 commit moving similarity gate to drainer: 0fc7220
- Live probe transcript: this session 2026-06-02
