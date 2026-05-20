#!/usr/bin/env python3
"""Yadgar SubagentStop hook — entry point for Claude Code hook system.

This script is installed to ~/.claude/hooks/yadgar-subagent-stop.py
and registered in settings.json under the SubagentStop event.

Logic lives in yadgar/hooks/subagent_stop.py for testability.
When run standalone (installed copy), falls back to inline impl.

Claude Code SubagentStop event payload (stdin JSON):
  {
    "session_id": "...",
    "cwd": "/path/to/project",
    "agent_type": "general-purpose",
    "transcript_path": "/path/to/transcript.jsonl",
    "stop_hook_active": false
  }

Output: nothing (no blocking, no injection).
Errors: swallowed silently — never block subagent completion.
"""

from __future__ import annotations

import sys

# Try to import from the yadgar package (daemon-installed or pipx venv).
# Fall back to the self-contained inline implementation if not importable.
try:
    from yadgar.hooks.subagent_stop import main
except ImportError:
    # Standalone fallback — duplicate of subagent_stop.py logic for portability.
    # Keeps this script functional even without the yadgar package on sys.path.
    import json
    import os
    import re
    import urllib.request

    _PORT = os.environ.get("YADGAR_PORT", "8765")
    _AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

    _HEADING_RE = re.compile(r"^(#{1,6})\s+([^\n]+)$", re.MULTILINE)
    _NEXT_HEADING_RE = re.compile(r"\n#{1,6}\s+")
    _BULLET_RE = re.compile(r"^\s*-\s+(.+)$", re.MULTILINE)

    def _extract_findings(text):
        # Lenient: any heading containing both 'yadgar' and 'find' (case-insensitive)
        section_body = None
        for hm in _HEADING_RE.finditer(text):
            heading_text = hm.group(2).lower()
            if "yadgar" in heading_text and "find" in heading_text:
                start = hm.end()
                rest = text[start:]
                end_m = _NEXT_HEADING_RE.search(rest)
                section_body = rest[: end_m.start()] if end_m else rest
                break
        if section_body is None:
            return []
        bullets = []
        for m in _BULLET_RE.finditer(section_body):
            v = m.group(1).strip()
            if v.startswith("<!--") or v.lower() == "none":
                continue
            if v:
                bullets.append(v)
        return bullets

    def _get_report_text(data):
        transcript_path = data.get("transcript_path", "")
        if not transcript_path:
            return ""
        try:
            import pathlib

            p = pathlib.Path(transcript_path)
            if not p.exists():
                return ""
            last = ""
            for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message", entry)
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    last = content
                elif isinstance(content, list):
                    parts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    joined = "\n".join(p for p in parts if p.strip())
                    if joined.strip():
                        last = joined
            return last
        except Exception:
            return ""

    def _post_findings(agent_type, cwd, findings):
        if not findings:
            return
        payload = json.dumps({"agent_type": agent_type, "cwd": cwd, "findings": findings}).encode()
        headers = {"Content-Type": "application/json"}
        if _AUTH_TOKEN:
            headers["Authorization"] = f"Bearer {_AUTH_TOKEN}"
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{_PORT}/hooks/subagent-stop", data=payload, headers=headers
            )
            urllib.request.urlopen(req, timeout=3.0)
        except Exception:
            pass

    def main():
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            return
        agent_type = str(data.get("agent_type", "general-purpose")).strip() or "general-purpose"
        cwd = str(data.get("cwd", os.getcwd())).strip() or os.getcwd()
        report_text = _get_report_text(data)
        if not report_text:
            return
        findings = _extract_findings(report_text)
        if not findings:
            return
        _post_findings(agent_type, cwd, findings)


if __name__ == "__main__":
    main()
