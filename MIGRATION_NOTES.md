# Migration Notes

## v5.3.9 — Crash hotfix (2026-05-20)

These commands run **manually** during v5.3.9 deploy. Per HARD RULE — No Apply / Import, the repo cannot apply them.

### 1. systemd cascade decouple (CRITICAL — fixes 2026-05-20 OOM cascade)

**Root cause:** `~/.config/systemd/user/yadgar.service` has `BindsTo=yadgar-backend.service` + `Requires=yadgar-backend.service`. When backend OOMKilled at 19:57:53, `BindsTo` forced core to stop. Core didn't auto-restart because the SIGKILL exit (143) bypassed `Restart=on-failure` semantics.

**Fix lives in `~/git/nix/modules/home/yadgar.nix`** (the systemd unit for the user yadgar service):

```nix
# Before
systemd.user.services.yadgar = {
  Unit = {
    After = "yadgar-backend.service";
    BindsTo = "yadgar-backend.service";     # ← remove
    Requires = "yadgar-backend.service";    # ← remove (or weaken to Wants)
  };
};

# After
systemd.user.services.yadgar = {
  Unit = {
    After = "yadgar-backend.service";
    Wants = "yadgar-backend.service";       # loose dependency, ordering only
  };
};
```

Result: backend death no longer forces core stop. Core rides out backend restarts (degraded reads via v5.4 N4 circuit breaker).

**Apply** (user runs manually):

```bash
cd ~/git/nix
git checkout -b fix/yadgar-systemd-decouple master
# edit modules/home/yadgar.nix per the diff above
git add modules/home/yadgar.nix
git commit -m "fix(yadgar): decouple core from backend lifetime — remove BindsTo+Requires (v5.3.9 N0)"
home-manager switch
systemctl --user daemon-reload

# Verify
systemctl --user cat yadgar.service | grep -E "BindsTo|Requires|Wants"
# Expected: Wants=yadgar-backend.service. No BindsTo, no Requires.
```

**Verification test:**

```bash
systemctl --user stop yadgar-backend
sleep 5
systemctl --user is-active yadgar  # should print "active"
systemctl --user start yadgar-backend
```

If `yadgar` shows `inactive`/`failed` after stopping backend → fix didn't apply correctly.

### 2. DLQ cleanup — 16 stale wiki_add entries

16 `wiki_add` entries stuck in DLQ since 2026-05-18 with `schema_version_too_old: got None, require >= 2`. Pre-v5.0 payloads from before schema migration #004 enforced `wiki_schema_version=2`.

**Decision:** explicit drop (entries 3 days old; content likely re-captured by subsequent writes).

```bash
# List
yadgar dlq-list

# Drop matching entries
yadgar dlq-drop --filter 'op_type=wiki_add,last_error~schema_version_too_old'
```

If `yadgar dlq-drop` doesn't exist yet, fall back: list filenames via MCP `dlq_inspect`, remove from `~/.yadgar/dlq/` manually.

### 3. Image versions

- Core: build `docker.io/openfantasy/yadgar:5.3.9` locally (amd64). Do NOT push per WORKFLOW RULE 2026-05-19.
- Backend: `docker.io/openfantasy/yadgar-backend:5.0.2` (unchanged).
- Nix bump: `modules/home/yadgar.nix` → `yadger_core_version = "5.3.9"` (alongside the BindsTo decouple).

### 4. Acid test

```bash
# Smoke
curl -sS -H "Authorization: Bearer $YADGAR_TOKEN" http://127.0.0.1:8765/health
# Expected: 200

# Cascade
systemctl --user stop yadgar-backend
sleep 5
systemctl --user is-active yadgar          # active
# Recall fails fast (≤5s timeout per N1, not 30s)
time curl -sS -H "Authorization: Bearer $YADGAR_TOKEN" \
  -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"recall","arguments":{"query":"test"}}}' \
  | head -c 200
# Expected: ≤10s, error indicating backend unavailable

systemctl --user start yadgar-backend
sleep 10
# Recall succeeds again
```

### 5. Rollback

