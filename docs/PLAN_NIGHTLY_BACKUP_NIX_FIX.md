# PLAN — Nightly Backup Cycle Fix (nix systemd-user env)

**Status:** drafted 2026-05-29 after investigation. Two-tier fix: immediate (nix-only, no yadgar release) + permanent (v5.X.Y train).

**Master at draft time:** core v5.10.0 + backend v5.3.1 deployed.

---

## Why

`yadgar-nightly-cycle.timer` fires correctly at 19:00 UTC daily (`Persistent=true` works). `yadgar-nightly-cycle.service` then runs `/home/max/.local/bin/yadgar-nightly-cycle` (pipx-installed console script) which imports `numpy` via the consolidation scheduler chain. **The import fails:**

```
maj 27 21:00:27 yadgar-nightly-cycle[...]: ImportError: libstdc++.so.6: cannot open shared object file
maj 28 21:00:34 yadgar-nightly-cycle[...]: ImportError: libstdc++.so.6: cannot open shared object file
```

`numpy`'s compiled `.so` extensions were built against the Nix user shell environment (where `libstdc++` lives at `/nix/store/.../gcc-15.2.0-lib/lib/`). The systemd-user service environment is **minimal** — it does NOT inherit `LD_LIBRARY_PATH` or other Nix-injected env vars from the user login shell.

**Consequence:** Service exits code=1 on every run. `Persistent=true` only retries failed TIMERS (catchup if host was off when timer should have fired), NOT failed SERVICES. The cycle has been dead since 2026-05-27, ~13 days stale.

**Last successful backup snapshot:** `~/.yadgar/backups/pre-v5-merge-20260516-1313` (2026-05-16).

---

## What ships — Tier 1: Immediate fix (nix-only, today)

User edits `~/git/nix/modules/home/yadgar.nix`. Two options:

### Option A: nix-managed wrapper script (RECOMMENDED)

Add to the nix module:

```nix
# In yadgar.nix, alongside existing systemd unit definitions:

home.file.".local/bin/yadgar-nightly-cycle-wrapper.sh" = {
  source = pkgs.writeShellScript "yadgar-nightly-cycle-wrapper" ''
    export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"
    exec ${homeDir}/.local/bin/yadgar-nightly-cycle "$@"
  '';
  executable = true;
};

systemd.user.services.yadgar-nightly-cycle = {
  # ... existing config ...
  Service = {
    ExecStart = "${homeDir}/.local/bin/yadgar-nightly-cycle-wrapper.sh";
    # Keep existing Environment=... Type=oneshot TimeoutStartSec=1h
  };
};
```

**Pros:** Nix-tracked dependency on `pkgs.stdenv.cc.cc.lib`. Version-pinned via nixpkgs. Self-documenting. Reproducible.

**Cons:** Adds one new file to `~/.local/bin/`. Requires `nix-update` to apply.

### Option B: Inline ExecStart wrapper (simpler, fragile)

```nix
ExecStart = pkgs.writeShellScript "yadgar-nightly-cycle-exec" ''
  export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"
  exec ${homeDir}/.local/bin/yadgar-nightly-cycle
'';
```

**Pros:** No separate file at `~/.local/bin/`. Pure nix.

**Cons:** ExecStart referencing a generated script means each nix-update rebuilds + relinks. Less ergonomic to debug (`systemctl --user cat` shows nix-store path).

### Verification after applying Option A or B

```bash
# Force immediate test run:
systemctl --user start yadgar-nightly-cycle.service
journalctl --user -u yadgar-nightly-cycle.service --since "1 minute ago" --no-pager

# Expected: 7-step cycle output, exit 0 (or codes 10/20/30/40/50/60/70 per orchestrator).
# NOT expected: ImportError or libstdc++.so.6 errors.

# After verified, check backup ran:
ls -la ~/.yadgar/backups/ | tail -3
```

**Recommendation:** **Option A.** Self-documenting, easier debugging, standard nix pattern.

---

## What ships — Tier 2: Permanent fix (v5.X.Y train, deferred)

Three candidates, not all in same train:

### Candidate 1: Pure-Python consolidation scheduler

Remove the `numpy` import from `yadgar.consolidation` (and from `yadgar.scripts.nightly_cycle` import chain). Replace cosine + clustering math with pure-Python implementations OR scipy-stubs OR lazy-import numpy only when needed (and not at import time).

- **Effort:** medium (~300 LOC refactor + tests).
- **Trade-off:** numpy is ~5× faster than pure Python on the math hot paths. Acceptable for nightly cycle (runs once/day, not latency-sensitive). Unacceptable for online consolidation (already runs in backend container where numpy works).
- **Constraint:** Keep numpy for backend (containerized — has libstdc++). Only the host-executed nightly script needs to be numpy-free.
- **Path:** Split `yadgar.scripts.nightly_cycle` from `yadgar.consolidation`. Have nightly script call backend APIs (`POST /api/consolidate_step`) instead of importing yadgar-core's consolidation module.

### Candidate 2: Move nightly cycle into container

Replace the host-executed `yadgar-nightly-cycle` with a container that runs daily via systemd. Backend already has numpy + libstdc++ inside. Cycle script can run inside backend container via `docker exec` or as a sidecar service triggered by a host systemd unit.

