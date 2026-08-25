"""Managed-hook wiring verification — "which of my hooks are actually wired?".

Ledger task 306.  The installer (``_settings._build_core_hooks`` +
``_install_append_hooks`` + ``_install_global_scripts``) emits a fixed set of
managed hook entries, and until this module there was NO surface that answered
whether any of them reached the live ``settings.json``.  A hook that is
*configured somewhere* but not *actually wired* produces silence, and silence
reads as "nothing to do" — the same root cause that killed ``action_log``
capture for six days (task 303).

Measured on the author's box 2026-08-21, which is what motivated this:

  * ``PostToolUse`` carried ``post-tool-capture`` and NOT ``block-reflect``
    (the installer emits both — ``_settings.py`` ``_build_core_hooks``).
  * ``PreCompact`` was an EMPTY array, so ``pre-compact-drain`` never fired
    either — a second silently-unwired managed hook, found by looking once.

Two install families, one logical name
--------------------------------------
The live wiring on that box is written by nix (``modules/home/yadgar.nix``),
which hand-rolls the same hooks with ``jq`` as ``yadgar-<name>.py`` standalone
scripts, while yadgar's own installer dispatches them through
``hook_runner.py <name>``.  Both spellings ARE the hook, so the comparison is
keyed on the LOGICAL NAME (see :func:`_hook_logical_name`), never on the
command string.  A naive command compare would call every live entry missing
and be useless exactly where it matters.

Authority boundary
------------------
This module REPORTS.  It never edits a hook another tool owns — reconciling
nix's hand-rolled wiring is task 305, a different repo.  A shape mismatch
(``yadgar-<name>.py`` where yadgar would emit ``hook_runner.py <name>``) is
reported as ``foreign``, which is informative rather than red: the hook fires,
a different tool installed it.  Only a genuinely ABSENT managed hook fails the
check.

Scope honesty
-------------
ADR-0173: Claude Code merges the global and project ``settings.json`` without
dedup, so a hook absent from ``~/.claude/settings.json`` may still be wired in
a project one.  Every candidate settings file is inspected and each finding
records WHICH scope carried it; the report names the files it read, so a clean
result cannot be mistaken for a file that was never opened.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe

# The runner script every runner-dispatched managed hook is invoked through.
_RUNNER_BASENAME = "hook_runner.py"

# Basename prefix nix (and yadgar's own standalone-script installs) use.
_YADGAR_PREFIX = "yadgar-"

_SCRIPT_SUFFIXES = (".py", ".sh")

#: Finding statuses.  ``missing`` is the only one that fails the check —
#: ``foreign`` means a different tool wired it (it fires), and ``unexpected``
#: means a yadgar-named hook is wired that this yadgar no longer installs.
STATUS_PRESENT = "present"
STATUS_MISSING = "missing"
STATUS_FOREIGN = "foreign"
STATUS_UNEXPECTED = "unexpected"
STATUS_UNRECOGNIZED = "unrecognized"

# Per-hook probe stdin payloads (task #322).  Each handler in
# ``core/cli/hook.py`` reads the Claude Code native payload from stdin; the
# probe sends the smallest payload each one tolerates without raising.
_PROBE_PAYLOADS: dict[str, str] = {
    "prompt-recall": json.dumps({"type": "prompt-recall", "prompt": "probe"}),
    "post-tool-capture": json.dumps({"type": "post-tool-capture", "tool_name": "Bash"}),
    "session-start-context": json.dumps({"type": "session-start-context", "cwd": "/tmp"}),
    "post-compact-rehydrate": json.dumps({"type": "post-compact-rehydrate", "cwd": "/tmp"}),
    "pre-compact-drain": json.dumps({"type": "pre-compact-drain"}),
    "block-reflect": json.dumps(
        {"type": "post-tool-capture", "tool_name": "mcp__yadgar__block_create"}
    ),
}

# Probe timeout (seconds).  Yields on a hung hook fast enough that the doctor
# stays responsive while letting a hook that needs the daemon's HTTP
# GET/POST complete.
_PROBE_TIMEOUT_SECONDS = 2.0

# Crash-reason capture cap.  Probes return only a short snippet; the full
# stderr lives in ~/.claude/yadgar-hook-errors.log.
_PROBE_STDERR_CHARS = 200


# ── logical-name extraction ──────────────────────────────────────────────────


@observe(tier="stage")
def _command_tokens(command: str) -> list[str]:
    """Split *command* into tokens, tolerating unbalanced quotes."""
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


@observe(tier="stage")
def _is_runner_dispatched(command: str) -> bool:
    """True when *command* invokes a managed hook via ``hook_runner.py <name>``."""
    return _RUNNER_BASENAME in command


@observe(tier="stage")
def _hook_logical_name(command: str) -> str | None:
    """Return the managed-hook logical name *command* invokes, or None.

    Spans both install families:

      ``… /hook_runner.py post-tool-capture``   → ``post-tool-capture``
      ``… /yadgar-post-tool-capture.py``        → ``post-tool-capture``
      ``… /yadgar-post-compact-rehydrate.sh``   → ``post-compact-rehydrate``

    Returns None for a command carrying no yadgar identity (a foreign hook
    another tool owns entirely, e.g. nix's caveman SessionStart entry).
    """
    tokens = _command_tokens(command)
    if not tokens:
        return None

    # Family 1 — runner dispatch: the name is the token AFTER the runner path.
    for index, token in enumerate(tokens):
        if Path(token).name == _RUNNER_BASENAME:
            following = tokens[index + 1 :]
            return following[0] if following else None

    # Family 2 — standalone script: a yadgar-prefixed .py/.sh basename.
    for token in tokens:
        name = Path(token).name
        if not name.startswith(_YADGAR_PREFIX):
            continue
        for suffix in _SCRIPT_SUFFIXES:
            if name.endswith(suffix):
                return name[len(_YADGAR_PREFIX) : -len(suffix)]
    return None


@observe(tier="stage")
def _entry_commands(entry: object) -> list[str]:
    """Return every hook command string carried by a settings.json entry."""
    if not isinstance(entry, dict):
        return []
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return []
    return [h.get("command", "") for h in hooks if isinstance(h, dict) and h.get("command")]


# ── expected set — harvested from the installer, never hand-written ──────────


@observe(tier="boundary")
def _harvest_expected(
    home_dir: Path,
    project_directory: str | None = None,
) -> tuple[dict[str, dict[str, bool]], list[dict[str, str]]]:
    """Return ``({event: {name: runner_dispatched}}, unrecognized)`` for the installer.

    ``unrecognized`` carries every command the installer emits that
    :func:`_hook_logical_name` cannot name.  Dropping those silently would be
    the module's own defect turned inward: a managed hook absent from the
    expected set makes the verifier report CLEAN for a hook it cannot see.  The
    shape is reachable — ``core/cli/hook.py`` documents a ``yadgar hook
    <event>`` dispatch used to wire ported clients, which matches neither
    install family — so an emitter change must fail loudly here rather than
    quietly shrink what gets checked.

    Harvested by running ``install_hooks_impl`` in ``dry_run`` mode, which
    computes the full settings dict and writes nothing (every ``_copy_hook`` /
    ``_sweep_stale_hook_scripts`` call is a dry-run no-op, ``_atomic_write`` is
    skipped, and the hooks dir is only created off the dry-run path).  Deriving
    the expectation from the installer itself is the point: a hand-written
    parallel list is a second source of truth that drifts the first time
    someone adds an entry, which is the very failure this module exists to
    catch.

    ``scope="global"`` is used because that scope's preview carries the whole
    managed set, including the Stop / SessionEnd entries that the project scope
    writes to the global file separately.

    The installer prints its preview to stdout; that is swallowed here — it can
    echo a resolved auth env block, which has no business on a verify path.
    """
    from yadgar.core.install.install_hooks_lib import install_hooks_impl

    with contextlib.redirect_stdout(io.StringIO()):
        result = install_hooks_impl(
            home_dir=home_dir,
            scope="global",
            project_directory=project_directory or str(home_dir),
            dry_run=True,
        )

    preview = result.get("preview") or {}
    expected: dict[str, dict[str, bool]] = {}
    unrecognized: list[dict[str, str]] = []
    for event, entries in (preview.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        names: dict[str, bool] = {}
        for entry in entries:
            for command in _entry_commands(entry):
                name = _hook_logical_name(command)
                if name is None:
                    unrecognized.append({"event": event, "command": command})
                    continue
                names[name] = _is_runner_dispatched(command)
        if names:
            expected[event] = names
    return expected, unrecognized


@observe(tier="boundary")
def expected_managed_hooks(
    home_dir: Path,
    project_directory: str | None = None,
) -> dict[str, set[str]]:
    """Return ``{event: {logical hook name, …}}`` yadgar's installer would emit."""
    expected, _unrecognized = _harvest_expected(home_dir, project_directory)
    return {event: set(names) for event, names in expected.items()}


# ── live wiring ──────────────────────────────────────────────────────────────


@observe(tier="stage")
def _candidate_settings_files(
    home_dir: Path,
    project_directory: str | None,
) -> list[tuple[str, Path]]:
    """Return the ``(scope, path)`` settings files Claude Code merges."""
    candidates: list[tuple[str, Path]] = [
        ("global", home_dir / ".claude" / "settings.json"),
        ("global", home_dir / ".claude" / "settings.local.json"),
    ]
    if project_directory:
        project = Path(project_directory)
        candidates.append(("project", project / ".claude" / "settings.json"))
        candidates.append(("project", project / ".claude" / "settings.local.json"))
    return candidates


@observe(tier="stage")
def _load_hooks(path: Path) -> dict[str, list]:
    """Read the ``hooks`` mapping out of *path*; ``{}`` when absent/unreadable."""
    try:
        data = json.loads(path.read_text())
    # fmt: skip — ruff format 0.16.x rewrites this tuple to Python-2 syntax.
    except (OSError, ValueError):  # fmt: skip
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, dict) else {}


