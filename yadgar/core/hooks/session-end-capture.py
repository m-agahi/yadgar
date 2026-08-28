#!/usr/bin/env python3
"""Yadgar SessionEnd hook — writes session_end_sentinel filesystem marker.

Fires on exit/Ctrl-D. Observational only — no blocking, no injecting prompts.

Flow:
  1. Read JSON from stdin (end_reason, cwd, transcript_path, session_id).
  2. If SESSION_END_CAPTURE_ENABLED=false → exit 0 immediately.
  3. If end_reason in ("clear", "resume") → exit 0 (not a true exit).
  4. If message_count < SESSION_END_MIN_MESSAGES → exit 0 (trivial session).
  5. Write sentinel to YADGAR_SESSION_END_DIR (or ~/.local/state/yadgar/session-ends/) atomically.

The sentinel is read-once: SessionStart's hook_session_context imports it into memory
and deletes it. If the daemon is down at exit time, the filesystem write survives —
no daemon dependency.

Env knobs:
  SESSION_END_CAPTURE_ENABLED  true   Kill switch
  SESSION_END_MIN_MESSAGES     2      Min human messages before writing sentinel
  SESSION_END_SNIPPET_TURNS    5      Max human turns to embed in sentinel
  YADGAR_SESSION_END_DIR       ~/.local/state/yadgar/session-ends  Override sentinel dir (testing)
"""

# DC2-DECISION: KEEP-CURATED (train-bug-bag-2 DC2, task #35)
#
# The session-exit sentinel writes a CURATED snippet, not the raw transcript.
# The audit considered two paths:
#
#   A. KEEP curated + ANNOTATE  (this implementation)  ← chosen
#   B. SWITCH to raw save with secret-scrub             ← rejected
#
# WHY A (kept):
#   1. The hook fires at process exit, observational-only, and cannot block.
#      Any secret-scrub has to be regex over arbitrary text — known losing game.
#   2. Transcripts carry real secrets (Bearer tokens, API keys, customer PII).
#      Persisting the raw transcript (or a "scrubbed" copy of unknown fidelity)
#      to ~/.yadgar/session-ends/ and then into memory via memorize() widens
#      the secret surface across a filesystem marker AND a memory row.
#   3. The transcript path is already in the sentinel record. The next
#      session's LLM can re-read the transcript directly when it still exists.
#   4. The snippet is ROTATION-RESILIENCE context for the synthesising LLM
#      in the next session, not the primary signal. The primary signal is
#      the SessionStart `extract_last_session_findings` recommended action
#      that points at the transcript path.
#   5. Last human turns have the highest semantic density per byte (encode
#      intent). Last touched file paths are cheap and high-signal (encode
#      "we were working on X.py"). Assistant turns and tool names are verbose
#      and low-density — excluded by design.
#
# WHY B was rejected:
#   - Secret-scrub cannot be made safe enough at hook runtime. The hook is
#     observational-only (cannot block); the next best alternative is to
#     store nothing secret-bearing, which the curated path already does.
#   - The raw path duplicates information already available at the
#     transcript_path field. Storing it twice widens the blast radius of
#     any leak without giving the next session's LLM new information.
#   - A scrubbed-but-still-rich save is the worst of both worlds: large
#     enough to leak fragments, small enough to be a maintenance burden.
#
# CAPS (restated so a maintainer can audit the constants against the decision):
#   - 5 human turns max (SESSION_END_SNIPPET_TURNS, default 5)
#   - 500 bytes per turn (max_per, see _cap_turns)
#   - 4096 bytes total for last_human_turns (max_total, see _cap_turns)
#
# REVISIT TRIGGER (when to re-audit this decision):
#   - The next session's LLM is observed (via recall() of synthesized
#     findings, or via manual review) to MISS a key fact that was in the
#     raw transcript but not the snippet. If that happens ≥2 times in a
#     30-day window, the cap is too tight — raise 4096→8192 and 5→10
#     turns BEFORE switching to raw. Snippet growth is a one-way ratchet.
#   - A host-deployable secret-scrub library (not regex) becomes available
#     AND proves ≥99% recall on a benchmark of real yadgar transcripts.
#     Until then, B is rejected. (No env-var runtime switch — DC2 spec.)
#   - SessionEnd gains the ability to BLOCK, changing the threat model.
#
# If this block is removed, the audit test in
# yadgar/tests/core/test_session_end_capture_dc2_audit.py will fail and
# the next reviewer will be forced to re-decide.

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe

