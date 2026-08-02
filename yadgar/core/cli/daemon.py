"""daemon subcommand — manage the Yadgar background daemon (Docker container)."""

import sys

from yadgar.core.daemon import DOCKERHUB_IMAGE

# ---------------------------------------------------------------------------
# Per-subcommand helpers — extracted from the original monolithic
# cmd_daemon dispatcher to bring cyclomatic / LOC / nesting under cap.
# ---------------------------------------------------------------------------


def _require_docker(daemon_cls) -> None:
    """Exit 1 if Docker is unavailable."""
    check = daemon_cls.check_docker()
    if not check["ok"]:
        print(f"Docker not available: {check['reason']}", file=sys.stderr)
        sys.exit(1)


def _handle_pull(daemon, daemon_cls) -> None:
    _require_docker(daemon_cls)
    result = daemon.pull()
    if result["ok"]:
        print(f"Pulled {result['image']}")
        if result.get("backend_image"):
            print(f"Pulled {result['backend_image']}")
        return
    print(f"Pull failed: {result['reason']}", file=sys.stderr)
    sys.exit(1)


def _handle_build(args, daemon, daemon_cls, dev: bool) -> None:
    _require_docker(daemon_cls)
    no_cache = bool(getattr(args, "no_cache", False))
    result = daemon.build(dev=dev, no_cache=no_cache)
    if result["ok"]:
        print(f"Built image {result['image']!r} (target={result['target']})")
        return
    print(f"Build failed: {result['reason']}", file=sys.stderr)
    sys.exit(1)


def _handle_push(args, daemon, daemon_cls) -> None:
    _require_docker(daemon_cls)
    result = daemon.push(tag=getattr(args, "tag", None))
    if result["ok"]:
        for t in result["pushed"]:
            print(f"Pushed {t}")
        return
    print(f"Push failed: {result['reason']}", file=sys.stderr)
    sys.exit(1)


def _handle_start(daemon, daemon_cls, dev: bool) -> None:
    _require_docker(daemon_cls)
    result = daemon.start(dev=dev)
    status = result["status"]
    if status == "started":
        container = result["container"]
        p = result["port"]
        mem = result.get("memory_mb", "?")
        print(f"Yadgar daemon started (container: {container}, port: {p}, memory: {mem}MB)")
        print("  Switch MCP to HTTP:  yadgar daemon configure-mcp")
        print("  Auto-start on login: yadgar daemon install-service")
        return
    if status == "already_running":
        container = result["container"]
        p = result["port"]
        print(f"Yadgar daemon already running (container: {container}, port: {p})")
        return
    if status == "failed":
        print(f"Cannot start daemon: {result['reason']}", file=sys.stderr)
        sys.exit(1)
        return
    print(f"Unexpected result: {result}", file=sys.stderr)
    sys.exit(1)


def _handle_stop(daemon, dev: bool) -> None:
    result = daemon.stop(dev=dev)
    if result["status"] == "stopped":
        print(f"Yadgar daemon stopped (container: {result['container']})")
        return
    print("Yadgar daemon is not running.")


