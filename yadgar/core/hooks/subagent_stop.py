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

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import shutdown_tracing

# car #87: the extract-footer + POST helpers live in the shared module so BOTH
# capture paths (this legacy SubagentStop body + the live Stop-hook sweep) use
# one implementation. Re-export the pre-existing private names so importers +
# the characterization suite (which import _extract_findings / _post_findings
# from THIS module) are byte-unaffected.
from yadgar.core.hooks.findings_capture import (  # noqa: F401
    _auth_headers,
)
from yadgar.core.hooks.findings_capture import (
    detect_branch_from_cwd as _detect_branch_from_cwd,
)
from yadgar.core.hooks.findings_capture import (
    extract_findings as _extract_findings,
)
from yadgar.core.hooks.findings_capture import (
    last_assistant_text as _last_assistant_text,
)
from yadgar.core.hooks.findings_capture import (
    post_findings as _post_findings,
)

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


@observe(tier="stage")
def _get_report_text(data: dict) -> str:
    """Extract the agent's final report text from the SubagentStop payload.

    Claude Code SubagentStop does not include the full report text directly
    in the payload — reads the last assistant turn from the transcript JSONL
    (delegated to the shared ``last_assistant_text`` helper). Falls back to
    empty string if the transcript is unavailable.
    """
    return _last_assistant_text(data.get("transcript_path", ""))


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
