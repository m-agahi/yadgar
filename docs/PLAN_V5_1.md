# Yadgar v5.1 Plan — `project_brief` Bootstrap + Branch Tagging + repo-wiki Dispatch

## Context

After v5 ships the security + bugs + observability backlog, the next pain point is **session bootstrap noise**. `get_project_context()` currently returns a heap of memories sorted by heat — useful but unstructured, and the agent still re-greps the codebase for facts yadgar already knows. There is no per-project "table of contents" memory; no awareness of in-flight work; no signal that repo-wiki pages have drifted from source; no branch dimension on stored knowledge.

v5.1 reshapes the bootstrap into a structured `project_brief` layered response, adds a one-per-directory `_project_init` memory that serves as the project's table of contents, adds `_active_work` for in-flight state, branch-tags memory + wiki_page rows, and wires the stop-hook to ask the running session whether `repo-wiki` should run in the background.

Also bundled: the mega-function decomposition (`retrieval/core.py:305` complexity 330, `causal_discovery.py:187` complexity 146) deferred from v5 — protected by a characterization test suite written first.

Version bump: `5.0.0 → 5.1.0`. Single release, sequential commits on `docs/plans-cleanup-and-v5` (same branch as PLAN_V5).

---

## 1. `project_brief` — layered bootstrap

Rename `get_project_context(directory: str) -> dict` to `project_brief(directory: str, mode: str = "catalog") -> dict`. Old name kept as deprecated alias for one release.

### Walk-up project-root resolution

Tool walks up from `directory` looking for `.git`, `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`. First hit wins. Returned as `_resolved_directory` in response. All downstream lookups (init memory, anchors, hot memories, wiki) use the resolved root, so a call from a subdirectory finds the project-level data.

### Mode `catalog` (default, ~500 tokens)

```json
{
  "_resolved_directory": "/home/max/git/yadgar",
  "_mode": "catalog",
  "project": "yadgar",
  "tech": "Python 3.14, SurrealDB v3, MCP, sentence-transformers",
  "branch": "master",
  "branch_state": "clean",
  "in_flight": "v5 ready to implement (PLAN_V5.md committed 15bbf22)",

  "init_memory": {
    "memory_id": 12345,
    "content": "<the _project_init memory's content, verbatim, ≤2000 chars>"
  },

  "anchors": [
    {"id": 15, "tag": "_anchor", "hook": "Codeberg PR via Forgejo + 1Password PAT"},
    {"id": 33, "tag": "_anchor", "hook": "Never query SurrealDB directly"},
    {"id": 50, "tag": "_anchor", "hook": "nix-update applies nix config changes"}
  ],

  "signals": {
    "stale_wiki_count": 3,
    "uncommitted_files": 0,
    "active_pr_age_days": null,
    "active_work_present": true,
    "active_work_age_hours": 4
  },

  "_render": "# yadgar — catalog\n\n**Branch**: master (clean) ..."
}
```

Notes:
- `init_memory` returns the full content of the `_project_init` memory (≤2000 chars enforced at write time). This is the project's table of contents.
- `anchors` returns 5 short hooks (id + tag + ~50-char hook). No content body.
- No hot-memory array in catalog mode — read via `recall()` when needed.
- `_active_work` content NOT inlined in catalog; only flagged in `signals.active_work_present`. Read with `recall('_active_work', max_results=1)` or `mode="full"`.

### Mode `full` (opt-in, ~1050 tokens)

Adds:
- `_active_work.content` — the in-flight markdown, inlined
- `recent_memories[5]` — filtered top-5 hot memories, 200 chars each, `_auto` / `_action_stream` tags excluded, anchors-first then `_active_work` then heat-sorted
- `snapshot.last_commits[5]` — recent git log
- `signals.stale_wiki[]` — slug list capped at 8 (full detail instead of just count)
- `_render` rendered with full content

### Token budget (estimated)

| Block | Catalog | Full |
|---|---:|---:|
| init_memory | 500 | 500 |
| anchors (5 × ~50 char hooks) | 70 | 70 |
| _active_work content | — | 300 |
| recent_memories (5 × 200 char) | — | 250 |
| git snapshot | 50 | 150 |
| signals | 50 | 100 |
| _render wrapper | 50 | 100 |
| **TOTAL** | **~520** | **~1050** |

### Layer compute cost

Cached 60s within a session. Cold call ~110 ms (catalog) / ~200 ms (full). Subprocess git calls (`rev-parse`, `status`, `log`, `symbolic-ref`) cached per `int(time.time() // 60)` bucket.

