#!/usr/bin/env python3
"""Sync version from pyproject.toml into server.json."""

import json
import re
import sys
from pathlib import Path

root = Path(__file__).parent.parent

toml = (root / "pyproject.toml").read_text()
match = re.search(r'^version\s*=\s*"(.+?)"', toml, re.MULTILINE)
if not match:
    print("ERROR: could not find version in pyproject.toml", file=sys.stderr)
    sys.exit(1)

version = match.group(1)

server_json_path = root / "server.json"
data = json.loads(server_json_path.read_text())

changed = False
if data.get("version") != version:
    data["version"] = version
    changed = True
for pkg in data.get("packages", []):
    if pkg.get("version") != version:
        pkg["version"] = version
        changed = True

if changed:
    server_json_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"server.json updated to {version}")
    sys.exit(1)  # exit 1 so pre-commit re-stages the file
