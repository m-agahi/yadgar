## ▼ Live state (canonical — supersedes legacy preamble)



| Component | Version | Notes |
|---|---|---|
| Core | **v5.46.0** | Distribution (pipx + brew + nix flake + SBOM + yadgar-setup.sh Option C). Built amd64 + nix bumped 2026-06-05. APPLY PENDING (user runs `home-manager switch`). |
| Backend | **v5.4.0** | unchanged whole batch |
| Master | `5f648e0` | v5.46.0 distribution + Step 9.5 release.yaml download-artifact pin fix. Pushed to codeberg. |
| Image registry (local) | `docker.io/openfantasy/yadgar:5.42.2 → 5.46.0` | v5.46.0 built amd64 2026-06-05 (`docker.io/openfantasy/yadgar:5.46.0` 655 MB; image id 9fc5f51906cc). Skipped intermediate v5.45.0 + v5.45.1 builds (nix tracks live tip; bundle deploys all). No dockerhub push per 2026-05-19 rule. |
| Nix repo | `ac1c989` | v5.46.0 bump pushed to codeberg/maxagahi/nix (2026-06-05). Apply: `home-manager switch`. |
| Last consolidation | 2026-06-04T11:52:28Z | per /api/stats |
| Roadmap sync source | `docs/yadgar-roadmap-future-improvements.md` | file-canonical mirror (RMW safety pattern until v5.64 surgical edit primitives ship) |

**Recently shipped this cycle:** v5.42.2 → v5.42.6 hotfix train + v5.43.0 MCP schema discipline + v5.44.0 subagent MCP wiring + v5.45.0 make-canonical setup foundation + v5.45.1 macOS launchd (paper-only) + v5.46.0 distribution (pipx + brew + nix flake + SBOM + yadgar-setup.sh Option C). See `## Recently shipped` for entries.

> ⚠ **Legacy preamble below** (bold "Currently deployed (LIVE):" block + Ship summary range + Plans committed + In flight) is **historical archive content** restored 2026-05-31 from a 2026-05-30 snapshot. References v5.41.1 + 2026-06-02 dates — **STALE**. Full RMW cleanup deferred (would need ~46k-char wiki_update; risky). This section is the canonical current-state pointer; treat the legacy preamble as archeology.



# Yadgar Roadmap & Future Improvements

> **⚠ RECOVERED FROM ARCHIVE 2026-05-31 17:30 UTC** — page was overwritten to a 4-entry stub by successive ship-agents calling `wiki_update(content=<patch>)` without read-modify-write. Structure restored from `/home/max/.yadgar/archive/wiki/2026-05-30/yadgar-yadgar-roadmap-future-improvements.md`. **2026-05-30 → 2026-06-02 shipped v5.10.4 → v5.41.1** (~41 versions).

**Currently deployed (LIVE):**
- Core: **v5.41.1** — wiki versioning transactional atomicity. Built amd64 + nix applied 2026-06-02. Health verified.
- Backend: **v5.4.0** (unchanged whole batch)

---

**Ship summary (2026-05-30 → 2026-06-02):**
- v5.10.4 → v5.25.x batch — see CHANGELOG
- v5.26.0 (2026-06-01) — Sonnet 4.6 500q LongMemEval-s, 69.4%, Adopt-1 CLOSED
- v5.27.0 → v5.31.1 (2026-06-01 afternoon) — DuckDB + bi-temporal + pipeline plugin arch + hotfix
- v5.33.0 (2026-06-01 evening) — memory blocks (Letta-style)
- v5.35.0 (2026-06-01 evening) — JS SDK + branding hotfix
- v5.35.1 (2026-06-01 night) — blocks follow-ups hotfix
- **v5.37.0 (2026-06-02 morning)** — viz integration testing (Playwright + API contract + Vitest + CI). 56 tests across 4 layers.
- **v5.39.0 (2026-06-02 morning)** — wiki similarity gate (anti-duplicate). 5 I25 knobs. Real-embedding calibration (separation margin 0.24 at 0.80 threshold). 19/19 tests.
- **v5.41.0 (2026-06-02 morning)** — wiki versioning Phase 1+2. Migration 013 + 5 MCP tools (`wiki_history`/`wiki_read_version`/`wiki_diff`/`wiki_restore`/`wiki_append_section`). 38/38 tests. Closes 2026-05-31 corruption class.
- **v5.41.1 (2026-06-02 mid-day)** — wiki versioning transactional atomicity hotfix. Compound `BEGIN; CREATE wiki_page; CREATE wiki_page_version; COMMIT` via single `_q()` call. try/except removed; version-write failures propagate. 241/241 tests.

**Plans committed (not yet shipped):**
- v5.41.2 — `wait` flag on wiki write tools for read-your-writes consistency. v5.41.0 smoke-test 2026-06-02 revealed forced `sleep(3)` to avoid stale `wiki_history` reads. Opt-in `wait=True` blocks until queue commits. **IN FLIGHT 2026-06-02.**
- v5.41.3 — MCP-handler perf test + correct I9 attribution. v5.41.1's `TestUpdatePerfUnder5msP50` measures storage layer (queue-worker path), not MCP handler. I9 governs MCP handler only. v5.41.3 adds true MCP-handler perf test + corrects CHANGELOG/MIGRATION_NOTES/test docstring framing. 0.25d.
- v5.43.0 SKELETON — memory_archive retention (heat=0 ≈1300 rows)
- v5.45/v5.46/v5.47 — setup refactor (foundation + distribution + updates)
- v5.50/v5.51/v5.52 — viz overhaul. **v5.50 bookmarks addendum committed 2026-06-02:** full forensic-library-dashboard redesign (search-first w/ semantic default, preview pane w/ star toggle, versions rail w/ size-delta sparklines, side-by-side diff view, `--star` gold accent). 7 new component files. Composes v5.41 versioning + v5.39 similarity gate + v5.23 bookmarks.
- v5.57/v5.58 D2/D3 A/B re-scope — NLI on/off + PPR weight via v5.31 plugin arch
- v6.3.0 SKELETON — Adopt-7 extract-on-ingest (RE-SLOTTED from v5.36; sub-release inside v6 curator framework)

**In flight (2026-06-02 afternoon):**
- **v5.41.2** — wait flag. Agent dispatched. Branch `fix/v5.41.2-wiki-add-wait` off `5129ee6`. 3 phases.

---

## HARD DEADLINE

**2026-08-26 — `migration_grace=true` anchor expiry.** ✅ PD-23 handler SHIPPED v5.21.0 (87d early).

## Competitor scans

- 2026-05-30 — `docs/competitor-audit-2026-05-30.md` (full landscape)
- 2026-05-31 — `docs/competitor-graphify-2026-05-31.md` (irrelevant)

## Source-of-truth

`CHANGELOG.md`, `docs/DECISIONS.md`, `docs/PLAN_V5_*.md`, `docs/PLAN_V6_*.md`, `MIGRATION_NOTES.md`