```bash
cd ~/git/nix
git revert <commit-from-§1>
home-manager switch
systemctl --user restart yadgar yadgar-backend
```

Pin yadgar core image tag to v5.3.7 in nix until rollback resolved.

---

## v4.8 backup retention

Replace the old "keep 7 newest by mtime" policy with three-cap retention
(age + count + size). The nix module edit is staged in `/home/max/git/nix`
but not applied — the user runs `nix-update`.

### Steps

**1. One-shot manual cleanup of the 28 GB stuck pre-rebuild snapshots:**

~~~bash
find ~/.backups/yadgar/db -maxdepth 1 -name 'surreal_db_*' -type d -mtime +1 -size +1G -exec rm -rf {} +
~~~

This removes any snapshot older than 1 day that is larger than 1 GB —
targeting the five 5.5 GB pre-rebuild bloated snapshots from 2026-05-12
while leaving the two post-rebuild snapshots (137 MB, 479 MB) untouched.

**2. Apply the nix module edit to install the new retention logic in
`yadgar-backend.service:ExecStartPre`:**

~~~bash
home-manager switch
~~~

The edit is at `/home/max/git/nix/modules/home/yadgar.nix`. It:
- Adds `options.programs.yadgar.backup.{maxAgeDays,maxCount,maxGiB}` with defaults 7/7/10.
- Replaces the `tail -n +8 | xargs rm -rf` retention with the three-cap
  logic (age → count → size). Values are baked into the unit at `home-manager switch` time.

To override defaults, add to your home configuration before switching:

~~~nix
programs.yadgar.backup = {
  maxAgeDays = 14;   # keep up to 14 days
  maxCount   = 5;    # keep at most 5 snapshots
  maxGiB     = 20;   # allow up to 20 GiB
};
~~~

**3. Verify on next service restart:**

~~~bash
journalctl --user -u yadgar-backend.service -b | grep -i 'snapshot\|cleanup'
~~~

### Helper script

`scripts/cleanup-backups.sh` in this repo is the single source of truth for
the retention logic. It accepts env vars `YADGAR_BACKUP_DIR`,
`YADGAR_BACKUP_MAX_AGE_DAYS`, `YADGAR_BACKUP_MAX_COUNT`, `YADGAR_BACKUP_MAX_GIB`
and a `--dry-run` flag. Callable with a different `YADGAR_BACKUP_DIR` so the
new `cmd_vacuum` reuses it for pre-vacuum snapshot retention.

---

## v4.8 weekly SurrealKV vacuum

A new `yadgar-vacuum.service` + `.timer` runs the rewritten `yadgar vacuum`
on the proven export → swap → reimport flow every Sunday at 04:00 local
time (with a 30-minute random delay).

### Steps

**1. Apply the nix module edit:**

~~~bash
home-manager switch
~~~

This installs `yadgar-vacuum.service` (oneshot) + `yadgar-vacuum.timer`
(weekly). The vacuum binary stops/starts both `yadgar` and `yadgar-backend`
itself, so the timer unit does not declare `Conflicts=` or `PartOf=` on
the daemons.

**2. (Optional) Trigger a one-off vacuum on the current bloated DB:**

~~~bash
systemctl --user start yadgar-vacuum.service
journalctl --user -u yadgar-vacuum.service -f
~~~

Expected log lines: `Phase 1: export...`, `Phase 2: snapshot + drop...`,
`Phase 3: restart + import...`, `Phase 4: finalize...`, then
`Vacuum complete. before=N MB after=M MB saved=N-M (X%)`.

**3. Verify timer schedule:**

~~~bash
systemctl --user list-timers yadgar-vacuum.timer
~~~

**4. Rollback (if reimport fails):**

The bloated dir is retained at `~/.yadgar/surreal_db.bloated-<ts>` and
`yadgar` is NOT restarted. To recover the old state:

~~~bash
systemctl --user stop yadgar yadgar-backend
mv ~/.yadgar/surreal_db.pre-vacuum-<ts> ~/.yadgar/surreal_db
systemctl --user start yadgar-backend yadgar
~~~

**5. Disable the timer entirely:**