def _handle_graceful_stop(args, daemon_cls, dev: bool, port: int) -> None:
    """Gracefully stop with SIGTERM + drain barriers.

    Uses ``<runtime> stop --time=<timeout>`` which sends SIGTERM to container
    PID 1 and waits up to <timeout> seconds before sending SIGKILL.
    The daemon's shutdown() handles the actual drain (sd_notify.stopping(),
    flush_barrier, drain_in_flight_requests) — this CLI just signals it
    and polls until stopped or timeout exceeded.

    task:0083: the runtime binary is resolved via ``_get_runtime()``; a literal
    ``"docker"`` here crashes on a podman-only host.
    """
    import subprocess as _sp  # noqa: PLC0415

    from yadgar.core.daemon import (  # noqa: PLC0415
        _dev_profile,
        _get_runtime,
        _prod_profile,
    )

    timeout = int(getattr(args, "timeout", 30) or 30)
    profile = _dev_profile() if dev else _prod_profile(port)
    container = profile.container_name

    _require_docker(daemon_cls)
    rt = _get_runtime()

    result_running = _sp.run(
        [rt, "inspect", "--format", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
    )
    if result_running.returncode != 0 or result_running.stdout.strip() != "true":
        print(f"Container {container!r} is not running.")
        sys.exit(0)

    print(f"Gracefully stopping {container!r} (timeout={timeout}s)…")
    result_stop = _sp.run(
        [rt, "stop", f"--time={timeout}", container],
        capture_output=True,
        text=True,
    )
    if result_stop.returncode == 0:
        print(f"Yadgar daemon stopped gracefully (container: {container})")
        sys.exit(0)

    print(
        f"Graceful stop failed (rc={result_stop.returncode}): {result_stop.stderr.strip()}",
        file=sys.stderr,
    )
    sys.exit(1)


def _handle_restart(daemon, dev: bool) -> None:
    result = daemon.restart(dev=dev)
    start = result["started"]
    if start.get("status") in ("started", "already_running"):
        print(
            f"Yadgar daemon restarted "
            f"(container: {start.get('container')}, port: {start.get('port')})"
        )
        return
    print(f"Restart result: {result}", file=sys.stderr)


def _handle_status(daemon, dev: bool) -> None:
    result = daemon.status(dev=dev)
    if result.get("running"):
        print("Yadgar daemon: running")
        print(f"  Container: {result.get('container')}")
        print(f"  Port:      {result.get('port')}")
        print(f"  Version:   {result.get('version', '?')}")
        print(f"  Uptime:    {result.get('uptime_seconds', '?')}s")
        return
    flag = " --dev" if dev else ""
    print("Yadgar daemon: not running")
    print(f"  Start with: yadgar daemon start{flag}")


def _handle_configure_mcp(args, daemon, dev: bool) -> None:
    from yadgar.core.daemon import DEFAULT_DEV_PORT  # noqa: PLC0415

    port = int(getattr(args, "port", None) or 8765)
    result = daemon.configure_mcp(dev=dev)
    p = DEFAULT_DEV_PORT if dev else port
    print(f"MCP config updated: {result['updated']}")
    print(f"  Sessions connect to: http://127.0.0.1:{p}/mcp")


def _handle_install_service(daemon, dev: bool) -> None:
    result = daemon.install_systemd_service(dev=dev)
    print(f"Backend: {result['backend_service']}  Core: {result['core_service']}")
    print(f"  Enable:  {result['enable']}")
    print(f"  Start:   {result['start']}")
    print(f"  Status:  {result['status']}")


def _handle_render_units(args) -> None:
    """``yadgar daemon render-units`` — the renderer ``generate_systemd.sh`` delegates to.

    task:0110 Stage D (ADR-0190). Handled BEFORE ``cmd_daemon`` builds a
    ``YadgarDaemon``: rendering unit files needs no container runtime, and the
    wrapper queries ``--print-schema`` on a host that may have neither, where a
    daemon construction failure would be misread as "renderer too old".
    """
    from yadgar.core.daemon.unit_generate import generate_units  # noqa: PLC0415
    from yadgar.core.daemon.unit_install import UNIT_SCHEMA_VERSION, InstallAborted  # noqa: PLC0415

    if getattr(args, "print_schema", False):
        print(UNIT_SCHEMA_VERSION)
        return
    try:
        result = generate_units()
    except InstallAborted as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"Systemd units written to {result['output_dir']}/")
    for name in result["units"]:
        print(f"  {name}")
    print("Maintenance entry points resolved at render time:")
    print(f"  vacuum:        {result['vacuum_exec']}")
    print(f"  nightly-cycle: {result['nightly_exec']}")
    print(f"SurrealDB published on 127.0.0.1:{result['surreal_port']} (loopback only).")
    print(f"Vacuum trigger dir: {result['trigger_dir']}")
    if result["upgrade_env_seeded"]:
        print(f"Seeded {result['upgrade_env']}")
    else:
        print(
            f"Note: {result['upgrade_env']} already exists — not overwritten "
            "(orchestrator manages it)."
        )


def _handle_test(args, daemon) -> None:
    extra = list(getattr(args, "extra_args", []) or [])
    if not any(a.startswith("-n") or a == "--dist" for a in extra):
        extra = ["-n", "auto"] + extra
    sys.exit(daemon.exec_in_container(["pytest"] + extra, dev=True))


# ---------------------------------------------------------------------------
# Public dispatcher — thin router, no business logic here.
# ---------------------------------------------------------------------------

_SUBCOMMAND_DISPATCH = {
    "pull": lambda a, d, cls, dev, p: _handle_pull(d, cls),
    "build": lambda a, d, cls, dev, p: _handle_build(a, d, cls, dev),
    "push": lambda a, d, cls, dev, p: _handle_push(a, d, cls),
    "start": lambda a, d, cls, dev, p: _handle_start(d, cls, dev),
    "stop": lambda a, d, cls, dev, p: _handle_stop(d, dev),
    "graceful-stop": lambda a, d, cls, dev, p: _handle_graceful_stop(a, cls, dev, p),
    "restart": lambda a, d, cls, dev, p: _handle_restart(d, dev),
    "status": lambda a, d, cls, dev, p: _handle_status(d, dev),
    "configure-mcp": lambda a, d, cls, dev, p: _handle_configure_mcp(a, d, dev),
    "install-service": lambda a, d, cls, dev, p: _handle_install_service(d, dev),
    "test": lambda a, d, cls, dev, p: _handle_test(a, d),
    "lint": lambda a, d, cls, dev, p: sys.exit(
        d.exec_in_container(["ruff", "check", "yadgar/"], dev=True)
    ),
    "shell": lambda a, d, cls, dev, p: sys.exit(
        d.exec_in_container(["/bin/bash"], interactive=True, dev=True)
    ),
}


