"""Shared Yadgar findings-capture helpers (car #87 → ADR-0156 redesign).

The extract-footer + on-disk collector logic behind LLM-curated subagent
findings capture (ADR-0156). The main-thread ``Stop`` hook checkpoint cadence
injects a prompt that has the MAIN INSTANCE curate pending subagent findings
via its own MCP tools — NO script auto-stores raw bullets to the DB.

Read surface: ``collect_pending_findings`` globs the session's completed
subagent ``.output`` transcripts (``<tmp>/claude-*/<slug>/<session-uuid>/tasks/
<agentId>.output``, ``<tmp>`` per ``_tasks_roots_default`` — a JSONL sidechain
whose last assistant message holds the ``## Yadgar findings`` footer), extracts
the footer bullets, and returns them WITHOUT writing anything.
``list_pending_findings`` is the checkpoint LIST: the same scan plus a
per-session snapshot of exactly what was shown. ``advance_listed_state`` marks
that snapshot consumed AFTER the caller has curated it, so a crash between LIST
and advance re-surfaces pending findings on the next cadence, and an agent that
finishes between the two is not consumed unseen. Nothing deletes the
``.output`` files: dedup is keyed on mtime, so a delete adds nothing. The
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
    if not isinstance(entry, dict):
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
def _tasks_roots_default() -> list[str]:
    """Default glob roots for subagent .output transcripts, in priority order.

    Claude Code writes each async subagent's transcript to
    ``<root>/claude-<uid>/<project-slug>/<session-uuid>/tasks/<agentId>.output``
    where ``<root>`` is ``$CLAUDE_CODE_TMPDIR`` when set, else Node's
    ``os.tmpdir()`` (``$TMPDIR``, then ``$TMP``, then ``$TEMP``, then ``/tmp``).

    This CLI runs as a Bash child of the harness. A sandboxed Bash child gets
    its own ``TMPDIR``, which is not the harness's, so the roots are returned
    as a UNION (every distinct candidate, deduped by realpath) instead of a
    first hit. That is safe: the glob pattern is pinned to the session uuid.
    """
    candidates: list[str] = []
    cc_tmp = os.environ.get("CLAUDE_CODE_TMPDIR")
    if cc_tmp:
        candidates.append(cc_tmp)
    node_tmp = os.environ.get("TMPDIR") or os.environ.get("TMP") or os.environ.get("TEMP")
    if node_tmp:
        candidates.append(node_tmp)
    candidates.append("/tmp")  # noqa: S108 — Claude Code's own transcript root, not our write

    roots: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        root = raw.rstrip("/") or raw
        key = os.path.realpath(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


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
def _write_json_atomic(path: str, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` via tmp file + ``os.replace``. May raise OSError."""
    import pathlib  # noqa: PLC0415

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(p))


