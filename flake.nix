# Yadgar nix flake — v5.49.1
#
# Outputs: packages.{default,yadgar} / apps.{default,yadgar}
#          / nixosModules.default / homeManagerModules.default
#
# Channel choice: nixos-unstable required — Python 3.14 is not yet in stable
# nixpkgs channels as of 2026-06 (expected in nixos-26.05 stable ~mid-2026).
# Pin via flake.lock at lock time: `nix flake lock`.
#
# Usage (pipx equivalent via nix profile):
#   nix profile install codeberg:maxagahi/yadgar
#   yadgar-setup
#
# home-manager users (recommended): import homeManagerModules.default and set
# `programs.yadgar.enable = true;` to get systemd user units (yadgar.service
# + yadgar-backend.service) wired with the v5.49 Phase 7 design:
# Type=notify + podman --sdnotify=healthy + explicit --health-cmd flags
# (Dockerfile HEALTHCHECK is NOT propagated to Config.Healthcheck by podman
# build — known quirk — so the healthcheck is supplied at run time instead),
# NotifyAccess=all so the host CLI subprocess can emit STOPPING=1 for the
# v5.49 graceful-stop drain barrier, and EnvironmentFile=upgrade.env so the
# upgrade orchestrator can compose. See programs.yadgar.* options below.
#
# NixOS users: yadgar-setup refuses on NixOS (detect_os.sh linux-nixos guard).
# The nixosModules.default ships the package only — use home-manager
# for the systemd unit wiring (user-scope daemon is the expected deploy model
# for personal Claude Code use cases).

