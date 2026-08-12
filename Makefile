# Yadgar top-level Makefile — v5.45.0 make-canonical setup
# GNU Make required. Run `make help` for target list.

# Guard: refuse non-GNU make immediately (pre-setup checks this too)
ifeq (,$(findstring GNU Make,$(shell $(MAKE) --version 2>&1 | head -1)))
$(error GNU Make is required. Install it with: nix-env -iA nixpkgs.gnumake  OR  apt install make  OR  brew install make)
endif

SHELL := /usr/bin/env bash -euo pipefail

# Paths
REPO_ROOT    := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
SCRIPTS_DIR  := $(REPO_ROOT)scripts/install
INSTALL_ASSETS_DIR := $(REPO_ROOT)install_assets
# v5.88: canonical seed materials live under the package tree (one dir to edit seeds).
ANCHORS_YAML := $(REPO_ROOT)yadgar/seed/materials/anchors.yaml
FRAGMENT     := $(INSTALL_ASSETS_DIR)/CLAUDE.md.fragment
CLAUDE_MD    := $(HOME)/.claude/CLAUDE.md

# User-facing defaults (override via env or command line)
INSTALL_NONINTERACTIVE ?= 0
YADGAR_CONTAINER_RUNTIME ?=
YADGAR_DIR   ?= $(HOME)/.yadgar
# OS spoofing for cross-platform testing (set YADGAR_TEST_OS_MARKER=macos to simulate macOS)
YADGAR_TEST_OS_MARKER ?=
# Test seams for install_runtime.sh
YADGAR_TEST_OS_RELEASE ?=
YADGAR_TEST_INSTALL_DRYRUN ?=
YADGAR_TEST_TTY ?=
# Enable systemd lingering so the user units survive logout and start at boot.
# Default matches yadgar-setup.sh (opt-out only) — divergent defaults between the
# two install surfaces is the bug class this train is cleaning up.
# Opt out with: make setup YADGAR_ENABLE_LINGER=0
YADGAR_ENABLE_LINGER ?= 1

# Provision code_graph: the codebase-memory-mcp host binary AND the
# code_graph.enabled runtime-config row, together. Default matches
# yadgar-setup.sh (opt-out only) — divergent defaults between the two install
# surfaces is the bug class this train is cleaning up.
# Opt out with: make setup YADGAR_CODE_GRAPH=0
YADGAR_CODE_GRAPH ?= 1
# Every plist generate_launchd.sh renders. Both macOS enable paths below iterate
# this ONE list. They previously carried an inline two-plist list while
# yadgar-setup.sh::_step_enable_units bootstrapped all six — so `make setup` on
# macOS rendered six maintenance jobs and loaded two, and `--doctor` linted all
# six without asserting any were loaded. Guarded by
# test_setup_sh_and_make_agree_on_activated_macos_plists.
MACOS_PLISTS := com.openfantasy.yadgar.plist \
                com.openfantasy.yadgar-backend.plist \
                com.openfantasy.yadgar-vacuum.plist \
                com.openfantasy.yadgar-nightly-cycle.plist \
                com.openfantasy.yadgar-vacuum-trigger.plist \
                com.openfantasy.yadgar-worktree-sweep.plist

# Resolved at parse time (not runtime) so `make -n` shows the real plan.
ifeq ($(YADGAR_ENABLE_LINGER),0)
LINGER_STEP := @echo "    Skipping systemd lingering (YADGAR_ENABLE_LINGER=0)"
else
LINGER_STEP := @bash $(SCRIPTS_DIR)/enable_linger.sh || true
endif

