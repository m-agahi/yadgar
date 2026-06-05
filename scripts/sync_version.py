#!/usr/bin/env python3
"""Sync version from pyproject.toml into server.json and flake.nix."""

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

changed = False

server_json_path = root / "server.json"
data = json.loads(server_json_path.read_text())
if data.get("version") != version:
    data["version"] = version
    changed = True
for pkg in data.get("packages", []):
    if pkg.get("version") != version:
        pkg["version"] = version
        changed = True

if data.get("version") == version and all(
    pkg.get("version") == version for pkg in data.get("packages", [])
):
    server_changed = False
else:
    server_changed = True

if changed:
    server_json_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"server.json updated to {version}")

flake_nix_path = root / "flake.nix"
if flake_nix_path.exists():
    flake_text = flake_nix_path.read_text()
    new_flake_text, n_subs = re.subn(
        r'(version\s*=\s*")[^"]+(";)',
        rf"\g<1>{version}\g<2>",
        flake_text,
        count=1,
    )
    if n_subs == 0:
        print("ERROR: could not find version field in flake.nix", file=sys.stderr)
        sys.exit(1)
    if new_flake_text != flake_text:
        flake_nix_path.write_text(new_flake_text)
        print(f"flake.nix updated to {version}")
        changed = True

if changed:
    sys.exit(1)  # exit 1 so pre-commit re-stages the file
