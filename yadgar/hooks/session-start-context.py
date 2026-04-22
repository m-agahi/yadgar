#!/usr/bin/env python3
"""Yadgar session context — SessionStart hook handler.

Injects recent project context into Claude's conversation on every
session start. Uses lightweight DB queries only — no ML model loading.

Output goes to stdout and is injected into Claude's context window.
Works in both stdio and HTTP transport modes (reads SurrealDB directly).
"""

import fcntl
import json
import os
import sys
from pathlib import Path


def _db_locked(db_path: Path) -> bool:
    """Check if the MCP server holds the surrealkv DB lock."""
    lock_path = db_path.parent / "yadgar.lock"
    if not lock_path.exists():
        return False
    try:
        lf = open(lock_path, "r")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
        return False
    except OSError:
        return True


def main():
    db_path = Path(os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")).expanduser()

    # Read hook input from stdin to get cwd
    try:
        data = json.load(sys.stdin)
        cwd = data.get("cwd", os.getcwd())
    except Exception:
        cwd = os.getcwd()

    if _db_locked(db_path):
        return  # MCP server owns the DB — skip direct access

    try:
        from surrealdb import Surreal
        db = Surreal(f"surrealkv://{db_path}")
        db.use("yadgar", "main")
    except Exception:
        return

    try:
        # 1. Get latest checkpoint
        cp_res = db.query(
            "SELECT current_task, key_decisions, custom_context, created_at "
            "FROM checkpoints WHERE is_active = true "
            "ORDER BY created_at DESC LIMIT 1"
        )
        checkpoint = None
        if cp_res and cp_res[0]:
            checkpoint = cp_res[0][0]

        # 2. Get hot memories for this directory
        hot_res = db.query(
            "SELECT content, heat, created_at "
            "FROM memory "
            "WHERE directory_context = $dir AND heat >= 0 "
            "ORDER BY heat DESC LIMIT 6",
            {"dir": cwd},
        )
        hot = hot_res[0] if hot_res and hot_res[0] else []

        # 3. Get anchored memories
        anch_res = db.query(
            "SELECT content FROM memory "
            "WHERE is_protected = true AND heat > 0 "
            "AND tags CONTAINSANY ['_anchor'] "
            "ORDER BY created_at DESC LIMIT 4"
        )
        anchored = anch_res[0] if anch_res and anch_res[0] else []

        # 4. Get recent actions (last 10)
        act_res = db.query(
            "SELECT tool_name, tool_input_summary, timestamp "
            "FROM action_log "
            "ORDER BY timestamp DESC LIMIT 10"
        )
        actions = act_res[0] if act_res and act_res[0] else []

    except Exception:
        return

    # Only output if we have something useful
    if not hot and not checkpoint and not anchored:
        return

    lines = []
    lines.append("# Yadgar — Session Context")
    lines.append("")

    if checkpoint and checkpoint.get("current_task"):
        task = checkpoint["current_task"]
        if not task.startswith("[auto-captured"):
            lines.append(f"**Last task:** {task}")
            if checkpoint.get("key_decisions"):
                try:
                    decisions = json.loads(checkpoint["key_decisions"])
                    if decisions:
                        for d in decisions:
                            lines.append(f"  - {d}")
                except (json.JSONDecodeError, TypeError):
                    pass
            lines.append("")

    if anchored:
        lines.append("## Critical Facts")
        for row in anchored:
            lines.append(f"- {row['content'][:200]}")
        lines.append("")

    if hot:
        lines.append("## Project Context")
        for row in hot:
            content = row["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"- [{row['heat']:.1f}] {content}")
        lines.append("")

    if actions:
        lines.append("## Recent Actions")
        for a in reversed(list(actions)):
            summary = a["tool_input_summary"]
            if len(summary) > 80:
                summary = summary[:80] + "..."
            lines.append(f"- {a['tool_name']}: {summary}")
        lines.append("")

    lines.append(f"*Context for: {cwd}*")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