# NOTE the asymmetry with LINGER_STEP: the code_graph opt-out RUNS the
# subcommand with --no-code-graph rather than echo-skipping it. A skip would
# leave code_graph.enabled at its true default (ADR-0163: no row -> true) with
# no binary installed — the exact incoherence this step exists to remove, only
# inverted. Lingering has no such paired runtime flag, so a skip is correct there.
#
# `python3 -m yadgar`, NOT a bare `yadgar`: this Makefile serves the
# repo-checkout path (`git clone && cd yadgar && make setup`), where no console
# shim is on PATH — every sibling target (install-hooks, install-agents,
# config-sync, seed-anchors) uses the module form for that reason. With `|| true`
# swallowing the failure, a bare `yadgar` would turn a missing shim into a SILENT
# no-op, i.e. exactly the divergence this step exists to remove. The shell
# installer deliberately keeps the bare form: it ships inside the installed
# wheel, where the shim exists, and its steps 6-11 all invoke it that way.
ifeq ($(YADGAR_CODE_GRAPH),0)
CODE_GRAPH_STEP := @python3 -m yadgar code-graph install --no-code-graph || true
else
CODE_GRAPH_STEP := @python3 -m yadgar code-graph install || true
endif

# Version — read once from server.json at parse time
YADGAR_VERSION := $(shell grep -m1 '"version"' $(REPO_ROOT)server.json | cut -d'"' -f4)
# Backend image version — independent track; canonical source: yadgar/__init__.py BACKEND_VERSION
YADGAR_BACKEND_VERSION := $(shell grep -m1 '^BACKEND_VERSION' $(REPO_ROOT)yadgar/__init__.py | cut -d'"' -f2)

.PHONY: all help pre-setup setup uninstall uninstall-purge \
        install-hooks install-agents config-sync install-rules \
        seed-anchors code-graph-install detect-runtime detect-os install-runtime clean check \
        pull-images bootstrap-secrets enable-units enable-units-linux enable-units-macos \
        _enable-units-auto restore upgrade-test eval longmemeval perf ci-local

all: setup

## help: Show this help
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## //' | column -t -s ':'

## pre-setup: Preflight checks (GNU make guard, detect runtime/OS)
pre-setup:
	@echo "==> Preflight checks..."
	@$(MAKE) --version | grep -q "GNU Make" || { echo "ERROR: GNU Make required"; exit 1; }
	@echo "    GNU Make: OK"
	@if [ "$(YADGAR_TEST_SKIP_RUNTIME_CHECK)" = "1" ]; then \
	    echo "    Container runtime: SKIP (YADGAR_TEST_SKIP_RUNTIME_CHECK=1)"; \
	  elif ! YADGAR_TEST_OS_RELEASE="$(YADGAR_TEST_OS_RELEASE)" \
	        YADGAR_TEST_INSTALL_DRYRUN="$(YADGAR_TEST_INSTALL_DRYRUN)" \
	        YADGAR_TEST_TTY="$(YADGAR_TEST_TTY)" \
	        bash $(SCRIPTS_DIR)/detect_runtime.sh --quiet >/dev/null 2>&1; then \
	    if [ "$(INSTALL_NONINTERACTIVE)" = "1" ]; then \
	      YADGAR_TEST_OS_RELEASE="$(YADGAR_TEST_OS_RELEASE)" \
	        bash $(SCRIPTS_DIR)/detect_runtime.sh; exit 1; \
	    else \
	      $(MAKE) install-runtime \
	        INSTALL_NONINTERACTIVE=$(INSTALL_NONINTERACTIVE) \
	        YADGAR_TEST_OS_RELEASE="$(YADGAR_TEST_OS_RELEASE)" \
	        YADGAR_TEST_INSTALL_DRYRUN="$(YADGAR_TEST_INSTALL_DRYRUN)" \
	        YADGAR_TEST_TTY="$(YADGAR_TEST_TTY)"; \
	    fi; \
	  else \
	    RUNTIME=$$(YADGAR_TEST_OS_RELEASE="$(YADGAR_TEST_OS_RELEASE)" \
	                YADGAR_TEST_INSTALL_DRYRUN="$(YADGAR_TEST_INSTALL_DRYRUN)" \
	                YADGAR_TEST_TTY="$(YADGAR_TEST_TTY)" \
	                bash $(SCRIPTS_DIR)/detect_runtime.sh); \
	    echo "    Container runtime: $$RUNTIME"; \
	  fi
	@OS=$$(bash $(SCRIPTS_DIR)/detect_os.sh); \
	  echo "    Host OS: $$OS"; \
	  if [ "$$OS" = "linux-nixos" ]; then \
	    echo ""; \
	    echo "ERROR: NixOS detected. Use the nix flake install (v5.46+) instead of make setup."; \
	    echo "       See: https://github.com/m-agahi/yadgar#nixos-install"; \
	    exit 1; \
	  fi

