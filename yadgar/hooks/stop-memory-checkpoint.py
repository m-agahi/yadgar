#!/usr/bin/env python3
"""Yadgar stop hook — periodic signal-evaluation checkpoint (§27).

Fires every INTERVAL human messages and prompts Claude to evaluate signals
via project_brief() and take action (wiki regen, active_work refresh, etc.).

This hook is a DUMB PIPE — no Python signal detection, no API calls.
All evaluation happens in the Claude session via tool calls.

State: ~/.local/state/yadgar/stop-hook-state.json (keyed by session_id, atomic writes).

Output: JSON to stdout.
  {"decision": "block", "reason": "..."} — inject signal-eval prompt
  {}                                      — allow stop normally
"""

import json
import os
import sys
from pathlib import Path

import yadgar.paths as _paths

INTERVAL = 25  # human messages between checkpoints

_PROMPT_TEMPLATE = """\
Yadgar checkpoint. CAPTURE FIRST (steps 1-2), then maintenance (steps 3-4).
Decisions and findings scroll out of context and are lost forever; maintenance
signals re-fire next checkpoint. Capture is the irreplaceable work — if you must
triage anything away under length pressure, drop maintenance, NEVER capture.

1. ADR CAPTURE (always run; the Yadgar wiki is the source of truth — no file,
   works for non-git projects too).
   Page: slug "{project}-adr-log", tag "adr", scoped to this directory.
   - Find it: wiki_read("{project}-adr-log", directory="{directory}"). If absent,
     create with wiki_add(title="{project} ADR Log", content="<one-line header>",
     tags=["adr"], directory="{directory}", wait=True). NOTE: wiki_add commits
     directly — do NOT call wiki_approve (it only promotes drafts and errors on a
     live page).
   - Scan THIS session for durable decisions since the last checkpoint.
     KEEP (precision over recall): a clear durable decision — architecture, a
     tool/config choice, an approach committed-to, a scope cut; a conclusion we
     commit to and stop investing in (NOT a passing status report); a fix that
     changes an approach or contract. A user "record this" ALWAYS qualifies.
     SKIP: routine work (git push, branch cleanup, progress/status checks),
     in-flux or abandoned ideas, pure status ("tests pass"), routine corrections
     (typos, lint).
   - Read existing entries FIRST; append ONLY new decisions (dedup by decision,
     not wording).
   - Entry schema — ALL fields MANDATORY (write "none"/"n/a" if truly empty,
     never omit, so it stays machine-parseable):
       ADR-NNNN — <title>   (NNNN = 4-digit zero-padded, project-sequential: ADR-0001, ADR-0002, …)
       status:          open | accepted | superseded | rejected | deprecated
       date:            <ISO date>
       context:         <what triggered this decision>
       decision:        <what was decided>
       rationale:       <why — the reasoning>
       alternatives:    <options considered + why rejected; "none">
       consequences:    <trade-offs / costs / caveats / flags>
       revisit_trigger: <condition to reconsider>
       supersedes:      ADR-NNNN | none
     A decision still unresolved this session → status: open, with the pending
     question in revisit_trigger; confirm/close it in a later checkpoint.
   - Append via wiki_append_section("{project}-adr-log", section_heading="ADR-NNNN: <title>",
     content=<the fields above>, directory="{directory}", wait=True). Verify with
     wiki_history("{project}-adr-log", directory="{directory}").

2. STRUCTURAL WRITE-BACK (always consider). Durable repo-structure / convention /
   module-purpose findings from THIS session → the EXISTING wiki page that owns
   the topic (wiki_list → slug → wiki_read; update via wiki_add(replace_slug=<slug>,
   ..., directory="{directory}", wait=True); no near-duplicate pages). If no page
   fits, create one with wiki_add(tags=[...], directory="{directory}", wait=True).
   Verify wiki_history. Facts/structure only — decisions go in step 1.

3. Call project_brief("{directory}", mode="signals").

4. MAINTENANCE — for each entry in recommended_actions:
   - ANCHOR HYGIENE: if any of audit_anchors / forget_expired_anchors /
     merge_redundant_anchors / promote_anchor_to_wiki appear, handle them with ONE
     flow: audit_anchors("{directory}", dry_run=True) → review →
     audit_anchors("{directory}", dry_run=False) to apply forgets/merges. The tool
     self-guards (never drops semantic_immortal or protected-legacy anchors). For
     any promote draft it returns, wiki_add it only if wiki-worthy (step-2 rules),
     else skip. Run this flow at most once.
   - Else if the action has a `suggested_call`: run it verbatim, supplying content
     from THIS session for placeholders (content='...', key_decisions=[...]) — the
     suggested_call is the exact shape; supply only the content, don't invent it.
     (Covers refresh_active_work, consider_refresh_active_work, refresh_checkpoint,
     consider_refresh_checkpoint, extract_last_session_findings, update_roadmap,
     review_rejections.)
   - bootstrap_project (no suggested_call): propose a <=1500-char project-summary
     memory, then bootstrap_project("{directory}", content).
   - Any action type NOT covered above AND with no suggested_call → SKIP and flag
     it in your reply (do not improvise the mechanics).

[yadgar] Checkpoint cadence reached — capture, then continue. If you were
mid-thought, repeat your last question so the conversation continues. Resume after
/clear or session end: restore(directory="{directory}").
"""


def _count_human_messages(transcript_path: str) -> int:
    """Count human (user) turns in the JSONL transcript.

    Skips system-injected turns (<system-reminder>, <command-message>).
    Handles both flat and nested Claude Code transcript formats.
    """
    p = Path(transcript_path)
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

        # Nested format: {"message": {"role": "user", "content": "..."}, ...}
        # Flat format:   {"role": "user", "content": "..."}
        msg = entry.get("message", entry)
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue

        content = msg.get("content", "")
        if isinstance(content, str) and (
            "<system-reminder>" in content or "<command-message>" in content
        ):
            continue
        # List content that is only tool results — skip
        if (
            isinstance(content, list)
            and content
            and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        ):
            continue

        count += 1

    return count


def _state_file_path() -> Path:
    """Return path to stop-hook-state.json under XDG state dir."""
    return _paths.STOP_HOOK_STATE_PATH


def _load_state() -> dict:
    """Load the global stop-hook state dict. Returns {} on any error."""
    sf = _state_file_path()
    if not sf.exists():
        return {}
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomically write state dict to stop-hook-state.json (tmp + os.replace)."""
    sf = _state_file_path()
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        tmp = sf.parent / (sf.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(sf))
    except Exception:
        pass


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")
    stop_hook_active = str(data.get("stop_hook_active", "false")).lower() in ("true", "1", "yes")
    directory = data.get("cwd", os.getcwd())

    # Infinite-loop guard: Claude already ran a checkpoint this turn — allow stop
    if stop_hook_active:
        print("{}")
        return

    # No transcript available (some agent contexts) — skip
    if not transcript_path:
        print("{}")
        return

    state = _load_state()
    session_state: dict = state.get(session_id, {})
    last_save: int = session_state.get("last_save", 0)

    current_count = _count_human_messages(transcript_path)

    if current_count - last_save < INTERVAL:
        print("{}")
        return

    # Checkpoint time — update state atomically and block
    session_state["last_save"] = current_count
    state[session_id] = session_state
    _save_state(state)

    project = os.path.basename(directory.rstrip("/")) or "project"
    prompt = _PROMPT_TEMPLATE.format(directory=directory, project=project)
    print(json.dumps({"decision": "block", "reason": prompt}))


if __name__ == "__main__":
    main()
