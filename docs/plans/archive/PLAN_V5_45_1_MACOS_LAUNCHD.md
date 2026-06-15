# PLAN — v5.45.1: macOS launchd Plist Generation + Install

## Implementation status

**SHIPPED PAPER-ONLY — 2026-06-04.**

All Steps 1–5 implemented. Cross-platform render tests pass on Linux (54 pass, 5 skipif darwin). Runtime verification deferred: no macOS host was available. Fix-ups via hotfix.

**Deferred verification:** Run the 5 probes in `MIGRATION_NOTES.md` v5.45.1 section on first macOS host access. See `docs/DECISIONS.md` PD-38 for formal deferral record.

**Delivered files:**
- `scripts/install/launchd/com.openfantasy.yadgar.plist.in`
- `scripts/install/launchd/com.openfantasy.yadgar-backend.plist.in`
- `scripts/install/generate_launchd.sh`
- `scripts/install/detect_os.sh` — added `YADGAR_TEST_OS_MARKER` hook
- `scripts/install/detect_runtime.sh` — added `YADGAR_TEST_PODMAN_MACHINE_SOCKET` sentinel (DP-C)
- `scripts/install/uninstall.sh` — macOS path (launchctl + rm plists + logs on --purge)
- `Makefile` — `enable-units-macos`, `_enable-units-auto`, OS-routing in `setup`
- `yadgar/tests/test_v5_45_1_*.py` — 6 test modules (54 pass, 5 darwin-skipif)

---

**Status (original):** skeleton drafted 2026-06-04. Split from v5.45.0 per opus-reviewer. Plan-first per I27.

**Parent plan:** `docs/PLAN_V5_45_0_SETUP_FOUNDATION.md` (Step 4 — macOS launchd path, scope-cut out).

**Effort estimate:** TBD (pending verifying host). See DP-A.

**Split rationale:** v5.45.0 cut to Linux-only. macOS launchd path requires a verifying host for smoke-test. Dispatch only once a macOS machine is confirmed available.

**Ships in train:** after v5.45.0. Before v5.46.0 distribution (Homebrew tap targets macOS users — launchd daemon must work before brew formula ships).

---

## Goal

Generate and install launchd plists for the yadgar core + backend daemons on macOS. Provides the macOS equivalent of v5.45.0's `generate_systemd.sh`.

- `yadgar-setup` on macOS auto-detects OS (`detect_os.sh` → `macos`) and runs `generate_launchd.sh`. (`make setup` on macOS via repo checkout also routes through this path.)
- Plists install to `~/Library/LaunchAgents/` (per-user, no root required).
- `launchctl bootstrap gui/$UID` (Catalina+) with `launchctl load` fallback.
- Auto-restart on crash (`KeepAlive=true`).
- Log paths routed to `~/Library/Logs/yadgar/`.

---

## Non-goals

- No system-level (`/Library/LaunchDaemons/`) install. Per-user only.
- No brew service integration (`brew services start`). Manual launchd only in v5.45.1; brew integration is v5.46.0.
- No Windows / WSL2 path.

---

## Open DPs Blocking Dispatch

**DP-A — Verifying host availability.**
- macOS smoke test cannot be mocked fully (launchctl behavior, `sw_vers`, `plutil -lint`).
- Implementer MUST have a macOS Ventura+ machine to verify plist load/unload cycle.
- Resolve before dispatch: confirm host availability in pre-flight Step 0.

**DP-B — `launchctl bootstrap` vs `launchctl load`.**
- `bootstrap gui/$UID` is the Catalina+ API (macOS 10.15+). `load -w` is deprecated but more compatible.
- Lean (from v5.45.0 §1 open question): `bootstrap` for macOS 11+, `load` fallback (probe via `sw_vers -productVersion | cut -d. -f1`).
- Confirm at implementation via real launchctl behavior on host.

**DP-C — Podman-on-macOS via `podman machine`.**
- `podman info` health check must work via `podman machine` socket.
- Socket path differs from Linux. `detect_runtime.sh` may need a macOS-specific socket probe.
- Resolve via direct test on macOS host.

**DP-D — `KeepAlive` vs `OnDemand`.**
- v5.45.0 lean: `RunAtLoad=true KeepAlive=true`.
- Confirm this doesn't conflict with user's manual stop (`launchctl unload`).

---

## Architecture Conformance (P1)

Cites `docs/architecture.md`:

- **Transport Modes §**: daemon runs in streamable-HTTP mode on macOS (same as Linux). Plist must set `--transport streamable-http --port 8765`.
- **Docker Deployment §**: `yadgar-core` + `yadgar-backend` are the two container services. Two plists required: `com.openfantasy.yadgar.plist` + `com.openfantasy.yadgar-backend.plist`.
- **Module Responsibilities §** (`daemon.py`): `check_runtime()` (post-v5.45.0 rename from `check_docker()`) handles runtime detection. macOS path must call the same function.
- `docs/architecture.md` install asset paths (from v5.45.0 §Scope): `scripts/install/launchd/com.openfantasy.yadgar.plist.in` + `com.openfantasy.yadgar-backend.plist.in`.

## Proposed Architecture Updates

None. v5.45.0 already specifies the launchd asset paths in the install layout.

---

## Touched Invariants (P2)

