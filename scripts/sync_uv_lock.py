#!/usr/bin/env python3
"""Re-lock uv.lock when pyproject.toml version changes."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).parent.parent
lock_path = root / "uv.lock"

before = lock_path.read_text() if lock_path.exists() else ""
try:
    result = subprocess.run(["uv", "lock"], cwd=root, capture_output=True, text=True)
except FileNotFoundError:
    sys.exit(0)  # uv not installed (e.g. CI) — skip, check-versions handles validation
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    sys.exit(1)

after = lock_path.read_text() if lock_path.exists() else ""
if before != after:
    print("uv.lock updated — re-stage and retry commit")
    sys.exit(1)
