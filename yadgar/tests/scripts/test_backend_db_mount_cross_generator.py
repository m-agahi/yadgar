"""Cross-generator regression: every in-repo generator that renders the BACKEND
container must mount its ``/data`` — the SurrealDB store — from a host bind
mount, never from a container-runtime named volume.

Bug 11 moved the backend DB onto the XDG data dir (``yadgar/core/daemon/systemd.py``
still says so in a comment: "use XDG DATA_DIR as host bind mount instead of named
volume"). ``install_systemd_service`` and the ``.service.in`` / ``.plist.in``
templates complied; ``daemon.py``'s ``start_backend`` never got the change and kept
mounting the named volume ``yadgar-db-data``. Observed live on a fresh Debian 13 VM
(2026-07-31, 5.170.0) installed via ``yadgar daemon start``::

    podman inspect yadgar-backend --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}'
      /var/lib/containers/storage/volumes/yadgar-db-data/_data -> /data
    $ ls ~/.local/share/yadgar/          # EMPTY
    $ yadgar vacuum
      [vacuum] ERROR: DB dir not found: /root/.local/share/yadgar/surreal_db

``yadgar vacuum`` runs on the HOST and translates ``$DATA_DIR`` → ``/data`` as a
prefix rewrite (``yadgar/core/vacuum/__init__.py``). That translation is only true
when the backend's ``/data`` really is ``$DATA_DIR``. A generator that mounts a
named volume there makes vacuum — and every other host-side tool that reasons about
the store's location — silently wrong.

**Core's ``/data`` is deliberately NOT checked here.** The two look identical at the
call site and a future reader will be tempted to "fix the inconsistency": don't. The
core container mounts the *queue* volume at ``/data`` (``yadgar-data``, a named
volume by design — see ADR-0075 and ``test_backend_unit_queue_base_cross_generator.py``)
and the backend takes that same queue volume at ``/queue-data``. Only the BACKEND's
``/data`` is the DB, and only the backend's ``/data`` is in question.

**Deliberately out of scope** (absence is a decision, not an oversight):

* ``docker-compose.yml`` — a self-contained dev/CI stack whose volumes are created
  and destroyed by compose itself. Nothing host-side runs ``yadgar vacuum`` against
  a compose stack, so the bind-mount contract does not apply to it.
* ``flake.nix`` — covered by a weaker, honestly-scoped assertion at the bottom of
  this module: its ``-v ${dataDir}:/data`` is a Nix interpolation that pytest cannot
  expand, so all this suite can prove there is "the source is not a named-volume
  literal". Called out separately rather than folded into the strong invariant.

Same structural template as its siblings ``test_admin_token_cross_generator.py``
(ADR-0180) and ``test_backend_unit_queue_base_cross_generator.py``: a FUTURE
generator — or a change to an existing one — that reverts to a named volume fails
this ONE shared test.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from yadgar.core.daemon import daemon as daemon_mod
from yadgar.core.daemon import systemd as systemd_mod
from yadgar.core.daemon.profiles import _prod_profile
from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import render_launchd, render_systemd

FLAKE_NIX = REPO_ROOT / "flake.nix"
DAEMON_PY = REPO_ROOT / "yadgar" / "core" / "daemon" / "daemon.py"

# `-v <source>:/data` where `/data` is the WHOLE destination — `:/data/logs` and
# `:/data/config.yaml:ro` are different mounts and must not match.
_DATA_MOUNT_RE = re.compile(r"-v\s+(\S+?):/data(?=[\s\\\"']|$)")


def _backend_data_mount_source(rendered: str, label: str) -> str:
    """The single `-v <src>:/data` source in *rendered*, or fail loudly."""
    sources = _DATA_MOUNT_RE.findall(rendered)
    assert sources, (
        f"{label}: no `-v <source>:/data` mount found in the rendered backend "
        f"unit — the backend has no DB mount at all, or this suite's extractor "
        f"has drifted from the generator's syntax"
    )
    assert len(sources) == 1, (
        f"{label}: {len(sources)} mounts target /data ({sources}) — ambiguous; "
        f"the backend DB mount must be the only one"
    )
    return sources[0]


# ── Renderers: (label) -> the rendered BACKEND unit text only ─────────────────
#
# Every renderer slices to the backend role. Concatenating the core unit in would
# let core's (legitimately named-volume) `/data` be mistaken for the backend's —
# the exact confusion this module's docstring warns against.


def _render_systemd_sh(tmp_path: Path, _mp: pytest.MonkeyPatch) -> str:
    render_systemd(tmp_path)
    return (tmp_path / "units" / "yadgar-backend.service").read_text()


def _render_launchd_sh(tmp_path: Path, _mp: pytest.MonkeyPatch) -> str:
    render_launchd(tmp_path)
    plist = plistlib.loads(
        (tmp_path / "units" / "com.openfantasy.yadgar-backend.plist").read_bytes()
    )
    return "\n".join(plist["ProgramArguments"])


def _render_python_systemd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    profile = _prod_profile(8765)
    result = systemd_mod.install_systemd_service(profile, dev=False)
    return Path(result["backend_service"]).read_text()


def _render_daemon_start_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Drive the REAL ``start_backend`` and return its captured ``run`` argv.

    Asserted by driving the method rather than by reading its source (the
    admin-token sibling reads source because a bare `-e VAR` is a literal): the
    mount source here is *computed* from ``_paths.DATA_DIR``, so only the real
    argv proves what a live install would mount.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    monkeypatch.delenv("YADGAR_BACKEND_VOLUME", raising=False)

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append([str(c) for c in cmd])
        return SimpleNamespace(returncode=0, stdout="fakecontainerid", stderr="")

    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_mod, "_ensure_network", lambda: None)
    monkeypatch.setattr(daemon_mod, "_get_runtime", lambda: "docker")
    monkeypatch.setattr(daemon_mod, "_container_memory_mb", lambda: 512)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda *_: None)

    d = daemon_mod.YadgarDaemon()
    monkeypatch.setattr(d, "_container_running", lambda *a, **k: False)
    monkeypatch.setattr(d, "_image_exists", lambda *a, **k: True)
    d.start_backend()

    for cmd in calls:
        if "run" in cmd and any("yadgar-backend" in a for a in cmd):
            return " ".join(cmd)
    raise AssertionError(f"no backend `docker run` argv captured; calls={calls}")


_RENDERERS = {
    "generate_systemd.sh": _render_systemd_sh,
    "generate_launchd.sh": _render_launchd_sh,
    "install_systemd_service (Python)": _render_python_systemd,
    "daemon.py start_backend": _render_daemon_start_backend,
}


# ── The invariant ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", sorted(_RENDERERS))
def test_backend_generator_binds_db_from_host_path(label, tmp_path, monkeypatch):
    """THE INVARIANT: the backend's /data comes from an absolute HOST path.

    A named volume (a bare token with no path separator) is what Bug 11 removed
    and what `yadgar vacuum`'s $DATA_DIR → /data prefix rewrite cannot see.
    """
    rendered = _RENDERERS[label](tmp_path, monkeypatch)
    source = _backend_data_mount_source(rendered, label)
    assert source.startswith("/"), (
        f"{label}: the backend mounts {source!r} at /data — that is a "
        f"container-runtime NAMED VOLUME, not a host bind mount. Bug 11 moved "
        f"the backend DB onto $DATA_DIR; a named volume puts the store where no "
        f"host-side tool can reach it, so `yadgar vacuum` dies with "
        f"'DB dir not found: <data>/surreal_db' before doing any work. Mount "
        f"`_paths.DATA_DIR` (or the generator's @DATA_DIR@ equivalent) at /data."
    )


def test_every_backend_generator_agrees_on_the_same_data_dir(tmp_path, monkeypatch):
    """The two Python generators must resolve the SAME host path.

    `install_systemd_service` and `start_backend` both run in-process off
    `_paths.DATA_DIR`; if they ever diverge, `daemon start` and
    `daemon install-service` would point the same install at two different
    stores. The shell/plist generators are excluded — they take the path from
    their own `@DATA_DIR@` / `@YADGAR_INSTALL_PREFIX@` render-time input, which
    is an install-prefix choice rather than a code path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "data"))

    from_systemd = _backend_data_mount_source(
        _render_python_systemd(tmp_path, monkeypatch), "install_systemd_service"
    )
    from_daemon = _backend_data_mount_source(
        _render_daemon_start_backend(tmp_path, monkeypatch), "daemon.py start_backend"
    )
    assert from_systemd == from_daemon, (
        f"install_systemd_service mounts {from_systemd!r} at the backend's /data "
        f"but `yadgar daemon start` mounts {from_daemon!r} — the same install "
        f"would use two different DB locations depending on how it was started."
    )


