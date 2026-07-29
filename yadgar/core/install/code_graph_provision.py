"""code_graph provisioning — keep the host binary and the store flag in AGREEMENT.

Moved out of ``cli/setup.py`` (2026-07-29) so that BOTH installer surfaces can
reach it. ``yadgar setup`` is not the installer most users run: the pipx / brew /
nix-profile path is ``scripts/install/yadgar-setup.sh`` and the repo-checkout
path is ``make setup``, and neither ever invokes ``yadgar setup``. They therefore
produced a machine where ``code_graph.enabled`` resolved TRUE (ADR-0163: no row →
default true) with no ``codebase-memory-mcp`` binary on disk — the exact
incoherence the default-on setup change existed to eliminate. Both shell surfaces
now call ``yadgar code-graph install``, which lands here.

Rationale for a module of its own rather than growing ``codebase_memory_mcp.py``:
that module is a pinned DOWNLOADER (SHA-256 table + OS/arch asset matrix), and
keeping it free of runtime-config writes and operator prose keeps its pin table
auditable.

The two coherent outcomes:

==========================  ===============  =========================
invocation                  host binary      ``code_graph.enabled``
==========================  ===============  =========================
default                     installed        ``true``
``opt_out=True``            not installed    ``false``
install failed (offline)    not installed    ``false``
==========================  ===============  =========================
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe

#: Runtime-config store key holding the code_graph enable flag (ADR-0163).
_CODE_GRAPH_KEY = "code_graph.enabled"

#: Read-before-write sentinel. MUST be distinguishable from a stored ``False``:
#: ``runtime_config_client.get`` is fail-open, so a daemon-down read returns the
#: caller's default — passing ``False`` as the default would make an unreachable
#: daemon look exactly like a deliberate global opt-out.
_NO_ROW = object()


@observe(tier="stage")
def _resolve_code_graph_action(args) -> str:
    """Decide the code_graph setup action. Pure — no TTY, no env, no prompt.

    Returns one of:
      * ``"install"`` — the DEFAULT: install the host binary AND persist
        ``code_graph.enabled=true``.
      * ``"opt_out"`` — ``--no-code-graph``: install nothing AND persist
        ``code_graph.enabled=false``.

    task:0082 — the decision tree used to branch on ``sys.stdin.isatty()``, an
    interactive ``[y/N]`` prompt, and a ``CODE_GRAPH_ENABLED`` env trigger. That
    made the ONLY scriptable path ``--no-code-graph``, which skipped the binary
    while ``code_graph.enabled`` still defaulted to True (ADR-0163) — an install
    whose runtime flag disagreed with its own filesystem. code_graph is on by
    default, so the DEFAULT install now provisions it; the single opt-out turns
    BOTH halves off together. No branch reads stdin, so setup can never block on
    a closed/absent stdin.
    """
    return "opt_out" if getattr(args, "no_code_graph", False) else "install"


@observe(tier="stage")
def _do_install_code_graph() -> bool:
    """Install the codebase-memory-mcp binary host-side. Returns success.

    ``skip_if_exists=True``: a setup re-run with the binary already present must
    not need the network (offline re-provisioning, nix-provided binary). Never
    raises — a genuinely impossible install (offline, unsupported platform) is
    reported and the caller degrades by disabling the feature.
    """
    from yadgar.core.install.codebase_memory_mcp import (  # noqa: PLC0415
        BINARY_NAME,
        VERSION,
        install_codebase_memory_mcp,
    )

    print(f"Installing codebase-memory-mcp {VERSION}...", end="  ", flush=True)
    try:
        binary_path = install_codebase_memory_mcp(skip_if_exists=True)
    except Exception as exc:  # noqa: BLE001 — a failed optional install never aborts setup
        print(f"✗ {exc}")
        print(
            "  The code_graph binary could not be installed (offline, or an "
            "unsupported platform). Setup CONTINUES — code_graph will be turned "
            "off so the runtime flag matches the missing binary.\n"
            "  Re-run `yadgar setup` once the problem is resolved to install it "
            "and turn code_graph back on."
        )
        return False

    print(f"✓ {binary_path}")
    print(f"  Binary: {binary_path}\n  Ensure ~/.local/bin is on PATH to use '{BINARY_NAME}'.")
    return True


@observe(tier="stage")
def _global_opt_out_present() -> bool:
    """True when an explicit GLOBAL ``code_graph.enabled=false`` row exists.

    Read-before-write guard. The enable persist used to write ``true`` at global
    scope unconditionally; that was inert only because the write almost always
    failed. Now that it lands, an unconditional write would make every re-run of
    the deliberately idempotent installer silently resurrect code_graph for a
    user who ran ``config_set("code_graph.enabled", false, scope="global")`` —
    an idempotent installer turned destructive. Per-repo overrides survive on
    their own (per-dir beats global, ADR-0163); a global opt-out does not.

    ``directory=None`` resolves the GLOBAL row specifically. ``get`` is fail-open,
    so an unreachable daemon returns :data:`_NO_ROW` and we degrade to "write it"
    — never to "assume opted out".
    """
    from yadgar.core import runtime_config_client  # noqa: PLC0415

    return runtime_config_client.get(_CODE_GRAPH_KEY, default=_NO_ROW) is False


@observe(tier="stage")
def _persist_code_graph_enable() -> bool:
    """Persist ``code_graph.enabled=true`` in the runtime config store (ADR-0163).

    Returns True only when a write actually landed — so False covers BOTH "the
    daemon refused it" and "we deliberately declined to clobber an existing
    global opt-out". Callers use this for messaging only.

    Uses the host WRITE client (``runtime_config_client.set``) which is NOT
    fail-open: daemon-down / non-2xx returns False. A failure here is BENIGN on a
    fresh machine — ``code_graph.enabled`` already defaults to True with no row —
    so the message says so rather than alarming the user; it only matters when a
    previous run (or a manual ``config_set``) left an explicit ``false`` behind.
    """
    from yadgar.core import runtime_config_client  # noqa: PLC0415

    if _global_opt_out_present():
        print(
            "  code_graph.enabled=false is already set globally — leaving your "
            "deliberate opt-out alone (the binary is installed but the feature "
            "stays off). Re-enable with the MCP tool "
            f'`config_set("{_CODE_GRAPH_KEY}", true, scope="global")`.'
        )
        return False

    if runtime_config_client.set(_CODE_GRAPH_KEY, True, scope="global"):
        print("  code_graph enabled globally (runtime_config store).")
        return True
    print(
        "  code_graph binary installed — the daemon is not reachable, so the enable "
        "was NOT persisted. code_graph.enabled already defaults to true, so no action "
        "is needed unless you previously disabled it; in that case run the MCP tool "
        f'`config_set("{_CODE_GRAPH_KEY}", true, scope="global")` or re-run '
        "`yadgar setup` once `yadgar daemon start` is up."
    )
    return False


@observe(tier="stage")
def _persist_code_graph_disable(why: str) -> bool:
    """Persist ``code_graph.enabled=false`` so the store matches an absent binary.

    This is the half the old flow was missing: ``--no-code-graph`` skipped the
    binary but left ``code_graph.enabled`` at its True default, producing a
    machine where the feature was ON with nothing to run. A failed write IS
    consequential here (the default is True), so the message spells out the one
    manual step.

    No read-before-write on this path: an explicit opt-out instruction is the
    user's current intent, and writing ``false`` over ``false`` is a no-op anyway.
    """
    from yadgar.core import runtime_config_client  # noqa: PLC0415

    if runtime_config_client.set(_CODE_GRAPH_KEY, False, scope="global"):
        print(f"  code_graph disabled globally (runtime_config store) — {why}.")
        return True
    print(
        f"  code_graph was NOT disabled ({why}) — the daemon is not reachable and the "
        "flag defaults to true, so the feature would stay ON with no binary installed. "
        f'Run the MCP tool `config_set("{_CODE_GRAPH_KEY}", false, scope="global")` '
        "or re-run `yadgar setup --no-code-graph` once `yadgar daemon start` is up."
    )
    return False


@observe(tier="boundary")
def provision_code_graph(*, opt_out: bool = False) -> bool:
    """Provision code_graph so the store flag and the host binary always AGREE.

    code_graph is ON by default (ADR-0162/0163: no row → ``is_enabled`` True), so
    this installs the host binary by DEFAULT — unattended, with no prompt and no
    stdin read, which is what makes a scripted/QA install work without flags
    (task:0082).

    Never raises and never exits: a failed download, an unreachable daemon, and an
    unsupported platform are all reported and survived. Callers are installers
    running under ``set -euo pipefail``.

    Args:
        opt_out: skip the binary AND persist ``code_graph.enabled=false``.

    Returns:
        True when the host binary is in place (the feature is usable), False on
        opt-out or a failed install. NOT a "did the persist land" signal — the
        persist is best-effort by design.
    """
    if opt_out:
        _persist_code_graph_disable("--no-code-graph")
        return False

    if _do_install_code_graph():
        _persist_code_graph_enable()
        return True

    _persist_code_graph_disable("binary install failed")
    return False
