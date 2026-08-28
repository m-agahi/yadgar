"""Seed phase 2 — config summarisation and structure analysis.

Converts raw file contents into concise summaries suitable for
storing as memories.
"""

import json
import os
import tomllib

from ._scan import _truncate


def _detect_stack(configs: list[dict], stats: dict) -> str:
    """Detect the primary tech stack from configs and file stats."""
    languages = set()
    for cfg in configs:
        languages.add(cfg["language"])

    ext_map = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".rs": "Rust",
        ".go": "Go",
        ".java": "Java",
        ".kt": "Kotlin",
        ".rb": "Ruby",
        ".php": "PHP",
        ".cs": "C#",
        ".cpp": "C++",
        ".c": "C",
        ".swift": "Swift",
        ".ex": "Elixir",
        ".zig": "Zig",
        ".jsx": "React/JSX",
        ".tsx": "React/TSX",
        ".vue": "Vue",
        ".svelte": "Svelte",
    }

    for ext, _count in stats.get("top_extensions", []):
        if ext in ext_map:
            languages.add(ext_map[ext])

    return ", ".join(sorted(languages)) if languages else "Unknown"


def _summarize_structure(structure: dict, max_depth: int = 3) -> str:
    """Create a concise directory tree summary."""
    lines = []
    dirs = sorted(structure.keys())

    for d in dirs:
        depth = d.count(os.sep) if d != "." else 0
        if depth >= max_depth:
            continue

        indent = "  " * depth
        dirname = os.path.basename(d) if d != "." else "."
        files = structure[d]
        file_count = len(files)

        # Show first few files and count
        if file_count <= 5:
            file_list = ", ".join(files)
        else:
            file_list = ", ".join(files[:4]) + f", ... (+{file_count - 4} more)"

        lines.append(f"{indent}{dirname}/ [{file_list}]")

    return "\n".join(lines[:50])  # Cap at 50 lines


def _summarize_package_json(content: str) -> str:
    """Extract key info from package.json."""
    try:
        pkg = json.loads(content)
    except (TypeError, ValueError):  # fmt: skip
        return content[:500]

    parts = []
    if pkg.get("name"):
        parts.append(f"Name: {pkg['name']}")
    if pkg.get("description"):
        parts.append(f"Description: {pkg['description']}")
    if pkg.get("version"):
        parts.append(f"Version: {pkg['version']}")

    if pkg.get("scripts"):
        scripts = list(pkg["scripts"].keys())
        parts.append(f"Scripts: {', '.join(scripts[:10])}")

    deps = list(pkg.get("dependencies", {}).keys())
    if deps:
        parts.append(f"Dependencies ({len(deps)}): {', '.join(deps[:15])}")

    dev_deps = list(pkg.get("devDependencies", {}).keys())
    if dev_deps:
        parts.append(f"DevDependencies ({len(dev_deps)}): {', '.join(dev_deps[:10])}")

    if pkg.get("workspaces"):
        ws = pkg["workspaces"]
        if isinstance(ws, list):
            parts.append(f"Workspaces: {', '.join(ws)}")
        elif isinstance(ws, dict) and ws.get("packages"):
            parts.append(f"Workspaces: {', '.join(ws['packages'])}")

    return "\n".join(parts)


def _summarize_pyproject(content: str) -> str:
    """Extract key info from pyproject.toml using proper TOML parsing."""
    try:
        data = tomllib.loads(content)
    except (TypeError, ValueError):  # fmt: skip
        # Fallback for malformed TOML
        return _truncate(content, 800)

    parts = []
    project = data.get("project", {})

    if project.get("name"):
        parts.append(f"Name: {project['name']}")
    if project.get("description"):
        parts.append(f"Description: {project['description']}")
    if project.get("version"):
        parts.append(f"Version: {project['version']}")
    if project.get("requires-python"):
        parts.append(f"Python: {project['requires-python']}")

    deps = project.get("dependencies", [])
    if deps:
        parts.append(f"Dependencies ({len(deps)}): {', '.join(str(d) for d in deps[:15])}")

    optional = project.get("optional-dependencies", {})
    for group, group_deps in list(optional.items())[:3]:
        parts.append(
            f"Optional [{group}] ({len(group_deps)}): {', '.join(str(d) for d in group_deps[:8])}"
        )

    scripts = project.get("scripts", {})
    if scripts:
        script_strs = [f"{k}={v}" for k, v in list(scripts.items())[:5]]
        parts.append(f"Entry points: {', '.join(script_strs)}")

    # Build system
    build = data.get("build-system", {})
    if build.get("build-backend"):
        parts.append(f"Build backend: {build['build-backend']}")

    if not parts:
        return _truncate(content, 800)

    return "\n".join(parts)


