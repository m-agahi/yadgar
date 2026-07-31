"""Cross-generator regression: every in-repo generator that renders a container
unit which SERVES or CALLS ``/admin/*`` must make ``YADGAR_MCP_AUTH_TOKEN`` reach
that container's process env.

ADR-0180. On a fresh VM ``GET /admin/dbsize`` returned
``503 {"detail":"Admin token not configured"}`` while ``GET /health`` returned
``200 {"db":true,...}`` — the backend was entirely healthy and every /admin call
was rejected before doing any work, because the install-generated
``yadgar-backend.service`` never forwarded the token into the container.
``_require_admin_token`` (yadgar/backend/embed_service/embed_service.py) reads
``YADGAR_MCP_AUTH_TOKEN`` from ``os.environ`` and fails closed when it is empty,
so a generator that forgets it bricks the whole admin surface — seeding,
consolidation, dbsize, recall, restore, viz, read-query.

This is the THIRD instance of one class in two days — an install-generated
artifact missing an auth credential:

1. task:0075 — the claude-code MCP entry written without the Bearer header.
2. the ``runtime_config_client`` env-only token read (v5.169 train).
3. this one — the backend's own admin surface.

All three are invisible to CI for the same structural reason: CI never installs
from scratch. Per ADR-0180 the class "deserves one cross-generator invariant
rather than three point fixes", so this is that invariant rather than a
third point fix. Same structural template as its two siblings,
``test_backend_unit_queue_base_cross_generator.py`` (task:0076) and
``test_vacuum_trigger_cross_generator.py`` (task:0044): a FUTURE generator — or a
change to an existing one — that forgets the token fails this ONE shared test.

**Both roles are covered.** The BACKEND container serves ``/admin/*`` and
compares the presented bearer against its own ``YADGAR_MCP_AUTH_TOKEN``; the
CORE container calls those routes and sends that same token. One missing half
breaks the pair, so every generator is asserted for both roles it renders.

**Four satisfaction shapes are accepted** — the healthy surfaces genuinely use
different ones, and a regex that only accepts ``-e VAR=`` would false-fail three
of them:

* ``-e YADGAR_MCP_AUTH_TOKEN=${YADGAR_MCP_AUTH_TOKEN}`` — systemd ``.in``
  templates, expanded by systemd from the unit's ``EnvironmentFile``.
* ``-e YADGAR_MCP_AUTH_TOKEN`` (bare) — ``daemon.py`` and ``flake.nix``; the
  container runtime copies the value from the parent process env.
* ``--env-file <secrets.env>`` — the launchd plists and ``daemon.py``. Only
  honest if ``secrets.env`` actually carries the key, which
  ``test_bootstrap_secrets_still_writes_the_admin_token`` pins.
* a ``YADGAR_MCP_AUTH_TOKEN:`` key under a compose service's ``environment:``.

**Deliberately out of scope** (absence here is a decision, not an oversight):

* Host-side units (``yadgar-vacuum.service``, ``yadgar-nightly-cycle.service``)
  run on the host with no container and target core's ``/api/*``, not
  ``/admin/*``. They already receive the token via ``EnvironmentFile``.
* The private nix module (``modules/home/yadgar.nix``) renders the same contract
  but lives in the dotfiles repo — out-of-repo and unreachable from this suite,
  exactly as its two sibling tests note.

``docker-compose.yml`` and ``daemon.py`` are asserted as TEXT rather than parsed
(YAML / AST), matching both siblings: the invariant is "does this surface spell
the token anywhere in the unit it renders", which text answers exactly, and a
parser would add a dependency plus a second failure mode that reports as a
guard bug rather than a generator bug.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

from yadgar.core.daemon import systemd as systemd_mod
from yadgar.core.daemon.profiles import _prod_profile
from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import BASH, render_launchd, render_systemd

TOKEN_ENV = "YADGAR_MCP_AUTH_TOKEN"

INSTALL_DIR = REPO_ROOT / "scripts" / "install"
LAUNCHD_DIR = INSTALL_DIR / "launchd"
FLAKE_NIX = REPO_ROOT / "flake.nix"
COMPOSE_YML = REPO_ROOT / "docker-compose.yml"
DAEMON_PY = REPO_ROOT / "yadgar" / "core" / "daemon" / "daemon.py"
BOOTSTRAP_SECRETS_SH = INSTALL_DIR / "bootstrap_secrets.sh"

# `-e VAR=...` (explicit value / shell expansion) or bare `-e VAR` (runtime copies
# it from the parent process env). The bare form must not match a *different*
# variable that merely starts with the same name, hence the trailing boundary.
_E_FLAG_RE = re.compile(rf"-e\s+{TOKEN_ENV}(?:=|\b)")
# `--env-file <path>` — the file is secrets.env, pinned to still carry the key by
# test_bootstrap_secrets_still_writes_the_admin_token below.
_ENV_FILE_RE = re.compile(r"--env-file\s+\S+")
# compose `environment:` mapping key, e.g. `YADGAR_MCP_AUTH_TOKEN: ${...}`.
_COMPOSE_KEY_RE = re.compile(rf"^\s*{TOKEN_ENV}\s*:", re.MULTILINE)


def _token_reaches_container(rendered: str) -> bool:
    """True when *rendered* makes TOKEN_ENV land in the container's process env."""
    return bool(
        _E_FLAG_RE.search(rendered)
        or _ENV_FILE_RE.search(rendered)
        or _COMPOSE_KEY_RE.search(rendered)
    )


