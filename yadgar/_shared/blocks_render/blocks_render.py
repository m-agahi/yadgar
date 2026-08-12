"""Shared memory-block markdown renderer (v5.35.1 — DRY extract).

Previously inlined in yadgar.restoration.RestorationEngine._render_blocks_section.
Extracted so hook_runner.py (block-reflect) and http.py (session-context, block-reflect
endpoint) can render blocks without importing the full restoration module.

Public API:
    render_blocks_section(blocks, project_id) -> str
"""

from __future__ import annotations


def render_blocks_section(blocks: list[dict], project_id: str) -> str:
    """Render memory blocks as markdown for hook injection.

    Returns "" when blocks is empty — safe to call unconditionally.

    C9a (0047 §5): ``directory`` renamed to ``project_id`` per ADR-0225. The
    value is presentation-only — it labels the Project-blocks header and is
    never used to select or scope anything.

    Args:
        blocks: List of block dicts with keys: scope, name, content.
        project_id: Project identity string (used in the Project-blocks header).
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
        project_label = project_id or "project"
        lines.append(f"### Project blocks ({project_label})")
        for b in project_blocks:
            content = b.get("content", "")
            name = b.get("name", "")
            lines.append(f"- `{name}`: {content}" if content else f"- `{name}`: *(empty)*")
        lines.append("")

    return "\n".join(lines)
