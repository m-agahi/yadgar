# Changelog

Authoritative release log. Each entry links to the matching `MIGRATION_NOTES.md` section for full detail.

Format: terse one-line subject per change. Versions ordered newest-first. Tagged releases ship to `docker.io/openfantasy/yadgar:<version>`.

---

## [5.46.11] — 2026-06-06

Hotfix: `yadgar-setup` step 6 fails on pipx fresh install — CLI invocations used system `python3` instead of pipx venv via shim.

- **fix(install):** `yadgar-setup.sh` — replace `run python3 -m yadgar X` with `run yadgar X` at steps 6/7/8/10 (install-hooks, install-subagents, config sync, seed). `python3 -m yadgar` resolves to system python on Rocky Linux / bare Debian; the `yadgar` shim shebang points to the pipx venv python.
- **fix(install):** `yadgar-setup.sh` — add `_resolve_yadgar_version()` helper. Version detection at steps 2/4 (`python3 -c "import yadgar; print(yadgar.__version__)"`) replaced with shim-shebang extraction. Falls back to `"latest"` when shim absent or venv python unusable.
- **fix(install):** `yadgar-setup.sh` — update `_locate_setup_scripts` comment to reflect shim-based design (was: `python3 -m yadgar CLI subcommands instead`).
- **test:** `test_v5_46_11_pipx_cli_invocation.py` — 10 static-analysis tests (4 classes) verify no forbidden invocations remain and helper is wired correctly.
- **chore:** bump version 5.46.10 → 5.46.11
- **note:** `yadgar` CLI lacks `--version` flag; version detection uses shim-shebang workaround. Proper `--version` flag deferred to v5.46.12.

---

## [5.46.10] — 2026-06-06

Hotfix: pipx distribution wheel bundle gap — `yadgar-setup` broken on fresh hosts since v5.45.0.

- **fix(packaging):** `pyproject.toml` `[tool.hatch.build.targets.wheel.shared-data]` — replace single-file `yadgar-setup.sh` mapping with directory-wide `"scripts/install" = "share/yadgar/scripts"`. Wheel now ships all 9 helper scripts (`detect_runtime.sh`, `detect_os.sh`, `install_runtime.sh`, `generate_systemd.sh`, `generate_launchd.sh`, `bootstrap_secrets.sh`, `restore.sh`, `uninstall.sh`, `append_claude_rules.sh`) plus systemd `.in` templates and `launchd/` plist templates.
- **fix(install):** `yadgar-setup.sh` — add fail-fast bundle-integrity check at startup. When any required helper is absent, exits code 2 (vs previous silent fall-through to unhelpful error) with explicit message naming the missing file and actionable workarounds.
- **test:** `test_v5_46_10_wheel_bundle.py` — 18 parametrized assertions verify all required files present in built wheel archive.
- **test:** `test_v5_46_10_yadgar_setup_helper_check.py` — 4 tests verify exit code 2 + explicit error on missing helper.
- **chore:** bump version 5.46.9 → 5.46.10

---

## [5.46.9] — 2026-06-06

Hotfix: bake yadgar-ci Docker image (CI speedup) + F1/F6 test regression fixes from v5.46.7 CI log analysis.

- **fix(test/F1):** `test_branch_auto_capture.py` — add `monkeypatch.delenv('YADGAR_CI_BRANCH', raising=False)` to `test_memorize_branch_none_when_detect_returns_none` and `test_anchor_branch_none_when_non_git`. YADGAR_CI_BRANCH set by CI at workflow level caused env fallback to fire even when tests mocked `detect_branch → None` to assert reject behavior.
- **fix(test/F1):** `test_v5_42_3_drainer_branch_enforcement.py` — same `monkeypatch.delenv` fix added to `test_memorize_missing_branch_hard_rejects`, `test_memorize_hard_reject_no_queue_entry`, `test_memorize_no_branch_returns_error_dict`. Each gains `monkeypatch` fixture parameter.
- **fix(test/F6):** `test_subagent_stop_hook.py::test_endpoint_stores_findings_with_provenance` — `_fake_memorize` lacked `branch_hint=None` parameter; production endpoint calls `memorize(..., branch_hint=...)` causing TypeError → caught silently → `stored=0`. Added `branch_hint=None` to fake signature.
- **feat(ci):** `Dockerfile.ci` updated to v5.46.9: adds `bsdmainutils` (fixes F5 `make help` failure from missing `column` binary), bakes SurrealDB v3.0.5 (saves 15-30s CI per run), bakes HuggingFace `all-MiniLM-L6-v2` weights (saves 30-60s CI per run).
- **feat(ci):** New `Dockerfile.ci-viz` — extends `yadgar-ci:5.46.9` with Playwright + Chromium pre-installed. Splits viz browser layer from core test image (saves ~75s pull time on core jobs).
- **feat(ci):** `ci.yaml` viz-tests job migrated to `yadgar-ci-viz:5.46.9` image; removes 15-line inline `apt-get install` step; adds npm cache step for `viz-tests/node_modules`.
- **feat(ci):** `ci.yaml` + `release.yaml` image refs bumped from `yadgar-ci:5.46.3` → `yadgar-ci:5.46.9`.
- **test:** TDD regression guards — `test_v5_46_9_branch_fallback_conditional.py` (F1 doc), `test_v5_46_9_subagent_stop_findings.py` (F6 guard with correct `branch_hint` param).
- **chore:** bump version 5.46.8 → 5.46.9

---

## [5.46.8] — 2026-06-06

Hotfix: gate Forgejo CI workflows to `workflow_dispatch`-only — internal dev workflow vs production CI separation (PD-45).

- **fix(ci):** ci.yaml `on.push.tags` removed — tag pushes no longer fire any CI jobs. `build` job (multi-arch Docker Build Cloud + dockerhub push) gated to `workflow_dispatch` only.
- **fix(ci):** release.yaml all 4 jobs (build-wheel, build-sbom, attach-to-release, publish-pypi) gated to `workflow_dispatch`. Tag-push trigger subscription kept for future production handoff.
- **docs:** Header comment `WORKFLOW STATE: GATED FOR INTERNAL DEV` added to both workflow files explaining the gate.
- **docs:** PD-45 added to `docs/DECISIONS.md` — codifies internal dev workflow (local amd64 build + nix bump + home-manager switch + manual twine upload) vs production CI (Forgejo, manual-trigger only).
- **deferred:** SBOM cyclonedx-bom install issue in release.yaml build-sbom job — production-transition concern, not internal-dev scope.
- **test:** TDD scaffolding `test_v5_46_8_workflow_triggers.py` — 14 assertions guard trigger gate and header comment.
- **chore:** bump version 5.46.7 → 5.46.8

---

## [5.46.7] — 2026-06-06

Hotfix: daemon-side YADGAR_CI_BRANCH wiring (P1 CRITICAL), hardcoded path removal (P2), os.walk mock target (P7), Makefile runtime-check skip guard (P8), health endpoint empty-body race (P6), export_duckdb unique-pair guarantee (N1), viz_daemon env override reliability (N2), anchor surfacing skip marker (N3).

- **fix(server/tools):** P1 CRITICAL — `memorize`, `anchor`, `checkpoint`, `update_active_work` now read `YADGAR_CI_BRANCH` env var as third fallback in `_detect_branch` chain (after git detection and `branch_hint` kwarg). `YADGAR_CI_BRANCH: master` was added to CI workflows in v5.46.3 but daemon code never consumed it; all four tools returned `missing_branch` on every CI run since v5.46.3.
- **fix(test):** P2 — `test_v565_checkpoint_scoping.py` replaces hardcoded `/home/max/git/yadgar/yadgar/hooks/` paths with dynamic `_REPO_ROOT = Path(__file__).resolve().parents[2]`. Tests now pass in any checkout location.
- **fix(test):** P7 — `test_embed_service_v530.py` `_reload_es()` accepts `db_path` kwarg and sets `YADGAR_DB_PATH` env var so `admin_dbsize`'s `db_path.exists()` guard passes, enabling `_walk_db_sizes`/`os.walk` to be reached by tests.
- **fix(ci):** P8 — `Makefile` pre-setup recipe honors `YADGAR_TEST_SKIP_RUNTIME_CHECK=1` to skip container-runtime detection in CI runners where podman/docker is absent. Env var added to workflow-level `env:` in both `ci.yaml` and `release.yaml`.
- **fix(test):** P6 — `test_transport.py::test_session_count_reflected_in_health` retries once on empty body to mitigate startup race in test fixture (Starlette ASGI lifespan not yet fully started).
- **fix(test):** N1 — `test_export_duckdb.py` `seeded_storage` fixture DELETEs any existing `memory_similarity_link` for the `(memory:1, memory:2)` pair before inserting, avoiding SurrealDB unique-index violation on repeated runs.
- **fix(test):** N2 — `test_viz_daemon_health.py::test_env_override_propagates` patches `yadgar.viz_daemon_health.get_settings` directly (not just `yadgar.core.config.get_settings`) to bypass LRU cache re-fill between `cache_clear()` and `run_health_scraper()`.
- **fix(test):** N3 — `test_anchor_surfacing.py::test_empty_string_directory_context_treated_as_global` re-skip-marked. v5.46.6 attempted to remove the skip by normalising `directory_context=''` → `'global'`, but the test fails due to a separate gate; fix deferred.
- **test:** 3 TDD scaffolding files `test_v5_46_7_*.py` (guard tests for N3 skip marker, N1 unique-pair fixture, P1 env fallback — 8 behavioral tests).
- **chore:** bump `.complexity-baseline.json` for `memorize.py`, `misc.py`, `project.py` after env-fallback lines added.
- **chore:** bump version 5.46.6 → 5.46.7

---

## [5.46.6] — 2026-06-05

Fixes B14 (circuit breaker clock skew), B15 (NLI spy wrong module binding), B18–B21 (SurrealDB missing → install unblocks downstream), carryover (empty-string directory_context normalization). CI green cycle slot 4.

- **fix(ml_client):** B14 — `RemoteMLClient._CircuitBreaker` construction now passes `time_fn=self._now` for all three mode breakers (`ce`, `nli`, `pair`). Without this, test-injected fake clocks diverged from the breaker's internal clock (real monotonic ≈1.1M s vs. fake ≈1.0M+N s), causing premature OPEN→HALF_OPEN transitions in `test_breaker_reopens_on_probe_failure`.
- **fix(test):** B15 — `test_write_time_contradiction.py::test_default_on_fires_detector` spy now patches `yadgar.curation.detect_contradictions` (the bound name from `__init__` import), NOT `yadgar.curation.contradiction.detect_contradictions` (source module). Patching the source module does not intercept calls made via the imported bound name.
- **fix(deps):** B18–B21 — `surrealdb>=1.0.0` added to `[project.optional-dependencies].test` in `pyproject.toml`. SurrealDB 2.0.0 installed into `.venv-test`. This unblocks B17 (health endpoint), B18 (anchor_scope_split), B19 (project_brief_modes), B20 (consolidate_anchor_pass), B21 (consolidation_drainer_metrics) — all previously failing due to `StorageEngine` import error (no surrealdb module).
- **fix(test):** B19 — `test_project_brief_modes.py`: `update_active_work`, `checkpoint`, and `anchor` calls on non-git `tmp_path` directories now pass `branch_hint='master'` so branch-context pre-validation passes.
- **fix(test):** B21 — `test_consolidation_drainer_metrics.py`: drainer test payloads now include `_internal=True` to bypass branch-context pre-validation and reach the patched `_apply_inner`, allowing stage metrics to fire.
- **fix(storage):** Carryover — `insert_memory` normalises `directory_context=''` → `'global'` at write time. SurrealDB 2.x embedded does not reliably round-trip `''` in equality comparisons; this ensures empty-string directory_context anchors surface via the global anchor bucket query. Skip-mark removed from `test_anchor_surfacing.test_empty_string_directory_context_treated_as_global`.
- **fix(test):** Extra — `test_branch_schema_migration.py::_insert_bare_wiki_page` now supplies `directory_context='global'` to comply with migration_016 `DEFINE FIELD ... ASSERT $value != NONE` on `wiki_page`. Without it, SurrealDB rejects the INSERT.
- **test:** 4 TDD scaffolding files `test_v5_46_6_*.py` guarding B14 clock injection, B15 module binding, carryover normalization, B19/B21 branch_hint regression.
- **chore:** bump version 5.46.5 → 5.46.6 + uv.lock sync

---

## [5.46.5] — 2026-06-05

Missing functions, endpoints, hook files (CI green cycle slot 3). Fixes B3 (hook_db_lockdown_check import), B12 (consolidate_now sleep_cycle key). B4/B5/B16/B22 discovered pre-fixed.

