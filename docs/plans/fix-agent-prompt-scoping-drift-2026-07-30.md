# Fix cross-project wiki page scoping drift — legacy residue, not an active leak

**Date:** 2026-07-30
**Status:** planned, not started
**Task:** #0093
**Trigger:** a cross-repo session (from `/home/max/quinyx/infrastructure/aws/control-tower`)
found 9 wiki pages carrying a project-scoped `directory_context` — 8 agent-prompt
library pages plus `model-tier-dispatch` — and proposed forcing `global` at write
time plus an invariant. Investigation showed the write-time fix **already shipped**
on 2026-07-22.

---

## 1. Root cause — and what is already fixed

### 1.1 The write-time enforcement exists

`yadgar/_shared/wiki/policy.py:97`:

```python
POLICY_BY_TYPE: dict[str, WikiPolicy] = {
    "agent_prompt": WikiPolicy(
        gate_mode="similarity",
        recall_disposition="exclude",
        dir_scope="strict",
        merge="allow",
        storage_scope="global",
    ),
}
```

`yadgar/_shared/wiki/store.py:715` — the single write chokepoint:

```python
if _get_wiki_policy(page_type).storage_scope == "global":
    effective_dir = "global"
    branch = None
```

Both fields are forced. The **type**, not the caller, decides scope. The
in-source comment (`store.py:717-727`) already names the failure mode this plan
was opened for — *"the agent-prompt 404 drift"* — and explains the branch
coupling: a global page written with a caller `branch_hint` (SessionStart passes
`"master"`) strands at `global + branch=<x>`, unreachable via §25 step 3
(`directory='global' AND branch IS NONE`), **yet still reachable via the
plain-slug prelude path**.