- **Effort:** small (~50 LOC nix unit + 1 simple exec wrapper).
- **Trade-off:** Container exec couples nightly cycle to backend uptime. If backend is down at 19:00, nightly skips. Acceptable — daemon manages backup before any other heavy work.
- **Risk:** Container exec creates a long-running shell inside an already-running container. Watch resource use.

### Candidate 3: nix systemd-user "library-path" wrapper helper (cross-cutting)

Generalize the Tier 1 fix into a reusable nix helper:

```nix
# In yadgar.nix:
mkPythonSystemdService = name: { ExecStart, ... }@args: {
  Service = args // {
    ExecStart = pkgs.writeShellScript "${name}-exec" ''
      export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"
      exec ${ExecStart}
    '';
  };
};

systemd.user.services.yadgar-nightly-cycle = mkPythonSystemdService "yadgar-nightly-cycle" {
  ExecStart = "${homeDir}/.local/bin/yadgar-nightly-cycle";
  # ...
};
```

- **Effort:** trivial.
- **Trade-off:** N/A — strictly a refactor of Tier 1.
- **Use case:** Every future yadgar systemd-user service that imports yadgar code gets the wrapper automatically. Prevents recurrence.

### Recommendation

**Ship Candidate 3 ALONGSIDE Tier 1 immediate fix.** They share the same nix-side change set. User-managed nix repo edits, single commit, single `nix-update`.

Defer Candidate 1 (pure-Python consolidation) until OTHER systemd-user services start hitting the same library issue OR until backend v5.5+ shifts the nightly cycle to a container-based execution model (Candidate 2).

---

## Verification (post Tier 1 fix)

1. `systemctl --user start yadgar-nightly-cycle.service` — manual fire.
2. `systemctl --user status yadgar-nightly-cycle.service` — exit 0 expected. Exit codes 10/20/30/40/50/60/70 are per-step degraded outcomes documented in PR-1a (v5.7.0).
3. `ls -la ~/.yadgar/backups/` — new snapshot dated today.
4. `journalctl --user -u yadgar-nightly-cycle.service --since "5 minutes ago" --no-pager | grep -E 'snapshot|backup|step'` — orchestrator step logs visible.
5. Next 19:00 UTC: `systemctl --user list-timers --all | grep yadgar-nightly-cycle` — `Last:` should update.

---

## Acceptance criteria

- Service runs to completion exit 0 (or documented degraded exit) on next 19:00 UTC fire.
- Backup snapshot appears in `~/.yadgar/backups/` dated within 24h.
- No `ImportError: libstdc++.so.6` in `journalctl --user -u yadgar-nightly-cycle.service`.
- Optional: snapshot retention (per `YADGAR_BACKUP_RETENTION` env knob from v5.7.0 PR-6) prunes old snapshots correctly.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Wrapper script overrides user `LD_LIBRARY_PATH` and breaks other tools | We APPEND to existing `LD_LIBRARY_PATH`, not overwrite. Other paths preserved. |
| `pkgs.stdenv.cc.cc.lib` version drifts and breaks compatibility | nixpkgs pins gcc version per channel. Drift only on channel upgrade — tested then. |
| Backup runs successfully but pruning fails on stale snapshots | `YADGAR_BACKUP_RETENTION` already exists. Separate concern. |
| pipx editable install may be missing after nix-update | Investigation found `~/.local/bin/yadgar-nightly-cycle` EXISTS. If it disappears, document the `pipx reinstall yadgar` step. |

---

## Open / parked questions

- **Why does numpy work in backend container but not in host?** Backend container ships its own glibc/libstdc++ via the Docker image base layer. Host pipx venv uses system glibc (which IS available) but compiled `.so`s look for libstdc++ NOT in `/usr/lib/`. The lookup path mismatch is purely an LD_LIBRARY_PATH issue.
- **Should we audit ALL yadgar systemd-user services for the same issue?** YES — Candidate 3 above generalizes the fix. Check `yadgar-vacuum.service` + `yadgar-vacuum-trigger.service` for same import chain risk. If they don't import numpy at boot, they're fine; otherwise wrap them too.
- **Add CI test?** Hard — CI doesn't have systemd-user. Could add a smoke test that runs `yadgar-nightly-cycle --help` in a minimal `env -i` shell to catch import-time failures.

---

## Sequencing

This is NOT a yadgar core release. The Tier 1 fix is a nix-repo edit by the user. The Tier 2 candidates are deferred work.

**Action items:**

1. User edits `~/git/nix/modules/home/yadgar.nix` per Option A above.
2. User runs `cd ~/git/nix && nix-update`.
3. User manually fires service once to confirm: `systemctl --user start yadgar-nightly-cycle.service`.
4. Wait for next 19:00 UTC to verify timer-triggered run.
5. After 1 week of clean runs: declare resolved.
6. (Optional later) Refactor into Candidate 3 helper when adding next nix systemd-user service that has the same risk.

---

## Out of scope

- Refactoring consolidation to avoid numpy entirely (Candidate 1, deferred).
- Container-based execution of nightly cycle (Candidate 2, deferred).
- CLAUDE.md doc additions about systemd-user + compiled Python — user can add to their own rules if desired.
