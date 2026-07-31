#!/usr/bin/env python3
"""Yadgar auto-recall — UserPromptSubmit hook handler.

Automatically retrieves relevant memories for every user prompt and
outputs plain text to stdout so Claude receives them as context without
needing to call any tool.

HTTP-only: retrieval via daemon HTTP endpoint. No direct surrealkv.
Each invocation is a fresh process; kept fast for per-prompt use.

Target latency: <500ms.
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing


@observe(tier="hot")
def _db_locked(db_path: Path) -> bool:
    """Check if the MCP server holds the surrealkv DB lock."""
    lock_path = db_path.parent / "yadgar.lock"
    if not lock_path.exists():
        return False
    try:
        lf = open(lock_path)
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
        return False
    except OSError:
        return True


# Maximum memories to inject per turn
MAX_RESULTS = 5
# Minimum heat to surface (very low — let ranking handle quality)
MIN_HEAT = 0.0
# Maximum total characters to inject (keep context budget reasonable)
MAX_CONTEXT_CHARS = 3000
# Time budget in seconds
TIME_BUDGET = 0.5


@observe(tier="hot")
def _extract_query(data: dict) -> str:
    """Extract the user's prompt text from hook input."""
    prompt = data.get("prompt", "")
    if not prompt:
        prompt = data.get("user_prompt", "")
    return str(prompt).strip()


@observe(tier="hot")
def _preprocess_fts(query: str) -> str:
    """Convert user prompt into an FTS query (space-joined terms)."""
    words = []
    for word in query.split():
        cleaned = "".join(c for c in word if c.isalnum() or c == "_")
        if len(cleaned) >= 2:
            words.append(cleaned)
    if not words:
        return ""
    return " ".join(words[:15])  # Cap at 15 terms


@observe(tier="stage")
def _fts_search(db, query: str, directory: str) -> list:
    """SurrealDB FTS search, scoped to current project first."""
    fts_query = _preprocess_fts(query)
    if not fts_query:
        return []
    try:
        lim = MAX_RESULTS * 3
        # Search current project first
        res = db.query(
            "SELECT id, content, heat, directory_context, "
            "search::score(1) AS score "
            "FROM memory "
            "WHERE content @1@ $query AND heat >= $min_heat AND is_stale = false "
            "AND directory_context = $dir "
            "ORDER BY score DESC LIMIT $lim",
            {"query": fts_query, "min_heat": MIN_HEAT, "dir": directory, "lim": lim},
        )
        rows = res[0] if res and res[0] else []

        # Supplement with cross-cutting (global/empty) results if not enough.
        # v5.65: use IN ('', 'global') instead of != $dir to avoid leaking
        # other-project memories into the injected prompt context.
        if len(rows) < MAX_RESULTS and directory:
            global_res = db.query(
                "SELECT id, content, heat, directory_context, "
                "search::score(1) AS score "
                "FROM memory "
                "WHERE content @1@ $query AND heat >= $min_heat AND is_stale = false "
                "AND directory_context IN ('', 'global') "
                "ORDER BY score DESC LIMIT $lim",
                {
                    "query": fts_query,
                    "min_heat": MIN_HEAT,
                    "lim": MAX_RESULTS * 2,
                },
            )
            global_rows = global_res[0] if global_res and global_res[0] else []
            rows = list(rows) + list(global_rows)

        results = []
        for r in rows:
            # RecordID format is "memory:123" — extract numeric part as id key
            raw_id = r.get("id", "")
            if hasattr(raw_id, "__str__"):
                raw_id = str(raw_id)
            mem_id = raw_id.split(":")[-1] if ":" in raw_id else raw_id
            results.append(
                {
                    "id": mem_id,
                    "content": r.get("content", ""),
                    "heat": r.get("heat", 0.0),
                    "directory": r.get("directory_context", ""),
                    "score": r.get("score", 0.0),
                    "source": "fts",
                }
            )
        return results
    except Exception:
        return []


@observe(tier="hot")
def _merge_and_rank(fts_results: list, directory: str) -> list:
    """Deduplicate and rank results, boosting project matches."""
    seen = {}

    for r in fts_results:
        mid = r["id"]
        if mid not in seen:
            seen[mid] = r
            seen[mid]["combined"] = r["score"]
        else:
            seen[mid]["combined"] += r["score"] * 0.5

    results = list(seen.values())

    for r in results:
        # Boost memories from the current project directory
        if r["directory"] == directory:
            r["combined"] *= 1.5
        # Boost semantic/manual memories over action stream
        content = r.get("content", "")
        if not content.startswith("Session activity"):
            r["combined"] *= 2.0
        # Boost by heat (hotter = more relevant)
        r["combined"] *= 1.0 + r.get("heat", 0)

    results.sort(key=lambda x: -x["combined"])
    return results[:MAX_RESULTS]


@observe(tier="hot")
def _format_context(memories: list, directory: str) -> str:
    """Format memories as concise context for injection."""
    if not memories:
        return ""

    lines = []
    lines.append("# Yadgar — Auto-Recall\n")
    total_chars = 0
    for m in memories:
        content = m["content"]
        if total_chars + len(content) > MAX_CONTEXT_CHARS:
            remaining = MAX_CONTEXT_CHARS - total_chars
            if remaining > 50:
                content = content[:remaining] + "..."
            else:
                break
        proj = ""
        if m["directory"] and m["directory"] != directory:
            proj = f" [{Path(m['directory']).name}]"
        lines.append(f"- {content}{proj}")
        total_chars += len(content)

    lines.append(f"\n*{len(memories)} memories surfaced for: {directory}*")
    return "\n".join(lines)


@observe(tier="boundary")
def main():
    try:
        time.monotonic()

        try:
            data = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError) as _e:
            return

        query = _extract_query(data)
        if not query or len(query) < 2:
            return

        directory = data.get("cwd", "") or os.getcwd()

        Path(os.environ.get("YADGAR_DB_PATH", str(_paths.DB_PATH))).expanduser()

        # Try HTTP endpoint first — works in daemon mode where DB lock is always held
        _port = os.environ.get("YADGAR_PORT", "8765")
        try:
            import contextlib as _contextlib
            import urllib.error as _err
            import urllib.parse as _parse
            import urllib.request as _req

            _params = _parse.urlencode({"query": query, "directory": directory})
            _token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
            _req_obj = _req.Request(
                f"http://127.0.0.1:{_port}/hooks/prompt-recall?{_params}",
            )
            if _token:
                _req_obj.add_header("Authorization", f"Bearer {_token}")
            with _contextlib.closing(_req.urlopen(_req_obj, timeout=TIME_BUDGET)) as _resp:
                _text = json.loads(_resp.read().decode()).get("text", "")
            if _text:
                print(_text)
            return
        except _err.HTTPError as _http_exc:
            # Close the file wrapper (py3.14 ResourceWarning leak guard).
            _http_exc.close()
        except Exception:
            pass  # Daemon down — skip; never use surrealkv directly from host
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