~~~bash
systemctl --user disable --now yadgar-vacuum.timer
~~~

---

## v4.8 log-level config (from v4.7.0, repeated for reference)

After deploying v4.8, the `[ml]` extra is now bundled in the core image
so the NLI reranker (`sentence_transformers`) works out of the box. No
action required — the WARN `Reranker disabled: install yadgar[ml] to
enable` should no longer appear in logs.

---

## v4.8 consolidation cooldown

`CONSOLIDATION_COOLDOWN_SECONDS` defaults to `1800` (30 min). Idle-triggered
consolidation cycles fire at most once per cooldown window. The daily 18:30
UTC cycle and `force_consolidate()` ignore the cooldown.

To override, edit `~/.yadgar/config.yaml`:

~~~yaml
CONSOLIDATION_COOLDOWN_SECONDS: 3600   # 1 hour instead of 30 min
# CONSOLIDATION_COOLDOWN_SECONDS: 0    # restore legacy back-to-back behaviour
~~~

Then restart: `systemctl --user restart yadgar`.

---

## v5.0 Stage 1 — Credentials Hardening + MCP Auth

**CRITICAL: Complete ALL steps before deploying the new image.**
REQUIRE_AUTH defaults to `True` in v5.0. Operators MUST provision
YADGAR_MCP_AUTH_TOKEN in /etc/yadgar/secrets.env BEFORE upgrading.
To stage the upgrade (token provisioned later), set `YADGAR_REQUIRE_AUTH=0`
in env for the initial deploy, then unset after token rollout.

### 1. Generate YADGAR_MCP_AUTH_TOKEN

~~~bash
# Option A: Python (no deps)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Option B: 1Password CLI
op item create --vault Private --title "yadgar/MCP_AUTH_TOKEN" \
  --category "API Credential" --field-type concealed --generate-password=true
# Read back:
op read op://Private/yadgar/MCP_AUTH_TOKEN
~~~

### 2. Create /etc/yadgar/secrets.env (chmod 600, root-owned)

~~~bash
sudo mkdir -p /etc/yadgar
sudo tee /etc/yadgar/secrets.env > /dev/null <<EOF
SURREAL_USER=<root_username>
SURREAL_PASS=<root_password>
YADGAR_RW_USER=yadgar-rw
YADGAR_RW_PASS=<rw_password>
YADGAR_RO_USER=yadgar-ro
YADGAR_RO_PASS=<ro_password>
YADGAR_DB_USER=yadgar-rw
YADGAR_DB_PASS=<rw_password>
YADGAR_MCP_AUTH_TOKEN=<token_from_step_1>
EOF
sudo chmod 600 /etc/yadgar/secrets.env
sudo chown root:root /etc/yadgar/secrets.env
~~~

### 3. Update systemd units to use EnvironmentFile

Units generated by `scripts/setup.sh` now use `EnvironmentFile=/etc/yadgar/secrets.env`.
If you have existing units that inline `-e SURREAL_PASS=...`:

~~~bash
systemctl --user edit yadgar-backend.service --force
# Under [Service], add:
# EnvironmentFile=/etc/yadgar/secrets.env
# Remove: -e SURREAL_USER=... -e SURREAL_PASS=... from ExecStart
systemctl --user daemon-reload
~~~

### 4. Re-run install_hooks to inject bearer token

~~~bash
export YADGAR_MCP_AUTH_TOKEN="$(grep YADGAR_MCP_AUTH_TOKEN /etc/yadgar/secrets.env | cut -d= -f2-)"
~~~

Then from a Claude session: call `install_hooks()`.

This writes `hook_runner.py` (replaces old python3 -c strings) and injects
`YADGAR_MCP_AUTH_TOKEN` env block into every hook entry in settings.json.

### 5. Restart services

After steps 1–4 (token is in secrets.env, hooks updated):

~~~bash
systemctl --user restart yadgar-backend.service yadgar.service
~~~

Auth is enabled by default (REQUIRE_AUTH=True). No extra flag needed once
the token is provisioned. If you staged with YADGAR_REQUIRE_AUTH=0, remove
that override before restarting.

### 6. Verify

