#!/usr/bin/env bash
# Yadgar first-run setup.
#
# Usage:
#   ./scripts/setup.sh
#   ./scripts/setup.sh --db /path/to/backup.surql --archive /path/to/archive
#
# Options:
#   --db <file>        SurrealDB .surql export to restore
#   --archive <dir>    Archive directory (memories/ + wiki/) to restore
#   --version <tag>    Image tag to use (default: 4.4.7)
#   --log-level <lvl>  YADGAR_LOG_LEVEL to inject into the core container (e.g. INFO, DEBUG)
#   --root-user <u>    SurrealDB ROOT username (default: root)
#   --root-pass <p>    SurrealDB ROOT password (default: root)
#   --rw-user <u>      yadgar-rw DB OWNER username (YADGAR_RW_USER)
#   --rw-pass <p>      yadgar-rw DB OWNER password (YADGAR_RW_PASS)
#   --ro-user <u>      yadgar-ro DB VIEWER username (YADGAR_RO_USER)
#   --ro-pass <p>      yadgar-ro DB VIEWER password (YADGAR_RO_PASS)
#   --help             Show this help

set -euo pipefail

CORE_VERSION="4.7.0"
BACKEND_VERSION="4.7.0"
LOG_LEVEL=""
ROOT_USER="root"
ROOT_PASS=""  # required — provide via --root-pass or SECRETS_ENV_FILE
RW_USER=""
RW_PASS=""
RO_USER=""
RO_PASS=""
# Path to the secrets env file written to disk (chmod 600, root-owned).
# All credentials are written here and referenced via EnvironmentFile=
# in the systemd units — passwords never appear in /proc/<pid>/cmdline.
SECRETS_ENV_FILE="/etc/yadgar/secrets.env"
BACKEND_IMAGE="openfantasy/yadgar-backend:${BACKEND_VERSION}"
CORE_IMAGE="openfantasy/yadgar:${CORE_VERSION}"
BACKEND_CONTAINER="yadgar-backend"
CORE_CONTAINER="yadgar"
NETWORK="yadgar-net"
DOCKER="$(command -v docker)"
BACKEND_VOLUME="yadgar-db-data"
YADGAR_DIR="${HOME}/.yadgar"
HF_CACHE="${HOME}/.cache/huggingface"
BACKUP_DIR="${HOME}/.backups/yadgar/db"
DB_FILE=""
ARCHIVE_DIR=""

# ── Arg parsing ───────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)       DB_FILE="$2";      shift 2 ;;
        --archive)  ARCHIVE_DIR="$2";  shift 2 ;;
        --version)  CORE_VERSION="$2"; CORE_IMAGE="openfantasy/yadgar:${CORE_VERSION}"; shift 2 ;;
        --backend-version) BACKEND_VERSION="$2"; BACKEND_IMAGE="openfantasy/yadgar-backend:${BACKEND_VERSION}"; shift 2 ;;
        --log-level) LOG_LEVEL="$2";   shift 2 ;;
        --root-user) ROOT_USER="$2";   shift 2 ;;
        --root-pass) ROOT_PASS="$2";   shift 2 ;;
        --rw-user)  RW_USER="$2";      shift 2 ;;
        --rw-pass)  RW_PASS="$2";      shift 2 ;;
        --ro-user)  RO_USER="$2";      shift 2 ;;
        --ro-pass)  RO_PASS="$2";      shift 2 ;;
        --help|-h)
            sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ── Validate required credentials ────────────────────────────────────────────
# ROOT_PASS is required — fail fast rather than start with default root:root.
if [[ -z "${ROOT_PASS}" ]]; then
    echo "ERROR: --root-pass is required. Provide a strong password for the SurrealDB root user." >&2
    echo "  Example: $(basename "$0") --root-pass \"\$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))'\")" >&2
    exit 1
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

info()  { echo "  $*"; }
ok()    { echo "✓ $*"; }
fail()  { echo "✗ $*" >&2; exit 1; }
step()  { echo; echo "── $* ──"; }

check_docker() {
    if ! command -v docker &>/dev/null; then
        fail "docker not found. Install Docker or Podman with the Docker CLI shim."
    fi
    if ! docker info &>/dev/null 2>&1; then
        fail "Docker daemon not running or not accessible."
    fi
    ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
}