try:
    from yadgar.core.hooks._identity_mint import resolve_session_project
except ImportError:  # yadgar not importable from this interpreter
    resolve_session_project = None

# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

ENABLED = os.environ.get("SESSION_END_CAPTURE_ENABLED", "true").lower() not in (
    "false",
    "0",
    "no",
)
if not ENABLED:
    sys.exit(0)

# ---------------------------------------------------------------------------
# Parse stdin
# ---------------------------------------------------------------------------

try:
    data = json.loads(sys.stdin.read() or "{}")
except (OSError, ValueError):  # fmt: skip
    data = {}

cwd = data.get("cwd", os.getcwd())
transcript_path = data.get("transcript_path", "")
end_reason = data.get("end_reason", "other")
session_id = data.get("session_id", "unknown")

# ---------------------------------------------------------------------------
# Gate: skip on clear / resume (not a true exit)
# ---------------------------------------------------------------------------

SKIP_REASONS = frozenset({"clear", "resume"})

# ---------------------------------------------------------------------------
# Slash-command tag filter — tags injected by Claude Code as part of slash-command
# processing. These are NOT genuine user turns and must be excluded from
# last_human_turns to keep the sentinel's human-context signal clean.
# Extend this set when new slash-command tag patterns are observed.
# ---------------------------------------------------------------------------

SKIP_TAGS: frozenset[str] = frozenset(
    {
        "system-reminder",
        "command-message",
        "command-name",
        "command-args",
        "local-command-caveat",
        "local-command-stdout",
        "local-command-stderr",
    }
)
if end_reason in SKIP_REASONS:
    sys.exit(0)

# ---------------------------------------------------------------------------
# Helper: count human messages in transcript
# ---------------------------------------------------------------------------


@observe(tier="hot")
def _count_human_messages(tp: str) -> int:
    """Count genuine user turns in JSONL transcript (skip system injections)."""
    p = Path(tp)
    if not p.exists():
        return 0
    count = 0
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
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and any(f"<{tag}>" in content for tag in SKIP_TAGS):
            continue
        if (
            isinstance(content, list)
            and content
            and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        ):
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# Gate: skip short sessions
# ---------------------------------------------------------------------------

MIN_MESSAGES = int(os.environ.get("SESSION_END_MIN_MESSAGES", "2"))
message_count = _count_human_messages(transcript_path) if transcript_path else 0
if message_count < MIN_MESSAGES:
    sys.exit(0)

# ---------------------------------------------------------------------------
# Helper: extract last N human turn texts
# ---------------------------------------------------------------------------


@observe(tier="hot")
def _parse_user_content(content) -> str | None:
    """Extract text from a user message content field. Returns None to skip."""
    if isinstance(content, str):
        if any(f"<{tag}>" in content for tag in SKIP_TAGS):
            return None
        return content
    if isinstance(content, list):
        if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text_parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        combined = " ".join(p for p in text_parts if p)
        return combined if combined else None
    return None


@observe(tier="hot")
def _cap_turns(turns: list[str], n: int, max_per: int = 500, max_total: int = 4096) -> list[str]:
    """Take last n turns, truncating each to max_per bytes, total to max_total bytes."""
    last_n = turns[-n:] if len(turns) > n else turns
    out: list[str] = []
    total = 0
    for turn in reversed(last_n):
        chunk = turn[:max_per]
        encoded_len = len(chunk.encode("utf-8"))
        if total + encoded_len > max_total:
            break
        out.insert(0, chunk)
        total += encoded_len
    return out


@observe(tier="hot")
def _extract_last_human_turns(tp: str, n: int) -> list[str]:
    """Return the last N human turn content strings from transcript.

    Skips system injections. Each turn truncated to 500 bytes.
    Total capped at 4096 bytes.
    """
    p = Path(tp)
    if not p.exists():
        return []

    turns: list[str] = []
    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message", entry)
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _parse_user_content(msg.get("content", ""))
        if text is not None:
            turns.append(text)

    return _cap_turns(turns, n)


# ---------------------------------------------------------------------------
# Helper: extract last touched file paths from ToolUse entries
# ---------------------------------------------------------------------------


