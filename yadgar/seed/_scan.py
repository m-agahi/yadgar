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

    # Collect structure
    structure = {}  # rel_path -> list of files
    configs = []
    docs = []
    entry_points = []
    ci_cd = []
    ext_counts: dict[str, int] = {}
    total_files = 0
    total_dirs = 0

    for dirpath, dirnames, filenames in os.walk(
        str(root), followlinks=False, onerror=_on_walk_error
    ):
        # Filter out skip directories in-place
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

            # Count extensions
            ext = filepath.suffix.lower()
            if ext:
                ext_counts[ext] = ext_counts.get(ext, 0) + 1

            dir_files.append(fname)

            # Check if it's a config file (exact match + glob patterns)
            config_lang = _match_config(fname)
            if config_lang is not None:
                content = _read_file_safe(filepath)
                if content:
                    configs.append(
                        {
                            "path": os.path.join(rel_dir, fname) if rel_dir else fname,
                            "language": config_lang,
                            "content": content,
                        }
                    )

            # Check if it's a doc file
            if fname in _DOC_FILES:
                content = _read_file_safe(filepath)
                if content:
                    docs.append(
                        {
                            "path": os.path.join(rel_dir, fname) if rel_dir else fname,
                            "content": content,
                        }
                    )

            # Check CI/CD
            if rel_dir in (".github/workflows", ".github", ".gitlab") or fname in _CI_FILES:
                content = _read_file_safe(filepath)
                if content:
                    ci_cd.append(
                        {
                            "path": os.path.join(rel_dir, fname) if rel_dir else fname,
                            "content": content,
                        }
                    )

            # Check entry points (only in root or src/)
            if rel_dir in ("", "src"):
                for pattern in _ENTRY_PATTERNS:
                    rel_path = os.path.join(rel_dir, fname) if rel_dir else fname
                    if fnmatch.fnmatch(rel_path, pattern):
                        content = _read_file_safe(filepath)
                        if content:
                            entry_points.append(
                                {
                                    "path": rel_path,
                                    "content": content,
                                }
                            )
                        break

        if dir_files:
            structure[rel_dir or "."] = dir_files

    # Sort extensions by count
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