# ── Renderers: (label, role) -> the rendered text for THAT role only ───────────
#
# Every renderer slices to a single role. Concatenating both units into one blob
# would let the backend's `-e` satisfy the core's assertion (and vice versa) —
# the false green this suite exists to prevent.


def _render_systemd_sh(tmp_path: Path, role: str) -> str:
    render_systemd(tmp_path)
    unit = "yadgar-backend.service" if role == "backend" else "yadgar.service"
    return (tmp_path / "units" / unit).read_text()


def _render_launchd_sh(tmp_path: Path, role: str) -> str:
    render_launchd(tmp_path)
    name = (
        "com.openfantasy.yadgar-backend.plist"
        if role == "backend"
        else "com.openfantasy.yadgar.plist"
    )
    plist = plistlib.loads((tmp_path / "units" / name).read_bytes())
    return "\n".join(plist["ProgramArguments"]) + "\n" + repr(plist["EnvironmentVariables"])


def _render_python_systemd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    profile = _prod_profile(8765)
    result = systemd_mod.install_systemd_service(profile, dev=False)
    key = "backend_service" if role == "backend" else "core_service"
    return Path(result[key]).read_text()


# flake.nix declares each unit as a top-level `systemd.user.<kind>.<name> = {`
# attribute at a fixed indent. Anchoring on newline+indent (not the bare
# substring) keeps prose in a comment from truncating a block. Lifted from
# test_vacuum_trigger_cross_generator.py so the two stay in step.
_ATTR_ANCHOR = "\n            systemd.user."


def _flake_attr_block(attr: str) -> str | None:
    text = FLAKE_NIX.read_text()
    marker = f"{_ATTR_ANCHOR}{attr} = {{"
    if marker not in text:
        return None
    start = text.index(marker) + len(_ATTR_ANCHOR)
    rest = text[start:]
    end = rest.find(_ATTR_ANCHOR)
    return rest[:end] if end != -1 else rest


def _render_flake(_tmp_path: Path, role: str) -> str:
    attr = "services.yadgar-backend" if role == "backend" else "services.yadgar"
    block = _flake_attr_block(attr)
    assert block is not None, f"flake.nix has no systemd.user.{attr} unit"
    # The ExecStart is a Nix list of quoted strings; blank the quotes so `-e`
    # tokens parse the same way a rendered shell command line does.
    return block.replace('"', " ")


def _slice_between(text: str, start_marker: str, end_marker: str, label: str) -> str:
    start = text.find(start_marker)
    assert start != -1, f"{label}: {start_marker!r} not found"
    end = text.find(end_marker, start + len(start_marker))
    assert end != -1, f"{label}: {end_marker!r} not found after {start_marker!r}"
    return text[start:end]


def _render_daemon_docker_run(_tmp_path: Path, role: str) -> str:
    """The `yadgar daemon start` docker-run dev path.

    Asserted as source text rather than by driving DaemonManager: the `docker
    run` argv is built inline inside the start methods, not by an extractable
    command-builder, and driving the real method needs an image-exists probe
    plus a live backend. Sliced per method so `start_backend`'s `-e` cannot
    satisfy `start`'s assertion.
    """
    text = DAEMON_PY.read_text()
    if role == "backend":
        return _slice_between(text, "def start_backend(", "\n    def stop(", "daemon.py")
    return _slice_between(text, "def start(", "\n    def start_backend(", "daemon.py")


def _render_compose(_tmp_path: Path, role: str) -> str:
    """docker-compose.yml sliced to ONE service — read as TEXT (see module docstring).

    Both slices are bounded by the NEXT top-level service key (two-space indent,
    trailing colon), never by EOF: an unbounded tail would let a future third
    service's token satisfy `core`'s assertion.
    """
    text = COMPOSE_YML.read_text()
    services = [m.start() for m in re.finditer(r"^  [A-Za-z0-9_.-]+:$", text, re.MULTILINE)]
    key = f"\n  {'backend' if role == 'backend' else 'core'}:\n"
    start = text.find(key)
    assert start != -1, f"docker-compose.yml: no `{role}:` service"
    later = [pos for pos in services if pos > start + 1]
    return text[start : later[0]] if later else text[start:]


