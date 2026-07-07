"""Seed phase 1 — directory scanning.

Walks a project directory and collects config files, docs,
entry points, CI/CD configs, and structural stats.
"""

import fnmatch
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories to always skip
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "env",
        ".env",
        "dist",
        "build",
        "out",
        "target",
        ".next",
        ".nuxt",
        ".output",
        ".tox",
        ".nox",
        "vendor",
        ".terraform",
        ".idea",
        ".vscode",
        "coverage",
        ".coverage",
        ".lockstep",
        ".claude",
        "egg-info",
    }
)

# Binary file extensions to skip reading
_BINARY_EXTENSIONS = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".o",
        ".a",
        ".dll",
        ".exe",
        ".dylib",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".svg",
        ".webp",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".flac",
        ".wasm",
        ".class",
        ".jar",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".lock",  # package lock files are huge and not useful
    }
)

# Config files that reveal project structure and dependencies
# Keys are either exact filenames or glob patterns (prefixed with *)
_CONFIG_EXACT = {
    # Python
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "Pipfile": "python",
    "requirements.txt": "python",
    # JavaScript/TypeScript
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "tsconfig.base.json": "typescript",
    # Rust
    "Cargo.toml": "rust",
    # Go
    "go.mod": "go",
    "go.sum": "go",
    # Java/Kotlin
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "pom.xml": "java",
    # Ruby
    "Gemfile": "ruby",
    # PHP
    "composer.json": "php",
    # Docker
    "Dockerfile": "docker",
    "docker-compose.yml": "docker",
    "docker-compose.yaml": "docker",
    "compose.yml": "docker",
    "compose.yaml": "docker",
    # CI/CD
    "Makefile": "build",
    "Justfile": "build",
    "Taskfile.yml": "build",
    # Config
    ".env.example": "config",
    ".env.sample": "config",
}

# Glob patterns for config files (checked via fnmatch)
_CONFIG_GLOBS = [
    ("*.csproj", "csharp"),
    ("*.fsproj", "fsharp"),
]

# Documentation files to read
_DOC_FILES = frozenset(
    {
        "README.md",
        "README.rst",
        "README.txt",
        "README",
        "ARCHITECTURE.md",
        "DESIGN.md",
        "CONTRIBUTING.md",
        "CONTRIBUTING.rst",
        "CHANGELOG.md",
        "CHANGES.md",
        "CLAUDE.md",
    }
)

# Entry point patterns (checked with fnmatch)
_ENTRY_PATTERNS = [
    "main.*",
    "index.*",
    "app.*",
    "server.*",
    "cli.*",
    "cmd.*",
    "__main__.py",
    "src/main.*",
    "src/index.*",
    "src/app.*",
    "src/lib.*",
    "lib.rs",
]

# CI/CD files detected by name (outside of .github/.gitlab dirs)
_CI_FILES = frozenset(
    {
        ".gitlab-ci.yml",
        "Jenkinsfile",
        ".travis.yml",
        "azure-pipelines.yml",
        ".drone.yml",
    }
)

# Max file size to read (64KB)
_MAX_FILE_SIZE = 64 * 1024

# Max content length per memory
_MAX_MEMORY_CONTENT = 2000


def _match_config(fname: str) -> str | None:
    """Match a filename against config files (exact + glob patterns)."""
    if fname in _CONFIG_EXACT:
        return _CONFIG_EXACT[fname]
    for pattern, language in _CONFIG_GLOBS:
        if fnmatch.fnmatch(fname, pattern):
            return language
    return None


def _should_skip_dir(name: str) -> bool:
    """Check if a directory should be skipped."""
    if name in _SKIP_DIRS:
        return True
    if name.startswith(".") and name not in (".github", ".gitlab"):
        return True
    if name.endswith(".egg-info"):
        return True
    return False


def _read_file_safe(path: Path) -> str | None:
    """Read file content safely, respecting size limits and skipping binary files."""
    try:
        if path.suffix.lower() in _BINARY_EXTENSIONS:
            return None
        if path.stat().st_size > _MAX_FILE_SIZE:
            return None
        return path.read_text(errors="replace")
    except Exception:
        return None


def _truncate(text: str, max_len: int = _MAX_MEMORY_CONTENT) -> str:
    """Truncate text to max length, breaking at a line boundary."""
    if len(text) <= max_len:
        return text
    # Find last newline before limit, accounting for suffix
    suffix = "\n[... truncated]"
    effective_max = max_len - len(suffix)
    cut = text.rfind("\n", 0, effective_max)
    if cut < effective_max // 2:
        cut = effective_max
    return text[:cut] + suffix