def test_daemon_py_no_longer_mounts_the_legacy_named_volume():
    """The plan's literal acceptance criterion, read straight off the source.

    Redundant with the parametrized invariant above *by design*: this one names
    the exact regressed line, so a future revert reads as
    "daemon.py went back to yadgar-db-data" rather than as an opaque mount-shape
    failure. Sliced to `start_backend` so the core `start` method's legitimate
    `{profile.volume_name}:/data` (the QUEUE volume — see module docstring)
    cannot trip it.
    """
    text = DAEMON_PY.read_text()
    start = text.find("def start_backend(")
    assert start != -1, "daemon.py: `def start_backend(` not found"
    end = text.find("\n    def stop(", start)
    assert end != -1, "daemon.py: no `def stop(` after `start_backend`"
    body = text[start:end]

    assert 'f"{volume}:/data"' not in body, (
        "daemon.py start_backend still mounts the named volume "
        "`{volume}:/data` (default yadgar-db-data) as the backend's DB. Bug 11 "
        "moved this onto the host bind mount `_paths.DATA_DIR`; see "
        "yadgar/core/daemon/systemd.py, which already complies."
    )


def test_flake_nix_backend_data_mount_is_not_a_named_volume():
    """flake.nix, checked only as far as is honest.

    `-v ${dataDir}:/data` is a Nix interpolation this suite cannot expand, so the
    strong "starts with /" assertion above would be a lie here. What IS provable
    is that the source is not a bare named-volume token — which is exactly the
    regression shape (`yadgar-db-data:/data`) this module exists to catch.
    """
    text = FLAKE_NIX.read_text()
    marker = "\n            systemd.user.services.yadgar-backend = {"
    assert marker in text, "flake.nix has no systemd.user.services.yadgar-backend unit"
    start = text.index(marker) + 1
    rest = text[start:]
    nxt = rest.find("\n            systemd.user.")
    block = rest[:nxt] if nxt != -1 else rest

    source = _backend_data_mount_source(block.replace('"', " "), "flake.nix")
    assert "/" in source or "${" in source, (
        f"flake.nix mounts {source!r} at the backend's /data — a bare token with "
        f"no path separator and no interpolation is a named volume, which Bug 11 "
        f"removed from every backend surface."
    )
