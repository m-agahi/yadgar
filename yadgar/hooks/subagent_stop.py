"""Yadgar SubagentStop hook logic — importable module.

This module contains the implementation used by both:
- yadgar/hooks/subagent-stop.py (Claude Code hook script, run directly)
- yadgar/tests/test_subagent_stop_hook.py (test imports)

All functions are pure / side-effect-free except _post_findings and main().
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

_PORT = os.environ.get("YADGAR_PORT", "8765")
_AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

# Pattern to find the ## Yadgar findings section in a report.
# Matches the section heading (with optional [agent: ...] tag) and captures
# everything until the next ## heading or end-of-string.
_FINDINGS_SECTION_RE = re.compile(
    r"##\s+Yadgar\s+findings(?:\s+\[.*?\])?\s*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)

# A bullet line: starts with optional whitespace + "- "
_BULLET_RE = re.compile(r"^\s*-\s+(.+)$", re.MULTILINE)


def _auth_headers() -> dict:
    if _AUTH_TOKEN:
        return {"Authorization": f"Bearer {_AUTH_TOKEN}"}
    return {}


def _extract_findings(text: str) -> list[str]:
    """Return list of bullet texts from the '## Yadgar findings' section.

    Returns empty list if the section is absent or contains no bullets.
    Skips comment lines (<!-- ... -->) and the literal bullet "none".
    """
    match = _FINDINGS_SECTION_RE.search(text)
    if not match:
        return []

    section_body = match.group(1)
    bullets = []
    for m in _BULLET_RE.finditer(section_body):
        text_val = m.group(1).strip()
        # Skip comment lines and the sentinel "none" marker
        if text_val.startswith("<!--") or text_val.lower() == "none":
            continue
        if text_val:
            bullets.append(text_val)
    return bullets


def _get_report_text(data: dict) -> str:
    """Extract the agent's final report text from the SubagentStop payload.

    Claude Code SubagentStop does not include the full report text directly
    in the payload — reads the last assistant turn from the transcript JSONL.
    Falls back to empty string if transcript is unavailable.
    """
    transcript_path = data.get("transcript_path", "")
    if not transcript_path:
        return ""

    try:
        import pathlib

        p = pathlib.Path(transcript_path)
        if not p.exists():
            return ""

        last_assistant_content = ""
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", entry)
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue

            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                last_assistant_content = content
            elif isinstance(content, list):
                # Extract text blocks
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(p for p in parts if p.strip())
                if joined.strip():
                    last_assistant_content = joined

        return last_assistant_content
    except Exception:
        return ""


def _post_findings(agent_type: str, cwd: str, findings: list[str]) -> None:
    """POST findings to /hooks/subagent-stop endpoint.

    Silently swallows all errors — must never block subagent completion.
    """
    if not findings:
        return

    payload = json.dumps(
        {
            "agent_type": agent_type,
            "cwd": cwd,
            "findings": findings,
        }
    ).encode()

    url = f"http://127.0.0.1:{_PORT}/hooks/subagent-stop"
    headers = {"Content-Type": "application/json", **_auth_headers()}
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        urllib.request.urlopen(req, timeout=3.0)
    except Exception:
        pass  # Daemon down or error — fail silently, never block subagent


def main() -> None:
    """Entry point called by the hook script."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return  # Malformed payload — skip silently

    # Extract payload fields
    agent_type = str(data.get("agent_type", "general-purpose")).strip()
    cwd = str(data.get("cwd", os.getcwd())).strip() or os.getcwd()

    if not agent_type:
        agent_type = "general-purpose"

    # Get the agent's final report text
    report_text = _get_report_text(data)
    if not report_text:
        return

    # Parse findings from ## Yadgar findings section
    findings = _extract_findings(report_text)
    if not findings:
        return

    # POST findings to daemon
    _post_findings(agent_type, cwd, findings)
