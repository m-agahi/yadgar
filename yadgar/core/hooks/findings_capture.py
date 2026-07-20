"""Shared Yadgar findings-capture helpers (car #87).

The extract-footer + POST-to-endpoint logic used by BOTH subagent-capture paths:

- ``yadgar/core/hooks/subagent_stop.py`` — the legacy ``SubagentStop`` hook body
  (kept for back-compat; ``SubagentStop`` never fires for Agent-tool /
  ``run_in_background`` dispatches — upstream #33049 / #25147 — so it is inert).
- ``yadgar/core/hooks/stop-memory-checkpoint.py`` — the LIVE trigger (car #87):
  the main-thread ``Stop`` hook sweeps completed-subagent transcript files on
  disk and posts their ``## Yadgar findings`` footers.

Car #87 PROBE result (why the trigger moved): for ``run_in_background=true``
Agent-tool dispatches — the orchestrator-mode default and the exact broken case
— the ``PostToolUse`` ``tool_response`` carries ONLY the "Async agent launched"
stub, never the footer. The footer lands later in the subagent's on-disk
transcript (``/tmp/claude-*/<slug>/<session-uuid>/tasks/<agentId>.output`` — a
JSONL sidechain whose last assistant message holds the footer). Option A (this
module's ``sweep_subagent_transcripts``) reads those files on the main-thread
Stop hook. The ``/hooks/subagent-stop`` endpoint + #30 capture counters are
UNCHANGED — this car makes them finally fire.

All functions are pure / side-effect-free except ``post_findings`` (HTTP POST)
and ``sweep_subagent_transcripts`` (reads disk + posts). Every path degrades to
a no-op on error — subagent capture must never block a session.
"""

from __future__ import annotations

import json
import logging
import os
import re

from yadgar._shared.observability.observe import observe

_PORT = os.environ.get("YADGAR_PORT", "8765")
_AUTH_TOKEN = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

logger = logging.getLogger(__name__)

# ── findings-footer extraction ────────────────────────────────────────────────
#
# Lenient heading matcher — any H1–H6 whose text contains both "yadgar" and
# "find" (matches: "## Yadgar findings", "### Yadgar Findings",
# "## Findings (Yadgar)", "## yadgar-findings", "## FINDINGS — YADGAR", etc.)
_HEADING_RE = re.compile(r"^(#{1,6})\s+([^\n]+)$", re.MULTILINE)
# Any ## heading (used to find end of findings section)
_NEXT_HEADING_RE = re.compile(r"\n#{1,6}\s+")
# A bullet line: starts with optional whitespace + "- "
_BULLET_RE = re.compile(r"^\s*-\s+(.+)$", re.MULTILINE)


@observe(tier="hot")
def _auth_headers() -> dict:
    if _AUTH_TOKEN:
        return {"Authorization": f"Bearer {_AUTH_TOKEN}"}
    return {}


@observe(tier="stage")
def extract_findings(text: str) -> list[str]:
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
    """Extract text content from an assistant message dict (helper for _last_assistant_text)."""
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
def last_assistant_text(transcript_path: str) -> str:
    """Return the last assistant turn's text from a JSONL transcript file.

    Works for both the Claude Code SubagentStop transcript and the on-disk
    subagent ``.output`` sidechain (same JSONL shape). Falls back to empty
    string if the file is unavailable or unreadable. Never raises.
    """
    import pathlib  # noqa: PLC0415

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
def detect_branch_from_cwd(cwd: str) -> str | None:
    """Detect the git branch from a directory. Returns branch name or None. Never raises."""
    import subprocess  # noqa: PLC0415

    if not cwd:
        return None
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


@observe(tier="stage")
def post_findings(
    agent_type: str,
    cwd: str,
    findings: list[str],
    branch_hint: str | None = None,
    timeout: float = 3.0,
) -> bool:
    """POST findings to /hooks/subagent-stop endpoint. Returns True on a 2xx-ish POST.

    Same wire shape the legacy SubagentStop path built: findings bullets +
    agent_type + cwd + branch_hint + the ``_subagent_writeback`` signal tag.

    Silently swallows all errors — must never block session completion. Returns
    False on empty findings or any transport error so callers can decide whether
    to mark a transcript captured (car #87 dedup: only mark on a real post).
    """
    import urllib.request  # noqa: PLC0415

    if not findings:
        return False

    payload_dict: dict = {
        "agent_type": agent_type,
        "cwd": cwd,
        "findings": findings,
        "_subagent_writeback": True,
    }
    if branch_hint:
        payload_dict["branch_hint"] = branch_hint

    payload = json.dumps(payload_dict).encode()
    url = f"http://127.0.0.1:{_PORT}/hooks/subagent-stop"
    headers = {"Content-Type": "application/json", **_auth_headers()}
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False  # Daemon down or error — fail silently, never block


