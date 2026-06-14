#!/usr/bin/env python3
"""Sync version from pyproject.toml into server.json, flake.nix, and docker-compose.yml."""

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

# ── server.json ──────────────────────────────────────────────────────────────
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

backend_version = data.get("backend_version", "")

# ── flake.nix ─────────────────────────────────────────────────────────────────
flake_nix_path = root / "flake.nix"
if flake_nix_path.exists():
    flake_text = flake_nix_path.read_text()

    # 1. package version field (existing)
    new_flake_text, n_subs = re.subn(
        r'(version\s*=\s*")[^"]+(";)',
        rf"\g<1>{version}\g<2>",
        flake_text,
        count=1,
    )
    if n_subs == 0:
        print("ERROR: could not find version field in flake.nix", file=sys.stderr)
        sys.exit(1)

    # 2. coreVersion option default
    new_flake_text, n_core = re.subn(
        r'(coreVersion = lib\.mkOption \{[^}]*?default = ")[^"]+(";)',
        rf"\g<1>{version}\g<2>",
        new_flake_text,
        count=1,
        flags=re.DOTALL,
    )
    if n_core == 0:
        print("ERROR: could not find coreVersion default in flake.nix", file=sys.stderr)
        sys.exit(1)

    # 3. backendVersion option default
    if backend_version:
        new_flake_text, n_be = re.subn(
            r'(backendVersion = lib\.mkOption \{[^}]*?default = ")[^"]+(";)',
            rf"\g<1>{backend_version}\g<2>",
            new_flake_text,
            count=1,
            flags=re.DOTALL,
        )
        if n_be == 0:
            print("ERROR: could not find backendVersion default in flake.nix", file=sys.stderr)
            sys.exit(1)

    if new_flake_text != flake_text:
        flake_nix_path.write_text(new_flake_text)
        print(
            f"flake.nix updated (version={version}, coreVersion={version}, backendVersion={backend_version})"
        )
        changed = True

# ── docker-compose.yml ───────────────────────────────────────────────────────
compose_path = root / "docker-compose.yml"
if compose_path.exists():
    compose_text = compose_path.read_text()

    new_compose_text, n_core = re.subn(
        r"(\$\{CORE_VERSION:-)[^}]+(})",
        rf"\g<1>{version}\g<2>",
        compose_text,
    )
    if backend_version:
        new_compose_text, n_be = re.subn(
            r"(\$\{BACKEND_VERSION:-)[^}]+(})",
            rf"\g<1>{backend_version}\g<2>",
            new_compose_text,
        )
    if new_compose_text != compose_text:
        compose_path.write_text(new_compose_text)
        print(f"docker-compose.yml updated (core={version}, backend={backend_version})")
        changed = True

if changed:
    sys.exit(1)  # exit 1 so pre-commit re-stages the file