---

## 2. `_project_init` memory pattern

One memory per `directory_context`, tagged `_project_init`, `is_protected=True`, heat pinned at 1.0. Content is markdown ≤2000 chars (hard cap, server-enforced).

### Content shape (convention, not enforced)

```markdown
# <project> init

## Architecture wikis
- [[slug1]]
- [[slug2]]
- ...max 5

## Conventions (HARD rules)
- One line each, no prose
- ...max 5

## Key memory IDs
- 15 — short hook (under 60 chars)
- 33 — ...
- ...max 8

## Lookup tips
- BEFORE grep, try recall(query) or wiki_query(query)
- For module docs: wiki_read('mod-<file>')
- For function docs: wiki_read('fn:<file>::<func>')

## Active
- See _active_work memory
```

### New MCP tool — `bootstrap_project`

```python
@_tool(power=True)
def bootstrap_project(directory: str, content: str) -> dict:
    """Replace this directory's `_project_init` memory atomically.

    Content is markdown, MUST be <= 2000 chars (~500 tokens). Tool rejects
    with ValueError on overflow. Convention: pointers and short hooks only,
    no prose explanations. project_brief() surfaces the content verbatim
    at the top of the catalog response.

    Returns: {"replaced": bool, "memory_id": int, "char_count": int}
    """
    if len(content) > 2000:
        raise ValueError(
            f"content is {len(content)} chars, exceeds 2000 cap. "
            f"Trim with pointers + short hooks only — see tool docs for shape."
        )
    # delete-before-insert atomicity, same pattern as update_active_work
```

### Bootstrap path (B2 — combined options b + c)

- **(b) `seed_project` drafts starter.** When `seed_project(directory)` runs, the existing dir-scan logic now also drafts a starter `_project_init` from `README.md` + top-level docs + detected architecture markers. Drafted memory is created; user reviews and refines via `bootstrap_project` later.
- **(c) Stop-hook prompts when absent.** If `project_brief.init_memory` is null after N>5 sessions in a directory (tracked via simple counter memory), the stop hook injects a prompt: "no `_project_init` exists for this directory; propose one based on session context, call `bootstrap_project(directory, content)` to commit."

### Anchors integration (B3 — separate, init points to them)

Anchors stay as individual memories with `_anchor` tag. `_project_init` content references them by ID in its TOC section. `project_brief` surfaces both — init memory first (TOC), then anchors list. No duplication.

### Initial seed targets (B5)

After v5.1 ships, manually create `_project_init` for:
- `/home/max/git/yadgar`
- `/home/max/git/ccpm`
- `/home/max/git/nix`
- `/home/max/quinyx/meridian`
- `/home/max/quinyx/qwfm`
- `/home/max/quinyx/qwfm/tools/serverless/cloudfront-authorization-at-edge`

---

## 3. `_active_work` memory pattern

Single per-directory memory tagged `_active_work`. Auto-replaced (delete-before-insert) on each refresh. Content format: markdown. Example:

```markdown
**Branch:** docs/plans-cleanup-and-v5
**Open PR:** none
**Updated:** 2026-05-11T13:50:00Z

## Next steps
- Implement `project_brief()` layered output in `yadgar/server.py`
- Add `_active_work` test fixture
- Wire `wiki_refresh_stale()` to dispatch background Agent

## Recent context
- v5 plan committed (15bbf22); v5.1 plan in progress.
```

### New MCP tool — `update_active_work`

```python
@_tool(power=True)
def update_active_work(directory: str, content: str) -> dict:
    """Replace this directory's `_active_work` memory atomically.

    Deletes any existing `_active_work` memory(ies) for the directory,
    then inserts a new one with the provided content. No char cap (this
    is in-flight scratch, expected to churn).

    Returns: {"replaced": bool, "previous_count": int, "memory_id": int}
    """
```

### Staleness check in stop hook

Hook injects:

```
- _active_work memory is {missing|stale_24h}.
  → Synthesize current state from session: branch, open PR (if any),
    next 1–2 steps from recent conversation. Call
    `update_active_work(directory="...", content="...")` with markdown.
  → Tool atomically replaces any prior _active_work for this directory.
```

---

## 4. Branch tagging schema migration (Mode A)

### Schema

```
memory.branch         : str | null   -- captured at write time; null for pre-v5.1 entries or non-git contexts
wiki_page.branch      : str | null   -- same
```

Migration: nullable column added; existing rows stay null. No backfill.