- **fix(scripts):** B3 — restore `hook_db_lockdown_check()` to `yadgar/scripts/hook_runner.py`. Function was removed in v5.20.0 (migrated to standalone `yadgar/hooks/db-lockdown-check.py`) but `test_hook_runner_pretooluse_schema.py` still imports it. Restored with correct Python 3 `except` syntax.
- **fix(test):** B12 — `TestConsolidateNowWithSleepCycle::test_consolidate_runs_sleep_cycle` now calls `consolidate_now(mode='full')`. The `sleep_cycle` key is only emitted by mode='full' (v5.10.4+); test was calling default mode='light' and asserting sleep_cycle present.
- **discovery:** B4 (session-start-context.py + stop-memory-checkpoint.py), B5 (/hooks/session-context route), B16 (/viz/config route), B22 (os.walk mock) — all already fixed in prior commits; tests pass with conftest. No code changes needed.
- **test:** 2 RED scaffolding files `test_v5_46_5_*.py` (7 tests).
- **chore:** bump `.complexity-baseline.json` for test_integration.py (+1 LOC).
- **chore:** bump version 5.46.4 → 5.46.5 + uv.lock sync

---

## [5.46.4] — 2026-06-05

Test fixture refactor layer: B1/B8/B9/B10/B11/B13 CI green cycle slot 2. Fixes wiki_page fixtures missing directory_context, 4-dim vector fixtures, token budget overage, hardcoded paths, stale migration assertion, DLQ backoff pre-validation bypass.

- **fix(test):** B1 — add `directory_context='/test/sandbox'` to all positive-path wiki_page INSERT fixtures in `test_wiki_read_resolution.py`, `test_wiki_cleanup_merged_branches.py`, `test_queue_drainer_validation.py`, `test_export_duckdb.py`. Skip-mark `test_empty_string_directory_context_treated_as_global` (schema rejects empty string; deferred to v5.46.6).
- **fix(test):** B8 — update `seeded_storage` fixture in `test_export_duckdb.py`: `embedding_dim=4` → `embedding_dim=384`, `[0.1,0.2,0.3,0.4]` → `[0.0]*384`. Fix all `ExportConfig(embedding_dim=4)` references throughout.
- **fix(server):** B9 — omit `roadmap_update_lag_hours` from `project_brief` signals payload when value is `-1.0` (roadmap wiki page not found), saving 8 tokens. Extract `_omit_sentinel()` helper. Update `test_roadmap_update_signal.py` to handle absent key via `result.get(key, -1)`.
- **fix(test):** B10 — add `_REPO_ROOT = Path(__file__).resolve().parents[2]` to `test_harness_hardening.py`; replace hardcoded `/home/max/git/yadgar` in `cwd=` and `open()` calls.
- **fix(test):** B11 — replace brittle `_MIGRATIONS[-1]["version"] == "014_..."` assertion with membership check in `test_migration_014_wiki_embedding_backfill.py`.
- **fix(test):** B13 — add `branch='master'` and `directory_context='/test/sandbox'` to `memorize`/`wiki_add` payloads in `test_file_queue_dlq.py` so items pass pre-validation and reach retry/backoff mechanics under test.
- **test:** 5 RED scaffolding meta-test files `test_v5_46_4_*.py` (14 tests); all GREEN after fixes.
- **chore:** bump `.complexity-baseline.json` for `project.py` after helper addition.
- **chore:** bump version 5.46.3 → 5.46.4 + uv.lock sync

---

## [5.46.3] — 2026-06-05

CI infrastructure layer: custom yadgar-ci image, YADGAR_CI_BRANCH env var, SBOM workflow fix, pytest-asyncio. Addresses B2 (missing branch in CI), B6 (make not in CI), B7 (pytest-asyncio missing), and SBOM PyPI roundtrip.

- **feat(ci):** `Dockerfile.ci` — new custom CI runner image (`docker.io/openfantasy/yadgar-ci:5.46.3`). Base: `python:3.14-slim`. System deps: `make`, `git`, `curl`, `ca-certificates`, `build-essential`, `nodejs`. Pre-installs pytest, pytest-asyncio, anyio, pytest-xdist, pytest-timeout, pytest-rerunfailures, hypothesis, defusedxml, sentence-transformers, hf-xet. OCI labels. (B6 fix: `make` now available in CI runners)
- **feat(ci):** `.forgejo/workflows/{ci.yaml,release.yaml}` — all `image: python:3.14-slim` job containers replaced with `image: docker.io/openfantasy/yadgar-ci:5.46.3`. Redundant apt-get install steps for make/git/curl removed (now in image). `viz-tests` job keeps chromium-specific apt-get.
- **feat(env):** `YADGAR_CI_BRANCH: master` workflow-level env var added to both workflow files. (B2 fix: daemon branch detection fails on anonymised CI runner paths — env var provides fallback)
- **fix(ci):** `release.yaml` `build-sbom` job: replace PyPI roundtrip (`pip install "yadgar[sbom]==<version>"`) with local wheel install (`pip install "dist/yadgar-<version>-py3-none-any.whl[sbom]"`). Guarantees SBOM is generated from the exact release artifact.
- **feat(deps):** `pyproject.toml` `[project.optional-dependencies].test`: add `pytest-asyncio>=1.4.0` + `anyio>=4.0`. `[tool.pytest.ini_options]`: add `asyncio_mode = "auto"`. (B7 fix: async tests unblocked without per-test `@pytest.mark.asyncio`)
- **test:** 5 new self-test files `test_v5_46_3_*.py` (31 tests covering CI image content, env var, SBOM wheel pattern, image ref, pytest-asyncio extra)
- **chore:** bump version 5.46.2 → 5.46.3 + uv.lock sync

---

## [5.46.2] — 2026-06-05

Runtime detection UX hotfix: OS-aware install hints + optional interactive install + Makefile/yadgar-setup sync. Triggered by user fresh-VM test finding abrupt failure with stale error message.

- **fix(install):** `scripts/install/detect_runtime.sh` — replace stale `"Run: yadgar install"` message with `"yadgar-setup"`; add `YADGAR_TEST_OS_RELEASE` test seam; add OS-aware install hints for 7 distros (Debian/Ubuntu, Fedora/RHEL, Arch, Alpine, openSUSE, macOS) + `ID_LIKE` fallback for derivatives; `--quiet` flag to suppress verbose hints in chained calls; use bash-native `/etc/os-release` sourcing (no grep/sed dependency — NixOS-safe)
- **feat(install):** `scripts/install/install_runtime.sh` — new shared helper (~235 LOC). Interactive prompt ("Install podman now? [Y/n]"); `--install-runtime` (yes-mode) + `--no-install-runtime` (no-mode) flags; `INSTALL_NONINTERACTIVE=1` gate; `YADGAR_TEST_INSTALL_DRYRUN=1` + `YADGAR_TEST_TTY=0|1` test seams; post-install `detect_runtime.sh` retry; DRY — single implementation used by both `yadgar-setup.sh` and `Makefile`
- **feat(install):** `scripts/install/yadgar-setup.sh` — `_offer_install_runtime()` wrapper delegates to `install_runtime.sh`; `_step_detect()` calls it on detection failure; new `--install-runtime` + `--no-install-runtime` flags wired through
- **feat(build):** `Makefile` — `install-runtime` target (calls `install_runtime.sh` with `INSTALL_NONINTERACTIVE` pass-through); `YADGAR_TEST_OS_RELEASE`, `YADGAR_TEST_INSTALL_DRYRUN`, `YADGAR_TEST_TTY` defaults added; `check` target updated to include `test_v5_46_*.py`
- **docs:** `docs/PLAN_V5_46_2_RUNTIME_DETECTION_HOTFIX.md` + `docs/DECISIONS.md` PD-41 + `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN_RETIRED.md` archaeology rename
- **chore:** bump version 5.46.1 → 5.46.2
- **test:** 3 new test files `test_v5_46_2_*.py` (40 tests: detect_runtime hints, install_runtime interactive/noninteractive/flags/retry, Makefile install-runtime)

---

## [5.46.1] — 2026-06-05

Distribution infrastructure prep: PyPI publish via CI on tag push; `scripts/bump_version.py` helper; pre-commit flake.nix sync (already in @53de97a). Brew lane retired (PD-39); nix cross-repo PR retired (PD-40).

- **feat(dist):** `scripts/bump_version.py` — minimal version bumper helper (~90 LOC). Flags: `--new <VERSION>`, `--bump patch|minor|major`, `--dry-run`, `--current-version`, `--project-root`. Pre-commit hooks (sync_version + check_versions) cascade bump to server.json, flake.nix, uv.lock automatically on next commit.
- **feat(ci):** `.forgejo/workflows/release.yaml` — `publish-pypi` job: runs twine upload on tag push only (`if: startsWith(github.ref, 'refs/tags/v')`); depends on `build-wheel`; uses `PYPI_API_TOKEN` Forgejo secret (project-scoped); `--skip-existing` for idempotent re-tag.
- **docs:** `MIGRATION_NOTES.md` v5.46.1 section — no user action required for upgrade; `pipx install yadgar` from PyPI is primary non-nix install path (replaces brew per PD-39); nix users continue with flake (pre-commit auto-syncs flake.nix per PD-40).
- **chore:** bump version 5.46.0 → 5.46.1
- **test:** 3 new test files in `test_v5_46_1_*.py` (23 tests: bump_version script, publish-pypi job, flake sync regression)

---

## [5.46.0] — 2026-06-05

Distribution: pipx + Homebrew + Nix flake + SBOM + Codeberg release automation. `yadgar-setup` binary for non-repo users.

- **feat(dist):** `scripts/install/yadgar-setup.sh` — ~230 LOC standalone setup script for pipx/brew/nix users (Option C: not a CLI subcommand). Flags: `--noninteractive`, `--dryrun`, `--doctor`. Parallels `make setup` chain.
- **feat(dist):** `yadgar/scripts/yadgar_setup.py` — Python shim for `yadgar-setup` pipx entry point (`[project.scripts]`)
- **feat(dist):** `Formula/yadgar.rb.in` — Homebrew formula template with `@VERSION@`/`@SHA256@`/`@PYTHON_VERSION@` placeholders. Caveats-only (no `post_install` auto-exec). `depends_on python@3.13` fallback.
- **feat(dist):** `flake.nix` + `flake.lock` — Nix flake with `packages.default` (yadgar wheel + yadgar-setup binary), `nixosModules.default` stub, `homeManagerModules.default` stub. Channel: `nixos-unstable` (Python 3.14). `nix flake check --no-build` passes.
- **feat(dist):** `scripts/generate_sbom.sh` — CycloneDX 1.5 SBOM via `cyclonedx-bom environment`; writes `dist/yadgar-<version>-sbom.cdx.json`
- **feat(ci):** `.forgejo/workflows/release.yaml` — release automation on `tags: v*`. Active: `build-wheel`, `build-sbom`, `attach-to-release` (Forgejo REST API). Stub (`if: false`): `open-brew-pr`, `open-nix-pr` (v5.46.1 fills).
- **fix(meta):** `pyproject.toml` license classifier: `MIT License` → `Apache Software License` (was pre-existing metadata error; LICENSE file is Apache-2.0)
- **feat(meta):** `pyproject.toml` new classifiers: `POSIX Linux`, `MacOS`, `Console`, `Filesystems`
- **feat(meta):** `pyproject.toml` new extras: `[dist]` + `[sbom]` with `cyclonedx-bom==7.3.0` (pinned exact; resolved 2026-06-05)
- **feat(meta):** `pyproject.toml` `[project.scripts]` `yadgar-setup` entry + `wheel.shared-data` for `yadgar-setup.sh`
- **docs:** `README.md` four install paths (pipx/brew/nix/repo checkout)
- **docs:** `MIGRATION_NOTES.md` v5.46.0 section: install paths + tap creation + secrets + SBOM + deferred items
- **chore:** bump version 5.45.1 → 5.46.0
- **test:** 8 new test files in `test_v5_46_0_*.py` covering all distribution artifacts (68 tests, 3 skipped)

---

## [5.45.1] — 2026-06-04

macOS launchd plist generation + install. **Paper-only implementation** — no macOS host available at time of shipping; runtime verification deferred. Fix-ups via hotfix once host is available. See `MIGRATION_NOTES.md` v5.45.1 for the 5 verification probes to run on first macOS access.

