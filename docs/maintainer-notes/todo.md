# TODO

## Nix / Infrastructure

- **Fix garbled DST path in yadgar backup script** (`llm.nix`)
  The `date` format string in ExecStartPre has a nix store path leaking into it,
  producing a corrupt `DST` path. Backup silently skips (non-fatal) but never
  actually writes a snapshot. Fix the quoting/escaping of the bash heredoc in
  the nix service definition.

## Deferred Verifications

Tests / probes that could not run at ship time due to missing host, hardware,
external service, or other circumstance. Run when the gating circumstance is
resolved. Each entry: **gate** (what unblocks it), **probes** (commands), **ship version**, **MIGRATION_NOTES ref**.

### v5.45.1 — macOS launchd (paper-only ship 2026-06-04)

**Gate:** access to a macOS Ventura+ host with `podman machine` installed.
**Ship:** v5.45.1 @ master `0dd5171`. Plan: `docs/PLAN_V5_45_1_MACOS_LAUNCHD.md`. Decision: `docs/reference/decisions.md` PD-38.
**Ref:** `MIGRATION_NOTES.md` v5.45.1 section.

Probes:

1. `yadgar install --non-interactive` → `launchctl list | grep com.openfantasy.yadgar` returns active job
2. `plutil -lint ~/Library/LaunchAgents/com.openfantasy.yadgar.plist` exits 0
3. Kill core container → launchd auto-restarts within 30s (KeepAlive=true)
4. `curl http://localhost:8765/health` responds after restart
5. `curl http://localhost:8765/metrics | grep yadgar_` returns metrics
6. `make uninstall` → `launchctl list | grep com.openfantasy.yadgar` returns empty
7. `make uninstall-purge` → also removes `~/Library/Logs/yadgar/`

Affected files for fix-ups: `scripts/install/launchd/*.plist.in`, `scripts/install/generate_launchd.sh`, `scripts/install/detect_os.sh` (macOS branch), `scripts/install/detect_runtime.sh` (podman-machine socket probe), `scripts/install/uninstall.sh` (macOS path).

Pytest darwin-skipif tests will activate automatically on macOS host: `pytest yadgar/tests/test_v5_45_1_*.py -v` should drop the 5 skipped → 5 passed.
