#!/usr/bin/env python3
"""Guard: Dockerfile.ci-viz's two hand-maintained version strings must agree.

Task 415. ``Dockerfile.ci-viz`` carries the yadgar-ci base tag TWICE — once as
``ARG BASE_TAG`` (what ``FROM docker.io/openfantasy/yadgar-ci:${BASE_TAG}``
pulls) and once as an OCI ``org.opencontainers.image.version`` LABEL (what the
built artifact announces about itself). Its own header comment states the
contract — "Version: must track yadgar-ci base version. Bump both together." —
and nothing checked it. Both sat at 5.190.0 while 5.191.0 was the base being
built from, so a viz image built at 5.191.0 would have announced itself as
5.190.0 in its own metadata. That drift was found by hand, not by a gate.

WHAT THIS GATE CAN AND CANNOT CATCH
-----------------------------------
It CANNOT compare against the tag CI actually pulls. ADR-0135 moved that tag
OUT of git: every workflow references
``docker.io/openfantasy/yadgar-ci:${{ vars.YADGAR_VERSION }}``, a Forgejo repo
variable edited in the UI with no PR and no commit. Nothing in this repository
can read it, so "the strings match the deployed image" is not a checkable
statement here and this gate does not pretend otherwise. A wrong-but-internally
consistent pair still passes.

It also does NOT require equality with ``pyproject.toml``. yadgar-ci has no
auto-sync pipeline — it is manually rebuilt and pushed (ADR-0135, and
Dockerfile.ci's own LABEL comment) — so the CI images legitimately LAG the
source version between rebuilds. Demanding equality would fail on every release
bump and could only be greened by naming a base tag that is not published,
which would break the ci-viz build outright.

What it DOES enforce, both mechanically checkable from files in this tree:

  RULE 1  ``ARG BASE_TAG`` == ``LABEL org.opencontainers.image.version``.
          The file's own stated contract, and the actual content of task 415:
          the image must not pull one base and announce a different one.

  RULE 2  ``BASE_TAG`` <= ``pyproject.toml``'s version. A base tag NEWER than
          anything this repository has ever produced cannot exist in the
          registry — that is a typo or a forward-dated edit, and the ci-viz
          build would fail on a pull nobody can satisfy. Lag is legal; lead is
          not.

An unextractable string is a FAILURE, never a pass. A regex that quietly
misses and exits 0 is the precise shape of the nine gates the "Gate honesty"
train (2026-08-28) had to repair; a value left interpolated (``"${...}"``) in
the LABEL is likewise refused rather than skipped, since the whole point of the
LABEL is that it is baked literally into the artifact.

Deliberately stdlib-only (re/sys/pathlib), matching ``check_versions.py`` and
``check_version_compat_window.py`` in this same hook family — it imports no
``yadgar`` module, so it cannot be slowed by tracing setup on an ``always_run``
hook, and it reads the REPO's committed files by explicit path rather than
whatever copy happens to be importable.

Exit codes:
  0  the two strings agree and do not lead pyproject's version
  1  they disagree, one is unextractable, or BASE_TAG leads pyproject
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# ``ARG BASE_TAG=5.191.0`` — the default the FROM interpolates when no
# --build-arg is passed.
_BASE_TAG_RE = re.compile(r"^ARG\s+BASE_TAG\s*=\s*(\S+)\s*$", re.MULTILINE)

# ``LABEL org.opencontainers.image.version="5.191.0"``. Anchored on
# ``image.version`` specifically: the file carries three LABEL lines, and a
# looser pattern would happily match image.source or image.description.
_LABEL_RE = re.compile(
    r'^LABEL\s+org\.opencontainers\.image\.version\s*=\s*"([^"]*)"\s*$',
    re.MULTILINE,
)

_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)

# major.minor[.patch] — patch optional. Mirrors check_version_compat_window.py.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def _parse(version: str) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)``, or ``None`` when unparseable."""
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def check(dockerfile_text: str, pyproject_text: str) -> tuple[bool, str]:
    """Pure decision logic — no filesystem access, no git.

    Args:
        dockerfile_text: Raw text of Dockerfile.ci-viz.
        pyproject_text: Raw text of pyproject.toml.

    Returns:
        ``(ok, message)``. ``ok=False`` for a disagreement, an unextractable
        string, or a BASE_TAG that leads pyproject's version.
    """
    base_match = _BASE_TAG_RE.search(dockerfile_text)
    label_match = _LABEL_RE.search(dockerfile_text)

    if base_match is None or label_match is None:
        missing = []
        if base_match is None:
            missing.append("`ARG BASE_TAG=<version>`")
        if label_match is None:
            missing.append('`LABEL org.opencontainers.image.version="<version>"`')
        return False, (
            f"Dockerfile.ci-viz: could not extract {' and '.join(missing)}. "
            "This gate refuses to pass on a string it cannot read — a silent "
            "miss here is indistinguishable from agreement."
        )

    base_tag = base_match.group(1).strip()
    label = label_match.group(1).strip()

    parsed_base = _parse(base_tag)
    if parsed_base is None or _parse(label) is None:
        return False, (
            f"Dockerfile.ci-viz: BASE_TAG={base_tag!r} / LABEL version={label!r} "
            "— at least one is not a literal major.minor[.patch]. Neither may be "
            "left interpolated or blank: the LABEL is baked verbatim into the "
            "artifact and the ARG default is what a plain `podman build` pulls."
        )

    # RULE 1 — the file's own contract.
    if base_tag != label:
        return False, (
            f"Dockerfile.ci-viz disagrees with itself: ARG BASE_TAG={base_tag} but "
            f'LABEL org.opencontainers.image.version="{label}". The image would '
            "pull one base and announce another. Its header says so: "
            '"Version: must track yadgar-ci base version. Bump both together."'
        )

    # RULE 2 — cannot claim a base newer than anything this repo produced.
    pyproject_match = _PYPROJECT_VERSION_RE.search(pyproject_text)
    if pyproject_match is None:
        return False, 'pyproject.toml: could not extract `version = "..."`.'
    pyproject_version = pyproject_match.group(1)
    parsed_pyproject = _parse(pyproject_version)
    if parsed_pyproject is None:
        return False, (
            f"pyproject.toml: version {pyproject_version!r} is not a parseable major.minor[.patch]."
        )

    if parsed_base > parsed_pyproject:
        return False, (
            f"Dockerfile.ci-viz names base tag {base_tag}, which LEADS "
            f"pyproject.toml's {pyproject_version}. No such yadgar-ci image can "
            "have been built from this repository, so the FROM would fail on a "
            "pull nobody can satisfy. Lagging the source version is legal "
            "(yadgar-ci is rebuilt manually — ADR-0135); leading it is not."
        )

    lag = "" if parsed_base == parsed_pyproject else f" (lags pyproject {pyproject_version})"
    return True, f"Dockerfile.ci-viz: BASE_TAG == LABEL == {base_tag}{lag}"


def main() -> int:
    dockerfile_path = _ROOT / "Dockerfile.ci-viz"
    pyproject_path = _ROOT / "pyproject.toml"

    for path in (dockerfile_path, pyproject_path):
        if not path.exists():
            print(f"ERROR: {path.name} not found at {path}", file=sys.stderr)
            return 1

    ok, message = check(dockerfile_path.read_text(), pyproject_path.read_text())
    if not ok:
        print(f"ERROR: {message}", file=sys.stderr)
        return 1
    print(f"OK: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