@observe(tier="hot")
def _extract_last_touched_files(tp: str, n: int) -> list[str]:
    """Return last N unique file paths from Read/Edit/Write ToolUse entries."""
    FILE_TOOLS = frozenset({"Read", "Edit", "Write"})
    p = Path(tp)
    if not p.exists():
        return []

    seen: list[str] = []
    seen_set: set[str] = set()

    # Scan last 100 entries for efficiency
    all_lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    for raw in all_lines[-100:]:
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
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            if block.get("name") not in FILE_TOOLS:
                continue
            inp = block.get("input", {})
            fpath = inp.get("file_path", "") or inp.get("path", "")
            if fpath and fpath not in seen_set:
                seen_set.add(fpath)
                seen.append(fpath)

    # Return last n, newest-first (reverse order of seen)
    unique_last = list(reversed(seen))
    return unique_last[:n]


# ---------------------------------------------------------------------------
# Gather data
# ---------------------------------------------------------------------------

SNIPPET_TURNS = int(os.environ.get("SESSION_END_SNIPPET_TURNS", "5"))
last_human_turns = (
    _extract_last_human_turns(transcript_path, n=SNIPPET_TURNS) if transcript_path else []
)
last_touched_files = _extract_last_touched_files(transcript_path, n=3) if transcript_path else []


# ---------------------------------------------------------------------------
# Straggler safety-net (ADR-0156 §4): a session that exits before a checkpoint
# leaves its subagent findings orphaned — the next session's uuid differs, so the
# collector (scoped to the current session-uuid) never re-surfaces them. Record
# any pending findings into the sentinel so a future consumer (#97) can adopt
# them. This is a MECHANICAL raw write to the sentinel FILE (not the DB) —
# ADR-0156 permits it for the exit hook (no LLM turn available here). Consumption
# is deferred to #97; this hook only records.
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _collect_straggler_findings(tp: str, project_cwd: str) -> list[dict]:
    """Return pending subagent findings for the exiting transcript. [] on error."""
    if not tp:
        return []
    try:
        from yadgar.core.hooks.findings_capture import (
            _default_sweep_state_path,
            collect_pending_findings,
        )

        tasks_root = os.environ.get("YADGAR_TASKS_ROOT") or None
        return collect_pending_findings(
            tp, project_cwd, _default_sweep_state_path(), tasks_root=tasks_root
        )
    except (ImportError, OSError):  # fmt: skip
        return []


pending_findings = _collect_straggler_findings(transcript_path, cwd)


# ---------------------------------------------------------------------------
# Mint the session's project_id (Car F9, ADR-0227)
#
# The sentinel is consumed by the daemon at the NEXT SessionStart, inside a
# container with no git binary and no host project mounts. That process cannot
# derive an identity and must not try — so the identity has to travel IN the
# record, minted here, at a host-side hook entry point (the only category
# ADR-0227 permits to mint).
#
# ``cwd`` is NOT a fallback for it. An unmintable tree yields ``None``, the
# sentinel is still written (losing the session's exit record would be a worse
# failure than an unstorable one), and the importer rejects it loudly rather
# than inventing a key that would be indistinguishable from a real one.
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _mint_project_id(project_cwd: str) -> str | None:
    """Return the minted project_id for *project_cwd*, or None. Never raises."""
    if resolve_session_project is None:
        return None
    try:
        project_id, notice = resolve_session_project(project_cwd)
    except Exception:  # noqa: BLE001 — a mint crash must not lose the sentinel
        return None
    if project_id is None and notice:
        # Loud on stderr: SessionEnd hook stdout is not injected anywhere, and a
        # silently identity-less sentinel is exactly the failure this car exists
        # to stop being invisible.
        print(notice, file=sys.stderr)
    return project_id


project_id = _mint_project_id(cwd)

# ---------------------------------------------------------------------------
# Write sentinel atomically
# ---------------------------------------------------------------------------

sentinel_dir_env = os.environ.get("YADGAR_SESSION_END_DIR", "")
sentinel_dir = Path(sentinel_dir_env) if sentinel_dir_env else _paths.SESSION_ENDS_DIR

sentinel_dir.mkdir(parents=True, exist_ok=True)

record: dict = {
    "type": "session_end_sentinel",
    "version": 1,
    "cwd": cwd,
    # Car F9: the scope key. Always present so a consumer can tell "no identity
    # was mintable" from "this record predates the field"; never derived.
    "project_id": project_id,
    "end_reason": end_reason,
    "ended_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
    "transcript_path": transcript_path,
    "session_id": session_id,
    "message_count": message_count,
    "last_human_turns": last_human_turns,
    "last_touched_files": last_touched_files,
    "pending_findings": pending_findings,
}

marker_path = sentinel_dir / f"{session_id}.json"
tmp_path = marker_path.with_suffix(".json.tmp")
tmp_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
tmp_path.rename(marker_path)  # atomic on POSIX
