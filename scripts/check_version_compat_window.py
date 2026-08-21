#!/usr/bin/env python3
"""Guard: server.json's shipped versions must fall inside version_compat.json's window.

Task 173. ``yadgar/_shared/version_compat.json`` declares the core/backend
version window the ``/health`` handshake reports on
(``versions_compatible.compatible``). Nothing gates a live process on that
field (see the sidecar's own ``_comment`` — informational by design, so a
fresh install is never refused), but the DECLARATION itself still needs to be
honest. It went stale silently for five minor core releases and five minor
backend releases: ``core.max`` was pinned at ``5.183.1`` and
``backend.max`` at ``5.75.0`` while production shipped core ``5.188.0`` /
backend ``5.80.0`` — a correctly-matched, working pair that ``/health``
nonetheless reported as incompatible. Car 12 of the previous train fixed the
*reporting* half (``self_version`` now reports the process's own version,
task 234); nobody re-bumped the *data*.

This hook closes that gap the same way ``check_versions.py`` and
``check_backend_bump.py`` close their own gate-blindness classes (ADR-0080):
``always_run``, because ``server.json``'s ``version`` / ``backend_version``
and ``yadgar/_shared/version_compat.json``'s bounds are two independent
files that can each be edited (or NOT edited) without the other being
staged — the exact combination that let this drift for 5 releases with every
individual commit looking green.

Deliberately stdlib-only (json/re/sys/pathlib), matching check_versions.py
and check_backend_bump.py in this same hook family. It does NOT import
``yadgar._shared.version_compat`` (the module the ``/health`` handshake
actually calls) for two reasons:

  1. That module imports ``yadgar._shared.observability.observe`` at module
     load, which pulls in tracing setup — scripts/scan_db_for_secrets.py's
     own header documents the measured cost (OTLP exporter hangs ~10s at
     process exit retrying a host that doesn't resolve). This hook is
     ``always_run``: every hook in this family justifies that with
     "sub-second on this tree"; a hook that can hang 10s on every commit in
     the repo gets ``SKIP=``'d within a week.
  2. ``version_compat.py``'s ``_SIDECAR`` path is resolved relative to the
     *imported package* — the repo file under an editable install, but a
     site-packages copy otherwise. A guard whose entire job is validating
     the REPO's committed sidecar must read that file by explicit path, not
     whatever copy happens to be importable.

The comparison logic here is intentionally a small, independent copy of
``version_compat._parse`` / ``_in_range`` (major.minor.patch tuple, patch
optional=0, unverifiable strings pass permissively) rather than a divergent
reinvention. ``yadgar/tests/scripts/test_check_version_compat_window.py``
pins agreement between the two over a shared table of cases, so the two
copies cannot silently drift apart.

Exit codes:
  0  server.json's shipped versions are within the declared window
  1  one or both are outside the window (window is stale — widen it)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# major.minor[.patch] — patch optional, suffix like "a1"/"rc1" tolerated.
# Mirrors yadgar/_shared/version_compat.py's _VERSION_RE exactly.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

# Anything that doesn't parse as a semver-ish tuple is unverifiable, not
# incompatible — mirrors version_compat.py's _UNVERIFIABLE.
_UNVERIFIABLE = frozenset({"", "unknown", "latest", "0.0.0"})


def _parse(version: str) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` or ``None`` if unverifiable."""
    if not version or version in _UNVERIFIABLE:
        return None
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _in_range(version: str, bounds: dict[str, str]) -> bool:
    """``version`` satisfies ``bounds = {"min": ..., "max": ...}``.

    Unverifiable input, or an unverifiable/missing bound, is permissive
    (returns True) — same contract as version_compat.py's _in_range.
    """
    parsed = _parse(version)
    if parsed is None:
        return True
    lo = _parse(bounds.get("min", ""))
    hi = _parse(bounds.get("max", ""))
    if lo is not None and parsed < lo:
        return False
    if hi is not None and parsed > hi:
        return False
    return True


def check(server_json_text: str, version_compat_text: str) -> tuple[bool, str]:
    """Pure decision logic — no filesystem access, no git.

    Args:
        server_json_text: Raw text of server.json.
        version_compat_text: Raw text of yadgar/_shared/version_compat.json.

    Returns:
        (ok, message) — ok=True means both tracks are within their declared
        window.
    """
    try:
        server_data = json.loads(server_json_text)
    except json.JSONDecodeError as exc:
        return False, f"server.json is not valid JSON: {exc}"

    try:
        compat_data = json.loads(version_compat_text)
    except json.JSONDecodeError as exc:
        return False, f"yadgar/_shared/version_compat.json is not valid JSON: {exc}"

    core_version = server_data.get("version", "")
    backend_version = server_data.get("backend_version", "")
    core_bounds = compat_data.get("core", {})
    backend_bounds = compat_data.get("backend", {})

    problems: list[str] = []
    if not _in_range(core_version, core_bounds):
        problems.append(f"core version {core_version!r} (window {core_bounds!r})")
    if not _in_range(backend_version, backend_bounds):
        problems.append(f"backend version {backend_version!r} (window {backend_bounds!r})")

    if problems:
        joined = " and ".join(problems)
        return False, (
            f"{joined} fall outside the window declared in "
            "yadgar/_shared/version_compat.json. Bump `max` (or `min`) there "
            "to cover the shipped pair — see the file's own _comment for the "
            "bump policy (major.minor; bump *_max on the next wire-incompatible "
            "change, bump *_min when the oldest supported release retires)."
        )
    return True, (
        f"core={core_version!r} backend={backend_version!r} within the "
        "declared version_compat.json window"
    )


def main() -> int:
    server_json_path = _ROOT / "server.json"
    compat_json_path = _ROOT / "yadgar" / "_shared" / "version_compat.json"
    try:
        server_text = server_json_path.read_text()
    except OSError as exc:
        print(
            f"check-version-compat-window: ERROR: cannot read server.json: {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        compat_text = compat_json_path.read_text()
    except OSError as exc:
        print(
            "check-version-compat-window: ERROR: cannot read "
            f"yadgar/_shared/version_compat.json: {exc}",
            file=sys.stderr,
        )
        return 1

    ok, message = check(server_text, compat_text)
    if not ok:
        print(f"check-version-compat-window: ERROR: {message}", file=sys.stderr)
        return 1
    print(f"check-version-compat-window: OK — {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