### Write path — auto-capture

`yadgar/server.py` helper:

```python
@lru_cache(maxsize=128)
def _detect_branch_cached(directory: str, _ts_bucket: int) -> str | None:
    """Cached per 30s. Returns None for detached HEAD or non-git."""
    try:
        out = subprocess.check_output(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
        return out if out and out != "HEAD" else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

def _detect_branch(directory: str) -> str | None:
    return _detect_branch_cached(directory, int(time.time() // 30))
```

Hook into `memorize`, `anchor`, `checkpoint`, `wiki_add` — one line each. `_project_init` and `_active_work` writes always set `branch = None` (project-level state, not branch-specific).

### Retrieval — Mode A

Default filter on `project_brief`, `recall`, `wiki_read`, `wiki_query`:

```
branch IN (current_branch, default_branch, NULL)
```

Where `default_branch` is auto-detected via `git symbolic-ref refs/remotes/origin/HEAD`. Feature-branch entries weighted 1.5× over default-branch entries in heat-blended ranking when both surface.

### `wiki_read(slug)` resolution

1. Try `WHERE slug = ? AND branch = current_branch` — if exists, return.
2. Else try `WHERE slug = ? AND branch IN (default_branch, NULL)` — return canonical/legacy.
3. Else None.

### Deferred to v5.2+

- Branch deletion cleanup
- Rebase invalidation
- Auto-promotion on merge
- Cross-branch dedup

---

## 5. `wiki_refresh_stale` — background regen dispatch

### New MCP tool