## detect-runtime: Probe and print container runtime
detect-runtime:
	@bash $(SCRIPTS_DIR)/detect_runtime.sh

## detect-os: Probe and print host OS
detect-os:
	@bash $(SCRIPTS_DIR)/detect_os.sh

## install-runtime: Install a container runtime (podman) interactively or print hint in CI
install-runtime:
	@INSTALL_NONINTERACTIVE=$(INSTALL_NONINTERACTIVE) \
	  YADGAR_TEST_OS_RELEASE="$(YADGAR_TEST_OS_RELEASE)" \
	  YADGAR_TEST_INSTALL_DRYRUN="$(YADGAR_TEST_INSTALL_DRYRUN)" \
	  YADGAR_TEST_TTY="$(YADGAR_TEST_TTY)" \
	  bash $(SCRIPTS_DIR)/install_runtime.sh

## install-hooks: Install Claude Code git hooks (daemon-independent)
install-hooks:
	python3 -m yadgar install-hooks

## install-agents: Install yadgar subagents into Claude Code
install-agents:
	python3 -m yadgar install-subagents

## config-sync: Sync yadgar config from repo to ~/.yadgar/
config-sync:
	python3 -m yadgar config sync

## install-rules: Append yadgar rules fragment to ~/.claude/CLAUDE.md (idempotent)
install-rules:
	@YADGAR_CLAUDE_MD_TARGET="$(CLAUDE_MD)" \
	  YADGAR_FRAGMENT_PATH="$(FRAGMENT)" \
	  bash $(SCRIPTS_DIR)/append_claude_rules.sh

## seed-anchors: Seed canonical anchor memories from yadgar/seed/materials/anchors.yaml
seed-anchors:
	python3 -m yadgar seed --anchors $(ANCHORS_YAML)

## pull-images: Pull yadgar core + backend container images
pull-images:
	@RUNTIME=$$(bash $(SCRIPTS_DIR)/detect_runtime.sh); \
	  echo "==> Pulling images: core=v$(YADGAR_VERSION) backend=v$(YADGAR_BACKEND_VERSION) using $$RUNTIME..."; \
	  $$RUNTIME pull docker.io/openfantasy/yadgar:$(YADGAR_VERSION); \
	  $$RUNTIME pull docker.io/openfantasy/yadgar-backend:$(YADGAR_BACKEND_VERSION)

## bootstrap-secrets: Generate ~/.yadgar/secrets.env (interactive prompt for missing creds)
bootstrap-secrets:
	@INSTALL_NONINTERACTIVE=$(INSTALL_NONINTERACTIVE) \
	  bash $(SCRIPTS_DIR)/bootstrap_secrets.sh

## enable-units: systemctl daemon-reload + enable --now yadgar.target (Linux)
enable-units:
	$(LINGER_STEP)
	systemctl --user daemon-reload
	systemctl --user enable --now yadgar.target
	@echo "==> Verifying services..."
	@sleep 2
	@systemctl --user --no-pager status yadgar.service yadgar-backend.service | head -20 || true

## enable-units-linux: alias for enable-units (Linux systemd path)
enable-units-linux: enable-units

## enable-units-macos: launchctl bootstrap gui/$UID for macOS 11+; load -w fallback for 10.15
enable-units-macos:
	@echo "==> Loading launchd agents..."
	@LAUNCHD_DIR="$(HOME)/Library/LaunchAgents"; \
	  MACOS_MAJOR=$$(sw_vers -productVersion 2>/dev/null | cut -d. -f1 || echo "11"); \
	  for plist in $(MACOS_PLISTS:%=$${LAUNCHD_DIR}/%); do \
	    [ -f "$$plist" ] || { echo "ERROR: $$plist not found. Run 'make setup' first." >&2; exit 1; }; \
	    launchctl unload "$$plist" 2>/dev/null || true; \
	    if [ "$${MACOS_MAJOR}" -ge 11 ] 2>/dev/null; then \
	      launchctl bootstrap "gui/$$(id -u)" "$$plist"; \
	    else \
	      launchctl load -w "$$plist"; \
	    fi; \
	    echo "    Loaded: $$(basename $$plist)"; \
	  done
	@echo "==> Verifying launchd agents..."
	@launchctl list | grep com.openfantasy.yadgar || true

