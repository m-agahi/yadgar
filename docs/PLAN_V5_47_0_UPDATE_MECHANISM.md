# PLAN — v5.47.0: Update Mechanism (`yadgar update` CLI + Auto-Check + Control-Tab API)

**Status:** drafted 2026-05-31. REVISED 2026-06-02 post-opus-review (MAJOR cut). Plan-first per I27.

**Revision notes (opus reviewer):**
- DROP `action=install` from v5.47.0 — `pipx upgrade yadgar` while daemon RUNNING kills daemon process. DEFER to **v5.48** once daemon-graceful-restart primitive exists.
- v5.47.0 ships CHECK-ONLY: `POST /api/control/update action=check`, CLI prints upgrade command, user runs manually.
- DROP unused `check_interval_hours` knob (Open Q5 placeholder) — don't ship dead config.
- Auto-check thread MUST be `daemon=True` + startup-completion barrier (no "log WARNING + continue" hiding real bugs).
- Phase-commit reorg: dispatch 1 (CLI + detection + probe + API gate) + dispatch 2 (auto-check + docs + bump).
- Drop `can_self_install` heuristic (only needed for `action=install`).
- Scope cut: 34 tests → ~22.
- HARD SEQUENCING: v5.47 MUST ship before v5.50.0 Step 6 (Control tab UI depends on Control API).

**Audit lineage:** prior Explore agent (post-v5.25.0 setup audit) flagged "Update mechanism: BLOCKED — No `yadgar update`." Users on PyPI / Homebrew / Nix / container install paths each need a different upgrade incantation; no unified UX.

**Ships in train:** v5.45.0 → v5.46.0 → v5.47.0 (foundation → distribution → updates). User-locked order; ships BEFORE v5.50/v5.51/v5.52 viz.

**Depends on:** v5.45.0 (install-method detection foundation) + v5.46.0 (brew + nix install paths must exist before update can detect them).

**Effort estimate:** 2–3 calendar days.

**Cross-cuts:** v5.50 viz (Control tab UI button) — depends on `/api/control/update` HTTP route landing in v5.47. v5.50 wires the button; v5.47 ships the API.

See also `docs/DECISIONS.md` — 2026-05-31 PD-37 (setup mechanism; update mechanism implements the upgrade path).

---

## Goal — single `yadgar update` UX across all install methods

Ship four coordinated update surfaces:

1. **`yadgar update` CLI subcommand** — detects install method (pipx / brew / nix-flake / container) + runs the correct upgrade command. `--check` (read-only version probe) vs `--install` (perform upgrade).
2. **Auto-check on daemon start** — opt-in, anonymous version-only check against Codeberg releases API. Default OFF.
3. **`/api/control/update` HTTP endpoint** — POST, gated by `YADGAR_DEBUG_APIS_ENABLED=on` + bearer token. Returns: current version, available version, install method, recommended upgrade command. Optional: trigger upgrade in-process (for Control-tab integration).
4. **Control-tab Update button (v5.50 wiring; API shipped in v5.47)** — Control tab in viz UI shows current version + "Update available" indicator + button that calls `/api/control/update`. Terminal CLI annotation: if user invokes from Control tab, show the equivalent CLI command for power users.

Privacy posture: anonymous version-only check. NO IP collection, NO user-ID, NO usage telemetry. Opt-in via `update.check_on_start: false` (default OFF).

---

## Non-goals (explicit)

