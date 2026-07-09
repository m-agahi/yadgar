> ARCHIVED 2026-07-09 — R3-SHIPPED PR #171 / core 5.117.0; hot-tool always-load landed

# Plan — Fix Yadgar MCP Tool Deferral (Hot-Tool Always-Load)

**Date:** 2026-07-05
**Task:** #43 — yadgar exposes ~83 MCP tools; Claude Code's ToolSearch defers the entire
server toolset, sweeping the hot day-to-day tools (recall, memorize, project_brief,
checkpoint, restore, anchor) into deferral. A caller without the loaded schema then sends a
malformed blob and fails first-try (main-thread hit this 3×).
**Status:** Planning only. No code in this change; doc committed direct to master per the
`yadgar-workflow-plan-commits-direct-to-master` convention.

---

## 1. Context / Root Problem

Claude Code v2.1.191 ships a mechanism called **ToolSearch** (internal constant `Kg="ToolSearch"`).
It keeps MCP context usage low by **deferring** tool *definitions* — at session start only tool
*names* + server instructions load; the full JSON schema for a deferred tool is fetched on demand
via a `ToolSearch` tool. A deferred tool cannot be called until its schema is loaded.

When yadgar's whole 75-tool set is deferred, the hot tools defer too. If a caller (or subagent)
invokes `recall`/`memorize` without first loading the schema, the client sends an argument blob
that was never validated against the schema → `InputValidationError` on first try. This is the
observed 3× failure.

**Goal:** guarantee the small hot set is *always loaded* (never deferred), so recall/memorize/etc.
are callable first-try, while the ~65 rare/admin tools stay deferred (which is desirable — it keeps
context lean).

---

## 2. Full Tool Inventory (grouped)

**Count reconciliation (the "~83" vs actual):**

- `grep -c '@_tool' yadgar/server/tools/*.py` → **83** raw hits. This is the number in the task
  title. But it over-counts: `admin.py` and `__init__.py` hits are docstring/comment mentions and
  an import shim, not decorators; `_test_tools.py` has 2 test-only tools (`_test_sleep`,
  `_test_thread_id`) registered only under a test flag, not exposed in normal runs.
- **75** tools are actually exposed over MCP (matches both the deferred-tool list the client
  surfaces and an independent inventory pass). Use **75** as the real number.
- Of those 75, the existing `YADGAR_PROFILE=minimal` tier registers only **26** ("core", `power=False`)
  and omits **49** (`power=True`). See §4.

Registration site (file:line) is the `@_tool(...)` decorator for each. `hot?` marks the proposed
always-load set (§5); `power` is the existing in-repo tier flag.

### Memory (core loop)
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| recall | memory | **HOT** | F | yadgar/server/tools/recall.py:176 |
| memorize | memory | **HOT** | F | yadgar/server/tools/memorize.py:69 |
| recent_memories | memory | no | F | yadgar/server/tools/admin_other.py:212 |
| forget | memory | no | F | yadgar/server/tools/admin_other.py:36 |
| memory_get | memory | no | F | yadgar/server/tools/admin_other.py:487 |
| memory_update | memory | no | T | yadgar/server/tools/admin_other.py:533 |
| memory_stats | memory | no | F | yadgar/server/tools/admin_other.py:373 |

### Project / context (session start + compaction shield)
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| project_brief | project | **HOT** | F | yadgar/server/tools/project.py:2040 |
| checkpoint | project | **HOT** | F | yadgar/server/tools/misc.py:167 |
| restore | project | **HOT** | F | yadgar/server/tools/misc.py:254 |
| anchor | project | **HOT** | F | yadgar/server/tools/misc.py:387 |
| bootstrap_project | project | no | T | yadgar/server/tools/project.py:2126 |
| update_active_work | project | no | T | yadgar/server/tools/project.py:2224 |
| seed_project | project | no | T | yadgar/server/tools/misc.py:682 |
| install_hooks | project | no | T | yadgar/server/tools/misc.py:476 |
| sync_instructions | project | no | T | yadgar/server/tools/misc.py:522 |

### Dispatch library (subagent prompt contract)
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| agent_dispatch_prelude | dispatch | maybe | F | yadgar/server/tools/dispatch_helper.py:76 |
| agent_prompt_save | dispatch | no | F | yadgar/server/tools/agent_prompts.py:165 |
| seed_agent_prompts | dispatch | no | T | yadgar/server/tools/agent_prompts.py:332 |

