"""Shared memory-block markdown renderer (v5.35.1 — DRY extract).

Previously inlined in yadgar.restoration.RestorationEngine._render_blocks_section.
Extracted so hook_runner.py (block-reflect) and http.py (session-context, block-reflect
endpoint) can render blocks without importing the full restoration module.

Public API:
    render_blocks_section(blocks, directory) -> str
"""

from __future__ import annotations


def render_blocks_section(blocks: list[dict], directory: str) -> str:
    """Render memory blocks as markdown for hook injection.

    Returns "" when blocks is empty — safe to call unconditionally.

    Args:
        blocks: List of block dicts with keys: scope, name, content.
        directory: Project directory string (used in the Project-blocks header).
    """
    if not blocks:
        return ""

    lines: list[str] = [
        "## Memory Blocks (always-injected, editable via block_* MCP tools)",
        "",
    ]
    global_blocks = [b for b in blocks if b.get("scope") == "global"]
    project_blocks = [b for b in blocks if b.get("scope") == "project"]

    if global_blocks:
        lines.append("### Global blocks")
        for b in global_blocks:
            content = b.get("content", "")
            name = b.get("name", "")
            lines.append(f"- `{name}`: {content}" if content else f"- `{name}`: *(empty)*")
        lines.append("")

    if project_blocks:
        dir_label = directory or "project"
        lines.append(f"### Project blocks ({dir_label})")
        for b in project_blocks:
            content = b.get("content", "")
            name = b.get("name", "")
            lines.append(f"- `{name}`: {content}" if content else f"- `{name}`: *(empty)*")
        lines.append("")

    return "\n".join(lines)