@observe(tier="stage")
def _save_sweep_state(state_path: str, state: dict) -> None:
    """Atomically write the sweep dedup state. Best-effort; never raises.

    Prunes keys whose path no longer exists (``os.path.lexists`` is False), so
    the state does not grow without bound. A dangling symlink still counts as
    present: its key stays until the link itself is gone.
    """
    try:
        live = {k: v for k, v in state.items() if isinstance(k, str) and os.path.lexists(k)}
        _write_json_atomic(state_path, live)
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

    READ-ONLY — never writes the state.  The mtime is stat'ed BEFORE the file is
    read, so a target that grows between the two re-lists next time (duplicate,
    never loss).  ``list_pending_findings`` snapshots exactly these mtimes.

    ``tasks_root`` set → that root only (``YADGAR_TASKS_ROOT``, tests).  Unset →
    every root from ``_tasks_roots_default``.
    """
    import glob as _glob  # noqa: PLC0415

    try:
        session_uuid = _session_uuid_from_transcript(transcript_path)
        if not session_uuid:
            return []

        roots = [tasks_root] if tasks_root is not None else _tasks_roots_default()
        candidates: list[str] = []
        for root in roots:
            pattern = os.path.join(root, "claude-*", "*", session_uuid, "tasks", "*.output")
            candidates.extend(p for p in _glob.glob(pattern) if p not in candidates)
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
    """Return pending subagent findings without writing anything.

    Car A (ADR-0156) — read surface.  PURE: writes neither the dedup state nor
    the LIST snapshot, so other readers (``session-end-capture``) can call it
    freely.  The checkpoint LIST goes through ``list_pending_findings``.

    Args:
        transcript_path: Main session transcript (stem = session-uuid).
        cwd:             Project directory (currently unused; kept for symmetry
                         with ``sweep_subagent_transcripts`` and future use).
        state_path:      Dedup state file (path -> last-captured mtime).
        tasks_root:      Pin the glob to this one root (``YADGAR_TASKS_ROOT``,
                         tests).  ``None`` → ``_tasks_roots_default()``.

    Returns:
        ``[{"agent_type": str, "findings": [str, ...], "transcript_path": str}]``
        — one entry per new/changed transcript that carries a footer.  Empty
        when there are no pending findings or on any error.
    """
    try:
        pending = _scan_pending(transcript_path, state_path, tasks_root=tasks_root)
        return _as_entries(pending)
    except (OSError, TypeError, ValueError):  # fmt: skip
        return []


@observe(tier="hot")
def _as_entries(pending: list[tuple[str, float, list[str]]]) -> list[dict]:
    """Shape ``_scan_pending`` tuples as the public entry dicts."""
    return [
        {
            "agent_type": "general-purpose",
            "findings": findings,
            "transcript_path": path,
        }
        for path, _mtime, findings in pending
    ]


@observe(tier="stage")
def _listed_snapshot_path(state_path: str, session_uuid: str) -> str:
    """Per-session LIST snapshot file, next to the dedup state file."""
    directory = os.path.dirname(state_path) or "."
    return os.path.join(directory, f"pending-findings-listed-{session_uuid}.json")


@observe(tier="boundary")
def list_pending_findings(
    transcript_path: str,
    cwd: str,
    state_path: str,
    tasks_root: str | None = None,
) -> list[dict]:
    """LIST for the checkpoint: scan, then snapshot exactly what was shown.

    Writes ``{output_path: mtime-as-listed}`` to the per-session snapshot
    (``_listed_snapshot_path``) so ``advance_listed_state`` consumes the listed
    set and nothing else.  An empty LIST deletes any older snapshot, so a later
    advance cannot consume a stale set.  The dedup state is NOT written.
    Snapshot I/O is best-effort: a failure means the next advance is a no-op
    and the findings re-list (duplicate, never loss).
    """
    session_uuid = _session_uuid_from_transcript(transcript_path)
    pending = _scan_pending(transcript_path, state_path, tasks_root=tasks_root)
    if session_uuid:
        snapshot = _listed_snapshot_path(state_path, session_uuid)
        try:
            if pending:
                _write_json_atomic(snapshot, {path: mtime for path, mtime, _ in pending})
            elif os.path.lexists(snapshot):
                os.unlink(snapshot)
        except (OSError, TypeError, ValueError):  # fmt: skip
            pass
    return _as_entries(pending)


@observe(tier="stage")
def advance_listed_state(transcript_path: str, state_path: str) -> int:
    """Mark the set the last LIST showed as consumed; return how many paths.

    Reads the per-session snapshot written by ``list_pending_findings``, merges
    its listed mtimes into the dedup state, saves, then deletes the snapshot.
    No re-scan: an agent that finished after LIST stays pending, and a target
    that grew after LIST re-lists (its recorded mtime is the listed one).
    No snapshot → no-op, returns 0.  Best-effort; never raises.
    """
    session_uuid = _session_uuid_from_transcript(transcript_path)
    if not session_uuid:
        return 0
    snapshot = _listed_snapshot_path(state_path, session_uuid)
    listed = _load_sweep_state(snapshot)
    if not listed:
        return 0
    try:
        state = _load_sweep_state(state_path)
        count = 0
        for path, mtime in listed.items():
            if isinstance(path, str) and isinstance(mtime, (int, float)):
                state[path] = mtime
                count += 1
        _save_sweep_state(state_path, state)
        os.unlink(snapshot)
        return count
    except (OSError, TypeError, ValueError):  # fmt: skip
        return 0
