"""Yadgar SubagentStop hook logic — importable module.

This module contains the implementation used by both:
- yadgar/hooks/subagent-stop.py (Claude Code hook script, run directly)
- yadgar/tests/test_subagent_stop_hook.py (test imports)

All functions are pure / side-effect-free except _post_findings and main().
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing

_PORT = os.environ.get("YADGAR_PORT", "8765")
_AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

logger = logging.getLogger(__name__)

# ── v5.44.0 X2: structured directive grammar ──────────────────────────────────
#
# Supported directive types (PREFIX: key=value pairs):
#   memorize: content="...", tags=["a","b"], context="..."
#   wiki_add: title="...", content="...", category="...", tags=["a"], directory="...", branch_hint="..."
#   anchor:   content="...", reason="...", tier="conditional"
#
# Parse strategy: extract the prefix (memorize/wiki_add/anchor), then parse
# key=value pairs using a lenient regex. Quoted strings and JSON lists accepted.
# Malformed directives return None + increment metric (lenient per DP-X2-2).

_DIRECTIVE_TYPES = ("memorize", "wiki_add", "anchor")
_DIRECTIVE_PREFIX_RE = re.compile(
    r"^(memorize|wiki_add|anchor)\s*:\s*(.*)$",
    re.DOTALL | re.IGNORECASE,
)

# Match key="..." or key=[...] or key=unquoted_word
_KV_RE = re.compile(
    r"""(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|(\[[^\]]*\])|([\w\-/\.]+))""",
    re.DOTALL,
)


@observe(tier="hot")
def _parse_kv_value(kv_m) -> object:
    """Extract the value from a key=value regex match (helper for _parse_directive)."""
    if kv_m.group(2) is not None:
        # Quoted string — unescape basic backslash sequences
        return kv_m.group(2).replace('\\"', '"').replace("\\\\", "\\")
    if kv_m.group(3) is not None:
        # JSON list
        try:
            return json.loads(kv_m.group(3))
        except Exception:
            return kv_m.group(3)
    return kv_m.group(4)


@observe(tier="hot")
def _parse_directive(bullet: str) -> dict | None:
    """Parse a structured directive bullet into a typed dict.

    Returns:
        {"type": "memorize"|"wiki_add"|"anchor", "params": {key: value, ...}}
        or None if the bullet is not a recognized directive or is malformed.

    Lenient per DP-X2-2: malformed entries return None (caller logs warning +
    increments metric). The whole report is NOT rejected.
    """
    if not bullet:
        return None

    m = _DIRECTIVE_PREFIX_RE.match(bullet.strip())
    if not m:
        return None

    directive_type = m.group(1).lower()
    remainder = m.group(2).strip()
    params = {kv_m.group(1): _parse_kv_value(kv_m) for kv_m in _KV_RE.finditer(remainder)}
    return {"type": directive_type, "params": params}


# Lenient heading matcher — any H1–H6 whose text contains both "yadgar" and
# "find" (matches: "## Yadgar findings", "### Yadgar Findings",
# "## Findings (Yadgar)", "## yadgar-findings", "## FINDINGS — YADGAR", etc.)
_HEADING_RE = re.compile(r"^(#{1,6})\s+([^\n]+)$", re.MULTILINE)
# Any ## heading (used to find end of findings section)
_NEXT_HEADING_RE = re.compile(r"\n#{1,6}\s+")

# Legacy strict pattern kept for reference — superseded by _extract_findings logic
_FINDINGS_SECTION_RE = re.compile(
    r"##\s+Yadgar\s+findings(?:\s+\[.*?\])?\s*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)

# A bullet line: starts with optional whitespace + "- "
_BULLET_RE = re.compile(r"^\s*-\s+(.+)$", re.MULTILINE)


@observe(tier="hot")
def _auth_headers() -> dict:
    if _AUTH_TOKEN:
        return {"Authorization": f"Bearer {_AUTH_TOKEN}"}
    return {}


@observe(tier="stage")
def _extract_findings(text: str) -> list[str]:
    """Return list of bullet texts from the Yadgar findings section.

    Lenient parser — accepts any heading (H1–H6) whose text contains both
    'yadgar' and 'find' (case-insensitive). Handles:
      ## Yadgar findings, ### Yadgar Findings, ## Findings (Yadgar),
      ## yadgar-findings, ## FINDINGS — YADGAR, etc.

    Returns empty list if the section is absent or contains no bullets.
    Skips comment lines (<!-- ... -->) and the literal bullet "none".
    """
    section_body: str | None = None
    for hm in _HEADING_RE.finditer(text):
        heading_text = hm.group(2).lower()
        if "yadgar" in heading_text and "find" in heading_text:
            # Slice from end of this heading line to next heading or EOF
            start = hm.end()
            rest = text[start:]
            end_m = _NEXT_HEADING_RE.search(rest)
            section_body = rest[: end_m.start()] if end_m else rest
            break

    if section_body is None:
        return []

    bullets = []
    for m in _BULLET_RE.finditer(section_body):
        text_val = m.group(1).strip()
        # Skip comment lines and the sentinel "none" marker
        if text_val.startswith("<!--") or text_val.lower() == "none":
            continue
        if text_val:
            bullets.append(text_val)
    return bullets


@observe(tier="hot")
def _extract_content_from_msg(msg: dict) -> str:
    """Extract text content from an assistant message dict (helper for _get_report_text)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p.strip())
    return ""


