"""Static pin: every core-liveness health probe must target /health/live.

ADR-0019 (accepted 2026-06-30): the core's own healthcheck must probe
/health/live (liveness, answerable from the core's own loop), not /health
(readiness, which proxies backend health and caused SIGKILLs under
--health-on-failure=kill when the backend was transiently busy).

This is the missing pin: the ADR's consequences clause sat half-applied
(flake.nix + Dockerfile still probed /health) for a month with nothing to
catch it. This test asserts the invariant across all THREE surfaces that probe
the core's own port (8765) — flake.nix, Dockerfile, docker-compose.yml —
so the defect class cannot silently recur.

Explicitly OUT of scope: the backend/embed-service healthcheck (port
8001, present in both flake.nix and docker-compose.yml) — that is the
embed service probing its own /health, a different service with no
/health/live endpoint. ADR-0019 governs the core's liveness probe only.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
FLAKE_NIX = REPO_ROOT / "flake.nix"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKER_COMPOSE = REPO_ROOT / "docker-compose.yml"

# Matches ":8765/health" NOT followed by "/live" — i.e. a stale readiness
# probe against the core's own port.
_STALE_CORE_PROBE = re.compile(r":8765/health(?!/live)")


def test_flake_nix_core_healthcheck_uses_liveness_endpoint():
    """flake.nix must not probe the core's :8765/health (readiness) directly."""
    assert FLAKE_NIX.exists(), f"Missing: {FLAKE_NIX}"
    content = FLAKE_NIX.read_text()
    match = _STALE_CORE_PROBE.search(content)
    assert match is None, (
        f"flake.nix probes stale core readiness endpoint {match.group(0)!r} "
        "instead of /health/live (ADR-0019)"
    )
    assert ":8765/health/live" in content, "flake.nix must probe :8765/health/live somewhere"


def test_dockerfile_healthcheck_uses_liveness_endpoint():
    """Dockerfile's baked HEALTHCHECK must not probe :8765/health (readiness) directly."""
    assert DOCKERFILE.exists(), f"Missing: {DOCKERFILE}"
    content = DOCKERFILE.read_text()
    match = _STALE_CORE_PROBE.search(content)
    assert match is None, (
        f"Dockerfile probes stale core readiness endpoint {match.group(0)!r} "
        "instead of /health/live (ADR-0019)"
    )
    assert ":8765/health/live" in content, "Dockerfile must probe :8765/health/live somewhere"


def test_docker_compose_healthcheck_uses_liveness_endpoint():
    """docker-compose.yml's core healthcheck must not probe :8765/health (readiness) directly."""
    assert DOCKER_COMPOSE.exists(), f"Missing: {DOCKER_COMPOSE}"
    content = DOCKER_COMPOSE.read_text()
    match = _STALE_CORE_PROBE.search(content)
    assert match is None, (
        f"docker-compose.yml probes stale core readiness endpoint {match.group(0)!r} "
        "instead of /health/live (ADR-0019)"
    )
    assert ":8765/health/live" in content, (
        "docker-compose.yml must probe :8765/health/live somewhere"
    )
