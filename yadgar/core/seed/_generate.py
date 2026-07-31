"""Seed phase 3 — memory generation and storage.

Converts scan data into memory entries and persists them,
replacing any prior seed memories for the same directory.
"""

import logging
import os

from ._analysis import (
    _detect_stack,
    _find_subproject_boundaries,
    _summarize_config,
    _summarize_structure,
)
from ._scan import _truncate

logger = logging.getLogger(__name__)

# Heat values by memory type
_HEAT_BY_TYPE = {
    "overview": 0.9,
    "documentation": 0.85,
    "config": 0.7,
    "ci_cd": 0.6,
    "entry_point": 0.75,
    "component": 0.5,
}

_PROJECT_INIT_CAP = 2000  # Must match config.PROJECT_INIT_CAP_CHARS


# ---------------------------------------------------------------------------
# Section helpers — each builds one logical group of memories
# ---------------------------------------------------------------------------


def _generate_overview_memory(scan_data: dict, directory: str) -> dict:
    """Build the project overview memory (section 1)."""
    project_name = scan_data["project_name"]
    stats = scan_data["stats"]
    structure = scan_data["structure"]

    stack = _detect_stack(scan_data["configs"], stats)
    structure_summary = _summarize_structure(structure)
    overview = (
        f"Project: {project_name}\n"
        f"Stack: {stack}\n"
        f"Files: {stats['total_files']}, Directories: {stats['total_dirs']}\n"
        f"Top extensions: {', '.join(f'{ext}({n})' for ext, n in stats['top_extensions'][:8])}\n\n"
        f"Structure:\n{structure_summary}"
    )
    return {
        "content": _truncate(overview),
        "context": directory,
        "tags": ["_seed", "overview", "structure"],
        "heat_type": "overview",
    }


def _generate_config_memories(scan_data: dict, directory: str) -> list[dict]:
    """Build config file memories (section 2)."""
    memories = []
    for config in scan_data["configs"]:
        summary = _summarize_config(config)
        content = f"Config: {config['path']}\nLanguage: {config['language']}\n\n{summary}"
        memories.append(
            {
                "content": _truncate(content),
                "context": directory,
                "tags": ["_seed", "config", config["language"]],
                "heat_type": "config",
            }
        )
    return memories


def _generate_doc_memories(scan_data: dict, directory: str) -> list[dict]:
    """Build documentation memories (section 3)."""
    memories = []
    for doc in scan_data["docs"]:
        doc_dir = os.path.dirname(doc["path"])
        extra_tags = [doc_dir] if doc_dir else []
        content = f"Documentation: {doc['path']}\n\n{doc['content']}"
        memories.append(
            {
                "content": _truncate(content),
                "context": directory,
                "tags": ["_seed", "documentation"] + extra_tags,
                "heat_type": "documentation",
            }
        )
    return memories


def _generate_ci_memory(scan_data: dict, directory: str) -> list[dict]:
    """Build CI/CD memory (section 4). Returns empty list if no ci_cd data."""
    if not scan_data["ci_cd"]:
        return []
    project_name = scan_data["project_name"]
    ci_parts = []
    for ci in scan_data["ci_cd"]:
        ci_parts.append(f"--- {ci['path']} ---\n{_truncate(ci['content'], 600)}")
    ci_content = f"CI/CD configuration for {project_name}:\n\n" + "\n\n".join(ci_parts)
    return [
        {
            "content": _truncate(ci_content),
            "context": directory,
            "tags": ["_seed", "ci_cd", "devops"],
            "heat_type": "ci_cd",
        }
    ]


def _generate_entry_point_memories(scan_data: dict, directory: str) -> list[dict]:
    """Build entry point memories (section 5)."""
    memories = []
    for ep in scan_data["entry_points"]:
        content = f"Entry point: {ep['path']}\n\n{_truncate(ep['content'], 1500)}"
        memories.append(
            {
                "content": _truncate(content),
                "context": directory,
                "tags": ["_seed", "entry_point"],
                "heat_type": "entry_point",
            }
        )
    return memories


# ---------------------------------------------------------------------------
# Component memory helpers (section 6) — split for complexity
# ---------------------------------------------------------------------------


def _compute_boundaries(structure: dict, configs: list) -> list[str]:
    """Compute component boundaries; fallback to top-level dirs."""
    boundaries = _find_subproject_boundaries(structure, configs)
    if not boundaries:
        boundaries = sorted(d for d in structure if d != "." and "/" not in d and os.sep not in d)
    return boundaries


def _collect_component_files(d: str, structure: dict) -> list[str]:
    """Gather all relative file paths under component directory d."""
    sub_files = []
    for key, files in structure.items():
        if key == d or key.startswith(d + "/") or key.startswith(d + os.sep):
            for f in files:
                sub_files.append(os.path.join(key, f))
    return sub_files


def _build_component_content(
    d: str,
    sub_files: list[str],
    structure: dict,
    docs: list[dict],
) -> str:
    """Build the text content for a component memory entry."""
    # Extension frequency map
    exts: dict[str, int] = {}
    for f in sub_files:
        ext = os.path.splitext(f)[1].lower()
        if ext:
            exts[ext] = exts.get(ext, 0) + 1

    ext_summary = ", ".join(
        f"{ext}({n})" for ext, n in sorted(exts.items(), key=lambda x: -x[1])[:5]
    )

    # Subdirectory names relative to d
    subdirs = sorted(
        set(
            key
            for key in structure
            if (key.startswith(d + "/") or key.startswith(d + os.sep)) and key != d
        )
    )
    subdir_names = [os.path.relpath(sd, d) for sd in subdirs[:15]]

    # Check for own README
    component_readme = None
    for doc in docs:
        doc_dir = os.path.dirname(doc["path"])
        if doc_dir == d:
            component_readme = os.path.basename(doc["path"])
            break

    content = f"Component: {d}/\n"
    content += f"Files: {len(sub_files)} ({ext_summary})\n"
    if subdir_names:
        content += f"Subdirectories: {', '.join(subdir_names)}\n"
    if component_readme:
        content += f"Has own documentation: {component_readme}\n"
    if len(sub_files) <= 20:
        content += f"All files: {', '.join(os.path.basename(f) for f in sub_files)}\n"
    else:
        content += (
            f"Sample files: {', '.join(os.path.basename(f) for f in sub_files[:15])},"
            f" ... (+{len(sub_files) - 15} more)\n"
        )
    return content