```python
@_tool(power=True)
def wiki_refresh_stale(
    directory: str,
    slugs: list[str] | None = None,
    force_branch: bool = False,
) -> dict:
    """Detect stale repo-wiki pages and regenerate them.

    Stale = `.local-review/wiki/*.md` frontmatter `hash` ≠ SHA256(source_file).
    Refuses on non-default-branch unless force_branch=True.

    Returns:
        {"detected": [...], "branch": "master", "skipped_reason": null}
    """
```

The tool **only detects + reports** — actual regen is done by spawning a background Agent that runs the `/repo-wiki update` skill. The skill auto-runs `export-yadgar` for changed pages, which queues `wiki_add` operations that the drainer applies to the `wiki_page` table.

### Regen chain (explicit — two wikis exist)

1. Stop-hook prompts session every 25 messages
2. Session calls `wiki_refresh_stale(directory)` → reports drift
3. Session dispatches background Agent: `Agent(subagent_type="general-purpose", run_in_background=True, prompt="run /repo-wiki update for these slugs: [...]")`
4. Agent runs the skill (cold-start downloads `repo-indexer` from Codeberg releases per SKILL.md, ~30s on first run; cached thereafter)
5. Skill regenerates `.local-review/wiki/*.md` (on-disk markdown)
6. Skill auto-runs `export-yadgar` → drops `wiki_add` operations in `~/.yadgar/queue/`
7. Yadgar queue drainer picks up → updates `wiki_page` table in SurrealDB

### Master-only enforcement

```python
branch = _detect_branch(directory)
default = _default_branch(directory)
if branch not in (default, "master", "main") and not force_branch:
    return {
        "detected": [],
        "branch": branch,
        "skipped_reason": "not_default_branch",
    }
```

Reasoning: regenerating on a feature branch creates branch-tagged wiki entries that no default-branch session ever sees, so the LLM cost is wasted.

---

## 6. Stop-hook expansion (dumb pipe)

`~/.claude/hooks/yadgar-stop-memory-checkpoint.py` fires every 25 messages (unchanged interval). Currently emits a static checkpoint prompt. Replace with:

```python
PROMPT = """Yadgar checkpoint. Evaluate signals and decide actions.

1. Call `project_brief(directory)` and check `signals`:
   - `stale_wiki_count > 0` AND branch is master/main/default → consider repo-wiki regen
   - `active_work_present == False` OR `active_work_age_hours > 24` → refresh _active_work
   - `init_memory == None` after >5 sessions in this dir → create one

2. If repo-wiki regen warranted, dispatch background Agent:
   Agent(
     subagent_type="general-purpose",
     run_in_background=True,
     description="repo-wiki regen on default branch",
     prompt="cd into the project, run /repo-wiki:repo-wiki update, "
            "verify export-yadgar fires, report regenerated slug list."
   )

3. If _active_work needs refresh, call update_active_work(directory, content).

4. If init_memory missing and you have enough session context, propose one
   and call bootstrap_project(directory, content) (≤2000 chars).

5. Otherwise: capture any key decisions via memorize/wiki_add.

Then look at your last message — if mid-thought, repeat the question so
conversation continues naturally.
"""
```

Hook is pure Python: prints this prompt, exits. No signal detection in the hook itself. No Anthropic API call from the hook. Session evaluates everything via tool calls.

### Compound trigger condition (lives in the prompt)

```
(branch in [default_branch, "master", "main"])
AND
(signals.stale_wiki_count > 0 OR session_edits_to_project_modules > 5)
```

Session computes `session_edits_to_project_modules` from action-batch log if needed.

---

## 7. SessionStart hook pipe integration

`yadgar-session-start-context.py` after v5.1:

```python
import json, urllib.request, os
DIR = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
url = f"http://127.0.0.1:8765/hooks/session-context?directory={DIR}&mode=catalog"
try:
    resp = json.loads(urllib.request.urlopen(url, timeout=2).read())
    print(resp.get("text", ""))   # the `_render` markdown
except Exception:
    pass
```

Server-side `/hooks/session-context` endpoint calls `project_brief(directory, mode=catalog)` and returns `{"text": resp["_render"]}`. Hook is a thin pipe; all curation lives server-side. PostCompact hook uses the same endpoint.

---

## 8. Mega-function decomposition (deferred from v5)

Before decomposing, write characterization test suite that snapshots current outputs:

- `yadgar/tests/test_retrieval_core_characterization.py` — pin behaviour of `retrieval/core.py:305` (cognitive complexity 330). Fixed-seed corpus of 200 memories, query set of 25 queries spanning fast/balanced/full profiles. Snapshot ranked output IDs + scores per query. Asserts no drift across the decomposition.
- `yadgar/tests/test_causal_discovery_characterization.py` — pin `causal_discovery.py:187` (complexity 146). Fixed-seed adjacency matrices through R1–R4 orientation rules. Snapshot orientation output.

Then decompose:
- `retrieval/core.py:305` → pipeline stages `candidate_fetch → score_fusion → rerank → filter → trim`. Each stage as a separate function with explicit input/output shapes.
- `causal_discovery.py:187` → R1, R2, R3, R4 each as own method on the discovery class.

Verification: characterization test suite must pass post-decomposition with **zero drift** in ranked-ID lists. Score floats compared with `math.isclose(rel_tol=1e-9)`.

---

## Files touched

| File | Change |
|------|--------|
| `yadgar/server.py` | Rename `get_project_context → project_brief`, layered output, two modes; `bootstrap_project`, `update_active_work`, `wiki_refresh_stale` MCP tools; deprecated alias for `get_project_context` |
| `yadgar/storage.py` | Add `branch` column to `memory` + `wiki_page` schemas (nullable); branch filter in retrieval queries; `search_memories_by_tag(tag, directory_context)` helper |
| `yadgar/seed.py` | Draft starter `_project_init` from README + top-level docs |
| `yadgar/config.py` | New: `PROJECT_INIT_CAP_CHARS: int = 2000`, `BRIEF_MODE_DEFAULT: str = "catalog"`, `WIKI_STALE_GRACE_DAYS: int = 7` |
| `yadgar/hooks/yadgar-stop-memory-checkpoint.py` | Expand to inject signal-evaluation prompt (no Python detection, no API call) |
| `yadgar/hooks/yadgar-session-start-context.py` | Pipe `project_brief._render` markdown |
| `yadgar/server.py` | New endpoint `/hooks/session-context` |
| `yadgar/retrieval/core.py` | Decompose complexity-330 function into pipeline stages |
| `yadgar/causal_discovery.py` | Decompose complexity-146 function into R1–R4 methods |
| `yadgar/tests/test_project_brief.py` (new) | Catalog vs full mode; walk-up resolution; init_memory inlining; signals computation |
| `yadgar/tests/test_bootstrap_project.py` (new) | 2000-char cap enforcement; atomic replace; reject overflow |
| `yadgar/tests/test_update_active_work.py` (new) | Atomic replace; previous_count returned |
| `yadgar/tests/test_branch_tagging.py` (new) | Auto-capture on memorize/anchor/checkpoint/wiki_add; retrieval filter; wiki_read resolution order |
| `yadgar/tests/test_wiki_refresh_stale.py` (new) | Master-only enforcement; force_branch override; hash drift detection |
| `yadgar/tests/test_retrieval_core_characterization.py` (new) | Pin retrieval/core.py:305 behaviour pre-decomposition |
| `yadgar/tests/test_causal_discovery_characterization.py` (new) | Pin causal_discovery.py:187 behaviour pre-decomposition |
| `yadgar/tests/test_stop_hook_prompt.py` (new) | Hook emits the expected prompt; no other side effects |
| `pyproject.toml` | Bump `5.0.0 → 5.1.0` |
| `docs/configuration.md` | Document `PROJECT_INIT_CAP_CHARS`, `BRIEF_MODE_DEFAULT`, `WIKI_STALE_GRACE_DAYS`, `project_brief` two-mode response |
| `MIGRATION_NOTES.md` | Steps to seed `_project_init` for the 6 initial projects |

---

## Execution order (within v5.1)

1. **Characterization tests** for retrieval/core + causal_discovery. Must be green before any decomposition. Independent commit.
2. **Schema migration** — add `branch` column to memory + wiki_page. Nullable, no backfill. Independent commit; no behaviour change yet.
3. **`project_brief` + `_project_init` + `bootstrap_project` + `update_active_work`**. New MCP surface. Old `get_project_context` retained as deprecated alias.
4. **`wiki_refresh_stale` + stop-hook expansion + SessionStart pipe**. Hook side.
5. **Branch-tag auto-capture in write paths**. Wire into memorize/anchor/checkpoint/wiki_add. Retrieval filter active.
6. **Mega-function decomposition**. Characterization tests gate the commit.
7. **Seed initial `_project_init` memories** for yadgar / ccpm / nix / meridian / qwfm / cloudfront-authorization-at-edge. Documented in `MIGRATION_NOTES.md`; user runs manually.

CI must be green after each commit. No commit depends on a later one.

---

## Verification

1. **Walk-up resolution.** `project_brief("/home/max/git/yadgar/yadgar/tests")` returns `_resolved_directory = "/home/max/git/yadgar"`.
2. **Mode split.** Catalog response ≤ 700 tokens; full response ≤ 1200 tokens (measured via tiktoken).
3. **Cap enforcement.** `bootstrap_project(directory, content="x"*2001)` raises `ValueError`.
4. **Atomic replace.** Call `bootstrap_project` then `bootstrap_project` again with different content; assert exactly one `_project_init` memory exists for the directory after.
5. **Deprecated alias.** `get_project_context(directory)` still works, returns the same shape as `project_brief(directory, mode="catalog")`, emits a `DeprecationWarning`.
6. **Branch tag auto-capture.** `memorize(content="x", context="/some/git/repo/dir", tags=[])` writes a row with `branch = <current branch>`.
7. **Retrieval filter.** From feature branch, recall returns feature-branch entries + default-branch entries; from default branch, only default + null surface.
8. **Wiki resolution order.** Branch-specific page preferred over default-branch page over null-branch page when slug collides.
9. **`wiki_refresh_stale` master gate.** From feature branch returns `skipped_reason="not_default_branch"`. With `force_branch=True`, proceeds.
10. **Hook prompt emission.** `yadgar-stop-memory-checkpoint.py` invoked → stdout matches expected fixture; exit code 0; no network calls.
11. **Characterization parity.** Pre-decomp snapshot vs post-decomp run shows identical ranked-ID lists and scores (`math.isclose(rel_tol=1e-9)`).
12. **End-to-end repo-wiki regen.** Edit a yadgar source file, trigger stop hook, watch dispatched Agent regen wiki, watch queue drain into `wiki_page` table. Assert frontmatter hash updated.

---

## Out of scope (v5.2 or later)

- Branch deletion cleanup (memories with deleted branches stay until heat decay).
- Rebase invalidation (heat decay + manual `forget()` handles edge cases).
- Auto-promotion on merge (branch-tagged entries don't auto-rewrite when feature branch merges).
- Cross-branch dedup (same memory on two branches creates two rows; acceptable).
- Auto-tagging strategy for `_session_context` (retired — replaced by `_project_init` table-of-contents pattern; no auto-tag needed).
- LLM-driven semantic comparison from inside the stop hook (cost + risk).
- Auto-regen on every save (file-watcher reactive regen). Stick with stop-hook cadence.
- Per-language project-snapshot integration (currently only git is generic; package-manifest detection is broad).
- Web UI for editing `_project_init` / `_active_work` memories.
- Cross-project session context (today: one directory at a time).
- Action-stream → narrative compression in the checkpoint hook (already happens during sleep cycles).
- `BRIEF_NUDGES_ENABLED` first-session nudges from the original v4.5 plan — defer until needed.