### Wiki (read-first + curation)
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| wiki_query | wiki | maybe | F | yadgar/server/tools/wiki.py:565 |
| wiki_read | wiki | maybe | T | yadgar/server/tools/wiki.py:690 |
| wiki_list | wiki | no | T | yadgar/server/tools/wiki.py:773 |
| wiki_add | wiki | no | F | yadgar/server/tools/wiki.py:361 |
| wiki_get | wiki | no | F | yadgar/server/tools/admin_other.py:511 |
| wiki_check_duplicate | wiki | no | F | yadgar/server/tools/wiki.py:944 |
| wiki_history | wiki | no | F | yadgar/server/tools/wiki.py:1062 |
| wiki_diff | wiki | no | F | yadgar/server/tools/wiki.py:1118 |
| wiki_read_version | wiki | no | F | yadgar/server/tools/wiki.py:1092 |
| wiki_coverage | wiki | no | F | yadgar/server/tools/wiki_coverage.py:94 |
| wiki_delete | wiki | no | T | yadgar/server/tools/wiki.py:758 |
| wiki_approve | wiki | no | T | yadgar/server/tools/wiki.py:889 |
| wiki_drafts | wiki | no | T | yadgar/server/tools/wiki.py:874 |
| wiki_discard | wiki | no | T | yadgar/server/tools/wiki.py:931 |
| wiki_autolink | wiki | no | T | yadgar/server/tools/wiki.py:830 |
| wiki_lint | wiki | no | T | yadgar/server/tools/wiki.py:820 |
| wiki_append_section | wiki | no | T | yadgar/server/tools/wiki.py:1194 |
| wiki_insert_after | wiki | no | T | yadgar/server/tools/wiki.py:1386 |
| wiki_insert_before | wiki | no | T | yadgar/server/tools/wiki.py:1424 |
| wiki_insert_at | wiki | no | T | yadgar/server/tools/wiki.py:1557 |
| wiki_delete_at | wiki | no | T | yadgar/server/tools/wiki.py:1514 |
| wiki_delete_text | wiki | no | T | yadgar/server/tools/wiki.py:1349 |
| wiki_replace_at | wiki | no | T | yadgar/server/tools/wiki.py:1465 |
| wiki_replace_text | wiki | no | T | yadgar/server/tools/wiki.py:1301 |
| wiki_replace_markdown_block | wiki | no | T | yadgar/server/tools/wiki.py:1607 |
| wiki_restore | wiki | no | T | yadgar/server/tools/wiki.py:1150 |
| wiki_set_metadata | wiki | no | T | yadgar/server/tools/wiki.py:1257 |
| wiki_update | wiki | no | T | yadgar/server/tools/admin_other.py:593 |
| wiki_refresh_stale | wiki | no | T | yadgar/server/tools/project.py:2689 |
| wiki_cleanup_merged_branches | wiki | no | T | yadgar/server/tools/project.py:2727 |

### Blocks (memory-block CRUD)
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| block_create | blocks | no | T | yadgar/server/tools/blocks.py:47 |
| block_get | blocks | no | T | yadgar/server/tools/blocks.py:94 |
| block_update | blocks | no | T | yadgar/server/tools/blocks.py:135 |
| block_delete | blocks | no | T | yadgar/server/tools/blocks.py:182 |
| block_list | blocks | no | T | yadgar/server/tools/blocks.py:217 |
| block_replace | blocks | no | T | yadgar/server/tools/blocks.py:255 |
| block_append | blocks | no | T | yadgar/server/tools/blocks.py:308 |

### Bookmarks
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| bookmark_add | bookmarks | no | F | yadgar/server/tools/bookmarks.py:23 |
| bookmark_remove | bookmarks | no | F | yadgar/server/tools/bookmarks.py:59 |
| bookmark_list | bookmarks | no | F | yadgar/server/tools/bookmarks.py:83 |
| bookmark_reorder | bookmarks | no | F | yadgar/server/tools/bookmarks.py:111 |

### Rules / ADR
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| add_rule | rules | no | T | yadgar/server/tools/admin_other.py:443 |
| get_rules | rules | no | T | yadgar/server/tools/admin_other.py:477 |
| adr_add | adr | no | T | yadgar/server/tools/adr.py:142 |