## _enable-units-auto: Internal — routes enable-units to systemd or launchd based on OS (used by setup)
_enable-units-auto:
	$(LINGER_STEP)
	@OS=$$(YADGAR_TEST_OS_MARKER="$(YADGAR_TEST_OS_MARKER)" bash $(SCRIPTS_DIR)/detect_os.sh); \
	  case "$$OS" in \
	    linux|linux-other) \
	      systemctl --user daemon-reload; \
	      systemctl --user enable --now yadgar.target; \
	      ;; \
	    macos) \
	      LAUNCHD_DIR="$(HOME)/Library/LaunchAgents"; \
	      MACOS_MAJOR=$$(sw_vers -productVersion 2>/dev/null | cut -d. -f1 || echo "11"); \
	      for plist in $(MACOS_PLISTS:%=$${LAUNCHD_DIR}/%); do \
	        [ -f "$$plist" ] || { echo "WARNING: $$plist not found — did generate_launchd.sh fail?" >&2; continue; }; \
	        launchctl unload "$$plist" 2>/dev/null || true; \
	        if [ "$${MACOS_MAJOR}" -ge 11 ] 2>/dev/null; then \
	          launchctl bootstrap "gui/$$(id -u)" "$$plist"; \
	        else \
	          launchctl load -w "$$plist"; \
	        fi; \
	      done; \
	      ;; \
	    linux-nixos) echo "NixOS: use nix flake." >&2; exit 1 ;; \
	    *) echo "Unsupported OS: $$OS" >&2; exit 1 ;; \
	  esac

## code-graph-install: Provision code_graph (codebase-memory-mcp binary + enabled flag); opt out with YADGAR_CODE_GRAPH=0
code-graph-install:
	@echo "==> Provisioning code_graph (codebase-memory-mcp)..."
	$(CODE_GRAPH_STEP)

## restore: Restore from .surql backup + archive (advanced; set YADGAR_RESTORE_DB=... env var)
restore:
	@bash $(SCRIPTS_DIR)/restore.sh

## setup: Full install (pre-setup → pull-images → bootstrap-secrets → units → enable-units → hooks → agents → config → rules → anchors)
setup: pre-setup
	@echo "==> Detecting container runtime..."
	@RUNTIME=$$(bash $(SCRIPTS_DIR)/detect_runtime.sh); \
	  echo "    Runtime: $$RUNTIME"
	@$(MAKE) pull-images
	@$(MAKE) bootstrap-secrets
