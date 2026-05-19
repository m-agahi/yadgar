"""Subagent dispatch helper — v5.3.6 M2.

Provides agent_dispatch_prelude() which returns a markdown-formatted prelude
for the orchestrator to prepend to a subagent's prompt.

The prelude includes:
  1. Yadgar protocol contract (read-first, report findings, don't memorize).
  2. Latest agent_prompt for the given pattern (if one exists in the wiki).
  3. A recall hint for the task_topic so the subagent knows what to recall.

Usage:
    prelude = agent_dispatch_prelude("dispatch-fix-bug", "vacuum regression")
    agent_prompt = prelude + "\\n\\n" + actual_task_description
"""

from __future__ import annotations

import logging

from yadgar.server._app import _tool

logger = logging.getLogger(__name__)

# Fixed-cost contract section (always present, ~400 chars)
_YADGAR_CONTRACT = """\
## Yadgar subagent contract

Before substantive work:
1. `recall("…relevant topic…")` — surface hot memories, anchors, prior findings.
2. Report findings in a `## Yadgar findings` section using bullet points.
3. Do NOT call `memorize()` or write to memory — the orchestrator does this.
4. Observed state always wins over recalled state (update if contradicted).
""".strip()

# Budget: contract + recall hint ≈ 600 chars. Leave ~1 400 chars for prompt body.
_AGENT_PROMPT_BUDGET = 1_400
_TOTAL_BUDGET = 2_000


@_tool()
def agent_dispatch_prelude(
    pattern: str,
    task_topic: str,
    storage=None,
) -> str:
    """Return a markdown prelude to prepend to a subagent prompt.

    The prelude contains the Yadgar protocol contract, the latest stored
    agent-prompt for *pattern* (if any), and a recall hint for *task_topic*.
    Total length is capped at 2 000 characters (orchestrator context budget).

    Args:
        pattern:    Task pattern identifier used to look up agent_prompt
                    (e.g. "dispatch-fix-bug"). Passes directly to
                    agent_prompt_get(). Pass "" to skip prompt lookup.
        task_topic: Short description of the task — injected as a recall hint
                    so the subagent knows which yadgar memories to surface.
        storage:    StorageEngine instance (injected for testing; otherwise
                    resolved from server lifecycle).

    Returns:
        Markdown string ≤ 2 000 characters.
    """
    if storage is None:
        from yadgar.server.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()

    sections: list[str] = [_YADGAR_CONTRACT]

    # Optional: latest agent_prompt for pattern
    if pattern:
        try:
            from yadgar.server.tools.agent_prompts import agent_prompt_get  # noqa: PLC0415

            prompt_result = agent_prompt_get(pattern, storage=storage)
            if prompt_result and prompt_result.get("content"):
                raw_content = prompt_result["content"]
                version = prompt_result.get("version", "?")
                # Truncate if needed to respect overall budget
                used = sum(len(s) for s in sections) + 100  # 100 for separators
                available = _AGENT_PROMPT_BUDGET - used
                if available > 80:
                    snippet = raw_content[:available]
                    sections.append(f"## Agent-prompt [{pattern} v{version}]\n\n{snippet}")
        except Exception as _e:
            logger.debug("agent_dispatch_prelude: agent_prompt_get failed: %s", _e)

    # Recall hint
    if task_topic:
        sections.append(f'## Recall hint\n\nSuggest: `recall("{task_topic}")` before starting.')

    prelude = "\n\n".join(sections)

    # Hard cap at _TOTAL_BUDGET
    if len(prelude) > _TOTAL_BUDGET:
        prelude = prelude[: _TOTAL_BUDGET - 3] + "..."

    return prelude