### Admin / ops (DLQ, vacuum, consolidation, audit, validate, archive)
| tool | group | hot? | power | file:line |
|---|---|---|---|---|
| dlq_inspect | admin | no | F | yadgar/server/tools/admin_dlq.py:47 |
| dlq_requeue | admin | no | T | yadgar/server/tools/admin_dlq.py:126 |
| dlq_dismiss | admin | no | T | yadgar/server/tools/admin_dlq.py:193 |
| vacuum_now | admin | no | T | yadgar/server/tools/admin_vacuum.py:12 |
| vacuum_checkpoints | admin | no | T | yadgar/server/tools/admin_other.py:567 |
| consolidate_now | admin | no | T | yadgar/server/tools/admin_other.py:82 |
| reembed_all | admin | no | T | yadgar/server/tools/admin_other.py:140 |
| check_invariants | admin | no | T | yadgar/server/tools/admin_invariants.py:783 |
| validate_memory | admin | no | T | yadgar/server/tools/admin_other.py:47 |
| audit_anchors | admin | no | T | yadgar/server/tools/audit.py:727 |
| archive_purge | admin | no | T | yadgar/server/tools/admin_archive.py:18 |
| repo_wiki_generate | admin | no | F | yadgar/server/tools/repo_wiki.py:31 |

**Registration mechanism (single control point):** the `@_tool(power: bool = False)` decorator at
`yadgar/server/_app.py:347`. Its body (`_app.py:369-394`) skips registration when
`power and _PROFILE == "minimal"`, else calls `mcp_server.tool()(async_wrapper)` at `_app.py:391`.
`mcp_server` is a FastMCP instance; importing `yadgar.server.tools` fires all decorators
(`yadgar/server/tools/__init__.py`). Both transports (stdio and streamable-http/sse, selected at
`yadgar/server/lifecycle.py:878-887`) expose the identical registered set. There is no
allowlist/denylist env var — only the binary `YADGAR_PROFILE` (`_app.py:46`).

---

## 3. The Defer Threshold (what actually triggers ToolSearch)

Verified by disassembling the installed client binary, **Claude Code v2.1.191**
(`/nix/store/…-claude-code-2.1.191/…/.claude-wrapped`) — NOT from docs (docs claims were treated
as unverified and cross-checked against shipped code):

- **There is no fixed tool-count threshold.** The threshold is a **context-window-percentage**
  budget on the *combined description size* of tools, not a raw count.
- Env var **`ENABLE_TOOL_SEARCH`** controls the mode (`dNt()` in the binary):
  - **unset (default) / `true` / `1` / `yes` → `"tst"` mode**: ToolSearch active. Uses the default
    threshold `CCo=10` (≈10%, with an internal `×2.5` char multiplier → effective ~25% of context
    chars in description length before deferral kicks in). This is the mode we are running in.
  - `false` / `0` / `no` → `"standard"`: deferral OFF, all tools load upfront.
  - `auto` → `"tst-auto"` (same 10% default).
  - `auto:N` (N = 0–100 integer percent) → custom threshold; `auto:0` = always defer,
    `auto:100` = never defer.
- The per-request deferral decision short-circuits on always-load BEFORE the threshold test:
  `function ej(e){ if(e.alwaysLoad===true) return false; … }` — `false` = "do not defer".
- `alwaysLoad` for a tool is computed as the OR of two sources:
  `alwaysLoad: e.config.alwaysLoad===true || p._meta?.["anthropic/alwaysLoad"]===true`.
  - **server-level:** `alwaysLoad: true` in the `mcpServers` entry (`.claude.json` / `.mcp.json`) —
    Zod-schema field, describe string: *"When true, all tools from this server are always included
    in the prompt and never deferred behind tool search."* Present on stdio/SSE/HTTP/SDK server types.
  - **per-tool:** the tool's MCP `_meta` carries `"anthropic/alwaysLoad": true`.

