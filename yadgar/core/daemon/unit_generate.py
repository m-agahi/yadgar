"""The ``yadgar-setup`` arm's render entry point (task:0110 Stage D, ADR-0190).

``scripts/install/generate_systemd.sh`` rendered nine units with ``sed`` from
nine ``.in`` templates. Stage D deletes the templates and turns that script into
a wrapper that marshals its documented environment contract, detects the
container runtime, asserts the renderer's schema and then calls
``yadgar daemon render-units`` — which is this module.

The env contract is unchanged, and is the wrapper's own documented one
(``generate_systemd.sh:5-22``). It is read HERE rather than passed as flags so
the wrapper stays a marshaller: a new input becomes one default in one place
instead of a flag, a shell variable and a passthrough.

What this module does NOT own: the unit text (``units.py`` /
``maintenance_units.py`` build it as data), the host probes and the atomic write
(``unit_install.py``), or runtime detection (``scripts/install/detect_runtime.sh``,
still called by the wrapper — it is shell-only logic with no Python counterpart).

Fail-loud everywhere, because the alternative is a unit that starts and is
wrong: an unresolvable host CLI, a nix-managed unit or a unit that fails
validation aborts BEFORE anything is moved into place, so the previous units are
still on disk and still running (plan §9.2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar.core.daemon.maintenance_units import HostExecs
from yadgar.core.daemon.unit_install import (
    HostCliUnresolved,
    ensure_trigger_dir,
    fail_no_host_cli_message,
    guard_nix_symlinks,
    resolve_host_exec,
    seed_upgrade_env,
    write_units,
)
from yadgar.core.daemon.unit_model import render_unit
from yadgar.core.daemon.units import build_units, setup_unit_spec

__all__ = ["SetupEnv", "generate_units", "setup_env_from_environ"]


@dataclass(frozen=True)
class SetupEnv:
    """``generate_systemd.sh``'s documented environment contract, resolved."""

    output_dir: Path
    runtime: str
    data_dir: str
    state_dir: Path
    secrets_env_file: str
    backend_image: str
    core_image: str
    surreal_port: int
    host_cli: str | None
    host_nightly_cli: str | None


@observe(tier="hot")
def setup_env_from_environ(env: dict[str, str] | None = None) -> SetupEnv:
    """Resolve the contract, defaults verbatim from ``generate_systemd.sh:29-44``.

    ``YADGAR_RUNTIME`` has no default: the wrapper detects it (or the caller sets
    it) and a missing one is an abort, exactly as the shell version's
    ``detect_runtime.sh`` failure branch is.
    """
    e = dict(os.environ if env is None else env)
    home = Path.home()
    runtime = e.get("YADGAR_RUNTIME", "").strip()
    if not runtime:
        raise HostCliUnresolved(
            "ERROR: YADGAR_RUNTIME is not set and no runtime was detected.\n"
            "  Install podman or docker, or set YADGAR_RUNTIME=podman|docker."
        )
    return SetupEnv(
        output_dir=Path(e.get("YADGAR_SYSTEMD_OUTPUT_DIR") or home / ".config/systemd/user"),
        runtime=runtime,
        data_dir=e.get("YADGAR_INSTALL_PREFIX") or str(home / ".local/share/yadgar"),
        state_dir=Path(e.get("YADGAR_STATE_DIR") or home / ".local/state/yadgar"),
        secrets_env_file=e.get("YADGAR_SECRETS_ENV_FILE")
        or str(home / ".config/yadgar/secrets.env"),
        backend_image=e.get("YADGAR_BACKEND_IMAGE") or "openfantasy/yadgar-backend:latest",
        core_image=e.get("YADGAR_CORE_IMAGE") or "openfantasy/yadgar:latest",
        surreal_port=int(e.get("YADGAR_BACKEND_SURREAL_PORT") or "8000"),
        host_cli=e.get("YADGAR_HOST_CLI") or None,
        host_nightly_cli=e.get("YADGAR_HOST_NIGHTLY_CLI") or None,
    )


@observe(tier="hot")
def _resolve_execs(cfg: SetupEnv) -> HostExecs:
    """Both host entry points, or abort. Ported from ``generate_systemd.sh:168-177``.

    They are DIFFERENT binaries — ``yadgar-nightly-cycle`` is a console script and
    there is no ``yadgar nightly-cycle`` subcommand — so each is resolved with its
    own script/module pair rather than through one tidied code path.
    """
    vacuum = resolve_host_exec("yadgar", "yadgar", cfg.host_cli)
    if vacuum is None:
        raise HostCliUnresolved(
            fail_no_host_cli_message("vacuum", "YADGAR_HOST_CLI", "yadgar", "yadgar")
        )
    nightly = resolve_host_exec(
        "yadgar-nightly-cycle", "yadgar.core.scripts.nightly_cycle", cfg.host_nightly_cli
    )
    if nightly is None:
        raise HostCliUnresolved(
            fail_no_host_cli_message(
                "nightly-cycle",
                "YADGAR_HOST_NIGHTLY_CLI",
                "yadgar-nightly-cycle",
                "yadgar.core.scripts.nightly_cycle",
            )
        )
    return HostExecs(vacuum=vacuum, nightly=nightly)


@observe(tier="boundary")
def generate_units(env: dict[str, str] | None = None, version: str = "") -> dict:
    """Render and install all nine units. Returns a summary for the CLI to print.

    Order is the shell version's and it is load-bearing: the nix guard and the
    host-CLI resolution both abort BEFORE anything is written, and
    ``seed_upgrade_env`` runs after the units are in place because the core unit
    is what reads the file it seeds.
    """
    from yadgar import __version__  # noqa: PLC0415 — avoid an import cycle at module load

    cfg = setup_env_from_environ(env)
    guard_nix_symlinks(cfg.output_dir)
    execs = _resolve_execs(cfg)
    ensure_trigger_dir(cfg.state_dir)

    # Same rule as the profile arm: bind the host HuggingFace cache when it
    # exists, so a container replacement does not re-download every model.
    hf_cache = Path.home() / ".cache" / "huggingface"
    spec = setup_unit_spec(
        runtime=cfg.runtime,
        data_dir=cfg.data_dir,
        state_dir=str(cfg.state_dir),
        secrets_env_file=cfg.secrets_env_file,
        backend_image=cfg.backend_image,
        execs=execs,
        surreal_port=cfg.surreal_port,
        hf_cache_dir=str(hf_cache) if hf_cache.exists() else None,
    )
    rendered = {name: render_unit(unit) for name, unit in build_units(spec).items()}
    written = write_units(rendered, cfg.output_dir, version or __version__)

    upgrade_env, seeded = seed_upgrade_env(Path.home() / ".local/state/yadgar", cfg.core_image)
    return {
        "output_dir": str(cfg.output_dir),
        "units": [p.name for p in written],
        "vacuum_exec": execs.vacuum,
        "nightly_exec": execs.nightly,
        "surreal_port": cfg.surreal_port,
        "trigger_dir": str(cfg.state_dir / "triggers"),
        "upgrade_env": str(upgrade_env),
        "upgrade_env_seeded": seeded,
    }
