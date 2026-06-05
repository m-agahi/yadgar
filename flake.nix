# Yadgar nix flake — v5.46.0
#
# Outputs: packages.{default,yadgar} / apps.{default,yadgar} / nixosModules.default
#
# Channel choice: nixos-unstable required — Python 3.14 is not yet in stable
# nixpkgs channels as of 2026-06 (expected in nixos-26.05 stable ~mid-2026).
# Pin via flake.lock at lock time: `nix flake lock`.
#
# Usage (pipx equivalent via nix):
#   nix profile install codeberg:maxagahi/yadgar
#   yadgar-setup
#
# No auto-invocation of yadgar-setup from the flake.
# Users run it manually after install (Option C contract, v5.46.0).
#
# NixOS users: yadgar-setup refuses on NixOS (detect_os.sh linux-nixos guard).
# Use the nixosModules.default module instead to configure yadgar as a service.
#
# homeManagerModules: the existing home-manager module at
# ~/git/nix/modules/home/yadgar.nix is the current source of truth.
# Migration to flake-hosted module is opt-in; backward-compat shim documented
# in MIGRATION_NOTES.md v5.46.0.

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
          version = "5.46.5";
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
      # NixOS module — system-level yadgar service configuration.
      # Stub in v5.46.0: wires the package but does NOT auto-invoke yadgar-setup.
      # Full NixOS service module is a v5.46.x follow-up.
      nixosModules.default = { config, lib, pkgs, ... }: {
        options.services.yadgar = {
          enable = lib.mkEnableOption "Yadgar persistent memory engine";
        };

        config = lib.mkIf config.services.yadgar.enable {
          environment.systemPackages = [
            self.packages.${pkgs.stdenv.system}.yadgar
          ];
          # Note: daemon systemd unit configuration is managed by yadgar-setup.
          # Run `yadgar-setup` after installing this module.
          # Automated systemd unit wiring is deferred to a future NixOS module version.
        };
      };

      # homeManagerModules stub — points to existing nix-repo module.
      # Migration from ~/git/nix/modules/home/yadgar.nix to this flake is opt-in.
      # See MIGRATION_NOTES.md v5.46.0 for the backward-compat shim.
      homeManagerModules.default = { ... }: {
        # Stub: actual home-manager module lives at ~/git/nix/modules/home/yadgar.nix
        # Import it from there until the module migration is complete.
        # Future: this will be a full module at nix/modules/home-manager.nix.
      };
    };
}