@observe(tier="hot")
def _parse_transcript_line(raw: str) -> str:
    """Parse one JSONL line from a transcript; return assistant text or empty string."""
    if not raw:
        return ""
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    msg = entry.get("message", entry)
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return ""
    return _extract_content_from_msg(msg)


@observe(tier="stage")
def _get_report_text(data: dict) -> str:
    """Extract the agent's final report text from the SubagentStop payload.

    Claude Code SubagentStop does not include the full report text directly
    in the payload — reads the last assistant turn from the transcript JSONL.
    Falls back to empty string if transcript is unavailable.
    """
    import pathlib  # noqa: PLC0415

    transcript_path = data.get("transcript_path", "")
    if not transcript_path:
        return ""

    try:
        p = pathlib.Path(transcript_path)
        if not p.exists():
            return ""

        last_assistant_content = ""
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            text = _parse_transcript_line(raw.strip())
            if text:
                last_assistant_content = text

        return last_assistant_content
    except Exception:
        return ""


@observe(tier="stage")
def _post_findings(
    agent_type: str,
    cwd: str,
    findings: list[str],
    branch_hint: str | None = None,
) -> None:
    """POST findings to /hooks/subagent-stop endpoint.

    v5.44.0 X2: branch_hint forwarded in payload so daemon writes land on the
    correct branch (regression guard for v5.42.2 precedent — writer + checker
    must use the same branch, NOT daemon CWD). Also adds _subagent_writeback
    signal tag.

    Silently swallows all errors — must never block subagent completion.
    """
    if not findings:
        return

    payload_dict: dict = {
        "agent_type": agent_type,
        "cwd": cwd,
        "findings": findings,
        # v5.44.0 X2: signal tag for daemon-side tagging
        "_subagent_writeback": True,
    }
    if branch_hint:
        payload_dict["branch_hint"] = branch_hint

    payload = json.dumps(payload_dict).encode()

    url = f"http://127.0.0.1:{_PORT}/hooks/subagent-stop"
    headers = {"Content-Type": "application/json", **_auth_headers()}
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        urllib.request.urlopen(req, timeout=3.0)
    except Exception:
        pass  # Daemon down or error — fail silently, never block subagent


@observe(tier="stage")
def _detect_branch_from_cwd(cwd: str) -> str | None:
    """Detect the git branch from the caller's cwd.

    Returns branch name or None. Never raises.
    Used by SubagentStop hook to supply branch_hint to daemon so writes land
    on the correct branch (v5.44.0 X2, regression guard for v5.42.2).
    """
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


@observe(tier="boundary")
def main() -> None:
    """Entry point called by the hook script."""
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            return  # Malformed payload — skip silently

        # Extract payload fields
        agent_type = str(data.get("agent_type", "general-purpose")).strip()
        cwd = str(data.get("cwd", os.getcwd())).strip() or os.getcwd()

        if not agent_type:
            agent_type = "general-purpose"

        transcript_path = data.get("transcript_path", "")

        # v5.44.0 X2: detect branch from caller cwd so writes land on correct branch.
        # This avoids the v5.42.2 bug where drainer used daemon CWD instead of caller CWD.
        branch_hint = _detect_branch_from_cwd(cwd)

        # I12: log structured outcome so capture rate is observable across sessions.
        # Get the agent's final report text
        report_text = _get_report_text(data)
        if not report_text:
            logger.debug(
                "subagent_stop outcome=transcript_missing agent_type=%s transcript_path=%r",
                agent_type,
                transcript_path,
            )
            return

        # Parse findings from Yadgar findings section (lenient heading matcher)
        findings = _extract_findings(report_text)
        heading_matched = bool(findings)

        logger.debug(
            "subagent_stop outcome=%s agent_type=%s report_len=%d heading_matched=%s bullets=%d",
            "captured" if heading_matched else "not_matched",
            agent_type,
            len(report_text),
            heading_matched,
            len(findings),
        )

        if not findings:
            return

        # POST findings to daemon (branch_hint forwarded — v5.44.0 X2)
        _post_findings(agent_type, cwd, findings, branch_hint=branch_hint)
    finally:
        shutdown_tracing()