- **feat(install):** `scripts/install/launchd/com.openfantasy.yadgar.plist.in` — core LaunchAgent plist template
- **feat(install):** `scripts/install/launchd/com.openfantasy.yadgar-backend.plist.in` — backend LaunchAgent plist template
- **feat(install):** `scripts/install/generate_launchd.sh` — renders `.in` templates via sed; `plutil -lint` on macOS, skip on Linux with warning; `YADGAR_LAUNCHD_OUTPUT_DIR` default `~/Library/LaunchAgents`; creates `~/Library/Logs/yadgar/`
- **feat(install):** `scripts/install/detect_os.sh` — adds `YADGAR_TEST_OS_MARKER=macos` test hook for cross-platform macOS spoofing
- **feat(install):** `scripts/install/detect_runtime.sh` — adds `YADGAR_TEST_PODMAN_MACHINE_SOCKET` sentinel (DP-C); macOS-specific podman-machine failure message
- **feat(install):** `Makefile` — `setup` target routes to `generate_systemd.sh` (linux) vs `generate_launchd.sh` (macos); `enable-units-macos` target with `launchctl bootstrap gui/$UID` (macOS 11+) / `launchctl load -w` (10.15) fallback; `_enable-units-auto` dispatcher
- **feat(install):** `scripts/install/uninstall.sh` — macOS path: `launchctl unload` + rm plists; `--purge` also removes `~/Library/Logs/yadgar/`; `YADGAR_TEST_OS_MARKER` test hook
- **chore:** bump version 5.45.0 → 5.45.1
- **test:** 54 new tests in `test_v5_45_1_*.py` (cross-platform render + install + detect + uninstall + Makefile routing); 5 skipped (darwin-only runtime probes); `defusedxml` added to test dependencies for safe plist XML validation

---

## [5.45.0] — 2026-06-04

Setup Foundation (Linux-only, make-canonical). `make setup` is the single install entrypoint. Container runtime detection: podman-first → docker → error with `YADGAR_CONTAINER_RUNTIME` override. NixOS guard: refuses install with nix flake suggestion. systemd unit templates (`.in` files) rendered by `generate_systemd.sh`. `check_docker()` → `check_runtime()` in daemon (backward-compat alias kept). Seed anchors: `yadgar seed --anchors <file>` with content-hash dedup. CLAUDE.md fragment with idempotent append. Uninstall preserves data by default; `make uninstall-purge` for full wipe. 64 new tests.

- **feat(install):** top-level `Makefile` with GNU make guard + NixOS refusal in `pre-setup`
- **feat(install):** `scripts/install/detect_runtime.sh` — podman-first detection, `YADGAR_CONTAINER_RUNTIME` env override
- **feat(install):** `scripts/install/detect_os.sh` — linux-nixos / linux / macos output, `YADGAR_TEST_NIXOS_MARKER` test hook
- **feat(install):** `scripts/install/generate_systemd.sh` — renders `.in` templates; nix-symlink guard rejects managed units
- **feat(install):** systemd unit templates: `yadgar.service.in`, `yadgar-backend.service.in`, `yadgar.target.in`
- **feat(install):** `scripts/install/uninstall.sh` — preserves `~/.yadgar/` by default; `--purge` removes it
- **feat(install):** `scripts/install/append_claude_rules.sh` — idempotent CLAUDE.md fragment append via `YADGAR-RULES-BEGIN` marker
- **feat(assets):** `install_assets/CLAUDE.md.fragment` with begin/end markers; `install_assets/seeds/anchors.yaml` (8 canonical anchors)
- **feat(cli):** `yadgar seed --anchors <file>` flag; content-hash dedup; `--dry-run` support
- **feat(daemon):** `YadgarDaemon.check_runtime()` replaces `check_docker()`; `_RUNTIME` module var + `_get_runtime()` helper; `check_docker()` kept as alias
- **chore:** `pyproject.toml` `[tool.hatch.build.targets.wheel.shared-data]` — ships `install_assets/` in wheel
- **chore:** bump version 5.44.0 → 5.45.0
- **test:** 64 new tests in `test_v5_45_*.py` covering all shell scripts, daemon migration, CLI flags

---

## [5.44.0] — 2026-06-04

Subagent MCP wiring + 5 automation extensions (X1-X5). Base: per-agent allowlist via bundled agent templates (`yadgar/install_assets/agents/`). X1: `agent_dispatch_prelude` extended with `branch_hint`/`directory`/`subagent_type`/`include_context` params for auto-prefetch (opt-in per DP-X1-1). X2: `SubagentStop` hook extended with `_parse_directive` (memorize/wiki_add/anchor grammar) + `branch_hint` forwarding in POST payload (regression guard for v5.42.2 precedent). X3: `platform_paths.py` — OS-detection helpers for Linux/macOS/Windows Claude Code config paths, no hardcoded `/home/max` paths. X4: `yadgar install-subagents` CLI subcommand — copies bundled agent templates to `~/.claude/agents/`, idempotent, `--check`/`--force`/`--dry-run`, nix carve-out. X5: `yadgar config sync` CLI subcommand — incremental YAML sync adds missing Settings fields with defaults + FIELD_META comments, preserves user values, idempotent, `--check`/`--dry-run`.

- **feat(install):** bundled agent templates at `yadgar/install_assets/agents/` — `general-purpose.md`, `Explore.md`, `cavecrew-investigator.md`, `cavecrew-builder.md`, `cavecrew-reviewer.md`
- **feat(dispatch):** `agent_dispatch_prelude` + `_build_context_block` — X1 auto-prefetch context using v5.43.0 `recall(directory, branch_hint)` + `wiki_query(directory, branch_hint)` signatures; opt-in via `include_context=True` (DP-X1-1)
- **feat(hooks):** `subagent_stop.py` gains `_parse_directive` + `_detect_branch_from_cwd` + `branch_hint` in POST payload (X2); structured directive grammar: `memorize:`, `wiki_add:`, `anchor:` per DP-X2-1
- **feat(platform):** `yadgar/platform_paths.py` — `get_claude_config_dir()`, `get_claude_agents_dir()`, `get_claude_settings_path()`, `is_nix_managed()` (X3)
- **feat(cli):** `yadgar install-subagents` subcommand via `yadgar/cli/install_subagents.py` + `yadgar/install_subagents_lib.py` (X4)
- **feat(config):** `yadgar config sync` subcommand via `cmd_config_sync` in `config_yaml.py` — fixes recurring knob-invisibility bug class (X5)
- **chore:** bump version 5.43.0 → 5.44.0 (pyproject.toml, docker-compose.yml, server.json, uv.lock)
- **test:** 48 new tests in `test_v5_44_0_subagent_mcp_wiring.py` covering base templates, X1-X5, production write-path

---

## [5.43.0] — 2026-06-04

MCP schema discipline — caller-context enforcement across the full MCP surface. Two primary fixes: (1) `wiki_query` gains `directory` + `branch_hint` parameters, eliminating daemon-CWD branch resolution and scoping results to caller directory; (2) `recall` gains `branch_hint` parameter, enabling container-deployed agents to supply branch context for memory retrieval. Both fixes use the established resolution chain: `_detect_branch(directory)` → `branch_hint` → `None`. Phase 3: `wiki_approve` branch inheritance confirmed and returned in result dict (DP-2). Design points resolved: DP-1 (directory canonical, branch_hint secondary), DP-2 (wiki_approve inherits draft branch), DP-3 (hard-reject from v5.43.0, no warn period).

- **feat(wiki):** `wiki_query` gains `directory: str | None = None` + `branch_hint: str | None = None` — scopes results to caller directory, uses branch_hint for §25 filter when daemon CWD unreliable (v5.43.0)
- **feat(recall):** `recall` gains `branch_hint: str | None = None` — enables container agents to pass branch context; resolution order: `_detect_branch(directory or os.getcwd())` → `branch_hint` → `None` (DP-1)
- **fix(wiki):** `wiki.add()` now includes `branch` in returned page dict — `wiki_approve` result carries propagated branch (DP-2 branch inheritance)
- **test:** 19 new tests in `test_v5_43_0_mcp_schema_discipline.py` covering Q1-Q4 (wiki_query), R1-R4 (recall), A1-A3 (wiki_approve inheritance), V1-V5 (v5.42.5 regression guards), B1-B2 (v5.42.5 boundary guards), I1 (long-running agent integration)

---

## [5.42.6] — 2026-06-03

Directory backfill repair + wiki_read resolution hole + enforcement knobs. Three production bugs fixed: (Bug 1) migration 016 Phase A missed all field-absent rows via `IS NONE` — migration 018 re-backfills using a Python-side filter + numeric ID extraction; (Bug 2) `wiki_read` called daemon-side `_detect_branch` (returns None in containers) making branch="master" rows unreachable — `branch_hint` parameter added symmetric with `wiki_add`; (Bug 3) `wiki_update`/`wiki_append_section`/`wiki_restore` failed on legacy rows with ASSERT coerce error — schema temporarily relaxed during migration 018 backfill. Two new operator escape-hatch knobs added.

- **feat(storage):** migration 018 — re-backfill field-absent `wiki_page.directory_context` rows using tag heuristic + Python-side filter (fixes `IS NONE` miss from migration 016)
- **fix(storage):** migration 016 Phase A source fix — replaced `WHERE directory_context IS NONE` query with fetch-all + Python filter to catch field-absent rows; numeric ID extraction via `_extract_id()` to fix silent `type::record()` failures
- **feat(wiki):** `wiki_read` gains `branch_hint: str | None = None` — when daemon `_detect_branch` returns None (container), `branch_hint` supplies the branch for §25 step 1 lookup
- **feat(config):** `YADGAR_DIRECTORY_ENFORCEMENT` (default true) — set to false to relax directory_context requirement in drainer; emits WARN + `yadgar_writes_with_enforcement_relaxed{enforcement="directory"}` metric
- **feat(config):** `YADGAR_BRANCH_ENFORCEMENT` (default true) — set to false to relax branch requirement in drainer for wiki_add and memorize; emits WARN + `yadgar_writes_with_enforcement_relaxed{enforcement="branch"}` metric
- **feat(metrics):** `yadgar_writes_with_enforcement_relaxed{enforcement}` Counter — tracks relaxation events per enforcement type (I23)

---

## [5.42.5] — 2026-06-03

Directory contract — every wiki_page and memory row now has `directory_context` NOT NULL. MCP boundary rejects wiki_add / block_* / agent_prompt_save without `directory`. Drainer pre-apply validates and routes missing-directory records to DLQ. §25 4-step resolution extended with directory scoping. Three bug fixes: F1 `_resolve_page_id_by_slug` uses caller directory instead of daemon CWD; F2 `agent_prompt_save` routes through wiki machinery; F3 block tools enforce directory for `scope='project'`.

- **feat(storage):** migration 016 — `directory_context` NOT NULL on `wiki_page` and `memory`; backfill via tag heuristic; `wiki_draft.directory_context` nullable column
- **feat(wiki):** `wiki_add` / `wiki_read` / `wiki_list` / `wiki_check_duplicate` + derivative tools gain `directory` param; §25 4-step resolution: project+branch → project+canonical → global+canonical → not found
- **feat(boundary):** hard-reject `missing_directory` when `wiki_add` / `block_create` / `block_get` / `block_update` / `block_delete` / `block_replace` / `block_append` (scope='project') called without directory
- **feat(drainer):** `_validate_wiki_add` check #5 — DLQ routing with `failure_reason=missing_directory` for external writes lacking `directory_context`
- **feat(recall):** `recall` post-filter scopes to caller directory when supplied
- **fix(F1):** `_resolve_page_id_by_slug` accepts `directory`+`branch_hint` from caller — fixes daemon-CWD lookup bug
- **fix(F2):** `agent_prompt_save` routes through `_wiki.add()` machinery, storing `directory_context`
- **fix(F3):** block tools return `{"ok": false, "error": "missing_directory"}` for `scope='project'` without directory

---

## [5.42.4] — 2026-06-03

Hardcoded `"master"` exception-fallback cleanup. 5 sites previously fell back to `"master"` when `_get_default_branch()` raised — wrong on `main`-default repos and on no-git contexts. All replaced with `None` (canonical slot, reachable via §25 step 3).

- **fix(wiki):** `wiki_query` / `wiki_read` / `wiki_check_duplicate` / `_resolve_page_id_by_slug` default-branch fallback `"master"` → `None`
- **fix(recall):** same fallback `"master"` → `None`
- **fix(export):** `v_branch_distribution` view `COALESCE(branch, 'master')` → `COALESCE(branch, '(canonical)')` for display correctness
- **test:** 6 new RED tests in `test_v5_42_4_master_fallback_cleanup.py` simulating `_get_default_branch` failure; all GREEN post-fix

Deferred: `_get_default_branch_cached` final fallback (project.py:185) — return type cascade to all callers; defer to v5.43+.

---

## [5.42.3] — 2026-06-03