# ── car #87 — Stop-hook transcript sweep (Option A) ────────────────────────────
#
# The LIVE trigger. On every main-thread Stop, enumerate the session's completed
# subagent transcript files and post any ``## Yadgar findings`` footers not yet
# captured. Dedup keyed on (path → captured mtime) so a still-partial file is
# re-read once it grows (a background agent's footer lands after it finishes).


@observe(tier="stage")
def _tasks_root_default(uid: int | None = None) -> str:
    """Default glob root for subagent .output transcripts.

    Claude Code writes each async subagent's transcript to
    ``/tmp/claude-<uid>/<project-slug>/<session-uuid>/tasks/<agentId>.output``.
    We return the ``/tmp/claude-*`` prefix; the session-uuid is globbed in
    ``sweep_subagent_transcripts`` so we do not depend on the slug encoding.
    """
    return "/tmp"  # noqa: S108 — Claude Code's own transcript root, not our write


@observe(tier="stage")
def _load_sweep_state(state_path: str) -> dict:
    """Load the sweep dedup state (path -> last-captured mtime). Returns {} on error."""
    import pathlib  # noqa: PLC0415

    try:
        p = pathlib.Path(state_path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@observe(tier="stage")
def _save_sweep_state(state_path: str, state: dict) -> None:
    """Atomically write the sweep dedup state. Best-effort; never raises."""
    import pathlib  # noqa: PLC0415

    try:
        p = pathlib.Path(state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / (p.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception:
        pass


@observe(tier="stage")
def _session_uuid_from_transcript(transcript_path: str) -> str:
    """Return the session-uuid (== transcript filename stem). '' when unusable."""
    import pathlib  # noqa: PLC0415

    if not transcript_path:
        return ""
    try:
        return pathlib.Path(transcript_path).stem
    except Exception:
        return ""


@observe(tier="stage")
def _sidechain_git_branch(transcript_path: str) -> str | None:
    """Read the ``gitBranch`` field from a subagent .output sidechain. None on error."""
    import pathlib  # noqa: PLC0415

    try:
        p = pathlib.Path(transcript_path)
        if not p.exists():
            return None
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            branch = entry.get("gitBranch")
            if branch:
                return str(branch)
    except Exception:
        pass
    return None


@observe(tier="boundary")
def sweep_subagent_transcripts(
    transcript_path: str,
    cwd: str,
    state_path: str,
    tasks_root: str | None = None,
) -> int:
    """Sweep completed-subagent transcript files and post their findings footers.

    Car #87 — the LIVE capture trigger. Called from the main-thread Stop hook on
    EVERY stop (unconditional — NOT gated on the checkpoint interval).

    - ``transcript_path``: the main session transcript (its stem is the session-uuid).
    - ``cwd``: project directory (used as the memory context + branch fallback).
    - ``state_path``: dedup state file (path -> last-captured mtime).
    - ``tasks_root``: override for the ``/tmp/claude-*`` glob root (tests).

    Dedup semantics (advisor #2): re-read a file when never-captured OR its mtime
    advanced (a still-running agent's ``.output`` has no footer yet — retry once
    it grows). Only mark captured after a real post, so a daemon-down sweep
    retries next stop.

    Returns the number of transcript files whose findings were posted this sweep.
    Never raises — every error path degrades to a no-op.
    """
    import glob as _glob  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    try:
        session_uuid = _session_uuid_from_transcript(transcript_path)
        if not session_uuid:
            return 0

        root = tasks_root if tasks_root is not None else _tasks_root_default()
        # /tmp/claude-*/<slug>/<session-uuid>/tasks/*.output
        pattern = os.path.join(root, "claude-*", "*", session_uuid, "tasks", "*.output")
        candidates = _glob.glob(pattern)
        if not candidates:
            return 0

        state = _load_sweep_state(state_path)
        posted_files = 0
        changed = False

        for path in candidates:
            try:
                mtime = os.stat(path).st_mtime  # cheap — stat all, read only new/changed
            except OSError:
                continue

            prev = state.get(path)
            # Re-read when never captured OR mtime advanced (partial-file retry).
            if prev is not None and isinstance(prev, (int, float)) and mtime <= prev:
                continue

            report_text = last_assistant_text(path)
            if not report_text:
                continue
            findings = extract_findings(report_text)
            if not findings:
                # No footer yet (or none). Do NOT mark captured — a growing file
                # may add the footer later; mtime-advance will re-trigger the read.
                continue

            branch_hint = _sidechain_git_branch(path) or detect_branch_from_cwd(cwd)
            # agent_type is not reliably present in the sidechain; endpoint
            # defaults it. Per-file post preserves per-agent memory granularity.
            if post_findings("general-purpose", cwd, findings, branch_hint=branch_hint):
                state[path] = mtime
                posted_files += 1
                changed = True
                logger.debug(
                    "subagent sweep captured file=%s bullets=%d",
                    pathlib.Path(path).name,
                    len(findings),
                )

        if changed:
            _save_sweep_state(state_path, state)
        return posted_files
    except Exception:
        return 0