# task:0110 Stage D: generate_systemd.sh renders nothing — it resolves a `yadgar`
# CLI and delegates. Left alone it would fall through to `command -v yadgar`, i.e.
# whatever is INSTALLED, and `make setup` from a checkout would stop rendering
# from the checkout. YADGAR_RENDERER_CLI + PYTHONPATH keep this target's units
# derived from the working tree, which is what a repo-local `make setup` means.
	@OS=$$(YADGAR_TEST_OS_MARKER="$(YADGAR_TEST_OS_MARKER)" bash $(SCRIPTS_DIR)/detect_os.sh); \
	  RUNTIME=$$(bash $(SCRIPTS_DIR)/detect_runtime.sh); \
	  case "$$OS" in \
	    linux|linux-other) \
	      YADGAR_RUNTIME=$$RUNTIME \
	      YADGAR_INSTALL_PREFIX="$(YADGAR_DIR)" \
	      YADGAR_SECRETS_ENV_FILE="$(YADGAR_DIR)/secrets.env" \
	      YADGAR_BACKEND_IMAGE="docker.io/openfantasy/yadgar-backend:$(YADGAR_BACKEND_VERSION)" \
	      YADGAR_CORE_IMAGE="docker.io/openfantasy/yadgar:$(YADGAR_VERSION)" \
	      YADGAR_SYSTEMD_OUTPUT_DIR="$(HOME)/.config/systemd/user" \
	      YADGAR_RENDERER_CLI="python3 -m yadgar" \
	      PYTHONPATH="$(CURDIR)" \
	      bash $(SCRIPTS_DIR)/generate_systemd.sh \
	      ;; \
	    macos) \
	      YADGAR_RUNTIME=$$RUNTIME \
	      YADGAR_INSTALL_PREFIX="$(YADGAR_DIR)" \
	      YADGAR_SECRETS_ENV_FILE="$(YADGAR_DIR)/secrets.env" \
	      YADGAR_BACKEND_IMAGE="docker.io/openfantasy/yadgar-backend:$(YADGAR_BACKEND_VERSION)" \
	      YADGAR_CORE_IMAGE="docker.io/openfantasy/yadgar:$(YADGAR_VERSION)" \
	      YADGAR_LAUNCHD_OUTPUT_DIR="$(HOME)/Library/LaunchAgents" \
	      bash $(SCRIPTS_DIR)/generate_launchd.sh \
	      ;; \
	    linux-nixos) echo "NixOS detected. Use nix flake." >&2; exit 1 ;; \
	    *) echo "Unsupported OS: $$OS" >&2; exit 1 ;; \
	  esac
	@$(MAKE) _enable-units-auto
	@$(MAKE) install-hooks
	@$(MAKE) install-agents
	@$(MAKE) config-sync
	@$(MAKE) install-rules
	@$(MAKE) seed-anchors
	@$(MAKE) code-graph-install
	@echo ""
	@echo "==> Yadgar setup complete!"

## uninstall: Remove daemon units; preserve ~/.yadgar/ data
uninstall:
	@YADGAR_DIR="$(YADGAR_DIR)" \
	  YADGAR_SYSTEMD_OUTPUT_DIR="$(HOME)/.config/systemd/user" \
	  bash $(SCRIPTS_DIR)/uninstall.sh

## uninstall-purge: Remove daemon units AND ~/.yadgar/ data directory
uninstall-purge:
	@YADGAR_DIR="$(YADGAR_DIR)" \
	  YADGAR_SYSTEMD_OUTPUT_DIR="$(HOME)/.config/systemd/user" \
	  bash $(SCRIPTS_DIR)/uninstall.sh --purge

## clean: Remove generated unit files from ~/.config/systemd/user/ (does NOT touch data)
clean:
	@rm -f $(HOME)/.config/systemd/user/yadgar.service \
	        $(HOME)/.config/systemd/user/yadgar-backend.service \
	        $(HOME)/.config/systemd/user/yadgar.target
	@echo "Cleaned generated systemd units."

## check: Run v5.45.x + v5.46.x tests
## Capped like every other pytest entry point (see scripts/test-capped.sh). The
## `--override-ini="addopts="` strip also drops pytest-timeout's session default,
## so `--timeout=300` is restored explicitly — `--noconftest` cannot take the
## `-n 4 --dist loadgroup` the full addopts carry, hence the strip stays.
check:
	@TEST_TIMEOUT=900 TEST_CPU_QUOTA=200% TEST_MEM_MAX=8G scripts/test-capped.sh \
	  python3 -m pytest yadgar/tests/scripts/test_v5_45_*.py yadgar/tests/scripts/test_v5_46_*.py \
	    --noconftest --override-ini="addopts=" --timeout=300 -q

## test-clean: Kill orphaned test SurrealDB procs left by crashed runs (never touches prod)
test-clean:
	@bash scripts/reap-test-surreal.sh
	@echo "Reaped stale test surreal procs (production daemon on /data untouched)."

# Shared mutual-exclusion lock for ALL local surreal-spawning test runs
# (test / test-ci / e2e). Two concurrent runs would each fire the orphan-reaper
# and kill the OTHER run's /tmp/pytest surreals → spurious errors. flock
# serializes them: a second run (pre-push hook OR a manual/agent trigger) waits
# up to 15min for the in-flight run, then fails fast rather than colliding.
# The reaper runs INSIDE the lock, so it can only ever touch true orphans.
# (CI is unaffected — it invokes pytest directly with --splits, not these targets.)
TEST_LOCK := $(HOME)/.cache/yadgar/test.lock
LOCKED = mkdir -p $(dir $(TEST_LOCK)) && flock -w 900 $(TEST_LOCK) bash -c

