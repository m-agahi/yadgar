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
["agent-prompt", "task:<pattern>"]
```

The `agent-prompt` tag enables broad enumeration; `task:<pattern>` enables
pattern-specific lookup.

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
results = recall("audit this PR for vulns", type="wiki", tags=["agent-prompt"], directory="global")
# Each result carries the "agent-prompt" tag. General recall (no tags) EXCLUDES them.
```

Exact-key lookup (used internally by `agent_dispatch_prelude`) reads the
deterministic slug directly via the internal `_read_agent_prompt(slug)` helper —
not an MCP tool, not semantic recall.

To save a prompt (upserts one page per pattern; second save bumps the wiki version):

```python
result = agent_prompt_save("dispatch-fix-bug", "Updated prompt text...", directory="global")
# Returns: {"saved": True, "version": 2, "slug": "agent-prompt-dispatch-fix-bug", "page_id": ...}
```

## MCP tools

| Tool | Description |
|---|---|
| `agent_prompt_save(pattern, content, directory)` | Upserts one page per pattern; returns saved=True + version + slug |
| `recall(query, type="wiki", tags=["agent-prompt"], directory)` | Semantic lookup of agent-prompt pages (replaces the removed `agent_prompt_search`) |

## Using prompts at dispatch time

`agent_dispatch_prelude(pattern, task_topic)` resolves the prompt for a pattern
via the internal deterministic slug-read (`agent-prompt-<pattern>`), injects it
plus the yadgar protocol into the subagent's instructions, then the orchestrator
processes the subagent's `## Yadgar findings` section on return.

To pull a prompt manually in your own dispatch prompt, use semantic recall:

```python
hits = recall("fix the bug", type="wiki", tags=["agent-prompt"], directory="global")
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
