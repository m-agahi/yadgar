# Migration Notes

## HOOKS Car 2 — global nix rehydrate script (BUG2, OUT-OF-REPO handoff)

The HOOKS compact/restore car fixed the in-repo post-compact restore key bug
(BUG1: `hook_runner.py` now reads `formatted`) and enriched the drain with
in-flight orchestration capture. **BUG2 — the wrong-directory restore in the
GLOBAL nix `yadgar-post-compact-rehydrate.sh`** — could NOT be verified or fixed
here: that script lives in the **nix dotfiles repo** (`dotfiles/common/yadgar-hooks/`,
per memory 993 / `llm.nix`), NOT in `/home/max/git/yadgar`. Hand-off, not a fix.

**Action for you (Max) — in the dotfiles/nix repo, not this one:**

1. Locate the rehydrate script:
   ```bash
   grep -rn 'post-compact-rehydrate\|yadgar restore\|yadgar-post-compact' \
     ~/path/to/dotfiles/common/yadgar-hooks/
   ```
2. Confirm whether it reads the project dir from stdin `cwd` first with `$(pwd)`
   only as a fallback (CORRECT — matches the in-repo `pre-compact-drain.sh`
   pattern), or uses `CWD=$(pwd)` unconditionally (the BUG2 wrong-dir defect).
   The correct pattern is:
   ```sh
   CWD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")
   if [ -z "$CWD" ]; then CWD=$(pwd); fi
   ```
3. If it uses `$(pwd)`-only → patch it to the stdin-first pattern above so
   post-compact restore targets the project dir, not the daemon-adjacent CWD.
   If it already reads stdin `cwd` first → BUG2 is a non-issue; no change needed.

**Optional (Car 2 enrichment, dotfiles side):** for the global nix
`yadgar-post-compact-rehydrate.sh` / `pre-compact-drain.sh` to feed the new
in-flight capture, the PreCompact `.sh` must pass the stdin `transcript_path`
through to `yadgar drain --transcript-path "$TRANSCRIPT"` (the in-repo
`yadgar/core/hooks/pre-compact-drain.sh` was updated to do this; mirror that edit
in the dotfiles copy). Without it, the drain still works — it just captures no
in-flight state (degrades to pre-Car-2).

---

## T4 Ettin CE-swap train — post-merge ops (core 5.132.0 / backend 5.43.0)

