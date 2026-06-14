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


def _delete_existing_seed_memories(
    storage, directory: str, exclude_ids: list[int] | None = None
) -> int:
    """Delete existing _seed tagged memories for this directory before re-seeding.

    §6 Q17: exclude_ids lets callers preserve newly-inserted memories so the
    delete step only removes OLD seed memories, not the fresh ones.

    Returns count of deleted memories.
    """
    rows = storage._q(
        "SELECT id FROM memory WHERE directory_context = $dir AND '_seed' IN tags",
        {"dir": directory},
    )
    if not rows:
        return 0

    exclude_set: set[int] = set(exclude_ids or [])
    ids = [
        storage._extract_id(r.get("id"))
        for r in rows
        if storage._extract_id(r.get("id")) not in exclude_set
    ]
    for mid in ids:
        # Delete SR transitions referencing this memory
        storage._q(
            "DELETE memory_transition WHERE from_memory_id = $id OR to_memory_id = $id",
            {"id": mid},
        )
        # Delete the memory itself (embedding fields are on the record — no separate table)
        storage._q("DELETE type::record('memory', $id)", {"id": mid})

    return len(ids)


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
    db_path: str | None = None,
    dry_run: bool = False,
    storage=None,
    embeddings=None,
    thermo=None,
    curator=None,
) -> dict:
    """Scan a project and store foundational memories.

    Args:
        directory: Project root directory to scan.
        db_path: Optional SQLite database path override.
        dry_run: If True, scan and generate but don't store.
        storage: Optional pre-initialized StorageEngine (to reuse server's).
        embeddings: Optional pre-initialized EmbeddingEngine.
        thermo: Optional pre-initialized MemoryThermodynamics.
        curator: Optional pre-initialized MemoryCurator.

    Returns:
        Dict with scan stats and memories created/skipped.
    """
    from ._scan import scan_project

    # Scan
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

    # Use provided engines or initialize our own
    own_storage = storage is None
    if own_storage:
        from yadgar.config import Settings
        from yadgar.curation import MemoryCurator
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.knowledge_graph import KnowledgeGraph
        from yadgar.storage import StorageEngine
        from yadgar.thermodynamics import MemoryThermodynamics

        settings = Settings()
        storage = StorageEngine(db_path or settings.DB_PATH)
        embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)
        KnowledgeGraph(storage, settings)
        thermo = MemoryThermodynamics(storage, embeddings, settings)
        MemoryCurator(storage, embeddings, thermo, settings)

    created = 0
    replaced = 0

    try:
        # §6 Q17: Build new memories FIRST; delete old ones only after successful insert.
        # Old code deleted first → a crash mid-insert left the DB with no seed memories.
        new_memory_ids: list[int] = []

        for mem in memories:
            content = mem["content"]
            context = mem["context"]
            tags = mem["tags"]
            heat_type = mem.get("heat_type", "component")

            # Generate embedding
            embedding = embeddings.encode(content)

            # Base heat from memory type
            base_heat = _HEAT_BY_TYPE.get(heat_type, 0.6)

            # Compute thermodynamic scores
            surprise = thermo.compute_surprise(content, context)
            importance = thermo.compute_importance(content, tags)
            valence = thermo.compute_valence(content)
            # Use modest surprise boost so seeded memories don't all max out
            initial_heat = min(base_heat + surprise * 0.1, 1.0)

            # Insert directly (no curator dedup since we will clear old seeds after)
            memory_id = storage.insert_memory(
                {
                    "content": content,
                    "embedding": embedding,
                    "tags": tags,
                    "directory_context": context,
                    "heat": initial_heat,
                    "is_stale": False,
                    "file_hash": None,
                    "embedding_model": embeddings.get_model_name(),
                }
            )
            new_memory_ids.append(memory_id)

            # Set thermodynamic scores
            storage.update_memory_scores(
                memory_id,
                surprise_score=surprise,
                importance=importance,
                emotional_valence=valence,
            )

            created += 1
            logger.info("Seed memory [created]: %s", content[:80])

        # All new memories inserted successfully — now safe to delete old seed memories.
        deleted = _delete_existing_seed_memories(
            storage, scan_data["root"], exclude_ids=new_memory_ids
        )
        if deleted:
            logger.info("Cleared %d old seed memories for %s", deleted, scan_data["root"])
            replaced = deleted

        # §23: Draft a starter _project_init memory from README + top-level docs.
        init_content = _draft_project_init(scan_data)
        try:
            storage.upsert_project_init(scan_data["root"], init_content)
            logger.info("Drafted _project_init for %s", scan_data["root"])
        except Exception:
            logger.warning("Failed to draft _project_init for %s", scan_data["root"], exc_info=True)

    finally:
        if own_storage:
            storage.close()

    return {
        "project": scan_data["project_name"],
        "directory": scan_data["root"],
        "stats": scan_data["stats"],
        "memories_generated": len(memories),
        "created": created,
        "replaced": replaced,
        "stored": True,
    }
