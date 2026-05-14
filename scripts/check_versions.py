#!/usr/bin/env python3
"""Check version consistency across pyproject.toml, server.json, and docker-compose.yml."""

import json
import re
import sys
from pathlib import Path

root = Path(__file__).parent.parent

# --- extract versions ---

toml_text = (root / "pyproject.toml").read_text()
m = re.search(r'^version\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
if not m:
    print("ERROR: version not found in pyproject.toml", file=sys.stderr)
    sys.exit(1)
pyproject_version = m.group(1)

server_data = json.loads((root / "server.json").read_text())
server_core_version = server_data.get("version", "")
server_backend_version = server_data.get("backend_version", "")
server_pkg_versions = [p.get("version", "") for p in server_data.get("packages", [])]

compose_text = (root / "docker-compose.yml").read_text()
m_core = re.search(r"\$\{CORE_VERSION:-([^}]+)\}", compose_text)
m_backend = re.search(r"\$\{BACKEND_VERSION:-([^}]+)\}", compose_text)
compose_core_version = m_core.group(1) if m_core else ""
compose_backend_version = m_backend.group(1) if m_backend else ""

# --- build comparison table ---

rows = [
    ("pyproject.toml", "core", pyproject_version),
    ("server.json version", "core", server_core_version),
    ("server.json backend_ver", "backend", server_backend_version),
    ("docker-compose CORE", "core", compose_core_version),
    ("docker-compose BACKEND", "backend", compose_backend_version),
]
for i, pkg_ver in enumerate(server_pkg_versions):
    rows.append((f"server.json packages[{i}]", "core", pkg_ver))

# --- determine canonical versions ---

core_versions = {v for src, role, v in rows if role == "core" and v}
backend_versions = {v for src, role, v in rows if role == "backend" and v}

mismatches = []
if len(core_versions) > 1:
    mismatches.append(("core", core_versions))
if len(backend_versions) > 1:
    mismatches.append(("backend", backend_versions))

# NOTE: core and backend versions are tracked independently since v4.7.0
# (split-versions feature). They are NOT required to match — only the
# intra-role checks above are enforced.

if not mismatches:
    sys.exit(0)

# --- print diff table ---

col_src = max(len(src) for src, _, _ in rows)
col_role = max(len(role) for _, role, _ in rows)
col_ver = max(len(v) for _, _, v in rows)
header = f"{'source':<{col_src}}  {'role':<{col_role}}  {'version':<{col_ver}}"
sep = "-" * len(header)

print("VERSION MISMATCH DETECTED", file=sys.stderr)
print(sep, file=sys.stderr)
print(header, file=sys.stderr)
print(sep, file=sys.stderr)
for src, role, ver in rows:
    print(f"{src:<{col_src}}  {role:<{col_role}}  {ver:<{col_ver}}", file=sys.stderr)
print(sep, file=sys.stderr)
for label, versions in mismatches:
    print(f"  conflict ({label}): {', '.join(sorted(versions))}", file=sys.stderr)

sys.exit(1)
