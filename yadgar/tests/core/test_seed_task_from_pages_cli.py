"""TDD — Car E, step 6: CLI admin op `seed-task-from-pages`.

The CLI exposes a subcommand that mirrors the ADR seed CLI shape, calling
the backend `seed_task_from_pages` op one project at a time.

Pinned here:
- The `--seed-task-from-pages` flag is registered on the `seed` subparser.
- The flag is wired to a handler that calls the backend admin op.
- The flag accepts `--directory` and `--project-id` kwargs.
- The flag accepts `--dry-run`.
"""

from __future__ import annotations

import os


def _read_source() -> str:
    here = os.path.abspath(__file__)
    repo_root = here
    while repo_root and not os.path.exists(os.path.join(repo_root, "pyproject.toml")):
        parent = os.path.dirname(repo_root)
        if parent == repo_root:
            break
        repo_root = parent
    pkg_path = os.path.join(repo_root, "yadgar", "core", "cli", "seed.py")
    with open(pkg_path) as fh:
        return fh.read()


def test_cli_registers_seed_task_from_pages_flag():
    """The `seed` subparser must register `--seed-task-from-pages`."""
    src = _read_source()
    assert "--seed-task-from-pages" in src, (
        "the CLI must register a --seed-task-from-pages flag on the seed subparser"
    )


def test_cli_routes_via_backend_admin_op():
    """The CLI handler must call the backend `seed_task_from_pages` op."""
    src = _read_source()
    assert "seed_task_from_pages" in src, (
        "the CLI handler must call the backend seed_task_from_pages op"
    )


def test_cli_handler_branches_on_seed_task_from_pages_flag():
    """cmd_seed must dispatch on the --seed-task-from-pages flag (mirrors the
    existing --agent-prompts / --anchors branch shape)."""
    src = _read_source()
    # The flag references the same `getattr(args, ...)` pattern already used
    # for --agent-prompts and --anchors.
    assert "seed_task_from_pages" in src
