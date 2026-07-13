"""PreCompact transcript parse — compute in-flight orchestration state.

Claude Code passes ``transcript_path`` (the session JSONL) on the PreCompact
hook payload. This module parses it and returns the orchestration state that is
*in flight* at compaction time so ``pre_compact_drain`` can persist it into the
checkpoint and ``restore()`` can surface it after compaction.

LAYER (Car fix-drain-inflight, v5.135): this module lives in ``_shared`` — NOT
``backend`` — because the parse must run HOST-SIDE. In the containerized deploy
the backend container mounts only ``/data`` + config: it can see NEITHER the
host ``.claude`` transcripts NOR the git worktree tree, so parsing there always
yields an empty result (the exact inertness this fix removes). The host-side
drain callers (``yadgar.core.cli._shared.forward_pre_compact_drain`` and the
PreCompact hook runner) import this module, parse in_flight where the paths ARE
visible, and carry the result in the /admin payload. The backend imports the
same module as a fallback for the embedded/dev deploy where the paths are local.
``_shared`` is importable by both core and backend; ``backend`` is not reachable
from core — hence the move.

Algorithm (verified against real transcripts, 2026-07-13)::

    in_flight = launched - terminal

    launched = { toolUseResult.agentId : status == "async_launched" }
        BACKGROUND dispatches ONLY. A foreground/synchronous agent echoes an
        `agentId:` token in a *completed non-async* toolUseResult and emits NO
        <task-notification>; counting it fabricates in-flight entries. The strict
        `async_launched`-status filter excludes it.

    terminal = { <task-id> : <task-notification> whose <status> is terminal }
        terminal ∈ {completed, failed, killed, stopped}. `running` is NOT
        terminal — a running agent is still in flight. (Status set enumerated
        from real transcripts: completed / failed / killed / stopped / running.)

Also captures ``run_in_background`` bash shells (``toolUseResult.backgroundTaskId``).

Liveness honesty: from a frozen transcript we can only observe that no terminal
notification was seen — not that the agent is still running. The result carries
a caveat and restore() surfaces it as "were in flight — verify," never as fact.

The parser is pure + fail-soft: a malformed line is skipped, a missing/None path
returns an empty result, and no failure ever raises into the drain path.
"""

from __future__ import annotations

import json
import logging
import re

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# Terminal <status> values — an agent with any of these has finished.
# Enumerated from real transcripts: completed / failed / killed / stopped / running.
# `running` is deliberately EXCLUDED (still in flight).
_TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})

# <task-id>X</task-id> ... <status>Y</status> inside a <task-notification> block.
_TASK_ID_RE = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_STATUS_RE = re.compile(r"<status>\s*([^<\s]+)\s*</status>")

_NOTE = "dispatched at compaction; liveness unverified — verify before relying"

# Hard cap on lines scanned to bound parse cost on very large transcripts.
_MAX_LINES = 200_000


# Bound the recursive string walk so a pathological deeply-nested entry can't
# blow the stack. Transcript entries are shallow (≤ ~6 levels observed).
_MAX_WALK_DEPTH = 12


@observe(exempt="hot-loop: recursive per-node string walk; a span per node would flood traces")
def _walk_strings(value, depth: int, out: list[str]) -> None:
    """Recursively collect every string value reachable in a JSON structure.

    Shape-agnostic on purpose: completion notifications appear in different
    places across entry types — ``user.message.content`` (str or list of text
    blocks), ``queue-operation`` top-level ``content`` (str), and
    ``attachment.attachment.prompt`` (nested dict). A recursive string walk
    reaches all of them and is resilient to Claude Code transcript schema drift.
    """
    if depth > _MAX_WALK_DEPTH:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _walk_strings(v, depth + 1, out)
    elif isinstance(value, list):
        for item in value:
            _walk_strings(item, depth + 1, out)


@observe(tier="hot")
def _collect_text_blocks(entry: dict) -> list[str]:
    """Return every string reachable anywhere in a transcript entry.

    Used only to find ``<task-notification>`` blocks, so an over-broad collection
    is harmless — non-notification strings are filtered out by the caller.
    """
    out: list[str] = []
    _walk_strings(entry, 0, out)
    return out


@observe(tier="hot")
def _load_line(raw: str) -> dict | None:
    """Parse one JSONL line → dict entry, or None (blank / malformed / non-dict)."""
    line = raw.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:  # JSONDecodeError is a ValueError subclass
        return None  # skip malformed line — fail-soft
    return entry if isinstance(entry, dict) else None