container_running() { docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

wait_healthy() {
    local port="$1" label="$2" deadline=$(( $(date +%s) + 120 ))
    info "Waiting for ${label}..."
    while [[ $(date +%s) -lt $deadline ]]; do
        if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${port}/health', timeout=2)" &>/dev/null 2>&1; then
            ok "${label} ready"
            return 0
        fi
        sleep 3
    done
    fail "${label} did not become healthy in 120s. Check: docker logs ${BACKEND_CONTAINER}"
}

# ── Step 1: directories ───────────────────────────────────────────────────────

step "Creating directories"
mkdir -p "${YADGAR_DIR}/queue" "${YADGAR_DIR}/archive" "${YADGAR_DIR}/dlq" \
         "${HF_CACHE}" "${BACKUP_DIR}"
ok "${YADGAR_DIR}/{queue,archive,dlq}"
ok "${HF_CACHE}"
ok "${BACKUP_DIR}"

# ── Step 2: Docker check ──────────────────────────────────────────────────────

step "Checking Docker"
check_docker

# ── Step 3: pull images ───────────────────────────────────────────────────────

step "Pulling images"
docker image inspect "${BACKEND_IMAGE}" &>/dev/null || docker pull "${BACKEND_IMAGE}"
docker image inspect "${CORE_IMAGE}" &>/dev/null || docker pull "${CORE_IMAGE}"
ok "Images ready"

# ── Step 4: restore archive (file queue) ──────────────────────────────────────

if [[ -n "${ARCHIVE_DIR}" ]]; then
    step "Restoring archive"
    [[ -d "${ARCHIVE_DIR}" ]] || fail "Archive directory not found: ${ARCHIVE_DIR}"
    mkdir -p "${YADGAR_DIR}/archive"
    cp -r "${ARCHIVE_DIR}/." "${YADGAR_DIR}/archive/"
    ok "Archive restored → ${YADGAR_DIR}/archive/"
fi

# ── Step 5: restore DB from .surql ───────────────────────────────────────────

if [[ -n "${DB_FILE}" ]]; then
    step "Restoring database"
    [[ -f "${DB_FILE}" ]] || fail ".surql file not found: ${DB_FILE}"

    # Wipe existing SurrealDB data so the import is clean
    rm -rf "${YADGAR_DIR}/surreal_db"

    # Ensure network exists
    docker network inspect "${NETWORK}" &>/dev/null || docker network create --driver bridge "${NETWORK}"

    # Start backend container
    docker rm -f "${BACKEND_CONTAINER}" &>/dev/null || true
    docker run -d \
        --name "${BACKEND_CONTAINER}" \
        --network "${NETWORK}" \
        --user root \
        -v "${YADGAR_DIR}:/data" \
        -p "127.0.0.1:8000:8000" \
        -p "127.0.0.1:8001:8001" \
        -e SURREAL_USER=root \
        -e SURREAL_PASS=root \
        "${BACKEND_IMAGE}"

    # Wait for SurrealDB HTTP port (8000)
    info "Waiting for SurrealDB..."
    deadline=$(( $(date +%s) + 120 ))
    while [[ $(date +%s) -lt $deadline ]]; do
        if curl -sf http://127.0.0.1:8000/health &>/dev/null; then
            break
        fi
        sleep 3
    done
    curl -sf http://127.0.0.1:8000/health &>/dev/null || fail "SurrealDB did not start"
    ok "SurrealDB ready"

    # Import the .surql file
    info "Importing $(basename "${DB_FILE}") ..."
    docker run --rm \
        --network "${NETWORK}" \
        -v "${DB_FILE}:/restore.surql:ro" \
        surrealdb/surrealdb:v3.0.5 \
        import \
        --endpoint http://"${BACKEND_CONTAINER}":8000 \
        --username root \
        --password root \
        --namespace yadgar \
        --database main \
        /restore.surql
    ok "Database restored"

    # Stop backend — systemd service will manage it from here
    docker stop "${BACKEND_CONTAINER}"
fi

# ── Step 6: install systemd service ──────────────────────────────────────────

step "Installing systemd service"
SERVICE_DIR="${HOME}/.config/systemd/user"
mkdir -p "${SERVICE_DIR}"

HF_MOUNT=""
[[ -d "${HF_CACHE}" ]] && HF_MOUNT="-v ${HF_CACHE}:/root/.cache/huggingface \\"$'\n    '

LOG_LEVEL_ENV=""
[[ -n "${LOG_LEVEL}" ]] && LOG_LEVEL_ENV="-e YADGAR_LOG_LEVEL=${LOG_LEVEL} \\"$'\n    '

# Write /etc/yadgar/secrets.env (chmod 600, root-owned).
# All credentials are stored here; systemd units load via EnvironmentFile=
# so passwords never appear in /proc/<pid>/cmdline.
step "Writing secrets env file"
SECRETS_DIR="$(dirname "${SECRETS_ENV_FILE}")"
if [[ ! -d "${SECRETS_DIR}" ]]; then
    if ! sudo mkdir -p "${SECRETS_DIR}" 2>/dev/null; then
        info "Cannot sudo mkdir ${SECRETS_DIR} — writing to ${HOME}/.yadgar/secrets.env instead"
        SECRETS_ENV_FILE="${HOME}/.yadgar/secrets.env"
        mkdir -p "$(dirname "${SECRETS_ENV_FILE}")"
    fi
fi
umask 177  # ensure file is created 600
cat > "${SECRETS_ENV_FILE}" <<SECRETS
# Yadgar secrets — chmod 600 root-owned — never commit or log
SURREAL_USER=${ROOT_USER}
SURREAL_PASS=${ROOT_PASS}
YADGAR_RW_USER=${RW_USER}
YADGAR_RW_PASS=${RW_PASS}
YADGAR_RO_USER=${RO_USER}
YADGAR_RO_PASS=${RO_PASS}
YADGAR_DB_USER=${RW_USER:-${ROOT_USER}}
YADGAR_DB_PASS=${RW_PASS:-${ROOT_PASS}}
SECRETS
umask 022
chmod 600 "${SECRETS_ENV_FILE}"
ok "Secrets written → ${SECRETS_ENV_FILE} (mode 600)"

cat > "${SERVICE_DIR}/yadgar-backend.service" <<EOF
[Unit]
Description=Yadgar Backend (SurrealDB + Embeddings)
After=network.target

[Service]
Environment=DOCKER_HOST=unix:///run/podman/podman.sock
EnvironmentFile=${SECRETS_ENV_FILE}
ExecStartPre=-${DOCKER} stop ${BACKEND_CONTAINER}
ExecStartPre=-${DOCKER} rm ${BACKEND_CONTAINER}
ExecStartPre=-${DOCKER} network create ${NETWORK}
ExecStart=${DOCKER} run --name ${BACKEND_CONTAINER} --rm --user root \\
    --network ${NETWORK} \\
    -p 127.0.0.1:8001:8001 \\
    -v ${YADGAR_DIR}:/data \\
    ${HF_MOUNT}-e SURREAL_USER=\${SURREAL_USER} \\
    -e SURREAL_PASS=\${SURREAL_PASS} \\
    -e YADGAR_RW_USER=\${YADGAR_RW_USER} \\
    -e YADGAR_RW_PASS=\${YADGAR_RW_PASS} \\
    -e YADGAR_RO_USER=\${YADGAR_RO_USER} \\
    -e YADGAR_RO_PASS=\${YADGAR_RO_PASS} \\
    --memory 4g --cpus 2 --stop-timeout 30 \\
    ${BACKEND_IMAGE}
ExecStop=${DOCKER} stop ${BACKEND_CONTAINER}
Restart=on-failure
RestartSec=5
Type=simple

[Install]
WantedBy=default.target
EOF

cat > "${SERVICE_DIR}/yadgar.service" <<EOF
[Unit]
Description=Yadgar Memory Engine / MCP Server (Docker)
After=network.target yadgar-backend.service
Requires=yadgar-backend.service

[Service]
Environment=DOCKER_HOST=unix:///run/podman/podman.sock
EnvironmentFile=${SECRETS_ENV_FILE}
ExecStartPre=-${DOCKER} stop ${CORE_CONTAINER}
ExecStartPre=-${DOCKER} rm ${CORE_CONTAINER}
ExecStart=${DOCKER} run --name ${CORE_CONTAINER} --rm --user root \\
    --network ${NETWORK} \\
    -p 127.0.0.1:8765:8765 \\
    -p 127.0.0.1:42069:42069 \\
    -v ${YADGAR_DIR}:/data \\
    -e YADGAR_DB_URL=http://${BACKEND_CONTAINER}:8000 \\
    -e YADGAR_EMBED_URL=http://${BACKEND_CONTAINER}:8001 \\
    -e YADGAR_HOST=127.0.0.1 \\
    -e YADGAR_PORT=8765 \\
    -e YADGAR_DATA_DIR=/data \\
    -e YADGAR_DB_USER=\${YADGAR_DB_USER} \\
    -e YADGAR_DB_PASS=\${YADGAR_DB_PASS} \\
    ${LOG_LEVEL_ENV}--memory 1g --cpus 1 --stop-timeout 30 \\
    ${CORE_IMAGE}
ExecStop=${DOCKER} stop ${CORE_CONTAINER}
Restart=on-failure
RestartSec=5
Type=simple

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now yadgar-backend.service
systemctl --user enable --now yadgar.service
ok "Services installed and started"

# ── Step 7: install Claude hooks ─────────────────────────────────────────────

step "Installing Claude hooks"
CLAUDE_DIR="${HOME}/.claude"
HOOKS_DIR="${CLAUDE_DIR}/hooks"
SETTINGS="${CLAUDE_DIR}/settings.json"
mkdir -p "${HOOKS_DIR}"

# Wait for the core container to exist (systemd start can lag a few seconds)
deadline=$(( $(date +%s) + 60 ))
while [[ $(date +%s) -lt $deadline ]]; do
    docker inspect "${CORE_CONTAINER}" &>/dev/null && break
    sleep 2
done
docker inspect "${CORE_CONTAINER}" &>/dev/null || fail "${CORE_CONTAINER} did not start — check: systemctl --user status yadgar.service"

# Copy hook scripts out of the running image, with yadgar- prefix
for f in pre-compact-drain.sh post-compact-rehydrate.sh \
         session-start-context.py post-tool-capture.py prompt-recall.py; do
    docker cp "${CORE_CONTAINER}:/app/.claude/hooks/${f}" "${HOOKS_DIR}/yadgar-${f}"
    chmod +x "${HOOKS_DIR}/yadgar-${f}"
done

# PreToolUse guard: block direct docker exec into yadgar DB/backend containers
cat > "${HOOKS_DIR}/yadgar-pre-tool-bash-guard.py" <<'PY'
#!/usr/bin/env python3
import json, sys
data = json.load(sys.stdin)
cmd = data.get("tool_input", {}).get("command", "")
if "docker exec yadgar-backend" in cmd or "docker exec yadgar-db" in cmd:
    print(json.dumps({"decision": "block", "reason": "Direct docker exec into yadgar DB/backend containers is blocked. Use yadgar MCP tools instead."}))
    sys.exit(0)
print(json.dumps({"decision": "allow"}))
PY
chmod +x "${HOOKS_DIR}/yadgar-pre-tool-bash-guard.py"
ok "Hook scripts → ${HOOKS_DIR}/yadgar-*"

# Backup settings, then merge yadgar hooks (preserves user's other settings/hooks)
[[ -f "${SETTINGS}" ]] && cp "${SETTINGS}" "${SETTINGS}.bak.$(date +%s)"

python3 - "${SETTINGS}" "${HOOKS_DIR}" <<'PY'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
hooks_dir = sys.argv[2]
marker = "yadgar"  # any command containing this is treated as yadgar-managed

settings = {}
if settings_path.exists() and settings_path.stat().st_size:
    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"could not parse {settings_path}: {e}")

