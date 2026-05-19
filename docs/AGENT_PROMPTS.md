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

To get the latest prompt for a pattern:

```python
# Via MCP tool
result = agent_prompt_get("dispatch-fix-bug")
# Returns: {"version": 3, "slug": "agent-prompt-dispatch-fix-bug-v3", "content": "...", ...}
```

Internally this queries:
```
SELECT * FROM wiki_page
WHERE tags CONTAINS "task:<pattern>" AND tags CONTAINS "agent-prompt"
```
then sorts by the `vN` suffix of the slug descending and returns the first result.

To save a new version:

```python
result = agent_prompt_save("dispatch-fix-bug", "Updated prompt text...")
# Returns: {"saved": True, "version": 4, "slug": "agent-prompt-dispatch-fix-bug-v4"}
```

## MCP tools

| Tool | Description |
|---|---|
| `agent_prompt_get(pattern)` | Returns latest version dict, or None if absent |
| `agent_prompt_save(pattern, content)` | Creates vN+1, returns saved=True + version + slug |

## Using prompts at dispatch time (v5.3.4 M2 preview)

The planned `dispatch` helper (v5.3.4) will:
1. Call `agent_prompt_get(<task-pattern>)` to get the latest prompt.
2. Inject the prompt + yadgar protocol into the subagent's instructions.
3. After the subagent returns, process its `## Yadgar findings` section.

Until v5.3.4, call `agent_prompt_get` manually in your dispatch prompt:

```python
prompt_page = agent_prompt_get("dispatch-fix-bug")
prompt_text = prompt_page["content"] if prompt_page else DEFAULT_PROMPT

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
