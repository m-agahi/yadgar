"""Host-side ``project_id`` mint — Car C2 of the 0047 PR-40 remediation train.

**This module is the only place identity is minted, and it is reachable only
from the host-side hook entry points.** ADR-0227:

> ``derive_project_id`` is removed from every code path reachable by core or
> backend. The sole minting point is a host-side script run at SessionStart,
> which resolves ``owner/repo`` from the working tree and sets ``project_id``
> for the session; it thereafter travels as an explicit caller parameter on
> every call that needs it. […] ALL fallbacks are deleted. A missing or
> unresolved ``project_id`` FAILS LOUD with a structured error at the boundary
> — it is never defaulted, never inferred, never silently substituted.

**Why the package location is the contract.** Neither container image installs
git and neither mounts a host project directory, so a derivation reachable from
``core/server`` or ``backend`` is not merely risky — it is *guaranteed* wrong,
and its wrongness is invisible because ``local/<basename>`` is a well-formed
key. Putting the mint under ``core/hooks/`` makes the boundary structural: the
residue lint is a set-difference over importers rather than a call-graph
analysis. ``yadgar/tests/hooks/test_identity_mint.py`` asserts that only three
host-side entry points name it: ``core/hooks/session-start-context.py``,
``core/cli/hook.py``, and ``core/cli/_shared.py`` (the ``yadgar`` console
script — a host-invoked binary distinct from the container's ``entrypoint.sh``,
which runs only the MCP server; ``core/cli/_shared.py::resolve_cli_project``
mints for CLI subcommands, like ``drain``/``restore``, invoked as their own
host subprocess with no SessionStart-minted value to inherit).

**Why the pure helpers are imported rather than moved.** ``_normalise_remote``,
``_parse_insteadof_map``, ``_insteadof_rules``, ``_walk_project_id_file`` and
``_origin_remote`` are pure readers with no identity policy in them — they parse
a URL, read a file, shell out for a config value. The *policy* is the
composition: which sources count, in what order, and what happens when none
resolve. That composition is what must not be reachable from a container, and
it lives here. ``yadgar.core.identity`` keeps the helpers (and, until C5
repoints its six unguarded call sites, the legacy ``derive_project_id``).

**No fallback lives here and none may be added.** The mint raises. A caller
that cannot proceed without an identity must fail, not guess.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.server_helpers.server_helpers import _resolve_project_root
from yadgar.core.identity import (
    _insteadof_rules,
    _normalise_remote,
    _origin_remote,
    _parse_insteadof_map,
    _walk_project_id_file,
)


class UnresolvableProjectError(RuntimeError):
    """No project identity could be resolved for a directory.

    Raised instead of returning a plausible-looking key. ADR-0227's rationale:
    "A fallback that cannot fail is worse than an error, because it manufactures
    a plausible-looking wrong answer."
    """


_BANNER_PREFIX = "yadgar: project_id="


@observe(tier="stage", metric="core.hooks.identity_mint.mint_project_id")
def mint_project_id(cwd: str) -> str:
    """Resolve the canonical ``owner/repo`` key for *cwd*, or raise.

    Resolution order — two sources, no third:

        1. ``.yadgar/project-id`` walked UP from *cwd* → its trimmed content.
           The documented override; also the escape hatch for a tree with no
           usable remote (a monorepo subproject, a fresh checkout).
        2. ``owner/repo`` from the git ``origin`` remote: insteadOf rewrites
           resolved, scheme + host stripped, trailing ``.git`` stripped,
           lowercased. Nested namespaces are preserved as an opaque path.

    Raises:
        UnresolvableProjectError: neither source resolved. The message names
            the directory and both remedies — a fail-loud error that does not
            say what to do is only marginally better than a silent fallback.
    """
    override = _walk_project_id_file(cwd)
    if override:
        return override

    remote = _origin_remote(_resolve_project_root(cwd))
    if remote:
        return _normalise_remote(_parse_insteadof_map(_insteadof_rules(), remote))

    raise UnresolvableProjectError(
        f"cannot resolve a project_id for {cwd!r}: no .yadgar/project-id file was "
        "found walking up from it, and `git config remote.origin.url` produced "
        "nothing there (no git, no repo, or no 'origin' remote). ADR-0227: yadgar "
        "never guesses an identity. Fix by adding an origin remote, or by writing "
        "the key yourself: mkdir -p .yadgar && echo owner/repo > .yadgar/project-id"
    )


@observe(tier="stage", metric="core.hooks.identity_mint.project_id_banner")
def project_id_banner(project_id: str) -> str:
    """Render the greppable SessionStart identity line.

    The transport (§1.3 T1 of the 0047 remediation plan) is this line plus an
    explicit caller parameter: MCP calls carry no session key, so there is no
    daemon-side registry to consult and nothing to infer from. The line
    therefore has to say what to *pass*, not merely what the id *is*.
    """
    return (
        f"{_BANNER_PREFIX}{project_id} — pass "
        f'project="{project_id}" on every yadgar tool call '
        "(a different value = deliberate cross-project work)."
    )


@observe(tier="stage", metric="core.hooks.identity_mint.mint_failure_notice")
def mint_failure_notice(cwd: str, reason: str) -> str:
    """Render the loud, actionable failure text. Carries NO candidate key.

    Deliberately free of anything shaped like a project_id: an agent reading
    this must not be able to copy a plausible value out of the error and pass
    it, which would reintroduce the guess through the human in the loop.

    Task 423: the notice also tells the reader to ASK THE USER. "Fail loud"
    used to end at the shell one-liner, which an agent has no standing to run
    on the user's behalf — it cannot know the right key, and inventing one is
    exactly what ADR-0227 forbids. Asking is therefore the sanctioned recovery,
    not a courtesy. The closing line names the OTHER failure (identity present,
    registry row absent) so the two are not treated with the same fix.
    """
    return (
        f"[yadgar] ERROR: no project identity for {cwd}.\n"
        f"[yadgar] {reason}\n"
        "[yadgar] Yadgar tool calls needing an identity will FAIL until this is "
        "fixed — no default is assumed (ADR-0227).\n"
        "[yadgar] Fix: add a git 'origin' remote, or write the key explicitly:\n"
        "[yadgar]   mkdir -p .yadgar && echo owner/repo > .yadgar/project-id\n"
        "[yadgar] Only the user knows the right key, and deriving one is forbidden "
        "(ADR-0227), so ASKING THEM IS THE SANCTIONED PATH: surface this now — say "
        "identity is missing, name what it blocks (ADR capture, task-ledger sync, "
        "checkpoint), and hand over the line above. Do not proceed silently and do "
        "not fall back to the directory name. The mint runs only at session start, "
        "so a new session is needed once it is fixed.\n"
        "[yadgar] A DIFFERENT failure looks similar but is not this one: if an "
        'identity IS present and calls answer "unknown project_id", the mint '
        "worked and the project registry has no row — diagnose with "
        "`yadgar project list`, not with the fix above."
    )


@observe(tier="stage", metric="core.hooks.identity_mint.resolve_session_project")
def resolve_session_project(cwd: str) -> tuple[str | None, str]:
    """Mint for *cwd* and render what the hook should print.

    Returns ``(project_id, text)``. On failure ``project_id`` is ``None`` and
    ``text`` is the loud notice — the caller prints the text either way and
    forwards ``project_id`` only when it is set.

    Split from the printing so both hook entry points (the Claude Code script
    and the CLI/opencode handler) share one policy and one wording, while
    remaining trivially testable without capturing stdout.
    """
    try:
        project_id = mint_project_id(cwd)
    except UnresolvableProjectError as exc:
        return None, mint_failure_notice(cwd, str(exc))
    return project_id, project_id_banner(project_id)
