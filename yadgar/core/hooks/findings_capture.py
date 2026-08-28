"""Shared Yadgar findings-capture helpers (car #87 → ADR-0156 redesign).

The extract-footer + on-disk collector logic behind LLM-curated subagent
findings capture (ADR-0156). The main-thread ``Stop`` hook checkpoint cadence
injects a prompt that has the MAIN INSTANCE curate pending subagent findings
via its own MCP tools — NO script auto-stores raw bullets to the DB.

Read surface: ``collect_pending_findings`` globs the session's completed
subagent ``.output`` transcripts (``/tmp/claude-*/<slug>/<session-uuid>/tasks/
<agentId>.output`` — a JSONL sidechain whose last assistant message holds the
``## Yadgar findings`` footer), extracts the footer bullets, and returns them
WITHOUT writing anything. ``advance_pending_state`` marks listed transcripts
consumed AFTER the caller has curated + cleaned them up, so a crash between LIST
and CLEANUP re-surfaces pending findings on the next cadence. The
``yadgar pending-findings`` CLI (``yadgar/core/cli/pending_findings.py``) is the
host-side wrapper the checkpoint prompt calls.

ADR-0156 ripped the auto-store path: ``post_findings`` (HTTP POST to
``/hooks/subagent-stop``), ``sweep_subagent_transcripts`` (disk-read + POST),
and the legacy ``SubagentStop`` scripts are GONE. All functions here are pure /
side-effect-free; every path degrades to a no-op on error — subagent capture
must never block a session.
"""

from __future__ import annotations

import json
import os
import re

from yadgar._shared.observability.observe import observe

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
    except (OSError, TypeError, ValueError):  # fmt: skip
        return ""


# ── ADR-0156 — Stop-hook transcript collector (read surface) ───────────────────
#
# On every main-thread Stop checkpoint cadence, enumerate the session's completed
# subagent transcript files and return any ``## Yadgar findings`` footers not yet
# consumed. Dedup keyed on (path → consumed mtime) so a still-partial file is
# re-read once it grows (a background agent's footer lands after it finishes).
# NOTHING is written to the DB here — the main instance curates via its MCP tools.


@observe(tier="stage")
def _tasks_root_default(uid: int | None = None) -> str:
    """Default glob root for subagent .output transcripts.

    Claude Code writes each async subagent's transcript to
    ``/tmp/claude-<uid>/<project-slug>/<session-uuid>/tasks/<agentId>.output``.
    We return the ``/tmp/claude-*`` prefix; the session-uuid is globbed in
    ``collect_pending_findings`` so we do not depend on the slug encoding.
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
    except (OSError, TypeError, ValueError):  # fmt: skip
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
    except (OSError, TypeError, ValueError):  # fmt: skip
        pass


@observe(tier="stage")
def _session_uuid_from_transcript(transcript_path: str) -> str:
    """Return the session-uuid (== transcript filename stem). '' when unusable."""
    import pathlib  # noqa: PLC0415

    if not transcript_path:
        return ""
    try:
        return pathlib.Path(transcript_path).stem
    except TypeError:
        return ""


@observe(tier="stage")
def _default_sweep_state_path() -> str:
    """Return the default dedup state path used by both the stop hook and the CLI.

    Mirrors ``_subagent_sweep_state_path()`` in stop-memory-checkpoint.py:
    ``<XDG_STATE_HOME>/yadgar/subagent-capture-state.json``.  Kept importable
    here so Car B can retire the duplication in the stop hook.
    """
    import pathlib  # noqa: PLC0415

    state_home = os.environ.get("XDG_STATE_HOME") or str(
        pathlib.Path("~/.local/state").expanduser()
    )
    return str(pathlib.Path(state_home) / "yadgar" / "subagent-capture-state.json")


@observe(tier="stage")
def _scan_pending(
    transcript_path: str,
    state_path: str,
    tasks_root: str | None = None,
) -> list[tuple[str, float, list[str]]]:
    """Scan for pending subagent .output transcripts with unread findings footers.

    Returns a list of ``(output_path, mtime, findings_bullets)`` for each
    transcript that has a footer AND has not been recorded in the dedup state.

    READ-ONLY — never writes the state.  Both ``collect_pending_findings`` and
    ``advance_pending_state`` derive their results from this helper so the
    mtime used for dedup is exactly the same value seen at scan time.
    """
    import glob as _glob  # noqa: PLC0415

    try:
        session_uuid = _session_uuid_from_transcript(transcript_path)
        if not session_uuid:
            return []

        root = tasks_root if tasks_root is not None else _tasks_root_default()
        pattern = os.path.join(root, "claude-*", "*", session_uuid, "tasks", "*.output")
        candidates = _glob.glob(pattern)
        if not candidates:
            return []

        state = _load_sweep_state(state_path)
        pending: list[tuple[str, float, list[str]]] = []

        for path in candidates:
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue

            prev = state.get(path)
            if prev is not None and isinstance(prev, (int, float)) and mtime <= prev:
                continue

            report_text = last_assistant_text(path)
            if not report_text:
                continue
            findings = extract_findings(report_text)
            if not findings:
                continue

            pending.append((path, mtime, findings))

        return pending
    except (OSError, TypeError, ValueError):  # fmt: skip
        return []


@observe(tier="boundary")
def collect_pending_findings(
    transcript_path: str,
    cwd: str,
    state_path: str,
    tasks_root: str | None = None,
) -> list[dict]:
    """Return pending subagent findings without advancing the dedup state.

    Car A (ADR-0156) — read surface.  Repurposes ``sweep_subagent_transcripts``
    logic but returns data instead of POSTing.  Caller decides when to consume
    (via ``advance_pending_state``), so a crash between LIST and CLEANUP
    re-surfaces pending findings on the next cadence.

    Args:
        transcript_path: Main session transcript (stem = session-uuid).
        cwd:             Project directory (currently unused; kept for symmetry
                         with ``sweep_subagent_transcripts`` and future use).
        state_path:      Dedup state file (path -> last-captured mtime).
        tasks_root:      Override for ``/tmp/claude-*`` glob root (tests only).

    Returns:
        ``[{"agent_type": str, "findings": [str, ...], "transcript_path": str}]``
        — one entry per new/changed transcript that carries a footer.  Empty
        when there are no pending findings or on any error.
    """
    try:
        pending = _scan_pending(transcript_path, state_path, tasks_root=tasks_root)
        return [
            {
                "agent_type": "general-purpose",
                "findings": findings,
                "transcript_path": path,
            }
            for path, _mtime, findings in pending
        ]
    except (OSError, TypeError, ValueError):  # fmt: skip
        return []


@observe(tier="stage")
def advance_pending_state(pending: list[dict], state_path: str) -> None:
    """Mark all entries returned by collect_pending_findings as consumed.

    Batch-writes the current on-disk mtime for each ``transcript_path`` in
    ``pending`` into the dedup state so a subsequent ``collect_pending_findings``
    call skips them.  Best-effort; never raises.

    Designed to be called AFTER the caller has curated and optionally deleted
    the listed transcripts.
    """
    if not pending:
        return
    try:
        state = _load_sweep_state(state_path)
        for entry in pending:
            path = entry.get("transcript_path", "")
            if not path:
                continue
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            state[path] = mtime
        _save_sweep_state(state_path, state)
    except (AttributeError, OSError, TypeError, ValueError):  # fmt: skip
        pass