Shipped: `c318ad7b`, 2026-07-22, core 5.161.0 (#83, ADR-0158 `wiki_policy`).
Pinned by `yadgar/tests/core/test_agent_prompts.py:101` (project dir → global)
and `:132` (raw `wiki_add` with `page_type='agent_prompt'` → global).

### 1.2 Therefore the 9 observed rows are legacy residue

Written before 2026-07-22 and never re-saved since. This inverts the framing of
the originating report ("fix the mechanism, not the rows"): for the 8
agent-prompt pages the mechanism **is** fixed and the rows are the only
remaining artifact.

### 1.3 Severity is not what it appears

The prelude's pattern lookup is **not** §25-scoped:

```
agent_dispatch_prelude → _cached_agent_prompt → _read_agent_prompt
  → storage.get_wiki_page_by_slug(slug)
  → SELECT * FROM wiki_page WHERE slug = $slug LIMIT 1     # wiki.py:383
```

No directory, no branch. So the 8 misscoped agent-prompt pages have been loading
correctly in every dispatch from every directory. **Nothing was broken about
them except auditability.**

`model-tier-dispatch` is the opposite case: it is reached via `wiki_read` and
`[[...]]` links — both §25-scoped — and it is double-pinned
(`/home/max/git/yadgar` + `branch=master`). Its ~23 inbound pointers are
genuinely dead outside yadgar-on-master.

**Real cost incurred:** an audit agent run from control-tower reported all 9 as
dangling pointers and recommended deleting the TOC rows. Acting on that would
have destroyed live pages. Misscoping does not merely hide pages — it
manufactures false drift findings.

---

## 2. Residual gaps (the parts still worth building)

| # | Gap | Evidence | Severity |
|---|---|---|---|
| G1 | No heal/detect path for pre-policy rows | `store.py:722` says drifted rows are healed via `wiki_set_metadata` — manual, on demand, unowned. `check_invariants` has no scope check. | high |
| G2 | UPDATE half-heals | `store.py:762` puts `directory_context` in `updates`; **`branch` is absent**. Re-saving a drifted page fixes the directory and leaves the branch pinned forever. | high |
| G3 | No page class for cross-project convention | `POLICY_BY_TYPE` has exactly one entry. `model-tier-dispatch` is a convention page; nothing covers it, so a convention page written from inside a git repo is *necessarily* double-pinned. | high |
| G4 | Chokepoint bypass | `yadgar/backend/admin_exec/wiki.py:450-481` — the "wiki not initialised" fallback inserts `directory_context=effective_dir` via raw `storage.insert_wiki_page`, skipping `WikiStore.add` and its policy. | low (self-described should-not-happen, but it falsifies "single enforcement point") |
| G5 | Pattern miss is silent and ambiguous | `dispatch_helper.py:433` wraps the lookup in `except Exception: logger.debug(...)`. "No such pattern", "exists but unreadable here", and "the read threw" are one indistinguishable outcome — callers then write bespoke dispatches and re-save duplicate patterns. | medium |
| G6 | No sanctioned unscoped read | §25 has no unscoped mode, so a drift audit structurally cannot distinguish misscoped from deleted. `db_inspect` is gated on `YADGAR_DEBUG_APIS_ENABLED` (off in prod) — i.e. unavailable exactly where audits run. | medium |

G2 is why natural re-save never converges: an agent-prompt page touched today
gets its directory healed and keeps its stale branch.

---

## 3. ADRs — not the same bug

`adr` is **not** in `POLICY_BY_TYPE` → resolves to `DEFAULT_POLICY` →
`storage_scope="project"`. Combined with `_wiki_write_canonical` setting
`branch=None` (`wiki.py:175`), ADR pages are **branch-canonical, deliberately
project-scoped**: each project owns its own ADR log.

`adr_list` returning empty from `/home/max/quinyx/infrastructure/aws/control-tower`
is therefore **correct** — that directory owns no ADRs. The ~18 yadgar ADR ids
cited across the agent-prompt corpus are a *content* problem (globally-shared
prompt text citing one repo's decision log), not a scoping defect. Out of scope
here; see §6.

---

## 4. Decision

Not an architectural redesign. Ranked:

1. **Backfill the 8 agent-prompt rows** — `wiki_set_metadata`
   (`field="directory_context"`, `value="global"`), all-rows, idempotent,
   versioned. Closes the residue. Must use the all-rows tool, never a fresh
   write: `get_wiki_page_by_slug` is `LIMIT 1`, so a global row coexisting with
   a project row makes the prelude pick whichever the engine returns first.
2. **Backfill `model-tier-dispatch`** — needs **both**
   `directory_context → "global"` and `branch → null`.
3. **Decide the convention-page class (G3)** — ADR-worthy. Either add a
   `convention` / `cross_project` `page_type` with `storage_scope="global"`, or
   state explicitly that convention pages must be authored with
   `directory="global"` and accept that nothing enforces it. Without this,
   `model-tier-dispatch` recurs the next time a convention page is saved from
   inside a repo.
4. **Invariant as detector, not guard (G1)** — `check_invariants` rule: any
   `page_type` whose policy declares `storage_scope="global"` must have
   `directory_context='global' AND branch IS NONE`. Catches pre-policy drift and
   any future G4-style bypass.
   **Auto-repair, not report-only** — justified *specifically here* because the
   target state is a constant with no judgment call (unlike the dangling-FK
   repairs already in `invariants.py`), and `wiki_set_metadata` is already
   all-rows / idempotent / versioned. Log every repair.
5. **Fix G2** — heal `branch` on the UPDATE path. Non-trivial: the generic
   setter stores an explicit `null`, which is the branch-null trap the comment
   at `store.py:723-727` warns about. Needs the same omit-the-column treatment
   `insert_wiki_page` uses, or an explicit `wiki_set_metadata` call from the
   update path.
6. **Distinguish miss from unreadable (G5)** — `agent_dispatch_prelude` should
   separate "no such pattern" from "pattern exists but is not readable from
   here" from "the read raised". Overlaps task #0015's *"ADD prelude fuzzy-match
   on exact-miss"* — do not duplicate; fold in.
7. **Audit read surface (G6)** — a read-only unscoped wiki list mode. Preferred
   over documenting the `db_inspect` recipe, which is debug-gated off in prod.

### Rejected

**Parent-directory-prefix fallback in §25 resolution.** Right complaint, wrong
fix. It changes read semantics corpus-wide to repair 9 rows; the quinyx paths
show deep nesting is normal, so project-scoped pages would start leaking across
sibling trees. It also cannot fix `model-tier-dispatch`, which is branch-pinned
as well as directory-pinned. If the underlying complaint ("a child dir cannot
read its own repo-root page") needs answering, the narrower lever already
exists: `_resolve_project_root` (`server_helpers.py:187`) walks to the git root
on the ADR path — the wiki read path simply does not use it.

---

## 5. Anti-recurrence invariant

> For any `page_type` whose `WikiPolicy.storage_scope == "global"`, every
> `wiki_page` row with that type MUST have `directory_context = 'global'` AND
> `branch IS NONE`.

Expressed against the policy table rather than a hardcoded `agent_prompt`
literal, so adding a convention page class (item 3) extends coverage for free.

---

## 6. Explicitly out of scope

- ADR scoping — correct by design (§3).
- Rewriting yadgar-ADR citations out of globally-shared prompt text. Real, but a
  content-hygiene task, not this defect.
- Task #0009 (`normalize_write_context('global')` CWD-sensitivity guard) — an
  adjacent open task on the memorize/misc write paths. Cross-reference; the
  agent-prompt path does not use that helper and is covered by policy instead.

---

## 7. Execution gating

Backfill (items 1-2) is a **separate, user-approved step**. It mutates live wiki
rows and must not ride silently with the code changes. Sequence:

1. Land items 3-7 (code + ADR).
2. Re-run the detector, confirm it reports exactly the expected rows.
3. Ask for approval.
4. Backfill; re-run detector; confirm zero.

Verification query (needs `YADGAR_DEBUG_APIS_ENABLED`):

```sql
SELECT slug, directory_context, branch, page_type FROM wiki_page
WHERE page_type = 'agent_prompt'
  AND (directory_context != 'global' OR branch IS NOT NONE)
```

## 8. Open question for review

`model-tier-dispatch`'s `page_type` / `category` were inferred from the wiki
catalog, not read directly. Confirm both before building on the
agent-prompt-vs-convention split in §2/G3 — it is the load-bearing distinction
for item 3.
