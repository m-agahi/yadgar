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
          version = "5.68.0";
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
              default = "5.68.0";
              description = "Container image tag for the yadgar core service.";
            };

            backendVersion = lib.mkOption {
              type = lib.types.str;
              default = "5.7.2";
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
          };
        };
    };
}