def _summarize_cargo_toml(content: str) -> str:
    """Extract key info from Cargo.toml using proper TOML parsing."""
    try:
        data = tomllib.loads(content)
    except (TypeError, ValueError):  # fmt: skip
        return _truncate(content, 800)

    parts = []
    pkg = data.get("package", {})

    if pkg.get("name"):
        parts.append(f"Name: {pkg['name']}")
    if pkg.get("description"):
        parts.append(f"Description: {pkg['description']}")
    if pkg.get("edition"):
        parts.append(f"Edition: {pkg['edition']}")

    deps = data.get("dependencies", {})
    if deps:
        dep_names = list(deps.keys())
        parts.append(f"Dependencies ({len(dep_names)}): {', '.join(dep_names[:15])}")

    dev_deps = data.get("dev-dependencies", {})
    if dev_deps:
        parts.append(f"Dev dependencies ({len(dev_deps)}): {', '.join(list(dev_deps.keys())[:10])}")

    # Workspace members
    workspace = data.get("workspace", {})
    members = workspace.get("members", [])
    if members:
        parts.append(f"Workspace members: {', '.join(members[:10])}")

    if not parts:
        return _truncate(content, 800)

    return "\n".join(parts)


def _summarize_go_mod(content: str) -> str:
    """Extract key info from go.mod."""
    import re

    parts = []
    mod_match = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
    if mod_match:
        parts.append(f"Module: {mod_match.group(1)}")

    go_match = re.search(r"^go\s+(\S+)", content, re.MULTILINE)
    if go_match:
        parts.append(f"Go version: {go_match.group(1)}")

    requires = re.findall(r"^\s+(\S+)\s+v", content, re.MULTILINE)
    if requires:
        parts.append(f"Dependencies ({len(requires)}): {', '.join(requires[:15])}")

    if not parts:
        return _truncate(content, 800)

    return "\n".join(parts)


def _summarize_config(config: dict) -> str:
    """Summarize a config file based on its type."""
    path = config["path"]
    content = config["content"]
    fname = os.path.basename(path)

    if fname == "package.json":
        return _summarize_package_json(content)
    if fname == "pyproject.toml":
        return _summarize_pyproject(content)
    if fname == "Cargo.toml":
        return _summarize_cargo_toml(content)
    if fname == "go.mod":
        return _summarize_go_mod(content)
    if fname == "requirements.txt":
        lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.startswith("#")]
        return f"Python dependencies ({len(lines)}): {', '.join(lines[:20])}"
    if fname in (
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ):
        return _truncate(content, 1000)
    return _truncate(content, 800)


def _find_subproject_boundaries(structure: dict, configs: list[dict]) -> list[str]:
    """Detect sub-project boundaries in monorepos.

    A sub-project boundary is any directory containing a config file
    (package.json, Cargo.toml, pyproject.toml, etc.) that is not the root.
    """
    # Directories that contain their own config files
    config_dirs = set()
    for cfg in configs:
        cfg_dir = os.path.dirname(cfg["path"])
        if cfg_dir:  # Skip root-level configs
            config_dirs.add(cfg_dir)

    # Also check top-level dirs that don't have configs but have significant content
    top_dirs = set()
    for d in structure:
        if d != "." and "/" not in d and os.sep not in d:
            top_dirs.add(d)

    # Merge: config-bearing dirs + top-level dirs without config children
    boundaries = set(config_dirs)
    for d in top_dirs:
        # Only add if no config_dir is a child of this top-level dir
        has_config_child = any(cd.startswith(d + "/") or cd == d for cd in config_dirs)
        if not has_config_child:
            boundaries.add(d)

    return sorted(boundaries)