| Invariant | Verb | Notes |
|---|---|---|
| I9 (daemon start latency budget) | **preserves** | Launchd load is async; daemon start latency governed by existing `--transport streamable-http` startup path, not launchd mechanics. |
| I23 (Prometheus metric availability post-start) | **preserves** | `/metrics` endpoint available after daemon starts. macOS path must verify `/metrics` responds before `yadgar-setup` (or `make setup`) reports success (same as Linux acceptance check). |
| I25 (three-way-sync registry) | **preserves** | No new config knobs in v5.45.1. `YADGAR_CONTAINER_RUNTIME` knob (from v5.45.0) applies unchanged. |

---

## Config Knob Lifecycle (P3)

No new config knobs. macOS detection uses `detect_os.sh` (v5.45.0 deliverable). `YADGAR_CONTAINER_RUNTIME` override unchanged.

---

## Schema Constraint Lifecycle (P4)

No schema changes.

---

## MCP Contract Changes (P5)

No MCP changes. CLI-side only.

---

## Cross-Plan Coordination (P6)

| Plan | Relationship |
|---|---|
| `docs/PLAN_V5_45_0_SETUP_FOUNDATION.md` | Parent. `generate_launchd.sh` is Step 4 deliverable from v5.45.0 — shipped as a stub in v5.45.0 or deferred entirely to v5.45.1. Coordinate: v5.45.0 implementer should leave `scripts/install/generate_launchd.sh` as an empty stub (not panic) when `macos` is detected, so v5.45.1 can fill it in without a breaking change. |
| `docs/PLAN_V5_46_0_DISTRIBUTION.md` | Homebrew formula (`Formula/yadgar.rb`) `caveats` block tells users to run `yadgar-setup`. On macOS, `yadgar-setup` calls `generate_launchd.sh` (v5.45.1 deliverable). Launchd daemon must be runtime-verified before brew users run `yadgar-setup` on macOS. Post-v5.46.0: brew formula does NOT call `yadgar-setup` automatically; user runs manually per brew caveat. Sequence: v5.45.1 macOS runtime verification MUST complete before v5.46.0 brew tap creation. |

No migration number conflicts.

---

## Bug Class Precedent (P7)

**Precedent 1 — launchd vs systemd semantics:** launchd has no `daemon-reload` equivalent; plist changes require `unload` + `load` cycle. The `generate_launchd.sh` script must unload before regenerating and reloading. Failure to unload first leaves stale jobs registered.

**Precedent 2 — `plutil` validation gap:** systemd has `systemd-analyze verify`; launchd has `plutil -lint`. v5.45.0 acceptance criterion requires `plutil -lint` pass. Tests on Linux can render the plist (template substitution is OS-agnostic) but only macOS can `plutil -lint`. Mark `plutil` test as `skipif(sys.platform != "darwin")` but keep the render test on all platforms.

**Verification Probes (post-ship):**
1. On macOS: `yadgar-setup --noninteractive` (or `make setup INSTALL_NONINTERACTIVE=1`) → `launchctl list | grep com.openfantasy.yadgar` returns active job.
2. `plutil -lint ~/Library/LaunchAgents/com.openfantasy.yadgar.plist` exits 0.
3. Kill core container → launchd auto-restarts within 30s (KeepAlive behavior).
4. `curl http://localhost:8765/health` responds after restart.
5. `/metrics` available — `curl http://localhost:8765/metrics | grep yadgar_` returns results.

---

## Rollback Path (P9)

`scripts/install/uninstall.sh` (v5.45.0 deliverable) must handle macOS path: `launchctl unload ~/Library/LaunchAgents/com.openfantasy.yadgar.plist` + remove plist files. No irreversible ops. Data preserved in `~/.yadgar/` unless `--purge`.

---

## Dependency Pinning (P10)

No new external dependencies. `launchctl` is a macOS system tool (no version pinning needed). `plutil` is also system-provided.

---

## Agent Dispatch Budget (P11)

N/A — no agent dispatch for benchmark work. Standard implementer dispatch; estimated 0.5-1 calendar day (post-DP-A host confirmation).

---

## Effort Estimate

| Step | Days |
|---|---:|
| Step 0 — pre-flight (host confirm + DP-B/C/D resolution) | 0.25 |
| Step 1 — TDD scaffolding | 0.25 |
| Step 2 — plist templates + `generate_launchd.sh` | 0.25 |
| Step 3 — `yadgar-setup` / `make setup` macOS wiring | 0.25 |
| Step 4 — macOS smoke test + `uninstall.sh` macOS path | 0.25 |
| Step 5 — CHANGELOG + MIGRATION_NOTES | 0.1 |
| **Total** | **~1 calendar day** |

---

## Acceptance Criteria

- [ ] `yadgar-setup --noninteractive` (or `make setup INSTALL_NONINTERACTIVE=1`) on macOS generates + loads both plists.
- [ ] Both plists pass `plutil -lint`.
- [ ] Auto-restart on crash (KeepAlive) verified via kill-and-wait.
- [ ] `yadgar-setup` / `make setup` on Linux is unaffected (macOS launchd path gated by `detect_os.sh`).
- [ ] `scripts/install/uninstall.sh` on macOS unloads + removes plists.
- [ ] `pytest yadgar/tests/test_install.py` green (macOS tests pass on macOS; render tests pass everywhere).
- [ ] CHANGELOG.md v5.45.1 entry.
- [ ] MIGRATION_NOTES.md v5.45.1 section.