def cmd_daemon(args):
    """Manage the Yadgar background daemon (Docker container)."""
    import os as _os

    from yadgar.core.daemon import YadgarDaemon

    sub = args.daemon_command
    if sub == "render-units":
        # Before the daemon is constructed — see _handle_render_units.
        _handle_render_units(args)
        return

    port = int(getattr(args, "port", None) or _os.environ.get("YADGAR_PORT", "8765"))
    dev = bool(getattr(args, "dev", False))
    daemon = YadgarDaemon(port=port, db_path=getattr(args, "db_path", None))

    if sub is None:
        print(
            "Usage: yadgar daemon [--dev] <pull|build|push|start|stop|graceful-stop|restart|"
            "status|configure-mcp|install-service|render-units|test|lint|shell>"
        )
        return

    handler = _SUBCOMMAND_DISPATCH.get(sub)
    if handler is None:
        print(f"Unknown daemon subcommand: {sub!r}", file=sys.stderr)
        sys.exit(1)
    handler(args, daemon, YadgarDaemon, dev, port)


def register(subparsers):
    daemon_parser = subparsers.add_parser(
        "daemon", help="Manage the Yadgar background daemon (Docker container)"
    )
    daemon_parser.add_argument("--port", type=int, default=None, help="Daemon port (default: 8765)")
    daemon_parser.add_argument("--db-path", type=str, default=None, help="Database path")
    daemon_parser.add_argument(
        "--dev", action="store_true", help="Use dev profile (port 8766, source bind-mount)"
    )
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command")
    daemon_sub.add_parser(
        "pull", help=f"Pull the latest prod image from Docker Hub ({DOCKERHUB_IMAGE})"
    )
    # NOT A BUG that `build` exposes no --backend flag despite
    # YadgarDaemon.build(backend=True) existing (task:0101 sweep): under ADR-0176
    # CI is the sole builder/publisher of both images, and a locally built image
    # SHADOWS the CI-built one carrying the same tag (podman's default pull
    # policy is `missing`), so the machine silently runs an untested artifact.
    # The core-only local build survives as a developer convenience; the
    # backend=True keyword stays reachable in-process for tooling that needs it.
    build_p = daemon_sub.add_parser("build", help="Build the Docker image locally (prod or dev)")
    build_p.add_argument("--no-cache", action="store_true", help="Pass --no-cache to docker build")
    push_p = daemon_sub.add_parser("push", help="Tag and push the prod image to Docker Hub")
    push_p.add_argument(
        "--tag", type=str, default=None, help="Override version tag (default: package version)"
    )
    daemon_sub.add_parser("start", help="Start the daemon container")
    daemon_sub.add_parser("stop", help="Stop the running daemon container (immediate SIGKILL)")
    graceful_stop_p = daemon_sub.add_parser(
        "graceful-stop",
        help="Gracefully stop the daemon (SIGTERM + drain barriers, then SIGKILL after timeout)",
    )
    graceful_stop_p.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Seconds to wait for in-flight requests + queue flush before SIGKILL (default: 30)",
    )
    daemon_sub.add_parser("restart", help="Restart the daemon container")
    daemon_sub.add_parser("status", help="Show daemon container status")
    daemon_sub.add_parser(
        "configure-mcp", help="Switch ~/.claude.json MCP config to streamable-http transport"
    )
    daemon_sub.add_parser(
        "install-service", help="Install systemd user service for auto-start on login"
    )
    render_p = daemon_sub.add_parser(
        "render-units",
        help="Render all nine yadgar-setup systemd user units (called by generate_systemd.sh)",
    )
    render_p.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the rendered-unit schema version and exit (wrapper skew check)",
    )
    test_p = daemon_sub.add_parser("test", help="Run pytest inside the dev container (yadgar-dev)")
    test_p.add_argument("extra_args", nargs="*", help="Extra arguments forwarded to pytest")
    daemon_sub.add_parser("lint", help="Run ruff inside the dev container (yadgar-dev)")
    daemon_sub.add_parser(
        "shell", help="Open an interactive bash shell in the dev container (yadgar-dev)"
    )
    daemon_parser.set_defaults(func=cmd_daemon)