@observe(tier="stage")
def _collect_live(
    home_dir: Path,
    project_directory: str | None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    """Return ``{(event, name): record}`` plus the per-file inspection log.

    The first file that carries an ``(event, name)`` wins; later scopes do not
    overwrite it, so the recorded ``scope`` is the one Claude Code resolves
    first in the candidate order above.
    """
    live: dict[tuple[str, str], dict[str, Any]] = {}
    inspected: list[dict[str, Any]] = []
    for scope, path in _candidate_settings_files(home_dir, project_directory):
        exists = path.exists()
        inspected.append({"scope": scope, "path": str(path), "exists": exists})
        if exists:
            _index_file_hooks(_load_hooks(path), scope, path, live)
    return live, inspected


@observe(tier="stage")
def _index_file_hooks(
    hooks: dict[str, list],
    scope: str,
    path: Path,
    live: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Fold one settings file's hook entries into the *live* index, in place."""
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for command in _entry_commands(entry):
                name = _hook_logical_name(command)
                if name is None:
                    continue
                live.setdefault(
                    (event, name),
                    {
                        "scope": scope,
                        "path": str(path),
                        "command": command,
                        "runner_dispatched": _is_runner_dispatched(command),
                    },
                )


# ── the verification ─────────────────────────────────────────────────────────


@observe(tier="stage")
def _probe_hook_execution(
    entries: list[dict[str, Any]],
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Invoke each runner-dispatched *entry* and classify the outcome.

    Returns ``{name: {"status": …, "scope": …, "crash_reason": …?}}``.  Only
    entries with ``runner_dispatched`` are probed — nix-installed standalone
    scripts (``yadgar-<name>.py``) are skipped because probing them would
    double-count hooks that already have a runner-dispatched twin, and the
    runner IS the canonical shape yadgar installs.

    Status values:

      * ``ran``              — subprocess exited 0
      * ``crash``            — non-zero exit; ``crash_reason`` carries the
                               first :data:`_PROBE_STDERR_CHARS` chars of stderr
      * ``hung``             — ``TimeoutExpired``; the hook did not return in
                               *timeout* seconds
      * ``binary-missing``   — ``FileNotFoundError``; the runner binary is not
                               on disk
    """
    results: dict[str, dict[str, Any]] = {}
    for entry in entries:
        command = entry.get("command", "")
        # Standalone-script entries are skipped: probing them would double-count
        # hooks that already have a runner-dispatched twin.
        if not entry.get("runner_dispatched", False):
            continue
        name = _hook_logical_name(command)
        if name is None or name not in _PROBE_PAYLOADS:
            continue
        argv = _command_tokens(command)
        record: dict[str, Any] = {"scope": entry.get("scope")}
        try:
            completed = (
                subprocess.run(  # arg built from shlex.split + curated payload, not user input
                    argv,
                    input=_PROBE_PAYLOADS[name],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            )
        except subprocess.TimeoutExpired:
            record["status"] = "hung"
        except FileNotFoundError as exc:
            record["status"] = "binary-missing"
            record["crash_reason"] = str(exc)[:_PROBE_STDERR_CHARS]
        else:
            if completed.returncode == 0:
                record["status"] = "ran"
            else:
                record["status"] = "crash"
                record["crash_reason"] = (completed.stderr or "")[:_PROBE_STDERR_CHARS]
        results[name] = record
    return results


@observe(tier="boundary")
def verify_managed_hooks(
    home_dir: Path,
    project_directory: str | None = None,
) -> dict[str, Any]:
    """Compare the hooks yadgar's installer emits against what is actually wired.

    Returns a report dict::

        {
          "ok": bool,                  # False iff any managed hook is MISSING
          "scopes_inspected": [{"scope", "path", "exists"}, …],
          "findings": [{"event", "name", "status", "scope", "command"}, …],
          "counts": {"present", "missing", "foreign", "unexpected", "unrecognized"},
        }

    ``foreign`` and ``unexpected`` are reported but do NOT fail the check —
    a foreign-shaped entry still fires, and an ``unexpected`` one is a hook
    another tool wired that this yadgar no longer installs.  An absent managed
    hook is a real "this never runs" finding and fails.  So does
    ``unrecognized``: a command the installer emits that this module cannot
    name would otherwise vanish from the expected set and be reported clean —
    a blind spot in the very tool built to end blind spots.

    LIMITATION: the comparison answers "is this hook REGISTERED", not "does it
    WORK".  A standalone-script entry is matched by name only; nothing here
    checks that the script it points at still exists on disk, that the baked
    interpreter resolves, or that the hook exits 0.  A clean result means the
    wiring is present, not that the hooks run.
    """
    expected, unrecognized = _harvest_expected(home_dir, project_directory)
    live, inspected = _collect_live(home_dir, project_directory)

    findings: list[dict[str, Any]] = []
    for event in sorted(expected):
        for name in sorted(expected[event]):
            record = live.get((event, name))
            if record is None:
                findings.append(
                    {
                        "event": event,
                        "name": name,
                        "status": STATUS_MISSING,
                        "scope": None,
                        "command": "",
                    }
                )
                continue
            # A shape mismatch means a different tool wrote this entry.  Only
            # runner-dispatched hooks can mismatch: a standalone script is the
            # same script under both installers.
            expected_runner = expected[event][name]
            status = (
                STATUS_FOREIGN
                if expected_runner and not record["runner_dispatched"]
                else STATUS_PRESENT
            )
            findings.append(
                {
                    "event": event,
                    "name": name,
                    "status": status,
                    "scope": record["scope"],
                    "command": record["command"],
                }
            )

    for (event, name), record in sorted(live.items()):
        if name in expected.get(event, {}):
            continue
        findings.append(
            {
                "event": event,
                "name": name,
                "status": STATUS_UNEXPECTED,
                "scope": record["scope"],
                "command": record["command"],
            }
        )

    # A command yadgar's OWN installer emits that this module cannot name is a
    # hole in the check itself: the hook silently leaves the expected set and
    # the verifier then reports clean for something it never looked at.  Fail.
    for item in unrecognized:
        findings.append(
            {
                "event": item["event"],
                "name": "",
                "status": STATUS_UNRECOGNIZED,
                "scope": None,
                "command": item["command"],
            }
        )

    counts = {
        status: sum(1 for f in findings if f["status"] == status)
        for status in (
            STATUS_PRESENT,
            STATUS_MISSING,
            STATUS_FOREIGN,
            STATUS_UNEXPECTED,
            STATUS_UNRECOGNIZED,
        )
    }

    # Probe (task #322): invoke every PRESENT runner-dispatched hook and
    # classify the outcome.  A registered-but-broken hook (hang, crash,
    # missing binary) flips ``ok`` to False so the user gets a real signal
    # rather than "registered but never runs".
    probe_targets = [
        {
            "command": f["command"],
            "scope": f.get("scope"),
            "path": None,
            "runner_dispatched": True,
        }
        for f in findings
        if f["status"] == STATUS_PRESENT
    ]
    execution = _probe_hook_execution(probe_targets)
    execution_failed = any(
        rec.get("status") in {"hung", "crash", "binary-missing"} for rec in execution.values()
    )

    return {
        "ok": (
            counts[STATUS_MISSING] == 0
            and counts[STATUS_UNRECOGNIZED] == 0
            and not execution_failed
        ),
        "scopes_inspected": inspected,
        "findings": findings,
        "counts": counts,
        "execution": execution,
    }


# ── report rendering ─────────────────────────────────────────────────────────


@observe(tier="stage")
def format_hook_verify_report(report: dict[str, Any]) -> str:
    """Render *report* as human-readable text for a terminal / doctor probe."""
    lines: list[str] = ["yadgar managed-hook wiring:"]

    for entry in report["scopes_inspected"]:
        mark = "read" if entry["exists"] else "absent"
        lines.append(f"  inspected ({entry['scope']}, {mark}): {entry['path']}")

    by_status: dict[str, list[dict[str, Any]]] = {}
    for finding in report["findings"]:
        by_status.setdefault(finding["status"], []).append(finding)

    unrecognized = by_status.get(STATUS_UNRECOGNIZED, [])
    if unrecognized:
        lines.append("")
        lines.append(
            f"  UNRECOGNIZED — the installer emits {len(unrecognized)} command(s) this "
            "check cannot name, so they were never verified. Teach "
            "_hook_logical_name the new shape:"
        )
        for finding in unrecognized:
            lines.append(f"    {finding['event']}: {finding['command']}")

    missing = by_status.get(STATUS_MISSING, [])
    if missing:
        lines.append("")
        lines.append(f"  MISSING — these managed hooks never fire ({len(missing)}):")
        for finding in missing:
            lines.append(f"    {finding['event']}: {finding['name']}")

    foreign = by_status.get(STATUS_FOREIGN, [])
    if foreign:
        lines.append("")
        lines.append(
            f"  foreign shape — wired by another tool, still fires ({len(foreign)}). "
            "yadgar reports these and does not touch them:"
        )
        for finding in foreign:
            lines.append(f"    {finding['event']}: {finding['name']} [{finding['scope']}]")

    unexpected = by_status.get(STATUS_UNEXPECTED, [])
    if unexpected:
        lines.append("")
        lines.append(f"  unexpected — wired but not installed by this yadgar ({len(unexpected)}):")
        for finding in unexpected:
            lines.append(f"    {finding['event']}: {finding['name']} [{finding['scope']}]")

    present = by_status.get(STATUS_PRESENT, [])
    lines.append("")
    lines.append(
        f"  {len(present)} present, {len(missing)} missing, "
        f"{len(foreign)} foreign, {len(unexpected)} unexpected, "
        f"{len(unrecognized)} unrecognized"
    )
    if report["ok"]:
        lines.append("  OK: every managed hook is registered (registration only — see LIMITATION).")
    elif missing:
        lines.append(
            "  DIVERGENCE: at least one managed hook is absent from every "
            "settings file inspected — it will never fire."
        )
    else:
        lines.append(
            "  BLIND SPOT: the installer emits a command shape this check "
            "cannot name — some managed hooks were not verified at all."
        )
    return "\n".join(lines)