Drainer branch enforcement + memory write branch_hint parity. All write tools (memorize, anchor, checkpoint, update_active_work, wiki_add) now hard-reject at MCP boundary when branch context is absent. Drainer pre-apply stage validates branch on queued records and routes to DLQ with `missing_branch` reason if absent. `dlq_requeue` blocks `missing_branch` entries without `force=True`.

- **feat(v5.42.3):** hard-reject gate on all write ops — `memorize`, `anchor`, `checkpoint`, `update_active_work`, `wiki_add` return `{"error": "missing_branch", "stored": false}` when `_detect_branch()` fails and no `branch_hint` supplied
- **feat(dlq):** `_validate_wiki_add` + `_validate_branch_context` mixin on `QueueDrainer` — drainer pre-apply validates branch presence, routes missing-branch records to DLQ
- **feat(storage):** migration 015 — `wiki_draft.branch` column; `insert_wiki_draft` stores branch; `wiki_approve` reads and propagates it
- **feat(metrics):** `yadgar_dlq_rejection_count` Gauge — tracks DLQ rejection counts by `failure_reason`
- **feat(admin):** `dlq_requeue` blocks `missing_branch` entries without `force=True`; `force=True` allowed only after operator patches branch into payload
- **test:** 28 TDD tests in `test_v5_42_3_drainer_branch_enforcement.py` covering full branch enforcement contract

## [5.42.2] — 2026-06-02

Critical hotfix: wiki branch-default scope mismatch — silence similarity gate in production (real root cause).
See `MIGRATION_NOTES.md` v5.42.2 and `docs/PLAN_V5_42_2_WIKI_BRANCH_DEFAULT_FIX.md`.

- **fix(file_queue):** `_fill_wiki_add_defaults` — drainer no longer injects hardcoded `branch="master"` when payload omits branch. Now stores `branch=None` (canonical slot), matching the `wiki_add` direct-write path. Both writer paths now agree on the canonical slot. (`yadgar/file_queue/dlq.py:133`)
- **fix(wiki):** `wiki_check_duplicate` — auto-detects current/default branch via `_detect_branch` / `_get_default_branch` when `branch` arg is `None`, mirroring `wiki_query`. Passes `_default_branch` to `find_similar_wiki_pages` so scope = `{None, default_branch}` covers both canonical-slot pages (post-fix) and legacy `branch="master"` pages (pre-fix). (`yadgar/server/tools/wiki.py:695-720`)
- **test(wiki):** `test_v5_42_2_branch_default_e2e.py` — new `@pytest.mark.integration` E2E test reproducing the production sequence: drainer write (no branch) → `wiki_check_duplicate` (no branch) → assert candidate found. RED before this fix, GREEN after.
- **chore(tests):** `test_branch_filled_with_master_when_absent` → renamed to `test_branch_left_as_none_when_absent`; assertion updated to `branch is None`. (`yadgar/tests/test_queue_drainer_validation.py:84`)

**Breaking change (no known callers):** drainer no longer sets `branch="master"` as a default. Any external caller that relied on the drainer to inject `branch="master"` must now pass `branch="master"` explicitly. No callers in this codebase depend on the old behavior.

**Root cause summary:** four prior fix attempts (v5.39.0, v5.41.5, v5.42.0, v5.42.1) targeted the wrong layers (embedding gaps, gate location, backfill). The actual bug: writer asymmetry. Drainer wrote `branch="master"`; `wiki_check_duplicate` searched `{None}`. The two canonical slots never overlapped. Live probe 2026-06-02 confirmed: same content, `branch=None` → 0 candidates, `branch="master"` → 1 candidate at similarity 0.9055.

## [5.42.1] — 2026-06-02

Critical hotfix: wiki_page embedding backfill + embed-failure surfacing.
See `MIGRATION_NOTES.md` v5.42.1 and `docs/PLAN_V5_42_1_WIKI_EMBEDDING_BACKFILL.md`.

- **fix(storage):** migration_014 — backfill wiki_page embeddings on NULL rows. ~1.9k production rows shipped pre-v5.39 with `embedding=NULL`. SurrealDB KNN silently excludes NULL rows → `find_similar_wiki_pages` returned 0 candidates → similarity gate never fired.
- **fix(storage):** `get_wiki_pages_without_embedding()` handles both SurrealDB `NONE` and JSON `null` (distinct types — null from Python params, NONE from SQL literal). `update_wiki_page_embedding_only()` sets embedding without creating version row (backfill is not a content change).
- **feat(wiki):** `WikiStore.backfill_null_embeddings()` — idempotent, per-row exception handling, batch-able (default batch_size=50), logs progress. Called from `lifecycle.py` post-`init_engines()` after both StorageEngine + EmbeddingEngine ready.
- **feat(wiki):** `_compute_embedding` now emits WARN log + `yadgar_wiki_embedding_compute_failed_total{reason}` Prometheus counter on failure (reason: `exception` | `returned_none`). Was previously a silent debug log.
- **feat(config):** `WIKI_EMBED_FAILURE_BLOCKS_WRITE: bool = False` — I25 three-way registered. Default False preserves backward compat. Set True to enforce embedding-on-write.
- **feat(lifecycle):** post-backfill CRITICAL log if NULL-embedding rows remain (embed service unavailable → similarity gate still degraded).
- **tests:** 38 new tests across 3 test files (RED bug reproduction + migration 014 + embed failure surfacing). 1 new `@pytest.mark.integration` E2E smoke test confirming gate fires on real near-clone post-backfill.

## [5.42.0] — 2026-06-02

Async rejection tracking via DLQ + Stop hook signal.
See `MIGRATION_NOTES.md` v5.42.0 and `docs/PLAN_V5_42_0_ASYNC_REJECTION_NOTIFICATION.md`.

- **feat(dlq):** `failure_reason` taxonomy in DLQ entry schema (`permanent_error` default; new `duplicate_detected`). `failure_metadata` carries candidates, threshold, and `caller_context.directory`.
- **feat(drainer):** Similarity gate rejections now route to DLQ (not archive) with `failure_reason="duplicate_detected"`. `wait=True` callers still receive sync rejection payload (v5.41.5 contract preserved).
- **feat(metrics):** `yadgar_dlq_rejection_count` Gauge — current count of DLQ rejection entries. Written per drain cycle.
- **feat(dlq):** `dlq_inspect(filter=...)` — new optional param: `"all"` (default), `"rejections"`, `"failures"`. Result includes `failure_reason` field.
- **feat(dlq):** `dlq_requeue` blocks rejection entries (`duplicate_detected`) with helpful error pointing to `force=True`, `wiki_delete`, or `dlq_dismiss` alternatives.
- **feat(dlq):** `dlq_dismiss(filename)` — new power-gated MCP tool. Removes DLQ entry without retry. I26: no user content, no secret scan needed.
- **feat(project_brief):** `pending_rejections_count` signal in `mode="signals"`. Counts DLQ rejections filtered by `caller_context.directory`. `review_rejections` recommended action fires when count > 0.
- **tests:** 33 new tests across 3 test files covering taxonomy, drainer push, filter, requeue block, dismiss, signal, action, cross-directory isolation.

## [5.41.3] — 2026-06-02

MCP-handler perf test + I9 attribution correction.
See `MIGRATION_NOTES.md` v5.41.3.

- **test(wiki):** `test_wiki_mcp_handler_perf.py` — new test times `wiki_add(wait=False)` MCP handler directly (100 calls, real file queue dir). Asserts p50 ≤5ms (true I9 budget). Marked `xfail(strict=True)`: current baseline p50 ≈ 28–48ms (5.8–9.6× over budget). Real fix slot: v5.41.5.
- **refactor(tests):** `TestUpdatePerfUnder5msP50` → `TestStorageUpdatePerfRegressionGuard`. Docstring corrected: storage-layer latency (~89ms embedded SurrealKV) is a queue-worker concern, NOT an I9 violation. I9 governs MCP handlers only.
- **docs:** MIGRATION_NOTES v5.41.3 clarifies the layer model (handler vs. storage) and attributes the ~89ms baseline correctly.

## [5.41.2] — 2026-06-02

`wiki_add` / `wiki_update` / `wiki_restore` / `wiki_append_section` wait flag for read-your-writes consistency.
See `MIGRATION_NOTES.md` v5.41.2.

- **feat(wiki):** `wiki_add(wait=True)` — bypasses async queue and writes synchronously; returns `{"committed": true, "queued": false}`. Eliminates need for `sleep(N)` before `wiki_history` in tests and interactive callers.
- **feat(wiki):** `wiki_update`, `wiki_restore`, `wiki_append_section` now accept `wait=True` for API symmetry (no-op — all three are already synchronous).
- **feat(queue):** `FileQueue.enqueue()` now returns `job_id` (UUID) instead of file path. `register_wait()` / `signal_complete()` / `wait_for_job()` added for per-job completion tracking infrastructure.
- **feat(config):** `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` (default 5.0) — I25 three-way registered.
- **docs:** `wiki_history` docstring updated: use `wait=True` on preceding write to avoid stale reads.
- **tests:** 21 new tests (10 Phase 1 queue + 11 Phase 2 wait flag); all 45 v5.41.0+v5.41.1 tests still green.

## [5.41.1] — 2026-06-02

Wiki versioning transactional atomicity hotfix.
See `MIGRATION_NOTES.md` v5.41.1.

- **fix(wiki/storage):** `insert_wiki_page` and `update_wiki_page` now wrap `wiki_page` + `wiki_page_version` mutations in a single `BEGIN TRANSACTION … COMMIT TRANSACTION` compound statement. Either both rows commit or both roll back.
- **tests:** 7 atomicity regression tests in `test_wiki_versioning_atomicity.py` (RED in v5.41.0, GREEN after fix).

## [5.41.0] — 2026-06-01

Wiki versioning + section-patching — closes the 2026-05-31 corruption class.
Migration 013 seeds version history for all existing wiki pages.
See `docs/PLAN_V5_41_0_WIKI_VERSIONING.md` and `MIGRATION_NOTES.md` v5.41.0.

- **feat(wiki/storage):** `wiki_page_version` table — per-write snapshot of every wiki page field except embedding. Version row written on every `insert_wiki_page` and `update_wiki_page` call. Hash-identical content still creates a version.
- **migration 013:** `_migration_013_wiki_page_version` — DDL + seed `version=1` from all existing `wiki_page` rows. Idempotent. Three indexes: `page_id`, `(page_id, version) UNIQUE`, `created_at`.
- **feat(wiki/tools):** `wiki_history(slug, limit=20)` — version history list, newest first, no content (light payload).
- **feat(wiki/tools):** `wiki_read_version(slug, version)` — full snapshot of any historical version.
- **feat(wiki/tools):** `wiki_diff(slug, v1, v2, fmt='unified'|'json')` — compare two versions.
- **feat(wiki/tools):** `wiki_restore(slug, version)` — restore to prior version as new version N+1. Bypasses v5.39 similarity gate (explicit recovery). Rebuilds embedding + crossrefs.
- **feat(wiki/tools):** `wiki_append_section(slug, heading, content, position)` — section-atomic write. Prevents full-content overwrites. Supports `Pipeline#2` disambiguation. `power=True` + secret-gated.
- **feat(wiki):** `_compute_change_summary` — pure-Python difflib stats + section headings. No LLM (I9).
- **tests:** 38 tests in `test_wiki_versioning.py`. Extended `test_wiki.py` + `test_memory_update_wiki_update.py`.
- **recovery:** Future corruption → `wiki_restore(slug, N-1)` instead of 90-minute archive dig.

## [5.39.0] — 2026-06-01

Wiki similarity gate — blocks near-duplicate page creation (prevents 2026-05-30 corruption class).

- **feat(wiki):** `wiki_add()` now rejects near-duplicate pages (cosine similarity ≥ 0.80 on combined title+content embedding) with `{"stored": false, "reason": "duplicate_detected", "candidates": [...]}`. Bypasses: `force=True`, `replace_slug=<slug>`, `append=True`.
- **feat(wiki):** `wiki_check_duplicate(title, content, branch?, threshold?, top_k?)` — dry-run MCP tool to probe for duplicates without writing.
- **feat(wiki):** `WikiStore.find_similar_wiki_pages()` — branch-scoped KNN search against HNSW vector index with configurable threshold.
- **feat(config):** 5 new env knobs — `WIKI_SIM_GATE_ENABLED`, `WIKI_SIM_CONTENT_THRESHOLD`, `WIKI_SIM_MODE`, `WIKI_SIM_TOP_K`, `WIKI_SIM_TITLE_THRESHOLD` — registered in all three config layers (I25 compliant).
- **calibration:** Threshold 0.80 calibrated on 7 sample pairs with all-MiniLM-L6-v2; near-dup cluster 0.956–0.993, distinct cluster 0.439–0.714, separation margin 0.242.
- **tests:** 18 unit tests + 1 calibration test in `test_wiki_similarity_gate.py` and `test_wiki_sim_calibration.py`; real embeddings, no mocks.
- **I26:** `wiki_check_duplicate` marked `# secret-gate: skip` (read-only dry-run).
- **I25:** All 5 knobs registered in `config.py`, `config_registry.py`, `config_yaml.py`.
## [5.35.1] — 2026-06-01

