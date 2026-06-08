"""daemon subcommand — manage the Yadgar background daemon (Docker container)."""

import sys

from yadgar.daemon import DOCKERHUB_IMAGE


def cmd_daemon(args):
    """Manage the Yadgar background daemon (Docker container)."""
    import os as _os

    from yadgar.daemon import DEFAULT_DEV_PORT, YadgarDaemon

    port = int(getattr(args, "port", None) or _os.environ.get("YADGAR_PORT", "8765"))
    dev = bool(getattr(args, "dev", False))
    daemon = YadgarDaemon(port=port, db_path=getattr(args, "db_path", None))

    sub = args.daemon_command
    if sub is None:
        print(
            "Usage: yadgar daemon [--dev] <pull|build|push|start|stop|graceful-stop|restart|"
            "status|configure-mcp|install-service|test|lint|shell>"
        )
        return

    if sub == "pull":
        check = YadgarDaemon.check_docker()
        if not check["ok"]:
            print(f"Docker not available: {check['reason']}", file=sys.stderr)
            sys.exit(1)
        result = daemon.pull()
        if result["ok"]:
            print(f"Pulled {result['image']}")
        else:
            print(f"Pull failed: {result['reason']}", file=sys.stderr)
            sys.exit(1)

    elif sub == "build":
        check = YadgarDaemon.check_docker()
        if not check["ok"]:
            print(f"Docker not available: {check['reason']}", file=sys.stderr)
            sys.exit(1)
        no_cache = bool(getattr(args, "no_cache", False))
        result = daemon.build(dev=dev, no_cache=no_cache)
        if result["ok"]:
            print(f"Built image {result['image']!r} (target={result['target']})")
        else:
            print(f"Build failed: {result['reason']}", file=sys.stderr)
            sys.exit(1)

    elif sub == "start":
        # Check Docker availability first
        check = YadgarDaemon.check_docker()
        if not check["ok"]:
            print(f"Docker not available: {check['reason']}", file=sys.stderr)
            sys.exit(1)

        result = daemon.start(dev=dev)
        if result["status"] == "started":
            container = result["container"]
            p = result["port"]
            mem = result.get("memory_mb", "?")
            print(f"Yadgar daemon started (container: {container}, port: {p}, memory: {mem}MB)")
            print("  Switch MCP to HTTP:  yadgar daemon configure-mcp")
            print("  Auto-start on login: yadgar daemon install-service")
        elif result["status"] == "already_running":
            container = result["container"]
            p = result["port"]
            print(f"Yadgar daemon already running (container: {container}, port: {p})")
        elif result["status"] == "failed":
            print(f"Cannot start daemon: {result['reason']}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Unexpected result: {result}", file=sys.stderr)
            sys.exit(1)

    elif sub == "push":
        check = YadgarDaemon.check_docker()
        if not check["ok"]:
            print(f"Docker not available: {check['reason']}", file=sys.stderr)
            sys.exit(1)
        result = daemon.push(tag=getattr(args, "tag", None))
        if result["ok"]:
            for t in result["pushed"]:
                print(f"Pushed {t}")
        else:
            print(f"Push failed: {result['reason']}", file=sys.stderr)
            sys.exit(1)

    elif sub == "stop":
        result = daemon.stop(dev=dev)
        if result["status"] == "stopped":
            print(f"Yadgar daemon stopped (container: {result['container']})")
        else:
            print("Yadgar daemon is not running.")

    elif sub == "graceful-stop":
        # v5.49.0 Phase 6: graceful-stop with SIGTERM + drain barriers.
        # Uses `docker stop --time=<timeout>` which sends SIGTERM to container
        # PID 1 and waits up to <timeout> seconds before sending SIGKILL.
        # The daemon's shutdown() handles the actual drain (sd_notify.stopping(),
        # flush_barrier, drain_in_flight_requests) — this CLI just signals it
        # and polls until stopped or timeout exceeded.
        import subprocess as _sp  # noqa: PLC0415

        timeout = int(getattr(args, "timeout", 30) or 30)
        from yadgar.daemon import _dev_profile, _prod_profile  # noqa: PLC0415

        profile = _dev_profile() if dev else _prod_profile(port)
        container = profile.container_name

        # Check container is running first
        check = YadgarDaemon.check_docker()
        if not check["ok"]:
            print(f"Docker not available: {check['reason']}", file=sys.stderr)
            sys.exit(1)

        result_running = _sp.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
        )
        if result_running.returncode != 0 or result_running.stdout.strip() != "true":
            print(f"Container {container!r} is not running.")
            sys.exit(0)

        print(f"Gracefully stopping {container!r} (timeout={timeout}s)…")
        result_stop = _sp.run(
            ["docker", "stop", f"--time={timeout}", container],
            capture_output=True,
            text=True,
        )
        if result_stop.returncode == 0:
            print(f"Yadgar daemon stopped gracefully (container: {container})")
            sys.exit(0)
        else:
            print(
                f"Graceful stop failed (rc={result_stop.returncode}): {result_stop.stderr.strip()}",
                file=sys.stderr,
            )
            sys.exit(1)

    elif sub == "restart":
        result = daemon.restart(dev=dev)
        start = result["started"]
        if start.get("status") in ("started", "already_running"):
            print(
                f"Yadgar daemon restarted "
                f"(container: {start.get('container')}, port: {start.get('port')})"
            )
        else:
            print(f"Restart result: {result}", file=sys.stderr)

    elif sub == "status":
        result = daemon.status(dev=dev)
        if result.get("running"):
            print("Yadgar daemon: running")
            print(f"  Container: {result.get('container')}")
            print(f"  Port:      {result.get('port')}")
            print(f"  Version:   {result.get('version', '?')}")
            print(f"  Uptime:    {result.get('uptime_seconds', '?')}s")
        else:
            flag = " --dev" if dev else ""
            print("Yadgar daemon: not running")
            print(f"  Start with: yadgar daemon start{flag}")

    elif sub == "configure-mcp":
        result = daemon.configure_mcp(dev=dev)
        p = DEFAULT_DEV_PORT if dev else port
        print(f"MCP config updated: {result['updated']}")
        print(f"  Sessions connect to: http://127.0.0.1:{p}/mcp")

    elif sub == "install-service":
        result = daemon.install_systemd_service(dev=dev)
        print(f"Systemd service written: {result['service_file']}")
        print(f"  Enable:  {result['enable']}")
        print(f"  Start:   {result['start']}")
        print(f"  Status:  {result['status']}")

    elif sub == "test":
        # Run pytest inside the dev container with xdist parallelism
        extra = list(getattr(args, "extra_args", []) or [])
        if not any(a.startswith("-n") or a == "--dist" for a in extra):
            extra = ["-n", "auto"] + extra
        sys.exit(daemon.exec_in_container(["pytest"] + extra, dev=True))

    elif sub == "lint":
        sys.exit(daemon.exec_in_container(["ruff", "check", "yadgar/"], dev=True))

    elif sub == "shell":
        sys.exit(daemon.exec_in_container(["/bin/bash"], interactive=True, dev=True))


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
    test_p = daemon_sub.add_parser("test", help="Run pytest inside the dev container (yadgar-dev)")
    test_p.add_argument("extra_args", nargs="*", help="Extra arguments forwarded to pytest")
    daemon_sub.add_parser("lint", help="Run ruff inside the dev container (yadgar-dev)")
    daemon_sub.add_parser(
        "shell", help="Open an interactive bash shell in the dev container (yadgar-dev)"
    )
    daemon_parser.set_defaults(func=cmd_daemon)