## test: Run the full suite CPU/mem-capped + timeout-bounded + orphan-reaped + lock-serialized
## (a hung run can't peg the box or crash the prod daemon; see scripts/test-capped.sh)
test:
	@$(LOCKED) 'bash scripts/reap-test-surreal.sh; trap "bash scripts/reap-test-surreal.sh" EXIT; \
	  scripts/test-capped.sh uv run --extra test pytest yadgar/tests/ -q $(PYTEST_ARGS)'

## test-ci: CI-visible selection (-m "not integration and not e2e"), locked + parallel.
## Use this for a local pre-merge preflight — mirrors what CI runs, serialized so it
## never collides with a concurrent e2e/pre-push run.
test-ci:
	@$(LOCKED) 'bash scripts/reap-test-surreal.sh; trap "bash scripts/reap-test-surreal.sh" EXIT; \
	  scripts/test-capped.sh uv run --extra test --extra ml python -m pytest yadgar/tests/ \
	    -m "not integration and not e2e" -p no:randomly -n auto -q $(PYTEST_ARGS)'

## ci-local: Reproduce CI's four subsystem test jobs (test-fast/test-shared/test-backend/test-core in
## .github/workflows/ci-pr.yml) — same dirs + marker CI actually gates a PR on. PR #40 shipped on a green
## `make e2e`, a target that runs a DISJOINT directory+marker from CI (yadgar/tests/e2e/ -m e2e) — CI then
## found 49 failures. This is the target that would have caught them. It is NOT `test-ci` (broader,
## unfiltered directory set incl. e2e/integration/restoration dirs, no perf exclusion) and NOT `e2e`.
## INTEGRATION-STEP TARGET, NOT A CAR TARGET: ADR-0218 forbids a car ever running the full unit suite —
## a car's obligation stays targeted tests over changed symbols; run this at the PR/push seam instead.
## Subset for mid-work use: `make ci-local DIRS=yadgar/tests/_shared`. Full run covers all 8 CI dirs.
## Anti-drift: scripts/check_ci_local_parity.py (pre-commit hook check-ci-local-parity) parses BOTH this
## Makefile and ci-pr.yml independently and fails if their dirs/marker ever disagree — the exact silent
## drift that produced PR #40's gap, closed by comparing live files instead of trusting a copied snapshot.
CI_LOCAL_DIRS := yadgar/tests/scripts/ yadgar/tests/server/ yadgar/tests/hooks/ yadgar/tests/_meta/ \
                 yadgar/tests/clients/ yadgar/tests/_shared/ yadgar/tests/backend/ yadgar/tests/core/
CI_LOCAL_MARKER := not integration and not e2e and not perf
DIRS ?= $(CI_LOCAL_DIRS)
ci-local:
	@$(LOCKED) 'bash scripts/reap-test-surreal.sh; trap "bash scripts/reap-test-surreal.sh" EXIT; \
	  scripts/test-capped.sh uv run --extra test --extra ml python -m pytest $(DIRS) \
	    -q -rs --tb=short -n 4 --dist loadgroup --reruns 2 --reruns-delay 2 \
	    -m "$(CI_LOCAL_MARKER)" $(PYTEST_ARGS)'

