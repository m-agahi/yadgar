# Upgrade Test Runbook

Manual end-to-end recipe for testing the yadgar upgrade orchestrator (`yadgar update --install`) against a real environment. Not automated — container pulls, systemd interaction, and podman daemon behavior are environment-dependent. Real upgrades hit real registries; running this in CI is not safe.

---

## 1. Why manual?

- Container image pull requires a live registry (docker.io/openfantasy/yadgar). No mock.
- `Type=notify` / `sd_notify` integration requires a real systemd user session.
- Graceful-stop drain (in-flight HTTP + file_queue flush) requires a live daemon with real connections.
- Rollback restores a previous image tag — meaning a prior tagged release must exist on the registry.

These preconditions cannot be satisfied hermetically in CI without a full podman-in-podman + systemd-in-systemd stack. The automated test suite (`test_upgrade_orchestrator.py`) mocks all external calls; this runbook is the complementary end-to-end check.

---

## 2. Prerequisites

Before starting:

- **podman ≥ 4.0** — `podman --version`
- **systemd ≥ 234** — `systemctl --version` (user session must be active: `loginctl show-session`)
- **pipx ≥ 1.0** — `pipx --version`
- **`~/.config/yadgar/secrets.env` exists** and is sourced — `yadgar daemon status` returns healthy
- **Daemon currently running** — `yadgar daemon status` shows version X
- **`update.install_enabled: true`** in `~/.config/yadgar/config.yaml`
- **Upgrade available** — `yadgar update --check` reports a newer version on PyPI (current ≠ latest)

If `yadgar update --check` reports "already at latest," use a pinned older version for the test: `pipx install yadgar==<prev>` and pull the older image manually before proceeding.

---

## 3. Test sequence

### Step 1 — Baseline snapshot

```bash
yadgar daemon status
yadgar --version
cat ~/.config/yadgar/upgrade.env   # shows current YADGAR_IMAGE_TAG
ls ~/.config/yadgar/upgrade-snapshots/ 2>/dev/null && echo "(existing snapshots above)"
```

Record the current version string. You will verify it changed after upgrade.

### Step 2 — Trigger the orchestrator

```bash
yadgar update --install
```

Expected output progression:

```
[PROBING_PYPI] Checking latest version on PyPI...
[CREATING_SNAPSHOT] Writing upgrade snapshot to ~/.config/yadgar/upgrade-snapshots/<ts>/
[PULLING_IMAGE] Pulling docker.io/openfantasy/yadgar:<new-version>...
[REWRITING_ENV] Writing YADGAR_IMAGE_TAG=<new-version> to upgrade.env...
[STOPPING_DAEMON] Running yadgar daemon graceful-stop --timeout=30...
[RESTARTING_DAEMON] systemctl --user restart yadgar.service...
[HEALTH_CHECKING] Waiting for daemon to report new version...
[UPGRADING_CLI] pipx upgrade yadgar...
[RE_EXECING] Re-executing as new CLI for --finalize...
[FINALIZING] Verifying daemon serves new version...
Upgrade complete: 5.48.0 → 5.49.0
```

### Step 3 — Observe

```bash
# Snapshot directory should exist
ls -la ~/.config/yadgar/upgrade-snapshots/
# Inspect the latest snapshot
SNAP=$(ls ~/.config/yadgar/upgrade-snapshots/ | sort | tail -1)
ls ~/.config/yadgar/upgrade-snapshots/$SNAP/
cat ~/.config/yadgar/upgrade-snapshots/$SNAP/prev_image_tag
cat ~/.config/yadgar/upgrade-snapshots/$SNAP/forward_log.json
```

### Step 4 — Verify post-upgrade

```bash
yadgar --version          # must show new version
yadgar daemon status      # must report new version + recent uptime (reset by restart)
cat ~/.config/yadgar/upgrade.env   # YADGAR_IMAGE_TAG must match new version
```

### Step 5 — Rollback drill

```bash
yadgar update --rollback
```

Expected: orchestrator reads `prev_image_tag` from latest snapshot, rewrites `upgrade.env`, restarts daemon.

```bash
yadgar --version          # reverted
yadgar daemon status      # reverted version + recent uptime
```

---

## 4. Common failure modes and recovery

### Pull fails (network)

Orchestrator state: `PULLING_IMAGE` → `ROLLING_BACK` → `ROLLED_BACK_OK` (exit 1).

Nothing was mutated. Retry after fixing network / registry auth.

```bash
# Verify nothing changed
yadgar --version
yadgar daemon status
```

### Health-check timeout post-restart

Orchestrator state: `RESTARTING_DAEMON` → `HEALTH_CHECKING` timeout → `ROLLING_BACK` → `ROLLED_BACK_OK` (exit 1).

Old image tag restored in `upgrade.env`. Daemon restarted on old image.

Investigate: `journalctl --user -u yadgar.service -n 100`

### `--finalize` mismatch (exit 4)

Orchestrator terminal state: `DONE_BUT_FINALIZE_FAILED` (exit 4). Daemon is healthy on new image; new CLI is installed; only the verification handshake failed (new CLI process reported wrong version or timeout).

Recovery:
```bash
yadgar update --rollback
```
Then investigate daemon logs: `journalctl --user -u yadgar.service -n 50`

### CLI rollback failed (exit 2, `DONE_CLI_ROLLBACK_FAILED`)

Image pull + restart + health-check succeeded. pipx upgrade succeeded. But new CLI re-exec / --finalize failed AND the subsequent `pipx install --force yadgar==<prev>` also failed (e.g., prior version not on PyPI — see PD-45 note below).

Daemon is on the new image (healthy). CLI version mismatch.

Recovery: pin CLI manually.
```bash
pipx install --force yadgar==<prev-version>
```

> **PD-45 note:** internal dev releases may not be published to PyPI. Check `https://pypi.org/project/yadgar/#history` for available versions. If the prior version is absent, use the cached wheel in the snapshot directory if present (v5.50+ will cache the wheel; v5.49 does not).

### Stuck lock from killed orchestrator

If `yadgar update --install` was killed mid-run, `~/.config/yadgar/upgrade.lock` may remain.

Next `--install` invocation auto-detects stale lock: reads PID from lock file, runs `kill -0 <pid>` — if process is dead (or lock age > `update.lock_max_age_seconds`, default 3600s), lock is overwritten and orchestrator proceeds.

If you need to force-clear immediately:
```bash
rm ~/.config/yadgar/upgrade.lock
```

---

## 5. What this does NOT cover

- **macOS launchd parity** — `ExitTimeOut` behavior differs from systemd `TimeoutStopSec`. Manual macOS test recommended after any change to the launchd plist template. Graceful-stop drain timing may differ.
- **Container-host network partitions** — pull-phase network failures are tested but registry auth failures (expired token, private registry) require environment-specific setup.
- **Multi-version coexistence** — not supported. A single `YADGAR_IMAGE_TAG` env var drives the running daemon. Parallel installs require separate `YADGAR_PORT` + separate `~/.config/yadgar/` directories.