~~~bash
# Health — always unauthenticated
curl -s http://127.0.0.1:8765/health

# API with auth enabled — should get 401 without token
curl -s http://127.0.0.1:8765/api/graph

# API with token — should work
curl -s -H "Authorization: Bearer $YADGAR_MCP_AUTH_TOKEN" http://127.0.0.1:8765/api/graph
~~~

### Rollback

If something breaks after the upgrade:

~~~bash
# Temporarily disable auth while debugging:
echo "YADGAR_REQUIRE_AUTH=0" | sudo tee -a /etc/yadgar/secrets.env
# Then restart:
systemctl --user restart yadgar.service
# Once resolved, remove the override line and restart again.
~~~

---

## v5.0.1 — durable MCP token wiring in nix module

**Context:** v5.0.1 deploy crashlooped 74× — H-7 startup validator demands
`YADGAR_MCP_AUTH_TOKEN` when `REQUIRE_AUTH=1` (default). Nix module did not
wire token at all (op-inject template missing it, ExecStart missing `-e`).

**Also:** Claude Code v2.1.x silently ignores the `headers` field on HTTP MCP
servers loaded via `programs.mcp` (the home-manager plugin path that produces
`~/.config/mcp/mcp.json` entries labelled `plugin:claude-code-home-manager:*`).
It tries OAuth discovery instead, which 404s. The fix is to register Yadgar
directly in `~/.claude.json` under `mcpServers` (where `headers` *is*
honoured) and drop `programs.mcp.servers.yadgar` from the nix module.

**Current state:** ephemeral fix in place — manual `YADGAR_MCP_AUTH_TOKEN=`
line in `~/.config/yadgar/secrets.env` + systemd drop-in at
`~/.config/systemd/user/yadgar.service.d/mcp-token.conf` that re-defines
ExecStart with `-e YADGAR_MCP_AUTH_TOKEN` appended. Server is up on v5.0.1.

**Risk if not fixed:** next `nix-update` regenerates secrets.env via op-inject
template (which lacks token line) → wipes the manual line. Container restart
crashes again.

**1Password item** (already created by user, 2026-05-16):
`Private/yadgar-mcp`, field `password`, length 32.

### Patch `~/git/nix/modules/home/yadgar.nix`

**1. Op-inject template — add token line (file ~line 158, inside `writeText`):**

~~~nix
${pkgs.writeText "yadgar-secrets.env.tpl" ''
  SURREAL_USER=op://Private/yadgar-root/username
  SURREAL_PASS=op://Private/yadgar-root/password
  YADGAR_RW_USER=op://Private/yadgar-rw/username
  YADGAR_RW_PASS=op://Private/yadgar-rw/password
  YADGAR_RO_USER=op://Private/yadgar-ro/username
  YADGAR_RO_PASS=op://Private/yadgar-ro/password
  YADGAR_DB_USER=op://Private/yadgar-rw/username
  YADGAR_DB_PASS=op://Private/yadgar-rw/password
  YADGAR_MCP_AUTH_TOKEN=op://Private/yadgar-mcp/password
''}
~~~

**2. systemd.user.services.yadgar ExecStart — add token passthrough:**

After `-e YADGAR_DB_PASS`, insert `-e YADGAR_MCP_AUTH_TOKEN` (no value — read
from EnvironmentFile).

### Apply

~~~bash
cd ~/git/nix
# edit modules/home/yadgar.nix per above
nix-update
~~~

### Cleanup ephemeral fix (only after `nix-update` succeeds and health is OK)

~~~bash
rm ~/.config/systemd/user/yadgar.service.d/mcp-token.conf
rmdir ~/.config/systemd/user/yadgar.service.d 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user restart yadgar
curl -sf http://127.0.0.1:8765/health   # expect status=ok, version=5.0.1
~~~

### Verify token survives op-inject

~~~bash
grep YADGAR_MCP_AUTH_TOKEN ~/.config/yadgar/secrets.env | cut -d= -f1
# Expect: YADGAR_MCP_AUTH_TOKEN
~~~

### Rollback

If `nix-update` fails or secrets.env loses the token, restore manually:

~~~bash
TOK=$(op item get yadgar-mcp --vault Private --fields password --format json | jq -r '.value')
grep -v '^YADGAR_MCP_AUTH_TOKEN=' ~/.config/yadgar/secrets.env > /tmp/se.new
printf 'YADGAR_MCP_AUTH_TOKEN=%s\n' "$TOK" >> /tmp/se.new
mv /tmp/se.new ~/.config/yadgar/secrets.env
chmod 600 ~/.config/yadgar/secrets.env
systemctl --user restart yadgar
~~~

## v5.1 — yadgar-vacuum.service ExecStart fix (A2)

The systemd unit has been failing exit 127 on every scheduled trigger
since v4.8.3. Root cause: ExecStart passed `vacuum --service-mode=systemd`
as the container CMD; the image entrypoint is not the bare `yadgar`
binary, so crun tried to exec a binary literally named `vacuum` and
failed `executable file not found in $PATH`.

Companion fix to v5.1 A1 (`storage.get_db_size()` bearer-token client,
commit `bc22f0b`). The vacuum subprocess calls `get_db_size()` internally
during phase logging, so it now also requires `YADGAR_MCP_AUTH_TOKEN`
in its container env. The SurrealDB export+reimport phases require
`YADGAR_DB_USER/PASS` (already in `secrets.env`).

Nix module edit is committed at `~/git/nix` (master, commit `7068449`)
but not applied — the user runs `nix-update`.

### Apply

~~~bash
cd ~/git/nix
nix-update
~~~

### Validate

~~~bash
# Trigger manually — should now exit 0 + log before/after byte counts
systemctl --user start yadgar-vacuum.service
journalctl --user -u yadgar-vacuum.service -n 80 --no-pager

# Confirm next scheduled trigger
systemctl --user list-timers yadgar-vacuum.timer --no-pager
~~~

### Expected log markers on success

- `vacuum.py` phase markers: `export → strip → snapshot → swap → import`
- `before_bytes=NNN, after_bytes=MMM` with a measurable reduction
- exit 0, container `--rm` cleaned up

### Rollback

If the vacuum service fails for a new reason post-apply:

~~~bash
# Revert the nix commit
cd ~/git/nix && git revert 7068449 && nix-update

# Or: stop the timer until the underlying issue is resolved
systemctl --user stop yadgar-vacuum.timer
systemctl --user disable yadgar-vacuum.timer
~~~

A1 client-side fix is on branch `v5.1/a1-dbsize-instrumentation` in the
yadgar repo (commit `bc22f0b`). It does NOT need migration — it will
land via the next yadgar image release (`5.1.0`). Until then, the
vacuum service runs against the `5.0.1` image, where `get_db_size()`
inside vacuum still returns 0 in log output (cosmetic — the export+
import phases use `YADGAR_DB_USER/PASS` not the bearer token, so the
core compaction logic works).

---

## v5.4.2 — CB-1 probe fixes + F5-A saturation fix (2026-05-22)

### 1. Image builds (user runs manually)

```bash
# Build core (5.4.2) — CB-1 changes are in core image
cd ~/git/yadgar
podman build -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.2 .

# Build backend (5.0.3) — F5-A semaphore is in backend image
podman build -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.0.3 .
```

### 2. Nix bump (in ~/git/nix)

Update `modules/home/yadgar.nix`:

```nix
# Core image version
yadgar_core_version = "5.4.2";  # was 5.4.1

# Backend image version
yadgar_backend_version = "5.0.3";  # was 5.0.2
```

Then apply:

```bash
cd ~/git/nix
git checkout -b chore/v5.4.2-image-bump master
# edit modules/home/yadgar.nix per above
git add modules/home/yadgar.nix
git commit -m "chore(yadgar): bump core 5.4.1→5.4.2 + backend 5.0.2→5.0.3"
home-manager switch
systemctl --user restart yadgar yadgar-backend
```

### 3. Optional: F5-C cgroup bump (if saturation persists after F5-A)

If CPU fan spin-up resumes after the F5-A semaphore deploy, consider bumping backend container resources in `modules/home/yadgar.nix`:

```nix
# Before
--cpus 2 --memory 4g

# After (F5-C)
--cpus 4 --memory 6g
```

F5-A (semaphore N=1) should be sufficient because it ensures probes fast-fail instead of piling on. F5-C is the escape hatch if the model's normal inference load (non-probe) still saturates 2 CPUs.

### 4. New env vars (optional overrides)

All have sensible defaults — no config change required for standard deploy.

| Env var | Default | Notes |
|---|---|---|
| `YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC` | 2.0 | Core — probe HTTP read timeout |
| `YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC` | 600 | Core — backoff ceiling |
| `YADGAR_CIRCUIT_BREAKER_BACKOFF_FACTOR` | 2.0 | Core — cooldown multiplier per failed probe |
| `YADGAR_RERANK_MAX_CONCURRENCY` | 1 | Backend — semaphore slots per mode |
| `YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` | 2.0 | Backend — acquire wait before 503 |

### 5. Verification

```bash
# Core health
curl -sS -H "Authorization: Bearer $YADGAR_TOKEN" http://127.0.0.1:8765/health

# Backend semaphore: confirm /rerank returns 503 when concurrency exceeded
# (requires manually saturating the slot — normal usage is fine)
# Expected normal behavior: /rerank returns 200 in <5s

# CB-1 backoff: check logs for "circuit breaker ... → OPEN ... backoff"
journalctl --user -u yadgar -n 50 | grep -i "circuit breaker"
```

### 6. I14 — Structured logging (new in v5.4.2)

**Breaking: default log format changed `human` → `json`.**

Log output is now I14-conformant JSON lines by default. If any Loki/Grafana dashboards or `journalctl | grep` scripts depend on the old plain-text format or old field names (`timestamp`, `logger`, `message`), update them.

| Change | Detail |
|---|---|
| Old field `timestamp` | Now `ts` (ISO 8601 with `+00:00` timezone) |
| Old field `message` | Now `event` |
| Old field `logger` | Removed (use `component` extra field instead) |
| New required caller fields | `component`, `action`, `outcome` in `extra={}` |

**New env var:**

| Env var | Default | Notes |
|---|---|---|
| `YADGAR_LOG_FORMAT` | `json` | `json` = I14 structured (Loki/Grafana ingest); `text` = human-readable for local dev |

**Local dev:** set `YADGAR_LOG_FORMAT=text` in your `.env` or shell to restore human-readable logs.

**Observability:** Loki / Grafana can now ingest yadgar logs as structured records. Parse on `ts`, `level`, `component`, `action`, `outcome` labels.

**Redaction:** `ContentRedactor` strips any log field whose name contains (case-insensitive): `content`, `password`, `token`, `secret`, `auth`, `authorization`, `api_key`, `bearer`. Known sharp edge: substring match also redacts `content_type`/`content_length` if passed as extra fields. Full-conformance round (v5.6) will tighten the denylist.

### 7. Release-readiness check — image size ratchet (P9, new in v5.4.2)

**Optional but recommended** — run after each `podman build` to verify the backend image stays within the 2.0 GB cap.

```bash
# After building the backend image:
pre-commit run check-image-size-backend --hook-stage manual

# After building the core image:
pre-commit run check-image-size-core --hook-stage manual
```

**Manual invocation (no pre-commit):**

```bash
python scripts/check_image_size.py \
  --image docker.io/openfantasy/yadgar-backend:5.0.3

# Override cap if needed:
python scripts/check_image_size.py \
  --image docker.io/openfantasy/yadgar-backend:5.0.3 \
  --max-size-gb 2.0 \
  --warn-layer-mb 500
```

Exit 0 = within cap (warnings for individual layers >500 MB). Exit 1 = over budget.

**Caps:** backend ≤2.0 GB, core ≤0.8 GB (auto-detected from image name; override with `--max-size-gb`).

**Context:** F0 achieved 6.78 GB → 1.63 GB incidentally during v5.4.2 backend rebuild. This hook prevents regression on future dep bumps. See `docs/ARCHITECTURE_INVARIANTS.md` P9 section.