## e2e: Run the behavior-contract e2e safety-net suite against the local `surreal` binary.
## Requires: ~/.local/bin/surreal (or surreal on PATH). See yadgar/tests/e2e/conftest.py.
## Excluded from CI (-m 'not e2e') — CI's embedded SurrealDB can't run these reliably.
## Install the pre-push hook once with: pre-commit install --hook-type pre-push
## Capped like the other pytest entry points, with e2e-specific limits: serial
## (-n0) and measured ~7min, so a 30min ceiling is generous while still bounding
## the pre-push hook; 400% leaves room for the suite's SurrealDB children.
e2e:
	@$(LOCKED) 'bash scripts/reap-test-surreal.sh; trap "bash scripts/reap-test-surreal.sh" EXIT; \
	  OTEL_SDK_DISABLED=true PATH="$$HOME/.local/bin:$$PATH" \
	  TEST_TIMEOUT=1800 TEST_CPU_QUOTA=400% TEST_MEM_MAX=12G \
	  scripts/test-capped.sh uv run --extra test --extra ml python -m pytest yadgar/tests/e2e/ \
	    -m e2e -p no:randomly -n0 --reruns 2 --reruns-delay 2 --tb=short -q $(PYTEST_ARGS)'

## eval: Run the v6 Phase 0 eval harness (native golden set, recall@k/MRR/nDCG/latency).
## Requires: `surreal` on PATH (or YADGAR_DB_URL set to a running instance).
## NON-GATING: informational quality measurement, not a merge gate.
## Golden set is a bootstrap (auto-drafted) — see benchmarks/golden/golden_set.jsonl.
eval:
	@echo "==> Running v6 Phase 0 eval harness ..."
	@echo "    Golden set: benchmarks/golden/golden_set.jsonl"
	@echo "    WARNING: golden set is BOOTSTRAP (auto-drafted) — REQUIRES HUMAN CURATION."
	@echo "    Results are informational only until the golden set is reviewed."
	@OTEL_SDK_DISABLED=true TEST_TIMEOUT=3600 TEST_CPU_QUOTA=300% TEST_MEM_MAX=16G \
	  scripts/test-capped.sh uv run --extra test --extra ml python benchmarks/run_eval.py

## longmemeval: Run LongMemEval (external memory benchmark) via the unified MCP recall path (v5.80 fan-out).
## Retrieval-only + a stratified subset by default (fast iteration). Override: `make longmemeval Q=100`,
## pass extra flags via ARGS=..., full 500-q run: `make longmemeval Q=0 ARGS="--output benchmarks/reports/lme_full.json"`.
## Requires: `surreal` on PATH (or YADGAR_DB_URL set). NON-GATING: informational quality measurement.
Q ?= 30
longmemeval:
	@echo "==> LongMemEval (unified MCP recall · retrieval-only) — $(Q) questions (0=all) ..."
	@OTEL_SDK_DISABLED=true TEST_TIMEOUT=14400 TEST_CPU_QUOTA=300% TEST_MEM_MAX=20G \
	  scripts/test-capped.sh uv run --extra test --extra ml python benchmarks/run_longmemeval.py \
	    --unified --retrieval-only --variant s --stratify-per-type --max-questions $(Q) $(ARGS)

## perf: Run the #79 recall-latency load-test harness (RECORD-ONLY, non-gating).
## Fires N recalls at a RUNNING daemon, captures recall p50/p95 + the CE-span
## budget (backend yadgar_embed_rerank_duration_seconds{mode=ce}), writes a JSON
## report under benchmarks/reports/ and diffs against benchmarks/reports/perf_baseline.json.
## Requires a live daemon: set YADGAR_DAEMON_URL (+ YADGAR_BACKEND_METRICS_URL for CE).
## SKIPS with a reason (exit 0) if YADGAR_DAEMON_URL is unset. See
## docs/plans/perf-loadtest-contract-2026-06-30.md. NEVER gates a PR (Phase 1).
## CI runs this target inside a container with no user-scope systemd — both
## perf workflows set TEST_ALLOW_UNCAPPED=1 explicitly (the container runtime
## already provides the cgroup); locally it runs fully capped.
perf:
	@echo "==> #79 recall-latency load-test (record-only, non-gating) ..."
	@OTEL_SDK_DISABLED=true TEST_TIMEOUT=1800 TEST_CPU_QUOTA=300% TEST_MEM_MAX=12G \
	  scripts/test-capped.sh python benchmarks/run_perf_loadtest.py

## upgrade-test: Print the manual upgrade-test runbook (see docs/testing/upgrade-test.md)
upgrade-test:
	@echo "Manual recipe — see docs/testing/upgrade-test.md for the runbook."
	@cat docs/testing/upgrade-test.md