def _generate_component_memories(scan_data: dict, directory: str) -> list[dict]:
    """Build per-component memories (section 6)."""
    structure = scan_data["structure"]
    boundaries = _compute_boundaries(structure, scan_data["configs"])
    memories = []
    for d in boundaries:
        sub_files = _collect_component_files(d, structure)
        if not sub_files:
            continue
        content = _build_component_content(d, sub_files, structure, scan_data["docs"])
        memories.append(
            {
                "content": _truncate(content),
                "context": directory,
                "tags": ["_seed", "component", d],
                "heat_type": "component",
            }
        )
    return memories


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_memories(scan_data: dict) -> list[dict]:
    """Generate memory entries from scan data.

    Returns a list of dicts with keys: content, context, tags, heat_type
    Each represents one memory to store.
    """
    directory = scan_data["root"]
    memories: list[dict] = []
    memories.append(_generate_overview_memory(scan_data, directory))
    memories.extend(_generate_config_memories(scan_data, directory))
    memories.extend(_generate_doc_memories(scan_data, directory))
    memories.extend(_generate_ci_memory(scan_data, directory))
    memories.extend(_generate_entry_point_memories(scan_data, directory))
    memories.extend(_generate_component_memories(scan_data, directory))
    return memories


def _draft_project_init(scan_data: dict) -> str:
    """Compose a starter _project_init markdown skeleton from scan data.

    Content is capped at _PROJECT_INIT_CAP chars (same as server-side cap).
    """
    project_name = scan_data.get("project_name", "")
    root = scan_data.get("root", "")
    configs = scan_data.get("configs", [])
    docs = scan_data.get("docs", [])
    stats = scan_data.get("stats", {})

    # --- stack detection ---
    stack = _detect_stack(configs, stats)

    # --- readme snippet ---
    readme_snippet = ""
    for doc in docs:
        name = doc.get("name", "").lower()
        if "readme" in name:
            snippet = doc.get("content", "")[:300]
            if snippet:
                readme_snippet = f"\n## README snippet\n{snippet}\n"
            break

    # --- top-level doc list ---
    doc_names = [d.get("name", "") for d in docs if d.get("name")]
    doc_list = ", ".join(doc_names[:10]) if doc_names else "(none)"

    lines = [
        f"# {project_name} — Project Init",
        "",
        f"**Root:** `{root}`",
        f"**Stack:** {stack}",
        "",
        "## Key wiki pages",
        "(populate after seeding wiki pages)",
        "",
        "## Key memory IDs",
        "(populate after anchoring key decisions)",
        "",
        "## Conventions",
        "- (add project conventions here)",
        "",
        "## Top-level docs",
        f"{doc_list}",
        readme_snippet.strip(),
        "",
        "## Lookup tips",
        "- Use recall('_anchor') for key decisions",
        "- Use wiki_query() for architecture docs",
    ]
    content = "\n".join(lines)
    return content[:_PROJECT_INIT_CAP]


def seed_project(
    directory: str,
    dry_run: bool = False,
) -> dict:
    """Scan a project and store foundational memories.

    T2 Car E1 (census verdict #9, ADR-0078): only the host-FS half runs here —
    ``scan_project`` + ``generate_memories`` + the ``_project_init`` draft. The
    store phase (embedding, thermo scoring, insert/update, old-seed deletion,
    init upsert) forwards to the backend ``seed_store`` /admin op, which owns
    the ML engines and the DB.

    Args:
        directory: Project root directory to scan.
        dry_run: If True, scan and generate but don't store.

    Returns:
        Dict with scan stats and memories created/replaced.
    """
    from ._scan import scan_project

    # Scan (host FS — stays core)
    scan_data = scan_project(directory)
    memories = generate_memories(scan_data)

    if dry_run:
        return {
            "project": scan_data["project_name"],
            "directory": scan_data["root"],
            "stats": scan_data["stats"],
            "memories_generated": len(memories),
            "memories": [{"content": m["content"][:200], "tags": m["tags"]} for m in memories],
            "stored": False,
        }

    # §23: Draft a starter _project_init memory from README + top-level docs
    # (reads scan_data — host side), then forward the WHOLE store phase.
    init_content = _draft_project_init(scan_data)

    payload = {
        "root": scan_data["root"],
        "memories": [
            {
                "content": m["content"],
                "context": m["context"],
                "tags": m["tags"],
                "base_heat": _HEAT_BY_TYPE.get(m.get("heat_type", "component"), 0.6),
            }
            for m in memories
        ],
        "init_content": init_content,
    }

    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

    # Generous timeout: the backend embeds + scores every generated memory.
    result = _forward_admin("seed_store", payload, timeout_s=300.0)

    return {
        "project": scan_data["project_name"],
        "directory": scan_data["root"],
        "stats": scan_data["stats"],
        "memories_generated": len(memories),
        "created": int(result.get("created", 0)),
        "replaced": int(result.get("replaced", 0)),
        "stored": True,
    }