**Implication (this flips the task's framing):** because our default mode (`tst`) evaluates a
whole-toolset description-size budget and 75 tools blow past it, the set defers *as a whole*.
Therefore **reducing the count (Option B) or splitting the server (Option A) do not, by themselves,
stop deferral** — only dropping under the size budget (fragile, count-sensitive) or an explicit
`alwaysLoad` exemption does. The exemption is the real lever.

**Unresolved but immaterial to the recommendation:** whether the size budget is *per-server* or
*aggregate across all connected servers* is not settled (the binary disassembly didn't resolve it;
the earlier docs pass guessed "aggregate"). It does not affect Option C — per-tool `alwaysLoad`
short-circuits deferral for those tools regardless of how the budget is scoped. It does *strengthen*
the case against Option A: if the budget is aggregate, splitting yadgar into two servers cannot
reduce total description size, so both halves would still defer — making A strictly weaker.

---

## 4. Existing in-repo lever: `YADGAR_PROFILE`

`_app.py:46` reads `YADGAR_PROFILE` (default `full`). `YADGAR_PROFILE=minimal` makes every
`@_tool(power=True)` return the undecorated function (no MCP registration) — leaving **26 core
tools**. This is a genuine, already-shipped hot/rare split at the *registration* layer.

But it does **not** defeat deferral on its own: 26 tools' combined description size may still exceed
the `tst` budget, and even if it didn't, relying on "stay under the budget" is brittle (adding one
tool or a longer docstring silently re-triggers deferral). Treat `YADGAR_PROFILE` as a useful
*building block* (it already knows the core/power split), **not** as the fix. The fix must be an
explicit `alwaysLoad` exemption.

---

## 5. Options

### Hot set (the always-load target)
The truly every-session/every-subagent tools. Keep it minimal — each always-loaded tool spends
context budget unconditionally, so do not pad it.

**Core hot (6):** `recall`, `memorize`, `project_brief`, `checkpoint`, `restore`, `anchor`.
**Candidate additions (decide via §Open-Questions):** `agent_dispatch_prelude` (every subagent
dispatch per the library rule), `wiki_query` + `wiki_read` (read-first contract), `recent_memories`.

Recommended initial exemption list: the **6 core hot** + `agent_dispatch_prelude` = **7**. Add
`wiki_query`/`wiki_read` only if the read-first contract proves to need them loaded first-try.

