# Agent Prompt Versioning — Yadgar v5.3.0 A4

Agent prompts are versioned wiki pages that capture the best-known
prompt for a recurring agent task. They are stored in the wiki under
a slug convention and retrieved by task pattern at dispatch time.

## Slug convention

```
agent-prompt-<task-pattern>-v<N>
```

Examples:
- `agent-prompt-dispatch-fix-bug-v1`
- `agent-prompt-dispatch-research-v3`
- `agent-prompt-cavecrew-investigator-v2`

`task-pattern` uses hyphens to separate words. It should be short, descriptive,
and stable across versions (only the `vN` suffix changes).

## Tags

Every agent-prompt page is tagged:
```
["agent-prompt", "task:<pattern>", "global"]
```

The `agent-prompt` tag enables broad enumeration; `task:<pattern>` enables
pattern-specific lookup. `global` is the Car C7 reach tag: without it a page is
readable only from the project that saved it. `agent_prompt_save` and
`discipline_save` attach it themselves (core 5.194.5); before that only
`WikiStore.add`'s storage-scope rule did, and the backend op's no-wiki fallback
wrote the payload tags verbatim. `scripts/heal_global_reach_scope_drift.py`
repairs pages saved without it.

## Category

Always `"reference"` — agent prompts are stable reference material, not
evolving decisions or analysis.

## Version semantics

- Versions are immutable once written. Never update an existing page.
- Each call to `agent_prompt_save(pattern, content)` creates a new page at `vN+1`.
- The previous version remains in the wiki for rollback and diffing.

## Retrieval

> **v5.85 (ADR-0007):** the bespoke `agent_prompt_get` / `agent_prompt_search`
> MCP tools were removed. One wiki page per pattern at the deterministic slug
> `agent-prompt-<pattern>` (no `-vN` suffix — wiki versioning carries history).

Semantic lookup of saved prompts is the unified recall path:

```python
# Via MCP tool — SQL pre-filter over agent-prompt pages (dilution-safe)
results = recall("audit this PR for vulns", type="wiki", tags=["agent-prompt"], project="<owner/repo>")
# Each result carries the "agent-prompt" tag. General recall (no tags) EXCLUDES them.
```

Exact-key lookup (used internally by `agent_dispatch_prelude`) reads the
deterministic slug directly via the internal `_read_agent_prompt(slug)` helper —
not an MCP tool, not semantic recall.

To save a prompt (upserts one page per pattern; second save bumps the wiki version):

```python
result = agent_prompt_save("dispatch-fix-bug", "Updated prompt text...", project="<owner/repo>")
# Returns: {"saved": True, "version": 2, "slug": "agent-prompt-dispatch-fix-bug", "page_id": ...}
```

## MCP tools

| Tool | Description |
|---|---|
| `agent_prompt_save(pattern, content, project)` | Upserts one page per pattern; returns saved=True + version + slug |
| `agent_prompt_get(pattern)` | Exact-key read: the `agent_pattern` ledger row plus its wiki body (0047 Car I) |
| `agent_prompt_list(status=None, limit=20)` | Discovery listing from the `agent_pattern` ledger table, ordered `uses` DESC (0047 Car I; the `agent-prompt-toc` wiki page is retired) |
| `recall(query, type="wiki", tags=["agent-prompt"], project)` | Semantic lookup of agent-prompt pages (replaces the removed `agent_prompt_search`) |

`project` is the scope key on every call above. Since C5 / ADR-0227 an identity
is never derived from a directory, so `directory="global"` — which earlier
revisions of this page used — no longer names anything: global REACH now travels
as a tag on the page, never as a project identity.

## Using prompts at dispatch time

`agent_dispatch_prelude(pattern, task_topic)` resolves the prompt for a pattern
via the internal deterministic slug-read (`agent-prompt-<pattern>`), injects it
plus the yadgar protocol into the subagent's instructions, then the orchestrator
processes the subagent's `## Yadgar findings` section on return.

Budgets (`dispatch_helper.py`): the pattern snippet has its own 1 300-char
allowance; the whole prelude is capped at 6 000 chars (8 500 with
`include_context=True`). Before core 5.194.5 the snippet was the allowance MINUS
the contract's length, so a contract longer than ~1.3 KB left no pattern body in
the prelude at all. A contract larger than the packaged one now drops composed
disciplines last-listed-first (with a warning); the contract and the pattern
snippet always survive.

## Ledger rows and seeding

The `agent_pattern` / `agent_discipline` ledger rows are the discovery surface
(`agent_prompt_list` / `agent_prompt_get`); the wiki page is the body. Every
save writes the page first, then the row.

`seed_agent_prompts(project=...)` is create-if-absent per page, and since core
5.194.5 it also heals the rows of the slugs the package owns (starters, the
contract, the disciplines): a packaged page with no row gets one, pinned to the
LIVE page bytes. `baseline_hash` (ADR-0217/0218, the ancestor of the three-way
upgrade merge) is the hash of the packaged content, recorded only when the live
page still equals it; the result lists the other names under
`ledger_baseline_unknown`. An ordinary save never moves `baseline_hash` — the
upsert keeps the stored value when the caller sends none (backend 5.87.6).

The contract's row lives in `agent_pattern` under `name="contract"`, even though
its page type is `agent_discipline`, because that is where `agent_prompt_save`
writes it.

Pages the package does not own get their rows through the human-run procedure
`docs/prompts/backfill-agent-pattern-ledger.md` (ADR-0005), not automatically.

To pull a prompt manually in your own dispatch prompt, use semantic recall:

```python
hits = recall("fix the bug", type="wiki", tags=["agent-prompt"], project="<owner/repo>")
prompt_text = hits[0]["content"] if hits else DEFAULT_PROMPT

Agent(
    subagent_type="general-purpose",
    prompt=f"{prompt_text}\n\nTask: {task_description}",
    ...
)
```

## Browsing all prompts

```python
# List all agent-prompt wiki pages
results = wiki_query("agent dispatch prompt", tags=["agent-prompt"], category="reference")
```