## Recently shipped
- **v5.46.0 (2026-06-05):** Distribution — pipx + Homebrew + nix flake + SBOM + `yadgar-setup.sh` (Option C). Audit (`docs/V5_46_AUDIT_2026_06_04.md`) → remediation (commits 51112a8 + e53bbe0 + ce5805a) → impl (10 commits 8d2164e..551f6fa) → merge (5f648e0). NEW: `scripts/install/yadgar-setup.sh` (230 LOC hand-written shell parallel to Makefile chain — distribution-side entrypoint for pipx/brew/nix users without repo checkout; flags `--noninteractive`/`--dryrun`/`--doctor`); `yadgar/scripts/yadgar_setup.py` Python shim invoking the bundled .sh via `importlib.resources`; `Formula/yadgar.rb.in` (Homebrew template, caveats-only, NO post_install); `flake.nix` + `flake.lock` (nixos-unstable, Python 3.14, `nix flake check --no-build` passes); `scripts/generate_sbom.sh` (cyclonedx-bom==7.3.0 CycloneDX 1.5 JSON); `.forgejo/workflows/release.yaml` (active: build-wheel/sbom/attach; stubs: brew/nix with `if: false` — v5.46.1 flips); `docs/INSTALL.md` (4-path install guide). Step 0 pre-flight: cyclonedx-bom==7.3.0 pin + Apache-2.0 license alignment (SPDX + classifier consistency). Step 9.5 post-advisor fix: `download-artifact@v3` → `@v3.1.0` (no bare v3 tag on data.forgejo.org). 68 v5.46 tests pass + 3 skipped; 214 pass full v5.45+v5.46 suite. Build amd64 + nix bump shipped 2026-06-05 (nix repo @ac1c989).
- **v5.45.1 (2026-06-04):** macOS launchd plist generation + install (paper-only ship; host verification deferred per `docs/maintainer-notes/todo.md` Deferred Verifications + DECISIONS.md PD-38). Adds `scripts/install/launchd/com.openfantasy.yadgar{,backend}.plist.in` templates, `scripts/install/generate_launchd.sh` (analog of generate_systemd.sh with `plutil -lint` on Darwin + `launchctl bootstrap gui/$UID` Catalina+ with `load -w` fallback), detect_os.sh macOS branch, detect_runtime.sh podman-machine socket probe extension, Makefile setup target routes systemd vs launchd via detect_os, uninstall.sh macOS path (`launchctl unload` + plist removal + `~/Library/Logs/yadgar/` on --purge). 140 tests pass + 5 darwin-skipif (auto-activate on macOS host). Distinct from v5.45.0 — Linux-only setup. Verification gate: macOS Ventura+ host with `podman machine`. 5 P7 probes documented in MIGRATION_NOTES v5.45.1 + repo todo Deferred Verifications.
- **v5.45.0 (2026-06-04):** make-canonical Setup Foundation (Linux). `make setup` is the only user-facing entrypoint — replaces fragmented `scripts/setup.sh` (deleted) + `pipx + yadgar install` path. 8 building-block Makefile targets: `setup` (full chain pre-setup → pull-images → bootstrap-secrets → generate_systemd → enable-units → install-hooks → install-agents → config-sync → install-rules → seed-anchors), `uninstall` (preserve `~/.yadgar/`), `uninstall-purge` (full wipe), `install-hooks` / `install-agents` / `config-sync` / `install-rules` (idempotent CLAUDE.md fragment append via marker dedup) / `seed-anchors` (`yadgar seed --anchors` content-hash dedup) / `pull-images` / `bootstrap-secrets` (idempotent credential prompts → `~/.yadgar/secrets.env` chmod 600; `--system` flag for `/etc/yadgar/secrets.env`) / `enable-units` (systemctl daemon-reload + enable --now yadgar.target) / `restore` (DB restore via `YADGAR_RESTORE_DB=...`). DPs: (DP2) podman → docker runtime detection with `YADGAR_CONTAINER_RUNTIME` override + `check_runtime()` rename in daemon.py (backward-compat alias kept; ~16 callsites with TODO(v5.46) for full migration); (DP3 OVERRIDE) make-canonical, NO `yadgar install` CLI; (DP4) hook install daemon-independent confirmed; (DP5) `yadgar.target` uses `Wants=yadgar.service yadgar-backend.service`; (DP6 OVERRIDE) seed anchors + CLAUDE.md fragment FOLDED back via make targets (v5.45.1 split cancelled, freed slot reassigned to macOS launchd). NixOS guard: `detect_os.sh` returns `linux-nixos` → `pre-setup` refuses with "use nix flake" message; nix-symlink defense-in-depth in `generate_systemd.sh`. install_assets/ shipped: `CLAUDE.md.fragment` (~30 lines yadgar usage rules), `seeds/anchors.yaml` (8 canonical anchors), systemd templates. 92 tests pass. DECISIONS.md PD-37 amendments. Build amd64 + nix bump PENDING (user-action per WORKFLOW RULE 2026-05-18).
- **v5.44.0 (2026-06-04):** subagent MCP wiring + X1-X5 automation extensions. 5 bundled agent .md templates (Explore + cavecrew-{investigator,builder,reviewer} + general-purpose) under `install_assets/agents/`. X1 `agent_dispatch_prelude` extended with `branch_hint` + `directory` + `subagent_type` + `include_context` (opt-in per DP-X1-1) — calls `recall(directory, branch_hint)` + `wiki_query(directory, branch_hint)` using v5.43.0 surface. X2 SubagentStop hook directive parser (`## Yadgar Findings` section) with `provenance_agent` tag. X3 `platform_paths.py` cross-OS detection (Linux `~/.claude/`, macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`). X4 `yadgar install-subagents` CLI with NixOS carve-out (detects `/etc/NIXOS`). X5 `yadgar config sync` for incremental yaml updates without `--force` destruction (closes the v5.42.6 X5 gap). 48 tests pass. Post-deploy: non-nix users run `yadgar install-subagents` + `yadgar config sync`; nix-managed users get config via nix repo separately. Post-ship verification V1-V5 GREEN.
- **v5.43.0 (2026-06-04):** MCP schema discipline — caller-context for `recall` + `wiki_query`. Added `branch_hint` param to `recall` (signature parity with `wiki_read` v5.42.6); added `directory` + `branch_hint` to `wiki_query`. `wiki.add()` return dict now surfaces `branch` key (enables wiki_approve inheritance via draft branch column). Resolution order per DP-1: `_detect_branch(directory or os.getcwd())` → `branch_hint` → None. Read-only tools fall through gracefully without hard-reject (DP-3 applies to write tools only; those already hard-reject from v5.42.3-6). Cross-plan: v5.44.0 subagent MCP wiring X2 SubagentStop integration now has the `recall(branch_hint=...)` signature it depends on. 19 new tests pass. Plan scope (5.5-7.5d) was pre-v5.42.x stale — actual 3 gaps, most tools already compliant from v5.42.5/6 cycle. Post-ship verification: V1-V5 all GREEN.
- **v5.42.6 (2026-06-03 night):** hotfix release — 3 v5.42.5 production bugs found by post-ship verification + 2 escape hatches added. **Bug 1:** migration 016 backfill didn't run on existing rows (SurrealDB `IS NONE` doesn't match field-absent records); migration 018 added with Python-side filter + migration 016 source patched for fresh deploys. **Bug 2:** §25 4-step resolution hole — daemon `_detect_branch(os.getcwd())` returned None → step 1 always missed → all writes unfindable via `wiki_read(slug, directory=...)`; added `branch_hint` param to `wiki_read` (Option A, symmetric with v5.42.5 F1). **Bug 3:** schema-data mismatch left 200 legacy rows read-only via MCP — UPDATE rejected with "Couldn't coerce: Expected string but found NONE"; migration 018 backfill restored writability. **Escape hatches:** `YADGAR_DIRECTORY_ENFORCEMENT` + `YADGAR_BRANCH_ENFORCEMENT` env knobs (default ON; OFF for debug/old-schema recovery with WARN log + `yadgar_writes_with_enforcement_relaxed_total` metric). 27 new tests. Post-ship live re-probe verified: directory-scoped queries return real corpus, roadmap writable again.
- **v5.42.5 (2026-06-03 night):** directory contract foundational release. Adds `directory_context` NOT NULL to wiki_page + memory tables. Migration 016 backfills via tag-based heuristic. §25 resolution upgraded to 4-step (project-branch → project-canonical → global → not-found). MCP boundary Pydantic + drainer DLQ taxonomy (`missing_directory`). F1: `_resolve_page_id_by_slug` accepts directory. F2: `agent_prompt_save` routes through wiki_add. F3: blocks tools enforce directory required when scope='project'. 122 tests pass. **HAD 3 SHIPPED REGRESSIONS — fixed in v5.42.6.**
- **v5.42.4 (2026-06-03 night):** hardcoded `"master"` exception-fallback cleanup. 5 sites in wiki/recall — fallback `"master"` → `None` (canonical, reachable via §25 step 3). views.sql `v_branch_distribution` COALESCE pull-forward to `'(canonical)'`. 6 new RED→GREEN tests. Deferred: `_get_default_branch_cached` final fallback (return-type cascade).
- **v5.42.3 (2026-06-03 night):** drainer branch enforcement + memory branch_hint parity. All write tools hard-reject at MCP boundary when branch context absent. Drainer pre-apply validates + routes missing-branch records to DLQ. New metric `yadgar_dlq_rejection_count{failure_reason="missing_branch"}`. Memory tools (memorize/anchor/checkpoint/update_active_work) get branch_hint parity. Migration 015: `wiki_draft.branch` column + wiki_approve propagation. Symmetric contract with wiki (user override on agent's soft-warn proposal). 154 tests pass.
- **v5.42.2 (2026-06-03 night):** wiki branch-default fix — REAL root cause of 4-ship silent similarity gate. Live-probe confirmed writer asymmetry: drainer hardcoded `branch="master"` while `wiki_check_duplicate` defaulted `branch=None` with no auto-detect — scope filter `{None}` excluded every drainer-written page. Fix: drainer default → None; `wiki_check_duplicate` auto-detects default branch. Scope becomes `{None, default}` covering canonical + legacy. Live re-probe verified: same call returning `[]` pre-fix now returns `{sim: 0.9055}`.
- **v5.42.2 (2026-06-03 night):** wiki branch-default fix — REAL root cause of 4-ship silent similarity gate. Live-probe confirmed writer asymmetry: drainer hardcoded `branch="master"` (file_queue/dlq.py:127-134) while `wiki_check_duplicate` defaulted `branch=None` with no auto-detect — scope filter `{None}` excluded every drainer-written page. Fix: drainer default → None (canonical); `wiki_check_duplicate` auto-detects default branch like `wiki_query`. Scope becomes `{None, default}` covering canonical + legacy. 2 new e2e tests (drainer-write + legacy-master). HNSW-rebuild hypothesis (prior v5.42.2 plan) refuted: SurrealDB 3.0.5 HNSW auto-updates on UPDATE. Replaces `docs/PLAN_V5_42_2_WIKI_HNSW_REBUILD.md` with `docs/PLAN_V5_42_2_WIKI_BRANCH_DEFAULT_FIX.md`. Live re-probe verified: same call returning `[]` pre-fix now returns `{sim: 0.9055}`. Sample audit found 19/20 prod pages = `branch="master"` (drainer-dominant path); cross-branch wiki copies are illusory — branch-multiplexing exists in code but not data.


- **v5.41.5 (2026-06-02 evening):** MCP handler I9 budget fix. Moved v5.39 similarity gate from request thread to drainer pre-apply stage. `wiki_add` handler p50: **28.89ms → ≤5ms** (I9 restored). Breaking change: `wait=False` callers get `{queued: true, similarity_check: deferred}` instead of sync candidate list — 3 migration options documented. Reuses v5.41.2 wait_for_job for `wait=True` sync path.
- **v5.41.4 (2026-06-02 afternoon):** roadmap_update_lag signal + wiki_append_section convention. New `roadmap_update_lag_hours` signal in `project_brief(mode="signals")`. New `update_roadmap` recommended_action. Detection via pyproject version diff (primary) + commit-message regex (fallback). New `docs/WORKFLOW_ROADMAP_UPDATE.md`. CLAUDE.md NOT touched (nix-managed; goes through nix repo). Convention dogfooded immediately in this very wiki entry.
- **v5.41.3 (2026-06-02 mid-day):** MCP-handler perf test + I9 attribution correction. New `test_wiki_mcp_handler_perf.py` (xfail at 28.89ms baseline; PASSED after v5.41.5 ships). Renamed `TestUpdatePerfUnder5msP50` → `TestStorageUpdatePerfRegressionGuard`. CHANGELOG + MIGRATION_NOTES updated: storage layer (~89ms) is not I9 territory.
- **v5.41.2 (2026-06-02 noon):** wiki write `wait` flag. Per-job completion future in queue. `wait: bool = False` on `wiki_add`/`wiki_update`/`wiki_restore`/`wiki_append_section`. New `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` knob (5s default). FIFO-preserving via wait_for_job. 20 tests. Surfaced ~48ms/~29ms I9 violation in MCP handler — fixed by v5.41.5.

- **v5.41.1 (2026-06-02 mid-day):** wiki versioning transactional atomicity hotfix. 7 atomicity tests + 38 v5.41.0 regression + 196 broader = 241/241. Storage-layer compound txn. try/except removed (breaking: version-write errors now propagate; previously silent). I9 caveat clarified in v5.41.3: storage layer != I9 layer.
- **v5.41.0 (2026-06-02 morning):** wiki versioning Phase 1+2. Migration 013 `wiki_page_version` table. 5 MCP tools. Auto change_summary ("+N -M lines | sections: ... | size: X → Y"). 38 tests. End-to-end smoke verified.
- **v5.39.0 (2026-06-02 morning):** wiki similarity gate. `wiki_add(force=False, replace_slug=None)` + `wiki_check_duplicate` dry-run. 5 I25 knobs. Calibrated thresholds w/ real embeddings (near-dup cluster 0.956-0.993, distinct 0.439-0.713, separation 0.24 at 0.80). 19 tests.
- **v5.37.0 (2026-06-02 morning):** viz integration testing. 4 layers: 18 API contract pytest + 10 Playwright headless + 28 Vitest JS unit + CI workflow. `viz_helpers.js` DRY extract.
- **v5.35.1 (2026-06-01 night):** blocks follow-ups hotfix. 7 deferred items + `_MEMORY_UPDATABLE_FIELDS` class. 6 phase-commits. 39 tests. `_active_work` = Option C (defer to v5.50+).
- **v5.35.0 (2026-06-01 evening):** `@yadgar/sdk` v0.1.0 JS/TS SDK. 53/53 tool wrappers, 73 vitest tests.
- **v5.33.0 (2026-06-01 evening):** memory blocks. Migration 012. 5 block_* MCP tools. 25 tests. (5h lost on first attempt to context overflow.)
- **v5.31.1 (2026-06-01):** hotfix bundle. Entity nodes restored (broken since v5.0!) + MCP recall kwargs. 85 tests.
- **v5.31.0 (2026-06-01):** R2 recall pipeline plugin arch. 30 tests.
- **v5.29.0 (2026-06-01):** Adopt-3 bi-temporal extension. 22 tests.
- **v5.27.0 (2026-06-01):** Adopt-6 DuckDB analytics export. 30 tests.
- **v5.26.0 (2026-06-01):** Sonnet 4.6 500q LongMemEval-s 69.4%. Adopt-1 CLOSED.

## Pipeline (in dispatch order)


| Slot | Item | Status | Effort |
|---|---|---|---|
| ~~v5.27.0 → v5.46.0~~ | ~~Adopt-6 / Adopt-3 / R2 / hotfix / Adopt-4 / Adopt-5 / blocks hotfix / viz testing / similarity gate / versioning / transactional fix / wait flag / perf test / roadmap signal / handler I9 fix / rejection DLQ / embedding backfill / branch-default fix / drainer reject + memory parity / hardcoded master cleanup / directory contract foundational / hotfix backfill + resolution + knobs / MCP schema discipline / subagent MCP wiring + X1-X5 automation / make-canonical setup foundation Linux / macOS launchd paper-only / distribution (pipx+brew+nix flake+SBOM+yadgar-setup.sh)~~ | ✅ **SHIPPED 2026-06-01 → 2026-06-05** | done |
| ~~v5.45.1 seeds~~ | ~~seed-anchors + bundled CLAUDE.md fragment~~ | ✅ **FOLDED into v5.45.0 via make targets (DP6 override)** | absorbed |
| ~~v5.45.2 macOS launchd~~ | ~~macOS launchd plist (split from v5.45)~~ | ✅ **RENUMBERED to v5.45.1 and shipped paper-only 2026-06-04 — host verification deferred** | done |
| **v5.46.1** | cross-repo PR auto-open (flip `if: false` stubs in .forgejo/workflows/release.yaml from v5.46.0) | drafted + remediated (P8 detail added 2026-06-04) | 1-2d |
| **v5.47.0** | update mechanism — CHECK-ONLY (action=install dropped) | drafted | 1-2d |
| **v5.48.0** | daemon graceful-restart + `action=install` (split from v5.47) | drafted (docs/v5.48.0-plan @ db9da18) | 2-3d |
| **v5.49.0** | memory_archive retention — heat=0 ≈1300 rows | remediated (docs/v5.49.0-plan-remediation @ a96ba3e — CRITICAL P4 IS NONE gap addressed) | 1.5-2d |
| **v5.50.0** | viz core — tabs + overlays + Home/Stats/Health/Info + logos + zoom fix | drafted (cut) | 3-4d |
| **v5.50.1** | Bookmarks tab refactor (forensic library dashboard) | drafted (docs/v5.50.1-plan @ 705fc0b) | 2-3d |
| **v5.50.2** | Control tab + restart endpoints (depends on v5.47 + v5.48) | drafted (docs/v5.50.2-plan @ cfb8d90) | 2-3d |
| **v5.51.0** | hooks fast-profile tuning + Prometheus timeout counter | drafted | 1.5-2d |
| **v5.52.0** | debug viz APIs + console capture | drafted | 2d |
| **v5.55.0** | D2 NLI A/B benchmark run (default-OFF reframing — close the loop on unmeasured flip) | drafted (docs/v5.55.0-plan @ 95f4834) | 0.5-1d |
| **v5.57.0** | D3 PC algo A/B benchmark run | drafted (re-scoped, slot-locked) | 1-1.5d |
| **v5.61.0** | repo-wiki as yadgar-native | drafted (docs/v5.61-plan @ c2a3f1b; migration 017 reserved) | 12-17d total |
| **v5.62.0** | yadgar CLI — Typer wrapper; 63 subcommands | drafted (docs/v5.62-plan @ 961168b) | 3d core + 2d polish |
| **v5.63.0** | wiki corpus maintenance tools | SKELETON (docs/v5.63-skeleton @ 0881700) | ~1d |
| **v5.99.0** | roadmap freshness mechanism | deferred | — |
| **v6.0.0** | LLM curator scaffolding | anchor 484431 — needs plan file | 5-7d |
| v6.1 → v6.5 | curator jobs (Adopt-7 v6.3) | sub-releases | 2-7d each |
| **v7.0.0** | Real-time synthesis | anchor 484431 | TBD |

## Deferred decisions

| Item | Decision | Revisit when |
|---|---|---|
| D2/D3 NLI + PPR A/B | DEFER — v5.31 plugin arch makes it trivial now | **READY to close** — PLAN_V5_57_58 |
| R1-full split consolidation/sleep | PARTIAL ✅ | v6 curator |
| D1 MTREE auto-repair | KEEP-AS-IS | HNSW corruption observed |
| R3 file queue replace | REJECT | LIVE SELECT GA |
| Adopt-7 standalone-vs-bundled | **DECIDED 2026-06-01** — bundled v6.3 sub-release | DPs at v6.0 |
| Adopt-7 eval target | **DECIDED 2026-06-01** — +5pp on synthesis-heavy categories | model landscape shift |
| Seed-anchors at install (v5.45) | OPEN | v5.45 start |
| memory_archive retention 5 DPs (v5.43) | OPEN | v5.41.x done |
| `_active_work` canonicalization | **DECIDED v5.35.1** — Option C (defer) | v5.50+ when block UX proven |

## Follow-ups logged

- ✅ CLOSED v5.35.1: `_MEMORY_UPDATABLE_FIELDS` recurring class + invariant test
- ✅ CLOSED v5.41.0: `wiki_append_section()` + full versioning toolset (corruption-class closer)
- ✅ CLOSED v5.41.1: storage-layer transactional atomicity
- audit_anchors(dry_run) recommended (count=77 > 15)
- D2/D3 A/B READY — v5.31 plugin arch enables single-config-diff. PLAN_V5_57_58.
- v6.0 plan file NEEDED — blocks v6.1–v6.5. Anchor 484431 has decisions.
- PreCompact hook deferred — only PostCompact + SessionStart confirmed in Claude Code 2026 (anchor 491682)
- v5.50 picked up `_active_work` block canonicalization + bookmarks tab refactor (2026-06-02 addendum)
- v5.41.0 smoke test (2026-06-02) found `wiki_history` stale read after `wiki_add` → v5.41.2 wait flag
- v5.41.1 I9 mis-attribution → v5.41.3 correction plan


- **NEW 2026-06-02 evening:** v5.42.0 async-rejection notification plan drafted (`docs/PLAN_V5_42_0_ASYNC_REJECTION_NOTIFICATION.md`). **HOLD pending bench** — opus reviews flagged 3 blockers: yadgar transport is stateless_http (no server-push substrate), Claude Code 2026 has no NotificationReceived hook, wait=True latency objection unverified (claimed ~100-200ms; actually likely ~30ms post-v5.41.5). Bench dispatched on `bench/v5.42-wait-latency`: 54-cell matrix (3 content sizes × 3 similarity outcomes × 2 modes × 3 wiki populations × 50 calls = 2700 calls + substep breakdown). Verdict decides whether v5.42 ships at all OR docs-only patch to v5.41.5 MIGRATION_NOTES with measured numbers.
- **NEW 2026-06-02 evening:** opus reviewer report on remaining v5 plans landed (file `/tmp/.../a8b7f890ac6039367.output`). 12 plans patched + pushed (`0289fe6`). Key adjustments:
  - v5.41.4 dropped CLAUDE.md edit (No Per-Project CLAUDE.md hard rule — goes through nix repo)
  - v5.43 added Phase 0 audit_anchors extension (anchored-by-prose detection — closes DP-D data-loss)
  - v5.45 cut to Linux-only; split macOS launchd → v5.45.2 + seed-anchors → v5.45.1
  - v5.46 cross-repo PR auto-open split → v5.46.1
  - v5.47 dropped `action=install` → v5.48 (pipx upgrade kills running daemon)
  - v5.50 split into v5.50.0 (core) + v5.50.1 (Bookmarks) + v5.50.2 (Control) — 557-line plan was phase-commit-infeasible
  - v5.51 add Prometheus counter for hook timeouts (I23)
  - v5.52 add I24 trace_span + I9 budget on _publish_state + byte-cap ring buffer + XSS test
  - v5.57/58 D2/D3 lock slots (now v5.55 + v5.57)
- **v5.42 bench IN FLIGHT** — `bench/v5.42-wait-latency` branch off `de2cafd`. Spawns fresh surreal + embed. Writes to `/tmp/`, not `~/.yadgar/`. RAII teardown via `_surreal_runner.py`. Outputs: `scripts/bench_wiki_add_latency.py` + `docs/V5_42_LATENCY_BENCHMARK_DATA.csv` + `docs/V5_42_LATENCY_BENCHMARK_REPORT.md`.


- **NEW 2026-06-02 evening:** v5.42.0 bench complete (2700 calls, 54-cell matrix). `wait=True` p50=228ms, p99=607ms (worst-cell 542ms — confirmed too slow). `wait=False` handler 0.5ms ✓. v5.42 plan REWRITTEN (v3) to DLQ-based mechanism after opus reviewer blockers on MCP-notif (stateless_http + no NotificationReceived hook). DLQ rewrite reuses existing v4.5 infrastructure + v5.41.4 signal pattern. **Agent dispatched** on `feat/v5.42.0-rejection-dlq` — 4 phases, 0.5-1d. Bench branch `bench/v5.42-wait-latency` preserved (scripts + report + CSV; reusable harness for future similarity-gate regressions). See `docs/V5_42_LATENCY_BENCHMARK_REPORT.md`.
- **NEW 2026-06-02 evening:** workflow doc updated (`docs/WORKFLOW_ROADMAP_UPDATE.md`) — added section-by-section guide (append vs replace_section vs full RMW). Pipeline + Deferred decisions + Branches sections take `replace_section`; bullet-list sections (Recently shipped, Follow-ups, Open questions) take `append_section`. Closes "I appended to wrong section" footgun.

- **NEW 2026-06-03 night — v5.42.3 / v5.43 long-tail cleanup queued.** Hardcoded `"master"` exception-fallback strings remain in `server/tools/wiki.py:478,540,730`, `recall.py:86`, `project.py:185,1682,1844`, `export/views.sql:159,165`. Trip only when `git symbolic-ref` fails. Real bug on `main`-default repos with no remote, lower severity than gate-silence. Replace with `_get_default_branch()` + `None` final fallback. Bundle with v5.42.3 or v5.43.
- **NEW 2026-06-03 night — v5.50+ DEFERRED DECISION: drop `branch` field on wiki entirely (Option C).** User flagged 2026-06-03 morning that hardcoded `"master"` is repo-specific (`main`-default repos exist) AND that identical wikis across branches feels conceptually off. Audit found 19/20 prod pages = `branch="master"` by drainer default-injection; 0 multi-branch pages observed across 200-page sample. Branch-multiplexing exists in code but not in data. BUT: user wanted to reconsider before dropping — there was an original reason. Archaeology dispatched 2026-06-03 night to dig out the original rationale from git log + plan docs + ADRs (see `branch-on-wiki-rationale-2026-06-03` wiki page when written). DO NOT drop branch field until that rationale is documented + a v5.50+ plan with instrumentation (count non-NULL non-default-branch writes for 1 week) confirms zero real users.
- **NEW 2026-06-03 night — Wiki page rotting class identified.** v5.39 / v5.41.5 / v5.42.0 / v5.42.1 all shipped passing unit tests but live gate silent. Tests used CREATE-path with matching branch both sides; never exercised drainer-write-then-MCP-check sequence. Class of bug: "test harness uses fresh-DB CREATE, prod has pre-existing rows with different writer-path-injected defaults." Mitigation pattern: every async-handler bug fix MUST include a test that simulates the EXACT production write path (drainer apply, not direct storage CREATE) followed by the read/check path. Recommend codifying as I29 (or similar invariant) in v5.42.3.

- **NEW 2026-06-03 night — branch-on-wiki rationale documented.** Archaeology complete (agent a3d41d6586b6a5c47). Wiki page: [[branch-on-wiki-original-rationale-2026-06-03-archaeology]]. TLDR: speculative infrastructure shipped v5.0.0 commit `042f42b` (Stage 10 — "branch-aware retrieval"), README:21 + docs/configuration.md:§25 documented use cases (per-branch WIP docs, auto-GC on merge, canonical fallback). 12-month adoption: zero. v5.50+ Option C evaluation MUST start with 1-week instrumentation `yadgar_wiki_writes_with_explicit_branch_total` before dropping. Hybrid path possible: drop branch from `wiki_page` only, keep on `memory` (where users do write branch-scoped notes).

- **NEW 2026-06-03 night — v5.42.3 / v5.42.4 / v5.61 plans dispatched in parallel.** User decision after rationale archaeology: KEEP branch on both wiki + memory (long-running-agent future use case + multi-agent in same branch + context-cleanup recovery). Schema MUST include branch for memory too (same or tighter than wiki). Fix at source — caller's responsibility; drainer rejects missing-branch writes → DLQ (no WARN, no default-master fallback, no garbage-in-garbage-out). Pipeline:
  - v5.42.3 in-flight — drainer reject + writer audit + caller contract. Memory writers tightened in same release.
  - v5.42.4 in-flight (escalated from v5.43+) — hardcoded "master" fallback cleanup at 8+ known sites. Memory parity covered.
  - v5.61 drafted — repo-wiki as yadgar-native feature. Resolves caller-contract gap by making yadgar the caller. Composes with similarity gate + versioning + branch tagging + bookmarks + bi-temporal. Multi-repo. Sub-release breakdown expected.
- **DEPRECATED 2026-06-03 night — v5.50+ Option C (drop branch entirely) REJECTED.** Future agentic workloads + context-cleanup recovery + multi-agent same-branch knowledge sharing are real use cases. Branch on wiki + memory stays. See [[branch-on-wiki-original-rationale-2026-06-03-archaeology]] §Recommendation.

- **NEW 2026-06-03 night — v5.42.3 revision + v5.43.0 (NEW RELEASE) dispatched.** After v5.42.3 first-pass plan review:
  - User OVERRIDE: memory hard-reject (was soft-warn in agent's proposal). Symmetric contract with wiki. Asymmetry rejected.
  - User decision: migration 015 (wiki_draft.branch column) baked INTO v5.42.3 — not deferred.
  - User decision: memory `branch_hint` parity (memorize/anchor/checkpoint) baked INTO v5.42.3 — blocking prerequisite for hard-reject (without it, hook-driven captures break).
  - User decision: BROAD MCP schema audit becomes new v5.43.0 release. Less about "required fields", more about consistent schema discipline + caller-context required for any provenance-tracked op. Agent aec7bbd9f9fd248f8 → `docs/PLAN_V5_43_0_MCP_SCHEMA_DISCIPLINE.md`.
  - v5.42.3 revision agent a0bcfb775499a9420 → updated `docs/PLAN_V5_42_3_DRAINER_REJECT_MISSING_BRANCH.md`.
- **Migration number reservation 2026-06-03 night:** v5.42.3 = migration 015 (wiki_draft.branch column). v5.61 renumbered 015 → 016 (`directory_context` + `wiki_source_hash` table). Sequence matches ship order.

- **NEW 2026-06-03 night — v5.42.3 revision + v5.44.0 (renumbered from v5.43.0 collision) plans landed.**
  - v5.42.3 revision @ 7e803f0 on docs/v5.42.3-plan — memory hard-reject + migration 015 (wiki_draft.branch) + branch_hint parity for memorize/anchor/checkpoint/update_active_work + MCP boundary Pydantic + drainer defense-in-depth. ~12.5h, 1.5-2 cal-days. Ships in single release. Container hazard documented: SessionStart hook update mandatory cross-repo deploy step (without it, hard-reject blocks all hook captures → DLQ accumulates).
  - v5.44.0 MCP schema discipline @ a392bd4 on docs/v5.44.0-plan — 65 tools audited, 22 gaps. 5.5-7.5 cal-days. ORIGINALLY DISPATCHED AS v5.43.0; collided with existing PLAN_V5_43_0_MEMORY_ARCHIVE_RETENTION.md (commit f6e7270). Memory archive retention keeps v5.43.0; broad MCP audit renumbered to v5.44.0.
- **NEW 2026-06-03 night — CRITICAL FINDING from v5.44.0 audit: `_resolve_page_id_by_slug` (wiki.py:735) STILL HAS daemon-cwd bug post-v5.42.2.** Shared helper called by 5 versioning tools (wiki_history, wiki_read_version, wiki_diff, wiki_restore, wiki_append_section). All route through `_detect_branch(os.getcwd())` which resolves to daemon container's branch = master regardless of caller. Same class as the v5.42.2 wiki_check_duplicate bug. NOT YET FIXED. Decide scope: fold into v5.42.3 (writer-asymmetry release) or v5.42.4 (hardcoded master cleanup) or new patch.
- **NEW 2026-06-03 night — v5.44.0 surfaced 2 more bypass tools:** (1) `agent_prompt_save` (agent_prompts.py:64) writes wiki direct via `storage.insert_wiki_page()` — bypasses branch entirely. (2) Blocks tools (block_create through block_append) accept `directory: None` even when scope='project' — project-scoped blocks become ungroupable.
- **3 OPEN DPs from v5.44.0:** (1) canonical mechanism — `directory` vs `branch_hint`? Phase 2 hardening locked until decided. (2) `wiki_approve` — inherit draft branch or re-derive at approval? (3) Phase 2 warning-to-error window length.

- **NEW 2026-06-03 night — v5.42.5 SLOTTED. Directory contract foundational release.** User confirmed semantic model 2026-06-03 night:
  - `directory` NEVER NULL — absolute path OR literal `"global"`. Enforce schema + queue boundary.
  - `branch` NULL-able — NULL = canonical/branchless. Non-NULL = branch-scoped. (Sentinel `"global"` for branch rejected.)
  - 3 semantic categories: project-canonical, project-branch-scoped, global. See [[yadgar-directory-branch-contract-v5-42-3-5-architecture]].
- **v5.42.5 scope folds in surprise findings from v5.44.0 audit:**
  - `wiki_page` gets `directory_context` NOT NULL column via NEW migration 017 (016 reserved for v5.61's `wiki_source_hash`). Backfill all 200 rows.
  - `memory.directory_context` tightened NOT NULL.
  - §25 resolution extended: directory + branch precedence (3-step → 4-step).
  - `_resolve_page_id_by_slug` (wiki.py:735) daemon-cwd bug fixed (5 versioning tools affected).
  - `agent_prompt_save` (agent_prompts.py:64) — direct `insert_wiki_page` bypass closed; routes through `wiki_add` machinery.
  - Blocks tools (`block_create`/`block_append`) — `directory` becomes required when `scope='project'`.
- **Sequential ship order 2026-06-03 night:** v5.42.3 → v5.42.4 → v5.42.5. No parallel ships within v5.42.x. v5.43.0 (memory archive retention, pre-existing plan), v5.44.0 (MCP discipline), v5.61 (repo-wiki) downstream.
- **Migration number table (locked 2026-06-03):**
  - 015 → v5.42.3 (wiki_draft.branch column)
  - 016 → v5.61 (wiki_source_hash table + wiki_page.directory_context as part of v5.61 — NEEDS REVIEW given v5.42.5 also adds directory_context)
  - 017 → v5.42.5 (PROPOSED: wiki_page.directory_context NOT NULL + memory.directory_context NOT NULL)
  - **OPEN DP:** does v5.42.5 take 017 (clean separation) or does v5.42.5 take 016 (since it adds the column, then v5.61 just adds the hash table)? Lean: v5.42.5 takes 016 (column add), v5.61 takes 017 (hash table). Cleaner — column add precedes hash table that depends on it.
- **NEW 2026-06-03 night — advisor consult deferred.** Service overloaded twice this turn. Want to validate semantic model with advisor when service recovers. 3 open questions in [[yadgar-directory-branch-contract-v5-42-3-5-architecture]] §Open questions.

- **NEW 2026-06-03 night — nix CLAUDE.md HARD RULE added (a99fd16, master pushed).** "Yadgar MCP Unavailable: ASK, Don't Improvise". When yadgar MCP disconnects, Claude must STOP + ASK user to reconnect. No fallback to curl / podman exec / direct file writes / Python module import / yadgar CLI / sidecar memory stores. Read-only triage allowed (podman ps/logs, curl /metrics). Reason: v5.42.x contract (branch + directory + similarity gate + DLQ) requires MCP as the only write path. Live after next nix-apply.
- **NEW 2026-06-03 night — v5.62 yadgar CLI SLOTTED.** Plan agent aba1c38967fb9c2d8 dispatched. Pattern: Typer wrapper over MCP, mirroring cloudbeaver-cli (`cb` command). Purpose: enable non-Claude tools, cron jobs, scripts, other AI agents to use yadgar via shell. NOT a fallback for failed MCP (hard rule forbids that); a sanctioned alternative interface for ops/scripting. MUST comply with v5.42.3 + v5.42.5 contracts: defaults `--directory` from caller CWD `git rev-parse --show-toplevel` (not daemon CWD), `--branch` from `git symbolic-ref --short HEAD`. Effort estimate pending agent report (~2-3 days expected).

- **NEW 2026-06-03 night — v5.62 yadgar CLI plan LANDED @961168b on docs/v5.62-plan.** 63 subcommands (61 MCP tools 1:1 + `list-tools` + `whoami`). Architecture: MCP SDK client speaking native streamable-http protocol to the running daemon (FastMCP stateless_http=True per lifecycle.py:516-522). Direct-import rejected — would duplicate or bypass v5.42.3 drainer gate. HTTP-to-daemon keeps drainer as single enforcement point. Effort: 3 cal-days v5.62.0 + 2 cal-days v5.62.1 = 5 total. Open DPs documented; `yg` short alias recommended for v5.62.0.

- **NEW 2026-06-03 night — v5.63 SKELETON drafted (later-minor slot).** docs/v5.63-skeleton @ 0881700 — `docs/PLAN_V5_63_WIKI_CORPUS_MAINTENANCE_TOOLS.md`. Parks 2 MCP tools surfaced by post-v5.42.6 corpus audit:
  - `wiki_reclassify_directory(slug, directory)` — patches directory_context (currently blocked by wiki_update field allowlist)
  - `wiki_set_branch(slug, branch)` — patches branch field for promote-canonical / demote-branch-scoped flow
  - Optional migration 019: bulk-NULL legacy `branch="master"` rows (~200 from pre-v5.42.2 drainer default)
  - Audit findings (no readonly risk; cosmetic taxonomy only): 372 unique pages, 100% have valid directory_context post-v5.42.6 migration 018. 1 heuristic-misclassified page (`aws-vpc-terraform-workspace-...` in global, should be aws-work). ~200 legacy `branch="master"` pages should be `branch=NULL` per architecture semantic categories.
  - **Not urgent** — v5.42.6 already eliminated knob-ON readonly risk. v5.63 is polish. ~1 cal-day estimate.

- **NEW 2026-06-04 night — yadgar config sync dogfooded on this install.** v5.42.6 X5 gap closed retroactively: 275 missing keys added with defaults + section comments; 10 user customizations preserved byte-for-byte. yaml grew 17 → 517 lines. Backup at `~/.yadgar/config.yaml.bak-*`. All sensitive knob values verified matching documented tuning (v5.39 similarity threshold 0.8, v5.42.6 enforcement knobs ON, CB-1 v5.4.2 thresholds, F5-A semaphore N=1, I14 JSON logging).
- **NEW 2026-06-04 night — `nli_reranking_enabled` confirmed deferred to v5.55.0 benchmark.** Currently default-OFF (code comment: "no quality gain over CE alone"). v5.55.0 plan (docs/v5.55.0-plan @8fec70b) runs LongMemEval-s 500q A/B with NLI on vs off. If off wins (or ties), the deferred decision becomes permanent. NLI model: `cross-encoder/nli-deberta-v3-base` (DeBERTa entailment); weight 0.3 if enabled; scope: open-domain queries only.

## Workflow rules (anchored)

- Pure plan/docs → master direct
- Single-isolated-change releases → LOCAL merge + amd64 podman build (`docker.io/openfantasy/yadgar:VER`) + nix bump + user nix-apply. Codeberg master push via HTTPS PAT (SSH busted since 2026-06-01).
- New features → odd minor; patches odd or even; hotfixes patch-version. User may break convention.
- **After EACH ship: read-modify-write the roadmap wiki.** DO NOT pass short snippets to `wiki_update` — use full RMW OR `wiki_append_section` for targeted edits. The 2026-05-31 corruption was caused by short-snippet overwrites.
- **Phase-commit discipline:** 4+ commits per phase + 40k-token STOP threshold per agent dispatch.
- **LLM-in-pipeline = v6 territory:** standalone v5.x LLM features rejected.
- **Worktree-isolation stale-base bug (2026-06-01/02):** isolation=worktree mode keeps spawning agent worktrees from old refs (b6696b5/17058c4). Workaround: dispatch WITHOUT isolation; agent operates on feat branch in main worktree; main thread parks on master.
- **Roadmap-update gap mechanism (2026-06-02):** Stop hook signals don't detect "version bumped since last roadmap update." Investigation + fix-proposal in this session.
- **After EACH ship (v5.41.4+ convention):** use `wiki_append_section(slug='yadgar-roadmap-future-improvements', section_heading='Recently shipped', content='- vX.Y.Z (date): ...', position='start_of_section')`. Reserve full RMW for restructures, table-row edits, or closing open items. See `docs/WORKFLOW_ROADMAP_UPDATE.md` for template. The old "full RMW only" rule is superseded by this convention.

- **POST-SHIP VERIFICATION protocol (mandatory, codified 2026-06-03 night).** When user says "up check" / "check" / "up" / equivalent after a nix-apply confirmation, Claude AUTOMATICALLY:
  1. List changes implemented in the just-shipped release: `git log <prev-tag>..master --oneline` + cite plan doc claims.
  2. Plan per-claim verification scenarios (MCP probes, code-grep checks, metrics curls). One scenario per claim. Identify which run main-thread vs delegate-to-shell-agent.
  3. Execute scenarios sequentially. Both pass/fail captured.
  4. Report find-vs-claim table. Any FAIL → propose hotfix plan immediately (don't wait for next session).
  5. Persist results: anchor verification outcome; append succinct line to roadmap "Recently shipped" entry.
  - Why: v5.42.5 shipped 3 production bugs that passed all 122 unit tests but failed first live probe (migration 016 backfill missed existing rows, §25 4-step resolution hole, schema-data mismatch → 200 pages read-only). v5.42.6 hotfix landed all 3. Post-ship verification catches the unit-vs-prod divergence class.
  - Trigger phrases: "up check", "check", "up", "verify", "live check", "post-ship check". Soft trigger after nix-apply confirmation in same conversation.

- **PLANNING INVARIANTS (P1-P8, mandatory, codified 2026-06-04).** Every yadgar release plan doc MUST include these sections (when applicable). Drafted after v5.42.x cycle pain — 3 shipped regressions + X5 yaml gap + cross-plan overlaps were preventable.
  - **P1 — Canonical architecture conformance.** Plan cites which `docs/architecture.md` sections it conforms to. Required section: `## Architecture Conformance`. Plans that need arch changes propose them in a separate `## Proposed Architecture Updates` section. **`docs/architecture.md` is NEVER modified by a plan — only by an explicit user-approved commit BEFORE implementer dispatch.**
  - **P2 — Invariant register.** Plan lists every existing invariant (I1-I28+) it touches with explicit verb: `preserves` / `changes` / `new`. Required section: `## Touched Invariants`.
  - **P3 — Config knob lifecycle.** Plans adding config knobs MUST include: code default + FIELD_META + registry + **yaml incremental-sync logic for existing users** + env-var precedence note. Required section (when applicable): `## Config Knob Lifecycle`.
  - **P4 — Schema constraint lifecycle.** Plans adding NOT NULL / DEFINE FIELD constraints MUST address in order: existing-row backfill BEFORE constraint, prospective-vs-retrospective enforcement semantics in target DB, rollback path. Plans MUST verify SurrealDB semantics (`IS NONE` vs field-absent, coerce-on-UPDATE) against the live DB version BEFORE designing the migration. Required section (when applicable): `## Schema Constraint Lifecycle`.
  - **P5 — MCP contract changes.** Plans touching MCP tool surface MUST describe: contract changes (caller-visible deltas), carve-outs (`_internal=True` exempt callers), defense-in-depth layers (MCP boundary + drainer), error response shapes. Required section (when applicable): `## MCP Contract Changes`.
  - **P6 — Cross-plan consistency.** Plans MUST cross-reference adjacent in-flight plans on other `docs/v5.*-plan` branches. Conflicts (migration numbers, shared functions, overlapping scope) MUST be resolved explicitly with renumbering or coordinator-designation. Plans MUST reserve shared resources (migration numbers, version slots) before assuming them. Required section: `## Cross-Plan Coordination`.
  - **P7 — Bug class precedent + production write-path test.** Plans MUST cite past bugs of similar class + how proposed tests prevent recurrence. Plans MUST include at least one test that simulates the EXACT production write path (drainer-apply, not direct storage CREATE) where applicable. Plans MUST include post-ship verification probes per the verification protocol. Required sections: `## Bug Class Precedent` + `## Verification Probes`.
  - **P8 — Implementer-ready detail.** Plans MUST be detailed enough that implementer agent dispatches without needing to "figure out" file:line locations, function signatures, test fixture choices, or contract semantics. Required for each targeted edit: file:line, function-signature delta, test fixture reference, expected error response, /metrics delta. "Implementer should figure out X" is plan failure. Applies throughout; no single section.
  - **Bug class precedents from v5.42.x cycle that drove these invariants:**
    - v5.42.5 Bug 1 — SurrealDB `IS NONE` field-absent gap → **P4**.
    - v5.42.5 Bug 3 — NOT NULL applied without backfill → **P4**.
    - v5.42.6 X5 — yaml stale on knob additions → **P3**.
    - v5.42.5 DP-1 — v5.44.0 `_resolve_page_id_by_slug` overlap → **P6**.
    - Multiple v5.42.x — unit-pass + prod-fail divergence → **P7**.
    - v5.42.4 implementer left fixes uncommitted → **P8**.
  - **Future plan template:** new `docs/PLAN_TEMPLATE.md` to be added in next plan cycle, codifying the section skeleton. Existing in-flight plans (v5.42.3-plan, v5.42.4-plan, v5.42.5-plan, v5.44.0-plan, v5.61-plan, v5.62-plan, v5.63-skeleton, v5.43.0-plan) are grandfathered; new plans use template.
  - **Hook codification (deferred):** ship `check_planning_invariants.py` pre-commit hook in v5.44.x cycle (or earlier) that verifies plan docs in `docs/PLAN_V5_*.md` contain required sections. Until then, planning discipline is convention-enforced.

- **PLANNING INVARIANTS P9-P11 (added 2026-06-04 post audit).** Audit of 14 pipeline plans surfaced 3 additional invariant classes worth codifying.
  - **P9 — Rollback / recovery path for irreversible runtime ops.** Plans touching retention purge, daemon restart, external-API mutation, file deletion, etc. MUST document: (a) rollback mechanism (or explicit "no rollback possible" with rationale), (b) last-safe-window before irreversibility, (c) partial-failure recovery flow. P4 covers DB migrations specifically; P9 covers everything else irreversible. Required section (when applicable): `## Rollback Path`.
  - **P10 — External dependency version pinning + upgrade policy.** Plans adding new external deps (PyPI/brew/nix/npm packages, container images, system services) MUST specify: (a) pinned version + lockfile location, (b) upgrade policy (frequency, breaking-change handling), (c) supply-chain risk (CVE monitoring path). Required section (when applicable): `## Dependency Pinning`.
  - **P11 — Agent dispatch cost (tokens / wall-clock / API quota).** Plans dispatching agents for benchmark / long-running work MUST estimate: (a) context-token footprint per dispatch, (b) wall-clock budget, (c) API-quota budget (rough Anthropic spend), (d) phased-dispatch decision (one big agent vs N small). Required section (when applicable): `## Agent Dispatch Budget`.
  - **Audit results (2026-06-04):** 14 pipeline plans audited; 14/14 missing P1; P7 missing in 8/14 (production-path test — v5.42.x root cause); P3 missing in 5 config-knob plans (X5 class); P4 critical gap in v5.49.0 + v5.61 (IS NONE field-absent gotcha mirrors v5.42.5 Bug 1); P6 yellow across all 14. Tier 1 (remediate before dispatch): v5.43.0, v5.44.0, v5.45.0, v5.49.0. 7 split plans never written: v5.45.1, v5.45.2, v5.46.1, v5.48.0, v5.50.1, v5.50.2, v5.55.0.

## License compliance

16 GREEN / 3 YELLOW / 0 RED.

## Invariants

I23 / I24 / I25 / I26 / I27 / I28 in force. I9 governs MCP handler layer only; storage layer + queue worker have own latency budgets (see v5.41.3 plan).

## Open architectural questions

1-17: see prior version (resolved or rolled forward unchanged)
18. ✅ RESOLVED 2026-06-01: Adopt-7 moved to v6.3 sub-release
19. NEW (open): v5.45.0 seed-anchors scope. PLAN_V5_45_0 §6
20. NEW (open): v5.43.0 memory_archive retention DPs
21. ✅ RESOLVED v5.35.1 (Option C)
22. NEW (open): PreCompact hook existence
23. NEW (open): v6.0 scaffolding plan needed
24. NEW (open 2026-06-02): wiki write `wait` flag semantics — should `wait=True` be opt-in only? Should it apply to all 4 wiki write tools or just `wiki_add`? PLAN_V5_41_2.
25. NEW (open 2026-06-02): MCP-handler I9 verification — needs dedicated perf test in v5.41.3. Existing storage-layer test is mis-attributed.

## Branches


- `master` — at `48a0005` (v5.57/58 NLI default correction). Codeberg origin/master synced via HTTPS PAT.
- **Live container:** yadgar 5.44.0 (post-nix-apply 2026-06-04, verified via post-ship probes V1-V5 GREEN).
- **Image registry (local):** docker.io/openfantasy/yadgar:5.42.2 → 5.44.0 all present.
- **Nix repo:** at `d0c422c` (v5.44.0 bump pushed via HTTPS PAT).
- **Plan branches (shipped / merged-to-master):**
  - `docs/v5.42.3-plan` @ `7e803f0` — branch contract (SHIPPED)
  - `docs/v5.42.4-plan` @ `e401ed9` — hardcoded master cleanup (SHIPPED)
  - `docs/v5.42.5-plan` @ `52193cf` — directory contract foundational (SHIPPED, regressions hotfixed in v5.42.6)
  - `docs/v5.42.6-plan` @ `8d12bba` — hotfix backfill + resolution + knobs (SHIPPED)
  - `docs/v5.43.0-plan` @ `6f51bc6` — MCP schema discipline (SHIPPED 2026-06-04 @22188bc)
  - `docs/v5.44.0-plan` @ `55f1c61` — subagent MCP wiring + X1-X5 (SHIPPED 2026-06-04 @46ccb67)
- **Plan branches (drafted, not yet implemented):**
  - `docs/v5.45.0-plan-remediation` @ `3f34965` — setup foundation Linux-only (remediated post P1-P11)
  - `docs/v5.45.1-plan` @ `0eb63e0` — seed-anchors + CLAUDE.md fragment (6 DPs block dispatch)
  - `docs/v5.45.2-plan` @ `206d8b2` — macOS launchd (blocker: no macOS host confirmed)
  - `docs/v5.46.1-plan` @ `94b458d` — cross-repo PR auto-open
  - `docs/v5.48.0-plan` @ `db9da18` — daemon graceful-restart + action=install
  - `docs/v5.49.0-plan-remediation` @ `a96ba3e` — memory_archive retention (P4 IS NONE gap closed)
  - `docs/v5.50.1-plan` @ `705fc0b` — Bookmarks tab refactor
  - `docs/v5.50.2-plan` @ `cfb8d90` — Control tab + restart endpoints (depends v5.47 + v5.48)
  - `docs/v5.55.0-plan` @ `95f4834` — D2 NLI A/B benchmark (default-OFF reframing)
  - `docs/v5.61-plan` @ `c2a3f1b` — repo-wiki yadgar-native
  - `docs/v5.62-plan` @ `961168b` — yadgar CLI
  - `docs/v5.63-skeleton` @ `0881700` — wiki corpus maintenance tools
- `chore/v5.10.4-roadmap-freshness-renumber` — preserved local-only