### Option A — Two MCP servers (general + rare)
Small "general" server (hot 7) + "rare" server (other 68), two `mcpServers` entries.
- With server-level `alwaysLoad: true` on the general server, its 7 tools never defer. **Works.**
- BUT this needs two server processes/ports, two `.claude.json` entries, nix/systemd + container
  wiring for a second endpoint, auth for the second endpoint, and a decision on shared-vs-separate
  backend (they must share the SurrealDB + engines, or the hot tools can't see the same memory).
  Splitting a stateful, engine-backed server is real surgery.
- The split itself is unnecessary for the fix: server-level `alwaysLoad: true` on the *current*
  single server would load all 75 (defeats the point), so A only "works" *because* of the split —
  but the same guarantee is available per-tool on one server (Option C) with none of the process cost.
- **Verdict:** works, but strictly more infrastructure than needed. Fallback only.

### Option B — Trim/consolidate under the threshold (one server)
Merge related tools behind fewer entrypoints (mode/action param), retire dead tools, to get the
whole set under the `tst` budget.
- The threshold is description-size, not count, and is **not officially fixed** — "under the budget"
  is a moving target; one added tool or longer docstring silently re-defers everything.
- Consolidating 30 wiki CRUD tools behind one dispatcher is a large, behavior-changing refactor with
  its own schema-usability cost (a mega-tool with a `mode` enum is harder for the model to call well).
- Good *hygiene* regardless, but a bad *mechanism* for the deferral fix — it does not give a hard
  guarantee. **Verdict:** do independently as cleanup; not the fix.

### Option C — Per-tool `alwaysLoad` exemption (one server) — RECOMMENDED
Mark the hot set with `_meta = {"anthropic/alwaysLoad": true}`; leave everything else deferred.
- **Client honors it — verified in the shipped v2.1.191 binary:**
  `alwaysLoad: … || p._meta?.["anthropic/alwaysLoad"]===true`, and the deferral function returns
  "don't defer" when `alwaysLoad` is set.
- **Server can emit it — verified in the installed `mcp==1.27.0`:** FastMCP `.tool()` accepts a
  `meta=` kwarg (`mcp/server/fastmcp/server.py:453`); the `Tool` model carries `meta`
  (`mcp/server/fastmcp/tools/base.py:39`); `list_tools()` serializes it as `_meta`
  (`mcp/server/fastmcp/server.py:327`, aliased at `mcp/types.py:1331`). No library patch needed.
- Cost: **~2 lines** in the `@_tool` decorator + a per-tool flag. One server, one process, one
  config. The rare tools stay deferred (context stays lean — the whole point of ToolSearch).
- **Verdict:** cleanest. Hard guarantee, minimal surface, no infra.

### Option D — Accept deferral + caller discipline
No server change; rely on callers loading schemas via ToolSearch first, plus the SessionStart hook.
- This is the status quo that produced the 3× failure. Discipline is exactly what fails under
  auto-defer. **Verdict:** insufficient alone; keep the SessionStart hint as defense-in-depth.

### Recommendation
**Option C** (per-tool `anthropic/alwaysLoad` on the hot 7), single server. Both required facts are
verified against shipped code, not docs: the client reads the flag, the installed FastMCP emits it.
Keep `YADGAR_PROFILE` as-is. Do Option B's dead-tool retirement separately as hygiene. Keep the
SessionStart hint (Option D) as belt-and-suspenders. Ship Option A only if a future need forces a
genuine backend split — the deferral problem does not.

---

## 6. Implementation Sketch (for Option C)

**Instance type confirmed (load-bearing):** `mcp_server` is a `mcp.server.fastmcp.FastMCP`
instance — `from mcp.server.fastmcp import FastMCP` (`_app.py:10`), `mcp_server = FastMCP(...)`
(`_app.py:48`). There is no standalone `fastmcp` package in the deps (which has a different
`.tool()` API), so `mcp_server.tool(meta=…)` binds to the verified `mcp==1.27.0` method.

**Interim config-only stopgap (today, no code/deploy):** adding `"alwaysLoad": true` to the yadgar
entry in `.claude.json` restores first-try callability immediately — but it exempts ALL 75 tools,
loading every schema upfront and paying exactly the token cost ToolSearch exists to avoid. Use only
as a bridge until Option C ships (which needs a container rebuild + nix version bump + restart). It
is a stopgap, not the fix — remove it once per-tool exemption is deployed.

1. **Extend the decorator** (`yadgar/server/_app.py:347`):
   ```python
   def _tool(power: bool = False, always_load: bool = False):
       ...
       def decorator(func):
           if power and _PROFILE == "minimal":
               return func
           ...
           meta = {"anthropic/alwaysLoad": True} if always_load else None
           mcp_server.tool(meta=meta)(async_wrapper)   # was: mcp_server.tool()(async_wrapper)
           return sync_wrapper
   ```
   (`meta=None` is the FastMCP default → no behavior change for un-flagged tools.)
2. **Flag the hot set** at their `@_tool` sites: add `always_load=True` to `recall`, `memorize`,
   `project_brief`, `checkpoint`, `restore`, `anchor`, `agent_dispatch_prelude`.
3. **Test-first (per Test-Driven rule):** a test that starts the FastMCP server (or calls
   `list_tools()`), asserts the 7 hot tools carry `_meta["anthropic/alwaysLoad"] is True` and that a
   sample rare tool (e.g. `vacuum_now`) does not. Red before the decorator change, green after.
4. **No client-side config required** for the per-tool path — the exemption travels in the server's
   `tools/list` response. (Server-level `alwaysLoad: true` in `.claude.json` is the alternative but
   would exempt all 75; do not use it here.)
5. **Verify end-to-end:** after deploy, in a fresh session confirm the 7 hot tools are directly
   callable without a ToolSearch load step, and the rare ones still require it.
6. **Version + changelog** per `yadgar-versioning-convention` (this touches `.py` → normal Branch
   First + PR, NOT the plan-direct-to-master path).

---

## 7. Risks

1. **`_meta` propagation through yadgar's dual sync/async wrapper.** yadgar wraps each tool
   (`_build_tool_wrappers`, `_app.py:400`) before handing the async wrapper to FastMCP. The `meta`
   kwarg goes to `mcp_server.tool(meta=…)`, not the wrapper, so it should be independent — but the
   test in step 3 must assert `_meta` actually appears in `list_tools()` output, because the wrapper
   layering is nonstandard. Verify, don't assume.
2. **Hot-set sizing.** Every always-loaded tool spends context unconditionally; an over-broad hot
   set erodes the benefit ToolSearch was giving. 7 is deliberately tight. Resist scope creep.
3. **Client-version drift.** The exemption relies on v2.1.191 behavior. A future client could rename
   the `_meta` key or change ToolSearch. Low risk (it's an Anthropic-namespaced key), but pin the
   observed version in the eventual PR and re-verify if ToolSearch behavior changes.
4. **`YADGAR_PROFILE=minimal` interaction.** If someone runs minimal, `always_load` power tools are
   never registered anyway. The hot 7 are all currently `power=False` except confirm
   `agent_dispatch_prelude` (F) — fine — so minimal still exposes them. No conflict, but note it.
5. **Doesn't shrink the deferred set.** Option C fixes callability of the hot tools, not the size of
   the deferred catalog. That is intended and correct — but pair it with Option B hygiene over time
   so the rare catalog doesn't grow unbounded.
6. **stdio vs http parity.** Both transports serialize the same `Tool` objects, so `_meta` should
   ride both. The step-3 test should ideally cover the transport actually used in production
   (streamable-http per the nix module) — confirm the deployed transport.

---

## 8. Advisor Input

**Pass 1 (after inventory + draft design, before committing to a recommendation).**
The advisor flagged that the entire design rested on the docs-agent's client-config claims
(`alwaysLoad`, `anthropic/alwaysLoad`, the 10% threshold), which had a confabulation signature
(fake-precise tables, exact version numbers, a doc-anchor URL) and were unverified. It called this
*blocking*: if the client-side finding held, it would flip the user's framing — default defer mode
defers regardless of count, so split (A) and trim (B) don't help on their own; the real fork is
per-tool `_meta` (C) vs split + server-level `alwaysLoad` (A), decided by two facts not yet nailed:
(i) does the *installed* client honor per-tool `_meta`, (ii) can FastMCP + the `@_tool` decorator
emit per-tool `_meta`. It also said: don't oversell `YADGAR_PROFILE=minimal` (shrinks the set, not
the defer decision), and reconcile the 83-vs-75 count.
**Response:** dispatched two verification agents against ground truth (not docs) — grep the shipped
client binary, and read the installed `mcp` package. Both came back **verified** (see §3, §5-C):
client reads the flag; FastMCP emits it. Reconciled the count (§2: 75 exposed, 83 raw). Reframed
`YADGAR_PROFILE` as a building block, not the fix (§4). This turned C from "maybe" into the
evidenced recommendation and demoted A to fallback.

**Pass 2 (before finalizing).**
The advisor endorsed shipping Option C, crediting the both-ends verification (client binary +
installed `mcp==1.27.0`) as the thing that mattered — not to re-open the recommendation. Three
closers, none changing the rec: (1) *harden the one still-open assumption in the sketch* — confirm
`mcp_server` really is an `mcp.server.fastmcp.FastMCP` instance (not the standalone `fastmcp`
package, whose `.tool()` API differs), since the whole sketch rests on `mcp_server.tool(meta=…)`.
Done — grep confirmed `from mcp.server.fastmcp import FastMCP` / `mcp_server = FastMCP(...)` and no
standalone `fastmcp` dep (§6). (2) *state the per-server-vs-aggregate budget as unresolved* — it's
immaterial to C but strengthens the case against A; added to §3. (3) *add the config-only stopgap*
(server-level `alwaysLoad: true` today) with its cost flagged as a bridge, not the fix; added to §6.
It also warned not to chase the exact `10%×2.5` threshold arithmetic — immaterial to C. All three
folded in; recommendation unchanged.

---

## 9. Open Questions

1. **Hot-set membership.** Confirm the final always-load list. Core 6 is settled. Include
   `agent_dispatch_prelude`? (Every subagent dispatch touches it per the library rule → likely yes.)
   Include `wiki_query`/`wiki_read`? (Read-first contract — but they're only needed when the model
   chooses to consult the wiki, arguably fine to load on demand.) Include `recent_memories`?
2. **Deployed transport.** streamable-http vs sse vs stdio in the nix/systemd deployment — the e2e
   verification and the step-3 test should target it. (Read `modules/home/yadgar.nix`.)
3. **Do we also want server-level `alwaysLoad` anywhere?** No, for yadgar (would exempt all 75). But
   worth documenting as the mechanism for any *future* genuinely-small companion server.
4. **Retirement candidates for Option-B hygiene** (separate work): which of the 30 wiki CRUD /
   surgical-edit tools and the block CRUD are dead or mergeable? Out of scope for this fix.

---

*Deferrals of architectural significance from this plan → extract to `docs/DECISIONS.md` per the
plan-commit convention before/at commit.*