- **No PyPI/Homebrew/Nix install path implementation.** That's v5.46.0.
- **No setup foundation rework.** v5.45.0.
- **No update server / metadata service.** Uses Codeberg releases REST API directly (`https://codeberg.org/api/v1/repos/maxagahi/yadgar/releases/latest`).
- **No background daemon update scheduler.** Auto-check runs ONCE on daemon start, no cron. User-initiated updates only.
- **No automatic application of updates.** v5.47 shows availability + provides the command; user runs it. (Container images can auto-pull on systemd restart, but that's user-configured, not v5.47-managed.)
- **No SBOM verification on update.** SBOMs are advisory; cryptographic verification is v5.48+ candidate.
- **No rollback mechanism.** v5.48+ candidate.
- **No multi-version coexistence (e.g. v5.46 + v5.47 simultaneously).** Update is in-place.
- **No phone-home telemetry.** Strictly version probe, no other data exchanged.

---

## Current state (verified from code, 2026-05-31)

| Component | Status | Gap for v5.47 |
|---|---|---|
| `yadgar update` subcommand | DOES NOT EXIST | New: `yadgar/cli/update.py` |
| Install-method detection | DOES NOT EXIST | Reuses v5.45 `scripts/install/detect_*.sh` + adds `detect_install_method.sh` (pipx/brew/nix/container/source) |
| Codeberg releases API | Public, no auth needed for read | n/a — direct httpx call |
| HTTP API gating | `YADGAR_DEBUG_APIS_ENABLED=on` + bearer token middleware exists (v5.22+) | Wire `/api/control/update` into same middleware |
| Control tab UI | DOES NOT EXIST yet — viz UI has Memory/Graph/Anchors tabs; Control tab planned for v5.50 | v5.47 ships the BACKEND API; v5.50 wires the FRONTEND button |
| config.yaml `update.*` section | None | New: `update.check_on_start: false`, `update.check_interval_hours: 24` (placeholder for future), `update.user_agent: yadgar/<version>` |
| Anonymous probe semantics | n/a | New: HTTP GET with `User-Agent: yadgar/<version>` + no other headers; no body; ignores response cookies; 5s timeout |

---

## Scope — concrete file changes

### New files

| Path | Purpose |
|---|---|
| `yadgar/cli/update.py` | `yadgar update [--check | --install]` subcommand. Detects install method, queries Codeberg releases API, prints / executes upgrade. |
| `scripts/install/detect_install_method.sh` | Emits `pipx` / `brew` / `nix-flake` / `container` / `source` on stdout. Probes:  pipx — `pipx list \| grep yadgar`; brew — `brew list --formula yadgar`; nix — `command -v yadgar` resolves into `/nix/store`; container — `docker ps --filter name=yadgar` AND `command -v yadgar` resolves to a script that invokes container; source — `$YADGAR_DIR/.git` exists. |
| `yadgar/server/routes/control_update.py` (or extend existing control routes) | FastAPI route `/api/control/update`. POST. Returns JSON with version, available, install_method, upgrade_command, can_self_install (bool). |
| `yadgar/update/check.py` | Pure-Python version-probe helper. Uses httpx; honors timeout; anonymous. Reusable by CLI + HTTP route + daemon auto-check. |
| `yadgar/update/install_methods.py` | Install-method detection + upgrade-command generation. Pure Python; wraps `scripts/install/detect_install_method.sh`. |
| `yadgar/tests/test_update.py` | Tests for cli/update.py + check.py + install_methods.py. |

### Modified files

| Path | Change |
|---|---|
| `yadgar/__main__.py` | Register `update` subcommand. |
| `yadgar/daemon.py` | Wire auto-check on `start` (if `update.check_on_start: true`). Spawns a background thread (one-shot, not periodic). Logs result; does NOT block daemon start. Failures (network down, Codeberg unreachable) logged at WARNING; no exit. |
| `yadgar/config_yaml.py` | New `update.*` config block. Three-way sync (I25) — Settings + yaml + registry. |
| `yadgar/settings.py` (or wherever `Settings` is defined) | Add `UpdateSettings`: `check_on_start: bool = False`, `check_interval_hours: int = 24` (placeholder for future periodic check; v5.47 only uses on-start), `user_agent_template: str = "yadgar/{version}"`, `codeberg_releases_url: str = "https://codeberg.org/api/v1/repos/maxagahi/yadgar/releases/latest"`, `check_timeout_seconds: int = 5`. |
| `yadgar/server/__main__.py` (or wherever FastAPI app assembled) | Register `control_update.py` router under `/api/control/update`. Gated by existing debug-APIs middleware. |
| `pyproject.toml` | Version bump 5.46.0 → 5.47.0 (via `scripts/bump_version.py` from v5.46). No new deps (`httpx` already present). |
| `server.json` | Version bump. |
| `MIGRATION_NOTES.md` | v5.47.0 section: enable auto-check explicitly + `yadgar update` UX + privacy posture. |
| `CHANGELOG.md` | v5.47.0 entry. |
| `README.md` | Add "Updating" section: `yadgar update` command + auto-check opt-in. |

---

## Install-method detection logic

`scripts/install/detect_install_method.sh` (and parallel `yadgar/update/install_methods.py`):

```
1. Probe `command -v yadgar`.
   - Not found → exit 1, emit "not_installed".
2. Resolve `which yadgar` → real path.
3. Match real path against patterns (in order):
   - `/nix/store/*/bin/yadgar`         → emit "nix-flake"
   - `*/Cellar/yadgar/*/bin/yadgar`    → emit "brew"
   - `*/.local/pipx/venvs/yadgar/*`    → emit "pipx"
   - `*/git/yadgar/*` (editable install) → emit "source"
4. If `yadgar` is a shim that runs a container (test: `head -1 $(which yadgar)` matches `docker run`):
   - emit "container"
5. Fallback → emit "unknown"
```

For each install method, the upgrade command:

| Method | Upgrade command |
|---|---|
| `pipx` | `pipx upgrade yadgar` |
| `brew` | `brew upgrade yadgar` |
| `nix-flake` | `nix flake update --inputs-from <flake-ref>; nix profile upgrade '.*yadgar.*'` (user's actual flake-ref varies; generic guidance + link to docs) |
| `container` | `docker pull docker.io/openfantasy/yadgar:latest && systemctl --user restart yadgar` (Linux) OR `docker pull ... && launchctl kickstart -k gui/$UID/com.openfantasy.yadgar` (macOS) |
| `source` | `cd $(YADGAR_SRC); git pull && pip install -e .` (with `--quiet` if YADGAR_VENV is set) |
| `unknown` | Print: "Cannot determine install method. See https://codeberg.org/maxagahi/yadgar#updating for manual steps." |

---

## `/api/control/update` API spec

**Endpoint:** `POST /api/control/update`

**Auth:** bearer token (existing middleware) + `YADGAR_DEBUG_APIS_ENABLED=on` gate (existing).

**Request body (optional):**
```json
{
  "action": "check" | "install",
  "install_method_override": "pipx" | "brew" | "nix-flake" | "container" | null
}
```
Defaults: `action: "check"`, `install_method_override: null` (auto-detect).

**Response (200, action=check):**
```json
{
  "current_version": "5.46.0",
  "available_version": "5.47.0",
  "update_available": true,
  "install_method": "pipx",
  "upgrade_command": "pipx upgrade yadgar",
  "can_self_install": true,
  "release_notes_url": "https://codeberg.org/maxagahi/yadgar/releases/tag/v5.47.0",
  "checked_at": "2026-05-31T10:00:00Z"
}
```

**Response (200, action=install):**
```json
{
  "started": true,
  "install_method": "pipx",
  "command_invoked": "pipx upgrade yadgar",
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0,
  "completed_at": "2026-05-31T10:02:00Z"
}
```

**`can_self_install` semantics:**
- `true` if the daemon process has write access to the install path (pipx + source, usually).
- `false` for brew (requires user homedir write), nix-flake (read-only nix store), container (requires daemon restart from outside).

When `can_self_install: false`, the API only supports `action: check`; `action: install` returns 400 with explanation.

**Failure modes:**
- 503 if Codeberg API unreachable (timeout, DNS, etc).
- 400 if `action: install` + `can_self_install: false`.
- 401 if bearer token missing (existing middleware).
- 403 if `YADGAR_DEBUG_APIS_ENABLED!=on` (existing middleware).

---

## Anonymous version-check probe spec

Strictly version-only — no telemetry.

```
GET https://codeberg.org/api/v1/repos/maxagahi/yadgar/releases/latest
Headers:
  User-Agent: yadgar/<version>
  Accept: application/json
Body: (none)
```

NO request body, NO cookies, NO query params beyond Codeberg's standard.

Response parsing extracts `tag_name` only (e.g. `v5.47.0`). All other response fields ignored.

Corporate firewall handling: probe respects `HTTPS_PROXY` env var (httpx default). If proxy auth required and no creds available, probe fails silently — logged at WARNING; daemon continues. Users behind proxies who want auto-check: set `HTTPS_PROXY` + creds via existing httpx mechanisms.

Privacy audit: probe sends NO user-identifying data. `User-Agent` includes yadgar version (necessary for compatibility tracking on Codeberg side — which is anonymous aggregate). No other client info exposed.

---

## Open questions (must resolve during implementation)

1. **Anonymous version-check payload exact shape — corporate firewalls + privacy auditors.**
   - **Concerns:** corporate firewalls may flag unknown UAs; privacy auditors may scrutinize any phone-home.
   - **Lean:** `User-Agent: yadgar/<version> (+https://codeberg.org/maxagahi/yadgar)` — includes link to source repo for transparency. NO other headers, NO body, NO cookies (httpx default discards Set-Cookie when no jar).
   - **Resolution:** finalize UA string in Step 0. Document exact wire format in `docs/PRIVACY.md` (new file) and `MIGRATION_NOTES.md`.

2. **`/api/control/update` action=install — sync vs async.**
   - Sync (block until upgrade completes; can take 30s+) → simpler; client times out on slow upgrades.
   - Async (return job ID; poll for status) → more complex; needs job-tracking endpoint.
   - **Lean:** sync with 300s timeout. v5.47 audience is power users + Control-tab integration. Async is over-engineering. Document timeout.

3. **`can_self_install` semantic for daemon-context.**
   - Daemon runs as user. pipx install dir is `~/.local/pipx/venvs/yadgar/` — daemon CAN write there.
   - brew install dir is `/opt/homebrew/Cellar/yadgar/` — daemon CAN write there if `$USER` owns brew install.
   - nix-flake — read-only `/nix/store`. Daemon CANNOT write.
   - container — daemon process is INSIDE the container. CANNOT pull a new image OR restart its own systemd unit cleanly.
   - **Lean:** `can_self_install=true` only for `pipx` + `source`. brew + nix + container → `can_self_install=false`. Confirm by probing actual filesystem write permission in `install_methods.py`.

4. **Container update path — restart from inside.**
   - Container yadgar process cannot `systemctl restart yadgar.service` reliably (no systemctl inside container).
   - **Lean:** Container install returns `can_self_install=false` always. Update command is "run `docker pull ... && systemctl --user restart yadgar` from host." API tells user; user does.

5. **Periodic auto-check vs on-start-only.**
   - On-start (v5.47 ships this) — one probe per daemon-start. Catches updates after daemon restart.
   - Periodic (every 24h) — needs cron-like scheduling; daemon lifetime can span weeks.
   - **Lean:** v5.47 ships on-start only. Periodic is v5.49+ candidate; placeholder config `update.check_interval_hours: 24` is parsed but unused in v5.47 (forward-compat).

6. **Failure to check vs failed update.**
   - Check fails (Codeberg unreachable) → daemon logs WARNING, no user-visible alert. Control tab shows "Could not check for updates" message.
   - Update fails (e.g. `pipx upgrade` exits 1) → API returns 500 with stdout/stderr in body.
   - Both documented; UI handles distinct states.

7. **Pre-release tags filtering.**
   - Codeberg `/releases/latest` returns the latest non-prerelease by default.
   - But user may want pre-release. Add `update.include_prereleases: bool = False` config knob? **Lean:** yes — useful for early adopters. Default false.

---

## Plan steps (concrete, executable)

### Step 0 — Pre-flight (≤ 0.25 day)

- Confirm Codeberg `/api/v1/repos/maxagahi/yadgar/releases/latest` returns expected schema (`tag_name`, `name`, `html_url`).
- Lock anonymous probe UA string: `yadgar/<version> (+https://codeberg.org/maxagahi/yadgar)`.
- Confirm existing FastAPI debug-APIs middleware can be extended cleanly (read `yadgar/server/middleware/debug_apis_gate.py` or equivalent).
- Decide async vs sync for `action=install` (lean: sync + 300s timeout).
- Decide `can_self_install` probe mechanism (lean: `os.access(install_path, os.W_OK)`).

### Step 1 — TDD scaffolding (≤ 0.5 day)

Tests under `yadgar/tests/test_update.py`:

- `detect_install_method` returns expected method when:
  - `which yadgar` resolves to `/nix/store/...` → `nix-flake`
  - `which yadgar` resolves to `/.../Cellar/yadgar/.../bin/yadgar` → `brew`
  - `which yadgar` resolves to `/.../.local/pipx/venvs/yadgar/...` → `pipx`
  - `which yadgar` resolves to a script whose first line is `docker run ...` → `container`
  - `which yadgar` not found → `not_installed`
- `check.py` probes Codeberg API + returns `available_version` from mock response.
- `check.py` honors timeout; raises `TimeoutError` after `check_timeout_seconds`.
- `check.py` respects `HTTPS_PROXY` env (via httpx default).
- `check.py` sends correct UA string + no other identifying headers.
- `cli/update.py --check` runs probe, prints current vs available, exits 0.
- `cli/update.py --install` runs the upgrade command, exits with subprocess's exit code.
- `cli/update.py` on `can_self_install=false` install method + `--install` flag → prints "manual: <command>" + exits 0 (not error; just informational).
- `/api/control/update` route returns 403 without `YADGAR_DEBUG_APIS_ENABLED=on`.
- `/api/control/update` route returns 401 without bearer token.
- `/api/control/update` action=check returns expected JSON shape.
- `/api/control/update` action=install + `can_self_install=false` → 400.
- `/api/control/update` action=install + Codeberg unreachable → 503.
- Daemon `start` with `update.check_on_start: true` triggers background thread; daemon start completes within 1s regardless of probe latency.
- Daemon `start` with probe failure → logs WARNING, daemon continues.
- Three-way sync (I25): `update.check_on_start` knob is in Settings + yaml + registry.

### Step 2 — Install-method detection (≤ 0.5 day)

- Create `yadgar/update/install_methods.py` — pure Python detection.
- Create `scripts/install/detect_install_method.sh` — shell parallel for non-Python callers (Makefile uses this).
- `install_methods.py` exports: `detect_install_method() -> str`, `upgrade_command(method: str) -> str`, `can_self_install(method: str) -> bool`.

### Step 3 — Anonymous version-check probe (≤ 0.5 day)

- Create `yadgar/update/check.py` — httpx-based probe.
- Default timeout 5s; honors `HTTPS_PROXY`.
- Returns `CheckResult(current, available, update_available, checked_at)` dataclass.
- Errors on timeout / DNS / 5xx; caller handles.

### Step 4 — `yadgar update` CLI subcommand (≤ 0.5 day)

- Create `yadgar/cli/update.py`.
- Register in `yadgar/__main__.py`.
- Flags: `--check` (read-only probe), `--install` (probe + execute upgrade), `--install-method` (override auto-detect), `--include-prereleases` (overrides config knob).
- Default action (no flag): same as `--check`.

### Step 5 — `/api/control/update` HTTP route (≤ 0.5 day)

- Create `yadgar/server/routes/control_update.py` (or extend existing control routes module).
- POST with optional body.
- Returns the spec'd JSON.
- Existing debug-APIs middleware gates the route.
- Register in FastAPI app assembly.

### Step 6 — Auto-check on daemon start (≤ 0.25 day)

- Wire `update.check_on_start` config knob.
- On daemon `start`, if knob true, spawn `threading.Thread(target=_async_check_for_update, daemon=True)`.
- Failures log WARNING; no daemon-start blocking.
- Result cached in-process for `/api/control/update?action=check` to return without re-probing (per 5min TTL — placeholder; refine in v5.49 periodic check).

### Step 7 — Config + Settings + registry (I25 three-way sync) (≤ 0.25 day)

- Add `update.*` section to `yadgar/config_yaml.py` template + Settings class.
- Update `yadgar/registry/__init__.py` (or wherever I25 registry lives) with new knob list.
- Run `pytest yadgar/tests/test_config_three_way_sync.py` — must stay green.

### Step 8 — docs/PRIVACY.md (≤ 0.25 day)

- New file: `docs/PRIVACY.md`.
- Document exact wire format of version-check probe.
- Document opt-in posture (`update.check_on_start: false` default).
- Document corporate-firewall handling (`HTTPS_PROXY` honored).
- Document privacy guarantees (no IP, no user-ID, no usage data).
- Cross-reference: README.md, MIGRATION_NOTES.md v5.47.0 section.

### Step 9 — Version bump + CHANGELOG + MIGRATION_NOTES (≤ 0.25 day)

- `scripts/bump_version.py 5.46.0 5.47.0`.
- CHANGELOG.md v5.47.0 entry.
- MIGRATION_NOTES.md v5.47.0 section: opt-in instructions for auto-check + new `yadgar update` UX + privacy posture.

### Step 10 — v5.50 viz coordination note

Plan doc explicitly notes for v5.50 (Control tab UI) implementer:

- Backend API `/api/control/update` is shipped in v5.47.
- v5.50 Control tab Update button calls this endpoint.
- v5.50 terminal CLI annotation: if user clicks "Update" in Control tab, show the equivalent `yadgar update --install` command in a tooltip / footer.
- v5.50 implementer should NOT re-implement the version check; reuse `/api/control/update`.

---

## Acceptance criteria

v5.47.0 ships when ALL of the following are true:

- [ ] `yadgar update --check` runs without error on all 5 install methods (pipx/brew/nix/container/source).
- [ ] `yadgar update --install` performs the correct upgrade on pipx + source installs.
- [ ] `yadgar update --install` on brew/nix/container prints manual instructions + exits 0 (informational, not error).
- [ ] `yadgar update --include-prereleases --check` includes pre-release tags.
- [ ] `POST /api/control/update` returns expected JSON shape (action=check).
- [ ] `POST /api/control/update` honors auth middleware (403 without `YADGAR_DEBUG_APIS_ENABLED=on`, 401 without bearer).
- [ ] `POST /api/control/update` action=install on `can_self_install=false` returns 400.
- [ ] Daemon start with `update.check_on_start: true` triggers background check; daemon start time not delayed.
- [ ] Daemon start with `update.check_on_start: false` (default) does NOT make any outbound network call.
- [ ] Version-check probe sends EXACTLY the documented headers; no others.
- [ ] Probe respects `HTTPS_PROXY` env var.
- [ ] Probe fails gracefully on network errors; logs WARNING; daemon continues.
- [ ] `pytest yadgar/tests/test_update.py` green.
- [ ] `pytest yadgar/tests/test_config_three_way_sync.py` green (I25 maintained).
- [ ] `docs/PRIVACY.md` exists with documented probe wire format.
- [ ] `MIGRATION_NOTES.md` v5.47.0 section documents opt-in + privacy + new UX.
- [ ] `CHANGELOG.md` v5.47.0 entry exists.
- [ ] README "Updating" section exists.
- [ ] `python scripts/check_versions.py` exit 0.
- [ ] `python scripts/check_metric_writers.py` exit 0 (I23 maintained; new update.* metrics if any).
- [ ] `python scripts/check_trace_spans.py` exit 0 (I24 maintained; new HTTP handler has @trace_span).

**NOT in scope:** rollback, signed-artifact verification, periodic auto-check (placeholder only), Control-tab UI button (v5.50).

---

## Effort estimate (calendar days)

| Step | Days |
|---|---:|
| Step 0 pre-flight | 0.25 |
| Step 1 TDD scaffolding | 0.5 |
| Step 2 install-method detection | 0.5 |
| Step 3 version-check probe | 0.5 |
| Step 4 CLI subcommand | 0.5 |
| Step 5 HTTP route | 0.5 |
| Step 6 daemon auto-check | 0.25 |
| Step 7 config + I25 sync | 0.25 |
| Step 8 docs/PRIVACY.md | 0.25 |
| Step 9 bump + CHANGELOG + MIGRATION_NOTES | 0.25 |
| **Total** | **2 – 3 calendar days** |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| `pipx upgrade yadgar` mid-daemon-runtime kills the daemon process unpredictably | Document: user should stop daemon before `--install`; CLI prints warning if daemon is running. v5.48 candidate: graceful pre-upgrade hook. |
| Codeberg API rate-limit on version probes | One probe per daemon-start; default OFF; opt-in users self-limit. If sustained traffic causes Codeberg to throttle, switch to mirror or self-hosted metadata in v5.48+. |
| Corporate firewall blocks Codeberg → check always fails | Documented: respect `HTTPS_PROXY`; users behind firewalls can disable auto-check + use `yadgar update` manually. |
| Privacy auditors flag `User-Agent: yadgar/<version>` as identifying | `docs/PRIVACY.md` documents exact wire format. UA only shares yadgar version — already public info (visible in CLI output). No additional disclosure. |
| `can_self_install` heuristic wrong → user blocks on failed `pipx upgrade` mid-daemon | TDD step 1 covers permission probe + sets `can_self_install=false` defensively when probe fails. |
| Container update path requires daemon restart from outside container → confusing UX | API returns clear manual command; documented in README + MIGRATION_NOTES; Control tab v5.50 surfaces the command to the user. |
| `nix flake update` cross-flake-ref semantics differ per user setup | Update command for nix-flake is "guidance + docs link"; not auto-executed. v5.48+ candidate: detect user's actual flake-ref from `/proc/<daemon_pid>/exe` symlink resolution. |
| Pre-release tags accidentally surfaced as "update available" in stable channel | Default `update.include_prereleases: false`; Codeberg `/releases/latest` excludes pre-releases by default. Confirmed in Step 0. |
| Auto-check thread leaks on rapid daemon restart | Thread is `daemon=True` (Python convention) — terminated automatically on process exit. Smoke-tested in Step 1 TDD. |
| `/api/control/update` action=install runs sync — slow update blocks HTTP response | 300s timeout documented. Future async-job pattern is v5.49+ candidate. |

---

## Dependencies + blockers

- **Depends on v5.45.0 shipped** — install-asset layout + `detect_runtime.sh` / `detect_os.sh` foundation.
- **Depends on v5.46.0 shipped** — brew + nix install paths must be real before `detect_install_method.sh` returns them as valid options.
- **Blocks v5.50.0 (Control tab UI)** — Control tab Update button needs `/api/control/update` route. v5.50 must wait for v5.47 merge.
- **Does NOT block any other v5.x train** — update mechanism is additive.

---

## TDD test list

Under `yadgar/tests/test_update.py` (new file). Markers: `not integration` (no live Codeberg API calls — all httpx mocks).

1. `test_detect_install_method_pipx` — mock `which yadgar` → pipx path; returns `pipx`.
2. `test_detect_install_method_brew` — mock Cellar path; returns `brew`.
3. `test_detect_install_method_nix_flake` — mock `/nix/store/...` path; returns `nix-flake`.
4. `test_detect_install_method_container` — mock shim script with `docker run` first line; returns `container`.
5. `test_detect_install_method_source` — mock editable install in git dir; returns `source`.
6. `test_detect_install_method_not_installed` — `which yadgar` empty; returns `not_installed`.
7. `test_upgrade_command_pipx` — `upgrade_command("pipx")` returns `pipx upgrade yadgar`.
8. `test_upgrade_command_brew` — returns `brew upgrade yadgar`.
9. `test_upgrade_command_nix_flake` — returns the documented nix command.
10. `test_upgrade_command_container` — returns docker pull + restart.
11. `test_can_self_install_pipx` — write access to pipx dir; returns true.
12. `test_can_self_install_nix_flake` — `/nix/store` read-only; returns false.
13. `test_can_self_install_container` — always false.
14. `test_check_returns_available_version` — mock httpx response → CheckResult with mock tag_name.
15. `test_check_honors_timeout` — mock slow response; raises TimeoutError after 5s.
16. `test_check_respects_https_proxy` — assert httpx call uses proxy from env.
17. `test_check_sends_correct_user_agent` — assert UA string matches `yadgar/<version> (+https://codeberg.org/maxagahi/yadgar)`.
18. `test_check_sends_no_extra_headers` — assert no headers beyond UA + Accept.
19. `test_check_no_request_body` — assert GET method, no body.
20. `test_check_handles_codeberg_5xx` — mock 503 → raises ConnectionError.
21. `test_cli_update_check` — `yadgar update --check` invokes probe + prints output + exits 0.
22. `test_cli_update_install_pipx` — `yadgar update --install` on pipx mock invokes `pipx upgrade yadgar` subprocess.
23. `test_cli_update_install_brew_prints_manual` — on brew (can_self_install=false), prints manual command + exits 0.
24. `test_cli_update_include_prereleases` — `--include-prereleases` calls Codeberg `/releases` endpoint (not `/releases/latest`).
25. `test_api_update_check_requires_auth` — POST without bearer → 401.
26. `test_api_update_check_requires_debug_gate` — POST without `YADGAR_DEBUG_APIS_ENABLED=on` → 403.
27. `test_api_update_check_returns_expected_shape` — POST action=check → asserts JSON keys.
28. `test_api_update_install_can_self_install_false_returns_400` — POST action=install on container method → 400.
29. `test_api_update_install_can_self_install_true_runs_subprocess` — POST action=install on pipx → invokes upgrade subprocess.
30. `test_api_update_503_on_codeberg_unreachable` — mock httpx ConnectError → 503.
31. `test_daemon_start_check_on_start_true_spawns_thread` — mock daemon start; assert thread spawned + daemon=True flag.
32. `test_daemon_start_check_on_start_false_no_network` — mock daemon start; assert no httpx call.
33. `test_daemon_start_check_failure_logs_warning_does_not_block` — mock probe raises; daemon start completes within 1s + WARNING logged.
34. `test_config_update_section_three_way_sync` — runs `test_config_three_way_sync.py` style assertion on new `update.*` keys.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation work requires a feature branch — `feat/v5.47.0-update-mechanism`. Branch from latest master after v5.46.0 merges.
- Cross-cut with v5.50 viz: `/api/control/update` route MUST land before v5.50 Control tab implementer starts. Document the cross-cut explicitly in v5.50 plan (TBD).
- Privacy posture is a user-visible promise — `docs/PRIVACY.md` content should be reviewed by user before merge.
- Related plans: `docs/PLAN_V5_45_0_SETUP_FOUNDATION.md` (prerequisite) + `docs/PLAN_V5_46_0_DISTRIBUTION.md` (prerequisite).
- Implementer must read `docs/DECISIONS.md` PD-37 before re-scoping any install-method choice.