_RENDERERS = {
    "generate_systemd.sh": lambda tmp, _mp, role: _render_systemd_sh(tmp, role),
    "generate_launchd.sh": lambda tmp, _mp, role: _render_launchd_sh(tmp, role),
    "install_systemd_service (Python)": _render_python_systemd,
    "flake.nix": lambda tmp, _mp, role: _render_flake(tmp, role),
    "daemon.py docker-run": lambda tmp, _mp, role: _render_daemon_docker_run(tmp, role),
    "docker-compose.yml": lambda tmp, _mp, role: _render_compose(tmp, role),
}


# ── The invariant ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["backend", "core"])
@pytest.mark.parametrize("label", sorted(_RENDERERS))
def test_generator_wires_admin_token_into_container(label, role, tmp_path, monkeypatch):
    """THE INVARIANT (ADR-0180): every generator that renders a container which
    serves or calls /admin/* puts YADGAR_MCP_AUTH_TOKEN in that container's env."""
    rendered = _RENDERERS[label](tmp_path, monkeypatch, role)
    served = "serves" if role == "backend" else "calls"
    assert _token_reaches_container(rendered), (
        f"{label} ({role}): the rendered unit never puts {TOKEN_ENV} in the "
        f"container's env — no `-e {TOKEN_ENV}[=...]`, no `--env-file`, no "
        f"compose `environment:` key. The {role} container {served} /admin/*, so "
        f"every admin call there fails closed on a fresh install (ADR-0180). Add "
        f"one of the four accepted shapes; see this module's docstring."
    )


def test_bootstrap_secrets_still_writes_the_admin_token():
    """Guard the guard: the `--env-file` shape is only honest while secrets.env
    actually carries the key.

    The launchd plists and daemon.py pass `--env-file <secrets.env>` and nothing
    else — their green above is *entirely* conditional on this. A future edit to
    bootstrap_secrets.sh that drops the key would silently rot three surfaces
    while every assertion above stayed green.
    """
    text = BOOTSTRAP_SECRETS_SH.read_text()
    m = re.search(r"REQUIRED_KEYS=\(([^)]*)\)", text)
    assert m, "bootstrap_secrets.sh no longer declares REQUIRED_KEYS"
    assert TOKEN_ENV in m.group(1).split(), (
        f"bootstrap_secrets.sh REQUIRED_KEYS no longer contains {TOKEN_ENV} — "
        f"every `--env-file`-satisfied surface in this suite is now a false green"
    )
    assert re.search(rf"^{TOKEN_ENV}=", text, re.MULTILINE), (
        f"bootstrap_secrets.sh no longer writes a {TOKEN_ENV}= line into secrets.env"
    )


def test_every_backend_or_core_template_is_covered_by_this_suite():
    """Auto-detect a NEW unit template so a fourth instance cannot ship unseen.

    The parametrization above is a hand-maintained list; a car that adds a new
    `.service.in` / `.plist.in` running the backend or core image would not
    appear in it and would ship uncovered — the exact shape of the three
    historical misses. This scans the install tree instead of trusting the list.
    """
    covered = {
        "yadgar.service.in",
        "yadgar-backend.service.in",
        "com.openfantasy.yadgar.plist.in",
        "com.openfantasy.yadgar-backend.plist.in",
    }
    # Detect by CONTAINER NAME, not by image placeholder: the templates spell the
    # image four different ways (`@BACKEND_IMAGE@`, `@YADGAR_CORE_IMAGE@`,
    # `${YADGAR_IMAGE_TAG}` from upgrade.env, ...), so an image-token list rots
    # the moment one of them changes. `run --name yadgar` / `--name yadgar-backend`
    # is what actually identifies a core/backend container unit.
    _CONTAINER_RE = re.compile(r"run\s+--name\s+yadgar(?:-backend)?\s")
    found = {
        p.name
        for p in [*INSTALL_DIR.glob("*.in"), *LAUNCHD_DIR.glob("*.in")]
        if _CONTAINER_RE.search(p.read_text())
    }
    assert found == covered, (
        f"install-tree templates running the core/backend image changed: "
        f"{found ^ covered}. Every such template renders a container that serves "
        f"or calls /admin/* — add it to _RENDERERS above (or, if it genuinely "
        f"needs no token, drop it from `covered` with a cited reason) so the "
        f"ADR-0180 invariant keeps covering the whole surface."
    )


def test_generate_systemd_sh_actually_runs_in_this_environment(tmp_path):
    """Sanity: a generator that fails to render would make every assertion above
    vacuous via an unrelated error rather than a real green."""
    result = subprocess.run(
        [BASH, "-n", str(INSTALL_DIR / "generate_systemd.sh")],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert result.returncode == 0, f"generate_systemd.sh is not valid bash\n{result.stderr}"