The train swaps the cross-encoder reranker from `Alibaba-NLP/gte-reranker-modernbert-base`
to the gate-winning Ettin variant (`GTE_RERANKER_MODEL` default flip), bakes all
default-ON model weights into `Dockerfile.backend` for offline self-sufficiency,
and sets `flake.nix` backend sizing to `--cpus 3` per ADR-0106 (supersedes
ADR-0097's 4-CPU verdict — 3→4 is flat under the corrected measurement). Config-only
swap + a backend-image change — no core recall code path changed.

### 1. Rebuild + push the backend image (bakes the models — build LOCALLY)

`Dockerfile.backend` now bakes MiniLM (embed), the Ettin CE, GTE CE (one-cycle
rollback), FlashRank ms-marco-MiniLM-L-12-v2, and doc2query-t5-small into the
image so a fresh container serves `/embed` + `/rerank` with no network and no HF
mount. Image grows ≈ +0.45–1.0 GB. `ci-release.yaml` `build-images` auto-builds
and pushes the backend image on merge (pyproject/backend changed), but if you
build/push manually:

```bash
podman build -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.43.0 .
podman push docker.io/openfantasy/yadgar-backend:5.43.0
```

Offline self-sufficiency was verified locally with a `--network none` + no-HF-mount
smoke (`/embed` returns a real vector, `/rerank` returns Ettin scores).

### 2. Set `--cpus 3` (ADR-0106) — restart the backend

`flake.nix` is edited to `--memory 6g --cpus 3` (was `--memory 4g --cpus 2`, the
temporary T4-planning posture). ADR-0106 supersedes ADR-0097's 4-CPU verdict: under
the corrected `yadgar_recall_duration_ms` measurement (ADR-0105) 3→4 is flat, so 3
CPUs captures the full 2→3 gather-budget win at lower cost. The file edit does NOT
restart the running container. After deploying 5.43.0, restart the backend so it
picks up 3 CPUs, then run the Ettin perf re-measure (empty slots in
`docs/testing/recall-perf-checklist.md`, "T4 Ettin swap — post-deploy measurement
prep"). Do NOT re-tune torch/gather knobs for Ettin — keep the GTE-derived settings
so a model-only revert stays clean.

### 3. Rollback lever (config-key, no revert needed)

To roll back to GTE without reverting the train:

```bash
# env override (busts the CE cache correctly — Car 0(d) `_ckpt` fix tracks the reranker)
YADGAR_GTE_RERANKER_MODEL=Alibaba-NLP/gte-reranker-modernbert-base
```

or edit the `GTE_RERANKER_MODEL` default in `yadgar/_shared/config/config.py`.
GTE is baked into the 5.43.0 backend image for one cycle, so an offline
config-revert to GTE works from the image alone. To roll back to the other Ettin
variant instead, set `YADGAR_GTE_RERANKER_MODEL=cross-encoder/ettin-reranker-68m-v1`
(or `-32m-v1`). Full-train revert = revert the single train PR; Car 0's fixes
(shipped separately as #188) survive the revert.

## Deps-modernization train — CI images rebuild required BEFORE merging (core 5.131.0 / backend 5.42.0)

`uv.lock` changed (blanket `uv lock --upgrade`: transformers 4.57.6→5.13.1,
huggingface-hub 0.36.2→1.23.0, hf-xet 1.3.2→1.5.1, starlette 1.0.0→1.3.1,
torch 2.11→2.13, and the rest of the float — see the PR body). `Dockerfile.ci`
bakes deps via `uv export --frozen`, so the yadgar-ci image MUST be rebuilt and
pushed at the new tag or CI silently tests the old stack (the 57-fail lock-parity
class). All workflow refs + `Dockerfile.ci-viz` now point at `5.131.0` — that tag
does not exist until you build it.

Build ORDER matters — `yadgar-ci-viz` does `FROM yadgar-ci:5.131.0`, so build
and push yadgar-ci FIRST (PD-42 carve-out: yadgar-ci is the one image that
pushes to dockerhub):

```bash
podman build -f Dockerfile.ci -t docker.io/openfantasy/yadgar-ci:5.131.0 .
podman push docker.io/openfantasy/yadgar-ci:5.131.0
podman build -f Dockerfile.ci-viz -t docker.io/openfantasy/yadgar-ci-viz:5.131.0 .
podman push docker.io/openfantasy/yadgar-ci-viz:5.131.0
```

Expected on merge (not a defect): `ci-release.yaml` `build-images` auto-builds
and pushes the **core** + **backend** DockerHub images (pyproject version ≠
latest `v*` tag). yadgar-ci itself has NO auto-build pipeline — the manual
build above is the only path.

Local NixOS dev-env note: numpy 2.5.1 (blanket float, was 2.4.4) dlopens the
system `libz.so.1`, which a Nix-built python does not see by default —
`import numpy` fails with `libz.so.1: cannot open shared object file` unless
zlib is on `LD_LIBRARY_PATH`. Local test/benchmark runs in this train used
`LD_LIBRARY_PATH=/run/current-system/sw/share/nix-ld/lib`. Consider extending
the existing numpy LD_LIBRARY_PATH wrapper in `flake.nix` (~line 426, currently
`stdenv.cc.cc.lib` only) with `pkgs.zlib`'s lib dir in the nix repo. CI/Docker
(Debian) unaffected.

Also note: the `[onnx]` extra was DROPPED from `sentence-transformers` in
`pyproject.toml` — `optimum-onnx` (0.1.0, latest) caps `transformers<4.58.0`
and hard-blocks the 5.x resolve. The dormant `GTE_RERANKER_BACKEND=onnx-int8`
code path + knobs were removed with it (no half-drop, per plan Q8). If you ever
re-add ONNX reranking, it needs an optimum-onnx release that supports
transformers 5.x first.

## R3 — CROSS_ENCODER_BACKEND knob removed (ADR-0043 NO-GO → full removal)

`YADGAR_CROSS_ENCODER_BACKEND` has been removed. The onnx-int8 CE backend was
never the default and was NO-GO per latency A/B (2× slower under `--cpus 2` due
to ORT thread thrash). The setting is now gone from Settings, config_registry,
config_yaml, and docs.

**If you have `YADGAR_CROSS_ENCODER_BACKEND=onnx-int8` in your env or yaml:**
Pydantic-settings ignores unknown environment variables (no `extra = "forbid"`),
so the knob will be silently ignored after upgrade — the CE will always use the
`st` (fp32 torch) backend. Remove the knob from your env/yaml to keep configs
clean.

**Note:** `YADGAR_GTE_RERANKER_BACKEND` (GTE reranker onnx-int8 path) is
unaffected and still present.

---

## v5.53.0 — Bootstrap catalog + read-first contract (2026-06-12)

### No config knob changes in this release

`wiki_catalog` is added to `project_brief` catalog/restore/full payloads as a new
structured key. No env var tuning needed; per-group cap is the internal constant
`_WIKI_CATALOG_MAX_PER_GROUP = 5` (not runtime-tunable by design).

## v5.50.2 — Control tab + backend control APIs (2026-06-11)

### New env var: `YADGAR_DEBUG_APIS_ENABLED`

The Control tab's config editor, action triggers, and restart endpoints are all gated behind a new
umbrella env var introduced in v5.50.2:

```
YADGAR_DEBUG_APIS_ENABLED=on   # or true / 1 / yes
```

Set this in `~/.config/yadgar/config.yaml`:

```yaml
debug_apis_enabled: true
```

**Important:** bearer token (`YADGAR_MCP_AUTH_TOKEN`) is ALSO required — the debug gate is a
second layer on top of auth, not a replacement.

`YADGAR_UPDATE_DEBUG_APIS_ENABLED` remains the narrower gate for `/api/control/update` only.
Do not confuse the two.

### Restart endpoints: sentinel-file design (non-negotiable)

`POST /api/control/restart/yadgar` and `POST /api/control/restart/backend` write a sentinel file
to `$XDG_STATE_HOME/yadgar/restart-<service>.request` (default: `~/.local/state/yadgar/`).

**The daemon does NOT restart itself.** Until you install the systemd watcher units below,
these endpoints are **inert** — they write a file and nothing else. This is the safe default.

#### Required systemd unit files (user session units)

Install these in `~/.config/systemd/user/` (or your nix home-manager config):

**`yadgar-restart-watcher.path`** — watches the sentinel for the `yadgar` daemon:

```ini
[Unit]
Description=Watch for yadgar restart sentinel
ConditionPathExists=%h/.local/state/yadgar/

[Path]
PathChanged=%h/.local/state/yadgar/restart-yadgar.request
Unit=yadgar-restart-actor.service

[Install]
WantedBy=default.target
```

**`yadgar-restart-actor.service`** — performs the actual restart:

```ini
[Unit]
Description=Restart yadgar daemon on sentinel

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl --user restart yadgar.service
ExecStartPost=/bin/rm -f %h/.local/state/yadgar/restart-yadgar.request
```

**`yadgar-backend-restart-watcher.path`** — watches for the `yadgar-backend` sentinel:

```ini
[Unit]
Description=Watch for yadgar-backend restart sentinel
ConditionPathExists=%h/.local/state/yadgar/

[Path]
PathChanged=%h/.local/state/yadgar/restart-yadgar-backend.request
Unit=yadgar-backend-restart-actor.service

[Install]
WantedBy=default.target
```

**`yadgar-backend-restart-actor.service`**:

```ini
[Unit]
Description=Restart yadgar-backend on sentinel

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl --user restart yadgar-backend.service
ExecStartPost=/bin/rm -f %h/.local/state/yadgar/restart-yadgar-backend.request
```

Enable the path units after creating them:

```bash
systemctl --user daemon-reload
systemctl --user enable --now yadgar-restart-watcher.path
systemctl --user enable --now yadgar-backend-restart-watcher.path
```

**nix home-manager:** add the four unit stanzas to `systemd.user.services` /
`systemd.user.paths` in your `home.nix`. Do not apply directly — hand this file to the user.

### Restart confirmation protocol

Both restart endpoints require `{"confirm": "<service-name>"}` in the JSON body:

- `/api/control/restart/yadgar` → `{"confirm": "yadgar"}`
- `/api/control/restart/backend` → `{"confirm": "yadgar-backend"}` ← note the full name

Any mismatch returns 400. The UI enforces typed confirmation before enabling the button.

---

## v5.50.0 — Tab router, viz Variant C, bookmarks redirect (2026-06-10)

### Tab router (frontend change)

`yadgar/static/index.html` now uses a hash-router with 6 tabs. The viz server (`viz_server.py`) serves it unchanged — the SPA handles routing in the browser. No backend changes required.

**Tabs shipped:**
- `#home` — full-canvas 3D graph (default).
- `#stats` — memory stats panel.
- `#health` — daemon health / process / queue metrics.
- `#info` — version info, viz config summary, keyboard shortcuts.
- `#bookmarks` — EMPTY placeholder (v5.50.1 adds content).
- `#control` — EMPTY placeholder (v5.50.2 adds content + API gate).

### bookmarks.html redirect

`GET /static/bookmarks.html` now returns **302 → /#bookmarks** instead of serving the standalone page. The HTML file itself also has a JS redirect as fallback. The file will be removed in **v5.52.0** — do not depend on it after v5.50.x.

If you bookmarked `http://localhost:42069/static/bookmarks.html` directly, update your browser bookmark to `/#bookmarks`.

### Viz Variant C defaults

Three viz knobs changed defaults in v5.50.0. If your `~/.yadgar/config.yaml` sets these explicitly, your overrides take precedence — no action needed. If you rely on the defaults, the new values take effect immediately:

| Knob | v5.49.x default | v5.50.0 default |
|---|---|---|
| `viz_edge_width_3d_multiplier` | 1.5 | **1.8** |
| `viz_physics_charge_strength` | -12.0 | **-18.0** |
| `viz_edge_opacity` | (new) | **0.9** |

New config knobs added (three-way registry):
- `YADGAR_VIZ_EDGE_OPACITY` (float, default 0.9) — wired to `.linkOpacity()` in 3D graph init.
- `YADGAR_VIZ_EDGE_VARIANT` (string, default `"C"`) — informational; no renderer consumer in v5.50.0.
- `YADGAR_VIZ_WIKI_SHAPE` (string, default `"octahedron"`) — config only; renderer deferred (see below).

### Deferred: wiki-node octahedron renderer

`VIZ_WIKI_SHAPE = "octahedron"` is registered in the config system but the mesh renderer is NOT wired in v5.50.0. Three prior implementation attempts (v5.10.7, v5.10.7.1, v5.10.7.2) produced "fragmented triangle shard" rendering and were reverted in v5.10.7.3. The guards in `test_viz_static_assets.py::TestV510703RevertCustomMesh` remain in place. The config default registers the intent; wiring the renderer requires deeper ForceGraph3D + Three.js investigation (tracked for a future minor).

### Deferred: zoom regression bisect

The spec identified a suspected zoom regression in the v5.10.4–v5.11.0 range (graph loads zoomed-in then auto-zooms out). Bisect requires a headless browser to assert `camera.position.z` — not available in the worktree environment. Deferred to v5.50.1. Suspected commit range based on code inspection: `966c9a4` (v5.10.10, `auto-zoom-fit on initial load`) added `zoomToFit()` logic using `_engineTickCount`; `280828d` (v5.11.0, `await loadVizConfig() before initGraph()`) changed boot order. If zoom appears incorrect, check `YADGAR_VIZ_LAYOUT_ZOOM_FIT_TICK` (default 80) — reducing it triggers zoom earlier; increasing it delays.

## v5.49.0 — Upgrade orchestrator + memory archive retention (2026-06-08)

Both strands ship OFF by default. No action required unless you want to opt in.

### Archive retention rollout (Strand A)

Follow these steps in order. Do not skip the dry-run.

1. **Ship v5.49.0.** `MEMORY_ARCHIVE_RETENTION_DAYS` defaults to 0. Auto-purge is disabled. No data deleted automatically.

2. **Dry-run first.** Validate the candidate set before any real deletion:

   ```bash
   # MCP call
   archive_purge(dry_run=True)
   # Returns: {"candidates": N, "would_delete": N, "protected": M, "dry_run": true}
   ```

   Confirm the candidate count looks reasonable (~1300 for existing installs with accumulated heat=0 archives).

3. **One-time explicit cleanup** (optional, clears the backlog):

   ```bash
   archive_purge(dry_run=False)
   ```

   This deletes heat=0 archives older than the default thrash guard (7 days). Anchored memories (`_anchor` tag or `is_protected=True`) are never deleted.

4. **Enable nightly auto-purge** (optional):

   Add to `~/.config/yadgar/config.yaml`:

   ```yaml
   memory_archive_retention_days: 90
   ```

   The nightly consolidation cycle will now purge archives older than 90 days. Circuit breaker caps a single cycle at 500 deletions.

5. **Audit anchors-by-prose** before enabling nightly purge:

   ```bash
   audit_anchors()
   ```

   Look for the `"anchored_by_prose_only"` bucket in the report. These memories carry anchor language in their content but lack the `_anchor` tag or `is_protected=True` — the purge helper cannot identify them as protected. Add `is_protected=True` via `memory_update` if you want them preserved.

### Upgrade orchestrator rollout (Strand B)

Follow these steps in order. Read the rollback section before running `--install`.

1. **Ship v5.49.0.** `update.install_enabled` defaults to `false`. `yadgar update --install` refuses with an opt-in message.

2. **Confirm update available:**

   ```bash
   yadgar update --check
   ```

   Verify the new version is what you expect before proceeding.

3. **Read the rollback recovery section below.** Understand what happens on each failure terminal before flipping the knob.

4. **Enable the orchestrator:**

   Add to `~/.config/yadgar/config.yaml`:

   ```yaml
   update:
     install_enabled: true
   ```

5. **Run the upgrade:**

   ```bash
   yadgar update --install
   ```

   Orchestrator will: probe PyPI → write snapshot to `~/.local/state/yadgar/upgrade-snapshots/` → pull new image → rewrite `upgrade.env` → graceful-stop daemon → restart via systemd → health-check → `pipx upgrade yadgar` → re-exec to `--finalize`.

   Snapshot retention: keeps the 3 most recent (configurable via `update.snapshot_retention`).

### Rollback recovery procedures

#### `ROLLED_BACK_OK` (exit 1)

Orchestrator caught a failure at image-pull or health-check and successfully reverted. Old image tag restored in `upgrade.env`. Daemon restarted on old image. Nothing more needed. Retry after investigating the failure.

#### `ROLLED_BACK_FAILED` (exit 2)

Orchestrator caught a failure AND the subsequent rollback also failed. Daemon may be in an inconsistent state.

Recovery:

```bash
yadgar update --rollback
```

This reads `prev_image_tag` from the latest snapshot in `~/.local/state/yadgar/upgrade-snapshots/`, rewrites `upgrade.env`, and restarts the daemon.

#### `DONE_CLI_ROLLBACK_FAILED` (exit 2)

Orchestrator succeeded on image + restart + health-check. `pipx upgrade yadgar` succeeded. But re-exec or `--finalize` failed AND the subsequent `pipx install --force yadgar==<prev>` also failed (prior version may not be on PyPI — see PD-45 note).

Daemon is on the new image (healthy). CLI version mismatch.

Manual recovery:

```bash
pipx install --force yadgar==<prev-version>
```

Check `https://pypi.org/project/yadgar/#history` for available versions. If the prior version is absent from PyPI (PD-45 internal-dev no-tag policy), you cannot auto-pin. Use `make setup` to regenerate a consistent state.

#### `DONE_BUT_FINALIZE_FAILED` (exit 4)

Daemon is healthy on new image. New CLI is installed. Only the `--finalize` version-verification handshake failed.

Recovery:

```bash
yadgar update --rollback
```

Then investigate daemon logs:

```bash
journalctl --user -u yadgar.service -n 100
```

---

## v5.48.0 — Update mechanism: `yadgar update` CLI + auto-check + `/api/control/update` (2026-06-07)

### What's new

Check-only update mechanism. `--install` deferred to v5.49.

### Opt-in: auto-check on daemon start

Add to `~/.config/yadgar/config.yaml`:

```yaml
update_check_on_start: true
```

Default is `false`. When enabled, a background thread probes PyPI on every daemon start and logs the result at WARNING if an update is available.

**Privacy posture:** anonymous GET to `https://pypi.org/pypi/yadgar/json` with `User-Agent: yadgar/<version>`. No user-ID, no telemetry, no IP collection beyond standard PyPI server logs. See `docs/reference/privacy.md` for exact wire format.

### `yadgar update --check` CLI

```bash
yadgar update          # same as --check
yadgar update --check  # probe PyPI, print upgrade command, exit 0
```

Output example:
```
yadgar 5.47.0
Install method: pipx
Update available: 5.48.0
Release notes: https://pypi.org/project/yadgar/5.48.0/
Upgrade command:
  pipx upgrade yadgar
```

### `/api/control/update` HTTP endpoint

Requires:
1. `YADGAR_REQUIRE_AUTH=1` + `YADGAR_MCP_AUTH_TOKEN=<token>` (existing auth).
2. `YADGAR_UPDATE_DEBUG_APIS_ENABLED=on` (new gate, default off).

```bash
curl -X POST http://localhost:8765/api/control/update \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"action": "check"}'
```

Response:
```json
{
  "current_version": "5.47.0",
  "available_version": "5.48.0",
  "update_available": true,
  "install_method": "pipx",
  "upgrade_command": "pipx upgrade yadgar",
  "release_notes_url": "https://pypi.org/project/yadgar/5.48.0/",
  "checked_at": "2026-06-07T10:00:00+00:00"
}
```

`action=install` returns 400 in v5.48 (deferred to v5.49 — pipx upgrade kills daemon mid-call).

### Corporate firewall handling

Set `HTTPS_PROXY` env var. httpx will route the probe through the proxy:

```bash
HTTPS_PROXY=http://proxy.corp:3128 yadgar daemon start
```

Disable auto-check in air-gapped environments: keep `update_check_on_start: false` (the default).

### No action required for existing installs

All new config knobs have safe defaults (auto-check OFF, API gate OFF). No BREAKING CHANGES.

---

## v5.46.20 — Comprehensive install path fixes (2026-06-07)

### Context

Six bugs discovered via Rocky Linux VM SSH diagnostic session. Backend daemon failed with:
```
RuntimeError: REQUIRE_AUTH=1 requires YADGAR_MCP_AUTH_TOKEN to be set
```
Once SELinux was set Permissive, daemon started but auth token was still missing.
Root cause investigation revealed 5 additional issues in the same install path.

### Bug Summary

| # | P | Symptom | Root cause |
|---|---|---------|------------|
| 1 | P0 | `RuntimeError: REQUIRE_AUTH=1 requires YADGAR_MCP_AUTH_TOKEN to be set` | `yadgar.service.in` ExecStart missing `-e YADGAR_MCP_AUTH_TOKEN=…` passthrough |
| 2 | P0 Rocky | `Permission denied` inside container even with SELinux Enforcing | `:Z` relabel flag insufficient on Rocky 9 `admin_home_t` context on `/root/.yadgar` |
| 3 | UX | Silent 30s wait timeout with no feedback; cold-start exceeds 30s | `_wait_for_daemon` timeout 30s too short; no progress log |
| 4 | UX | Upgrade: old container still running after pull | `_step_pull_images` didn't stop running containers before pull |
| 5 | UX | (covered by BUG 3 fix — no separate change needed) | — |
| 6 | correctness | Re-seed should be idempotent | Similarity gate confirmed dedup; 409/`created=0` both handled |

### Fixes

**BUG 1 — `yadgar.service.in`:** Added to ExecStart env block:
```ini
-e YADGAR_MCP_AUTH_TOKEN=${YADGAR_MCP_AUTH_TOKEN} \
```
`secrets.env` is loaded via `EnvironmentFile=` so `${YADGAR_MCP_AUTH_TOKEN}` expands at start time. Without this line the value never reached the container.

**BUG 2 — SELinux fix (Option A):**

Removed `:Z` from both service templates. Added `--security-opt label=disable` before `--user root`:
```ini
ExecStart=@RUNTIME@ run --name yadgar --rm --security-opt label=disable --user root \
    …
    -v @DATA_DIR@:/data \
```
Rationale: personal-mode installs run as root; container also runs as root. SELinux MAC adds no isolation in this configuration. `--security-opt label=disable` prevents all SELinux policy checks for these containers — simpler and more reliable than per-directory relabeling.

Trade-off: SELinux MAC is disabled for yadgar containers. Multi-tenant or shared-host deployments should use a dedicated data directory, run as a non-root user, and apply `chcon -Rt container_file_t` or keep `:Z` with a non-`/root` data path.

Note: `_step_pre_create_dirs` (v5.46.19) still runs before service start — belt-and-suspenders for first-write race conditions.

**BUG 3 — `_wait_for_daemon` timeout + progress:**
```bash
local timeout="${1:-120}"   # was 30
…
sleep 2
elapsed=$((elapsed + 2))
if [ $((elapsed % 10)) -eq 0 ]; then
    log "  Waiting for daemon... (${elapsed}s / ${timeout}s)"
fi
```
`_step_seed_anchors` call site updated from `_wait_for_daemon 30` to `_wait_for_daemon 120`.

**BUG 4 — Stop containers before pull:**
```bash
for ctr in yadgar yadgar-backend; do
    if "$RUNTIME" ps --format '{{.Names}}' 2>/dev/null | grep -qx "$ctr"; then
        log "  Stopping running container: $ctr"
        "$RUNTIME" stop "$ctr" 2>/dev/null || true
    fi
done
```
Added at start of `_step_pull_images` before the pull commands.

**BUG 6 — Seed idempotency:**
The `/hooks/seed-anchor` route calls `memorize()` which goes through the similarity gate. On second seed:
- Gate returns `status=duplicate/skipped/deduped` → route returns `{"created": 0}` → `seed.py` increments `skipped`.
- 409 Conflict responses also counted as `skipped`.
No client-side change required. Confirmed by test `TestSeedIdempotency`.

### Operator action — Rocky Linux users

With v5.46.20, SELinux can stay Enforcing. The `:Z` relabel is no longer attempted:
```bash
pipx upgrade yadgar
setenforce 1        # re-enable SELinux Enforcing if previously set Permissive
yadgar-setup        # regenerates units with label=disable; restarts services
yadgar --version    # should show daemon alive
```

If `yadgar --version` still shows daemon down, check:
```bash
journalctl --user -u yadgar.service -n 30
journalctl --user -u yadgar-backend.service -n 30
```

### Operator action — new installs

No action needed. `pipx install yadgar && yadgar-setup` gets all fixes automatically.

---

## v5.46.19 — Rocky Linux SELinux + restart-on-regen hotfix (2026-06-06)

### Context

Two bugs observed on Rocky Linux (SELinux enforcing):

1. Backend container failed immediately after start:
   ```
   mkdir: cannot create directory '/data/logs': Permission denied
   surrealdb_server: IO error: Permission denied (os error 13)
   Goodbye!
   ```
   Cause: podman bind-mount `-v /root/.yadgar:/data` lacks `:Z` (private-relabel) flag.
   SELinux denies `container_file_t` process writes to default-labeled host directory.
   `systemd Restart=on-failure` caused infinite restart loop.

2. After `yadgar-setup` re-run (upgrade or config change), the backend unit file was
   regenerated with the correct image tag but systemd was not reloaded/restarted.
   The running container continued on the stale unit (old image, old volume flags).

### Fix

**Service templates** (`yadgar-backend.service.in`, `yadgar.service.in`):
- All `-v @DATA_DIR@:/data` lines now read `-v @DATA_DIR@:/data:Z`.
- `:Z` instructs podman/docker to relabel the host directory with `container_file_t`
  (private to this container) before mounting. No manual `chcon` needed on fresh installs.

**`yadgar-setup.sh` — `_step_pre_create_dirs()`** (new, runs before unit start):
```bash
mkdir -p "${YADGAR_DIR}/logs"
chmod 700 "${YADGAR_DIR}/logs"
```
Prevents the container's own `mkdir /data/logs` from being the first write operation
on an unlabeled directory (belt-and-suspenders with `:Z`).

**`yadgar-setup.sh` — `_step_enable_units()`** (reinstall path):
```bash
run systemctl --user daemon-reload
run systemctl --user enable yadgar.target
if systemctl --user is-active --quiet yadgar.target 2>/dev/null; then
    log "  Reinstall detected — restarting yadgar.target"
    run systemctl --user restart yadgar.target
else
    run systemctl --user start yadgar.target
fi
```

### Operator action — existing broken Rocky install

```bash
systemctl --user stop yadgar.target
mkdir -p ~/.yadgar/logs
chcon -Rt container_file_t ~/.yadgar/   # one-time SELinux relabel (if needed)
pipx upgrade yadgar
yadgar-setup   # regenerates unit with :Z + correct backend version; reloads + restarts
```

After `yadgar-setup` completes, `yadgar --version` should show daemon running.

### Operator action — new installs

No action required. Fresh installs via `pipx install yadgar && yadgar-setup` will get
`:Z` in the generated unit automatically.

---

## v5.46.18 — yadgar --version flag (core + backend + daemon probe) (2026-06-06)

### Context

`yadgar --version` did not exist. `setup.sh` resolved package version by reading the shebang of the `yadgar` pipx shim to locate the venv python, then running `python -c "import yadgar; print(yadgar.__version__)"`. This broke on systems where the shim shebang path was unavailable or non-executable (brew install on macOS, nix environments, system python path drift).

Additionally, `yadgar.__version__` returned "unknown" in uninstalled dev environments because `importlib.metadata.version()` raised `PackageNotFoundError` with no fallback.

### Fix

**New flag:** `yadgar --version` prints:
```
yadgar core       5.46.18
yadgar backend    5.4.0
yadgar daemon     5.46.18 (uptime 142s, db ok, embed ok)
```
Or if daemon not running:
```
yadgar core       5.46.18
yadgar backend    5.4.0
yadgar daemon     not running (start with `systemctl --user start yadgar.target`)
```

**JSON mode:** `yadgar --version --json` emits:
```json
{"core": "5.46.18", "backend": "5.4.0", "daemon": {"running": true, "version": "5.46.18", "uptime_seconds": 142, "db": true, "embed": true}}
```

**setup.sh** `_resolve_yadgar_version()` / `_resolve_backend_version()` now use `yadgar --version | awk` as primary extraction. Shim-shebang approach preserved as fallback for staged upgrades.

**`yadgar/__version__`** now falls back to reading `pyproject.toml` when the package is not installed (dev environments, repo checkouts without `pip install -e .`).

### Operator action

**No action required.** `yadgar --version` is additive. Existing installs will gain the flag after upgrading to 5.46.18.

**setup.sh users:** `_resolve_yadgar_version` and `_resolve_backend_version` auto-use `yadgar --version` if available; shim-shebang fallback fires transparently on pre-5.46.18 installs during staged upgrade.

---

## v5.46.17 — secrets dedup: drop YADGAR_DB_USER/PASS from bootstrap (2026-06-06)

### Context

`bootstrap_secrets.sh` wrote two credential pairs to `secrets.env`:
- `YADGAR_RW_USER/PASS` — canonical name after post-rename
- `YADGAR_DB_USER/PASS` — legacy alias with the same intended value

The generated-mode path (test dryrun heredoc) called `$(_gen)` **twice** — once for `YADGAR_RW_PASS` and once for `YADGAR_DB_PASS` — producing **different passwords** on every fresh install. Interactive mode happened to assign both from the same shell variable (`${RW_PASS}`), masking the divergence. Net effect: fresh installs via `YADGAR_TEST_DRYRUN=1` or `INSTALL_NONINTERACTIVE=1` got divergent RW vs DB credentials.

### Fix

**New installs (v5.46.17+):** `secrets.env` contains only `YADGAR_RW_USER/PASS` (+ SURREAL and RO). `YADGAR_DB_USER/PASS` is not written.

**Runtime consumers updated** to prefer canonical `YADGAR_RW_USER`:
- `daemon.py` systemd unit: `-e YADGAR_DB_USER` resolves `${YADGAR_RW_USER:-${YADGAR_DB_USER:-${SURREAL_USER}}}` and same for `PASS`.
- `vacuum/__init__.py` `_build_http_client`: chain is SURREAL_USER → YADGAR_RW_USER → YADGAR_DB_USER → hardcoded root.
- `vacuum/phases.py` `_surreal_headers`: same chain.

**NOTE:** Vacuum is a root-level admin operation (DEFINE NAMESPACE, DEFINE USER ON ROOT). `SURREAL_USER` is preserved as the first preference — removing it would break vacuum on hosts where only root creds are injected. `YADGAR_RW_USER` is secondary.

### Operator action

**Existing installs:** no action needed. Runtime consumers fall back to `YADGAR_DB_USER` if present, so old `secrets.env` files continue working as-is.

**Fresh installs:** run `bootstrap_secrets.sh` — new `secrets.env` will have only `YADGAR_RW_USER/PASS`. Clean and correct.

**Hosts with divergent RW vs DB passwords** (generated-mode installs before v5.46.17): re-run bootstrap or manually set `YADGAR_RW_PASS` to the correct value. Check which password the running daemon accepted and use that.

### Follow-up (not in scope for v5.46.17)

`storage/__init__.py` reads `YADGAR_DB_USER` directly — it receives this value via the container env var set by the daemon systemd unit (which now sources from `YADGAR_RW_USER` first). No code change needed in storage for now. Once all legacy hosts have re-bootstrapped, `YADGAR_DB_USER` pass-through in the systemd unit can be simplified to `YADGAR_RW_USER` directly.

The core container (`yadgar.service`) currently does not receive `-e YADGAR_RW_USER=` — only the backend does. The RW-first branch in `daemon.py` is forward-looking; in production the RW value arrives as `YADGAR_DB_USER` via shell expansion. A follow-up cleanup can add `-e YADGAR_RW_USER=` to the core service unit.

---

## v5.46.15 — seed anchors via daemon REST endpoint (2026-06-06)

### Context

v5.46.14 fixed step 9 install-rules. v5.46.15 fixes the step 10 crash on Rocky VM:

```
==> Step 10/10: Seeding canonical anchors...
Loading anchors from: /root/.local/share/pipx/venvs/yadgar/share/yadgar/install_assets/seeds/anchors.yaml
Traceback ...
  File "yadgar/cli/seed.py", line 48, in _seed_anchors
    from yadgar.db import get_db
ModuleNotFoundError: No module named 'yadgar.db'
```

Root cause: `_seed_anchors` in `cli/seed.py` imported `yadgar.db.get_db` — a dead SQLite API from the pre-SurrealDB era. The `yadgar.db` module was removed when the daemon migrated to SurrealDB via a backend container. The import was never cleaned up.

### Fix

`_seed_anchors` now POSTs to the daemon's `/hooks/seed-anchor` endpoint (new in v5.46.15) instead of writing to SQLite directly. The daemon handles SurrealDB write, similarity-gate dedup, and branch resolution — CLI is a thin HTTP client.

**Why `/hooks/seed-anchor` and not JSON-RPC `POST /mcp`?**
The MCP tools use streamable-HTTP transport at `POST /mcp` with JSON-RPC 2.0 envelope and SSE-framed responses. No existing CLI call-site uses this path; parsing SSE responses adds complexity and fragility for a hotfix. The `/hooks/seed-anchor` REST wrapper is the same pattern as `/hooks/subagent-stop` — used by all existing daemon hook callers. Write path ownership is unchanged: daemon owns all SurrealDB writes.

### setup.sh step 10 changes

New `_wait_for_daemon()` helper polls `localhost:8765/health` for up to 30 seconds. On Linux it first attempts `systemctl --user start yadgar.target`. On macOS it probes only (launchctl auto-start deferred to v5.46.16).

If the daemon fails to start within 30 seconds, step 10 prints:
```
WARN: Daemon failed to start in 30s. Skipping anchor seed.
After daemon starts, run manually:
  yadgar seed --anchors <path/to/anchors.yaml>
```
and returns exit 0 (setup continues, anchors are not seeded).

### User migration

```bash
pipx upgrade yadgar
yadgar-setup
```

Step 10 now starts the daemon (Linux) or waits for it (macOS), then POSTs anchors via daemon. If daemon never starts, step 10 skips gracefully and prints the manual command.

### macOS note

`_wait_for_daemon` on macOS probes `/health` only — no launchctl auto-start. Daemon must be running before `yadgar-setup` for step 10 to seed anchors. launchctl auto-start ships in v5.46.16.

---

## v5.46.14 — step 9 install-rules venv python fix (2026-06-06)

### Context

v5.46.13 fixed step 8 config init-if-missing. v5.46.14 fixes the step 9 failure on fresh Rocky Linux pipx installs:

```
Step 9/10: Installing rules (CLAUDE.md fragment)...
ERROR: Cannot locate install_assets/. Is yadgar installed correctly? (sys.prefix=/usr/share/yadgar/install_assets)
```

Root cause: `_locate_install_assets()` called bare `python3 -c "import sys; ..."`. On Rocky Linux (and other bare Linux VMs), `/usr/bin/python3` is the system python with `sys.prefix=/usr`. The candidate path `/usr/share/yadgar/install_assets/` does not exist — wheel assets are shipped into the pipx venv (`~/.local/share/pipx/venvs/yadgar/`), not `/usr`.

Same class of bug as v5.46.11 (`_resolve_yadgar_version`) and v5.46.12 (`_resolve_backend_version`).

### Fix

New `_get_venv_python()` helper reads the shebang of the `yadgar` pipx shim (e.g. `/root/.local/bin/yadgar`) to get the venv python path:

```bash
_get_venv_python() {
    local yadgar_shim
    yadgar_shim=$(command -v yadgar 2>/dev/null) || { echo "python3"; return; }
    [ -f "$yadgar_shim" ] || { echo "python3"; return; }
    head -1 "$yadgar_shim" | sed 's|^#!||'
}
```

`_locate_install_assets()` now calls `venv_python=$(_get_venv_python)` and uses `"$venv_python" -c "..."` so `sys.prefix` resolves to the venv root where the wheel shipped its `share/yadgar/install_assets/` data.

Fallback: if the `yadgar` shim is absent (repo-checkout dev environment), the helper echoes `python3` — identical behaviour to before the fix.

### DRY refactor scope

`_resolve_yadgar_version` and `_resolve_backend_version` were **not** refactored to call `_get_venv_python`. Reason: `test_v5_46_12_backend_version_canonical.py::test_resolve_backend_version_uses_shim_pattern` extracts the function body and asserts the literal `"command -v yadgar"` is present — delegating to `_get_venv_python` removes that literal and breaks the v5.46.12 test. The fix scope is minimal: new helper + `_locate_install_assets` call site only.

### User migration

```bash
pipx upgrade yadgar
yadgar-setup
```

Step 9 now finds `CLAUDE.md` fragment via the venv python and appends it cleanly.

### Pending

- `yadgar --version` CLI flag deferred to v5.46.15.

---

## v5.46.13 — step 8 config init-if-missing fix (2026-06-06)

### Context

v5.46.12 fixed backend image versioning. v5.46.13 fixes the remaining fresh-install failure at step 8 on Rocky Linux (and any bare system without a pre-existing `~/.yadgar/config.yaml`):

```
Step 8/10: Syncing config...
Config file not found: /root/.yadgar/config.yaml. Run 'yadgar config init' to create it.
```

`yadgar config sync` increments an existing config file against the current Settings model. On a fresh install there is no file to increment against — it exits non-zero and setup aborts.

### What changed

**`scripts/install/yadgar-setup.sh` — `_step_config_sync()` (lines 463-472):**

```bash
_step_config_sync() {
    log "Step 8/10: Syncing config..."
    local yadgar_dir="${YADGAR_DIR:-${HOME}/.yadgar}"
    local config_file="${yadgar_dir}/config.yaml"
    if [ ! -f "$config_file" ]; then
        log "  config.yaml not found — running 'yadgar config init' first"
        run yadgar config init
    fi
    run yadgar config sync
}
```

- Fresh install: `init` creates default `config.yaml` with all fields commented, then `sync` adds any new fields from the current Settings model.
- Reinstall: `init` is skipped — `sync` only increments, preserving user edits.
- Data dir: `${YADGAR_DIR:-${HOME}/.yadgar}` — consistent with `_step_generate_units` and other setup.sh functions.

### User migration

```bash
pipx upgrade yadgar
yadgar-setup
```

Step 8 should now complete cleanly on fresh installs. No manual intervention required.

### Pending

- `yadgar --version` flag (v5.46.14).

---

## v5.46.12 — backend version canonical source fix (2026-06-06)

### Context

v5.46.11 fixed CLI invocation on fresh installs. v5.46.12 fixes the remaining step 2 failure on a fresh Rocky Linux VM: both `yadgar-setup.sh` and `Makefile` resolved the backend container image tag from the core (pip package) version instead of the independent backend image track.

Symptom on Rocky VM:
```
Step 2/10: Pulling container images...
  core=5.46.11  backend=5.46.11   ← WRONG: backend tag is 5.4.0
docker.io/openfantasy/yadgar:5.46.11   OK
docker.io/openfantasy/yadgar-backend:5.46.11   FAIL (tag does not exist)
```

The backend image track (`docker.io/openfantasy/yadgar-backend`) uses independent versioning — currently `5.4.0`. It is not bumped on every core release.

### What changed

**`yadgar/__init__.py`:**

```python
BACKEND_VERSION = "5.4.0"
```

Added immediately after the `__version__` try/except block. This is the single canonical source for the backend image version. Both `setup.sh` and `Makefile` derive the backend tag from here.

**`scripts/install/yadgar-setup.sh`:**

1. New `_resolve_backend_version()` function (parallels `_resolve_yadgar_version`):
   - Locates `yadgar` shim via `command -v yadgar`
   - Reads shebang to get venv python
   - Calls `$venv_python -c "import yadgar; print(yadgar.BACKEND_VERSION)"`
   - Fallback: `"5.4.0"` if shim absent or venv python unusable

2. `_step_pull_images` now resolves both versions:
   ```bash
   version=$(_resolve_yadgar_version)
   backend_version=$(_resolve_backend_version)
   ```
   Pull line: `yadgar-backend:${backend_version}` (was `${version}`).

3. `_step_generate_units` likewise — `YADGAR_BACKEND_IMAGE` now uses `${backend_version}` at both systemd + launchd sites.

**`Makefile`:**

```makefile
YADGAR_BACKEND_VERSION := $(shell grep -m1 '^BACKEND_VERSION' $(REPO_ROOT)yadgar/__init__.py | cut -d'"' -f2)
```

All 3 `yadgar-backend:$(YADGAR_VERSION)` → `yadgar-backend:$(YADGAR_BACKEND_VERSION)`.

### User action required

```bash
pipx upgrade yadgar
yadgar-setup
```

Step 2 should now pull `yadgar-backend:5.4.0` successfully.

### Bumping backend version (maintainer)

1. Edit `yadgar/__init__.py` — update `BACKEND_VERSION = "X.Y.Z"`.
2. Update `server.json` `backend_version` field (drift guard test enforces this).
3. Update nix module `yadger_backend_version` (manually, via release notes — nix is downstream consumer).
4. Rebuild + push `docker.io/openfantasy/yadgar-backend:X.Y.Z`.

### Drift guards added

- `pyproject.toml [project].version` must equal `server.json version` (file-to-file).
- `yadgar.BACKEND_VERSION` must equal `server.json backend_version`.

Both enforced by `test_v5_46_12_backend_version_canonical.py` (CI + pre-commit).

### Pending

- `yadgar --version` CLI flag still missing (deferred to v5.46.13). Current workaround: version detection uses shim-shebang extraction.

---

## v5.46.11 — pipx CLI invocation fix (2026-06-06)

### Context

v5.46.10 fixed the wheel bundle gap (helpers now ship correctly; steps 3–5 pass on
fresh Rocky Linux VM). v5.46.11 fixes the next failure: steps 6–10 invoked
`python3 -m yadgar <subcommand>` which resolves to system python (`/usr/bin/python3`
on Rocky Linux / bare Debian). System python has no `yadgar` package — it lives in
the pipx venv. Step 6 failed with:

```
==> Step 6/10: Installing Claude Code git hooks...
/usr/bin/python3: No module named yadgar
```

### What changed

**`scripts/install/yadgar-setup.sh`:**

1. **Steps 6/7/8/10 CLI calls** — `run python3 -m yadgar X` → `run yadgar X`.
   The `yadgar` shim at `~/.local/bin/yadgar` has shebang
   `#!/root/.local/share/pipx/venvs/yadgar/bin/python` — resolves to the
   correct venv python automatically via PATH.

2. **Version detection (steps 2/4)** — `python3 -c "import yadgar; print(yadgar.__version__)"`
   replaced with new `_resolve_yadgar_version()` helper. The helper:
   - Locates the `yadgar` shim via `command -v yadgar`
   - Reads its shebang (`head -1 | sed 's|^#!||'`) to get venv python path
   - Sanity-checks the path is executable
   - Calls `"$venv_python" -c "import yadgar; print(yadgar.__version__)"`
   - Falls back to `"latest"` if shim absent or venv python unusable

3. **Comment update** — `_locate_setup_scripts` docblock updated from
   `python3 -m yadgar CLI subcommands instead` to `yadgar CLI shim (pipx-aware)`.

### User action required

```bash
pipx upgrade yadgar    # upgrades to v5.46.11
yadgar-setup           # steps 6-10 now use yadgar shim correctly
```

### Residual concern

`yadgar` CLI has no `--version` flag (confirmed: argument parser doesn't
register it). Version detection uses the shim-shebang workaround above.
A proper `--version` flag should be added in v5.46.12 via `yadgar/__main__.py`.

### Deviations from v5.46.11 spec

- **`_SHIM_CALL_PATTERN` regex unused:** The `_SHIM_CALL_PATTERN` compiled
  regex in the test module is defined but not used in a parametrized assertion
  (individual tests use inline `re.compile`). Not a gap — all four subcommands
  are covered by `TestShimUsage` parametrize.
- **Comment-line exclusion in tests:** Tests skip pure comment lines (`#`-prefixed)
  when checking for forbidden invocations, to allow `_resolve_yadgar_version`'s
  docblock to reference the old form as context.

---

## v5.46.10 — pipx wheel bundle gap fix (2026-06-06)

### IMPACT: pipx users on v5.45.0–v5.46.9 broken on fresh hosts

Every PyPI release from v5.45.0 through v5.46.9 shipped a wheel where
`yadgar-setup` (`share/yadgar/scripts/yadgar-setup.sh`) is present but all
helper scripts it calls are **absent**. On a fresh host with no prior yadgar
checkout, running `yadgar-setup` aborted with a short unhelpful error instead
of completing setup.

Affected installs:
- `pipx install yadgar` or `pip install yadgar` on a fresh host, any version v5.45.0–v5.46.9
- Hosts that had yadgar previously installed from a repo checkout are NOT affected
  (helpers found via repo fallback path in `_locate_setup_scripts()`)

### What changed

**`pyproject.toml` (`[tool.hatch.build.targets.wheel.shared-data]`):**
Single-file mapping replaced with directory-wide mapping:
```toml
# Before (broken — only ships yadgar-setup.sh)
"scripts/install/yadgar-setup.sh" = "share/yadgar/scripts/yadgar-setup.sh"

# After (fixed — ships entire scripts/install/ recursively)
"scripts/install" = "share/yadgar/scripts"
```
This ships all helper scripts, systemd `.in` unit templates, and the
`launchd/` plist template subdirectory.

**`scripts/install/yadgar-setup.sh`:**
Added fail-fast bundle-integrity check at top of script (after shebang,
before flag parsing). On missing helper, exits code 2 (distinct from setup
failure exit code 1) with explicit error:
```
ERROR: yadgar-setup wheel bundle is incomplete — missing helper 'detect_runtime.sh'.
  This is a yadgar packaging bug (affects pipx installs before v5.46.10).
  Workarounds:
    1. Upgrade:       pipx upgrade yadgar   (requires yadgar >= v5.46.10)
    2. Repo checkout: git clone ... && make setup
    3. Report at:     https://codeberg.org/maxagahi/yadgar/issues
```

### User action required

**If you installed via `pipx` or `pip install` on a fresh host:**
```bash
pipx upgrade yadgar     # upgrades to v5.46.10+
yadgar-setup            # now works correctly
```

**If `pipx upgrade` is not yet available (PyPI upload pending):**
```bash
git clone https://codeberg.org/maxagahi/yadgar
cd yadgar
make setup
```

**Repo-checkout installs (`make setup`) are NOT affected** — the setup
script finds helpers via the local repo directory fallback path.

### Nix install path note

The `flake.nix` `postInstall` copies only `yadgar-setup.sh` to `$out/bin/yadgar-setup`
(helpers not adjacent). When invoked from `$out/bin/yadgar-setup`, the fail-fast
check will fire and exit 2. This is intentional and **better than silent partial
setup** — it surfaces the bundle gap with actionable instructions. A full nix formula
fix (copying helpers to `$out/libexec/yadgar/scripts/` or reading from the wheel's
`share/yadgar/scripts/`) is deferred to a future version.

Nix flake users: use `make setup` from repo checkout (unaffected path).

### Deviations from v5.46.10 spec

- **Step 3 (install_assets systemd/launchd subdir move):** No-op. Templates
  live in `scripts/install/` (systemd `.in` at root, launchd `.in` in
  `scripts/install/launchd/` subdir). `generate_systemd.sh` uses
  `${SCRIPT_DIR}/yadgar.service.in` and `generate_launchd.sh` uses
  `${SCRIPT_DIR}/launchd/...`. Directory-wide mapping ships both correctly
  with zero helper-script changes.
- **Fail-fast helper list excludes `uninstall.sh` and `restore.sh`:** These
  are standalone user entrypoints, not called by the setup flow. Bundle
  test still asserts they're present in the wheel.
- **Test sentinel mechanism:** Spec suggested a `YADGAR_TEST_FORCE_MISSING_HELPER`
  env var to force fail-path in tests; instead, tests copy only `yadgar-setup.sh`
  to a temp dir (no helpers present). Cleaner and doesn't require new env var.

---

## v5.46.6 — Circuit breaker clock fix, NLI spy wiring, SurrealDB install, carryover (2026-06-05)

### No user action required for upgrade

Users on v5.46.5 → v5.46.6: no configuration changes needed for standard deployments. One behavior change in `insert_memory` (see below).

### What changed

**yadgar/ml_client.py:** `RemoteMLClient` now passes `time_fn=self._now` to all three `_CircuitBreaker` constructors. This ensures the breaker's internal clock is the same clock as the client's `_now()` method, which tests can inject by setting `client._fake_now`. No runtime behavior change in production (both clocks return monotonic time from the same base).

**yadgar/storage/memory.py (BEHAVIOR CHANGE):** `insert_memory` now normalises `directory_context=''` (empty string) to `'global'` before writing to SurrealDB. Previously, an empty string was stored verbatim; SurrealDB 2.x embedded does not reliably evaluate `= ''` equality in queries, causing anchors with `directory_context=''` to silently fail to surface via the global bucket. After this change, any caller passing `directory_context=''` will see `'global'` stored. Callers that explicitly rely on reading back an empty string from this field must update to `'global'` as the canonical sentinel.

**yadgar/tests/test_write_time_contradiction.py:** `test_default_on_fires_detector` spy registration moved from `yadgar.curation.contradiction` to `yadgar.curation` (the package's bound name). No production change.

**pyproject.toml:** `surrealdb>=1.0.0` added to `[project.optional-dependencies].test`. This ensures `pytest` setups using `pip install -e ".[test]"` include the SurrealDB Python client, which is required for `StorageEngine` to function in tests. Previously, SurrealDB had to be installed separately.

**yadgar/tests/test_branch_schema_migration.py:** `_insert_bare_wiki_page` now supplies a `directory_context` value when simulating pre-v5 wiki pages. This is required by the migration_016 `wiki_page` schema constraint (active for all new SurrealDB sessions). The test still exercises branch-field migration (migration_004), not the directory_context constraint.

### For contributors

- When writing tests that call `update_active_work`, `checkpoint`, or `anchor` from non-git temporary directories, always pass `branch_hint='master'` to bypass branch-context pre-validation.
- When writing consolidation drainer tests that patch `_apply_inner`, include `_internal=True` in the enqueue payload so items are not DLQ'd before reaching the patched method.
- Empty-string `directory_context` is now silently normalised at write time — do not test for `directory_context == ''` after `insert_memory`; check for `'global'` instead.

---

## v5.46.2 — Runtime detection UX hotfix (2026-06-05)

### No user action required for upgrade

Users on v5.46.1 → v5.46.2: no configuration changes needed. Container images and MCP
protocol are unchanged. This is a UX-only fix for fresh installs on systems without podman.

### What changed

`yadgar-setup` and `make setup` no longer fail abruptly when no container runtime is found.
Instead, they print an OS-aware install command and (in interactive mode) offer to run it.

### New yadgar-setup flags

| Flag | Effect |
|------|--------|
| `--install-runtime` | Skip prompt; run podman install directly (yes-mode). |
| `--no-install-runtime` | Skip prompt; print install hint + exit 1 (no-mode). |

Existing `--noninteractive` behaviour unchanged (print hint + exit 1 when no runtime found).

### Fresh install on a system without podman

**Interactive (default):**
```
yadgar-setup
# ==> Step 1/10: Detecting runtime + OS...
# ERROR: No container runtime (podman or docker) found.
#     Install podman with:
#       sudo apt-get install -y podman
# Install podman now? [Y/n]
```
Answer `Y` (or press Enter) to install. `yadgar-setup` re-verifies and continues.

**Non-interactive (CI/automation):**
```bash
yadgar-setup --noninteractive
# Prints install command and exits 1. No prompt.
```

**Auto-install (unattended):**
```bash
yadgar-setup --install-runtime
# Runs: sudo apt-get install -y podman (or distro equivalent)
# Then continues setup.
```

### make install-runtime (new target)

```bash
make install-runtime
# Equivalent to yadgar-setup --install-runtime from a repo checkout.

INSTALL_NONINTERACTIVE=1 make install-runtime
# Non-interactive: print command + exit 1.
```

### macOS note

On macOS, the install command is `brew install podman`. After install, you must also run:
```bash
podman machine init && podman machine start
```
`yadgar-setup` prints this as follow-up guidance but does not execute it automatically
(state-dependent; deferred per PD-38 precedent).

---

## v5.46.1 — Distribution infrastructure prep (2026-06-05)

### No user action required for upgrade

Users on v5.46.0 → v5.46.1: no configuration changes needed. The container images
and MCP protocol are unchanged.

### Non-nix install path: `pipx install yadgar` from PyPI

As of v5.46.1 the primary non-nix install path is:

```bash
pipx install yadgar
yadgar-setup
```

This replaces the Homebrew lane, which was retired per PD-39 (2026-06-05).
Existing Homebrew installs continue to work; `brew tap maxagahi/yadgar` is
deprecated — no further formula updates will be published.

### Nix: pre-commit hook auto-syncs flake.nix (PD-40)

Cross-repo nix PR auto-open was retired per PD-40 (2026-06-05).
`NIX_BUMP_TOKEN` is no longer needed — remove it from Forgejo secrets.
The pre-commit hook (`scripts/sync_version.py`, committed @53de97a)
now auto-updates `flake.nix` version on every `pyproject.toml` bump commit.

Nix users continue to install via:
```bash
nix profile install codeberg:maxagahi/yadgar
```

### PyPI publish: CI on tag push (new Forgejo secret required)

A `publish-pypi` job is now active in `.forgejo/workflows/release.yaml`.
It triggers on tag push matching `v*.*.*` (not `workflow_dispatch`).

**Required secret (one-time setup):**

| Secret | Scope | Notes |
|--------|-------|-------|
| `PYPI_API_TOKEN` | Project-scoped to `yadgar` on pypi.org | Add in Forgejo Settings → Actions → Secrets |

Token rotation policy: rotate before expiry (PyPI default 365d); set a
90-day-before-expiry reminder. Revoke the bootstrap account-scoped token
(`op://Private/PyPI/api-token`) once the project-scoped token is verified.

### `scripts/bump_version.py` — version bump helper

New helper for bumping the pyproject.toml version before tagging a release:

```bash
python3 scripts/bump_version.py --bump patch      # 5.46.1 → 5.46.2
python3 scripts/bump_version.py --new 5.47.0      # explicit version
python3 scripts/bump_version.py --dry-run --bump minor   # preview only
python3 scripts/bump_version.py --current-version # print current version
```

Pre-commit hooks cascade the bump automatically to `server.json`, `flake.nix`,
and `uv.lock` — no manual editing of those files required.

---

## v5.46.0 — Distribution: pipx + Homebrew + Nix flake + SBOM + release automation (2026-06-05)

### New install paths

| Path | Command |
|------|---------|
| pipx | `pipx install yadgar && yadgar-setup` |
| Homebrew | `brew tap maxagahi/yadgar https://codeberg.org/maxagahi/homebrew-yadgar && brew install yadgar && yadgar-setup` |
| Nix flake | `nix profile install codeberg:maxagahi/yadgar && yadgar-setup` |
| Repo checkout | `git clone ... && make setup` (unchanged) |

`yadgar-setup` is a new standalone binary (not a `yadgar` CLI subcommand) that parallels `make setup`
for users without a repo checkout. Source: `scripts/install/yadgar-setup.sh`.
Entry point: `[project.scripts] yadgar-setup = yadgar.scripts.yadgar_setup:main`.

### Homebrew tap repo (user-action required)

Create the tap repo `homebrew-yadgar` on Codeberg manually:

1. Create `codeberg.org/maxagahi/homebrew-yadgar` as a public repository.
2. Add `Formula/yadgar.rb` (rendered from `Formula/yadgar.rb.in` in this repo).
3. Users tap via: `brew tap maxagahi/yadgar https://codeberg.org/maxagahi/homebrew-yadgar`

The release workflow (`release.yaml`) ships `open-brew-pr` + `open-nix-pr` jobs as `if: false` stubs.
v5.46.1 activates them once `BREW_BUMP_TOKEN` + `NIX_BUMP_TOKEN` Forgejo repo secrets are configured.

### Codeberg repo secrets (user-action, before v5.46.1)

Add these secrets to the `maxagahi/yadgar` Codeberg repo settings:

| Secret | Scope | Notes |
|--------|-------|-------|
| `FORGEJO_TOKEN` | release create + asset upload on `maxagahi/yadgar` | Used by attach-to-release job (v5.46.0 active) |
| `BREW_BUMP_TOKEN` | PR-create only on `maxagahi/homebrew-yadgar` | Used by open-brew-pr job (v5.46.1) |
| `NIX_BUMP_TOKEN` | PR-create only on `maxagahi/nix` | Used by open-nix-pr job (v5.46.1) |

`BREW_BUMP_TOKEN` and `NIX_BUMP_TOKEN` must be scoped to PR-create only. DO NOT grant push-to-main access.
Rotation policy: rotate on any personnel change or if token appears in CI logs.

### SBOM tooling

SBOM generation uses `cyclonedx-bom==7.3.0` (pinned exact version; alias package `cyclonedx-py` avoided).
Install: `pip install 'yadgar[sbom]'` or `pip install cyclonedx-bom==7.3.0`.
Generate: `bash scripts/generate_sbom.sh` → `dist/yadgar-<version>-sbom.cdx.json`.
Release workflow attaches SBOM + CHECKSUMS.txt to each Codeberg release automatically.

### License classifier fix

`pyproject.toml` classifier corrected from `License :: OSI Approved :: MIT License` to
`License :: OSI Approved :: Apache Software License`. The `LICENSE` file is Apache-2.0 —
this was a pre-existing metadata error. PyPI will show the correct license on next publish.

### pyproject.toml new optional dependencies

```toml
[project.optional-dependencies]
dist = ["cyclonedx-bom==7.3.0"]
sbom = ["cyclonedx-bom==7.3.0"]
```

### nix flake

`flake.nix` added at repo root. Channel: `nixos-unstable` (Python 3.14 not yet in stable nixpkgs).
`flake.lock` pins nixpkgs@331800de (2026-05-31).

NixOS users: the `yadgar-setup` script refuses on NixOS (linux-nixos guard). Use `nixosModules.default`
instead. The home-manager module migration from `~/git/nix/modules/home/yadgar.nix` is opt-in;
backward-compat shim is a user-action item documented in the module's own migration notes.

### Deferred verifications (same pattern as v5.45.1)

- macOS Homebrew smoke test: `brew install maxagahi/yadgar/yadgar` requires macOS host.
- `nix run codeberg:maxagahi/yadgar#yadgar -- --version` requires published flake registry entry.
- `yadgar-setup --dryrun` container integration test (parity with `make setup --dry-run`) deferred.

---

## v5.45.1 — macOS launchd plist generation + install (2026-06-04)

**PAPER-ONLY IMPLEMENTATION.** No macOS host was available at time of shipping. All code paths are implemented and cross-platform render/template tests pass on Linux. Runtime behavior (launchctl load/unload, plutil lint, podman-machine socket) is deferred. Fix-ups via hotfix once macOS host is accessible.

See `docs/reference/decisions.md` PD-38 for the formal deferral record.

### Who needs to act

**macOS users (when available):** Run `make setup` from the repo root. `detect_os.sh` returns `macos` and `generate_launchd.sh` is invoked automatically, writing plists to `~/Library/LaunchAgents/`.

**Linux users:** No change. `make setup` is unaffected; it routes to `generate_systemd.sh` as before.

### New macOS commands

```bash
make setup                 # Full install — routes to generate_launchd.sh on macOS
make enable-units-macos    # launchctl bootstrap gui/$UID (macOS 11+) or launchctl load -w
make uninstall             # launchctl unload + rm plists on macOS
make uninstall-purge       # uninstall + rm ~/.yadgar/ + ~/Library/Logs/yadgar/
```

### Post-ship verification probes (run on first macOS host access)

**These probes are REQUIRED to confirm paper implementation is correct. Run them in order.**

1. `yadgar install --non-interactive` → `launchctl list | grep com.openfantasy.yadgar` — both jobs listed as active.
2. `plutil -lint ~/Library/LaunchAgents/com.openfantasy.yadgar.plist` exits 0.
3. `plutil -lint ~/Library/LaunchAgents/com.openfantasy.yadgar-backend.plist` exits 0.
4. Kill the core container: `podman stop yadgar` → wait 30s → `launchctl list | grep com.openfantasy.yadgar` — job restarted (KeepAlive behavior).
5. `curl http://localhost:8765/health` responds after restart. `curl http://localhost:8765/metrics | grep yadgar_` returns results.

**Deferred verification items (paper-only gaps):**

- `plutil -lint` output: templates are XML-valid (cross-platform verified) but Apple-specific plist semantics (key ordering, type coercion) not tested.
- `launchctl bootstrap gui/$UID` vs `launchctl load -w` branch selection (macOS 11+ check): `sw_vers -productVersion` probe logic untested live.
- podman-machine port forwarding: `ProgramArguments` uses `-p 127.0.0.1:8765:8765` — verify this reaches the container through podman-machine's VM port forward on macOS (different from Linux direct socket).
- No `DOCKER_HOST` in plist `EnvironmentVariables`: podman-machine manages its own socket lookup. If podman commands fail, add `DOCKER_HOST=unix:///path/to/podman.sock` to the plist's `EnvironmentVariables` dict.
- `YADGAR_SECRETS_ENV_FILE` env passthrough in launchd: plist sets it as `EnvironmentVariables` key — verify the container runtime actually picks it up (launchd env injection differs from systemd `EnvironmentFile=`).

### Plist install locations

| File | Path |
|------|------|
| Core plist | `~/Library/LaunchAgents/com.openfantasy.yadgar.plist` |
| Backend plist | `~/Library/LaunchAgents/com.openfantasy.yadgar-backend.plist` |
| Core stdout log | `~/Library/Logs/yadgar/core.out.log` |
| Core stderr log | `~/Library/Logs/yadgar/core.err.log` |
| Backend stdout log | `~/Library/Logs/yadgar/backend.out.log` |
| Backend stderr log | `~/Library/Logs/yadgar/backend.err.log` |

### `YADGAR_TEST_OS_MARKER=macos` env var

Cross-platform testing hook. Set this to spoof macOS detection on Linux:

```bash
YADGAR_TEST_OS_MARKER=macos bash scripts/install/detect_os.sh   # → macos
YADGAR_TEST_OS_MARKER=macos make setup                          # dry-run macOS path
```

---

## v5.45.0 — Setup Foundation: make-canonical, multi-runtime, seed anchors (2026-06-04)

Core 5.44.0 → 5.45.0. Backend unchanged at 5.4.0. No DB migrations.

### Who needs to act

**New Linux installs (non-NixOS):** Run `make setup` from the repo root — this is now the canonical install entrypoint. It detects your container runtime (podman preferred, docker fallback), generates systemd user units, installs hooks + subagents, syncs config, appends CLAUDE.md rules, and seeds canonical anchors.

**NixOS users:** `make setup` will refuse on NixOS with a "use nix flake" message. Continue using the existing home-manager activation path. NixOS nix flake install ships in v5.46.0.

**Existing installs:** No breaking changes. `check_docker()` is still callable (backward-compat alias for `check_runtime()`). `make setup` is the canonical path.

**v5.45.0 deletes `scripts/setup.sh`.** Use `make setup`. Credentials prompt interactively unless `~/.yadgar/secrets.env` is already present. Image pull + systemd enable are now part of the `make setup` chain. For DB restore (formerly `setup.sh --db ... --archive ...`): use `YADGAR_RESTORE_DB=... YADGAR_RESTORE_ARCHIVE=... make restore`.

### New commands

```bash
make setup              # Full install (pull images → credentials → systemd units → enable → hooks → agents → config → rules → anchors)
make pull-images        # Pull yadgar core + backend images (version from server.json)
make bootstrap-secrets  # Generate ~/.yadgar/secrets.env interactively (idempotent)
make enable-units       # systemctl daemon-reload + enable --now yadgar.target
make restore            # Restore DB from .surql backup (set YADGAR_RESTORE_DB=... env var)
make uninstall          # Remove systemd units; preserve ~/.yadgar/ data
make uninstall-purge    # Remove systemd units AND ~/.yadgar/ data directory
make install-hooks      # Install Claude Code hooks only (daemon-independent)
make install-agents     # Install subagents to ~/.claude/agents/
make config-sync        # Sync yadgar config
make install-rules      # Append yadgar rules fragment to ~/.claude/CLAUDE.md (idempotent)
make seed-anchors       # Seed canonical anchor memories
make detect-runtime     # Print detected container runtime
make detect-os          # Print detected OS
```

### `yadgar seed --anchors <file>` (new CLI flag)

Seeds anchor entries from a YAML file into yadgar memory. Idempotent: content-hash dedup prevents duplicates on re-run.

```bash
yadgar seed --anchors install_assets/seeds/anchors.yaml
yadgar seed --anchors my-custom-anchors.yaml --dry-run
```

YAML format:
```yaml
anchors:
  - content: "Your anchor text here"
    tags: ["_anchor", "your-tag"]
```

### `check_docker()` → `check_runtime()` deprecation notice

`YadgarDaemon.check_docker()` is now an alias for `check_runtime()`. Both remain callable. `check_docker()` will be removed in v5.47.0. Update callers to use `check_runtime()`.

### `YADGAR_CONTAINER_RUNTIME` env override

Set `YADGAR_CONTAINER_RUNTIME=docker` (or `podman`) to bypass auto-detection. Useful in CI or when both runtimes are installed but you want to force a specific one.

---

## v5.44.0 — Subagent MCP wiring + automation extensions (2026-06-04)

Core 5.43.0 → 5.44.0. Backend unchanged at 5.4.0. No DB migrations.

### Who needs to act

**Non-nix users:** Run the new install script to get per-agent allowlists and SubagentStop hook registration:

```bash
yadgar install-subagents          # install agent templates to ~/.claude/agents/
yadgar install-hooks --scope global  # register SubagentStop hook (already does this)
```

### New commands

**`yadgar install-subagents`** — copies bundled agent `.md` templates from the yadgar package to `~/.claude/agents/`. Idempotent (safe to re-run). Options: `--dry-run`, `--force`, `--check`. Skips on NixOS (detected via `/etc/NIXOS` or `nixos-version`).

**`yadgar config sync`** — incrementally updates `~/.yadgar/config.yaml` with any new Settings fields added since the file was last written. Fixes the bug where `yadgar config init` is one-shot and new release knobs are invisible in existing configs. Options: `--check`, `--dry-run`, `--remove-unknown`.

```bash
yadgar config sync --check    # list missing keys, exit 1 if any
yadgar config sync            # add missing keys with defaults + comments
yadgar config sync --dry-run  # preview diff without writing
```

### Bundled agent templates

Five agent templates now ship with the yadgar package at `yadgar/install_assets/agents/`:

| File | Model | Yadgar tools |
|------|-------|--------------|
| `general-purpose.md` | inherit | recall, wiki_query, wiki_read, memorize, remember, anchor |
| `Explore.md` | haiku | none (Haiku, ToolSearch disabled) |
| `cavecrew-investigator.md` | sonnet | read-only: recall, wiki_query, wiki_read, wiki_list, restore |
| `cavecrew-builder.md` | sonnet | read + memorize |
| `cavecrew-reviewer.md` | sonnet | none (output is the review) |

### X1: agent_dispatch_prelude — auto-prefetch context (opt-in)

`agent_dispatch_prelude` gains optional params: `branch_hint`, `directory`, `subagent_type`, `include_context`. When `include_context=True`, the prelude includes an auto-fetched context block (recent memories + wiki pages) using `recall(directory, branch_hint)` + `wiki_query(directory, branch_hint)` (v5.43.0 signatures). Default `False` per DP-X1-1.

### X2: SubagentStop hook — structured directive parsing

`yadgar/hooks/subagent_stop.py` gains `_parse_directive()` which recognizes:

```markdown
## Yadgar Findings

- memorize: content="...", tags=["a","b"], context="..."
- wiki_add: title="...", content="...", category="...", tags=[], directory="...", branch_hint="..."
- anchor: content="...", reason="...", tier="conditional"
```

Malformed directives are skipped with a warning (lenient per DP-X2-2). `branch_hint` is now forwarded in the POST payload to the daemon (regression guard: v5.42.2 precedent — writer and checker must use the same branch).

### X3: Platform portability

`yadgar/platform_paths.py` resolves Claude Code config paths per OS:
- Linux: `~/.claude/`
- macOS: `~/Library/Application Support/Claude/`
- Windows: `%APPDATA%\Claude\`

No hardcoded `/home/max` paths in any module.

### Design points resolved (v5.44.0)

**DP-1:** general-purpose subagents may call `memorize` directly with `provenance_agent` set. Long-running carve-out documented in agent template.

**DP-2:** Explore + cavecrew-reviewer use `tools:` allowlist without `mcp__yadgar__*` (Haiku / no-write agents).

**DP-3:** optimistic concurrency for multi-agent writes; similarity gate deduplicates async.

**DP-X1-1:** `include_context=False` default — opt-in per caller.

**DP-X2-1:** SubagentStop hook is PRIMARY writer for non-long_running agents.

**DP-X4-1:** standalone `yadgar install-subagents` ships independently of v5.45.

---

## v5.43.0 — MCP schema discipline: caller-context enforcement (2026-06-04)

Core 5.42.6 → 5.43.0. Backend unchanged at 5.4.0.

### Design points resolved

**DP-1 (canonical caller-context mechanism):** `directory` is canonical and always non-NULL. `branch_hint` is secondary — used when `_detect_branch(directory)` returns None (container scenario). Resolution order: `_detect_branch(directory)` → `branch_hint` → `None` (canonical slot).

**DP-2 (wiki_approve branch inheritance):** `wiki_approve` inherits the draft's branch from the `wiki_draft.branch` column (stored since v5.42.3 migration 015). The approved `wiki_page` row carries the draft's branch. Legacy drafts (branch=NULL) write to the canonical NULL slot. The `wiki.add()` return dict now includes `branch` for downstream callers.

**DP-3 (Phase 2 enforcement — immediate):** Hard-reject from v5.43.0. No deprecation window. External callers that don't pass `directory` to write tools get `{"error": "missing_directory"}`. The `_internal=True` carve-out (migrations, drainer replay, consolidation `_try_store_action_summary`) is preserved.

### New parameters (non-breaking additions)

**`wiki_query`** gains two new optional parameters:

```python
wiki_query(
    query,
    tags=None,
    category=None,
    max_results=5,
    directory=None,          # NEW: scope results to caller dir + 'global'
    branch_hint=None,        # NEW: branch for §25 filter when daemon CWD unreliable
)
```

Without `directory`, all pages are returned with a WARNING (backward-compat). Without `branch_hint`, branch detected from `_detect_branch(directory or os.getcwd())`.

**`recall`** gains one new optional parameter:

```python
recall(
    query,
    max_results=5,
    min_heat=0.0,
    profile=None,
    stage_overrides=None,
    directory=None,          # existing
    branch_hint=None,        # NEW: branch when daemon-side detection returns None
)
```

Resolution order: `_detect_branch(directory or os.getcwd())` → `branch_hint` → `None`.

### Action required for external callers

None for read tools — `wiki_query` and `recall` remain permissive (warn-only when directory absent).

For write tools — the Phase 2 hard-reject established in v5.42.5 continues unchanged. If you're already passing `directory` to `wiki_add`, `block_*`, `agent_prompt_save`, you're compliant.

### _internal carve-out

Daemon-internal write paths (`_internal=True`) bypass directory enforcement:
- `QueueDrainer` replay path
- `_try_store_action_summary` (consolidation)
- Migration-time backfill writes
- Hook callback writes that predate the directory contract

These paths write to the canonical NULL slot and are exempt from the external caller requirement.

---

## v5.42.6 — directory backfill repair + resolution hole fix (2026-06-03)

Core 5.42.5 → 5.42.6. Backend unchanged at 5.4.0.

### Bug fixes (no manual action required)

**Bug 1 — migration 016 backfill missed all existing rows (field-absent IS NONE)**

SurrealDB 3.0.5 `WHERE directory_context IS NONE` matches only explicit-NULL rows, not field-absent rows (rows created before `DEFINE FIELD` ran). All ~200 pre-migration-016 rows had field-absent `directory_context`, so the backfill was silently skipped. Migration 018 re-backfills using a Python-side filter that catches both explicit-NULL and field-absent rows.

Migration 018 runs automatically on server start (no manual step required).

**Bug 2 — wiki_read resolution hole in container deployments**

Daemon-side `_detect_branch(os.getcwd())` returns None inside a container (no `.git` directory). With `_current_branch=None`, §25 step 1 was skipped and only canonical-slot (branch=NULL) rows were reachable. Any post-v5.42.3 write stored with `branch="master"` was unreachable via `wiki_read`.

Fix: `wiki_read` now accepts `branch_hint: str | None = None` (symmetric with `wiki_add`). Pass the known branch when calling from a container context:

```python
wiki_read(slug, directory="/abs/project/path", branch_hint="master")
```

**Bug 3 — wiki_update/wiki_append_section/wiki_restore coerce error on legacy rows**

SurrealDB ASSERT constraint fires on every UPDATE touching a row, even when the UPDATE sets the constrained field to a valid value — if the row was field-absent at update time, the coerce check failed before the SET was applied. Migration 018 temporarily relaxes the schema to `option<string>` before the backfill and re-tightens after.

### New operator knobs (I25 three-way registered)

| Env var | Default | Effect when `false` |
|---|---|---|
| `YADGAR_DIRECTORY_ENFORCEMENT` | `true` | drainer passes `wiki_add` records missing `directory_context` instead of routing to DLQ |
| `YADGAR_BRANCH_ENFORCEMENT` | `true` | drainer passes `wiki_add` / `memorize` records missing `branch` instead of routing to DLQ |

When either knob is off: WARN log emitted + `yadgar_writes_with_enforcement_relaxed{enforcement=...}` counter incremented. Default-on — existing behavior unchanged unless explicitly opted out.

Use these as migration escape hatches if legacy callers cannot be updated to supply `directory` or `branch` immediately.

---

## v5.42.5 — directory contract (2026-06-03)

Core 5.42.4 → 5.42.5. Backend unchanged at 5.4.0.

### Schema migration

Migration 016 runs automatically on server start. Steps:

1. Backfill `wiki_page.directory_context` for existing rows using tag heuristic:
   - tag `yadgar` → `/home/max/git/yadgar`
   - AWS tags (`aws`, `ecr`, `cloudfront`, etc.) → `/home/max/git/aws-work`
   - else → `"global"`
2. `DEFINE FIELD directory_context ON wiki_page TYPE string ASSERT $value != NONE AND string::len($value) > 0`
3. `DEFINE INDEX wiki_page_directory_context_idx ON wiki_page FIELDS directory_context`
4. Backfill `memory.directory_context` NULL rows → `"global"`
5. `DEFINE FIELD directory_context ON memory TYPE string ASSERT $value != NONE AND string::len($value) > 0`
6. `wiki_draft.directory_context` column added (nullable) for draft staging

No manual step required.

### Breaking change — write tools now require directory

`wiki_add`, `block_create`, `block_get`, `block_update`, `block_delete`, `block_replace`, `block_append` (scope='project'), and `agent_prompt_save` now hard-reject at the MCP boundary when `directory` is absent:

```json
{"error": "missing_directory", "stored": false, "field": "directory", "op_type": "<tool>"}
```

**Accepted values:** any non-empty string — absolute project path (e.g. `/home/max/git/myproject`) or the literal `"global"` for cross-project content. No disk-existence check is performed (DP-2).

**Normalization:** trailing slash stripped only (DP-3). No symlink resolution, no case folding.

**`is_draining()` carve-out:** drainer replay path and `_internal=True` writes bypass the directory check.

### Hook update requirement

SessionStart hooks and any integration that calls write tools must now pass `directory`:

```bash
# wiki_add example
wiki_add(title="...", content="...", branch_hint="$(git branch --show-current)", directory="$(git rev-parse --show-toplevel)")
```

For global/cross-project content use `directory="global"`.

### §25 4-step resolution (v5.42.5)

When `wiki_read`, `wiki_history`, etc. are called with `directory` and `branch_hint`:

1. `directory=$caller AND branch=$current` — project+branch-scoped page
2. `directory=$caller AND branch IS NULL` — project canonical page
3. `directory="global" AND branch IS NULL` — global fallback
4. Not found

Legacy calls without `directory` return all results + log WARNING (backward-compat mode; will tighten in v5.43+).

### DLQ `missing_directory` entries

`dlq_requeue` passes `missing_directory` entries through normally. To manually replay:

1. `dlq_inspect()` — find the entry id
2. Edit payload to add `directory_context: "<path>"`
3. `dlq_requeue(id)`

### DP-4 legacy branch="master" rows

Pre-v5.42.2 rows have `branch="master"` and `directory_context` backfilled to `"global"` or project path. These are still reachable via §25 step 2/3 when querying from the matching directory or `"global"`. No data loss.

## v5.42.3 — drainer branch enforcement + memory branch_hint parity (2026-06-03)

Core 5.42.2 → 5.42.3. Backend unchanged at 5.4.0.

### Schema migration

Migration 015 adds `wiki_draft.branch` column (nullable text). Runs automatically on server start. No manual step required.

### Breaking change — write tools now require branch context

All write tools (`memorize`, `anchor`, `checkpoint`, `update_active_work`, `wiki_add`) now hard-reject at the MCP boundary when branch context is absent:

```json
{"error": "missing_branch", "stored": false, "field": "branch_hint", "op_type": "<tool>"}
```

**Resolution order:** `_detect_branch(directory)` → `branch_hint` parameter → hard-reject.

**Impact:** Any caller that did not supply `branch_hint` and whose `directory` is not accessible to the yadgar daemon (e.g., containerized setups) will now receive an error. Fix: pass `branch_hint=<current-branch-name>` to affected tools.

**`is_draining()` carve-out:** drainer replay path is exempt. DLQ records that lack branch are rejected by `_validate_wiki_add` / `_validate_branch_context` before apply and routed to DLQ with `failure_reason=missing_branch`.

### DLQ `missing_branch` entries

`dlq_requeue` blocks entries with `failure_reason=missing_branch` without `force=True`. To manually replay a missing-branch DLQ entry:

1. `dlq_inspect()` — find the entry id
2. Edit payload to add `branch: "<branch-name>"`
3. `dlq_requeue(id, force=True)`

### New metric

`yadgar_dlq_rejection_count` (Gauge) — tracks DLQ rejection counts by `failure_reason` label.

## v5.42.2 — wiki branch-default scope mismatch fix (2026-06-02)

**Critical hotfix.** Core 5.42.1 → 5.42.2. Backend unchanged at 5.4.0. No schema migration, no data migration.

### The problem

Four prior fix attempts (v5.39–v5.42.1) targeted embedding gaps and backfill. The real bug was a
**branch-scope filter mismatch** discovered via live probe 2026-06-02:

- `wiki_check_duplicate(content, branch=None)`   → candidates: []     (bug)
- `wiki_check_duplicate(content, branch="master")` → candidates: [{sim: 0.9055, slug: ...}]  (works)

Root cause — writer asymmetry:
1. `_fill_wiki_add_defaults` in `yadgar/file_queue/dlq.py` hardcoded `branch="master"` when payload omits branch. Every wiki write via the drainer (the production path since v5.41.5) stored pages with `branch="master"`.
2. `wiki_check_duplicate` in `yadgar/server/tools/wiki.py` defaulted `branch=None` and passed it straight through to `find_similar_wiki_pages`, which built scope `{None}`.
3. `{None}` ∩ `{"master"}` = ∅ → zero candidates → gate silent.

### What changed

**Two single-line fixes:**

1. `yadgar/file_queue/dlq.py:133` — drainer now stores `branch=None` (not `"master"`) when branch is absent from payload. Matches `wiki_add` direct-write path behavior. Both writer paths agree on the canonical slot.

2. `yadgar/server/tools/wiki.py:695-720` — `wiki_check_duplicate` auto-detects `_get_default_branch(cwd)` when `branch` arg is `None`. Passes `_default_branch` to `find_similar_wiki_pages` so scope = `{None, default_branch}`. On a `master`-default repo this is `{None, "master"}`, catching both post-fix canonical pages and pre-fix legacy pages.

### No migration required

No schema changes. No data migration.

Pages written before this fix retain `branch="master"`. After the v5.42.2 `wiki_check_duplicate` fix, scope = `{None, "master"}` so those legacy pages remain visible to the gate. On a `main`-default repo, pre-fix `branch="master"` pages become invisible — this is tracked as a deferred item in v5.42.3 alongside the hardcoded-fallback cleanup.

### Breaking change

**Drainer no longer injects `branch="master"`.** Any external caller that sent wiki payloads through the drainer without an explicit `branch` field, and relied on the drainer to set `branch="master"`, must now pass `branch="master"` explicitly. **No callers in this codebase depend on this behavior.** This note is defensive documentation only.

### Verify after deploy

```python
# 1. Write a near-clone of an existing prod page (force=True bypasses gate)
wiki_add(title="test-branch-fix-probe", content=<near-clone of existing>, force=True)

# 2. Check duplicate without explicit branch
result = wiki_check_duplicate(title="test-branch-fix-probe", content=<same near-clone>)
assert len(result["candidates"]) >= 1  # gate is functional
```

---

## v5.42.1 — wiki_page embedding backfill + embed-failure surfacing (2026-06-02)

**Critical hotfix.** Core 5.42.0 → 5.42.1. Backend unchanged at 5.4.0.

### The problem

v5.39 added the wiki similarity gate (`find_similar_wiki_pages` + `_compute_embedding`).
v5.41.5 moved the gate to the drainer. v5.42.0 added DLQ tracking. All three releases
inherited a silent bug: `wiki_page` rows shipped pre-v5.39 have `embedding=NULL`.
SurrealDB KNN operator `<|fetch_k,40|>` silently excludes NULL rows → gate finds 0
candidates → always passes → never fires in production.

Two failure modes compounded:
1. Pre-v5.39 rows (~1.9k): `embedding=NULL` by construction (no migration to backfill).
2. New writes may also silently skip embedding: `_compute_embedding` catch-all swallowed
   all exceptions (debug log only), so a flaky embed service caused NULL embeddings on
   new pages too.

### What changed

**Migration 014 — wiki_page embedding backfill:**
- `_migration_014_wiki_page_embedding_backfill()` registers the schema version slot.
  The actual backfill runs via `WikiStore.backfill_null_embeddings()` in `init_engines()`,
  AFTER both StorageEngine and EmbeddingEngine are initialised (migrations run before
  embeddings are available, so the backfill is split from the schema migration).
- `backfill_null_embeddings()` is idempotent. Re-running finds 0 NULL rows and returns 0.
- Per-row exception handling: if the embed service is unavailable for a specific row,
  that row is skipped with a WARN log. Progress is preserved — rows that succeed are
  committed even if later rows fail.
- Post-backfill: CRITICAL log if any NULL-embedding rows remain (embed service unavailable
  → gate still degraded until next startup).

**Embed-failure surfacing:**
- `_compute_embedding` now emits WARN log + `yadgar_wiki_embedding_compute_failed_total`
  Prometheus counter (`reason=exception | returned_none`) instead of silent debug log.
- New knob `WIKI_EMBED_FAILURE_BLOCKS_WRITE: bool = False` (I25 three-way registered):
  - `False` (default): backward compat — WARN + counter, write proceeds with NULL embedding.
  - `True`: write fails with explicit `RuntimeError` when embed unavailable.

### Operator runbook

**At startup (automatic):**
1. `_migration_014_wiki_page_embedding_backfill` marks the schema version slot.
2. `backfill_null_embeddings()` encodes all NULL-embedding wiki_page rows.
   Duration: ~50-150ms/row × ~1.9k rows ≈ 1.5-5 min (one-time cost).
3. If embed service is unavailable → WARN per row + post-backfill CRITICAL log.
   Rows are retried at next startup (idempotent).

**Post-startup verification (optional but recommended):**
Run `V5_42_1_GATE_VERIFICATION.md` procedure to confirm gate fires on a real near-clone.

**Flip the block knob (later, optional):**
Once monitoring shows `yadgar_wiki_embedding_compute_failed_total` is consistently 0,
set `WIKI_EMBED_FAILURE_BLOCKS_WRITE=true` in config.yaml to enforce embedding-on-write
and surface any future embed service failures immediately.

### Cost estimate

- **Backfill duration:** ~50-150ms per row × ~1.9k rows = 1.5-5 min at startup.
  Daemon is fully functional during backfill (non-blocking).
- **Memory:** ~8MB for sentence-transformer model already loaded (no extra cost).
- **DB writes:** 1 `UPDATE wiki_page SET embedding=...` per row, no version rows created.

---

## v5.42.0 — DLQ-based async rejection tracking (2026-06-02)

Core 5.41.5 → 5.42.0. Backend unchanged at 5.4.0. **No DB migration required.**

### What changed

v5.41.5 moved the similarity gate to the drainer (I9 fix). `wait=False` callers lost
sync rejection signal. This release adds async rejection tracking via the existing DLQ
infrastructure: rejections land in DLQ with `failure_reason="duplicate_detected"`,
and `project_brief(mode="signals")` surfaces a `pending_rejections_count` signal at
the next Stop hook checkpoint.

### New tools / fields

**`dlq_inspect(filter=...)`** — extended:
```python
dlq_inspect()                   # all entries (default, unchanged)
dlq_inspect(filter="all")       # same as above
dlq_inspect(filter="rejections")  # only similarity gate rejections
dlq_inspect(filter="failures")    # only permanent_error entries
```
Result now includes `failure_reason` field on every entry.

**`dlq_dismiss(filename)`** — new power-gated tool:
```python
# Acknowledge and drop a DLQ rejection without retry.
dlq_dismiss("0001778139482800_<uuid>.json")
```

**`dlq_requeue` now blocks rejection entries:**
```python
result = dlq_requeue("0001778139482800_<uuid>.json")
# For duplicate_detected entries:
# {"requeued": False, "error": "rejection entry — cannot auto-requeue. Options: ..."}
```
Permanent error entries (`permanent_error`) still requeue normally.

**`project_brief(mode="signals")` new field:**
```python
result = project_brief(directory="/home/max/git/yadgar", mode="signals")
result["pending_rejections_count"]  # int — rejections for current directory
# If > 0, recommended_actions includes:
# {"action": "review_rejections", "suggested_call": "dlq_inspect(filter='rejections')"}
```

### v5.41.5 migration options — updated (Option 4 added)

Callers that relied on `wiki_add(wait=False)` returning sync rejection now have four options:

**Option 1: `wait=True` — synchronous but slow (~228ms p50)**
```python
result = wiki_add(title="...", content="...", wait=True)
if result.get("reason") == "duplicate_detected":
    candidates = result["candidates"]
```

**Option 2: accept async rejection (fire-and-forget, no feedback)**
```python
result = wiki_add(title="...", content="...", wait=False)
# Gate fires async; no DLQ in v5.41.5. (v5.42.0: lands in DLQ — use Option 4.)
```

**Option 3: `wiki_check_duplicate` pre-flight (unchanged)**
```python
check = wiki_check_duplicate(title="...", content="...")
if not check["candidates"]:
    wiki_add(title="...", content="...", wait=False)
```

**Option 4 (NEW, recommended for bulk callers): trust Stop hook + explicit poll**
```python
wiki_add(title="...", content="...", wait=False)
# ... later at session boundary / explicit poll:
rejections = dlq_inspect(filter="rejections")
for entry in rejections:
    # entry["failure_metadata"]["candidates"] has the duplicate candidates
    # resolve: wiki_add(force=True, ...) or wiki_delete + retry or dlq_dismiss
    pass
# Or: rely on Stop hook surfacing pending_rejections_count > 0 automatically.
```

---

## v5.41.5 — similarity gate moved to drainer (I9 fix) (2026-06-02)

Core 5.41.4 → 5.41.5. Backend unchanged at 5.4.0. **No DB migration required.**

### What changed

**v5.39** added a similarity gate to `wiki_add` that checked for near-duplicate pages
before writing. The gate (`find_similar_wiki_pages` = embed + KNN) ran on the MCP
request thread, costing p50=27ms — 5.4× over the 5ms I9 budget.

**v5.41.5** moves the gate to the drainer's pre-apply stage. The handler now returns
in <1ms. The gate fires asynchronously (or synchronously for `wait=True`).

### Breaking change: `wait=False` no longer returns sync rejection

**Before (v5.39–v5.41.4):**
```python
result = wiki_add(title="...", content="...", wait=False)
if result.get("reason") == "duplicate_detected":
    # Gate fired — page rejected.
    candidates = result["candidates"]
```

**After (v5.41.5+):**
```python
result = wiki_add(title="...", content="...", wait=False)
# result is now: {"stored": True, "queued": True, "similarity_check": "deferred", ...}
# Gate check is deferred — NO sync rejection on this path.
```

### Migration options

**Option 1 (recommended): switch to `wait=True` for sync rejection feedback.**
```python
result = wiki_add(title="...", content="...", wait=True)
if result.get("reason") == "duplicate_detected":
    # Gate fired in drainer — rejection is synchronous.
    candidates = result["candidates"]
elif result.get("committed"):
    # Success.
    pass
```
`wait=True` preserves the v5.39 sync rejection contract. Use this when you need
immediate feedback about duplicates before proceeding.

**Option 2: accept async rejection (fire-and-forget).**
```python
result = wiki_add(title="...", content="...", wait=False)
# result["similarity_check"] == "deferred" — gate runs in background.
# If gate fires, job is archived (not inserted). No DLQ entry.
# Prometheus counter yadgar_wiki_add_rejected_total{reason="duplicate_detected"} increments.
```
Use this for bulk writes where duplicate rejection is acceptable to observe later.

**Option 3: use `wiki_check_duplicate` before writing (unchanged).**
```python
check = wiki_check_duplicate(title="...", content="...")
if check["candidates"]:
    # Near-duplicate found — decide to force or skip.
    pass
else:
    wiki_add(title="...", content="...", wait=False)
```
`wiki_check_duplicate` is a dry-run tool and is unaffected by this change.

### Bypass flags still work (unchanged)

- `force=True`: bypasses gate in drainer (page written regardless of similarity).
- `replace_slug=<slug>`: overwrite semantics — gate skipped in drainer.
- `append=True`: update semantics — gate skipped in drainer.

### No external consumers

v5.39 shipped 2026-05-31 (2 days before this fix). No external deployments consume
the breaking shape. Internal callers (tests, dogfood hooks) updated in this patch.

---

## v5.41.4 — roadmap-update-lag signal + wiki_append_section convention (2026-06-02)

Core 5.41.3 → 5.41.4. Backend unchanged at 5.4.0. **No DB migration required.**

### New signal: `roadmap_update_lag_hours`

`project_brief(mode="signals")` now includes:

```json
{
  "roadmap_update_lag_hours": 14.7,
  "recommended_actions": [
    {
      "action": "update_roadmap",
      "reason": "master moved 14.7h ago; roadmap not updated since",
      "suggested_call": "wiki_append_section('yadgar-roadmap-future-improvements', ...)"
    }
  ]
}
```

Sentinel: `-1.0` if the roadmap wiki page is not found (no action emitted).

### Convention shift: wiki_append_section for ship entries

Old rule: "After EACH ship: read-modify-write the roadmap wiki."

New rule (v5.41.4+): use `wiki_append_section` for `Recently shipped` entries.
Reserve full RMW for restructures, table-row edits, or closing open items.

Template:

```python
wiki_append_section(
    slug="yadgar-roadmap-future-improvements",
    section_heading="Recently shipped",
    content="- **vX.Y.Z (YYYY-MM-DD):** summary. N/N tests.",
    position="start_of_section",
)
```

Full details in `docs/roadmap/workflow-roadmap-update.md`.

### CLAUDE.md note (out of scope for this release)

The workflow rule change is documented in the roadmap wiki and `docs/roadmap/workflow-roadmap-update.md`.
Global `~/.claude/CLAUDE.md` is nix-managed. To propagate the new convention there, update
`~/git/nix/modules/home/claude.nix` (or wherever CLAUDE.md content is sourced) separately.

### No operator action required

No schema change. No config change. No binary migration. Suite: 7 new tests (all green).

---

## v5.41.3 — MCP-handler perf test + I9 attribution correction (2026-06-02)

Core 5.41.2 → 5.41.3. Backend unchanged at 5.4.0. **No DB migration required.**

### Layer model clarification

| Layer | Path | Latency budget |
|---|---|---|
| MCP handler | `wiki_add(wait=False)` file enqueue | **I9 ≤5ms p50** — governs this layer only |
| File queue write | `Path.write_text(json)` | sub-ms; included in I9 scope |
| Queue worker | `QueueDrainer._apply()` | NOT I9; heavy work allowed (I2/I4) |
| Storage layer | `update_wiki_page()` (embedded SurrealKV) | NOT I9; ~89ms baseline |

The ~89ms storage-layer baseline is a queue-worker latency — it is **not** an I9
violation. I9 governs the MCP handler return time only (before the drainer touches
the DB). This was mis-attributed in v5.41.1 test docstrings; corrected in v5.41.3.

### New test: MCP handler I9 perf guard (xfail)

`yadgar/tests/test_wiki_mcp_handler_perf.py` measures `wiki_add(wait=False)`
handler latency directly (100 calls, real file queue write, no drainer).

Current baseline (measured 2026-06-02): **p50 ≈ 28–48ms** (5.8–9.6× over the
≤5ms I9 budget). The test is marked `xfail(strict=True)` — it fails on the
current codebase and keeps the suite green. When v5.41.5 fixes the handler cost,
the test will start passing and `strict=True` will signal that the marker can be
removed.

Root cause: similarity gate (`find_similar_wiki_pages` = embed + vector search)
runs on the request thread before enqueue. Moving it to the drainer or a
background check is the expected fix in v5.41.5.

### No operator action required

No schema change. No config change. No migration. Suite still passes (230 tests,
1 xfail).

---

## v5.41.2 — wiki_add wait flag for read-your-writes consistency (2026-06-02)

Core 5.41.1 → 5.41.2. Backend unchanged at 5.4.0. **No DB migration required.**

### What changed

`wiki_add`, `wiki_update`, `wiki_restore`, and `wiki_append_section` now accept
`wait: bool = False`. When `wait=True`:

- `wiki_add` bypasses the async file queue and writes directly to storage, returning
  `{"stored": true, "queued": false, "committed": true, ...}`. Callers can call
  `wiki_history(slug)` immediately after without a `sleep()`.
- `wiki_update`, `wiki_restore`, `wiki_append_section` are already synchronous —
  `wait=True` is a no-op accepted for API symmetry.

`FileQueue.enqueue()` return value changed from file path (string) to `job_id` (UUID
string). If your code captures the return value of `enqueue()`, update it. Callers that
ignore the return value are unaffected. `memorize` MCP tool return now includes
`queue_id` as a UUID string rather than a filename.

### New config knob (I25)

`WIKI_WRITE_WAIT_TIMEOUT_SECONDS` (default `5.0`): maximum seconds `wiki_add(wait=True)`
may block. Only applies to the opt-in wait path — the default async path is unaffected.
Set via env: `YADGAR_WIKI_WRITE_WAIT_TIMEOUT_SECONDS=10.0`.

**NOTE: v5.41.2 wait=True does not yet use this knob.** The wait=True path currently
bypasses the queue entirely (sync write) so no timeout is needed. The knob is registered
now (I25 compliance) for a future queue-wait implementation that may need it.

### Performance

- `wiki_add(wait=False)` (default): p50 latency unchanged (<50ms for queue enqueue).
  I9 budget applies as before.
- `wiki_add(wait=True)`: opt-in slow path, not bound by I9. Expect storage-write latency
  (~80-150ms for embedded SurrealKV). Do NOT use as default in agent prompts — reserve
  for callers that genuinely need immediate read-after-write consistency.

## v5.41.1 — Wiki versioning transactional atomicity hotfix (2026-06-02)

Core 5.41.0 → 5.41.1. Backend unchanged at 5.4.0. **No DB migration required.**

### What changed

`insert_wiki_page` and `update_wiki_page` now wrap the wiki_page mutation and
the wiki_page_version INSERT in a single `BEGIN TRANSACTION … COMMIT TRANSACTION`
compound statement. Either both rows land or both roll back.

In v5.41.0, the version INSERT was wrapped in `try/except` and swallowed on
failure — wiki_page could be mutated without a corresponding version row. This
created holes in the version chain (`wiki_history` returned non-contiguous
timelines; `wiki_restore` could not restore across a missing version).

### Failure-surface change — BREAKING for callers that swallowed version errors

In v5.41.0, a version INSERT failure was swallowed silently (debug log only).
In v5.41.1, version INSERT failure propagates as an exception, rolling back the
entire wiki_page write.

**Impact on callers:**
- `wiki_add` (MCP tool) — exception propagates to the caller response as
  `{"error": "..."}`. Clients that assumed `wiki_add` was always non-fatal now
  need to handle this error path. In practice, the version table is only at risk
  if the DB itself is in a bad state (I/O error, lock timeout, OOM), which should
  also prevent the wiki_page write from being useful anyway.
- `wiki_append_section` (MCP tool) — same as `wiki_add`.
- Direct `storage.update_wiki_page` callers — exception bubbles up unchanged.

### Gap note (low risk)

Any `wiki_page` mutation between v5.41.0 ship (2026-06-01 night) and v5.41.1
ship (2026-06-02) where the version INSERT failed silently will have a version
chain hole. There is no automated backfill — the window is narrow (≈1 day) and
the failure mode requires a DB-level write error on the version row specifically.
If you suspect a gap, run `wiki_history(slug)` and look for non-contiguous
version numbers.

### No schema change

Migration 013 (`wiki_page_version` table + indexes) is unchanged. No new
migration is needed.

---

## v5.41.0 — Wiki versioning + section-patching (2026-06-01)

Core 5.39.0 → 5.41.0. Backend unchanged at 5.4.0.
**DB migration required** — migration 013 runs automatically on daemon start.

### Schema migration

Migration `013_wiki_page_version` runs under `~/.yadgar/.migration.lock` on first server start after upgrade:

1. **DDL** — creates `wiki_page_version` SCHEMALESS table + three indexes.
2. **Seed** — inserts `version=1` row for every existing `wiki_page` row. Each seed row copies `title`, `content`, `category`, `tags`, `confidence`, `source_memory_ids`, `branch` with `change_summary="initial version"`. Pages that already have version rows are skipped (idempotent).

**Expected wall-clock on production (≈2,054 pages):** <30 s. Lock prevents parallel daemon starts from running migration twice.

**Safe-to-re-run:** `_migration_013_wiki_page_version` is idempotent — re-running after partial failure continues from where it left off (pages already seeded are skipped).

**Rollback:** The table and indexes are additive — no existing data is modified. Rolling back is `REMOVE TABLE wiki_page_version;` (manual, out-of-band). Version hook in `insert/update_wiki_page` would also need to be reverted (code-level rollback).

### Version retention policy

Every version row is kept forever. No garbage collection. Estimated storage growth: ~450 MB/year at current write rate (~5 wiki_updates/day across all pages). A `memory_stats`-style surface will be added in v5.42+ if needed.

### One-liner recovery (goal of this release)

```
wiki_restore(slug="yadgar-roadmap-future-improvements", version=15)
```

Compare with the 2026-05-31 incident recovery: 90 minutes of manual archive digging. After v5.41.0: seconds.

### New MCP tools

| Tool | Power | Notes |
|---|---|---|
| `wiki_history(slug, limit=20)` | No | Version list, no content field |
| `wiki_read_version(slug, version)` | No | Full snapshot |
| `wiki_diff(slug, v1, v2, fmt)` | No | unified or JSON format |
| `wiki_restore(slug, version)` | Yes | Creates new version N+1; bypasses sim gate |
| `wiki_append_section(slug, heading, content, position)` | Yes | Section-atomic write; secret-gated |

### async queue timing note

`wiki_add` uses an async file queue and returns before the write completes. `wiki_history(slug)` called immediately after `wiki_add` may show stale results until the queue drains. Use `wiki_read_version` after queue drain for consistency.

---

## v5.39.0 — Wiki similarity gate (2026-06-01)

Core 5.31.1 → 5.39.0. Backend unchanged at 5.4.0. **No DB migration.**

### Summary

`wiki_add()` now blocks near-duplicate page creation. If a page with cosine similarity ≥ 0.80 (combined title+content embedding) already exists, `wiki_add` returns an error dict instead of writing. Motivated by the 2026-05-30 incident where different slugs with near-identical content created corruption.

### Breaking change — `wiki_add()` rejection response

Callers that always treat `wiki_add()` as non-failing must now handle the new rejection path:

```python
result = wiki_add(title="...", content="...")
if result.get("stored") is False and result.get("reason") == "duplicate_detected":
    # Gate fired — page not written
    candidates = result["candidates"]  # list of {slug, title, similarity, branch}
    hint = result["hint"]              # "Use force=True to bypass, ..."
```

**Gate active by default in `"hard"` mode.** Soft mode (log + allow) available via `WIKI_SIM_MODE=soft`.

### Bypasses

| Scenario | Flag | Effect |
|---|---|---|
| Intentional duplicate | `force=True` | Gate skipped, page written unconditionally |
| Overwrite existing | `replace_slug="existing-slug"` | Treats as update, gate skipped |
| Appending to existing | `append=True` | Append path, gate skipped |
| Disable gate globally | `WIKI_SIM_GATE_ENABLED=0` | Gate never fires |

### New MCP tool — `wiki_check_duplicate`

Read-only probe. Call before `wiki_add` to check for duplicates without writing:

```python
result = wiki_check_duplicate(title="...", content="...", branch=None, threshold=0.80)
# result = {"candidates": [...], "threshold_used": 0.80}
```

### Config knobs (all I25-registered)

| Env var | Default | Description |
|---|---|---|
| `YADGAR_WIKI_SIM_GATE_ENABLED` | `true` | Master switch |
| `YADGAR_WIKI_SIM_CONTENT_THRESHOLD` | `0.80` | Cosine similarity threshold |
| `YADGAR_WIKI_SIM_MODE` | `hard` | `hard` (reject) or `soft` (warn + allow) |
| `YADGAR_WIKI_SIM_TOP_K` | `5` | Max candidates returned |
| `YADGAR_WIKI_SIM_TITLE_THRESHOLD` | `0.85` | Reserved — not active yet |

### Threshold calibration

Measured with all-MiniLM-L6-v2 on 2026-06-01:

| Pair | Similarity |
|---|---|
| Roadmap A vs B (near-duplicate) | 0.9560 |
| Arch vs Arch-paraphrase (near-duplicate) | 0.9931 |
| Arch vs Hooks (distinct) | 0.4396 |
| Arch vs Benchmark (distinct) | 0.4897 |
| Hooks vs Benchmark (distinct) | 0.4545 |
| Benchmark vs Config (distinct) | 0.4392 |
| Arch vs Config (distinct) | 0.7135 |

Min near-dup: 0.9560 | Max distinct: 0.7135 | **Separation margin: 0.2425**

Threshold 0.80 sits in the middle with ≥0.15 gap on each side.
## v5.37.0 — Viz integration testing infrastructure (2026-06-01)

Core `5.35.1 → 5.37.0`. Backend unchanged at `5.4.0`. **No DB migration.**

### What shipped

- **Layer 1 API contract tests** — 18 new pytest tests in `yadgar/tests/test_graph_api_contract.py`.
- **Layer 2 Playwright smoke tests** — 10 new pytest tests in `yadgar/tests/integration/viz/`.
- **Layer 3 JS unit tests** — 28 Vitest tests in `yadgar/static/viz_helpers.test.js`.
- **`viz-tests/` directory** at repo root with `package.json` + `vitest.config.js`.
- **`yadgar/static/viz_helpers.js`** — ES module with pure helper functions (extracted from `index.html`).
- **CI `viz-tests` job** added to `.forgejo/workflows/ci.yaml`.

### Consumer action required

**None for existing deployments.** Pure testing infrastructure — no runtime behavior change,
no API changes, no DB schema changes.

**For local dev with Layer 2 + 3:**

```bash
# Layer 2 (Playwright):
pip install -e ".[test,ml]"   # now pulls playwright + pytest-playwright
playwright install chromium   # or set PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH

# Layer 3 (Vitest):
cd viz-tests && npm ci
npx vitest run
```

**NixOS / system Chromium users:**

```bash
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(which chromium)
python -m pytest yadgar/tests/integration/viz/ -m integration
```

Layer 2 tests auto-skip if neither Playwright nor Chromium is available — non-blocking for
users who only need Layers 1 + 3.

See `docs/testing/viz-testing.md` for full local dev setup and failure interpretation.

---

## v5.35.1 — Memory block follow-ups (2026-06-01)

Core `5.35.0 → 5.35.1`. Backend unchanged at `5.4.0`. **No DB migration.**

### What shipped

- **I25 env knobs** — four `YADGAR_MEMORY_BLOCK_*` env vars now tunable without code changes.
- **`block_replace` + `block_append`** — two new MCP patch tools for incremental block edits.
- **block-reflect PostToolUse hook** — block contents re-injected into context after every block write call. Requires `install_hooks` re-run to pick up the new second PostToolUse entry.
- **SessionStart block injection** — `/hooks/session-context` now prepends blocks section.
- **`_MEMORY_UPDATABLE_FIELDS` fix** — `last_accessed` and `access_count` now updatable via `memory_update()`.
- **`yadgar/blocks_render.py`** — shared DRY render helper extracted from restoration engine.

### Consumer action required

**Re-run install_hooks** to pick up the block-reflect PostToolUse wiring:

```bash
# From within the project:
install_hooks(project_directory="/your/project/path")
# Or global scope:
install_hooks(scope="global")
```

The new PostToolUse entry (`block-reflect`) will not fire until `~/.claude/settings.json` (or project `.claude/settings.json`) is regenerated by `install_hooks`.

### New env knobs (all optional, have defaults)

```bash
YADGAR_MEMORY_BLOCK_MAX_PER_SCOPE=10       # max blocks per (scope, directory)
YADGAR_MEMORY_BLOCK_DEFAULT_CHAR_LIMIT=2000 # default char cap per block
YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT=8000    # hard max char cap (unoverridable per-block)
YADGAR_MEMORY_BLOCK_TOTAL_BUDGET_CHARS=12000 # total block budget at restore time
```

---

## v5.35.0 — JavaScript/TypeScript SDK `@yadgar/sdk` v0.1.0 (2026-06-01)

Core 5.31.1 → 5.35.0. Backend unchanged at 5.4.0. **No DB migration. No Python server changes.**

### What shipped

- `sdk-js/` subdirectory: self-contained Node/TypeScript package `@yadgar/sdk` v0.1.0.
- 53 MCP tool wrappers — all tools currently in `yadgar/server/tools/`.
- Streamable HTTP transport; bearer auth via `Authorization: Bearer <token>`.
- ESM + CJS + `.d.ts` build output (tsup).
- 73 vitest unit tests; in-process mock MCP server.
- CI workflow `.github/workflows/sdk-js.yml` (test + GitHub Packages publish).

### Consumer action required

**None** for existing Python / Claude Code consumers. Zero server-side changes.

### First publish (manual — NOT done in this PR)

```bash
cd sdk-js
npm run build
npm publish --no-git-checks
# Requires ~/.npmrc with GitHub Packages auth token
# Tag format: sdk-js/v0.1.0
```

### Install (once published to GitHub Packages)

```bash
# Add to ~/.npmrc:
# @yadgar:registry=https://npm.pkg.github.com
# //npm.pkg.github.com/:_authToken=YOUR_GITHUB_PAT
pnpm add @yadgar/sdk
```
## v5.33.0 — In-context memory blocks (2026-06-01)

Core `5.31.1 → 5.33.0`. Backend unchanged at `5.4.0`.

### Schema migration

**Migration 012** (`_migration_012_memory_block_table`) runs automatically on daemon start:

```sql
DEFINE TABLE IF NOT EXISTS memory_block SCHEMALESS;
DEFINE INDEX IF NOT EXISTS memory_block_name_scope_dir_idx
    ON memory_block FIELDS name, scope, directory;
DEFINE INDEX IF NOT EXISTS memory_block_scope_dir_idx
    ON memory_block FIELDS scope, directory;
```

Additive only — no existing tables or rows are modified. Safe to apply to v5.31.x databases.

### New MCP tools

Five new tools registered on the MCP server (`mcp__yadgar__block_*`):

| Tool | Purpose |
|---|---|
| `block_create(name, content, scope, char_limit, directory)` | Create a named block |
| `block_get(name, scope, directory)` | Fetch block content |
| `block_update(name, content, scope, directory)` | Full-replace content |
| `block_delete(name, scope, directory)` | Remove block (idempotent) |
| `block_list(scope, directory)` | List blocks for scope |

**Hard caps** (hardcoded in v5.33.0; env knobs promoted in v5.33.x):
- `MEMORY_BLOCK_MAX_PER_SCOPE = 10` — max blocks per (scope, directory)
- `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT = 2000` — default char cap
- `MEMORY_BLOCK_HARD_CHAR_LIMIT = 8000` — absolute max char_limit

### restore() change

`restore(directory)` now includes memory blocks in its `formatted` output under `## Memory Blocks`. If no blocks exist, the section is omitted (zero noise).

### bootstrap_project change

`bootstrap_project(directory, content)` now seeds two default blocks if they don't already exist:
- `current_task` (project scope, empty, char_limit=2000)
- `gotchas` (project scope, empty, char_limit=2000)

Re-running `bootstrap_project` does **not** overwrite existing block content.

### Known overlap with `_active_work`

`_active_work` (managed by `update_active_work`) and `current_task` block (managed by `block_update`) serve similar but distinct purposes. Both coexist in v5.33.0. Decision on canonicalization (keep both / migrate `_active_work` → block) deferred to v5.33.x.

### Deferred to v5.33.x

- **Env knobs**: `MEMORY_BLOCK_MAX_PER_SCOPE`, `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT`, `MEMORY_BLOCK_HARD_CHAR_LIMIT`, `MEMORY_BLOCK_TOTAL_BUDGET_CHARS` — three-way registration (I25) pending.
- **PostToolUse block-reflect hook** (`block-reflect.py`) — real-time re-injection on `block_*` writes.
- **SessionStart hook integration** — automatic injection at session start.
- **PreCompact hook** — block re-injection after `/compact`.
- **Patch semantics** (`block_replace` / `block_append`) — substring replace + append operations.

---

## v5.31.1 — Graph filter + MCP recall kwargs (2026-06-01)

Core 5.31.0 → 5.31.1. Backend unchanged at 5.4.0. **No DB migration.**

### Item 1 — `get_full_graph()` causal edge fix

No API change. Entity nodes now included in graph response. Existing callers unaffected; any clients that expected zero causal edges in the graph output (due to the bug) will now receive them correctly.

### Item 2 — MCP `recall()` new kwargs

**Opt-in only.** Existing callers with no `profile` argument behave identically to v5.31.0. New kwargs:

- `profile: str | None = None` — `"fast"` / `"balanced"` / `"full"` / `"debug"`. When set, routes through `Retriever.recall_via_pipeline()`.
- `stage_overrides: dict[str, dict] | None = None` — per-call stage disable map, e.g. `{"nli": {"enabled": false}}`.

Invalid `profile` raises `ValueError` immediately (before any DB access).

---

## v5.31.0 — Recall pipeline plugin architecture (2026-06-01)

Core 5.26.0 → 5.31.0. Backend unchanged at 5.4.0. **No DB migration.**

### Summary

Refactors the 8-stage recall pipeline (FTS + KNN + PPR + spreading + temporal → WRRF fusion → cross-encoder rerank → NLI → MMR diversity → adversarial detection → rules engine) into a plug-in architecture. Implements Refactor-R2 (ADOPT) from `docs/reports/audits/competitor-audit-2026-05-30.md`.

**Goal:** stages are A/B-testable and swappable. Foundation for ablation studies (D2 NLI on/off, D3 PC causal discovery) and future extract-on-ingest interplay (Adopt-7).

### Behavior impact

**None.** `recall()` is untouched. `recall_via_pipeline(profile="balanced")` produces bit-identical output. Verified by regression tests on 10-query corpus.

### New public API

```python
# New method on Retriever (profile-aware, pipeline-backed)
retriever.recall_via_pipeline(
    query="...",
    max_results=10,
    profile="balanced",          # "fast" | "balanced" | "full" | "debug"
    stage_overrides={"nli": False},  # per-call disable map
)

# A/B comparison harness
from yadgar.retrieval import recall_compare
result = recall_compare(retriever, "my query", profiles=["balanced", "balanced_no_nli"])
# result["profiles"]["balanced"]["results"] → list of memories
# result["profiles"]["balanced"]["stage_stats"] → {stage: {duration_ms, ...}}

# Profile validation
from yadgar.retrieval import get_profile
profile = get_profile("fast")   # raises ValueError on unknown profile
```

### New modules

| File | Purpose |
|---|---|
| `yadgar/retrieval/state.py` | `RetrievalState` dataclass — inter-stage carrier |
| `yadgar/retrieval/pipeline.py` | `RetrievalPipeline` orchestrator |
| `yadgar/retrieval/profiles.py` | Profile definitions (`fast`/`balanced`/`full`/`debug`) |
| `yadgar/retrieval/compare.py` | `recall_compare()` A/B harness |
| `yadgar/retrieval/stages/base.py` | `RetrievalStage` ABC |
| `yadgar/retrieval/stages/{fts,knn,ppr,spreading,temporal,fusion,ce_rerank,nli,mmr,adversarial,rules,query_analysis}.py` | Stage wrappers |

### New Prometheus metrics

| Metric | Type | Labels |
|---|---|---|
| `yadgar_recall_stage_duration_seconds` | Histogram | `stage`, `profile` |
| `yadgar_recall_stage_candidates_in` | Gauge | `stage`, `profile` |
| `yadgar_recall_stage_candidates_out` | Gauge | `stage`, `profile` |
| `yadgar_recall_profile_invocations_total` | Counter | `profile` |

### Version bumps

`pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock`: 5.26.0 → 5.31.0.

### Deferred to v5.31.x

- Extracting logic OUT of mixins into stage files (currently stages delegate to mixin methods)
- Async parallel stage execution (FTS + KNN concurrently)
- Per-stage model swapping (e.g. alternative CE model)
- External plugin loading (entrypoints)
## v5.29.0 — Bi-temporal edges extension (Adopt-3) (2026-06-01)

Core 5.27.0 → 5.29.0. v5.28.0 SKIPPED (even-minor reserved for hotfix patches per odd-only convention).

### Schema changes

Two new migrations run automatically on first daemon start. No manual action required.

**Migration 010 — `user_profile` bi-temporal** (`_migration_010_bitemporal_user_profile`):
- Adds `valid_from` (option<string>) and `valid_until` (option<string>) to `user_profile`.
- Backfills `valid_from = created_at` on existing rows.
- **Drops** the old unconditional UNIQUE index `profile_unique_idx`. Application-side uniqueness replaces it (SurrealDB v3.0.5 does not support `DEFINE INDEX ... WHERE`).

**Migration 011 — `derived_belief` bi-temporal** (`_migration_011_bitemporal_derived_belief`):
- Adds `valid_from` and `valid_until` to `derived_belief`.
- Backfills `valid_from = created_at` on existing rows.
- No index changes — `derived_belief` had no UNIQUE constraint.

### Behaviour changes

**`insert_profile` pivot to close-and-insert:**
Prior to v5.29.0, `insert_profile` was an UPSERT-in-place. From v5.29.0:
- When `attribute_value` changes OR `abs(new_confidence - old_confidence) >= PROFILE_BITEMPORAL_VERSION_DELTA` (default `0.05`): existing currently-valid row is closed (`valid_until = now()`), new row inserted with `valid_from = now()`.
- When change is below threshold (confidence noise): in-place update only — evidence merged, `updated_at` bumped. **No new row created.**
- This may increase `user_profile` row count over time. At typical rates (~5 profile facts/day per project) and 5-version average per key, expect ~5× row growth over a year. Negligible for typical yadgar deployments (<1 MB).

Env knob: `PROFILE_BITEMPORAL_VERSION_DELTA` (float, default `0.05`). Set to `0.0` to create a new row on every confidence change. Set to `1.0` to never supersede on confidence alone (only value changes trigger supersession).

**`insert_belief` gains `supersede=True` default:**
When inserting a belief for the same `(subject, belief_type, directory_context)`, prior currently-valid beliefs are closed before the new row is inserted. Pass `supersede=False` to opt out and allow competing co-existing beliefs (e.g. multiple hypotheses).

**Filtered read helpers now exclude invalidated rows by default:**
`search_profiles_fts`, `get_profiles_for_entity`, `search_beliefs_fts`, `get_beliefs_for_subject` all gain `include_invalidated: bool = False`. Default preserves current-state-only behaviour. Existing callers unaffected.

**New `as_of_filter` helper:**
`yadgar.storage.bitemporal.as_of_filter(table, as_of=None)` returns a SQL WHERE-fragment for point-in-time queries. Wired into `get_all_causal_edges(as_of=)` and `get_full_graph(as_of=)` — both default `None` = current state.

**MCP tool surface:** `recall` signature unchanged in v5.29.0 — `as_of` wiring to MCP is deferred to v5.31.x plugin architecture per open question §13.3.

### Operator recommendations

- **No manual steps** for existing deployments. Migrations run on first start.
- If you have very large `user_profile` tables (millions of rows), consider running `vacuum_now` after the first start to compact history. Set `VACUUM_USER_PROFILE_HISTORY_DAYS` (env knob — default `None` = keep all history) to prune older versions automatically.
- **Rollback:** downgrading to v5.27.x is safe — the new columns are nullable and old callers ignore them. The dropped UNIQUE index is NOT restored on downgrade (application-side enforcement is active).

### SurrealDB capability note

`DEFINE INDEX ... WHERE` (partial unique index) is **not supported** in SurrealDB v3.0.5. The plan §4 fallback was implemented: uniqueness on currently-valid rows is enforced application-side in `insert_profile`. This is documented in migration 010 comments and verified by test `test_user_profile_unique_constraint_scoped_to_current`.

---

## v5.27.0 — DuckDB analytics export (SHIPPED 2026-06-01)

Core 5.26.0 → 5.27.0. **No DB migration. No schema change.**

### New optional dependency

```bash
pip install yadgar[analytics]   # installs duckdb>=0.10,<2
```

Without this extra, the CLI entry-point `yadgar export duckdb` exits 2 with the install hint. All other yadgar commands are unaffected.

### Usage

```bash
yadgar export duckdb --output /path/to/snapshot.duckdb

# Flags (all optional)
# --include-secrets         forward-compat no-op; v5.10.2 gate is write-time
# --action-log-since 30d    time window for action_log (Nd/Nh/Nm or 'all')
# --action-log-limit 100000 hard row cap on action_log
# --no-views                skip the 10 analytics views
# --tables memory,wiki_page subset of tables to export
# --force                   overwrite existing output file
```

### Behavioral notes

- **Not a backup.** DuckDB export is analytics-only and lossy. Canonical backups remain `yadgar vacuum` (SurrealDB `.surql` export). Do not rely on DuckDB files for restore.
- **Snapshot semantics.** Re-run the export to pick up new SurrealDB writes.
- **Embeddings.** Stored as `FLOAT[<dim>]` native DuckDB arrays. Cosine similarity queryable via `list_cosine_similarity(a, b)`. DuckDB VSS extension is NOT auto-installed; opt-in via `INSTALL vss; LOAD vss;` if you want HNSW vector search.
- **secret_flag.** The v5.10.2 secret-gate blocks writes at write-time (no `secret_flag` column on memory rows). `--include-secrets` is a forward-compat stub for future row-level tagging schemas.

### Open questions (surfaced, not resolved)

1. **VSS bundling.** Recommend don't bundle; document user-side opt-in (see above).
2. **Parquet output format.** Deferred to v2 (`--format parquet` flag placeholder).
3. **Lossy-vs-backup confirmation.** Answered: DuckDB is analytics-only. `yadgar vacuum` owns canonical backups.
4. **Optional-dep extra name.** Chose `analytics` (reads best in `pip install yadgar[analytics]`).

---

## v5.26.0 — LongMemEval Sonnet 4.6 Full 500q (SHIPPED 2026-06-01)

Core 5.25.6 → 5.26.0. Backend unchanged at 5.4.0. **No DB migration.**

### Summary

Full 500-question Sonnet 4.6 LongMemEval-s benchmark. Closes Adopt-1 from competitor audit (2026-05-30).
Apples-to-apples comparison: mem0 (GPT-4o, 500q), Zep (GPT-4o, 500q), Yadgar (Sonnet 4.6, 500q).

**Phase 2 QA accuracy: 69.4% (347/500)** — beats Zep 63.8% by 5.6pp. 470 min wall-clock.

Per-type:
- single-session-assistant: 96.4% (54/56)
- single-session-user: 92.9% (65/70)
- knowledge-update: 75.6% (59/78)
- abstention: 80.0% (24/30)
- temporal-reasoning: 63.9% (85/133)
- multi-session: 55.6% (74/133)
- single-session-preference: 33.3% (10/30)

Phase 1 retrieval (embedded in full run, 500q natural distribution): MRR=0.928, Recall@10=0.906, NDCG@10=0.863.

D2 (NLI on/off) and D3 (causal graph signals) remain DEFER — single arm, no A/B.
See `docs/PLAN_V5_25_X_D2_NLI_AB.md` and `docs/PLAN_V5_25_X_D3_PC_AB.md`.

### Changes

- `benchmarks/run_longmemeval.py`: `--resume` flag + per-question JSONL incremental save + `--model` flag
- `benchmarks/results/longmemeval_v5.26.0_s_full.json`: final aggregated results (500 questions)
- `benchmarks/results/longmemeval_v5.26.0_s_full_hypotheses.jsonl`: per-question JSONL (500 lines)
- `benchmarks/results/longmemeval_v5.26.0_s_retrieval.json`: Phase 1 (96q stratified, reader-independent — kept for historical reference)
- `scripts/monitor_sonnet_run.sh`: progress monitoring
- `scripts/aggregate_sonnet_results.py`: JSONL → final JSON aggregation
- `docs/BENCHMARK_RESULTS.md`: replaced Haiku pilot table with Sonnet full-run numbers
- `docs/CHANGELOG.md`: v5.26.0 entry updated with Sonnet headline
- `docs/benchmarks-current.md`: updated status + per-release table
- `README.md`: benchmark section updated with Sonnet headline
- `docs/reference/decisions.md`: D2/D3 DEFER entries updated with post-Sonnet analysis

### To reproduce

```bash
uv run python benchmarks/run_longmemeval.py \
  --model claude-sonnet-4-6 \
  --output benchmarks/results/longmemeval_v5.26.0_s_full.json \
  --save-hypotheses benchmarks/results/longmemeval_v5.26.0_s_full_hypotheses.jsonl \
  --resume
```
Cost: zero cash (burns Max 20x usage quota). Wall-clock: ~470 min (28237s).

---

## v5.25.3 — Fast Profile Follow-up: instructions_loaded + viz_search (2026-05-31)

Core 5.25.2 → 5.25.3. Backend unchanged at 5.4.0. **No DB migration.**

### Summary

Micro-patch follow-up to v5.25.2 CPU burst hotfix. v5.51.0 plan rescope (ec33c92)
identified two more `retriever.recall()` call sites missing `profile="fast"`:

1. **`hook_instructions_loaded` (`http.py` ~line 953):** fires on every `session_start`
   + `compact` event — the highest-frequency burst path. Uncaught by v5.25.2.
2. **`api_viz_search` (`http.py` ~line 1394):** user-initiated viz graph search.
   Lower frequency than hooks but same rerank CPU cost per call.

Both are 1-line fixes. Same risk profile as v5.25.2 subagent_start fix. Completes
coverage of the three hooks fast-profile call sites (subagent_start v5.25.2;
instructions_loaded + viz_search v5.25.3). Broader fast-profile parameter tuning
continues in v5.51.0 plan.

### Changes

- `yadgar/server/http.py`: add `profile="fast"` to `hook_instructions_loaded` recall call
- `yadgar/server/http.py`: add `profile="fast"` to `api_viz_search` recall call
- `yadgar/tests/test_instructions_loaded_fast_profile.py`: new TDD tests (3 assertions)
- `yadgar/tests/test_viz_search_fast_profile.py`: new TDD tests (3 assertions)

---

## v5.25.2 — CPU Burst Hotfix: subagent_start fast profile + action-log poison-pill skip (2026-05-31)

Core 5.25.1 → 5.25.2. Backend unchanged at 5.4.0. **No DB migration.**

### Summary

Hotfix for two confirmed CPU burst root causes (HIGH confidence, 2-pass investigation).

### Root Cause 1: subagent_start ran full rerank pipeline on every dispatch

`/hooks/subagent-start` calls `retriever.recall()` inside `http.py`. The call was
missing `profile="fast"`, triggering the full CE/NLI/MP rerank pipeline. 100% of
subagent dispatch calls took 2.5-10s of CPU time. The sibling `/hooks/prompt-recall`
handler (~line 524) already used `profile="fast"` with an explicit comment warning
about 8-46s CPU bursts from the full pipeline. The same fix was never applied to
`hook_subagent_start`.

**Fix:** Added `profile="fast"` to the `retriever.recall()` call in
`yadgar/server/http.py` (was line 1043, now line 1048 after comment). One-line patch.
Matches the existing pattern and comment at the `prompt_recall` sibling.

### Root Cause 2: SecretLeakBlocked poison-pill blocked consolidation daemon

`_process_action_log()` in `yadgar/consolidation/cleanup.py` groups action-log rows
and calls `storage.insert_memory()` per group. When action-log content contains a
detected secret, `insert_memory()` raises `SecretLeakBlocked`. The exception exited
the group loop before `mark_actions_processed()` ran, so those action IDs were never
marked. Next cycle fetched the same 200 rows again, hit the same group, same
exception. Result: only 1 of N expected consolidation cycles completed in 5h10min.

**Fix:** Wrapped `insert_memory()` in a targeted try/except for `SecretLeakBlocked`.
On detection: logs WARNING (not CRITICAL) with directory + action_ids + reason,
increments `stats["actions_quarantined"]`, writes a quarantine entry to
`~/.yadgar/quarantine/action_log_poison.jsonl` (best-effort, disk errors swallowed),
then falls through to `mark_actions_processed()` so the poisoned group never
re-queues. Non-SecretLeakBlocked insert errors still re-raise.

Quarantine entry format (JSONL): `{timestamp, action_ids, reason, directory}`

Note: the quarantine logs the group's action IDs. The specific content row that
triggered the gate is not isolable at this call site — the gate fires on the
aggregated summary string built from all actions in the group. Isolating the
specific triggering row would require per-action gate checks, out of scope for
this hotfix.

### v5.51.0 rescope needed

The v5.51.0 plan was mistargeted at `/api/stats` (0.6% sustained CPU — not a burst
source). The bursts identified here come from `/hooks/*` endpoints. v5.51.0 needs
rescope to target the hooks pipeline and tuning of the `profile="fast"` retrieval
parameters.

### Changes

- `yadgar/server/http.py`: add `profile="fast"` to `hook_subagent_start` recall call
- `yadgar/consolidation/cleanup.py`: add `_quarantine_action_group()` helper + poison-pill
  catch in `_process_action_log()`, add `actions_quarantined` to stats dict
- `yadgar/tests/test_subagent_start_fast_profile.py`: new TDD tests (3 assertions)
- `yadgar/tests/test_action_log_poison_pill.py`: new TDD tests (6 assertions)

---

## v5.25.1 — Benchmark Phase 1: spawn surreal-server subprocess (2026-05-31)

Core 5.25.0 → 5.25.1. Backend unchanged at 5.4.0. **No DB migration.**

### Summary

Patch that unblocks Phase 1 retrieval benchmark execution. Root cause: embedded
`surrealkv://` does not support `FULLTEXT ANALYZER` SQL syntax, so all retrieval
metrics were zero in v5.25.0 (benchmark ran but FTS index was missing).

Fix: adopt the test-fixture pattern from `yadgar/tests/conftest.py`. Benchmark
now spawns a `surreal start` subprocess on a random port, sets `YADGAR_DB_URL`
so `StorageEngine` uses HTTP server mode (which has FULLTEXT support), and tears
down the process on exit.

Changes:
- `yadgar/_surreal_runner.py` (new): shared spawn/teardown/port-allocation
  helpers extracted from `yadgar/tests/_surreal_helpers.py`
- `yadgar/tests/_surreal_helpers.py`: converted to re-export shim — no import
  breakage in existing test code
- `benchmarks/run_longmemeval.py`: `spawn_surreal_for_benchmark()`,
  `wipe_benchmark_tables()`, and server-mode lifecycle wired into `run_benchmark()`
- `yadgar/tests/test_benchmark_phase1.py`: 4 new tests (shared runner import,
  shim re-export, YADGAR_DB_URL override, wipe callable)

### Per-question isolation in server mode

In server mode `StorageEngine` shares the `yadgar/main` namespace. `wipe_benchmark_tables()`
issues `DELETE <table>;` on all data tables between questions to prevent cross-contamination.
Schema (DEFINE TABLE/INDEX/ANALYZER) is not wiped — it's idempotent and recreating it each
question would add significant wall-clock overhead.

### Secret-gate disabled for benchmark ingestion

The LongMemEval corpus contains technical content (code snippets, Vulkan API names,
API-key-shaped strings) that triggers false positives in the storage-level secret
gate. The benchmark sets `YADGAR_SECRET_GATE_DISABLED=1` for the duration of the
run and restores the prior value on exit. This is the intended escape hatch for
fixed test corpora — same env var the production kill switch uses.

### Per-question error resilience

Each question runs inside a `try/except` so a single failing question records an
`error` field in its `per_query` entry and the run continues to completion.
Aggregation already skips entries without retrieval metrics, so error-only rows
do not perturb the per-type means.

### Phase 1 benchmark — run command (now works without pre-existing server)

```bash
uv run python benchmarks/run_longmemeval.py --retrieval-only \
  --output benchmarks/results/longmemeval_v5.25.1_s_retrieval.json
```

No `surreal` server needed — the benchmark spawns its own. To use an existing server:
```bash
YADGAR_DB_URL=http://127.0.0.1:8000 YADGAR_ALLOW_ROOT=1 \
  uv run python benchmarks/run_longmemeval.py --retrieval-only
```

### Phase 1 gate condition (unchanged)

`mrr > 0.1` AND `recall@10 > 0.3` must hold before v5.26.0 QA run starts.
Results in `benchmarks/results/longmemeval_v5.25.1_s_retrieval.json`.

### Phase 1 numbers — deferred

v5.25.1 ships infrastructure only. The Phase 1 benchmark run exceeded 2+ hours
wall-clock (295% CPU) without completing, blocking this deploy. Numbers will land
in v5.25.2 (dedicated benchmark patch) or as part of v5.26.0 execution.

`docs/BENCHMARK_RESULTS.md` remains PENDING.

---

## v5.25.0 — Benchmark Phase 1: retrieval infra + reproducibility metadata (2026-05-31)

Core 5.24.2 → 5.25.0. Backend unchanged at 5.4.0. **No DB migration.**

### Summary

Phase 1 of the LongMemEval benchmark infrastructure. Zero API spend. Ships:
- LongMemEval dataset download + sha256 pin (`LONGMEMEVAL_S_SHA256`)
- Reproducibility metadata in output JSON (`reproducibility` key)
- License attribution docs: `docs/BENCHMARK_LICENSE.md`, `docs/BENCHMARK_RESULTS.md`
- `benchmarks/README.md` LongMemEval citation fixed (correct HuggingFace URL)
- Tests: `yadgar/tests/test_benchmark_phase1.py` (12 tests)

Phase 2 QA accuracy (headline number vs mem0/Zep) ships in v5.26.0.

### Phase 1 retrieval run — requires live SurrealDB

The embedded SurrealDB path (`surrealkv://...`) does NOT support `FULLTEXT ANALYZER` syntax
(pre-existing upstream limitation, confirmed on master before v5.25.0).

**Phase 1 run requires a live SurrealDB server.** After deployment:

```bash
# 1. Ensure SurrealDB server is running (see: yadgar daemon install-service)
# 2. Run Phase 1 retrieval-only benchmark:
.venv/bin/python -m benchmarks.run_longmemeval \
  --variant s \
  --retrieval-only \
  --output benchmarks/results/longmemeval_v5.25.0_s_retrieval.json
```

Expected wall-clock: 30–120 min for 500 questions (CPU only, no GPU needed).
No LLM calls. No API spend.

### Phase 1 gate condition

Before v5.26.0 QA run, verify:
- `mrr > 0.1` AND `recall@10 > 0.3` in the aggregated results

If either condition fails, investigate `make_benchmark_settings()` config before any LLM budget burn.

### Dataset

Dataset downloaded to `benchmarks/data/longmemeval/longmemeval_s_cleaned.json` (264.5 MB, MIT).
SHA-256 pinned in `LONGMEMEVAL_S_SHA256` constant. Dataset NOT committed to repo.

### Reproducibility metadata

`run_benchmark()` output JSON now includes a `reproducibility` block:
```json
{
  "reproducibility": {
    "yadgar_commit": "<40-char git sha>",
    "dataset_sha256": "<64-char sha256 of longmemeval_s_cleaned.json>",
    "embedding_model": "all-MiniLM-L6-v2",
    "reader_llm": null,
    "judge_llm": null,
    "python_version": "...",
    "run_date_utc": "..."
  }
}
```
`reader_llm` and `judge_llm` remain `null` for Phase 1. v5.26.0 fills them in.

### What NOT to do

- Do NOT run Phase 2 (`python -m benchmarks.run_longmemeval --variant s` without `--retrieval-only`) until Phase 1 gate passes. Phase 2 spends LLM budget.
- Do NOT commit the dataset file to the repo.

---

## v5.24.2 — Bookmarks hotfix: marked v15 renderer.text round-trip crash (2026-05-30)

Core 5.24.1 → 5.24.2. Backend unchanged at 5.4.0. **No DB migration.**

### Bug: `Cannot use 'in' operator to search for 'tokens' in <string>` (bookmarks crash on any wiki page)

**Root cause:** v5.24.1 fixed `renderer.text()` to extract `token.text` from the marked v15 token
object, but then called `_origText(replaced)` — passing the resulting HTML string back to v15's
default `text` renderer, which does `'tokens' in arg` internally. When `arg` is a string (not a
token object), JavaScript throws "Cannot use 'in' operator to search for 'tokens' in <string>".
This fires on any wiki page whose heading or body contains inline text (i.e. essentially all pages).

**Fix:** `yadgar/static/bookmarks.js` — drop the `_origText` delegation entirely. The custom
`renderer.text` handler now returns the replaced HTML string directly. DOMPurify downstream already
sanitizes XSS; the default text renderer's HTML-escaping pass is not needed (and would have escaped
the `<a>` anchor we just built anyway).

**Vendored marked.js:** unchanged — still v15.0.12 (`yadgar/static/lib/marked.min.js`). No SRI hash
change (bookmarks.html loads marked via plain `<script src>` with no `integrity=` attribute).

### Regression test added

`yadgar/tests/test_viz_bookmarks_static.py::TestMarkedV15RendererRegression` — two node subprocess
tests that call `marked.parse()` on a heading with parenthetical text and assert no throw.

### Manual smoke test

After `nix-rebuild` / container restart, open `/bookmarks.html`, add any wiki page as a bookmark,
click it. Page should render without the "Error loading" banner. Specifically test the
`yadgar-roadmap-amp-future-improvements` or any page with `(date)` in a heading.

---

## v5.24.1 — Bookmarks hotfix: marked.js text guard + slug HTML entity normalisation (2026-05-30)

Core 5.24.0 → 5.24.1. Backend unchanged at 5.4.0. **No DB migration.**

### Bug 1 (PRIMARY — feature blocker): `text.replace is not a function` in bookmarks.html

**Root cause:** `marked` v15 changed the renderer API — `renderer.text()` now receives a token
object `{type:"text", text:"..."}` instead of a raw string. `bookmarks.js` called `text.replace(...)`
directly on the token object, throwing the reported error whenever a wiki page was loaded.

**Fix:** `yadgar/static/bookmarks.js` — the `renderer.text` handler now extracts the string from
the token object (`token.text` when typeof token === "object") before calling `.replace()`. A
companion guard in `_renderMarkdown` coerces any non-string `content` to `""` and logs a console
warning before calling `marked.parse()`.

### Bug 2 (SECONDARY — slug drift): `&amp;` entity in title creates duplicate slug

**Root cause:** Some code paths (e.g. `repo_wiki` skill) pass HTML-escaped titles such as
`"Yadgar Roadmap &amp; Future Improvements"` to `wiki_add`. `_slugify` did not unescape HTML
entities before generating the slug, so `&amp;` → `amp` token → `yadgar-roadmap-amp-...` while
the canonical page had slug `yadgar-roadmap-...`.

**Fix:** `yadgar/wiki.py::WikiStore._slugify` — calls `html.unescape(title)` before the regex
substitution, so `&amp;`, `&lt;`, `&gt;`, etc. collapse to their raw characters (then stripped by
the non-alphanumeric regex). Both code paths now produce identical slugs.

**Note:** The duplicate page `yadgar-roadmap-amp-future-improvements` already exists in your DB.
Merge or delete it manually via:
```
mcp_yadgar_wiki_delete(slug="yadgar-roadmap-amp-future-improvements")
```

### Smoke verification

1. Open `http://localhost:42069/bookmarks.html`
2. Select any bookmarked wiki page — content should render without error.
3. `wiki_add` a page with `&amp;` in the title — confirm slug does not contain `amp`.

---

## v5.24.0 — Wiki Bookmarks frontend: bookmarks.html + JS renderer + add modal (2026-05-30)

Core 5.23.0 → 5.24.0. Backend unchanged at 5.4.0. **No DB migration — frontend only.**
Completes the Wiki Bookmarks feature started in v5.23.0 (backend).

### New static files

| File | Size | Purpose |
|---|---|---|
| `yadgar/static/bookmarks.html` | ~4 KB | Bookmarks page UI |
| `yadgar/static/bookmarks.css` | ~6 KB | Dark-theme styles matching index.html |
| `yadgar/static/bookmarks.js` | ~10 KB | Fetch logic, markdown render, drag-to-reorder, add modal |
| `yadgar/static/lib/marked.min.js` | ~39 KB | Markdown → HTML renderer (vendored) |
| `yadgar/static/lib/highlight.min.js` | ~127 KB | Syntax highlighting (vendored) |
| `yadgar/static/lib/dompurify.min.js` | ~22 KB | XSS sanitization of rendered HTML (vendored) |
| `yadgar/static/lib/github-dark.css` | ~1.3 KB | highlight.js GitHub-dark theme (vendored) |

### Vendored library versions + SRI

| Library | Version | Source | SRI (sha384) |
|---|---|---|---|
| marked | 15.0.12 | jsdelivr/npm | `sha384-948ahk4ZmxYVYOc+rxN1H2gM1EJ2Duhp7uHtZ4WSLkV4Vtx5MUqnV+l7u9B+jFv+` |
| highlight.js | 11.11.1 | @highlightjs/cdn-assets | `sha384-RH2xi4eIQ/gjtbs9fUXM68sLSi99C7ZWBRX1vDrVv6GQXRibxXLbwO2NGZB74MbU` |
| DOMPurify | 3.2.6 | jsdelivr/npm | `sha384-JEyTNhjM6R1ElGoJns4U2Ln4ofPcqzSsynQkmEc/KGy6336qAZl70tDLufbkla+3` |
| github-dark.css | 11.11.1 | @highlightjs/cdn-assets | `sha384-wH75j6z1lH97ZOpMOInqhgKzFkAInZPPSPlZpYKYTOqsaizPvhQZmAtLcPKXpLyH` |

Note: Libraries are vendored under `yadgar/static/lib/` — no CDN dependency at runtime. CSP-safe.

### New daemon route

`GET /static/bookmarks.html` — served directly from daemon (port 8765) via `FileResponse`.
The viz server (port 42069) also serves all static files by path (including `bookmarks.css`, `bookmarks.js`, `lib/*`).

### Accessing the bookmarks page

- Via viz server (port 42069): `http://localhost:42069/bookmarks.html`
- Direct from daemon (port 8765): `http://localhost:8765/static/bookmarks.html`
- Via nav link in the graph view (`📑 Bookmarks` button in top bar)

### Manual smoke test

After deploying v5.24.0:

```bash
# From host (adjust port if different)
curl -sf http://localhost:42069/bookmarks.html | grep -c "bookmark-list"   # → 1
curl -sf http://localhost:42069/bookmarks.css | grep -c "#sidebar"         # → 1
curl -sf http://localhost:42069/bookmarks.js | grep -c "loadBookmarks"     # → 1
curl -sf http://localhost:42069/lib/marked.min.js | wc -c                  # → ~39903
curl -sf http://localhost:42069/lib/highlight.min.js | wc -c               # → ~127496
curl -sf http://localhost:42069/lib/dompurify.min.js | wc -c               # → ~22305

# Or via daemon port
curl -sf http://localhost:8765/static/bookmarks.html | grep -c "DOCTYPE"   # → 1
```

### Interactive smoke (manual, cannot be automated without browser)

Per PD-27, Playwright tests deferred. Manual steps:
1. Open `http://localhost:42069/bookmarks.html`
2. Click `+ Add bookmark` → modal opens with two modes
3. Switch to "Search semantically" → type "roadmap" → results appear within 500ms
4. Click first result → slug fills in slug field
5. Click `Add` → bookmark appears in left sidebar
6. Click bookmark → markdown renders in right pane with syntax-highlighted code blocks
7. Drag bookmark to reorder → position persists on reload
8. Click `↺` (row refresh) → content reloads

### v5.24.0: no JS test infra — relies on manual smoke test per PD-27

No Jest/Vitest/Playwright infrastructure in this repo. Static-asset tests in
`yadgar/tests/test_viz_bookmarks_static.py` verify file presence, structure, and
viz_server static file serving (including path-traversal guard). Interactive browser
tests deferred per PD-27 plan note.

---

## v5.23.0 — Wiki Bookmarks backend: storage + 4 MCP tools + HTTP proxy routes (2026-05-30)

Core 5.21.0 → 5.23.0. Backend unchanged at 5.4.0. **New DB migration: `wiki_bookmark` table.**
v5.22.0 slot reserved for hotfix (skipped per skip-1 convention).
Frontend (bookmarks.html UI) follows in v5.24.0.

### New table: `wiki_bookmark`

Migration `009_wiki_bookmark_table` adds the `wiki_bookmark` SCHEMALESS table with:
- `slug` — wiki page slug (UNIQUE index `wiki_bookmark_slug_idx`)
- `label_override` — optional user display name (None → frontend uses wiki title)
- `position` — 0-based dense integer for ordering
- `added_at` — creation timestamp

**Migration is additive only.** Existing data (memories, wiki pages, anchors) unaffected.

SurrealDB server mode: migration runs automatically on daemon startup via `_run_migrations_locked`.
Embedded mode: table created by `_init_schema` on startup.

### 4 new MCP tools

| Tool | Purpose |
|---|---|
| `bookmark_add(slug, label_override="")` | Add/update bookmark. Idempotent on slug. |
| `bookmark_remove(slug)` | Remove bookmark. Idempotent — no error if not found. |
| `bookmark_list()` | Return bookmarks ordered by position ascending. |
| `bookmark_reorder(slug, new_position)` | Move bookmark; adjacent entries shift (dense integers). |

All tools registered via `@_tool()` decorator. Located in `yadgar/server/tools/bookmarks.py`.

### New HTTP routes (daemon port 8765)

| Method | Path | Tool |
|---|---|---|
| GET | `/api/bookmarks` | `bookmark_list` |
| POST | `/api/bookmarks` | `bookmark_add` (body: `{slug, label_override?}`) |
| DELETE | `/api/bookmarks/{slug}` | `bookmark_remove` |
| PUT | `/api/bookmarks/{slug}/position` | `bookmark_reorder` (body: `{position}`) |
| GET | `/api/wiki/search` | wiki semantic search (passthrough for UI) |
| GET | `/api/wiki/list` | wiki slug list for autocomplete |

`/api/wiki/read` already existed (unchanged). All new routes respond with `Cache-Control: no-store`.

### viz_server changes

`yadgar/viz_server.py` gained `do_DELETE` and `do_PUT` methods so the browser-side proxy
forwards DELETE and PUT requests to the daemon. Previously only GET and POST were proxied.

### Upgrade steps

1. Pull image: `podman pull docker.io/openfantasy/yadgar:5.23.0`
2. Restart daemon: `systemctl --user restart yadgar.service`
3. Migration applies automatically on first startup (embedded) or on schema init (server mode).
4. Verify: `bookmark_list()` returns `[]` (empty — no bookmarks yet).
5. Test: `bookmark_add('yadgar-roadmap-future-improvements')` → confirms MCP tool works.

### What's NOT in v5.23.0 (deferred to v5.24.0)

- `yadgar/static/bookmarks.html`, `bookmarks.css`, `bookmarks.js`
- Navigation link in `yadgar/static/index.html`
- Vendored JS libs (`marked.min.js`, `highlight.min.js`, `dompurify.min.js`)
- Playwright browser tests (PD-27 deferred)

### Rollback

Drop the `wiki_bookmark` table and revert daemon. Existing wiki/memory data unaffected.

---

## v5.21.0 — Cross-project anchor dedup detection + PD-23 migration_grace handler (2026-05-30)

Core 5.20.0 → 5.21.0. Backend unchanged at 5.4.0. No schema changes. No DB migrations required.
Odd minor per skip-1 convention.

### PD-23 Deadline (CRITICAL)

**Deadline: 2026-08-26.** First pre-v5.8 anchors (backfilled 2026-05-27 with 90-day TTL +
`migration_grace=True`) expire on that date. Without this handler they become invisible to
restore/hot queries but persist indefinitely in DB, counting toward `anchor_count_project`
signal thresholds (silent data leak).

**You must upgrade to v5.21.0 before 2026-08-26** to surface these rows for review.

### What changed

**Cross-project anchor redundancy detection:**

`audit_anchors()` now returns a `cross_project_redundancy_candidates` key (always present, may be
empty). Detection criteria:
- Cosine similarity >= `ANCHOR_CROSS_PROJECT_COSINE` (default **0.95**, configurable)
- `content_length_ratio > 0.85` (rejects "common phrase" false positives)
- Anchors from **different** `directory_context` values
- `directory_context="global"` rows excluded (already globally scoped)

`project_brief(mode="signals")` also surfaces `cross_project_redundancy_candidates` when
candidates are found (omitted when empty to stay within 100-token budget, capped at 3).

Candidate shape:
```json
{
  "primary_id": 257,
  "duplicate_ids": [311992, 489731],
  "similarity": 0.97,
  "directory_contexts": ["/home/max/git/yadgar", "/home/max/git/nix"],
  "recommended_action": "promote_to_global"
}
```

**Semantics:** AUDIT-GATED ONLY. No auto-mutation. Candidates surface as read-only signals.
The user reviews and acts (via existing `forget` / `anchor` tools) after reviewing.

Primary anchor selection: highest `access_count × heat`; tie-broken by oldest `created_at`.

**PD-23 migration_grace handler:**

`audit_anchors()` now emits `verify_grace_expired_anchor` entries in `actions` for rows where
`migration_grace=True AND valid_until < now`. These entries always have `skipped=True` and
`skip_reason="user_verification_required"` — they are NEVER auto-applied even when
`dry_run=False`. They surface as user-gated review items.

Action shape:
```json
{
  "action": "verify_grace_expired_anchor",
  "id": 518764,
  "expired_at": "2026-08-26T00:00:00+00:00",
  "rationale": "migration_grace=True anchor past valid_until; tier=conditional. Verify whether this anchor should be kept (update tier) or forgotten.",
  "skipped": true,
  "skip_reason": "user_verification_required"
}
```

Review workflow: call `audit_anchors(directory=..., dry_run=True)` → review
`verify_grace_expired_anchor` entries → for each, either call `forget(id)` to remove or
`anchor(content=..., tier='conditional')` to re-anchor with explicit TTL.

**New env knob:**

| Knob | Default | Description |
|---|---|---|
| `ANCHOR_CROSS_PROJECT_COSINE` | `0.95` | Min cosine for cross-project dedup candidate |

Registered in three-way (Settings, config_registry, config_yaml). Configurable via
`~/.yadgar/config.yaml` under `anchor_hygiene` section.

**Deferred (planned for later releases):**

- §2 Jira MCP integration for ticket-bound anchors — deferred (optional, opt-in module)
- §3 `is_protected` repurpose as verified-by-audit flag — deferred (requires `audit_pass_count` schema migration)

### Upgrade path

No DB migration needed. Upgrade yadgar, restart the container. Existing
`migration_grace=True` anchors will surface as `verify_grace_expired_anchor` candidates on
the next `audit_anchors()` call.

```bash
# Pull new image
podman pull openfantasy/yadgar:5.21.0

# Run audit to see grace-expired rows
# audit_anchors(directory="/your/project", dry_run=True)
# → review verify_grace_expired_anchor entries in actions
```

---

## v5.20.0 — DB-lockdown PreToolUse hook migrated to yadgar/hooks/ + Claude Code 2026 schema fix (2026-05-30)

Core 5.19.0 → 5.20.0. Backend unchanged at 5.4.0. No schema changes. No DB migrations.
Hotfix slot (even version per skip-1 convention).

### Why

Multiple sessions produced recurring errors:

> PreToolUse:Bash hook error — Hook JSON output validation failed — hookSpecificOutput is missing required field 'hookEventName'

Root cause: the PreToolUse Bash hook was registered in `.claude/settings.json` as:

```
python3 /home/max/git/yadgar/.claude/hooks/hook_runner.py db-lockdown-check
```

`hook_runner.py` is a project-local file in `.claude/hooks/` — it is **gitignored and never git-tracked**. The `hook_db_lockdown_check()` handler in `yadgar/scripts/hook_runner.py` (the canonical source) emitted JSON without `hookEventName`, violating the Claude Code 2026 PreToolUse schema. In-session fixes to the workspace copy of the file were lost on every context reset.

### What changed

**`yadgar/hooks/db-lockdown-check.py`** (new):
- Standalone PreToolUse hook script — no dependency on `hook_runner.py` dispatcher.
- Emits schema-compliant JSON: `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"|"deny"}}` on both paths.
- Fail-soft: malformed stdin → allow (never blocks work).
- Logic: deny if `tool_input.command` contains `docker exec yadgar-backend` or `docker exec yadgar-db`.

**`yadgar/install_hooks_lib.py`**:
- Copies `db-lockdown-check.py` to `~/.claude/hooks/yadgar-db-lockdown-check.py` (global, always installed).
- `hooks_config["PreToolUse"]` now uses a direct `python3 "<path>"` command (not the `hook_runner.py` dispatcher).

**`yadgar/scripts/hook_runner.py`**:
- Removed `hook_db_lockdown_check()` function and `"db-lockdown-check"` from `_HOOKS` dict.

**`yadgar/tests/test_db_lockdown_hook.py`** (new):
- 7 tests: allow path, deny for `yadgar-backend`, deny for `yadgar-db`, fail-soft on malformed JSON,
  `hookEventName` present on allow, deny, and fail-soft paths.

### Upgrade path

**Run `install_hooks` MCP tool after upgrading** to deploy the new standalone script:

```
install_hooks(scope="global")
```

This writes `~/.claude/hooks/yadgar-db-lockdown-check.py` and updates `~/.claude/settings.json` with the new `PreToolUse` entry.

**If you have a project-local `.claude/settings.json` with the old entry:**

The old entry looks like:
```json
"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "python3 ... hook_runner.py db-lockdown-check"}]}]
```

Either remove it manually, or run `install_hooks(scope="global")` which will overwrite the `PreToolUse` key with the new wiring. The old `hook_runner.py db-lockdown-check` entry can be deleted — the handler no longer exists in `hook_runner.py`.

---

## v5.19.0 — Scope-aware anchor surfacing in HippocampalReplay (2026-05-30)

Core 5.17.0 → 5.19.0. Backend unchanged at 5.4.0. No schema changes. No DB migrations.
Plan: `docs/PLAN_V5_19_0_ANCHOR_SURFACING.md`.

### Why

`restoration.py::HippocampalReplay.restore()` called `get_anchored_memories(limit=20)` — a flat,
unscoped query ordered by `created_at DESC`. Any project with 20+ anchors crowded global anchors
completely out of the restore payload. The `project_brief(mode="restore")` path (used by the
session-start hook via catalog) already did a correct two-query scope split; the MCP `restore()`
tool did not. This inconsistency caused the 2026-05-18 "I forgot anchor X" incidents.

### What changed

**`yadgar/storage/memory.py`**:
- New method `get_anchored_memories_scoped(directory, limit)`: two queries (global bucket first,
  then project bucket), hard safety cap 50 per scope, merged with dedup by id, ordered heat DESC
  within each scope. Global = `directory_context IN ('', 'global', 'system')`. Project = exact
  `directory_context = directory` match. Expired anchors (`valid_until < now`) excluded.

**`yadgar/restoration.py`**:
- Line 263: replaced `get_anchored_memories(limit=max_memories)` with
  `get_anchored_memories_scoped(directory=directory, limit=max_memories)`.

**`yadgar/tests/test_anchor_surfacing.py`** (new):
- 12 tests covering: global anchor in unrelated project, project isolation, global-before-project
  ordering, dedup when same row matches both queries, hard cap 50, expired anchor exclusion,
  empty/system directory_context global treatment, heat ordering within scope, and two
  HippocampalReplay integration tests.

### Upgrade path

**No action required.** Drop-in replacement. Existing anchors with `directory_context = ''` now
correctly surface as global anchors (same behaviour as the session-start catalog path).

Anchors with `directory_context = NULL` (pre-v5.8 rows) are NOT included in the global bucket
(intentional — NULL = misconfigured, not global). Run `audit_anchors()` to surface them.

---

## v5.17.0 — Write-time contradiction detection default-on (2026-05-30)

Core 5.15.0 → 5.17.0. Backend unchanged at 5.4.0. No schema changes. No DB migrations.
Plan: `docs/PLAN_V5_17_0_WRITE_TIME_CONTRADICTION.md`.

### Why

Audit Adopt-2 ("Write-time conflict resolution") identified that the lightweight contradiction detector (`yadgar/curation/contradiction.py`, built v5.3.x) was never wired into the write path. Contradicting memories accumulated until the nightly consolidation pass — a ~24h stale window. This release closes that gap.

### What changed

**`yadgar/curation/__init__.py`**:
- `curate_on_remember()` now calls `detect_contradictions()` on every write where similar memories exist (cosine ≥ 0.6). Runs after `find_similar_memories`, before the merge/link/create branch. Env-gated (`YADGAR_WRITE_TIME_CONTRADICTION`, default `on`). Fail-soft: detector errors are logged but never block the write.

**`yadgar/metrics.py`**:
- New counter `yadgar_write_time_contradiction_total{reason}` (reason: `negation_mismatch` | `action_divergence`).

**`yadgar/storage/client.py`**:
- `confidence` added to `_MEMORY_UPDATABLE_FIELDS`. Previously absent, making `update_memory_fields(id, confidence=...)` a silent no-op. The detector's confidence-decay side effect was latent dead code until this fix.

**`yadgar/tests/test_write_time_contradiction.py`** (new):
- 7 tests: default-on fires detector, env-off skips, empty store noop, fail-soft, metric increment, LLM-resolver orthogonality, no-negation no-decay.

**`docs/reference/conflict-resolver.md`**:
- New "Lightweight write-time detector" section documenting the two-layer model.

### Upgrade path

**Default on.** No action required on upgrade. Pre-existing memories whose content contradicts a new write will have `confidence` decremented by 0.1–0.2 (clamped at 0.1 floor) on the next contradicting write.

To disable:

```bash
export YADGAR_WRITE_TIME_CONTRADICTION=off
```

The LLM resolver (`YADGAR_CONFLICT_RESOLVER=on`, Ollama-dependent) is unchanged and remains default-off.

---

## v5.15.0 — CPU burst detection + secret-gate plumbing (2026-05-30)

Core 5.13.1 → 5.15.0. Backend unchanged at 5.4.0. No schema changes. No DB migrations.
Plan: `docs/PLAN_V5_15_0_CPU_BURSTS_RESIDUAL.md`.

### Why (Part A)

CPU bursts from `_consolidation_cycle()` phases are intermittent and invisible without instrumentation. Prior fixes (v4.8 cooldown, v5.7 daemon removal, v5.10 orphan cleanup, v5.10.4 sleep-cycle bypass fix) addressed known vectors. D1 adds per-phase CRITICAL logging so the NEXT burst is diagnosable in real time from `journalctl`.

### What changed (Part A — CPU burst detection)

**`yadgar/consolidation/orchestrator.py`**:
- New module-level `PHASE_DURATION_WARN_MS: int` constant loaded from settings at import time.
- New `_warn_slow_phase(phase: str, duration_ms: int) -> None` helper: emits CRITICAL log if `duration_ms > PHASE_DURATION_WARN_MS` and threshold is non-zero.
- All 7 `_consolidation_cycle()` phases now capture duration into `_dur_ms` and call `_warn_slow_phase()`.

**`yadgar/config.py`**:
- New `PHASE_DURATION_WARN_MS: int = 60_000`. Override: `YADGAR_PHASE_DURATION_WARN_MS` env var or `phase_duration_warn_ms:` in `~/.yadgar/config.yaml`.

**`yadgar/config_yaml.py`** + **`yadgar/config_registry.py`**:
- I25 three-way sync: new `cpu_burst_detection` section in `FIELD_META` + `ConfigEntry` in `_REGISTRY`.

**`yadgar/tests/test_cpu_burst_detection.py`** (new):
- `test_phase_duration_warn_emits_critical_log`: PHASE_DURATION_WARN_MS=1 → CRITICAL log emitted for slow phase.
- `test_phase_duration_under_threshold_no_warn`: high threshold → no CRITICAL emitted.
- `test_no_unexpected_sleep_cycle_callers`: grep-based audit of `run_sleep_cycle` callers.
- `test_no_unexpected_force_consolidate_callers`: grep-based audit of `force_consolidate` callers.

### Why (Part B)

v5.13.0 shipped `gate_or_reject(tags=, source=)` + allowlist mechanism as DORMANT. Production write tools (`memorize`, `wiki_add`, `anchor`) called `check_secrets()` directly (no `tags=`), so allowlist entries never fired on real tool invocations.

### What changed (Part B — secret-gate tag plumbing)

**`yadgar/server/tools/memorize.py`**:
- Import changed: `from yadgar.secrets import check_secrets` → `from yadgar.secrets import gate_or_reject`.
- Secret gate replaced: `sec_blocked, ... = check_secrets(content)` → `_gate = gate_or_reject(content, tags=list(tags) if tags else [])`. Return format aligned to gate_or_reject dict.

**`yadgar/server/tools/wiki.py`**:
- Same migration for `wiki_add()`.

**`yadgar/server/tools/misc.py::anchor`**:
- Added `tags=["_anchor"]` kwarg to existing `gate_or_reject(content, reason)` call.

**`yadgar/tests/test_secret_gate_plumbing.py`** (new):
- 5 tests: memorize/wiki_add/anchor gate kwarg regression + checkpoint gate called + e2e allowlist acceptance.

**`yadgar/tests/test_memorize_reinject_gate.py`**:
- Updated `patch("yadgar.server.tools.memorize.check_secrets", return_value=(False, None, None))` → `patch("yadgar.server.tools.memorize.gate_or_reject", return_value=None)`.

### D2/D3 — deferred (backend-side)

D2 (`/health/inference` timed-inference probe) and D3 (embed_service uptime metric + 28h alert) require `yadgar-backend` release. Implement in a follow-on `yadgar-backend` version when deploying the next backend bump.

### Upgrade path

**D1 (phase duration alert):** No action required. Default threshold is 60s — alerts fire only if a phase runs over 1 minute. To lower (more sensitive): `YADGAR_PHASE_DURATION_WARN_MS=10000`. To disable: `YADGAR_PHASE_DURATION_WARN_MS=0`.

**Part B (allowlist plumbing):** If you have a `~/.yadgar/secret-gate-allowlist.yaml` configured (v5.13.0), it NOW fires on `memorize()`, `wiki_add()`, and `anchor()` calls. Previously dormant. Verify your allowlist entries are correct before deploying.

---

## v5.13.0 — Secret-gate context-awareness + allowlist (2026-05-30)

Core 5.11.0 → 5.13.0. Backend unchanged at 5.4.0. No schema changes. No DB migrations. Plan: `docs/PLAN_V5_13_0_SECRET_GATE_CONTEXT_AWARENESS.md`.

### Why

v5.10.2 secret-gate (I26) catches real secrets but trips on legitimate content: test fixtures with fake `ghp_` tokens, plan documents discussing pattern strings, CHANGELOG entries referencing format strings. This release adds a user-managed YAML allowlist with audit trail to express context-aware bypass without removing pattern strictness.

### What changed

**`yadgar/security/allowlist.py`** (new module):
- `AllowlistEntry(tags, patterns, reason)` frozen dataclass
- `is_allowlisted(content, tags, source) -> (bool, AllowlistEntry | None)` — checks YAML allowlist
- `_reload_allowlist()` — loads/reloads from disk; raises `ValueError` loudly on bad YAML or wrong schema
- `_write_audit(...)` — appends JSONL entry to audit log
- `_detect_source()` — `inspect.stack()` heuristic for call-site name

**`yadgar/secrets.py`** — `gate_or_reject()` extended:
- New kwargs: `tags: list[str] | None = None`, `source: str | None = None`
- Calls `is_allowlisted()` BEFORE pattern scan. Hit → `_write_audit()` + skip field (return clean)
- Existing callers without `tags=` → default-deny (identical to v5.10.x)

**`scripts/check_allowlist_audit.py`** (new I28 invariant):
- Static check: `_write_audit` and `is_allowlisted` defined in `allowlist.py`
- Static check: `gate_or_reject()` calls both
- Static check: both env knobs documented in module
- Pre-commit hook wired on `yadgar/security/allowlist.py` and `yadgar/secrets.py`

**`yadgar/tests/fixtures/secret-gate-allowlist.yaml`** (new):
- Canonical YAML covering all v5.10.2 false-positive cases (test fixtures, plan-document, changelog tags)

**`yadgar/tests/test_allowlist.py`** (new, 11 tests):
- `TestAllowlistPerTagBypass` (3 tests): tag bypass / deny without tag / deny with no tags
- `TestAllowlistAuditLogWritten` (2 tests): JSONL fields + content_preview truncation
- `TestAllowlistDefaultDeny` (2 tests): no allowlist file = default-deny; clean still passes
- `TestAllowlistYamlInvalidFailsLoud` (2 tests): malformed YAML + wrong schema
- `TestSourceCallSiteDetection` (2 tests): source field present + non-empty in all entries

### Upgrade steps

1. Deploy new image (or `uv install`)
2. (Optional) Create `~/.yadgar/secret-gate-allowlist.yaml` with your allow rules
3. Review `~/.yadgar/secret-gate-audit/` periodically — JSONL files record every bypass

### Configuring the allowlist

```yaml
# ~/.yadgar/secret-gate-allowlist.yaml
allowlist:
  - tags: ["test-fixture"]
    patterns: ["ghp_*", "gho_*"]
    reason: "test fixtures may contain fake GitHub tokens"

  - tags: ["plan-document"]
    patterns: ["sk-ant-*", "ghp_*"]
    reason: "plan docs discuss secret patterns as examples"
```

Rules:
- `tags`: ALL listed tags must be present at the call site (subset match)
- `patterns`: ANY matching pattern causes bypass for that field
- `reason`: required; logged in every audit entry
- Schema version: 1 (top-level `allowlist:` key required)

### Env knobs

| Var | Default | Effect |
|-----|---------|--------|
| `YADGAR_SECRET_GATE_ALLOWLIST_PATH` | `~/.yadgar/secret-gate-allowlist.yaml` | Path to allowlist YAML |
| `YADGAR_SECRET_GATE_AUDIT_DIR` | `~/.yadgar/secret-gate-audit/` | Audit log directory |

### Deferred (v5.15.0+)

- **Caller plumbing (KNOWN GAP):** v5.13.0 ships the allowlist mechanism, but no production
  write tool (memorize, wiki_add, anchor, etc.) yet forwards its `tags` parameter to
  `gate_or_reject(tags=...)`. The allowlist YAML loads and is validated, but will never
  match any real call until callers are plumbed. Tracked for v5.15.0+. In the meantime
  the test fixture (`yadgar/tests/fixtures/secret-gate-allowlist.yaml`) exercises the full
  path via direct `is_allowlisted()` calls in tests.
- Pattern OVERRIDE (raise threshold for one tag) — v5.13.0 is full-bypass only
- Allowlist YAML schema versioning / migration
- Audit log size-based rotation (currently date-based only)
- doc-ingest pipeline as a named call-site

### Rollback

v5.11.0 is backward-compatible: remove allowlist file → default-deny, identical to v5.11.0 behavior. No DB changes.

---

## v5.11.0 — Viz knobs configurable via config.yaml (2026-05-30)

Core 5.10.11 → 5.11.0. Backend unchanged at 5.4.0. No schema changes. No DB migrations. Plan: `docs/PLAN_V5_11_0_VIZ_CONFIG_YAML.md`.

### Why

After 11 viz patches (v5.10.7–v5.10.11) every tweak required a code change + redeploy + hard-refresh. All hardcoded viz values are now configurable via `config.yaml` without touching code.

### What changed

**`yadgar/config.py`** — 35 new `VIZ_*` flat Settings fields with v5.10.11 defaults as fallback:

- Node: `VIZ_NODE_SIZE_3D` (8.0), `VIZ_NODE_SIZE_2D` (4.0), `VIZ_HEAT_*` (6 HSL params), `VIZ_CAT_COLOR_*` (8 wiki category colors)
- Edge: `VIZ_EDGE_COLOR_*` (5 edge type colors), `VIZ_EDGE_WIDTH_3D_MULTIPLIER` (1.5), `VIZ_EDGE_ARROW_LEN` (5)
- Physics: `VIZ_PHYSICS_CHARGE_STRENGTH` (-12.0), `VIZ_PHYSICS_LINK_DISTANCE_2D` (30.0), `VIZ_PHYSICS_LINK_DISTANCE_3D` (36.0)
- Layout: `VIZ_LAYOUT_ZOOM_FIT_TICK` (80), `VIZ_LAYOUT_ZOOM_FIT_PADDING` (50), `VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS` (800)
- Search: `VIZ_SEARCH_MATCH_COLOR` (#ffffff), `VIZ_SEARCH_PINNED_COLOR` (#ffd700), `VIZ_SEARCH_DIM_OPACITY` (0.18)

**`yadgar/config_yaml.py`** — 35 `FIELD_META` entries + new `viz_config` section in `SECTION_TITLES`.

**`yadgar/config_registry.py`** — 35 `ConfigEntry` rows for I25 three-way sync.

**`yadgar/server/http.py`** — new `GET /api/viz/config` endpoint returning nested JSON with keys `node`, `edge`, `physics`, `layout`, `search`. Auto-protected by `BearerAuthMiddleware`. Traced via `@trace_span("api.viz_config")`.

**`yadgar/static/index.html`**:
- `YADGAR_VIZ_CONFIG` global declared with v5.10.11 hardcoded fallback defaults
- `loadVizConfig()` async function: fetches `/api/viz/config`, deep-merges over defaults; silent fallback on error
- `loadGraph()` now calls `await loadVizConfig()` as first step
- All viz constants replaced with `YADGAR_VIZ_CONFIG.*` references

### Upgrade steps

1. `nix-apply` (or `docker-compose pull && docker-compose up -d`) to deploy new image
2. Hard-refresh browser (`Ctrl+Shift+R`) to get new `index.html`
3. (Optional) Add a `viz:` section to `config.yaml` to override any knob — see `docs/reference/viz-config.md`

### Rollback

Revert to v5.10.11. No data migration needed. No schema changes. Frontend falls back to hardcoded defaults if `/api/viz/config` is unreachable.

### Zero-change deploy

Deployments without any viz keys in `config.yaml` behave identically to v5.10.11. All 35 defaults match the previous hardcoded values exactly.

---

## v5.10.6 — SESSION_END_CAPTURE sentinel-marker pattern (2026-05-30)

Core 5.10.5 → 5.10.6. Backend unchanged at 5.4.0. No schema changes.

### What's new

Session endings are now captured as filesystem sentinel markers in `~/.yadgar/session-ends/`.
On the next session start, the marker is imported into memory and surfaced as an
`extract_last_session_findings` recommended action in `project_brief(mode="signals")`.

### Required action: re-run install_hooks

The SessionEnd hook is new and must be registered in `~/.claude/settings.json`:

```
install_hooks(scope="global")
```

Or if using project scope:

```
install_hooks(project_directory="/your/project", scope="project")
```

This adds `SessionEnd` alongside the existing `Stop` hook in the global settings file.

### New directory (auto-created)

`~/.yadgar/session-ends/` — created automatically by the hook on first run.

Failed imports moved to `~/.yadgar/session-ends/failed/` after 3 retries.

Manual cleanup if needed: `rm ~/.yadgar/session-ends/*.json`

### New env knobs (all optional, defaults shown)

| Knob | Default | Purpose |
|------|---------|---------|
| `SESSION_END_CAPTURE_ENABLED` | `true` | Kill switch for entire feature |
| `SESSION_END_RETENTION_DAYS` | `30` | Auto-prune sentinels older than N days |
| `SESSION_END_SNIPPET_TURNS` | `5` | Last N human turns embedded in sentinel |
| `SESSION_END_MIN_MESSAGES` | `2` | Skip sentinel if session had fewer than N human messages |

### end_reason gating (intentional)

`end_reason=clear` and `end_reason=resume` are intentionally skipped — they are not
true session exits. Only `logout` and `other` write sentinels.

### Consuming a sentinel

When the next session's `project_brief(mode="signals")` emits `extract_last_session_findings`:

1. Read the transcript at `transcript_path` (if it still exists).
2. Synthesize key decisions/findings.
3. Call `memorize(content='...', context=<directory>, tags=['session-finding'])`.
4. Call `forget(memory_id=<sentinel_id>)` to consume the sentinel.

If transcript is gone (file rotated), the `suggested_call` directs `forget(sentinel_id)` only.
The sentinel's `last_human_turns` field still provides partial context.

## v5.10.5 — nightly cycle remaining bugs (2026-05-30)

Core 5.10.4 → 5.10.5. Backend unchanged at 5.4.0. No schema changes.

### Bug 1 — vacuum URL second call site

`v5.10.2` fixed `_log_consolidation_row` to read `YADGAR_DB_URL` from env. Two other
call sites were missed:

- `yadgar/scripts/nightly_cycle.py::main()` — the systemd entry point
- `yadgar/vacuum/__init__.py::cmd_vacuum_impl()` — called from nightly cycle step 4

Both used `getattr(args, "backend_url", "http://127.0.0.1:8080")` which returns the
hard-coded `:8080` literal when invoked without `--backend-url` (systemd unit path).
The backend runs on `:8000`; something else bound to `:8080` returned HTTP 307.

**Fix:** both sites now use `getattr(args, "backend_url", None) or os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080")`.

**Effect:** `[vacuum] ERROR: backend at http://127.0.0.1:8080 is not reachable: HTTP 307` is eliminated. `YADGAR_DB_URL=http://127.0.0.1:8000` in the systemd unit is now respected end-to-end.

### Bug 2 — prune deletes just-created post snapshot

The nightly cycle creates a post-backup snapshot (`label="nightly-post"`) in step 5,
then prunes in step 6. `shutil.copytree(copy_function=copy2)` propagates the source
DB directory's mtime to the snapshot directory. The DB dir mtime is the last time the
DB was written — with core stopped for the cycle, this can be hours in the past.

`prune_snapshots` sorts by mtime descending; the just-created snapshot sorted as
"oldest" and was pruned immediately, leaving zero post snapshots.

**Fix:** `create_snapshot()` calls `target.touch()` after `copytree()`, stamping the
snapshot directory to the current time. This ensures a just-created snapshot always
sorts as newest regardless of source DB age.

**Retention semantics:** unchanged. `YADGAR_BACKUP_RETENTION` (default 3) still controls how many total snapshots survive. No config changes required.

### No action required for upgrade

Docker image tag: `openfantasy/yadgar:5.10.5`. No DB migrations. No config changes. Restart the core container.

## v5.10.4 — consolidate_now mode parameter (2026-05-30)

Core 5.10.3 → 5.10.4. Backend unchanged at 5.4.0. No schema changes.

### consolidate_now() — mode parameter (BEHAVIOR CHANGE)

`consolidate_now()` now accepts `mode='light'` (default) or `mode='full'`.

**Light mode (default):** runs `force_consolidate()` only — decay, episodes, merge, CLS, causal phases. Typical runtime <30 seconds. Correct for pre-shutdown flushes, debug runs, and queue-fill scenarios.

**Full mode:** `force_consolidate()` + sleep cycle (dream replay, community detection, cluster summaries, re-embedding, compression, auto-narrate) + anchor audit pass (if `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`). Typical runtime 5–15 minutes. Use for deliberate maintenance before a multi-day break.

**Breaking behavior change:** previous `consolidate_now()` with no args ran the full sleep cycle every time. After this upgrade, no-arg calls only run consolidation. If you have scripts or workflows that relied on `consolidate_now()` triggering the sleep cycle, update them to `consolidate_now(mode='full')`.

**Timestamp fix:** `mode='full'` now sets `_consolidation._last_sleep_cycle` so the 6-hour nightly gate respects manual full cycles. Previously, calling `consolidate_now()` twice in rapid succession ran the sleep cycle twice.

### hook_runner.py — PreToolUse schema fix

`db-lockdown-check` hook now emits the current Claude Code PreToolUse schema:

```json
{"hookSpecificOutput": {"permissionDecision": "allow"}}
{"hookSpecificOutput": {"permissionDecision": "deny"}, "systemMessage": "..."}
```

Old schema (`{"decision": "allow"|"block"}`) caused `(root): Invalid input` validation noise on every Bash tool call. No behavior change — Claude Code was already failing-open on the allow path.

**Installed copy:** `.claude/hooks/hook_runner.py` was updated in-place. If you re-run `yadgar install_hooks`, it will copy the fixed source from `yadgar/scripts/hook_runner.py`.

### No action required for upgrade

Docker image tag: `openfantasy/yadgar:5.10.4`. No DB migrations. No config changes. Restart the core container.

## v5.10.3 — scan_db_for_secrets.py end-to-end fix (2026-05-29)

Core 5.10.2 → 5.10.3. Backend unchanged at 5.4.0. No schema changes.

### scan_db_for_secrets.py — now functional

The v5.10.2 backfill scan script had two bugs preventing it from running end-to-end:

1. **OTLP hang at exit**: `~/.yadgar/config.yaml` sets `otlp_endpoint: http://host.containers.internal:4318/v1/traces`.
   `yadgar/server/_app.py` calls `setup_tracing()` at module import time, which instantiates a
   `BatchSpanProcessor` that retries failed OTLP exports on exit (~10 s delay with exponential back-off).
   The HITS/Clean output line was printed but scrolled past `| tail -10` before the process exited.
   Fix: `os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")` at the top of the script before any yadgar import.

2. **Detection failure with --limit N**: DB rows are scanned in ascending ID order. Memory 519107
   (the known `ghp_` 33-char leak) is at position 2994 of 3147 rows. `--limit 200` only fetched
   IDs 1–200, never reaching the leak.
   Fix: `SELECT ... ORDER BY id DESC LIMIT N` — scans newest rows first where recent leaks live.

### Running the scan

```
~/.local/pipx/venvs/yadgar/bin/python scripts/scan_db_for_secrets.py --dry-run
```

- **NOT** system Python — uses pipx venv with mcp/surrealdb packages.
- Auto-sources `~/.config/yadgar/secrets.env` if `YADGAR_DB_USER`/`YADGAR_DB_PASS` not set.
- Auto-sets `YADGAR_DB_URL=http://127.0.0.1:8000` if not set (HTTP mode, avoids file-lock conflict).
- Read-only. Never mutates the DB. Reports to `~/.yadgar/secret-leak-scan-<TS>.txt`.
- Exit 0 = clean. Exit 1 = hits found. Review report; manually `forget(<id>)` for confirmed leaks.

### No action required on upgrade

---

## v5.10.2 — Secret-gate architecture + memorize parity + nightly cycle hotfix (2026-05-29)

Core 5.10.1 → 5.10.2. Backend unchanged at 5.4.0. No schema changes. Additive only.

### Security — Secret-gate architecture (I26)

Two-layer defence against accidental secret persistence:

**Layer 2 — API boundary**: `gate_or_reject(*content_fields)` in `yadgar/secrets.py` called at the top of all write tools (`memorize`, `anchor`, `checkpoint`, `update_active_work`, `bootstrap_project`, `wiki_update`, `agent_prompt_save`). Rejects before any storage or file-queue write.

**Layer 1 — Storage level**: `check_secrets()` called inside `insert_memory()` before the DB write. Raises `SecretLeakBlocked` on hit.

**DLQ handling**: `SecretLeakBlocked` classified as `permanent` in `_classify_error()` — file moves to DLQ after 3 attempts instead of infinite retry.

**Kill switch**: `YADGAR_SECRET_GATE_DISABLED=1` bypasses Layer 1 (not Layer 2). Logs `WARNING` at every boot iteration.

**Pattern thresholds tightened**:

| Pattern | Before | After |
|---|---|---|
| GitHub PAT `ghp_/gho_/ghu_/ghs_/ghr_` | `{36,}` | `{20,}` |
| Anthropic `sk-ant-` | `{32,}` | `{20,}` |
| OpenAI `sk-` | `{30,}` | `{20,}` |

**I26 invariant lint**: `python scripts/check_secret_gate.py` — run manually or via pre-commit. Exits 1 if any write tool is missing a gate.

**Backfill scan**: `python scripts/scan_db_for_secrets.py --dry-run` scans all existing DB rows. Non-destructive. Use `--storage-mock` in CI. Reports to `~/.yadgar/secret-leak-scan-<TS>.txt`.

### memorize() anchor parity (v5.10.x)

`memorize(is_protected=True)` now behaves identically to `anchor()`:

| Behaviour | Before | After |
|---|---|---|
| `tier` when unset | `None` (no expiry logic) | `"conditional"` (90d TTL) |
| `_anchor` in tags | Only in sync path after insert | Injected before insert + via `update_memory_fields` |
| `anchor:{reason}` tag | Never | Added when `reason` arg provided |
| `semantic_immortal` without `reason` | Silently accepted | Rejected when `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=True` |

New kwarg: `memorize(…, reason: str = "")`.

### Bug fixes

**surrealdb dependency**: was `[project.optional-dependencies].dev` only. Fresh `pip install yadgar` would `ImportError` on `StorageEngine.__init__`. Promoted to `[project.dependencies]`.

**vacuum `:8080` literal**: `_log_consolidation_row()` used `http://127.0.0.1:8080` as a hard-coded fallback. Now uses `os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080")`.

### No action required on upgrade

All changes are backwards-compatible. Existing memories, anchors, and configurations are unaffected.

---

## v5.10.1 — `_active_work` soft warning tier + watchdog timer (2026-05-29)

Core 5.10.0 → 5.10.1. Backend unchanged at 5.4.0. No schema changes. Additive only.

### New recommended_actions

Two new soft-tier action types emitted by `project_brief(mode="signals")`:

| Action | Condition | Meaning |
|---|---|---|
| `consider_refresh_active_work` | `ACTIVE_WORK_WARN_HOURS < age ≤ ACTIVE_WORK_STALE_HOURS` | Proactive nudge — update during natural pause |
| `consider_refresh_checkpoint` | `CHECKPOINT_WARN_HOURS < age ≤ CHECKPOINT_STALE_HOURS` | Proactive nudge |

Soft and hard actions are **mutually exclusive** per row. Hard (`refresh_active_work`, `refresh_checkpoint`) unchanged.

Both soft + hard actions now include a `suggested_call` field with a copy-paste-able MCP call.

### New env knobs (3, three-way registered)

| Env var | yaml key | default | role |
|---|---|---|---|
| `YADGAR_ACTIVE_WORK_WARN_HOURS` | `active_work_warn_hours` | `12.0` | Soft warn threshold (hours) |
| `YADGAR_CHECKPOINT_WARN_HOURS` | `checkpoint_warn_hours` | `12.0` | Soft warn threshold (hours) |
| `YADGAR_AUTO_REFRESH_ACTIVE_WORK` | `auto_refresh_active_work` | `false` | Watchdog opt-in (see below) |

### Active-work directory registry

`update_active_work()` now writes a marker file to `~/.yadgar/active-work-tracked/<sha256(dir)[:12]>/directory.txt`. This registry is purely additive (never auto-pruned).

Manual prune old entries:
```sh
find ~/.yadgar/active-work-tracked -type d -mtime +30 -exec rm -rf {} +
```

### Optional watchdog timer (user-managed, NOT enabled by default)

New systemd-user units at `scripts/systemd-user/`:
- `yadgar-active-work-watchdog.timer` — fires every 6h
- `yadgar-active-work-watchdog.service` — scans registry, POSTs `project_brief(mode="signals")` for each dir

**Install (user-managed only):**
```sh
mkdir -p ~/.config/systemd/user/
cp scripts/systemd-user/yadgar-active-work-watchdog.{timer,service} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now yadgar-active-work-watchdog.timer
```

**Auto-refresh opt-in** (stub `_active_work` written when stale):
```sh
# Add to ~/.config/systemd/user/yadgar-active-work-watchdog.service
# under [Service]:
#   Environment=YADGAR_AUTO_REFRESH_ACTIVE_WORK=true
```

Auto-refresh dilutes user-curated `_active_work` content with a stub. Default OFF.

### Token budget for signals mode

Token budget for `signals` mode raised from 100 → 350 (configurable via `SIGNALS_TOKEN_BUDGET_SOFT`). The 100-token budget still applies to the empty-dir/minimal case; 350 covers real-world payloads with 2 soft actions + `suggested_call` fields. Real overhead at 350 per fire is <1% of typical context window. Tune via yaml (`signals_token_budget_soft`) or env (`YADGAR_SIGNALS_TOKEN_BUDGET_SOFT`).

---

## backend v5.4.0 — Recall hot-path caching: CE score cache + embedding vector cache (2026-05-29)

Core unchanged at 5.10.0. Backend 5.3.1 → 5.4.0.

### What changed

Two LRU caches added to the backend container:

| Cache | Hit-path | Key | Default cap |
|---|---|---|---|
| CE score cache | `/rerank?mode=ce` — per-text lookup before ML inference | `query_sha16:text_sha16:ckpt_sha16` | 100K entries (~100MB) |
| Embedding vector cache | `/embed` — per-text lookup before model encode | `text_sha16:mode:ckpt_sha16` | 100K entries (~150MB) |

Cache format: in-memory `OrderedDict` LRU + periodic msgpack snapshot.
Snapshot files: `/data/cache/ce.snap` and `/data/cache/embed.snap`.
Restore on startup: checkpoint-hash mismatch → discard (new model = empty cache).

#### New env knobs (6, three-way registered: Settings + FIELD_META + registry)

| Env var | yaml key | default | role |
|---|---|---|---|
| `YADGAR_CE_CACHE_ENABLED` | `ce_cache_enabled` | `true` | CE cache kill switch |
| `YADGAR_EMBED_CACHE_ENABLED` | `embed_cache_enabled` | `true` | Embed cache kill switch |
| `YADGAR_CE_CACHE_MAX_ENTRIES` | `ce_cache_max_entries` | `100000` | CE cache entry cap (0 = disabled) |
| `YADGAR_EMBED_CACHE_MAX_ENTRIES` | `embed_cache_max_entries` | `100000` | Embed cache entry cap |
| `YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC` | `cache_snapshot_interval_sec` | `600` | Snapshot cadence (seconds) |
| `YADGAR_CACHE_SNAPSHOT_DIR` | `cache_snapshot_dir` | `/data/cache` | Snapshot directory |

#### New Prometheus metrics (10 series)

```
yadgar_embed_ce_cache_hits_total         counter
yadgar_embed_ce_cache_misses_total       counter
yadgar_embed_ce_cache_evictions_total    counter
yadgar_embed_ce_cache_size_entries       gauge
yadgar_embed_ce_cache_size_bytes         gauge
yadgar_embed_embed_cache_hits_total      counter
yadgar_embed_embed_cache_misses_total    counter
yadgar_embed_embed_cache_evictions_total counter
yadgar_embed_embed_cache_size_entries    gauge
yadgar_embed_embed_cache_size_bytes      gauge
yadgar_embed_cache_snapshot_age_seconds{cache} gauge
```

### Files changed

- `yadgar/cache.py` — new `LRUCache` class + msgpack snapshot (NEW FILE)
- `yadgar/embed_service.py` — `_ce_cache`, `_embed_cache` module-level instances; `_score_ce_with_cache()` partial-hit helper; embed cache in `_encode_all()`; lifespan restore + snapshot task
- `yadgar/embed_service_metrics.py` — 10 new metric declarations + `cache_snapshot_age_seconds{cache}` gauge
- `yadgar/config.py` — 6 new Settings fields (`CE_CACHE_ENABLED` etc.)
- `yadgar/config_yaml.py` — 6 new FIELD_META entries (`backend_hot_path_cache` section)
- `yadgar/config_registry.py` — 6 new `ConfigEntry` entries
- `pyproject.toml` — `msgpack>=1.0` dependency added
- `server.json` — `backend_version` 5.3.1 → 5.4.0
- `docker-compose.yml` — backend image tag 5.3.1 → 5.4.0

### Deploy steps

1. Rebuild backend image: `docker build -t docker.io/openfantasy/yadgar-backend:5.4.0 -f Dockerfile.backend .`
2. Mount writable directory for cache snapshots. The backend container mounts `/data:ro` by default — snapshot writes to `/data/cache` are silently skipped unless a writable volume is added. Options:
   ```yaml
   # Option A: named volume (recommended for persistence across restarts)
   volumes:
     - yadgar-cache-data:/data/cache
   # Option B: tmpfs (in-memory only — cache lost on restart, no disk benefit)
   tmpfs:
     - /data/cache
   ```
   Or override to a writable path: `YADGAR_CACHE_SNAPSHOT_DIR=/tmp/cache`
3. Bump `yadger_backend_version` in `~/git/nix/modules/home/yadgar.nix` to 5.4.0.
4. Restart backend container.

### Rollback

```bash
# Disable both caches (pre-v5.4.0 behaviour, zero overhead):
YADGAR_CE_CACHE_ENABLED=false
YADGAR_EMBED_CACHE_ENABLED=false
# OR rollback image to 5.3.1:
docker run ... openfantasy/yadgar-backend:5.3.1
```

### Verification

After backend restart with traffic:
```bash
# Check hit + miss counters both > 0 within 60s of first recall
curl -s http://127.0.0.1:8001/metrics | grep yadgar_embed_ce_cache
# → yadgar_embed_ce_cache_hits_total 0 (initially)
# → yadgar_embed_ce_cache_misses_total N (rising)
# After second recall with same query:
# → yadgar_embed_ce_cache_hits_total > 0

# After CACHE_SNAPSHOT_INTERVAL_SEC elapses:
ls -la /data/cache/  # ce.snap + embed.snap present
```

### Expected impact

- CE cache: 30-70% hit-rate at steady state (same session re-querying related contexts). Each hit saves ~400ms CE inference on CPU.
- Embed cache: 50-80% hit-rate (same texts recur frequently across recall calls). Each hit saves 20-50ms encode time.
- Baseline target: ≥30% CE hit-rate after 24h soak. Tune `CE_CACHE_MAX_ENTRIES` down if RSS > 250MB.

### Open questions (post-deploy tuning)

- After 24h soak: measure actual CE + embed hit rates. Adjust cap if hit-rate cliffs below 50K entries.
- Snapshot latency during heavy traffic: add Tempo span around `save_snapshot` if `/rerank` p99 shows anomaly at snapshot interval.
- Eviction rate > 100/min sustained → cap too low; increase `CE_CACHE_MAX_ENTRIES`.

---

## v5.10.0 — Test Harness Hardening: orphan reap + port determinism + session isolation (2026-05-29)

Core 5.9.0 → 5.10.0. Backend unchanged.

### What changed

#### 1. `pytest-timeout` default now 300s (was 120s via addopts)

The `--timeout=120` flag was removed from `addopts`; a `timeout = 300` key is
set in `[tool.pytest.ini_options]` instead. Per-test overrides work via:

```python
@pytest.mark.timeout(60)
def test_slow_thing(): ...
```

#### 2. SurrealDB subprocess spawn centralized in `yadgar/tests/_surreal_helpers.py`

All `surreal start` spawns go through `spawn_surreal(port, data_dir)`, which
registers PIDs in a module-level list. An `atexit` handler calls
`kill_all_spawned_surreal()` on process exit — including SIGINT and
pytest-timeout unwind. `pytest_sessionfinish` hook in `conftest.py` also calls
it as a last-resort cleanup pass.

No action required. Existing tests use the `surreal_server` session fixture
unchanged.

#### 3. Deterministic xdist port allocation

Port formula: `YADGAR_TEST_PORT_BASE + worker_index * 100 + n`

Default base: 12000. Env knob is **TEST-ONLY** — not in production yadgar config.

Multi-session usage (2+ concurrent agent sessions):

```bash
YADGAR_TEST_PORT_BASE=13000 uv run pytest yadgar/tests/
```

Up to 10 retries with linear 100ms backoff on EADDRINUSE before raising.

#### 4. Multi-agent tmp dir isolation

```bash
YADGAR_TEST_NAMESPACE=agent-42 uv run pytest yadgar/tests/
```

Redirects `TMPDIR` to `/tmp/pytest-agent-42/` so concurrent sessions don't
collide on `/tmp/pytest-of-max/`.

Default (no env var): unchanged behaviour, `/tmp/pytest-of-max/` as before.

#### 5. Optional watchdog systemd-user timer (user-managed, not auto-installed)

Unit files are in `scripts/systemd-user/`. Install once:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd-user/yadgar-test-orphan-cleanup.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now yadgar-test-orphan-cleanup.timer
```

Fires every 5 minutes. Kills any `surreal start` process whose args contain
`pytest-of-max` — does NOT match production `~/.yadgar/surreal_db/` paths.

Adjust `pytest-of-max` in the `.service` file if your username differs.

To disable: `systemctl --user disable --now yadgar-test-orphan-cleanup.timer`

#### 6. Perf test re-enablement (deferred — separate PR)

`test_merge_duplicates_under_5s_at_500_memories_with_embeddings` remains as-is.
Per-test `@pytest.mark.timeout(120)` mechanism is now available for re-enablement
in a follow-up PR.

---

## v5.9.0 — Anchor Audit: audit_anchors() tool + consolidate_now anchor pass (2026-05-28)

Core 5.8.0 → 5.9.0. Backend unchanged at 5.3.1.

### What changed

#### 1. New MCP tool: `audit_anchors(directory, dry_run=True, cosine_threshold=None, include_global=False)`

```python
result = audit_anchors(
    directory="/home/max/git/yadgar",
    dry_run=True,                # default: report-only, no mutations
    cosine_threshold=None,        # default: ANCHOR_REDUNDANCY_COSINE (0.92)
    include_global=False,         # default: skip directory_context="global"
)
# Returns: {
#   "scanned": int,
#   "actions": [
#     {"action": "forget_expired", "id": Y, "expired_at": "...", "rationale": "..."},
#     {"action": "merge", "ids": [a, b], "similarity": 0.94, "rationale": "..."},
#     {"action": "promote", "id": X, "draft": {...}, "next_step": "..."},
#   ],
#   "dry_run": bool,
#   "applied": [...],  # populated when dry_run=False
# }
```

**Behavior:**
- `dry_run=True` (default): returns recommendations, no mutations.
- `dry_run=False`: applies SAFE mutations only.
  - `forget_expired`: `valid_until < now() AND migration_grace=False`. Safe to auto-apply.
  - `merge`: keeps survivor with higher `last_accessed + access_count` rank; forgets lower.
  - `promote`: NEVER auto-applied — always returns draft dict. Caller decides + calls `wiki_add` + `forget`.
- NEVER auto-mutated: `tier="semantic_immortal"` rows, `is_protected=True` legacy rows.
- All mutations logged to `action_log` with `source=audit_anchors` tag + before/after row snapshots.
- Idempotent: second call on same state returns empty `applied` list.

#### 2. Extended `consolidate_now()` with anchor pass

Runs as final step (after write-gate replay → embedding refresh → heat decay → SR cogmap). Per-directory dry-run audit. Writes `_audit_anchors` sentinel memory (latest-wins single row per directory; matches `_active_work` pattern).

Surfaced in next `project_brief(mode="signals")` as audit history reference.

Gate: `ANCHOR_AUDIT_CONSOLIDATION_ENABLED` (default `true`). Set false to disable.

#### 3. Promote-to-wiki draft generator

When `audit_anchors` flags a `promote` candidate, returns:

```python
{
    "action": "promote",
    "id": 257,
    "draft": {
        "suggested_slug": "yadgar-workflow-rule-build-and-push",
        "suggested_title": "Yadgar Workflow Rule — Build and Push",
        "suggested_category": "convention",
        "suggested_tags": ["yadgar", "workflow", "build", "deploy", "_anchor"],
        "body": "<verbatim anchor content>",
        "rationale": "word_count=623 AND headers=3 AND tags ∩ {workflow}",
    },
    "next_step": "Call wiki_add(title, content, tags, category) with these values, then forget(257).",
}
```

Caller copy-pastes the `wiki_add` call. NO auto-`wiki_add` (would reproduce hygiene debt one layer down — dirty slugs, inconsistent tags, no approval workflow).

#### 4. `recommended_actions` enhancement

v5.8 emitted `audit_anchors` action with `action` + `reason`. v5.9 adds `suggested_call`:

```python
{
    "action": "audit_anchors",
    "reason": "count=70 > threshold=15",
    "suggested_call": "audit_anchors(directory='/home/max/git/yadgar', dry_run=True)",
}
```

Caller copy-pastes literal string. Same pattern as v5.7.12 `restore` hint.

#### 5. New config knobs (3 total, three-way registered per I25)

| Knob | Default | Type | Purpose |
|---|---|---|---|
| `ANCHOR_AUDIT_CONSOLIDATION_ENABLED` | true | bool | Toggle anchor pass inside `consolidate_now()` |
| `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` | 20 | int | Hard cap on actions returned per audit (token budget) |
| `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS` | 30 | int | How long `_audit_anchors` sentinel snapshots retained |

### Deploy

```bash
podman build --arch amd64 -t docker.io/openfantasy/yadgar:5.9.0 -f Dockerfile .

# Bump nix (user-managed)
# Edit ~/git/nix/modules/home/yadgar.nix:
#   yadger_core_version = "5.9.0"
cd ~/git/nix && nix-update
```

### Rollback

Pure code addition — no schema change, no breaking API. Rollback to 5.8.0 simply loses the `audit_anchors()` tool surface; sentinel memories remain harmless. Safe.

### What does NOT ship in v5.9.0 (deferred to v5.11.0)

- Cross-project anchor dedup (`scope=both` aware) — see `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md`.
- Optional Jira MCP integration.
- `is_protected` flag repurpose as verified-by-3-clean-audits.

v5.10.0 is the **test harness hardening** train (pytest-timeout + SurrealDB fixture atexit + xdist port determinism + watchdog systemd timer), ships BEFORE v5.11.

---

## v5.8.0 — Anchor Hygiene Foundation: tier + valid_until + signals (2026-05-28)

Core 5.7.12 → 5.8.0. Backend unchanged at 5.3.1 (SurrealDB is schemaless; all new fields added via `DEFINE FIELD IF NOT EXISTS` at yadgar-core layer).

### What changed

#### 1. New fields on `memorize()` and `anchor()`

```python
memorize(content, context, tags, *, is_protected=False,
         tier=None,                # "semantic_immortal" | "conditional" | "ephemeral"
         valid_until=None,         # ISO-8601 UTC datetime, exclusive expiry
         ttl_days=None,            # shorthand: now + ttl_days
         provenance_agent=None)

anchor(content, context, reason="", *,
       tier="conditional",         # default
       valid_until=None,
       ttl_days=None)
```

- `tier="semantic_immortal"`: truly cross-session forever (account IDs, hard rules). Requires non-empty `reason` (gated by `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true` by default).
- `tier="conditional"` (default): currently true but may stale. `valid_until` defaults to `now() + ANCHOR_CONDITIONAL_TTL_DAYS` (90d).
- `tier="ephemeral"`: in-flight work (ticket state, dry-run plans). `valid_until` defaults to `now() + ANCHOR_EPHEMERAL_TTL_DAYS` (14d).
- `valid_until < now()` → row excluded from `restore()`, hot ranking, `project_brief(restore)` top_anchors.

#### 2. New `signals` mode fields

| Field | Type | Computation |
|---|---|---|
| `anchor_count_project` | int | Project-scope anchors not expired. |
| `anchor_redundancy_candidates` | `list[[id_a, id_b, sim]]` | **Compact tuple encoding.** Same `directory_context`, cosine ≥ `ANCHOR_REDUNDANCY_COSINE`. Capped at 3. |
| `anchor_promote_candidates` | `list[int]` | IDs of oversized anchors (word_count > `ANCHOR_PROMOTE_WORDS` AND markdown_header_count ≥ `ANCHOR_PROMOTE_HEADERS` AND tags ∩ rule/pattern/convention/playbook/workflow/recipe). Capped at 3. |
| `_truncated` | bool | True when candidate lists truncated. |

Token budget `signals` mode ≤100 maintained via K=3 hard cap + compact tuple encoding (pathological case ~147 tokens total payload; candidate-only overhead ~37 tokens).

#### 3. New `recommended_actions` action types

| action | trigger |
|---|---|
| `audit_anchors` | `anchor_count_project > ANCHOR_AUDIT_THRESHOLD` (default 15) |
| `merge_redundant_anchors` | `len(anchor_redundancy_candidates) ≥ 1` |
| `promote_anchor_to_wiki` | `len(anchor_promote_candidates) ≥ 1` |
| `forget_expired_anchors` | exists row with `valid_until < now()` AND `migration_grace=False` |

Stop hook from v5.7.12 iterates these unchanged — caller maps action to tool call.

#### 4. New config knobs (7 total, three-way registered per I25)

| Knob | Default | Type |
|---|---|---|
| `ANCHOR_CONDITIONAL_TTL_DAYS` | 90 | int |
| `ANCHOR_EPHEMERAL_TTL_DAYS` | 14 | int |
| `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON` | true | bool |
| `ANCHOR_REDUNDANCY_COSINE` | 0.92 | float |
| `ANCHOR_PROMOTE_WORDS` | 500 | int |
| `ANCHOR_PROMOTE_HEADERS` | 2 | int |
| `ANCHOR_AUDIT_THRESHOLD` | 15 | int |

#### 5. Schema migration `migration_008`

Adds `tier`, `valid_until`, `migration_grace` columns to `memory` table via `DEFINE FIELD IF NOT EXISTS`. Idempotent. Runs on first startup post-v5.8.0 — gated by sentinel `_anchor_migration_v5_8_completed=True` memory.

Backfill: all pre-v5.8 `_anchor`-tagged rows → `tier="conditional", valid_until=now()+90d, migration_grace=True`. The `migration_grace` flag protects them from `forget_expired_anchors` action while the user audits. Clear the flag manually or via v5.9 `audit_anchors()` tool.

### Deploy

```bash
# Build image
podman build --arch amd64 -t docker.io/openfantasy/yadgar:5.8.0 -f Dockerfile .

# Bump nix (user-managed)
# Edit ~/git/nix/modules/home/yadgar.nix:
#   yadger_core_version = "5.8.0"
cd ~/git/nix && nix-update

# First-startup will run migration_008; verify:
journalctl --user -u yadgar.service --since "5 minutes ago" | grep migration_008
```

### Rollback

Schemaless SurrealDB means rolling back to 5.7.12 is safe — the new `tier`/`valid_until`/`migration_grace` columns will simply be ignored by 5.7.12 code paths. No data loss.

### What does NOT ship in v5.8.0 (deferred to v5.9.0)

- `audit_anchors(directory, dry_run=True)` MCP tool — see `docs/PLAN_V5_9_ANCHOR_AUDIT.md`.
- Extend `consolidate_now()` with anchor pass.
- Promote-to-wiki draft generator.

---

## v5.7.12 — project_brief two-audience split + signals/restore modes (2026-05-27)

Core 5.7.11 → 5.7.12. Backend unchanged at 5.3.1.

### What changed

`project_brief()` gained two new modes and three new config knobs.

**New modes:**

| Mode | Audience | Budget | Notes |
|---|---|---|---|
| `signals` | Stop hook | <100 tokens | Binary flags + age numerics + `recommended_actions`. No anchors, no _render. |
| `restore` | Post-/clear, post-/compact | <800 tokens | `top_anchors` (scope-tagged), `hot_memories`, `checkpoint`, `key_wiki_pages`. No signal flags, no _render. |
| `catalog` | (deprecated, back-compat) | ~500 tokens | Current shape unchanged. Marked deprecated in docstring. Remove in v5.8. |
| `full` | Power user / debug | ~1050 tokens | Superset of catalog + inlined content. |

**New `signals` payload fields:**
- `stale_checkpoint_hours` — float|null. Age of latest checkpoint row.
- `active_work_age_hours` — float|null. Age of `_active_work` row.
- `init_memory_age_hours` — float|null. Age of `_project_init` row.
- `recommended_actions` — pre-computed list: `refresh_active_work`, `refresh_checkpoint`, `bootstrap_project` based on configurable thresholds.

**New `restore` payload:**
- `top_anchors` — merged single list (no global/project split). Each entry has `scope: "global"|"project"|"both"`.
- Truncated at `PROJECT_BRIEF_MAX_ANCHORS` (default 12).

**Bug fixes:**
- `hot_memories` now excludes anchored entries in all modes (`'_anchor' NOTINSIDE tags`). Anchors were previously appearing at top of hot_memories list by virtue of heat=1.0.

**New config knobs (yaml-backed, three-way registered):**

| Knob | Default | Description |
|---|---|---|
| `YADGAR_ACTIVE_WORK_STALE_HOURS` | `24.0` | Hours before active_work triggers `refresh_active_work` action |
| `YADGAR_CHECKPOINT_STALE_HOURS` | `24.0` | Hours before checkpoint triggers `refresh_checkpoint` action |
| `YADGAR_PROJECT_BRIEF_MAX_ANCHORS` | `12` | Max anchors in `restore` mode `top_anchors` list |

Yaml keys (lowercase): `active_work_stale_hours`, `checkpoint_stale_hours`, `project_brief_max_anchors`.

### Files changed

- `yadgar/server/tools/project.py` — new helpers `_compute_row_age_hours`, `_get_max_anchors`, `_build_recommended_actions`; mode branching in `project_brief()`; hot_memories anchor filter; restore mode anchor merge with scope field.
- `yadgar/config.py` — 3 new Settings fields.
- `yadgar/config_yaml.py` — 3 new FIELD_META entries, new `project_brief` section.
- `yadgar/config_registry.py` — 3 new `ConfigEntry` registrations.
- `yadgar/tests/test_project_brief_modes.py` — 38 new tests.
- `pyproject.toml` 5.7.11 → 5.7.12; `server.json` core version; `docker-compose.yml` core tag; `uv.lock` yadgar version.

### Deploy steps

1. Rebuild `docker.io/openfantasy/yadgar:5.7.12` image.
2. Update `yadger_core_version` in `~/git/nix/modules/home/yadgar.nix` to `5.7.12`.
3. Optional: extend `~/.yadgar/config.yaml` with the 3 new knobs (defaults are fine for most users):
   ```yaml
   active_work_stale_hours: 24.0
   checkpoint_stale_hours: 24.0
   project_brief_max_anchors: 12
   ```
4. `cd ~/git/nix && nix-update`.

### Verification

```
podman exec yadgar python -c "from yadgar.config import get_settings; s=get_settings(); print('ACTIVE_WORK_STALE_HOURS:', s.ACTIVE_WORK_STALE_HOURS)"
# → ACTIVE_WORK_STALE_HOURS: 24.0

podman exec yadgar python -c "from yadgar import server; server.init_engines(); r=server.project_brief('/repo', mode='signals'); print(r.keys())"
# → dict_keys(['_resolved_directory', '_mode', 'init_memory_present', 'active_work_present', 'stale_wiki_count', 'stale_checkpoint_hours', 'active_work_age_hours', 'init_memory_age_hours', 'recommended_actions'])
```

### Note on stop hook + restore() tool

The stop hook script (`~/.claude/hooks/yadgar-stop-memory-checkpoint.py`) and `restore()` MCP tool are NOT updated in this release — that is the main thread's follow-up task. Until updated:
- Stop hook continues to call `mode="catalog"` (back-compat guaranteed).
- `restore()` continues to call `mode="catalog"` (back-compat guaranteed).

To take advantage of new modes, update call sites to `mode="signals"` / `mode="restore"`.

---

## v5.7.11 + backend v5.3.1 — Yamlify OTLP + DBSIZE knobs, drop dead LOG_LEVEL (2026-05-27)

Core 5.7.10 → 5.7.11. Backend 5.3.0 → 5.3.1.

### What changed

5 env-only knobs migrated through `Settings` to yaml-overridable:

| Knob | Old read site | New |
|---|---|---|
| `OTLP_ENDPOINT` | `tracing.py` `os.environ` | `Settings.OTLP_ENDPOINT` (yaml: `otlp_endpoint`) |
| `OTLP_HEADERS` | same | `Settings.OTLP_HEADERS` (yaml: `otlp_headers`) |
| `OTLP_TIMEOUT_SEC` | same | `Settings.OTLP_TIMEOUT_SEC` (yaml: `otlp_timeout_sec`) |
| `OTLP_INSECURE` | same | `Settings.OTLP_INSECURE` (yaml: `otlp_insecure`) |
| `DBSIZE_CACHE_TTL_SEC` (backend) | `embed_service.py::_dbsize_cache_ttl()` `os.environ` | `Settings.DBSIZE_CACHE_TTL_SEC` (yaml: `dbsize_cache_ttl_sec`) |

LOG_LEVEL investigation confirmed it was DEAD env — only declaration in `config_registry.py`, zero code reads. Registry entry dropped. Nix `-e YADGAR_LOG_LEVEL=INFO` dropped.

`YADGAR_*` env still beats yaml in pydantic-settings precedence.

### Files changed

- `yadgar/config.py` — 5 new Settings fields.
- `yadgar/tracing.py` — `_build_otlp_exporter()` + `_parse_otlp_headers()` refactored to Settings reads.
- `yadgar/embed_service.py` — `_dbsize_cache_ttl()` refactored to Settings read.
- `yadgar/config_yaml.py` — 5 new FIELD_META entries, 2 new SECTION_TITLES (`observability`, `backend_cache`).
- `yadgar/config_registry.py` — `YADGAR_LOG_LEVEL` declaration removed.
- `yadgar/tests/test_otlp_exporter.py` — new `TestYamlOverride` (3 tests) + `reset_otel` fixture now clears Settings cache (was masking pre-existing test bug).
- `yadgar/tests/test_embed_service_v530.py` — new `test_yaml_ttl_override` (A5).
- `pyproject.toml` 5.7.10 → 5.7.11; `server.json` core + backend bumps; `docker-compose.yml` both tags bump; `uv.lock` yadgar version bump.

Nix-side (`~/git/nix/modules/home/yadgar.nix`):
- Core ExecStart drops `-e YADGAR_OTLP_ENDPOINT`, `-e YADGAR_LOG_LEVEL=INFO`. Net: 6 -e flags (was 8 post-v5.7.10).
- Backend ExecStart drops `-e YADGAR_DBSIZE_CACHE_TTL_SEC=600`. Net: 8 -e flags (was 9 post-v5.7.10).
- `yadger_core_version` 5.7.10 → 5.7.11.
- `yadger_backend_version` 5.3.0 → 5.3.1.

Host `~/.yadgar/config.yaml` extended with the 5 new keys at prior nix-default values:
```
otlp_endpoint: http://host.containers.internal:4318/v1/traces
otlp_headers: ""
otlp_timeout_sec: 10
otlp_insecure: true
dbsize_cache_ttl_sec: 600
```

### Deploy steps

1. Images already rebuilt as `docker.io/openfantasy/yadgar:5.7.11` + `docker.io/openfantasy/yadgar-backend:5.3.1`.
2. Bumps in `~/git/nix/modules/home/yadgar.nix` already done.
3. Host `~/.yadgar/config.yaml` already extended.
4. `cd ~/git/nix && nix-update`.

### Verification

```
podman exec yadgar python -c "from yadgar.config import get_settings; s=get_settings(); print('OTLP:', s.OTLP_ENDPOINT)"
# → OTLP: http://host.containers.internal:4318/v1/traces (from yaml)
podman exec yadgar-backend python -c "from yadgar.config import get_settings; print(get_settings().DBSIZE_CACHE_TTL_SEC)"
# → 600
curl -s http://127.0.0.1:3200/api/search 2>&1 | grep -c yadgar-core
# → spans still arriving via OTLP (yaml-sourced endpoint)
```

I25 invariant test still green; grandfather list unchanged (these knobs were never on it — they had no Settings field before, so I25 had nothing to flag).

---

## v5.7.10 — Container yaml loading + I25 invariant + nix -e cleanup (2026-05-27)

Core 5.7.9 → 5.7.10. Backend unchanged (5.3.0).

### What changed

**Step A — Container yaml loading.** `yadgar/config_yaml.py::get_config_path()`
now honors `YADGAR_CONFIG_FILE` env override. Set in nix ExecStart to
`/data/config.yaml`. Container app now reads `/data/config.yaml` (= host
`~/.yadgar/config.yaml` via existing bind mount). Pre-v5.7.10 the hardcoded
`~/.yadgar/config.yaml` resolved to `/root/.yadgar/config.yaml` inside container,
which didn't exist → yaml silently ignored → ALL config came from env.

**Step B core — I25 invariant + three-way-sync TDD test.** New
`tests/test_config_three_way_sync.py` enforces that every `Settings` field
must be EITHER triple-registered (`config.py` + `config_yaml.py` +
`config_registry.py`) OR explicitly allowlisted as intentional env-only in
`yadgar/tests/config_env_only_allowlist.txt`. Wired into pre-commit + CI.

The allowlist landed with TWO tiers:
- **Tier 1** (8 entries): genuine env-only by design — secrets, infra paths,
  container flags.
- **Tier 2** (181 entries): GRANDFATHERED backlog (66 yaml-gap +
  115 registry-gap pre-v5.7.10 knobs). Invariant catches NEW drift; legacy
  cleanup is incremental.

Invariant **I25** added to `docs/ARCHITECTURE_INVARIANTS.md`.

**Step C — Nix `-e` flag cleanup.** Four operational knobs moved from nix
ExecStart `-e` flags into `~/.yadgar/config.yaml`:

| Knob | Old (nix -e) | New (yaml key) |
|---|---|---|
| HOST | `YADGAR_HOST=0.0.0.0` | `host: 0.0.0.0` |
| PORT | `YADGAR_PORT=8765` | `port: 8765` |
| WIKI_SLUG_PREFIX | `YADGAR_WIKI_SLUG_PREFIX=yadgar` | `wiki_slug_prefix: yadgar` |
| CORE_LOG_LEVEL | `YADGAR_CORE_LOG_LEVEL=INFO` | `core_log_level: info` |

Stayed env (intentional):
- Secrets (`DB_USER`, `DB_PASS`, `MCP_AUTH_TOKEN`)
- Infra wiring (`DB_URL`, `EMBED_URL`, `DATA_DIR`)
- Deployment flag (`IN_CONTAINER=1`)
- OTLP endpoint (`YADGAR_OTLP_ENDPOINT` — no Settings field, reads via os.environ in tracing.py)
- `YADGAR_LOG_LEVEL` (no Settings field — defensive keep)

Backend ExecStart gains `-e YADGAR_CONFIG_FILE=/data/config.yaml` for
consistency but no other knobs moved (DBSIZE_CACHE_TTL has no Settings
field; remains env-only).

End state: yadgar core ExecStart has 8 `-e` flags (was 12). Backend
has 9 (was 8 — the YADGAR_CONFIG_FILE addition for uniform yaml loading).

### Files changed

- `yadgar/config_yaml.py` — `get_config_path()` env override.
- `yadgar/config.py` — `YamlConfigSource._load()` delegates to the helper.
- `yadgar/config_registry.py` — `YADGAR_CONFIG_FILE` entry.
- `yadgar/tests/test_config_yaml_container_path.py` — 7 tests for Step A.
- `yadgar/tests/test_config_three_way_sync.py` — I25 enforcement.
- `yadgar/tests/config_env_only_allowlist.txt` — Tier-1 + Tier-2 lists.
- `.pre-commit-config.yaml` + `.forgejo/workflows/ci.yaml` — I25 hook + CI step.
- `docs/ARCHITECTURE_INVARIANTS.md` — I25 section.
- `~/.yadgar/config.yaml` — NEW host file with the 4 moved keys + log level.

### Deploy steps

**Required before restart:**

```
mkdir -p ~/.yadgar
cat > ~/.yadgar/config.yaml <<'EOF'
host: 0.0.0.0
port: 8765
wiki_slug_prefix: yadgar
core_log_level: info
backend_log_level: info
EOF
chmod 0600 ~/.yadgar/config.yaml
```

Then pull and restart the updated image (`docker.io/openfantasy/yadgar:5.7.10`).

### Verification

After restart, container should still listen on 8765 + announce
`yadgar-core/5.7.10` etc. To confirm yaml IS being read:

```
podman exec yadgar python -c "import os; print(os.environ['YADGAR_CONFIG_FILE'])"
# → /data/config.yaml
podman exec yadgar ls -la /data/config.yaml
# → exists, ~556 bytes
```

If something binds wrong (e.g. 127.0.0.1 instead of 0.0.0.0), yaml
loading is broken — rollback by adding `-e YADGAR_HOST=0.0.0.0` back to
nix ExecStart and reporting the issue.

### v5.7.11 follow-up

- Full Step B backfill: incrementally drain the 66+115 Tier-2 grandfather
  list. I25 catches NEW drift; this trims the historical backlog.
- I26 invariant: lint nix ExecStart for `-e <KNOB>=<value>` pairs where
  KNOB has a Settings field + yaml entry (i.e. should be in yaml not env).

---

## Backend v5.3.0 — dbsize cache + restart attribution (2026-05-27)

Backend 5.2.2 → 5.3.0. Core unchanged (5.7.9).

### What changed

**1. `/admin/dbsize` 60s in-memory cache.** Core polls this endpoint every 5s
for the viz daemon. Previously every poll walked `/data/surreal_db` via
`os.walk` — O(n) over the entire DB. Now: cached for `YADGAR_DBSIZE_CACHE_TTL_SEC`
seconds (default 60). 12× reduction in walk frequency. Response gains a
`cache_age_seconds` field; clients can ignore it for back-compat.

**2. Restart cause attribution.** New counter
`yadgar_embed_restart_reason_total{reason}` with labels: `clean` (graceful
SIGTERM, shutdown marker present at startup), `crash` (no marker — process
died unexpectedly), `first_boot` (no marker AND no surreal_db dir).
Shutdown event writes marker at `YADGAR_SHUTDOWN_MARKER_PATH`
(default `/data/.shutdown_clean`). Startup increments counter + removes
marker. Forensic-time savings on mystery restarts.

Bonus: pre-existing I13 HARD nesting violations in `rerank` + `admin_dbsize`
fixed via extracted helpers `_annotate_span` + `_walk_db_sizes`. No
behavior change.

### New env knobs

- `YADGAR_DBSIZE_CACHE_TTL_SEC` (int, default 60). Set to 0 to disable.
- `YADGAR_SHUTDOWN_MARKER_PATH` (string, default `/data/.shutdown_clean`).

Both registered in `config_registry.py`.

### New metrics (backend registry)

- `yadgar_embed_dbsize_cache_hits_total`
- `yadgar_embed_dbsize_cache_misses_total`
- `yadgar_embed_restart_reason_total{reason}`

### Files changed

- `yadgar/embed_service.py` — cache + restart attribution + helpers + lifespan extension.
- `yadgar/embed_service_metrics.py` — 3 new counters.
- `yadgar/config_registry.py` — 2 new env knob entries.
- `server.json` — backend_version 5.2.2 → 5.3.0.
- `docker-compose.yml` — backend image tag 5.2.2 → 5.3.0.
- `yadgar/tests/test_embed_service_v530.py` — 8 TDD tests.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar-backend:5.3.0`.
2. Bump `yadger_backend_version` 5.2.2 → 5.3.0 (already done in `~/git/nix/modules/home/yadgar.nix`).
3. `cd ~/git/nix && nix-update`.

### Verification

After backend restart:

```
curl -sS http://127.0.0.1:8765/metrics | grep yadgar_embed_restart_reason_total
```

Should show `reason="clean"` (post-graceful-restart) or `reason="crash"`
(if backend went down hard).

dbsize cache:
```
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8001/admin/dbsize | jq .cache_age_seconds
```

Second request within 60s should return a `cache_age_seconds` value > 0.

---

## v5.7.9 — Source-aware SessionStart response (2026-05-27)

Core 5.7.8 → 5.7.9. Backend unchanged (5.2.2).

### What changed

`SessionStart` hook handler now branches on the `source` field
(`startup` / `resume` / `clear` / `compact`). Compact auto-restore was
ALREADY shipped via a separate `matcher: "compact"` hook (routes to
`post-compact-rehydrate` → `replay.restore()`). The general handler was
emitting a redundant restore hint on top. v5.7.9 suppresses the hint
on `compact` to eliminate the duplicate and adds source-tailored copy
for the other three sources.

### Behavior matrix

| source | hint emitted? | copy |
|---|---|---|
| `startup` | yes | "Session starting — call restore(directory) to pick up where you left off" |
| `resume` | yes | "Resuming session — checkpoint loaded externally if available" |
| `clear` | yes | "Session cleared — call restore(directory) if needed" |
| `compact` | NO (post-compact-rehydrate handles auto-restore) | — |
| missing | yes (treated as startup) | startup copy |

### Files changed

- `yadgar/scripts/hook_runner.py` — `hook_session_start_context` reads stdin `source` and passes as query param.
- `yadgar/server/http.py` — `hook_session_context` reads `source` query, branches.
- `yadgar/tests/test_v579_smart_sessionstart.py` — 15 new TDD tests.
- `yadgar/tests/test_session_context_endpoint.py` — one assertion updated (exact equality → `in`).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.9`.
2. Bump `yadger_core_version` 5.7.8 → 5.7.9 (already done).
3. `cd ~/git/nix && nix-update`.

---

## v5.7.8 — /mcp trace_id wiring (2026-05-27)

Core 5.7.7 → 5.7.8. Backend unchanged (5.2.2).

### What changed

Closed Bug 4 residual. `POST /mcp` log lines previously lacked `trace_id`
because FastAPIInstrumentor's span closed when the inner app returned, so
`RequestLoggingMiddleware.finally` saw `None`. Added `MCPTraceSpanMiddleware`
ABOVE `RequestLoggingMiddleware` in both `_cors_wrapped_http_app` and
`_auth_wrapped_sse_app`. The outer span outlives the inner-app call, so the
finally block reads a valid trace_id.

### Files changed

- `yadgar/server/_app.py` — new `MCPTraceSpanMiddleware` (~60 LOC); wired into both wrappers.
- `yadgar/tests/test_mcp_trace_middleware.py` — 2 new tests.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.8`.
2. Bump `yadger_core_version` 5.7.7 → 5.7.8 (already done).
3. `cd ~/git/nix && nix-update`.

### Verification

After restart: `journalctl --user -u yadgar | grep 'POST /mcp' | jq .trace_id`
should return non-null values (was null before this release).

---

## v5.7.7 — VIZ_HEALTH_REFRESH_SEC env knob (2026-05-27)

Core 5.7.6 → 5.7.7. Backend unchanged (5.2.2).

### What changed

The viz daemon's health-scraper interval (hardcoded 5.0s since V1c) is
now configurable via `YADGAR_VIZ_HEALTH_REFRESH_SEC`. Live-reloaded per
iteration — no daemon restart needed when the env value is updated.
Default 5.0 preserves prior behavior.

### Files changed

- `yadgar/config.py` — `VIZ_HEALTH_REFRESH_SEC: float = 5.0` field.
- `yadgar/config_registry.py` — registered for `/admin/config` + gauge.
- `yadgar/viz_daemon_health.py` — `run_health_scraper` reads `get_settings().VIZ_HEALTH_REFRESH_SEC` per iteration; hardcoded constant + TODO removed.
- `yadgar/tests/test_viz_daemon_health.py` — 2 new tests (default + env override).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.7`.
2. Bump `yadger_core_version` 5.7.6 → 5.7.7 (already done).
3. `cd ~/git/nix && nix-update`.

---

## v5.7.6 — OTLP/HTTP span exporter to Tempo (2026-05-27)

Core 5.7.5 → 5.7.6. Backend unchanged (5.2.2).

### What changed

Added an OTLP/HTTP span exporter alongside the existing `LogSpanProcessor`.
When `YADGAR_OTLP_ENDPOINT` is set, spans ship directly to Tempo (or any
OTLP receiver). When unset (default), behavior is unchanged — spans
remain in JSON log lines for journal-jq.

New env knobs (all 4 registered in `config_registry.py`):

- `YADGAR_OTLP_ENDPOINT` — empty by default. Example: `http://tempo:4318/v1/traces`.
- `YADGAR_OTLP_HEADERS` — comma-separated `k=v` pairs, default empty.
- `YADGAR_OTLP_TIMEOUT_SEC` — default 10.
- `YADGAR_OTLP_INSECURE` — advisory; actual TLS is determined by URL scheme.

New dep: `opentelemetry-exporter-otlp-proto-http>=1.30,<2`.

### Files changed

- `yadgar/tracing.py` — `_parse_otlp_headers`, `_build_otlp_exporter`, conditional wiring in `setup_tracing()`.
- `yadgar/config_registry.py` — 4 new env-knob entries.
- `pyproject.toml` + `uv.lock` — new OTLP exporter dep + 5 transitive packages.
- `yadgar/tests/test_otlp_exporter.py` — 14 new tests.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.6`.
2. Bump `yadger_core_version` 5.7.5 → 5.7.6 (already done).
3. `cd ~/git/nix && nix-update`.
4. To activate Tempo ingestion (separate nix work, not in this release):
   set `YADGAR_OTLP_ENDPOINT=http://tempo:4318/v1/traces` (or wherever
   your Tempo receiver listens) in the yadgar systemd unit's
   `Environment=` block, then restart yadgar.

### Verification

After restart with `YADGAR_OTLP_ENDPOINT` set: spans appear in the
Tempo "Search" Grafana panel within seconds. With endpoint unset:
no behavior change vs v5.7.5.

---

## v5.7.5 — I24 @trace_span AST lint (2026-05-27)

Core 5.7.4 → 5.7.5. Backend unchanged (5.2.2).

### What changed

Added `scripts/check_trace_spans.py` (stdlib-only AST scanner), pre-commit
hook + Forgejo CI step, and new invariant **I24** ("declared public HTTP
handlers MUST carry `@trace_span`"). Mirrors I23 (PR-L)'s metric-writer
pattern. Scoped narrowly to `yadgar/server/http.py` top-level async
functions — storage / retrieval / consolidation subpackages have too many
public helpers without spans for a one-shot enforcement.

### Files changed

- `scripts/check_trace_spans.py` — new (208 LOC).
- `yadgar/tests/test_check_trace_spans.py` — new (7 tests).
- `yadgar/server/http.py` — 13 `@trace_span` decorators added on previously
  un-spanned public handlers: `hook.metrics`, `hook.pre_compact`,
  `hook.post_compact`, `hook.session_context`, `api.stats`,
  `api.graph_stats`, `api.graph_neighborhood`, `api.system`,
  `api.heat_histogram`, `api.consolidation_log`, `api.graph_events`,
  `api.wiki_read`, `api.graph_view`.
- `.pre-commit-config.yaml` — new `check-trace-spans` hook.
- `.forgejo/workflows/ci.yaml` — new I24 step after I23.
- `docs/ARCHITECTURE_INVARIANTS.md` — I24 entry + decision-log note.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.5`.
2. Bump `yadger_core_version` 5.7.4 → 5.7.5 (already done).
3. `cd ~/git/nix && nix-update`

### Verification

After restart, the 13 new `span="api.*"` / `span="hook.*"` series should
appear in Tempo / journal-jq trace lookup. `python scripts/check_trace_spans.py`
exits 0 on master.

---

## v5.7.4 — Hook observability extension (2026-05-27)

Core 5.7.3 → 5.7.4. Backend unchanged (5.2.2).

### What changed

Added `@trace_span` + duration histogram + failure counter wrapper on the
two HTTP hook handlers PR-K missed: `/hooks/auto-capture` and
`/hooks/prompt-recall`. Post-v5.6.7 analytics flagged these two as ~93%
of hook traffic with zero coverage — they're the busiest handlers in the
codebase. Pattern matches PR-K's `_hook_observe` + `_hook_observe_response`
envelope: only `>= 500` counts as failure (400 bad-JSON and 429
rate-limited are not failures); 503 storage-uninit IS.

### Files changed

- `yadgar/server/http.py` — `hook_auto_capture` + `hook_prompt_recall` get
  `@trace_span`, `_t0`/`_caught_exc`/`finally` envelope, early-return
  `_hook_observe_response` calls.
- `yadgar/tests/test_hook_handler_spans.py` — 4 new tests.
- `.complexity-baseline.json` — `http.py` LOC baseline updated (1245→1269).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.4`.
2. Bump `yadger_core_version` 5.7.3 → 5.7.4 (already done).
3. `cd ~/git/nix && nix-update`

### Verification

After restart, `yadgar_hook_execution_duration_ms_count{hook="auto_capture"}`
and `{hook="prompt_recall"}` should start incrementing on every hook
invocation. Both labels were absent from `/metrics` before this change.

---

## v5.7.3 — DB metric dedup (2026-05-27)

Core 5.7.2 → 5.7.3. Backend unchanged (5.2.2).

### What changed

Dropped duplicate metric `yadgar_db_query_duration_seconds`. Same writer
site, same semantics as the canonical `yadgar_surrealdb_query_duration_ms{op}`
(labeled by operation). Single writer in `storage/client.py::_observe_query_metrics`
emitted both — now emits only the labeled `_ms` variant.

### Files changed

- `yadgar/metrics.py` — declaration removed.
- `yadgar/storage/client.py` — `.observe(elapsed_s)` call + import removed.
- `yadgar/tests/test_storage_db_metrics.py` — `TestDbQueryDurationSeconds` deleted.
- `.complexity-baseline.json` — line-number keys shifted.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.3`.
2. Bump `yadger_core_version` 5.7.2 → 5.7.3 (already done).
3. `cd ~/git/nix && nix-update`

### Dashboard impact

None — no Grafana panels referenced the dropped metric.

---

## v5.7.2 — CROSS_ENCODER_TOP_K cut 20 → 10 (2026-05-27)

Core 5.7.1 → 5.7.2. Backend unchanged (5.2.2).

### What changed

`CROSS_ENCODER_TOP_K` default cut from 20 to 10. Cross-encoder rerank
dominates explicit-recall latency (post-v5.6.7 analytics: 19s p50 vs
<250ms for BM25+HNSW+embed combined). CE call cost scales roughly
linearly with candidate count, so halving TOP_K should roughly halve
rerank stage time.

Override at runtime via `YADGAR_CROSS_ENCODER_TOP_K=20` (env var) if
the wider candidate pool turns out to be necessary for recall quality
on your corpus.

### Files changed

- `yadgar/config.py` — `CROSS_ENCODER_TOP_K: int = 10` (was 20).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.2`.
2. Bump `yadger_core_version` 5.7.1 → 5.7.2 in `~/git/nix/modules/home/yadgar.nix` (already done).
3. `cd ~/git/nix && nix-update`

No pipx re-install needed (no new console-script entries).

### Verification

Compare `yadgar_recall_duration_ms` p50 / p95 before vs after. Expect
roughly 50% drop on the rerank stage; smaller drop on total recall
(BM25 + HNSW + embed unchanged).

---

## v5.7.1 — Consolidation systemctl container fix (2026-05-27)

Core 5.7.0 → 5.7.1. Backend unchanged (5.2.2).

### What changed

Removed the broken `systemctl --user is-active yadgar-vacuum.service` pre-check
inside `ConsolidationScheduler._maybe_auto_vacuum`. In container deploys
`systemctl` does not exist on `PATH`; `subprocess.check_output` raised
`FileNotFoundError`, the except clause returned early, and the threshold
backstop never fired. With v5.7.0 PR-4 the underlying `_fire_vacuum_service()`
became an atomic trigger-file write — the pre-check guard against double-start
is now both unnecessary and broken.

### Files changed

- `yadgar/consolidation/__init__.py` — removed `import subprocess as _subprocess`
  + the 16-line pre-check block in `_maybe_auto_vacuum`.
- `yadgar/tests/test_vacuum_auto_trigger.py` — dropped 2 tests asserting the
  broken pre-check behavior; stripped 3 dead `_subprocess` patches from
  existing tests; added `test_auto_trigger_fires_when_systemctl_missing_filenotfound`.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.1`.
2. Bump `yadger_core_version` 5.7.0 → 5.7.1 in `~/git/nix/modules/home/yadgar.nix` (already done).
3. `cd ~/git/nix && nix-update`
4. The pipx re-install from v5.7.0 (item 4 of that section) still applies if
   you haven't run it yet — `yadgar-nightly-cycle` entry point still needs
   first-time registration in `~/.local/bin/`.

---

## v5.7.0 — Nightly Cycle Redesign (2026-05-26)

Core 5.6.7 → 5.7.0. Backend unchanged (still 5.2.2).

### What changed

Replaces the daemon's removed 30-minute consolidation trigger with a single nightly
heavy cycle at 19:00 UTC. The cycle runs `backup → consolidation → vacuum → backup`
once per day via a host-side systemd timer that invokes the new
`yadgar-nightly-cycle` console script.

### PRs in this train (in order)

- **PR-0** Remove daemon 30-min consolidation trigger (`bac9540`).
- **PR-2** Vacuum exit-code warn-only on post-restart `check_invariants` 404 (`3b84af4`).
- **PR-3** 30s API readiness wait before `check_invariants` (`d41b63d`).
- **PR-4** Trigger-file pattern for MCP `vacuum_now()` (`e0e9ee0`).
- **PR-5** Documented `VACUUM_AUTO_THRESHOLD_BYTES` as emergency backstop (`bc653de`).
- **PR-6** `create_snapshot` + `prune_snapshots` helpers in `yadgar/backup.py` (`e6857b2`).
- **PR-1a** `yadgar/scripts/nightly_cycle.py` orchestrator (`4fbca8d` + `a7344cb`).
- **PR-7** Backup snapshot round-trip integrity tests (`b9c0026`).
- **PR-1b** Nix systemd timer/service + path-watch unit (in `~/git/nix/modules/home/yadgar.nix`).

### Deploy steps

1. **Rebuild yadgar core image** at the 5.7.0 tag (amd64-only per workflow rule):

   ```
   docker build -t docker.io/openfantasy/yadgar:5.7.0 .
   ```

### Verification

After deploy:

- `systemctl --user list-timers yadgar-nightly-cycle` — shows the next 19:00 UTC fire.
- `systemctl --user list-timers yadgar-vacuum` — weekly Sunday 04:00 still wired
  (NOT replaced; runs alongside the nightly as the emergency-only legacy backstop).
- `systemctl --user status yadgar-vacuum-trigger.path` — `Active: active (waiting)`.
- Trigger flow smoke test:
  ```
  touch ~/.yadgar/triggers/vacuum_requested
  systemctl --user status yadgar-vacuum-trigger.service
  # should show oneshot ran; trigger file should be gone; yadgar-vacuum.service running
  ```
- Daemon idle eviction soak (from v5.6.7): `yadgar_embed_model_loaded{ce,nli,pair}`
  should stay at 1 once first request loads them (24h soak ends ~2026-05-27 11:41).

### Behavior change

- Consolidation no longer auto-fires every 30 minutes from the daemon. It runs
  ONLY when:
  1. The nightly cycle (19:00 UTC) fires.
  2. MCP `consolidate_now()` is called explicitly.
- `VACUUM_AUTO_THRESHOLD_BYTES` (default 2 GiB) remains as an emergency backstop
  invoked from `ConsolidationScheduler._maybe_auto_vacuum()`. Documented as
  emergency-only in `README.md` and `yadgar/config.py`.

### Known gaps / followups

- `yadgar/consolidation/__init__.py:218-231` still has a `systemctl is-active`
  pre-check that returns early on `FileNotFoundError` — auto-trigger path
  remains broken in containers. Flagged for v5.7.x.
- Consolidation auto-trigger from threshold path uses the trigger-file pattern
  via `_fire_vacuum_service` (PR-4) — works correctly in containers now.
- Pre-existing test failures inherited from v5.6.7 still red on master:
  `test_branch_auto_capture::test_checkpoint_passes_branch_to_replay`,
  `test_check_invariants`, `test_ml_client`, `test_session_context_endpoint`,
  `test_structured_logging`, `test_transport`, `test_v546_parity`. Hotfix
  candidates for v5.7.x.
- 18s yadgar-core downtime during the nightly cycle is expected (same as
  the existing weekly vacuum). Active Claude sessions reconnect via `/mcp`.

---

## v5.6.7 PR-M — Optional log-dir relocation (2026-05-25)

Core 5.6.6 → 5.6.7. Backend unchanged.

### Why

PLT stack's Grafana Alloy log shipper runs as a `DynamicUser` and cannot traverse the
user's home directory (`mode 700`). Moving logs to a world-traversable system path (e.g.
`/var/log/yadgar`) lets Alloy read them without privilege escalation or `SupplementaryGroups`
hacks. The change is OS-agnostic. **File logging is now opt-in via `YADGAR_LOG_DIR`**: when the
env var is unset, yadgar continues stdout-only (the effective default for bare-metal installs
and Docker containers before v5.5.1 applied). Container entrypoints default to
`YADGAR_LOG_DIR=/data/logs` explicitly — no change for operators running containers.

### Files changed

- `yadgar/log_config.py` — new `_resolve_log_dir()` helper; `_resolve_log_file_path()`
  derives path from dir knob; `_install_file_handler` requires at least one of
  `YADGAR_LOG_DIR`, `YADGAR_LOG_FILE_PATH`, or `YADGAR_BACKEND_LOG_FILE_PATH` to be set
  (per-file vars still take priority for test-rig compatibility).
- `entrypoint.sh` + `entrypoint-backend.sh` — default `YADGAR_LOG_DIR=/data/logs` for
  containers; log resolved value to stderr at startup; mkdir + chmod 0750 with fallback.
- `docker-compose.yml` — comment block explaining dev (named volume) vs production
  (host bind-mount) log-dir options.
- `yadgar/tests/test_log_dir_env.py` — 6 new TDD tests.

### For NixOS hosts running Alloy

Set `services.yadgar.logDir = "/var/log/yadgar";` in the yadgar NixOS module
(assuming the module exposes this option — flagged for nix-repo follow-up).

**Manual commands (do NOT auto-apply — run as root or with sudo):**

```bash
mkdir -p /var/log/yadgar
chmod 0750 /var/log/yadgar
chown <yadgar-service-user>:users /var/log/yadgar
```

Then set `YADGAR_LOG_DIR=/var/log/yadgar` in the yadgar systemd `EnvironmentFile`
or via the NixOS module's `environment` attribute.

### Alloy config follow-up (separate nix-repo commit)

Update the Alloy pipeline's `__path__` glob from:

```
~/.yadgar/logs/*.log
```

to:

```
/var/log/yadgar/*.log
```

The `{job="yadgar"}` label and all Loki dashboard queries are unchanged.

### Migration of existing logs

Optional. The new path starts fresh on first write. Archive or leave old logs
in `~/.yadgar/logs/` — yadgar will not touch them after the env var is set.

### Backwards compatibility

**Container operators:** no change — entrypoints set `YADGAR_LOG_DIR=/data/logs` by default.

**Bare-metal/non-container operators who want file logging:** explicitly set `YADGAR_LOG_DIR`
(e.g. `YADGAR_LOG_DIR=$HOME/.yadgar/logs` to replicate the old implicit path, or
`YADGAR_LOG_DIR=/var/log/yadgar` for Alloy integration).

When `YADGAR_LOG_DIR` and per-file vars are all unset, yadgar is stdout-only (same as
before v5.5.1 introduced Sink B). No restart-time shims needed; yadgar picks up the env
on next deploy/restart.

---

## v5.6.1 — V1c bug fixes (2026-05-22)

Core 5.6.0 → 5.6.1. Backend unchanged (5.1.2).

### Changes

- `yadgar/viz_daemon_health.py` — Bug 1: backend URL now resolved via `_get_backend_metrics_url()` (`YADGAR_EMBED_URL` → `http://yadgar-backend:8001/metrics`; override via `YADGAR_BACKEND_METRICS_URL`). Bug 2: `parse_core_metrics` uses new `_parse_core_process()` that reads `yadgar_process_rss_bytes` / `yadgar_process_open_fds` / `yadgar_process_cpu_percent` from core's isolated registry.
- `pyproject.toml`, `server.json`, `docker-compose.yml` — version 5.6.0 → 5.6.1.
- No new deps, no schema changes, no env-var changes (existing `YADGAR_EMBED_URL` re-used).

### Deploy (core only)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.6.1 .
podman push docker.io/openfantasy/yadgar:5.6.1
# Bump nix yadgar_core_version=5.6.1 + home-manager switch
systemctl --user restart yadgar
```

### Verify

```bash
curl -sS http://127.0.0.1:8765/api/daemon-health | python3 -m json.tool | head -20
# Expected: core.process.rss_bytes non-null; backend reachable (not unavailable)
```

---

## v5.6.0 — V1c viz daemon sidebar (2026-05-22)

Core 5.5.3 → 5.6.0. Backend unchanged (5.1.2).

### Changes

- `yadgar/viz_daemon_health.py` — new module: background scraper + `/api/daemon-health` endpoint. Scrapes core metrics (local `generate_latest()`) + backend metrics (HTTP to `http://127.0.0.1:8001/metrics`) every 5s. Caches JSON. Always HTTP 200; `backend.unavailable=True` when unreachable.
- `yadgar/server/http.py` — SSE channel extended: emits `daemon_health` event (JSON) every 5s from `_make_event_stream`. New route `/api/daemon-health` → `api_daemon_health_route`.
- `yadgar/static/index.html` — 480px collapsible sidebar (`#dh-panel`) with Core + Backend cards: process/queue/log/CB/rerank/models. Toggle button in topbar. SSE `daemon_health` event handler wired. REST fallback `_dhFetchOnce()` on panel open.
- Version: `5.5.3 → 5.6.0` in `pyproject.toml`, `server.json`, `docker-compose.yml`.
- No new Python or JS dependencies.

### Deploy (core only — backend unchanged)

```bash
# Build new core image
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.6.0 .

# Bump nix yadgar_core_version=5.6.0 + home-manager switch
# (edit nix config to reference 5.6.0 image, then switch)
systemctl --user restart yadgar
```

### Verify

```bash
# 1. Daemon health endpoint
curl -sS http://127.0.0.1:8765/api/daemon-health | python3 -m json.tool | head -30
# Expected: JSON with core.circuit_breakers, core.queue, backend.process keys

# 2. Load viz UI in browser
# http://localhost:42069/ → click "⬡ Daemons" button in topbar
# Panel should open; core stats populate within 5s; backend may show "unreachable" if not running

# 3. SSE stream carries daemon_health
curl -sS -N http://127.0.0.1:8765/api/graph/events | grep daemon_health | head -1
```

---

## v5.5.3 — V1b CB-1 state gauge (2026-05-22)

Core 5.5.2 → 5.5.3. Backend unchanged (5.1.2).

### Changes

- `yadgar_circuit_breaker_state{endpoint}` now updates inline on every CB state transition.
- Removes dead polling function `_collect_circuit_breaker_states()` from `metrics.py` (looked for `_cb_ce`/`_cb_nli`/`_cb_pair` attrs that never existed — gauge was emitting nothing since introduction).
- `_CircuitBreaker` accepts `metrics_module=None` DI kwarg (default: `yadgar.metrics`).
- Label format: `endpoint="/rerank/ce"` (full path, matching CB log field). V1c panel must use this form.

### Deploy (core only)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.5.3 .
# Bump nix yadger_core_version=5.5.3 + home-manager switch
systemctl --user restart yadgar
```

### Verify

```bash
curl -sS http://127.0.0.1:8765/metrics | grep yadgar_circuit_breaker_state
# Expected: 3 lines with endpoint="/rerank/{ce,nli,pair}" 0.0 (or higher on open)
```

---

## v5.5.2 — backend log metric wiring fix (2026-05-22)

### What changed

- **Bug fix:** backend `yadgar_log_file_size_bytes`, `yadgar_log_file_rotations_total`, and `yadgar_log_dropped_total` metrics now update correctly in the backend's own Prometheus registry.
- No new env vars, no public API changes, no schema changes.
- Core version: **5.5.1 → 5.5.2**. Backend version: **5.1.1 → 5.1.2**.

### Build + restart

```bash
# Core (5.5.2)
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.5.2 .
podman push docker.io/openfantasy/yadgar:5.5.2

# Backend (5.1.2)
podman build --arch amd64 -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.1.2 .
podman push docker.io/openfantasy/yadgar-backend:5.1.2
```

Update nix config — bump versions:
- Core image tag: `5.5.1` → `5.5.2`
- Backend image tag: `5.1.1` → `5.1.2`

```bash
systemctl --user restart yadgar yadgar-backend
```

## v5.5.1 — log rotation + rate limiter (2026-05-22)

### What changed

- **Dual-sink logging:** `configure_logging()` now installs a `RotatingJSONLFileHandler` (Sink B) alongside the existing stdout handler. Both sinks emit the same I14-conformant JSONL.
- **Rotation defaults:** 100 MB max per file × 5 backups = **500 MB cap per daemon**. Core: `/data/logs/yadgar.log`. Backend: `/data/logs/backend.log`. Both paths map through the existing `-v ~/.yadgar:/data` bind mount.
- **Rate limiter:** token-bucket filter at 10 records/sec burst 50 installed by default on all loggers. Drops increment `yadgar_log_dropped_total`. Disable via `YADGAR_LOG_RATE_LIMIT_ENABLED=0`.
- **3 new metrics:** `yadgar_log_file_rotations_total{logger}`, `yadgar_log_file_size_bytes{logger}`, `yadgar_log_dropped_total{logger,level,reason}` — exposed on both core `/metrics` and backend `/metrics`.
- Core version: **5.5.0 → 5.5.1**. Backend version: **5.1.0 → 5.1.1**.

### Pre-deploy operator action (REQUIRED)

```bash
mkdir -p ~/.yadgar/logs
```

Without this, the log dir is missing → file handler skipped → graceful stdout-only fallback (no crash, just no file sink).

### Env var reference

All vars apply to both core and backend unless noted. Backend prefers `YADGAR_BACKEND_LOG_*` over `YADGAR_LOG_*`; falls back to shared var; then to hardcoded default.

| Env var | Backend override | Default | Description |
|---------|-----------------|---------|-------------|
| `YADGAR_LOG_FILE_PATH` | `YADGAR_BACKEND_LOG_FILE_PATH` | `/data/logs/yadgar.log` (core) / `/data/logs/backend.log` (backend) | Active log file path. Set to `""` to disable file logging entirely. |
| `YADGAR_LOG_FILE_MAX_BYTES` | `YADGAR_BACKEND_LOG_FILE_MAX_BYTES` | `100000000` (100 MB) | Rotate when file exceeds this size. |
| `YADGAR_LOG_FILE_BACKUP_COUNT` | `YADGAR_BACKEND_LOG_FILE_BACKUP_COUNT` | `5` | Backup files to keep. Total cap = MAX_BYTES × BACKUP_COUNT. |
| `YADGAR_LOG_RATE_LIMIT_ENABLED` | — | `1` (enabled) | Set `0` or `""` to disable rate limiter. |
| `YADGAR_LOG_RATE_LIMIT_TOKENS_PER_SEC` | — | `10.0` | Token refill rate per (logger, level) bucket. |
| `YADGAR_LOG_RATE_LIMIT_BURST` | — | `50` | Burst capacity (tokens at start). |

### Disk budget

| Scenario | Core | Backend | Total |
|---------|------|---------|-------|
| Default (500 MB cap each) | ≤500 MB | ≤500 MB | **≤1 GB** |
| Reduced (3 × 50 MB each) | ≤150 MB | ≤150 MB | ≤300 MB |

Journald still accumulates separately. Recommended defense-in-depth cap:

```ini
# ~/.config/systemd/user/journald.conf.d/yadgar.conf
[Journal]
SystemMaxUse=2G
MaxRetentionSec=14day
```

### 1. Pre-deploy: create log directory

```bash
mkdir -p ~/.yadgar/logs
```

### 2. Rebuild images (user runs manually)

```bash
# Core 5.5.1
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.5.1 .
podman push docker.io/openfantasy/yadgar:5.5.1

# Backend 5.1.1
podman build --arch amd64 -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.1.1 .
podman push docker.io/openfantasy/yadgar-backend:5.1.1
```

### 3. Nix bump (operator)

After images are pushed, bump version pins in `~/git/nix/modules/home/yadgar.nix`:
- Core image tag: `5.5.0` → `5.5.1`
- Backend image tag: `5.1.0` → `5.1.1`

No new bind mounts needed — `/data/logs/` is under the existing `-v ~/.yadgar:/data` mount.
Optionally add `systemd.user.tmpfiles.rules = [ "d %h/.yadgar/logs 0750 - - -" ]` to automate `mkdir -p` at activation.

---

## v5.4.8 — middleware request-log visibility fix (2026-05-22)

### What changed

- `configure_logging()` now installs a dedicated always-INFO `StreamHandler` on `yadgar.requests` (propagate=False). Request log lines now emit regardless of root log level.
- Root cause was `CORE_LOG_LEVEL` defaulting to `"warn"` → root handler at WARNING → `yadgar.requests` INFO records silently dropped.
- Secondary finding: `YADGAR_LOG_LEVEL` is NOT a valid Settings env var. The correct var is `YADGAR_CORE_LOG_LEVEL` (maps to `Settings.CORE_LOG_LEVEL`). Neither `yadgar.service` nor `docker-compose.yml` set it. Add it if full INFO visibility on all `yadgar.*` loggers is needed.
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### Operator action (recommended)

Add `YADGAR_CORE_LOG_LEVEL=info` to your deployment to enable INFO logging on all yadgar.* loggers (not just yadgar.requests):

**systemd** (`~/.config/systemd/user/yadgar.service`):
```ini
Environment=YADGAR_CORE_LOG_LEVEL=info
```

**docker-compose** (`docker-compose.yml`, under `core.environment`):
```yaml
YADGAR_CORE_LOG_LEVEL: info
```

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.8 .
podman push docker.io/openfantasy/yadgar:5.4.8
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.8";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

---

## v5.4.7 — I14 ratchet cleanup (2026-05-22)

### What changed

- `RequestLoggingMiddleware` migrated to I14 schema:
  - `duration_ms` → `latency_ms` (**BREAKING RENAME** — update Loki/Grafana queries)
  - `status` → `http_status`
  - Added: `component="http_server"`, `action="request"`, `outcome` ("ok"/"error"/"degraded")
  - Kept: `request_id`, `tool_name`, `trace_id`
- `ContentRedactor` denylist tightened (two-tier):
  - Exact-match only: `content`, `auth`, `token`, `secret`, `bearer`
  - Substring match: `password`, `api_key`, `authorization`, `access_token`, `refresh_token`, `client_secret`, `private_key`
  - `content_type`, `content_length` no longer falsely redacted
- `_outcome_from_status` helper: `2xx/3xx→"ok"`, `"cancelled"→"degraded"`, all else→`"error"`
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### Dashboard migration (BREAKING — action required)

If you have Loki/Grafana queries reading `duration_ms` from `yadgar.requests` logs, update to `latency_ms`:

```logql
# Before
{job="yadgar"} | json | __error__="" | duration_ms > 1000

# After
{job="yadgar"} | json | __error__="" | latency_ms > 1000
```

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.7 .
podman push docker.io/openfantasy/yadgar:5.4.7
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.7";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

---

## v5.4.6 — LOW-risk complexity refactor batch (2026-05-22)

### What changed

- 4 functions decomposed per P12 complexity audit (all LOW-risk):
  - `insert_typed_relationship` — params 9→4 via `RelationshipMeta` dataclass
  - `insert_new_memory` — params 12→4 via `NewMemorySpec` dataclass
  - `create_checkpoint` — params 9→3 via `CheckpointContext` dataclass
  - `cmd_config` — nesting 6→1, cyclo 7→3 via dispatch dict
- `pyproject.toml` per-file-ignores: 2 files fully removed, 1 partial (PLR0913 only)
- `.complexity-baseline.json` regenerated (4667 → 4819 entries)
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.6 .
podman push docker.io/openfantasy/yadgar:5.4.6
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.6";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

---

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

Units generated by `make setup` (via `scripts/install/generate_systemd.sh`) use `EnvironmentFile=/etc/yadgar/secrets.env`. (`scripts/setup.sh` was deleted in v5.45.0.)
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

### Verify `YADGAR_MCP_AUTH_TOKEN` is wired

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

## v5.4.3 — I14 framework-logger coverage + ruff grandfathering (2026-05-22)

### What changed

- `configure_logging()` now covers ALL framework loggers (uvicorn, mcp, fastmcp, httpx, starlette) via root-logger approach. Core daemon plain-text log lines are gone.
- 31 pre-existing C901/PLR0913 ruff violators grandfathered in `pyproject.toml` per-file-ignores (b27d218 gap). Refactor target: v5.4.4.
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.3 .
podman push docker.io/openfantasy/yadgar:5.4.3
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.3";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

### 4. Verify JSON logging covers framework lines

```bash
journalctl --user -u yadgar -n 50 --output=cat | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: json.loads(line); print('OK:', line[:80])
    except: print('NOT JSON:', line[:80])
"
```

All lines (including uvicorn.access, mcp, fastmcp) should parse as JSON.

### 5. New env var: YADGAR_LOG_FORMAT=human

For local dev (non-JSON output from ALL loggers):
```bash
YADGAR_LOG_FORMAT=human python -m yadgar --transport streamable-http
```

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

> Superseded by ADR-0106 (2026-07-13): the standing backend config is now `--cpus 3` (not the 4 shown in the F5-C example above). See the T4 train ops at the top of this file.

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
# Resolves version from server.json automatically:
python scripts/check_image_size.py --image-type backend
python scripts/check_image_size.py --image-type core

# Or pass a specific image ref directly:
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

---

## v5.5.0 — V1a backend /metrics endpoint (2026-05-22)

**What ships:** `yadgar/embed_service_metrics.py` + GET `/metrics` on `yadgar.embed_service:app`.
Core version 5.4.5 → 5.5.0. Backend version 5.0.3 → 5.1.0.
`prometheus_client` was already a main dep — no new package install needed.

### 1. Build both images

```bash
# Backend (5.1.0) — embed_service_metrics.py is part of shared package
podman build -f Dockerfile.backend -t openfantasy/yadgar-backend:5.1.0 .

# Core (5.5.0)
podman build -f Dockerfile -t openfantasy/yadgar:5.5.0 .
```

### 2. Nix bump — update both versions in nix config

In `~/git/nix` (adjust paths to match your nix module):

```bash
# Bump yadgar version 5.4.5 → 5.5.0
# Bump yadgar-backend version 5.0.3 → 5.1.0
# Then:
home-manager switch
```

### 3. Restart services

```bash
systemctl --user restart yadgar-backend.service
systemctl --user restart yadgar.service
```

### 4. Verify /metrics endpoint

```bash
# Should return 200 with Prometheus text output
curl -s http://127.0.0.1:8001/metrics | grep yadgar_embed

# Expected families:
# yadgar_embed_rerank_requests_total{mode="ce|nli|pair"}
# yadgar_embed_rerank_503_total{mode="ce|nli|pair"}
# yadgar_embed_rerank_duration_seconds{mode="ce|nli|pair"}
# yadgar_embed_rerank_semaphore_held{mode="ce|nli|pair"}
# yadgar_embed_model_loaded{model="ce|nli|pair|embedding"}
# process_* (RSS, CPU seconds, open FDs)
# python_info
```

### 5. Prometheus scraper config (example)

`/metrics` is **unauthenticated** on port 8001 (loopback only — `127.0.0.1:8001:8001`).
Prometheus scrapers on localhost need no bearer token.

```yaml
# prometheus.yml scrape config — add to scrape_configs:
- job_name: yadgar-backend
  static_configs:
    - targets: ['127.0.0.1:8001']
  metrics_path: /metrics
  scrape_interval: 15s
```

**Security note:** port 8001 is bound to loopback (`127.0.0.1`) only, per `docker-compose.yml`.
External hosts cannot reach it without an explicit port forward. No auth needed on loopback-only endpoints.

### 6. Image size check (post-build)

`prometheus_client` adds ~100 KB to the backend image. Backend should remain well under the 2.0 GB cap.

```bash
python scripts/check_image_size.py --image-type backend
```

---

## v5.7.0 PR-4 — vacuum_now() trigger-file pattern (2026-05-26)

### Why

The previous `vacuum_now()` MCP tool called `systemctl --user start --no-block yadgar-vacuum.service`
directly from inside the yadgar process.  When yadgar runs in a container, this systemctl call cannot
reach the host's systemd session (no dbus socket mounted into the container) — the trigger silently
failed or raised a RuntimeError.

The fix is a clean container ↔ host separation: yadgar writes a trigger file; a host-side systemd
path-watch unit picks it up and starts `yadgar-vacuum.service`.

### What changed in yadgar

- `yadgar/ops.py` — `_fire_vacuum_service()` now writes an atomic trigger file instead of calling
  systemctl.  The file path is controlled by `YADGAR_VACUUM_TRIGGER_PATH` (default:
  `/data/triggers/vacuum_requested`).  The file contains one-line JSON:
  `{"requested_at": "<ISO8601>", "source": "vacuum_now"}`.  Write is atomic (`*.tmp` then
  `os.replace()`).  Parent directory is created if missing.  Returns the `Path` written; raises
  `RuntimeError` on I/O failure.
- `yadgar/server/tools/admin_vacuum.py` — vacuum_now() MCP tool simplified: removed
  `detect_service_mode()` check, `is-active` subprocess check, and all `host_command` /
  `service_unit` / `shell_command` fields.  New response field: `trigger_path` (str path written,
  or `None` when skipped).
- `yadgar/config_registry.py` — new knob `YADGAR_VACUUM_TRIGGER_PATH` (default
  `/data/triggers/vacuum_requested`, kind=string) registered; appears in `/admin/config`.

### Host-side requirement (separate nix-repo change)

A systemd path-watch unit pair is needed on the host to bridge the trigger file to the service.
This is tracked as a follow-up in `~/git/nix/modules/home/yadgar.nix`.

Example unit fragments (do NOT apply manually — add via the nix module):

**`yadgar-vacuum-trigger.path`**:
```ini
[Unit]
Description=Watch for yadgar vacuum trigger file
After=yadgar.service

[Path]
PathExists=/data/triggers/vacuum_requested
Unit=yadgar-vacuum-trigger.service

[Install]
WantedBy=default.target
```

**`yadgar-vacuum-trigger.service`** (one-shot, removes trigger file then starts vacuum):
```ini
[Unit]
Description=Dispatch yadgar vacuum on trigger-file appearance

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'rm -f /data/triggers/vacuum_requested && systemctl --user start yadgar-vacuum.service'
```

Enable the path unit:
```bash
systemctl --user daemon-reload
systemctl --user enable --now yadgar-vacuum-trigger.path
```

### Graceful degradation

Until the path-watch unit is deployed, `vacuum_now()` still writes the trigger file and returns
`started=True` — the file simply accumulates until the watcher is wired.  No error is surfaced to
the caller; the only observable effect is that vacuum doesn't actually start.  Operators can check
for a stale trigger file at `/data/triggers/vacuum_requested` as a diagnostic signal.

### Known gap: auto-trigger in containers

`consolidation/__init__.py:218-231` contains its own `systemctl is-active` pre-check that returns
early on `FileNotFoundError`.  This means the auto-vacuum trigger (via `ConsolidationScheduler`)
is still skipped in containerized deploys — it calls `_fire_vacuum_service` but only after a
systemctl check that fails first.  Fixing the consolidation pre-check is out of scope for PR-4.

---

## v5.7.0 PR-5 — VACUUM_AUTO_THRESHOLD_BYTES is an emergency backstop (docs-only) (2026-05-26)

### Mental model change

Before v5.7.0 `VACUUM_AUTO_THRESHOLD_BYTES` was the **primary** vacuum trigger: vacuum fired when
the DB grew beyond the threshold inside the configured time window.  This was also the only
scheduled path; `vacuum_now()` existed for manual one-offs.

From v5.7.0, a **nightly cron at 19:00 UTC** (`yadgar-vacuum.timer`, shipping in PR-1a/1b) becomes
the primary trigger.  It runs unconditionally — DB size does not matter.

The threshold-driven path in `ConsolidationScheduler._maybe_auto_vacuum()` now plays a **narrower,
emergency-only role**: it catches runaway DB growth that happens between nightly cron cycles (e.g.
a bulk import that pushes the DB past 2 GiB at 22:00).  The per-day cooldown in that function
prevents double-fires on days when both cron and the threshold would trigger.

### Trigger precedence summary (v5.7.0+)

1. **Primary** — nightly cron at 19:00 UTC (`yadgar-vacuum.timer`).  Unconditional.
2. **Emergency backstop** — `VACUUM_AUTO_THRESHOLD_BYTES` (default 2 GiB) + window guard in
   `_maybe_auto_vacuum()`.  Fires only between cron cycles when DB exceeds the threshold.
3. **Manual** — `vacuum_now()` MCP tool writes a trigger file (see PR-4 above).

### No config migration required

All four `VACUUM_AUTO_*` knobs remain unchanged and functional:

| Knob | Default | Role |
|---|---|---|
| `VACUUM_AUTO_ENABLED` | `True` | Enable/disable the backstop path entirely. |
| `VACUUM_AUTO_THRESHOLD_BYTES` | `2147483648` (2 GiB) | DB size that arms the backstop. |
| `VACUUM_AUTO_WINDOW_START` | `19:00` | Backstop fires only after this local time. |
| `VACUUM_AUTO_WINDOW_END` | `23:00` | Backstop fires only before this local time. |

Existing deployments keep working without any env-var changes.  The only observable difference is
that vacuum will also fire at 19:00 UTC once `yadgar-vacuum.timer` is deployed (PR-1a/1b).

---

# Orchestration safety net (2026-06-14)

Prevents the failure mode where an unattended test/gate run hangs and pegs the
machine for hours (one ran 9.7h overnight, all cores ~95%). Three layers.
Run these yourself — infra/system changes are never auto-applied.

## 1. Make the scripts executable
```bash
cd ~/git/yadgar && chmod +x scripts/reap-stale-tests.sh scripts/test-capped.sh
```

## 2. Install + enable the watchdog timer (systemd --user)
Kills any test process older than 90min, every 10min. Skips the production
daemon (surreal on /data/surreal_db).
```bash
mkdir -p ~/.config/systemd/user
cp ~/git/yadgar/deploy/systemd/reap-stale-tests.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reap-stale-tests.timer
systemctl --user list-timers reap-stale-tests.timer        # verify
~/git/yadgar/scripts/reap-stale-tests.sh                   # sanity: prints "killed 0"
```
(If the repo is not at `~/git/yadgar`, edit `ExecStart=` in the .service first.)

## 3. Use the capped wrapper for long runs
`make test` now routes through it. Ad-hoc:
```bash
scripts/test-capped.sh uv run --extra test pytest yadgar/tests/ -q -n 4
```
Hard-killed after 90min; capped to ~3 cores / 20G (headroom for production).

`pyproject.toml` already sets `timeout_method = "signal"` (committed) so the 300s
per-test timeout can actually kill a deadlocked test (the thread method could not).

---

## v5.56.0 release

### What publishes and how

**PyPI `yadgar 5.56.0`** — published AUTOMATICALLY by CI on tag push `v5.56.0`. The publish job is `needs: test`; it runs without any manual dispatch once the tag is pushed.

**Container images `openfantasy/yadgar:5.56.0` + `openfantasy/yadgar-backend:5.6.0`** (amd64 + arm64) — the `build` job is `workflow_dispatch` ONLY. It is NOT triggered by the tag push. After the tag is pushed and CI passes, manually dispatch the build workflow from the Forgejo UI (or `gh workflow run`) to produce the container images.

**nix** — NOT handled by CI or Claude. Run `nix-update` / flake bump yourself after the PyPI package is published and the container images are available. Update both the core version (`5.56.0`) and the backend image tag (`5.6.0`) in the nix module.

### Orchestration-safety watchdog systemd units

The watchdog scripts and systemd units shipped in `chore/orchestration-safety` (see section above). If not yet installed, follow the steps in the "Orchestration safety net" section above — do NOT install them automatically; run the commands yourself.

---

## v5.57.0

### Forgejo branch protection — MUST configure manually

The new production CI split (`validate.yaml` / `ci-pr.yaml` / `ci-release.yaml`) requires branch
protection on `master` to enforce the PR-gated checks. Without protection, direct pushes to master
bypass test gating.

**Steps (Forgejo → Settings → Branches → Add Rule for `master`):**

1. Enable "Protect this branch"
2. Enable "Require status checks to pass before merging"
3. Add required checks:
   - `validate / validate`
   - `CI (PR) / test`
   - `CI (PR) / viz-tests`
   - `CI (PR) / verify-version-bump`
4. Enable "Restrict pushes that create matching branches" (PRs only; no direct pushes)
5. Save

**Why this is safe:** `ci-release.yaml` does NOT re-run tests on master push — it assumes the PR
checks already passed. Branch protection is what makes that assumption valid. Without it, a direct
push bypassing CI could trigger a publish to PyPI without test gating.

### nix / flake bump — manual

After the v5.57.0 PyPI package publishes and container images are available, bump the nix module:
- Core version: `5.56.0` → `5.57.0`
- Backend image tag: `5.6.0` (unchanged — no backend build input changes in v5.57.0)

Run `nix-update` or edit `flake.nix` manually. Claude does not do this.

---

## SurrealDB server upgrade v3.0.5 → v3.1.5 (branch `chore/surrealdb-3.1.5`)

Plan: `docs/plans/surrealdb-3.1.5-upgrade-plan-2026-06-30.md`. The code/pin edits are committed
on the branch. **Everything below is infra the USER must run — Claude does NOT execute any of it.**

### What changed in code (already committed)

- SurrealDB **server binary** `v3.0.5` → `v3.1.5`: `Dockerfile.backend:20`, `Dockerfile.ci:38`
  (`SURREAL_VERSION`) + `:39` (`SURREAL_SHA256`), `scripts/install/restore.sh:153`, comments
  `Dockerfile.ci:7,36`.
- **Verified SHA256** of `surreal-v3.1.5.linux-amd64.tgz` (linux-amd64):
  `f7d515203ba0010bde3fc6a5706ce7327d356aca293fbba8424d442f5dcb5002`
  (downloaded twice from the official GitHub release `v3.1.5`, hashes identical, tarball
  contains the `surreal` binary).
- Image tags re-rolled (surreal is baked into both images):
  - **backend** `yadgar-backend` 5.8.0 → **5.9.0**: `docker-compose.yml:39`,
    `nix/modules/home/yadgar.nix:18` (committed separately in the `nix` repo, NOT pushed).
  - **CI** `yadgar-ci` 5.72.0 → **5.73.0**: `Dockerfile.ci` LABEL + all functional refs in
    `.forgejo/workflows/{ci-pr,eval,ci-release}.yaml` (`yadgar-ci-viz:5.46.9` left untouched —
    different image, no surreal baked in).

### Deploy sequence (ORDERED — run as the user)

**Step 0 — BACK UP THE SURREAL DATADIR FIRST (mandatory; rollback depends on it).**
3.1.5 → 3.0.5 in-place binary downgrade is **NOT supported** — 3.1 may persist metadata 3.0.5
cannot read. The ONLY reliable rollback is restore-from-backup. Take BOTH a logical dump and a
volume snapshot from the running `yadgar-backend`:

```bash
# logical export (run against the LIVE 3.0.5 backend before stopping it)
docker exec yadgar-backend surreal export \
  --endpoint http://127.0.0.1:8000 \
  --username "$ROOT_USER" --password "$ROOT_PASS" \
  --namespace yadgar --database main \
  /tmp/surreal-pre-3.1.5.surql
docker cp yadgar-backend:/tmp/surreal-pre-3.1.5.surql ./backups/

# AND a raw volume snapshot of the datadir (/data/surreal_db)
docker run --rm -v yadgar_surreal_data:/data:ro -v "$PWD/backups:/backup" \
  alpine tar czf /backup/surreal_db-pre-3.1.5.tgz -C /data .
```
Store both off-box. Re-confirm freshness immediately before Step 3.

**Step 1 — Build + push the new CI image** (`yadgar-ci:5.73.0`, surreal 3.1.5 baked in). The
`SURREAL_SHA256` build arg is already set in `Dockerfile.ci`; the build runs `sha256sum -c`
and fails closed on mismatch.

> **PR-MERGE PREREQUISITE (not just a deploy step):** the branch's `.forgejo/workflows`
> already point at `yadgar-ci:5.73.0`. That image does NOT exist in the registry until this
> step runs. Opening/merging the PR before pushing `yadgar-ci:5.73.0` → CI fails with an
> image-pull error on `ci-pr.yaml` jobs **before any test runs**. Build + push this image
> FIRST, then open/re-run the PR.

```bash
docker build -f Dockerfile.ci -t docker.io/openfantasy/yadgar-ci:5.73.0 .
docker push docker.io/openfantasy/yadgar-ci:5.73.0
# smoke: confirm surreal 3.1.5 is on PATH inside the image
docker run --rm docker.io/openfantasy/yadgar-ci:5.73.0 surreal version
```

**Step 2 — Build + push the new backend image** (`yadgar-backend:5.9.0`, surreal 3.1.5). Backend
and CI builds are independent (different Dockerfiles) — can run in parallel.

```bash
docker build -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.9.0 .
docker push docker.io/openfantasy/yadgar-backend:5.9.0
```

**Step 3 — Point nix/compose at the new tags + rebuild.** The tag defaults are already bumped on
the branch (`docker-compose.yml:39` → 5.9.0; `nix/modules/home/yadgar.nix:18` → 5.9.0). Apply the
nix switch yourself (Claude does NOT run nix):

```bash
# nix repo change is committed (NOT pushed) on master of /home/max/git/nix
cd /home/max/git/nix && git push   # if you want it upstream first
home-manager switch   # or: sudo nixos-rebuild switch --flake .#<host>
# compose deployments:
BACKEND_VERSION=5.9.0 docker compose up -d backend
```

**Step 4 — Deploy ordering against the running datadir + verify on 3.1.5:**
1. Re-confirm the Step-0 backup is fresh.
2. Stop the old `yadgar-backend` (3.0.5) gracefully — it holds the `surrealkv` lock.
3. Start `yadgar-backend:5.9.0` (surreal 3.1.5) against the **same** datadir volume — in-place
   roll-forward, **no migration command** (on-disk format unchanged 3.0→3.1).
4. `GET /health/live` (liveness) then a probe `POST /sql` with the real headers
   (`Authorization: Basic …`, `Surreal-NS: yadgar`, `Surreal-DB: main`,
   `Accept: application/json`, `Content-Type: text/plain`, body `SELECT * FROM wiki_page;`) —
   confirms auth + read path on 3.1.5.
5. Soak. The server bump is independent of the async refactor; ship it standalone first.

**Step 5 — e2e on 3.1.5 (the real verification gate).** SQL-semantics compat for the `/sql`
surface is INFERRED (patch release + GraphQL-only announced break), not line-by-line verified —
the e2e suite IS the verification. The harness must run against surreal **3.1.5**, not 3.0.5:
put `surreal v3.1.5` on PATH locally (or use `yadgar-ci:5.73.0`) and run `make e2e`. Re-check the
known 3.0.5 SQL workarounds (`IS NONE` semantics, partial-unique-index drop, `IS NOT NONE`) did
not silently change.

**Rollback (if needed) = RESTORE THE BACKUP, NOT a binary downgrade:**
1. Redeploy the 3.0.5 images (`yadgar-backend:5.8.0`, `yadgar-ci:5.72.0`) — revert the branch.
2. Restore the Step-0 backup onto a **clean** datadir running 3.0.5 (logical `surreal import`
   of the `.surql` dump, or restore the volume snapshot). Do **NOT** point 3.0.5 at a datadir
   that 3.1.5 has already written to.

### Residual UNVERIFIED item (carried from the plan)

- **`/sql` SQL-semantics across 3.0→3.1: INFERRED, not line-by-line verified.** The full 3.1.x
  changelog is not machine-retrievable; "no break" is inferred from the patch nature + the
  GraphQL-only announced break. **The Step-5 e2e on 3.1.5 is the empirical gate.** (Note: the
  auth/header surface, previously flagged unverified, is now VERIFIED unchanged against the
  current 3.x HTTP docs — it is NOT an open risk.)

---

## Phase 2b: stdio transport dropped — MCP client config migration required

**Applies to:** any existing Claude Code MCP config that uses stdio transport for yadgar.

**Old config (no longer works):**
```json
{
  "mcpServers": {
    "yadgar": {
      "command": "yadgar",
      "args": ["--transport", "stdio"]
    }
  }
}
```
or bare `{"command": "yadgar", "args": []}` (relied on stdio being the default).

**New config (streamable-HTTP, run `yadgar daemon start` first):**
```json
{
  "mcpServers": {
    "yadgar": {
      "type": "streamable-http",
      "url": "http://localhost:8765/mcp",
      "headers": {"Authorization": "Bearer ${YADGAR_MCP_AUTH_TOKEN}"}
    }
  }
}
```

Run `yadgar daemon configure-mcp` to write this automatically, or merge manually into `~/.claude.json`.
The bearer token is in `/etc/yadgar/secrets.env` (generated by `yadgar setup`).

**Why:** stdio is no longer a valid transport (removed from `VALID_TRANSPORTS`). The deployed transport
is streamable-HTTP; stdio was a Docker-unavailable fallback that is no longer supported.

---

## R3 — shared queue volume for the backend drainer (prod deploy action)

**When:** before deploying the R3 build (write-path drainer moved core→backend).

The queue drainer now runs INSIDE the backend container, so backend needs read/write access to the
file queue (`queue/`, `archive/`, `dlq/`). Dev `docker-compose.yml` is already updated (backend mounts
`yadgar-queue-data:/queue-data` + `YADGAR_QUEUE_BASE=/queue-data`). **Production (nix/systemd `docker run`,
out-of-repo) needs the equivalent — apply manually:**

Add to the backend container's `docker run` (or its nix `virtualisation.oci-containers` unit):

```
  --volume yadgar-queue-data:/queue-data \     # same named volume core mounts at /data → both see <vol>/queue
  --env   YADGAR_QUEUE_BASE=/queue-data
```

- Backend's `/data` stays the read-only DB mount; the queue is a SEPARATE rw mount at `/queue-data`.
- Core is unchanged (mounts `yadgar-queue-data:/data`, `YADGAR_DATA_DIR=/data` → queue at `/data/queue`).
  With `YADGAR_QUEUE_BASE` unset, core falls back to `YADGAR_DATA_DIR` — same `<volume>/queue` directory.
- Backend keeps `read_only: true`; a named-volume mount is writable under a read-only root fs.

**Verify after deploy:** backend can write `/queue-data/queue/*.json`; a `memorize` enqueued by core is
drained by the backend (job moves `queue/`→`archive/`); `wait=True` returns.

---

## R3 — deployment prerequisites summary (core 5.117.0 / backend 5.30.0)

R3 completes the core→backend write-path split. Core is now a thin **router**; the backend is the
**compute** node (embeddings, drainer, consolidation, admin). Before deploying the R3 build, confirm all
three prerequisites — hand these to whoever owns the prod deploy (no infra apply from here):

1. **Backend REQUIRES the queue volume mount + `YADGAR_QUEUE_BASE`.** See the section above — the drainer
   runs inside the backend, so the backend needs rw access to the shared queue volume
   (`--volume yadgar-queue-data:/queue-data --env YADGAR_QUEUE_BASE=/queue-data`). Without it, the backend
   cannot drain enqueued writes and `wait=True` never returns.

2. **Core REQUIRES `YADGAR_EMBED_URL` reachable.** ALL write / recall / admin / consolidate operations now
   forward from core to the backend `/…` HTTP endpoints. If the backend is down or `YADGAR_EMBED_URL` is
   unset/unreachable, **core operations fail** — this is intended (core = router, backend = compute), not a
   regression. Set `YADGAR_EMBED_URL` to the backend's reachable base URL on the core container/unit.

3. **Nightly consolidation cron needs `YADGAR_EMBED_URL` in its env.** Consolidation now forwards to the
   backend too. The nightly cron/systemd-timer that triggers consolidation must have `YADGAR_EMBED_URL` set
   in its environment, or the consolidation pass fails to reach the compute node.

---

## Data-dir hygiene — one-time migration (core 5.118.0 / backend 5.31.0, ADR-0076)

**Root causes this fixes:**
1. `surreal_db.old-*` accumulation (~5.5 GB, 23 dirs): `_vacuum_finalize()` reaped `.old` only on
   `check_invariants` PASS, but PR #173's 120 s per-op timeout was merged in the same window — every nightly
   since June had a 34 s `check_invariants` vs 30 s client timeout, meaning CI never passed and `.old` was
   never retired. PR #173 fixed the timeout; this plan adds the `VACUUM_OLD_MAX_AGE_DAYS=7` age-backstop so
   accumulation cannot happen again even if CI is slow.
2. `vacuum_export_*.surql` orphans (~3.5 GB, 54 files, ~70 MB each): mid-vacuum scratch written by
   `_vacuum_export()` but never deleted. No retention anywhere. This build deletes them on successful finalize.
3. Wiki JSONL volume (~900 MB, 42 files at 6-hourly cadence): cadence now 24 h; output moves to
   `backups/wiki/`. Retention stays 14 d.
4. Nightly surql dumps at volume root (3 files, ~65 MB): path moves to `backups/surql/` on next run.

**One-time migration — run these commands manually (never executed by Claude):**

```bash
DATA="$HOME/.local/share/yadgar"

# 1. Create new layout dirs
mkdir -p "$DATA/backups/surql" "$DATA/backups/wiki"

# 2. Move existing nightly surql backups into backups/surql/
mv "$DATA"/surreal_db.nightly-*.surql "$DATA/backups/surql/" 2>/dev/null || true

# 3. Move existing wiki JSONL snapshots into backups/wiki/
mv "$DATA"/wiki_*.jsonl "$DATA/backups/wiki/" 2>/dev/null || true

# 4. DELETE all orphaned vacuum_export_*.surql scratch files (~3.5 GB, 54 files)
rm -f "$DATA"/vacuum_export_*.surql "$DATA"/vacuum_export_*.filtered.surql

# 5. DELETE excess surreal_db.old-* dirs — keep 3 newest, delete 20 oldest (~4.8 GB)
#    (Keep the 3 newest)
ls -dt "$DATA"/surreal_db.old-* | tail -n +4 | xargs rm -rf

# 6. Verify
du -sh "$DATA/"
ls "$DATA/backups/surql/" "$DATA/backups/wiki/"
```

**Sizes to delete (estimated):**
| Artifact | Count | Size |
|---|---|---|
| `vacuum_export_*.surql` + `.filtered.surql` | varies | ~70 MB each |
| `surreal_db.old-*` (excess beyond newest 3) | varies | ~240 MB each |
| **Total freed** | | varies |

**Post-deploy verification:**
- First nightly run writes pre/post backups to `~/.local/share/yadgar/backups/surql/`, prunes to 3.
- Vacuum finalize: export scratch files absent after CI pass; `surreal_db.old-*` count ≤ 1 + age-backstop.
- Container: one new wiki JSONL per day under `/data/backups/wiki/`; count trends to ≤ 14.
- `du -sh ~/.local/share/yadgar/` should be ≈ 2.5 GB (surreal_db + 3 backups + wiki 14-day window).