@observe(tier="hot")
def _process_entry(
    entry: dict, launched: set[str], terminal: set[str], bg_shells: list[str], seen_shells: set[str]
) -> None:
    """Fold a single transcript entry into the launched/terminal/bg_shells sets."""
    tur = entry.get("toolUseResult")
    if isinstance(tur, dict):
        # Background agent launch ack — STRICT async_launched filter.
        if tur.get("status") == "async_launched":
            aid = tur.get("agentId")
            if isinstance(aid, str) and aid:
                launched.add(aid)
        # run_in_background bash shell.
        shell = tur.get("backgroundTaskId")
        if isinstance(shell, str) and shell and shell not in seen_shells:
            seen_shells.add(shell)
            bg_shells.append(shell)

    # Completion notifications live in plain-text blocks.
    for txt in _collect_text_blocks(entry):
        if "<task-notification>" not in txt:
            continue
        tid_m = _TASK_ID_RE.search(txt)
        st_m = _STATUS_RE.search(txt)
        if tid_m and st_m and st_m.group(1) in _TERMINAL_STATUSES:
            terminal.add(tid_m.group(1))


@observe(tier="stage")
def _list_worktrees(directory: str) -> list[str]:
    """Return ``git worktree list`` entries as "path (branch)" strings.

    Best-effort — a non-git directory, missing git, or timeout yields []. Never
    raises (the drain path must not be blocked by a git failure).

    Moved from ``backend.restoration.checkpoint_restore`` (Car fix-drain-inflight)
    so the HOST-SIDE drain callers can capture worktrees where the git tree is
    actually visible; in-container ``git -C host_path`` returns nonzero → [].
    """
    import subprocess  # noqa: PLC0415

    if not directory:
        return []
    try:
        out = subprocess.run(
            ["git", "-C", directory, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # best-effort worktree capture; never blocks the drain
        return []
    if out.returncode != 0:
        return []
    entries: list[str] = []
    path = ""
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
        elif line.startswith("branch "):
            branch = line[len("branch ") :].strip().replace("refs/heads/", "")
            entries.append(f"{path} ({branch})")
            path = ""
        elif line == "" and path:
            entries.append(path)
            path = ""
    if path:
        entries.append(path)
    return entries


@observe(tier="stage")
def parse_in_flight(transcript_path: str | None) -> dict:
    """Parse a session transcript JSONL and return in-flight orchestration state.

    Returns a dict::

        {"agents": [...], "bg_shells": [...], "worktrees": [], "note": "..."}

    ``worktrees`` is left empty here — it is filled by ``capture_in_flight`` (or
    the drain caller) from ``git worktree list`` (not present in the transcript).
    On any failure the result degrades to empty lists; the parser never raises.
    """
    launched: set[str] = set()
    terminal: set[str] = set()
    bg_shells: list[str] = []
    seen_shells: set[str] = set()

    if not transcript_path:
        return _empty()

    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh):
                if lineno >= _MAX_LINES:
                    logger.warning(
                        "transcript_parse: line cap %d hit for %s", _MAX_LINES, transcript_path
                    )
                    break
                entry = _load_line(raw)
                if entry is not None:
                    _process_entry(entry, launched, terminal, bg_shells, seen_shells)
    except OSError:
        # Missing / unreadable transcript — degrade to empty, never raise.
        return _empty()

    in_flight = sorted(launched - terminal)
    # Background bash shells complete via the SAME <task-notification> channel as
    # agents (verified on real transcripts: 242/251 shells carried a terminal
    # notification whose <task-id> == the shell id). Subtract them so a finished
    # bg-bash is not falsely surfaced as in-flight. A shell with no terminal
    # notification (still running, or lagged past the freeze) stays.
    live_shells = sorted(set(bg_shells) - terminal)

    if launched and not in_flight:
        # Drift canary: a non-empty launched set that fully resolves may be a
        # completed session (expected) OR a parse-shape drift. Log for triage.
        logger.debug(
            "transcript_parse: %d launched, 0 in-flight for %s", len(launched), transcript_path
        )

    return {
        "agents": in_flight,
        "bg_shells": live_shells,
        "worktrees": [],
        "note": _NOTE,
    }


@observe(tier="stage")
def capture_in_flight(transcript_path: str | None, directory: str) -> dict:
    """Host-side combined capture: parse transcript + list worktrees.

    This is the single entry point both host-side drain callers use. It runs
    where the host filesystem is visible (the CLI process / the hook runner), so
    the transcript parse AND ``git worktree list`` both succeed — the whole point
    of Car fix-drain-inflight. Never raises: parse degrades to empty lists, the
    worktree capture degrades to [].

    Returns the same shape as ``parse_in_flight`` with ``worktrees`` populated.
    """
    result = parse_in_flight(transcript_path)
    result["worktrees"] = _list_worktrees(directory)
    return result


@observe(tier="hot")
def _empty() -> dict:
    return {"agents": [], "bg_shells": [], "worktrees": [], "note": _NOTE}