Hotfix bundle: memory-block follow-ups + `_MEMORY_UPDATABLE_FIELDS` fix.

- **feat(blocks/I25):** Four `MEMORY_BLOCK_*` knobs (`MAX_PER_SCOPE`, `DEFAULT_CHAR_LIMIT`, `HARD_CHAR_LIMIT`, `TOTAL_BUDGET_CHARS`) registered in `config.py` + `config_registry.py` + `config_yaml.py`. Storage layer reads from config instead of module constants. Env-overridable.
- **feat(blocks/tools):** Two new MCP patch tools: `block_replace` (string-replace, errors on 0 or >1 matches) and `block_append` (append with newline, char_limit enforced). Both `power=True`, secret-gated (I26).
- **feat(hooks/block-reflect):** PostToolUse `block-reflect` handler fires after any `block_create/update/delete/replace/append` call and re-injects updated block content into next context. Registered via `install_hooks` as second PostToolUse entry.
- **feat(hooks/session-start):** `session-context` endpoint now prepends `## Memory Blocks` section to SessionStart context output (non-compact sources only).
- **fix(memory):** `last_accessed` and `access_count` added to `_MEMORY_UPDATABLE_FIELDS` — both silently no-op'd in `memory_update()` since initial implementation.
- **feat(test):** `test_memory_updatable_fields.py` — invariant test that asserts every non-internal memory field is in `_MEMORY_UPDATABLE_FIELDS`. Prevents future regressions of this class.
- **refactor:** `_render_blocks_section` extracted to `yadgar/blocks_render.py` (DRY shared helper used by restoration, session-context, block-reflect).
- **decide:** `_active_work` canonicalization — Option C (defer to v5.50+). See `docs/DECISIONS.md`.
- **chore:** Version bump `5.35.0 → 5.35.1`.