def _on_walk_error(error: OSError) -> None:
    """Log permission errors during os.walk."""
    logger.warning("Skipped (permission denied): %s", error.filename or error)


def _rel_path(rel_dir: str, fname: str) -> str:
    """Return repo-relative path for a file, handling the root directory case."""
    return os.path.join(rel_dir, fname) if rel_dir else fname


def _maybe_config(fname: str, filepath: Path, rel_dir: str) -> dict | None:
    """Return a config record for fname if it matches a known config pattern."""
    config_lang = _match_config(fname)
    if config_lang is None:
        return None
    content = _read_file_safe(filepath)
    if not content:
        return None
    return {"path": _rel_path(rel_dir, fname), "language": config_lang, "content": content}


def _maybe_doc(fname: str, filepath: Path, rel_dir: str) -> dict | None:
    """Return a doc record for fname if it is a recognised documentation file."""
    if fname not in _DOC_FILES:
        return None
    content = _read_file_safe(filepath)
    if not content:
        return None
    return {"path": _rel_path(rel_dir, fname), "content": content}


def _maybe_ci_cd(fname: str, filepath: Path, rel_dir: str) -> dict | None:
    """Return a CI/CD record if the file lives in a CI dir or matches a CI name."""
    in_ci_dir = rel_dir in (".github/workflows", ".github", ".gitlab")
    if not in_ci_dir and fname not in _CI_FILES:
        return None
    content = _read_file_safe(filepath)
    if not content:
        return None
    return {"path": _rel_path(rel_dir, fname), "content": content}


def _maybe_entry_point(fname: str, filepath: Path, rel_dir: str) -> dict | None:
    """Return an entry-point record if the file matches any entry pattern."""
    if rel_dir not in ("", "src"):
        return None
    rel_path = _rel_path(rel_dir, fname)
    for pattern in _ENTRY_PATTERNS:
        if fnmatch.fnmatch(rel_path, pattern):
            content = _read_file_safe(filepath)
            if content:
                return {"path": rel_path, "content": content}
            return None
    return None


def _collect_file_records(
    fname: str,
    filepath: Path,
    rel_dir: str,
    configs: list,
    docs: list,
    ci_cd: list,
    entry_points: list,
) -> None:
    """Classify one file and append records to the appropriate output lists."""
    record = _maybe_config(fname, filepath, rel_dir)
    if record:
        configs.append(record)

    record = _maybe_doc(fname, filepath, rel_dir)
    if record:
        docs.append(record)

    record = _maybe_ci_cd(fname, filepath, rel_dir)
    if record:
        ci_cd.append(record)

    record = _maybe_entry_point(fname, filepath, rel_dir)
    if record:
        entry_points.append(record)


def scan_project(directory: str) -> dict:
    """Scan a project directory and return structured data for seeding.

    Returns a dict with keys:
    - project_name: str
    - structure: dict (directory tree summary)
    - configs: list[dict] (config file contents and their relative paths)
    - docs: list[dict] (documentation file contents)
    - entry_points: list[dict] (main entry points found)
    - ci_cd: list[dict] (CI/CD config files)
    - stats: dict (file counts by extension, total dirs, etc.)
    """
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    project_name = root.name

    structure: dict[str, list] = {}
    configs: list = []
    docs: list = []
    entry_points: list = []
    ci_cd: list = []
    ext_counts: dict[str, int] = {}
    total_files = 0
    total_dirs = 0

    for dirpath, dirnames, filenames in os.walk(
        str(root), followlinks=False, onerror=_on_walk_error
    ):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        dirnames.sort()

        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        total_dirs += 1

        dir_files = []
        for fname in sorted(filenames):
            filepath = Path(dirpath) / fname
            total_files += 1
            ext = filepath.suffix.lower()
            if ext:
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
            dir_files.append(fname)
            _collect_file_records(fname, filepath, rel_dir, configs, docs, ci_cd, entry_points)

        if dir_files:
            structure[rel_dir or "."] = dir_files

    top_extensions = sorted(ext_counts.items(), key=lambda x: -x[1])[:10]

    return {
        "project_name": project_name,
        "root": str(root),
        "structure": structure,
        "configs": configs,
        "docs": docs,
        "entry_points": entry_points,
        "ci_cd": ci_cd,
        "stats": {
            "total_files": total_files,
            "total_dirs": total_dirs,
            "top_extensions": top_extensions,
        },
    }
