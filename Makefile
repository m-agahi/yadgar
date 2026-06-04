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
ANCHORS_YAML := $(INSTALL_ASSETS_DIR)/seeds/anchors.yaml
FRAGMENT     := $(INSTALL_ASSETS_DIR)/CLAUDE.md.fragment
CLAUDE_MD    := $(HOME)/.claude/CLAUDE.md

# User-facing defaults (override via env or command line)
INSTALL_NONINTERACTIVE ?= 0
YADGAR_CONTAINER_RUNTIME ?=
YADGAR_DIR   ?= $(HOME)/.yadgar

.PHONY: all help pre-setup setup uninstall uninstall-purge \
        install-hooks install-agents config-sync install-rules \
        seed-anchors detect-runtime detect-os clean check

all: setup

## help: Show this help
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## //' | column -t -s ':'

## pre-setup: Preflight checks (GNU make guard, detect runtime/OS)
pre-setup:
	@echo "==> Preflight checks..."
	@$(MAKE) --version | grep -q "GNU Make" || { echo "ERROR: GNU Make required"; exit 1; }
	@echo "    GNU Make: OK"
	@RUNTIME=$$(bash $(SCRIPTS_DIR)/detect_runtime.sh); \
	  echo "    Container runtime: $$RUNTIME"
	@OS=$$(bash $(SCRIPTS_DIR)/detect_os.sh); \
	  echo "    Host OS: $$OS"; \
	  if [ "$$OS" = "linux-nixos" ]; then \
	    echo ""; \
	    echo "ERROR: NixOS detected. Use the nix flake install (v5.46+) instead of make setup."; \
	    echo "       See: https://codeberg.org/maxagahi/yadgar#nixos-install"; \
	    exit 1; \
	  fi

## detect-runtime: Probe and print container runtime
detect-runtime:
	@bash $(SCRIPTS_DIR)/detect_runtime.sh

## detect-os: Probe and print host OS
detect-os:
	@bash $(SCRIPTS_DIR)/detect_os.sh

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

## seed-anchors: Seed canonical anchor memories from install_assets/seeds/anchors.yaml
seed-anchors:
	python3 -m yadgar seed --anchors $(ANCHORS_YAML)

## setup: Full install (pre-setup → runtime → systemd units → hooks → agents → config → rules → anchors)
setup: pre-setup
	@echo "==> Detecting container runtime..."
	@RUNTIME=$$(bash $(SCRIPTS_DIR)/detect_runtime.sh); \
	  echo "    Runtime: $$RUNTIME"; \
	  YADGAR_RUNTIME=$$RUNTIME \
	  YADGAR_INSTALL_PREFIX="$(YADGAR_DIR)" \
	  YADGAR_SECRETS_ENV_FILE="$(YADGAR_DIR)/secrets.env" \
	  YADGAR_BACKEND_IMAGE="openfantasy/yadgar-backend:latest" \
	  YADGAR_CORE_IMAGE="openfantasy/yadgar:latest" \
	  YADGAR_SYSTEMD_OUTPUT_DIR="$(HOME)/.config/systemd/user" \
	  bash $(SCRIPTS_DIR)/generate_systemd.sh
	@$(MAKE) install-hooks
	@$(MAKE) install-agents
	@$(MAKE) config-sync
	@$(MAKE) install-rules
	@$(MAKE) seed-anchors
	@echo ""
	@echo "==> Yadgar setup complete!"
	@echo "    Run: systemctl --user start yadgar.target"

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

## check: Run v5.45.0 tests
check:
	python3 -m pytest yadgar/tests/test_v5_45_*.py --noconftest --override-ini="addopts=" -q