See [MIGRATION_NOTES.md §v5.35.1](MIGRATION_NOTES.md#v5351--memory-block-follow-ups-2026-06-01).

---

## [5.35.0] — 2026-06-01

JavaScript/TypeScript SDK release (Adopt-5 from 2026-05-30 competitor audit).

- **feat(sdk-js):** `@yadgar/sdk` v0.1.0 — typed thin client for all 53 MCP tools exposed by yadgar. Streamable HTTP transport via `@modelcontextprotocol/sdk`. Bearer token auth. ESM+CJS+types output. 73 unit tests (vitest). See `sdk-js/README.md` and `docs/sdk-js.md`.
- **ci:** `.github/workflows/sdk-js.yml` — test + publish pipeline gated on `sdk-js/**` path changes. Publish job fires on `sdk-js/v*` tags only.
- **docs:** `docs/sdk-js.md` pointer doc. Root `README.md` mentions JS SDK.
- No Python server changes. Zero migration required for existing Claude Code / Python consumers.
## [5.33.0] — 2026-06-01

In-context memory blocks (Adopt-4 Letta-style core memory primitive).

- **feat(blocks):** New `memory_block` primitive — named, scoped, char-capped text containers editable via MCP and always-injected on `restore()`. Five new MCP tools: `block_create`, `block_get`, `block_update`, `block_delete`, `block_list`. Two scopes: `project` (per-directory) and `global` (cross-project).
- **feat(migration 012):** New `memory_block` SurrealDB table with indexes on `(name, scope, directory)`. Additive, no existing data touched.
- **feat(restore):** `restore()` now prepends a `## Memory Blocks` section to its formatted markdown output. Global blocks rendered first, then project blocks for the current directory.
- **feat(bootstrap):** `bootstrap_project` seeds two empty default blocks per project: `current_task` (agent running state) and `gotchas` (non-obvious facts). Idempotent — re-running does not overwrite existing content.
- **chore:** Version bump `5.31.1 → 5.33.0`.

See [MIGRATION_NOTES.md §v5.33.0](MIGRATION_NOTES.md#v5330--in-context-memory-blocks-2026-06-01).

---

## [5.31.1] — 2026-06-01

Hotfix bundle: graph filter tests + MCP recall() pipeline kwargs.

- **fix(graph):** restore entity nodes in `get_full_graph()` so causal edges survive the orphan filter. Root cause: v5.0.0 monolith split removed `entity:*` nodes; every causal edge was silently dropped before returning, making `include_invalidated` filtering unobservable. Fix adds `_assemble_entity_nodes()` helper. Fixes 2 pre-existing `test_bitemporal_edges` failures.
- **feat(mcp):** `recall()` MCP tool now accepts `profile: str | None` (`"fast"` / `"balanced"` / `"full"` / `"debug"`) and `stage_overrides: dict[str, dict] | None`. When `profile=None` (default) behavior is unchanged. When set, routes through `Retriever.recall_via_pipeline()` and emits `yadgar_recall_profile_invocations_total{profile=...}`. Invalid profile raises `ValueError` before any retrieval work (I3).

---

## [5.31.0] — 2026-06-01

Recall pipeline plugin architecture (Adopt-R2 from 2026-05-30 competitor audit).

- **`RetrievalStage` ABC** (`yadgar/retrieval/stages/base.py`) — interface every stage implements: `name`, `apply(state)`, optional `is_enabled(profile, config)`.
- **`RetrievalState` dataclass** (`yadgar/retrieval/state.py`) — single inter-stage carrier (query, scores, embeddings, candidates, stats, branch context, profile).
- **`RetrievalPipeline`** (`yadgar/retrieval/pipeline.py`) — ordered stage orchestrator with per-stage timing, Prometheus metrics, per-call stage overrides, composite post-fusion dispatch.
- **11 stage wrappers** (`yadgar/retrieval/stages/`): `query_analysis`, `fts`, `knn`, `ppr`, `spreading`, `temporal`, `fusion`, `ce_rerank`, `nli`, `mmr`, `adversarial`, `rules` — each delegates to the existing `_collect_*` / `_apply_rerank_pipeline` methods; no computation moved.
- **`recall_via_pipeline()`** on `Retriever` — functionally identical to `recall()` with `profile="balanced"`, backed by the plugin pipeline. Legacy `recall()` unchanged.
- **`recall_compare()`** (`yadgar/retrieval/compare.py`) — A/B harness: runs the same query under multiple profiles side-by-side; returns results + per-stage timing for each profile.
- **4 new Prometheus metrics**: `yadgar_recall_stage_duration_seconds{stage,profile}` histogram, `yadgar_recall_stage_candidates_in{stage,profile}` gauge, `yadgar_recall_stage_candidates_out{stage,profile}` gauge, `yadgar_recall_profile_invocations_total{profile}` counter.
- **Profiles** (`yadgar/retrieval/profiles.py`): `fast`, `balanced`, `full`, `debug`. Balanced = current default behavior. All existing `profile["cross_encoder"]` / `profile["nli"]` / `profile["multi_passage"]` dict accesses preserved for backward compat.
- **29 new tests** in `yadgar/tests/test_retrieval_pipeline.py` — Phases 0/2/3/4/5/6; regression tests confirm `recall_via_pipeline(profile="balanced")` produces bit-identical output to legacy `recall()`.
- **No behavior change** — `recall()` untouched; existing callers unaffected.

See [MIGRATION_NOTES.md §v5.31.0](MIGRATION_NOTES.md#v5310--recall-pipeline-plugin-architecture-2026-06-01).
## [5.29.0] — 2026-06-01

Bi-temporal edges extension (Adopt-3) — user_profile and derived_belief.

- **Schema migrations 010 + 011**: `valid_from` / `valid_until` added to `user_profile` and `derived_belief` tables. Backfills `valid_from = created_at` on existing rows. Migration 010 drops the old unconditional UNIQUE index on `user_profile` (replaced by app-side uniqueness enforced in `insert_profile`).
- **`insert_profile` pivoted to close-and-insert**: When `attribute_value` changes or confidence delta ≥ `PROFILE_BITEMPORAL_VERSION_DELTA` (env knob, default `0.05`), the existing row is closed (`valid_until = now()`) and a new row is inserted. Minor confidence drift folds into an in-place update to bound row growth.
- **`insert_belief` gains `supersede=True` default**: New beliefs for the same `(subject, belief_type, directory_context)` close prior currently-valid rows before inserting. Pass `supersede=False` for competing co-existing beliefs.
- **`_VALID_EDGE_TABLES` extended**: `invalidate_edge()` now accepts `user_profile` and `derived_belief` without raising `ValueError`.
- **`as_of_filter(table, as_of)` helper added** (`yadgar/storage/bitemporal.py`): Returns a SQL WHERE-fragment selecting rows valid at a given ISO-8601 timestamp. `as_of=None` = current state. Wired into `get_all_causal_edges(as_of=)` and `get_full_graph(as_of=)`.
- **Filtered read helpers**: `search_profiles_fts`, `get_profiles_for_entity`, `search_beliefs_fts`, `get_beliefs_for_subject` gain `include_invalidated: bool = False` parameter — default excludes superseded rows.
- **SurrealDB partial-index capability verified**: `DEFINE INDEX ... WHERE` is NOT supported in v3.0.5. Application-side uniqueness used instead (documented in migration 010 and T5 tests).
- 22 new tests in `yadgar/tests/test_bitemporal_extension.py` (T1–T6, green). Pre-existing `test_bitemporal_edges.py` unchanged.

See [MIGRATION_NOTES.md §v5.29.0](MIGRATION_NOTES.md#v5290--bi-temporal-edges-extension-adopt-3-2026-06-01).

## [5.27.0] — 2026-06-01

DuckDB analytics export — behavioral observability add-on (Adopt-6).

- `yadgar export duckdb --output FILE` — dumps 19 SurrealDB tables to a local `.duckdb` file with typed schema (FLOAT[dim] embeddings, TIMESTAMP fields, JSON tag columns) and an `extra_fields JSON` catch-all for schema drift.
- 10 pre-built analytics views ship inside the file: `v_decay_distribution`, `v_recall_efficacy_by_tag`, `v_anchor_usage`, `v_high_heat_memories`, `v_domain_clustering`, `v_consolidation_effect`, `v_conflict_density`, `v_wiki_coverage`, `v_tool_call_volume`, `v_branch_distribution`. Each view has a `COMMENT ON VIEW` describing the behavioral question it answers.
- Optional dependency `analytics = ["duckdb>=0.10,<2"]`. Install with `pip install yadgar[analytics]`. CLI exits 2 with install hint if duckdb missing.
- Flags: `--include-secrets` (forward-compat no-op — v5.10.2 gate is write-time), `--action-log-since 30d`, `--action-log-limit 100000`, `--no-views`, `--tables`, `--force`.
- `*.duckdb` added to `.gitignore`.
- Adopt-6 from 2026-05-30 competitor audit: IMPLEMENTED.

Not a backup — analytics-only, lossy snapshot. Re-run to get fresh data. See MIGRATION_NOTES.md §v5.27.0.

## [5.25.1] — 2026-05-31

**Fixed:** benchmark now spawns `surreal start` subprocess for FULLTEXT-capable Phase 1 retrieval.

- Root cause: embedded `surrealkv://` lacks `FULLTEXT ANALYZER` support; v5.25.0 benchmark ran but produced all-zero retrieval metrics.
- `yadgar/_surreal_runner.py` (new): shared spawn/teardown/port helpers extracted from test-only module so benchmark can reuse them.
- `yadgar/tests/_surreal_helpers.py`: re-export shim — existing test imports unchanged.
- `benchmarks/run_longmemeval.py`: `spawn_surreal_for_benchmark()` spawns server on random port; `wipe_benchmark_tables()` issues `DELETE` on all data tables between questions (per-question isolation in server mode); `YADGAR_DB_URL` override skips spawn entirely. Sets `YADGAR_SECRET_GATE_DISABLED=1` during the run (corpus contains code / API-shaped strings that false-positive the storage gate) and restores prior value on exit. Per-question try/except keeps the run going if a single question fails.
- 4 new tests in `test_benchmark_phase1.py` (import, shim re-export, override path, wipe callable).
- **Phase 1 numbers deferred.** Benchmark wall-clock exceeded 2+ hours without completion; blocked deploy. Numbers land in v5.25.2 or v5.26.0 execution. `docs/BENCHMARK_RESULTS.md` remains PENDING.

Infrastructure ready. Phase 1 numbers deferred to v5.25.2 / v5.26.0 execution.

See [MIGRATION_NOTES.md §v5.25.1](MIGRATION_NOTES.md#v5251--benchmark-phase-1-spawn-surreal-server-subprocess-2026-05-31).

## [5.25.0] — 2026-05-31

Benchmark Phase 1 retrieval infra + reproducibility metadata.

- `benchmarks/run_longmemeval.py`: LongMemEval `s` variant download + sha256 pin (`LONGMEMEVAL_S_SHA256`), `build_reproducibility_dict()`, `--retrieval-only` flag, per-type + overall aggregation, JSON output with `reproducibility` key.
- `docs/BENCHMARK_RESULTS.md` v0 draft (PENDING placeholders — filled in v5.25.1).
- `docs/BENCHMARK_LICENSE.md`: LongMemEval (MIT) + LoCoMo (CC BY-NC) attribution.
- 12 tests in `yadgar/tests/test_benchmark_phase1.py`.

Note: Phase 1 retrieval run required live SurrealDB (embedded surrealkv lacks FULLTEXT). Fixed in v5.25.1.

See [MIGRATION_NOTES.md §v5.25.0](MIGRATION_NOTES.md#v5250--benchmark-phase-1-retrieval-infra--reproducibility-metadata-2026-05-31).

## [5.15.0] — 2026-05-30

CPU burst detection infrastructure (D1+D4) + secret-gate caller tag plumbing.

**Part A — CPU burst detection:**

- **D1: Phase duration CRITICAL alerting** (`yadgar/consolidation/orchestrator.py`): new `_warn_slow_phase(phase, duration_ms)` helper emits `CRITICAL` log (`SLOW_PHASE phase=X duration_ms=N threshold_ms=M`) when any `_consolidation_cycle()` phase exceeds the threshold. Covers 7 phases: `apply_decay`, `process_episodes`, `merge_duplicates`, `link_similar`, `detect_causality`, `memify`, `cls_consolidation`, `action_log`. Bursts immediately visible in `journalctl`.
- **New config `PHASE_DURATION_WARN_MS`**: default `60000` ms (1 min). Override via `YADGAR_PHASE_DURATION_WARN_MS` env var or `phase_duration_warn_ms` in config.yaml. Set to `0` to disable.
- **I25 three-way sync**: new `cpu_burst_detection` section in `config_yaml.py` + `config_registry.py`.
- **D4: Static caller audit tests** (`yadgar/tests/test_cpu_burst_detection.py`): grep-based tests assert no new callers of `run_sleep_cycle` / `force_consolidate` outside the known-good set. Fail on regression.

**Part B — secret-gate caller tag plumbing:**

- **`memorize.py`**: `check_secrets(content)` → `gate_or_reject(content, tags=list(tags))`. Allowlist now fires on real `memorize()` calls.
- **`wiki.py::wiki_add`**: same migration to `gate_or_reject`.
- **`misc.py::anchor`**: added `tags=["_anchor"]` to existing `gate_or_reject` call.
- **5 new tests** (`test_secret_gate_plumbing.py`): per-callsite regression + e2e allowlist acceptance (`ghp_FAKE + test-fixture tag → stored`).
- **Regression fix** (`test_memorize_reinject_gate.py`): updated mock target after import rename.

D2/D3 (backend `/health/inference` + uptime metric): deferred, require yadgar-backend release.

See [MIGRATION_NOTES.md §v5.15.0](MIGRATION_NOTES.md#v5150--cpu-burst-detection--secret-gate-plumbing-2026-05-30).

## [5.13.1] — 2026-05-30

Integration test backend image pin fix — drift since v5.0.3 era.

- **`yadgar/tests/integration/conftest.py`**: replaced hardcoded `openfantasy/yadgar-backend:5.0.3` with `_backend_image()` function that reads `backend_version` from `server.json` at fixture-import time. Integration tests now spin up `5.4.0` (current production). Future backend version bumps apply automatically.
- **`_SERVER_JSON` constant** + **`_backend_image()` function** added to conftest; skips cleanly if `server.json` unreadable.
- **3 new regression tests** (`yadgar/tests/integration/test_conftest_backend_pin.py`): version-match assertion, skip-on-missing guard, and no-hardcoded-5.0.3 gate.

See [MIGRATION_NOTES.md §v5.13.1](MIGRATION_NOTES.md#v5131--integration-test-backend-version-pin-fix-2026-05-30).

## [5.13.0] — 2026-05-30

Secret-gate context-awareness + allowlist — false-positive reduction for test fixtures, plan docs, and changelog entries.

- **New module `yadgar/security/allowlist.py`**: `AllowlistEntry` dataclass, `is_allowlisted(content, tags, source)`, `_reload_allowlist()`, `_write_audit()`. Tag-based + pattern-based bypass (full-bypass only; pattern override deferred).
- **`gate_or_reject()` extended** (`yadgar/secrets.py`): new `tags=` and `source=` kwargs. Calls `is_allowlisted()` BEFORE pattern scan. Allowlist hit → audit log + return clean. No allowlist file → identical to v5.10.x default-deny.
- **Allowlist config**: `YADGAR_SECRET_GATE_ALLOWLIST_PATH` (default `~/.yadgar/secret-gate-allowlist.yaml`) + `YADGAR_SECRET_GATE_AUDIT_DIR` (default `~/.yadgar/secret-gate-audit/`). Date-based JSONL rotation.
- **Audit trail**: every allowlist hit appended to `<audit-dir>/YYYY-MM-DD.jsonl` with fields: `ts`, `matched_pattern`, `tags`, `reason`, `source`, `content_preview` (≤80 chars).
- **Source detection**: `inspect.stack()` heuristic — `/tests/` → `"test:<file>"`, `/server/tools/` → `"tool:<name>"`, `/curation/` → `"doc-ingest"`, else `"unknown"`.
- **Allowlist YAML schema** (v1): `allowlist: [{tags: [...], patterns: [...], reason: "..."}]`. Tag match = entry tags ⊆ call-site tags. Pattern match = prefix-contains (`ghp_*` → `"ghp_" in content`).
- **New invariant `scripts/check_allowlist_audit.py`** (I28): static check that `_write_audit` and `is_allowlisted` co-present in `allowlist.py`, and that `gate_or_reject()` calls both. Pre-commit wired on `yadgar/security/allowlist.py` and `yadgar/secrets.py` changes.
- **Test fixture** `yadgar/tests/fixtures/secret-gate-allowlist.yaml`: canonical allowlist for all v5.10.2 false-positive cases.
- **11 new tests** (`test_allowlist.py`): per-tag bypass, audit log fields, default-deny, YAML invalid fails loud, source detection.
- I26 (`check_secret_gate.py`) still passes. Backward-compatible: callers without `tags=` get default-deny.

See [MIGRATION_NOTES.md §v5.13.0](MIGRATION_NOTES.md#v5130--secret-gate-context-awareness--allowlist-2026-05-30).

## [5.11.0] — 2026-05-30

Viz knobs configurable via config.yaml — all hardcoded viz constants replaced with config-driven values.

- **35 new `VIZ_*` Settings fields** (`yadgar/config.py`): node sizing, heat HSL params, 8 wiki category colors, 5 edge colors, edge width 3D multiplier, arrow length, physics charge/link-distance (2D+3D), layout zoom-fit params (tick threshold, padding, transition ms), search match/pinned stroke colors + dim opacity. All default to v5.10.11 hardcoded values — zero behavioral change on no-config deploys.
- **I25 three-way sync** (`config_yaml.py` + `config_registry.py`): 35 FIELD_META entries in new `viz_config` section + 35 `ConfigEntry` rows. All hooks pass.
- **`/api/viz/config` endpoint** (`yadgar/server/http.py`): `GET /api/viz/config` returns nested JSON (`node`/`edge`/`physics`/`layout`/`search`). Bearer-auth auto-applied by existing middleware. `@trace_span("api.viz_config")`.
- **Frontend wiring** (`yadgar/static/index.html`): `YADGAR_VIZ_CONFIG` global with hardcoded fallbacks; `loadVizConfig()` async function fetches `/api/viz/config` and deep-merges (silent fallback on error); `loadGraph()` calls `await loadVizConfig()` before init; all viz call sites replaced with `YADGAR_VIZ_CONFIG.*` references.
- **7 new tests** + **3 updated** (see `test_viz_config_endpoint.py`, `test_viz_static_assets.py::TestV5110VizConfigFetch`).
- **Complexity baseline** (`.complexity-baseline.json`): LOC baselines updated for `config_yaml.py` (1025→1152) and `server/http.py` (1440→1507) — growth is schema data.

See [MIGRATION_NOTES.md §v5.11.0](MIGRATION_NOTES.md#v5110--viz-knobs-configurable-via-configyaml-2026-05-30) + `docs/PLAN_V5_11_0_VIZ_CONFIG_YAML.md`.

## [5.10.11] — 2026-05-30

Viz polish (3D-only): edge thickness +50% + connected-node repulsion +20%.

- **3D edge thickness +50%** (`yadgar/static/index.html` line 863): 3D init block `.linkWidth` changed from plain `_linkWidth` to `l => _linkWidth(l) * 1.5`. 2D init block unchanged.
- **3D link distance 30 → 36** (`yadgar/static/index.html` after 3D init chain): added `graph.d3Force('link').distance(36)` in 3D branch only. 2D branch retains `distance(30)` in its else block. (Plan assumed shared post-init block; actual code is per-branch — 3D had no prior `distance()` call, so we added one directly.)
- **3 new static-asset regression tests** (`test_viz_static_assets.py::TestV51011VizEdgeThicknessAndRepulsion`): `test_3d_linkWidth_multiplier_present`, `test_2d_linkWidth_unchanged`, `test_3d_link_distance_36`.
- **Coloring logic untouched** — `_nodeColorFor`, `_linkColor`, `heatColor`, `WIKI_CAT_COLOR` unchanged per user instruction. 2D edge width untouched.

See [MIGRATION_NOTES.md §v5.10.11](MIGRATION_NOTES.md#v51011--viz-polish-3d-only-edge-thickness-50--repulsion-20-2026-05-30) + `docs/PLAN_V5_10_11_VIZ_EDGE_THICKNESS_AND_REPULSION.md`.

## [5.10.10] — 2026-05-30

Viz polish: 2x 3D node size + auto-zoom-fit on initial load (both 2D and 3D modes).

- **3D node size 2x** (`yadgar/static/index.html`): added `.nodeRelSize(8)` to 3D init chain. ForceGraph3D default is 4 — doubled radius makes nodes visibly larger on load without affecting layout coordinates.
- **Auto-zoom-fit on initial load** (`yadgar/static/index.html`): added `_zoomFitDone` module-level flag; extended `onEngineTick` callback in BOTH 2D and 3D init blocks to call `graph.zoomToFit(800, 50)` exactly once after tick 80 (layout well-settled, 30 ticks past the v5.10.8 pin threshold). Flag resets in `initGraph` (2D↔3D toggle re-fits) and `loadGraph` (reload button re-fits).
- **3 new static-asset regression tests** (`test_viz_static_assets.py::TestV51010VizPolish`): `test_nodeRelSize_set_to_8_in_3d_init`, `test_zoomFitDone_flag_declared`, `test_onEngineTick_calls_zoomToFit_at_threshold`.
- **Coloring logic untouched** — `_nodeColorFor`, `_linkColor`, `heatColor`, `WIKI_CAT_COLOR` unchanged per user instruction.

See [MIGRATION_NOTES.md §v5.10.10](MIGRATION_NOTES.md#v51010--viz-polish-2x-3d-node-size--auto-zoom-fit-2026-05-30) + `docs/PLAN_V5_10_10_VIZ_NODE_SIZE_AND_ZOOM_FIT.md`.

## [5.10.9] — 2026-05-30

Fix viz crash: filter orphan edges before passing to force-graph library (real root cause of all v5.10.7+ viz failures).

- **Root cause identified** (`force-graph.min.js`): library throws `Uncaught Error: node not found: entity:NNN` synchronously during `f.links` resolution when any link references an ID absent from the node set. One orphan edge crashes the entire physics simulation — no ticks run, all nodes clump at `(0,0,0)`. All v5.10.7–v5.10.8 symptom-chasing (mesh material, transparent flag, tick-count guard, mesh-leak removal) addressed downstream effects of this single crash.
- **Backend fix** (`yadgar/graph_api.py`): after assembling `nodes` + `edges`, filter edges to only those whose `source` AND `target` are in `{n["id"] for n in nodes}`. All `entity:*` causal edges are orphan-filtered because entity nodes are not included in the graph response (post-v5.0.0 monolith split). Logs count at INFO level. Increments `yadgar_graph_api_orphan_edges_dropped_total` counter.
- **New metric** (`yadgar/metrics.py`): `yadgar_graph_api_orphan_edges_dropped_total` Counter — tracks payload drift; non-zero after deploy confirms the fix fired on real data.
- **Frontend defensive filter** (`yadgar/static/index.html` `loadGraph()`): before `graph.graphData(...)`, builds `nodeIdSet` and filters `allLinks` to remove any edges whose endpoints are absent. `console.warn` logs count if any dropped — belt-and-suspenders for future backend drift.
- **5 new tests**: `test_graph_api_filters_orphan_edges`, `test_graph_api_orphan_drop_metric`, `test_graph_api_no_drops_in_healthy_payload` (backend); `test_loadGraph_filters_orphan_links`, `test_loadGraph_logs_dropped_count` (frontend static-asset).

See [MIGRATION_NOTES.md §v5.10.9](MIGRATION_NOTES.md#v5109--viz-orphan-edge-filter-2026-05-30) + `docs/PLAN_V5_10_9_VIZ_ORPHAN_EDGE_FILTER.md`.

## [5.10.8] — 2026-05-30

Fix 3D/2D viz physics hang (nodes clumped at origin) + Three.js mesh leak on filter cycles.

- **Bug A fixed** (`yadgar/static/index.html`): `onEngineStop` auto-pin guard — added `_engineTickCount` module-scope counter, incremented via `.onEngineTick()`. `onEngineStop` now returns early if `_engineTickCount < 50`, preventing premature pinning of all nodes at `(0,0,0)` before physics ran. Counter resets in `initGraph` so 2D↔3D toggle restarts it.
- **Bug B fixed** (`yadgar/static/index.html`): dropped `graph.graphData({ nodes: [], links: [] })` + `setTimeout(() => graph.graphData(d), 50)` empty-then-restore hack in `resetLayout`. ForceGraph3D does not dispose Three.js Mesh objects on the empty step — each call accumulated orphan meshes (700 nodes → 2297 meshes observed). Replaced with direct `graph.graphData(d)`.
- **3 new static-asset tests** (`test_viz_static_assets.py::TestV5108PhysicsAndMeshLeakFix`): `test_onEngineStop_has_tick_count_guard`, `test_onEngineTick_handler_present`, `test_no_empty_then_restore_pattern` (regression gate).

See [MIGRATION_NOTES.md §v5.10.8](MIGRATION_NOTES.md#v5108--viz-physics-hang--mesh-leak-fix-2026-05-30) + `docs/PLAN_V5_10_8_VIZ_PHYSICS_AND_MESH_LEAK_FIX.md`.

## [5.10.7.3] — 2026-05-30

Revert v5.10.7 custom 3D node geometry. Back to ForceGraph3D defaults.

- **Removed** `_makeNodeThreeObject` (custom THREE.Mesh factory for wiki/memory) from `yadgar/static/index.html`.
- **Removed** `.nodeThreeObject(_makeNodeThreeObject).nodeThreeObjectExtend(false)` from 3D graph init.
- **Removed** the 3D-mode `nodeThreeObject` re-call inside `_applySearchHighlight` (now only `.nodeColor()` re-fires).
- **Kept** `_nodeColorFor` + `.nodeColor(_nodeColorFor)` — applies heat-based colour to ForceGraph3D's default sphere material (may finally make 3D heat-coloring visible — bonus side-effect; was never working historically).
- **Why:** three attempts at custom 3D mesh (v5.10.7 Lambert; v5.10.7.1 Lambert→Basic; v5.10.7.2 conditional transparent) all rendered as fragmented triangle shards in user verification. Defaulting back to ForceGraph3D's library-managed solid spheres = last-known-good visual from v5.3.7.
- **Regression gates added** (`yadgar/tests/test_viz_static_assets.py::TestV510703RevertCustomMesh`): assert no `_makeNodeThreeObject` function, no `.nodeThreeObject(` call, no `new THREE.OctahedronGeometry`/`SphereGeometry` instantiation outside comments.
- **Removed** v5.10.7.1+v5.10.7.2 lighting/transparent tests (superseded by revert).
- **Trade-off:** S2.2 shape distinction (octahedra vs spheres) lost. User explicitly OK'd uniform shapes.

See [MIGRATION_NOTES.md §v5.10.7.3](MIGRATION_NOTES.md#v51073--revert-v5107-custom-3d-node-geometry-2026-05-30) + `docs/PLAN_V5_10_7_3_VIZ_REVERT_TO_DEFAULTS.md`.

## [5.10.7.2] — 2026-05-30

Hotfix: 3D viz wiki nodes still rendered as fragmented triangle shards after v5.10.7.1.

- **Root cause** (investigation 2026-05-30): `MeshBasicMaterial` with `transparent: true` + `opacity: 1.0` still places mesh in WebGL transparent render pass. Three.js sorts objects back-to-front in that pass but does NOT sort triangles within a single mesh. For an 8-faced `OctahedronGeometry`, back faces overdraw front faces → fragmented appearance. v5.10.7.1's Lambert→Basic swap was necessary but insufficient.
- **Fix** (`yadgar/static/index.html` line ~823): `transparent: true` → `transparent: !!node.__dimmed`. Mesh stays in opaque render pass when not dimmed → triangle ordering correct → solid octahedra (wiki) + solid spheres (memory) render properly. `opacity` value still controls dim-state alpha when `transparent` is true.
- **3D heat-coloring never worked** historically (PLAN_V5_10_7_VIZ_FIXES "soak-observed since 2026-05-20"); this fix restores SOLID-NODE rendering. Color treatment (whether heat gradient should be re-applied with proper material) is tracked as future work.

See [MIGRATION_NOTES.md §v5.10.7.2](MIGRATION_NOTES.md#v51072--3d-viz-transparent-flag-fix-2026-05-30).

## [5.10.7.1] — 2026-05-30

Bundled hotfix: sentinel filter + viz lighting fix.

- **Sentinel filter** (`yadgar/hooks/session-end-capture.py`): extended `SKIP_TAGS` frozenset to cover all slash-command output tags — `command-name`, `command-args`, `local-command-caveat`, `local-command-stdout`, `local-command-stderr` (in addition to existing `system-reminder`, `command-message`). Both `_count_human_messages` and `_parse_user_content` now reference the single module-level constant. Eliminates slash-command noise in `last_human_turns` sentinel field that was burying real human-turn context.
- **Viz lighting fix** (`yadgar/static/index.html`): `_makeNodeThreeObject` changed from `THREE.MeshLambertMaterial` → `THREE.MeshBasicMaterial`. ForceGraph3D adds no scene lights; Lambert rendered nodes as dark/fragmented triangle shards. Basic is unlit — colour always renders at set value. Wiki octahedra and memory spheres now render as solid coloured shapes.
- **8 new tests**: 6 sentinel-filter tests pinning per-tag skip behaviour + typo-turn survival; 2 viz tests asserting `MeshBasicMaterial` present + `MeshLambertMaterial` absent in `_makeNodeThreeObject` block.

See [MIGRATION_NOTES.md §v5.10.7.1](MIGRATION_NOTES.md#v51071--bundled-hotfix-sentinel-filter--viz-lighting-2026-05-30).

## [5.10.7] — 2026-05-30

Viz UX fixes S2.1–S2.4: heat colour in 3D, distinct node shapes, search mode fix, stats panel auto-refresh.

- **S2.1 — 3D heat colour**: `_nodeColorFor(node)` helper drives `.nodeColor()` in 3D init; heat gradient now visible in 3D (was uniform library default). `heatColor()` formula unchanged from 2D.
- **S2.2 — Node shape distinction**: `_makeNodeThreeObject(node)` returns `OctahedronGeometry` for wiki (visibly faceted) and `SphereGeometry` for memory. Material colour encodes heat (S2.1+S2.2 unified). Wired via `.nodeThreeObject()`.
- **S2.3 — Search in 3D**: `_applySearchHighlight()` now branches on `_graphMode`. 3D path re-fires `.nodeColor()` + `.nodeThreeObject()`. Old path called `nodeCanvasObject` (2D-only) causing `TypeError` in 3D.
- **S2.4 — Stats auto-refresh**: `openStats()` starts a 5 s `setInterval(refreshStats)`. `closeStats()` clears it. CPU/DB sparklines now animate while panel is open.
- **10 new static-asset tests** in `test_viz_static_assets.py`.

See [MIGRATION_NOTES.md §v5.10.7](MIGRATION_NOTES.md#v5107--viz-ux-fixes-2026-05-30).

## [5.10.6] — 2026-05-30

SESSION_END_CAPTURE sentinel-marker pattern + SessionStart extraction.

- **SessionEnd hook** (`yadgar/hooks/session-end-capture.py`): writes `~/.yadgar/session-ends/<session_id>.json` atomically on true exit (logout/other). Skips on `end_reason=clear/resume` and short sessions (`<SESSION_END_MIN_MESSAGES`). Embeds last N human turns + last 3 touched file paths for rotation resilience.
- **SessionStart import**: `hook_session_context` scans `~/.yadgar/session-ends/*.json`, imports each into memory with `_session_end_sentinel` tag, deletes on success. Retry semantics: `retries` counter incremented on failure; moved to `failed/` after 3 failures.
- **`_project_brief_signals` extension**: sentinel memory row → `extract_last_session_findings` recommended_action with `transcript_path`, `sentinel_id`, `last_human_turns`. Missing transcript → tombstone note + `forget(sentinel_id)` suggested_call.
- **Vacuum prune**: `_vacuum_stale_sentinels()` deletes `_session_end_sentinel` rows older than `SESSION_END_RETENTION_DAYS` (default 30).
- **4 new I25 env knobs**: `SESSION_END_CAPTURE_ENABLED=true`, `SESSION_END_RETENTION_DAYS=30`, `SESSION_END_SNIPPET_TURNS=5`, `SESSION_END_MIN_MESSAGES=2`.
- **`install_hooks` updated**: adds `SessionEnd` entry to `settings.json` (re-run required).
- **26 new tests** in `test_session_end_capture.py`.

See [MIGRATION_NOTES.md §v5.10.6](MIGRATION_NOTES.md#v5106--session-end-capture-sentinel-marker-pattern-2026-05-30).

## [5.10.5] — 2026-05-30

Patch: nightly cycle remaining bugs — vacuum URL second call site + prune deletes just-created snapshot.

- **Bug 1 — vacuum URL second call site**: `nightly_cycle.main()` and `cmd_vacuum_impl()` both had `getattr(args, "backend_url", "http://127.0.0.1:8080")` literals that bypassed `YADGAR_DB_URL` env when systemd invokes without `--backend-url`. Fixed both to `getattr(args, "backend_url", None) or os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080")`. Eliminates `[vacuum] ERROR: backend at http://127.0.0.1:8080 is not reachable: HTTP 307`.
- **Bug 2 — prune deletes just-created snapshot**: `shutil.copytree` with `copy2` propagates the source DB directory's mtime to the new snapshot directory. If the DB dir is old (stopped core, no writes for hours), the snapshot sorts as "oldest" by mtime and gets pruned in the same cycle. Fixed `create_snapshot()` to call `target.touch()` after copytree, stamping the snapshot to current time.
- **7 new tests**: `test_vacuum_url.py` (3 — structural + env-read correctness for both call sites) + `test_backup.py::TestPruneDoesNotDeleteJustCreated` (3 — mtime stamp, round-trip cycle) + structural source-scan (1).

See [MIGRATION_NOTES.md §v5.10.5](MIGRATION_NOTES.md#v5105--nightly-cycle-remaining-bugs-2026-05-30).

## [5.10.4] — 2026-05-30

Hotfix: `consolidate_now` heavyweight fix + PreToolUse hook schema fix.

- **`consolidate_now(mode='light'|'full')`**: new `mode` param (default `'light'`). Light = `force_consolidate()` only, typically <30 s. Full = consolidation + sleep cycle + anchor audit; sets `_last_sleep_cycle` timestamp so 6-hour gate fires correctly. Fixes 13-minute surprise on every on-demand flush.
- **Hook schema fix**: `hook_runner.py:db-lockdown-check` now emits `{"hookSpecificOutput": {"permissionDecision": "allow"|"deny"}}` (new PreToolUse schema). Eliminates `(root): Invalid input` noise on Bash tool calls.
- **I13 compliance fix**: extracted 4 helper functions from `memory_stats()` to resolve pre-existing HARD complexity violations (cyclo=32, fn_loc=155, nesting=5). No behavior change.
- **Behavior change**: `consolidate_now()` (default/no args) no longer runs the sleep cycle or anchor audit. Callers requiring the full cycle must pass `mode='full'`.
- **License correction**: `pyproject.toml` `license` field MIT → Apache-2.0 (matches `LICENSE` file). Resolves YELLOW finding #3 in `docs/LICENSE_COMPLIANCE_AUDIT_2026-05-30.md`.
- **Verified live**: deployed via local merge + amd64 build + nix bump per `yadgar-dev-workflow-single-isolated-change-release-cycle` anchor. `/health` reports `version=5.10.4` post-restart.

See [MIGRATION_NOTES.md §v5.10.4](MIGRATION_NOTES.md#v5104--consolidate_now-mode-parameter-2026-05-30).

## [5.10.3] — 2026-05-29

Patch: `scripts/scan_db_for_secrets.py` end-to-end fix.

- **OTLP hang fix**: `os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")` at script top — suppresses `BatchSpanProcessor` that hung at exit (~10 s backoff) pushing HITS/Clean output past `| tail -10`.
- **ORDER BY id DESC**: memory + wiki queries now scan newest rows first; `--limit 200` catches memory 519107 (ghp_ 33-char leak at DB position 2994/3147).
- **`--storage-mock-leak`**: new flag — mock data with known secret, exercises exit-1 path without live DB.
- **9 new tests** in `yadgar/tests/test_scan_script.py` via subprocess; 2 live-DB tests gated on `YADGAR_TEST_LIVE_SCAN=1`.
- **v5.10.3 bump**: pyproject.toml, server.json, docker-compose.yml, uv.lock.

See [MIGRATION_NOTES.md §v5.10.3](MIGRATION_NOTES.md#v5103--scan_db_for_secretspy-end-to-end-fix-2026-05-29).

## [5.10.2] — 2026-05-29

Unified security + parity + nightly-cycle hotfix.

- **Secret-gate architecture (I26)**: dual-layer protection — Layer 2 `gate_or_reject()` on all write tool API boundaries; Layer 1 `SecretLeakBlocked` exception in `insert_memory()` as last-resort defence. `YADGAR_SECRET_GATE_DISABLED` kill switch with loud warning.
- **Pattern strictness**: GitHub PAT `{36,}→{20,}`, Anthropic key `{32,}→{20,}`, OpenAI key `{30,}→{20,}`. Tighter thresholds reduce false-negative window.
- **I26 invariant lint**: `scripts/check_secret_gate.py` — AST-walks all `@_tool()` write tools; fails if any lacks `gate_or_reject()`. Pre-commit hook added.
- **Backfill scan**: `scripts/scan_db_for_secrets.py` — read-only scan of all memory + wiki rows; `--storage-mock` for CI; report to `~/.yadgar/`.
- **DLQ handling**: `_classify_error()` treats `SecretLeakBlocked` as permanent → moves to DLQ after 3 attempts, no infinite retry.
- **memorize() anchor parity** (v5.10.x): `is_protected=True` now auto-sets `tier="conditional"`, injects `_anchor` tag, adds `anchor:{reason}` tag. `reason` kwarg added. `semantic_immortal` without reason rejected when `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=True`.
- **surrealdb dep fix**: promoted `surrealdb>=1.0.0` from `[dev]` to `[project.dependencies]` — `ImportError` on clean installs eliminated.
- **vacuum `:8080` literal fix**: `_log_consolidation_row` now uses `YADGAR_DB_URL` env var with `:8080` as fallback only.

See [MIGRATION_NOTES.md §v5.10.2](MIGRATION_NOTES.md#v5102--secret-gate-architecture--memorize-parity--nightly-cycle-hotfix-2026-05-29).

## [5.10.1] — 2026-05-29

`_active_work` soft warning tier + optional watchdog timer.

- `_build_recommended_actions`: new soft actions `consider_refresh_active_work` + `consider_refresh_checkpoint` when `WARN_HOURS < age ≤ STALE_HOURS`. Mutual exclusion with hard actions per row.
- `suggested_call` enrichment on soft + hard refresh actions (continues v5.9 pattern).
- `update_active_work()`: writes `~/.yadgar/active-work-tracked/<sha256[:12]>/directory.txt` registry marker.
- 3 new env knobs three-way registered: `ACTIVE_WORK_WARN_HOURS`, `CHECKPOINT_WARN_HOURS`, `AUTO_REFRESH_ACTIVE_WORK`.
- New systemd-user units: `yadgar-active-work-watchdog.{timer,service}` — user-managed, NOT enabled by default.

See [MIGRATION_NOTES.md §v5.10.1](MIGRATION_NOTES.md#v5101--_active_work-soft-warning-tier--watchdog-timer-2026-05-29).

## [backend-5.4.0] — 2026-05-29

Backend hot-path caching: CE score LRU cache + embedding vector LRU cache.

- `yadgar/cache.py` — new `LRUCache` class: `OrderedDict` LRU + msgpack snapshot with `YADCACHE\0` magic header + checkpoint-hash validation.
- CE score cache in `/rerank?mode=ce`: partial-hit path splits texts into cached vs. miss batches; only misses go to ML; results merged + back-filled.
- Embedding vector cache in `/embed`: per-text SHA256 key; hit avoids re-encode.
- Lifespan: restore both caches from snapshot before first request; `_run_cache_snapshot_task` asyncio background task; final snapshot on shutdown.
- 10 new I23-compliant Prometheus metrics: hits/misses/evictions/size_entries/size_bytes per cache + `cache_snapshot_age_seconds{cache}` gauge.
- 6 new env knobs three-way registered: `CE_CACHE_ENABLED`, `EMBED_CACHE_ENABLED`, `CE_CACHE_MAX_ENTRIES`, `EMBED_CACHE_MAX_ENTRIES`, `CACHE_SNAPSHOT_INTERVAL_SEC`, `CACHE_SNAPSHOT_DIR`.
- `msgpack>=1.0` added to `pyproject.toml`.
- Kill switch: `YADGAR_CE_CACHE_ENABLED=false` → pre-v5.4.0 code path.

See [MIGRATION_NOTES.md §backend-v5.4.0](MIGRATION_NOTES.md#backend-v540--recall-hot-path-caching-ce-score-cache--embedding-vector-cache-2026-05-29).

## [5.10.0] — 2026-05-29

Test harness hardening: orphan reap + port determinism + session isolation.

- Add `pytest-timeout` (300s default, thread method) to gate hung tests.
- Centralize SurrealDB subprocess spawn in `yadgar/tests/_surreal_helpers.py` with `atexit` registration → orphan workers reaped on pytest exit (clean or signal-killed).
- Deterministic xdist port assignment via `YADGAR_TEST_PORT_BASE` (default 12000) + retry-on-EADDRINUSE.
- `pytest_sessionfinish` conftest hook for last-chance cleanup.
- `YADGAR_TEST_NAMESPACE` env var for multi-agent tmp dir isolation.
- Optional watchdog systemd-user units at `scripts/systemd-user/` (user-installed).
- Closes recurring CPU-fan / orphan-SurrealDB / false-regression root cause investigation 2026-05-28.

See [MIGRATION_NOTES.md §v5.10.0](MIGRATION_NOTES.md#v5100--test-harness-hardening-orphan-reap--port-determinism--session-isolation-2026-05-29).

## [5.9.0] — 2026-05-28

Anchor audit: `audit_anchors()` MCP tool + `consolidate_now()` anchor pass.

- New tool `audit_anchors(directory, dry_run=True, cosine_threshold=None, include_global=False)` — surfaces forget_expired/merge/promote actions, safe-mutation-only when `dry_run=False`, NEVER auto-`wiki_add`.
- Extended `consolidate_now()` with per-directory anchor audit pass (gated by `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`).
- `_audit_anchors` sentinel memory per directory (latest-wins, matches `_active_work` pattern).
- `recommended_actions.audit_anchors` now carries `suggested_call` field (copy-paste-able).
- 3 new I25-registered env knobs: `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`, `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN=20`, `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS=30`.
- `tier=semantic_immortal` + `is_protected=True` legacy rows NEVER auto-mutated.
- Idempotent: second call on unchanged state returns empty `applied` list.

See [MIGRATION_NOTES.md §v5.9.0](MIGRATION_NOTES.md#v590--anchor-audit-audit_anchors-tool--consolidate_now-anchor-pass-2026-05-28).

## [5.8.0] — 2026-05-28

Anchor hygiene foundation: `tier` enum + `valid_until` + 3 new signals + schema migration.

- New fields on `memorize()` and `anchor()`: `tier` (`semantic_immortal | conditional | ephemeral`), `valid_until` (datetime UTC), `ttl_days` (shorthand).
- Schema migration `migration_008` adds `tier`, `valid_until`, `migration_grace` columns to `memory` table (schemaless SurrealDB → no backend bump). Idempotent + sentinel-gated.
- 3 new `project_brief(mode="signals")` fields: `anchor_count_project`, `anchor_redundancy_candidates` (compact tuple-list encoding), `anchor_promote_candidates`. K=3 hard truncation to satisfy ≤100 token budget.
- 4 new `recommended_actions` action types: `audit_anchors`, `merge_redundant_anchors`, `promote_anchor_to_wiki`, `forget_expired_anchors`.
- 7 new I25-registered env knobs: `ANCHOR_CONDITIONAL_TTL_DAYS=90`, `ANCHOR_EPHEMERAL_TTL_DAYS=14`, `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true`, `ANCHOR_REDUNDANCY_COSINE=0.92`, `ANCHOR_PROMOTE_WORDS=500`, `ANCHOR_PROMOTE_HEADERS=2`, `ANCHOR_AUDIT_THRESHOLD=15`.
- Backwards-compat: existing `anchor(...)` calls without `tier` default to `conditional` with 90d expiry.

See [MIGRATION_NOTES.md §v5.8.0](MIGRATION_NOTES.md#v580--anchor-hygiene-foundation-tier--valid_until--signals-2026-05-28).

## [5.7.13] — 2026-05-28 (test-only, no version tag)

Test isolation + xdist fixture scope fixes + anchor hygiene plan trilogy drafted.

- 5 test fixes for env-var/config.yaml pollution (`_isolate_yaml_config` autouse fixture, `monkeypatch.setenv` over bare `os.environ` mutation, correct `_state` module path for `_db_size_warn_last_logged_hour`).
- Function-scope `_engines` fixture in `test_memory_behavior.py` to prevent cross-test storage state pollution under xdist.
- `@pytest.mark.skipif` on 500-memory merge timing test under `PYTEST_XDIST_WORKER` (unreliable under parallel CPU contention; serial pass ~38.5s).
- Plans drafted: `PLAN_V5_8_ANCHOR_HYGIENE.md`, `PLAN_V5_9_ANCHOR_AUDIT.md`, `PLAN_V5_11_ANCHOR_CROSS_PROJECT.md` (originally numbered v5.10).

No production code touched → no version bump. No deployable artifact.

## [5.7.12] — 2026-05-27

`project_brief` two-audience split: `signals` + `restore` modes.

- New modes: `signals` (≤100 tokens, stop-hook target), `restore` (≤800 tokens, post-/clear target). `catalog` marked deprecated, back-compat preserved.
- Age numerics: `stale_checkpoint_hours`, `active_work_age_hours`, `init_memory_age_hours` exposed as floats|null.
- Pre-computed `recommended_actions` list: `refresh_active_work`, `refresh_checkpoint`, `bootstrap_project` (threshold-driven).
- Bug fix: `hot_memories` now excludes anchored entries (`_anchor NOT IN tags`) in all modes.
- `top_anchors_global` + `top_anchors_project` merged into single `top_anchors` with `scope: "global" | "project" | "both"` per entry.
- Stop hook (`yadgar/hooks/stop-memory-checkpoint.py`) rewritten to iterate `recommended_actions` instead of text-comparing signal state.
- 3 new I25 env knobs: `ACTIVE_WORK_STALE_HOURS=24`, `CHECKPOINT_STALE_HOURS=24`, `PROJECT_BRIEF_MAX_ANCHORS=12`.

See [MIGRATION_NOTES.md §v5.7.12](MIGRATION_NOTES.md#v5712--project_brief-two-audience-split--signalsrestore-modes-2026-05-27).

## [5.7.11] + backend [5.3.1] — 2026-05-27

Yamlify 5 OTLP + DBSIZE env knobs; drop dead `LOG_LEVEL`.

## [5.7.10] — 2026-05-27

Container yaml load + I25 invariant (three-way `KEY reason=<category>` allowlist) + nix `-e` cleanup.

## [5.7.0] — 2026-05-26

Nightly cycle redesign: single 19:00 UTC heavy cycle (backup → consolidation → vacuum → backup) replaces daemon 30-min trigger.

## Earlier releases

See `git log --oneline --grep='chore(release)'` for the complete release history pre-v5.7.0. Migration notes for older versions live in this file's earlier sections.
