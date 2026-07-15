> Archived 2026-07-16 — DONE (already `scrape_interval="5s"` in nix repo modules/observability/prometheus.nix).

# PLAN — process-exporter scrape 2s → 5s (observer-effect) (#34)

Created 2026-06-25 (improvement-train #29, group A). theme: observability / nix /
observer-effect. priority: low (trivial). **NOT a yadgar-repo change — hand to user.**

## Problem

The CPU/fan-burst investigation
(`cpu-burst-rootcause-and-embedding-scan-fix.md`, Part 1) found that at deep idle,
**both `surreal` and `node_exporter` burst on a shared ~1200 s pacer**, and the
high-res Prometheus scrape is a live **observer-effect** suspect (mem 531374:
`--collector.systemd` over ~395 units + cadvisor `--docker_only=false`, scraped
frequently). Lowering the high-res scrape interval reduces the observer load and is the
cheap half of the A/B that distinguishes observer-effect from surreal-internal
compaction.

## Where it lives (verified 2026-06-25) — OUT OF THIS REPO

100% nix-side. **Zero** scrape-interval config exists in the yadgar repo (the repo only
depends on the `prometheus-client` library to *emit* metrics; it does not configure
Prometheus scraping). The high-res job is in the NixOS monitoring module:

- **File:** `~/git/nix/modules/observability/prometheus.nix` (NOT
  `modules/home/yadgar.nix`).
- **Job `highres-burst`** with **`scrape_interval = "2s"`**, targets
  `127.0.0.1:9256` (process-exporter), `:9100` (node_exporter), `:8765` (yadgar-core),
  `:8001` (yadgar-backend).

(Path/line found via a read-only look at `~/git/nix`; confirm before editing — the nix
repo evolves independently of yadgar.)

## Fix (hand to user via MIGRATION_NOTES — cannot be done from this repo)

In `~/git/nix/modules/observability/prometheus.nix`, change the `highres-burst` job:
`scrape_interval = "2s"` → `"5s"`. Apply with the standard nix workflow
(anchor 50: `nixos-rebuild …` / the user's deploy path). This is an infra change on
shared monitoring — **No-Apply / No-Terraform discipline applies; the user runs it.**

## How to verify it worked
- After the interval change, re-watch the deep-idle window: if the ~1200 s
  node_exporter/process-exporter burst attenuates while surreal's persists, that
  corroborates **observer-effect** for the node_exporter half (and isolates the surreal
  half as internal). This is the decisive A/B referenced in the cpu-burst Part 1
  "to actually close Part 1" list — pairs with briefly stopping node_exporter+cadvisor.

## Config / contracts / risks
- No yadgar code, no I25 knob, no BEHAVIOR_CONTRACT — pure nix scrape-interval tune.
- Risk: 5 s scrape coarsens burst-capture resolution. Acceptable — the 2 s job was a
  temporary high-res burst-hunt aid, not a permanent need; 5 s still resolves a
  ~1200 s-period burst fine.
- This change does NOT by itself silence the fan; it is a diagnostic + a small
  steady-state load reduction (see cpu-burst Part 1 — burst is still OPEN).
