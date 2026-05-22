#!/usr/bin/env python3
"""Release-readiness checker: verify container image size stays within budget.

Usage::

    python scripts/check_image_size.py --image docker.io/openfantasy/yadgar-backend:5.0.3
    python scripts/check_image_size.py --image docker.io/openfantasy/yadgar:5.4.2 --max-size-gb 0.8

    # Used by pre-commit hooks (resolves version from server.json automatically):
    python scripts/check_image_size.py --image-type backend
    python scripts/check_image_size.py --image-type core

Exit codes:
    0  — image is within cap (warnings may be printed for large individual layers)
    1  — image exceeds the size cap

Defaults (auto-detected from image name):
    backend image (name contains "backend"): cap 2.0 GB
    core image (everything else):            cap 0.8 GB
    warn threshold:                          500 MB per layer

Requires podman or docker to be available on PATH (podman tried first).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — public so tests can import them.
# ---------------------------------------------------------------------------

DEFAULT_BACKEND_CAP_GB: float = 2.0
DEFAULT_CORE_CAP_GB: float = 0.8
DEFAULT_WARN_LAYER_MB: float = 500.0

# Multipliers for human-readable size strings (case-insensitive suffix lookup).
_SIZE_UNITS: dict[str, float] = {
    "gb": 1e9,
    "mb": 1e6,
    "kb": 1e3,
    "b": 1.0,
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ImageSizeResult:
    """Outcome from evaluate()."""

    total_bytes: float
    cap_bytes: float
    over_budget: bool
    warn_layers: list[tuple[float, str]] = field(default_factory=list)  # (bytes, cmd)
    exit_code: int = 0


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def parse_size(raw: str) -> float:
    """Convert a human-readable size string to bytes.

    Accepts: "1.36GB", "119MB", "19.5kB", "512B", "1024" (bare number = bytes).
    Case-insensitive suffix; leading/trailing whitespace stripped.
    Cyclomatic complexity: 4 (well within I13 cap of 15).
    """
    raw = raw.strip()
    if not raw:
        return 0.0

    # Try to split off a recognised suffix (longest match first to avoid 'b' eating 'kb').
    for suffix, multiplier in sorted(_SIZE_UNITS.items(), key=lambda kv: -len(kv[0])):
        if raw.lower().endswith(suffix):
            numeric = raw[: -len(suffix)]
            try:
                return float(numeric) * multiplier
            except ValueError:
                return 0.0

    # No recognised suffix — treat as bare byte count.
    try:
        return float(raw)
    except ValueError:
        return 0.0


def detect_caps(image: str) -> tuple[float, float]:
    """Return (cap_gb, warn_layer_mb) based on image name heuristic.

    "backend" in name → backend defaults; otherwise core defaults.
    """
    if "backend" in image.lower():
        return DEFAULT_BACKEND_CAP_GB, DEFAULT_WARN_LAYER_MB
    return DEFAULT_CORE_CAP_GB, DEFAULT_WARN_LAYER_MB


def run_history(image: str) -> list[tuple[float, str]]:
    """Run `podman history` (fallback: `docker history`) and return parsed layers.

    Returns list of (size_bytes, created_by_command) tuples.
    Raises RuntimeError if neither tool is available or both fail.
    Cyclomatic complexity: 5.
    """
    fmt = "{{.Size}}\t{{.CreatedBy}}"
    cmd_podman = ["podman", "history", "--no-trunc", "--format", fmt, image]
    cmd_docker = ["docker", "history", "--no-trunc", "--format", fmt, image]

    result = subprocess.run(cmd_podman, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        result = subprocess.run(cmd_docker, capture_output=True, text=True)  # noqa: S603
        if result.returncode != 0:
            raise RuntimeError(
                f"Neither podman nor docker succeeded for image {image!r}. "
                "Ensure the image is pulled locally and podman or docker is on PATH."
            )

    return _parse_history_output(result.stdout)


def _parse_history_output(output: str) -> list[tuple[float, str]]:
    """Parse tab-separated history output into (bytes, cmd) pairs."""
    layers: list[tuple[float, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        size_bytes = parse_size(parts[0])
        cmd = parts[1].strip() if len(parts) > 1 else ""
        layers.append((size_bytes, cmd))
    return layers


def evaluate(
    layers: list[tuple[float, str]],
    total_bytes: float,
    cap_gb: float,
    warn_layer_mb: float,
) -> ImageSizeResult:
    """Apply threshold logic; return ImageSizeResult with exit_code set.

    Exit 1 when total > cap. Exit 0 for big-layer warnings only.
    Cyclomatic complexity: 4.
    """
    cap_bytes = cap_gb * 1e9
    warn_bytes = warn_layer_mb * 1e6

    warn_layers = [(s, cmd) for s, cmd in layers if s > warn_bytes]
    over_budget = total_bytes > cap_bytes
    exit_code = 1 if over_budget else 0

    return ImageSizeResult(
        total_bytes=total_bytes,
        cap_bytes=cap_bytes,
        over_budget=over_budget,
        warn_layers=warn_layers,
        exit_code=exit_code,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _fmt_bytes(n: float) -> str:
    """Format bytes as human-readable string for display."""
    for unit, threshold in [("GB", 1e9), ("MB", 1e6), ("kB", 1e3)]:
        if n >= threshold:
            return f"{n / threshold:.2f} {unit}"
    return f"{n:.0f} B"


def format_report(image: str, result: ImageSizeResult) -> str:
    """Build a human-readable report string from an ImageSizeResult."""
    lines: list[str] = []
    status = "OVER BUDGET" if result.over_budget else "OK"
    lines.append(
        f"[check-image-size] {image}: {_fmt_bytes(result.total_bytes)} "
        f"(cap {_fmt_bytes(result.cap_bytes)}) — {status}"
    )

    for size, cmd in result.warn_layers:
        short_cmd = cmd[:80] + "..." if len(cmd) > 80 else cmd
        lines.append(f"  WARN large layer: {_fmt_bytes(size)} — {short_cmd}")

    if result.over_budget:
        excess = result.total_bytes - result.cap_bytes
        lines.append(f"  ERROR: image exceeds cap by {_fmt_bytes(excess)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# server.json helper (used by --image-type)
# ---------------------------------------------------------------------------

_REGISTRY_PREFIX = "docker.io/openfantasy"


def _resolve_image_from_type(image_type: str) -> str:
    """Resolve full image ref from image_type (backend|core) using server.json.

    Reads server.json from the repo root (two levels up from scripts/).
    Cyclomatic complexity: 3.
    """
    server_json = Path(__file__).parent.parent / "server.json"
    try:
        data = json.loads(server_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read server.json: {exc}") from exc

    if image_type == "backend":
        version = data.get("backend_version")
        if not version:
            raise RuntimeError("server.json missing 'backend_version' field")
        return f"{_REGISTRY_PREFIX}/yadgar-backend:{version}"

    # core
    version = data.get("version")
    if not version:
        raise RuntimeError("server.json missing 'version' field")
    return f"{_REGISTRY_PREFIX}/yadgar:{version}"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Check container image total size against a release-readiness cap."
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--image",
        help="Full image reference (must be pulled locally).",
    )
    grp.add_argument(
        "--image-type",
        choices=["backend", "core"],
        help="Resolve image ref from server.json (backend→yadgar-backend:VER, core→yadgar:VER).",
    )
    p.add_argument(
        "--max-size-gb",
        type=float,
        default=None,
        help="Hard cap in GB. Default: auto-detect (backend→2.0, core→0.8).",
    )
    p.add_argument(
        "--warn-layer-mb",
        type=float,
        default=None,
        help="Warn threshold per layer in MB (exit 0). Default: 500 MB.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.image_type is not None:
        try:
            image = _resolve_image_from_type(args.image_type)
        except RuntimeError as exc:
            print(f"[check-image-size] ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        image = args.image

    auto_cap_gb, auto_warn_mb = detect_caps(image)
    cap_gb = args.max_size_gb if args.max_size_gb is not None else auto_cap_gb
    warn_mb = args.warn_layer_mb if args.warn_layer_mb is not None else auto_warn_mb

    try:
        layers = run_history(image)
    except RuntimeError as exc:
        print(f"[check-image-size] ERROR: {exc}", file=sys.stderr)
        return 1

    total = sum(s for s, _ in layers)
    result = evaluate(layers, total, cap_gb=cap_gb, warn_layer_mb=warn_mb)

    report = format_report(image, result)
    out = sys.stderr if result.over_budget else sys.stdout
    print(report, file=out)

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