{
  description = "Yadgar — persistent memory engine for Claude Code";

  inputs = {
    # nixos-unstable required for python314Packages (not yet in stable as of 2026-06)
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python314 or pkgs.python313;

        yadgar-pkg = python.pkgs.buildPythonApplication {
          pname = "yadgar";
          version = "5.102.0";
          format = "pyproject";

          src = ./.;

          nativeBuildInputs = with python.pkgs; [ hatchling ];

          propagatedBuildInputs = with python.pkgs; [
            fastapi
            uvicorn
            httpx
            pydantic
            numpy
            scipy
            networkx
            watchdog
            msgpack
            regex
            prometheus-client
          ];

          # Ship yadgar-setup.sh to $out/bin/yadgar-setup
          # No auto-invocation: user runs manually after install.
          postInstall = ''
            if [ -f $out/share/yadgar/scripts/yadgar-setup.sh ]; then
              install -Dm755 $out/share/yadgar/scripts/yadgar-setup.sh \
                $out/bin/yadgar-setup
            elif [ -f ${./.}/scripts/install/yadgar-setup.sh ]; then
              install -Dm755 ${./.}/scripts/install/yadgar-setup.sh \
                $out/bin/yadgar-setup
            fi
          '';

          meta = with pkgs.lib; {
            description = "Persistent memory engine for Claude Code";
            homepage = "https://codeberg.org/maxagahi/yadgar";
            license = licenses.asl20;
            maintainers = [ ];
          };
        };

      in {
        packages = {
          yadgar = yadgar-pkg;
          default = yadgar-pkg;
        };

        apps = {
          yadgar = {
            type = "app";
            program = "${yadgar-pkg}/bin/yadgar";
          };
          default = self.apps.${system}.yadgar;
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [ python pkgs.uv pkgs.git ];
        };
      }
    ) // {
      # NixOS module — system-level yadgar package install only.
      # The systemd unit wiring is intentionally left to the home-manager
      # module, since the canonical deploy model is user-scope (one yadgar
      # per developer, not a system-wide service). NixOS users who want a
      # system-wide deploy should copy the home-manager service definitions
      # below and adapt to systemd.services with a dedicated `yadgar` user.
      nixosModules.default = { config, lib, pkgs, ... }: {
        options.services.yadgar = {
          enable = lib.mkEnableOption "Yadgar persistent memory engine package";
        };

        config = lib.mkIf config.services.yadgar.enable {
          environment.systemPackages = [
            self.packages.${pkgs.stdenv.system}.yadgar
          ];
          # Daemon systemd unit wiring is in homeManagerModules.default.
          # For a system-wide deploy: copy that module and adapt to system scope.
        };
      };

      # home-manager module — real Phase 7 systemd user-unit wiring.
      #
      # Wires `yadgar-backend.service` (SurrealDB + embed) and `yadgar.service`
      # (core MCP server) as Type=notify systemd user units. Both use
      # `podman --sdnotify=healthy` so systemd marks the unit Active once the
      # container HEALTHCHECK reports healthy.
      #
      # Secrets management is the user's responsibility — point
      # `programs.yadgar.secretsEnvFile` at a `--mode=600` env-file containing
      # SURREAL_USER / SURREAL_PASS / YADGAR_DB_USER / YADGAR_DB_PASS /
      # YADGAR_MCP_AUTH_TOKEN / YADGAR_RW_USER / YADGAR_RW_PASS /
      # YADGAR_RO_USER / YADGAR_RO_PASS. Typically generated by `yadgar setup`.
      #
      # Image management: this module does NOT pull images. Users either
      # (a) build locally with `podman build --arch amd64 -t docker.io/openfantasy/yadgar:<ver> -f Dockerfile .`,
      # (b) `podman pull docker.io/openfantasy/yadgar:<ver>` from registry,
      # or (c) flip `programs.yadgar.imageRegistry` to a private mirror.
      # The v5.49 upgrade orchestrator (`yadgar update --install`, opt-in via
      # `update.install_enabled: true`) handles routine image pulls.
      homeManagerModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.programs.yadgar;
          homeDir = config.home.homeDirectory;

          # Python interpreter pinned for the pipx host-CLI activation.
          # Uses the or-fallback so that if python314 is not available in
          # the nixpkgs revision, it gracefully falls back to python313.
          # All three pipx activation references (--python, drift guard,
          # ruamel .pth) must use this single binding — do not inline a
          # separate pkgs.python314 anywhere in the pipx activation block.
          python = pkgs.python314 or pkgs.python313;

          # Default runtime: podman via the docker-compat shim. NixOS users
          # typically have virtualisation.podman.dockerCompat = true; which
          # exposes /run/current-system/sw/bin/docker as a podman shim. If
          # that's not available, set programs.yadgar.runtime explicitly.
          defaultRuntime = "/run/current-system/sw/bin/docker";

          coreImage = "${cfg.imageRegistry}/yadgar:${cfg.coreVersion}";
          backendImage = "${cfg.imageRegistry}/yadgar-backend:${cfg.backendVersion}";

          # Resolved XDG paths (computed once for the module).
          dataDir = "${homeDir}/.local/share/yadgar";
          configDir = "${homeDir}/.config/yadgar";
          stateDir = "${homeDir}/.local/state/yadgar";
          upgradeEnvPath = "${stateDir}/upgrade.env";
        in {
          options.programs.yadgar = {
            enable = lib.mkEnableOption "Yadgar systemd user units (core + backend)";

            coreVersion = lib.mkOption {
              type = lib.types.str;
              default = "5.102.0";
              description = "Container image tag for the yadgar core service.";
            };

            backendVersion = lib.mkOption {
              type = lib.types.str;
              default = "5.10.0";
              description = "Container image tag for the yadgar-backend service.";
            };

            imageRegistry = lib.mkOption {
              type = lib.types.str;
              default = "docker.io/openfantasy";
              description = "Registry prefix for yadgar + yadgar-backend images. Override for private mirrors.";
            };

            runtime = lib.mkOption {
              type = lib.types.str;
              default = defaultRuntime;
              description = "Path to the docker / podman binary that systemd will exec.";
            };

            secretsEnvFile = lib.mkOption {
              type = lib.types.str;
              default = "${configDir}/secrets.env";
              description = "Path to the chmod-600 env-file carrying SURREAL_USER/SURREAL_PASS/YADGAR_MCP_AUTH_TOKEN/etc. EnvironmentFile uses the '-' prefix so missing files are non-fatal.";
            };

            network = lib.mkOption {
              type = lib.types.str;
              default = "yadgar-net";
              description = "Podman/docker network shared between the core + backend containers.";
            };

            corePort = lib.mkOption {
              type = lib.types.port;
              default = 8765;
              description = "Loopback port bound by the core MCP server.";
            };

            vizPort = lib.mkOption {
              type = lib.types.port;
              default = 42069;
              description = "Loopback port bound by the viz UI inside the core container.";
            };

            backendSurrealPort = lib.mkOption {
              type = lib.types.port;
              default = 8000;
              description = "Loopback port for SurrealDB inside the backend container.";
            };

            backendEmbedPort = lib.mkOption {
              type = lib.types.port;
              default = 8001;
              description = "Loopback port for the embedding service inside the backend container.";
            };
          };

          config = lib.mkIf cfg.enable {
            home.packages = [ self.packages.${pkgs.stdenv.system}.yadgar ];

            # ── yadgar-backend.service ──────────────────────────────────────
            systemd.user.services.yadgar-backend = {
              Unit = {
                Description = "Yadgar Backend (SurrealDB + Embeddings)";
                After = [ "network.target" ];
              };
              Service = {
                # v5.49 Phase 7: Type=notify + podman --sdnotify=healthy.
                # Dockerfile HEALTHCHECK isn't propagated by podman build
                # (known quirk — lands in history.created_by only), so the
                # healthcheck is passed at run time via --health-cmd. Embed
                # model warm-up needs --health-start-period=60s;
                # TimeoutStartSec=180 covers cold model load.
                Type = "notify";
                NotifyAccess = "all";
                Environment = [
                  "CUDA_VISIBLE_DEVICES=-1"
                  "DOCKER_HOST=unix:///run/podman/podman.sock"
                  "SURREAL_RUNTIME_STACK_SIZE=536870912"
                ];
                EnvironmentFile = "-${cfg.secretsEnvFile}";
                TimeoutStartSec = 180;
                TimeoutStopSec = 45;
                ExecStartPre = [
                  "-${cfg.runtime} stop yadgar-backend"
                  "-${cfg.runtime} rm yadgar-backend"
                  "-${cfg.runtime} network create ${cfg.network}"
                  "-${pkgs.bash}/bin/bash -c 'mkdir -p ${dataDir} ${configDir} ${stateDir} && chmod 700 ${configDir} ${stateDir}'"
                ];
                ExecStart = lib.concatStringsSep " " [
                  cfg.runtime "run --name yadgar-backend --rm --user root"
                  "--network ${cfg.network} --sdnotify=healthy"
                  "--health-cmd 'curl -f http://localhost:8001/health || exit 1'"
                  "--health-interval 30s --health-timeout 5s --health-start-period 60s"
                  "-p 127.0.0.1:${toString cfg.backendSurrealPort}:8000"
                  "-p 127.0.0.1:${toString cfg.backendEmbedPort}:8001"
                  "-v ${dataDir}:/data"
                  "-v ${configDir}/config.yaml:/data/config.yaml:ro"
                  "-v ${homeDir}/.cache/huggingface:/root/.cache/huggingface"
                  "-e SURREAL_USER -e SURREAL_PASS"
                  "-e YADGAR_RW_USER -e YADGAR_RW_PASS"
                  "-e YADGAR_RO_USER -e YADGAR_RO_PASS"
                  "-e YADGAR_MCP_AUTH_TOKEN"
                  "-e SURREAL_RUNTIME_STACK_SIZE"
                  "-e YADGAR_CONFIG_FILE=/data/config.yaml"
                  "--memory 4g --cpus 2 --stop-timeout 30"
                  backendImage
                ];
                ExecStop = "${cfg.runtime} stop yadgar-backend";
                Restart = "on-failure";
                RestartSec = 5;
              };
              Install.WantedBy = [ "default.target" ];
            };

            # ── yadgar.service (core MCP server) ────────────────────────────
            systemd.user.services.yadgar = {
              Unit = {
                Description = "Yadgar Memory Engine / MCP Server (core)";
                After = [ "yadgar-backend.service" "network.target" ];
                Wants = [ "yadgar-backend.service" ];
              };
              Service = {
                # v5.49 Phase 7: same Type=notify + --sdnotify=healthy +
                # --health-cmd story as yadgar-backend above. NotifyAccess=all
                # lets the host CLI emit STOPPING=1 for graceful-stop.
                Type = "notify";
                NotifyAccess = "all";
                Environment = [
                  "DOCKER_HOST=unix:///run/podman/podman.sock"
                ];
                # upgrade.env carries YADGAR_IMAGE_TAG, written by the v5.49
                # upgrade orchestrator on `yadgar update --install`. Loaded
                # so the orchestrator can compose with snapshot/rollback
                # bookkeeping. The nix-time ExecStart still uses the literal
                # coreVersion as the canonical tag — for a nix-managed
                # deploy, bumping programs.yadgar.coreVersion and rebuilding
                # is the primary upgrade path.
                EnvironmentFile = [
                  "-${cfg.secretsEnvFile}"
                  "-${upgradeEnvPath}"
                ];
                TimeoutStartSec = 120;
                TimeoutStopSec = 45;
                ExecStartPre = [
                  "-${cfg.runtime} stop yadgar"
                  "-${cfg.runtime} rm yadgar"
                  "-${pkgs.bash}/bin/bash -c 'mkdir -p ${dataDir} ${configDir} ${stateDir} && chmod 700 ${configDir} ${stateDir}'"
                ];
                ExecStart = lib.concatStringsSep " " [
                  cfg.runtime "run --name yadgar --rm --user root"
                  "--network ${cfg.network} --sdnotify=healthy"
                  "--health-cmd 'curl -f http://localhost:8765/health || exit 1'"
                  "--health-interval 30s --health-timeout 5s --health-start-period 10s"
                  "--add-host=host.containers.internal:host-gateway"
                  "-p 127.0.0.1:${toString cfg.corePort}:8765"
                  "-p 127.0.0.1:${toString cfg.vizPort}:42069"
                  "-v ${dataDir}:/data"
                  "-v ${configDir}/config.yaml:/data/config.yaml:ro"
                  "-v ${stateDir}:/root/.local/state/yadgar"
                  "-e YADGAR_DB_URL=http://yadgar-backend:8000"
                  "-e YADGAR_EMBED_URL=http://yadgar-backend:8001"
                  "-e YADGAR_DATA_DIR=/data"
                  "-e YADGAR_CONFIG_FILE=/data/config.yaml"
                  "-e YADGAR_DB_USER -e YADGAR_DB_PASS -e YADGAR_MCP_AUTH_TOKEN"
                  "-e YADGAR_IN_CONTAINER=1"
                  "--memory 1g --cpus 1 --stop-timeout 30"
                  coreImage
                ];
                ExecStop = "${cfg.runtime} stop yadgar";
                Restart = "on-failure";
                RestartSec = 10;
              };
              Install.WantedBy = [ "default.target" ];
            };

            # ── pipx host-CLI activation ────────────────────────────────────
            # Installs the PyPI wheel into a pipx venv on the host so that
            # yadgar-vacuum and yadgar-nightly-cycle systemd units can execute
            # the yadgar binary without spawning a container (host-execution
            # model — see yadgar-vacuum.service comment below for rationale).
            #
            # NOTE: the vacuum/nightly TIMERS only (re)start on `home-manager
            # switch` if your home config sets `systemd.user.startServices =
            # "sd-switch";`. This module does NOT force that (it is a user-wide
            # preference affecting all your services); set it yourself to have the
            # timers activate without a re-login.
            #
            # Three guards before reinstalling:
            #   1. Version mismatch (HAVE vs WANT = cfg.coreVersion)
            #   2. EDITABLE guard — old `pipx install -e` editable installs
            #      report a version string that may match WANT, so we force a
            #      transition to the PyPI wheel even when versions match.
            #   3. Python-drift guard — pipx venvs do not migrate their
            #      interpreter on python derivation upgrades; a stale venv
            #      interpreter causes SEGV at teardown (seen with CPython
            #      _asyncio). Reinstall when the venv's interpreter path
            #      differs from the nix-store path of the pinned python.
            #
            # UV_NO_CACHE=1 forces uv (pipx's resolver) to re-fetch the PyPI
            # /simple index instead of serving a stale cached listing (#69).
            # uv caches the simple index for 600 s and does not auto-retry on
            # version-not-found (uv issue #16281); without this flag a release
            # published <10 min after a previous `up` was invisible →
            # "no version X" → activation aborted. Scoped to the version-change
            # branch only; no-op activations pay nothing.
            home.activation.pipxYadgar = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
              export PIPX_HOME="$HOME/.local/pipx"
              export PIPX_BIN_DIR="$HOME/.local/bin"
              YADGAR_VENV="$PIPX_HOME/venvs/yadgar"
              WANT="${cfg.coreVersion}"
              HAVE="$("$YADGAR_VENV/bin/python" -c 'import yadgar; print(yadgar.__version__)' 2>/dev/null || echo none)"
              # EDITABLE != 0 when the venv still has an old `pipx install -e`
              # editable install — force one-time transition to the PyPI wheel
              # even when the version string already matches.
              EDITABLE="$("$YADGAR_VENV/bin/python" -m pip show yadgar 2>/dev/null | grep -c 'Editable project location' || true)"
              # Python-drift guard: reinstall when the venv's interpreter
              # differs from the pinned python derivation. Prevents SEGV from
              # stale interpreters after a python package upgrade.
              WANT_PY="$(readlink -f ${python}/bin/python3 2>/dev/null || echo want)"
              HAVE_PY="$(readlink -f "$YADGAR_VENV/bin/python" 2>/dev/null || echo none)"
              if [ "$HAVE" != "$WANT" ] || [ "$EDITABLE" != "0" ] || [ "$HAVE_PY" != "$WANT_PY" ]; then
                # Destroy the venv first — `pipx install --force --python X`
                # does NOT swap a venv's interpreter; it reinstalls into the
                # EXISTING venv, leaving stale python bin symlinks. Fresh
                # uninstall + rm guarantees bin/python is rebuilt against the
                # pinned python derivation.
                $DRY_RUN_CMD ${pkgs.pipx}/bin/pipx uninstall yadgar || true
                $DRY_RUN_CMD rm -rf "$YADGAR_VENV"
                # UV_NO_CACHE=1 — see comment block above (#69).
                UV_NO_CACHE=1 $DRY_RUN_CMD ${pkgs.pipx}/bin/pipx install \
                  "yadgar==${cfg.coreVersion}" \
                  --python ${python}/bin/python3
                $DRY_RUN_CMD rm -f "$YADGAR_VENV/.editable-installed"
              fi

              # ruamel.yaml cannot be pip-installed on NixOS — inject from nix
              # store via a .pth file so the venv can import it without
              # breaking the pipx-managed dependency closure.
              if [ -d "$YADGAR_VENV" ]; then
                VENV_SITE="$("$YADGAR_VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
                echo "${python.pkgs.ruamel-yaml}/${python.sitePackages}" \
                  > "$VENV_SITE/ruamel-yaml-nix.pth"
              fi
            '';

            # ── numpy LD_LIBRARY_PATH wrapper ───────────────────────────────
            # systemd-user services do not inherit the login-shell
            # LD_LIBRARY_PATH, so numpy's .so files (built against the nix
            # store's libstdc++) fail to load with:
            #   ImportError: libstdc++.so.6: cannot open shared object file
            # This wrapper prepends gcc's runtime lib dir before exec-ing the
            # pipx-installed yadgar-nightly-cycle console script.
            # Mirror of reference module (PLAN_NIGHTLY_BACKUP_NIX_FIX.md opt A).
            home.file.".local/bin/yadgar-nightly-cycle-wrapper.sh".source =
              pkgs.writeShellScript "yadgar-nightly-cycle-wrapper" ''
                export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:''${LD_LIBRARY_PATH:-}"
                exec ${homeDir}/.local/bin/yadgar-nightly-cycle "$@"
              '';

            # ── yadgar-vacuum.service + timer ────────────────────────────────
            # Weekly SurrealKV vacuum. Runs `yadgar vacuum` via the
            # pipx-installed host binary rather than a container, because the
            # vacuum flow has interleaved phases requiring different daemon
            # states (export → backend DOWN → reimport → backend UP); the
            # container image does not include systemctl, so --service-mode=
            # systemd fails inside a container. Host execution sidesteps both.
            #
            # Uses cfg.secretsEnvFile and cfg.backendSurrealPort to match the
            # module's existing option style (no hardcoded paths/ports).
            systemd.user.services.yadgar-vacuum = {
              Unit = {
                Description = "Yadgar SurrealKV vacuum (export → fresh DB → reimport)";
                After = [
                  "yadgar.service"
                  "yadgar-backend.service"
                ];
              };
              Service = {
                Type = "oneshot";
                EnvironmentFile = "-${cfg.secretsEnvFile}";
                Environment = [
                  "YADGAR_DB_URL=http://127.0.0.1:${toString cfg.backendSurrealPort}"
                  "YADGAR_DATA_DIR=${dataDir}"
                ];
                ExecStart = "${homeDir}/.local/bin/yadgar vacuum --service-mode=systemd --yes";
                TimeoutStartSec = "30min";
              };
            };

            systemd.user.timers.yadgar-vacuum = {
              Unit.Description = "Weekly Yadgar vacuum";
              Timer = {
                OnCalendar = "Sun *-*-* 04:00:00";
                RandomizedDelaySec = "30min";
                Persistent = true;
              };
              Install.WantedBy = [ "timers.target" ];
            };

            # ── yadgar-nightly-cycle.service + timer ─────────────────────────
            # Nightly backup → consolidation → vacuum → backup cycle at 19:00
            # UTC. Runs via the LD_LIBRARY_PATH wrapper (see home.file above)
            # which prepends gcc's runtime lib dir for numpy's .so dependencies
            # before exec-ing the pipx console script yadgar-nightly-cycle.
            #
            # YADGAR_EMBED_URL routes nightly consolidation embeddings through
            # the backend embed service (host-published on backendEmbedPort)
            # instead of an in-process SentenceTransformer — the host pipx
            # yadgar binary has no [ml] extra.
            #
            # Uses cfg.secretsEnvFile, cfg.backendSurrealPort,
            # cfg.backendEmbedPort, and dataDir to match module option style.
            systemd.user.services.yadgar-nightly-cycle = {
              Unit = {
                Description = "Yadgar nightly cycle (backup → consolidate → vacuum → backup)";
                After = [
                  "yadgar.service"
                  "yadgar-backend.service"
                ];
              };
              Service = {
                Type = "oneshot";
                EnvironmentFile = "-${cfg.secretsEnvFile}";
                Environment = [
                  "YADGAR_DB_URL=http://127.0.0.1:${toString cfg.backendSurrealPort}"
                  "YADGAR_EMBED_URL=http://127.0.0.1:${toString cfg.backendEmbedPort}"
                  "YADGAR_DATA_DIR=${dataDir}"
                ];
                # Wrapper exports LD_LIBRARY_PATH for numpy's .so dependencies.
                ExecStart = "${homeDir}/.local/bin/yadgar-nightly-cycle-wrapper.sh";
                TimeoutStartSec = "1h";
              };
            };

            systemd.user.timers.yadgar-nightly-cycle = {
              Unit.Description = "Nightly Yadgar cycle (19:00 UTC)";
              Timer = {
                OnCalendar = "*-*-* 19:00:00 UTC";
                Persistent = true;
              };
              Install.WantedBy = [ "timers.target" ];
            };
          };
        };
    };
}