yadgar = {
    "PreCompact": [{"matcher": "", "hooks": [{"type": "command",
        "command": f"bash {hooks_dir}/yadgar-pre-compact-drain.sh"}]}],
    "SessionStart": [
        {"matcher": "", "hooks": [{"type": "command",
            "command": f"python3 {hooks_dir}/yadgar-session-start-context.py"}]},
        {"matcher": "compact", "hooks": [{"type": "command",
            "command": f"bash {hooks_dir}/yadgar-post-compact-rehydrate.sh"}]},
    ],
    "PostToolUse": [{"matcher": "", "hooks": [{"type": "command",
        "command": f"python3 {hooks_dir}/yadgar-post-tool-capture.py"}]}],
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command",
        "command": f"python3 {hooks_dir}/yadgar-prompt-recall.py"}]}],
    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
        "command": f"python3 {hooks_dir}/yadgar-pre-tool-bash-guard.py"}]}],
}

settings.setdefault("hooks", {})
for event in set(settings["hooks"].keys()) | set(yadgar.keys()):
    existing = settings["hooks"].get(event, [])
    pruned = []
    for entry in existing:
        kept = [h for h in entry.get("hooks", []) if marker not in h.get("command", "")]
        if kept:
            pruned.append({**entry, "hooks": kept})
    combined = pruned + yadgar.get(event, [])
    if combined:
        settings["hooks"][event] = combined
    else:
        settings["hooks"].pop(event, None)

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2))
PY
ok "Hooks registered → ${SETTINGS}"

# ── Step 8: register MCP server in ~/.claude.json ────────────────────────────

step "Registering MCP server"
CLAUDE_JSON="${HOME}/.claude.json"
[[ -f "${CLAUDE_JSON}" ]] && cp "${CLAUDE_JSON}" "${CLAUDE_JSON}.bak.$(date +%s)"

python3 - "${CLAUDE_JSON}" <<'PY'
import json, sys
from pathlib import Path

p = Path(sys.argv[1])
cfg = {}
if p.exists() and p.stat().st_size:
    try:
        cfg = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"could not parse {p}: {e}")

cfg.setdefault("mcpServers", {})["yadgar"] = {
    "type": "streamable-http",
    "url": "http://localhost:8765/mcp",
}

p.write_text(json.dumps(cfg, indent=2))
PY
ok "MCP server registered → ${CLAUDE_JSON}"

# ── Done ──────────────────────────────────────────────────────────────────────

echo
echo "Setup complete."
echo
echo "Check service status: systemctl --user status yadgar.service"
