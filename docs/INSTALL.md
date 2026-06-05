# Installing Yadgar

Four install paths. All end with `yadgar-setup` to complete configuration.

---

## 1. pipx (recommended)

```bash
pipx install yadgar
yadgar-setup
```

Installs the CLI in an isolated virtualenv. `yadgar-setup` is on PATH automatically.

For non-interactive (CI/automation):
```bash
pipx install yadgar
yadgar-setup --noninteractive
```

---

## 2. Homebrew (macOS and Linux with brew)

```bash
brew tap maxagahi/yadgar https://codeberg.org/maxagahi/homebrew-yadgar
brew install yadgar
yadgar-setup
```

The tap lives at `codeberg.org/maxagahi/homebrew-yadgar`. Follow the caveats printed
after `brew install` — they will remind you to run `yadgar-setup`.

macOS note: `yadgar-setup` uses launchd (v5.45.1 path). Run `yadgar-setup --doctor`
to verify the daemon loaded correctly.

After upgrades:
```bash
brew upgrade yadgar
yadgar-setup          # re-run to pick up new defaults
```

---

## 3. Nix flake

```bash
nix profile install codeberg:maxagahi/yadgar
yadgar-setup
```

Or pin to a specific version:
```bash
nix profile install codeberg:maxagahi/yadgar/v5.46.0
yadgar-setup
```

**NixOS users**: `yadgar-setup` refuses on NixOS (uses `nixosModules.default` instead).
Add the module to your NixOS configuration:

```nix
# flake.nix inputs
inputs.yadgar.url = "codeberg:maxagahi/yadgar";

# system configuration
imports = [ inputs.yadgar.nixosModules.default ];
services.yadgar.enable = true;
```

Then run `yadgar-setup` manually to configure Claude Code hooks and seed anchors.

---

## 4. Repo checkout (make-canonical)

```bash
git clone https://codeberg.org/maxagahi/yadgar.git
cd yadgar
make setup
```

`make setup` is the canonical path for contributors and power users. It does everything
`yadgar-setup` does, plus has access to all build targets. No `yadgar-setup` needed.

For macOS:
```bash
make setup   # auto-routes to generate_launchd.sh on macOS
```

---

## What `yadgar-setup` does

Configures Yadgar in 10 steps (matches `make setup` chain):

1. Detect container runtime (podman/docker)
2. Detect OS (linux/macos)
3. Pull container images (docker.io/openfantasy/yadgar + yadgar-backend)
4. Bootstrap secrets (`~/.yadgar/secrets.env`)
5. Generate daemon units (systemd on Linux, launchd on macOS)
6. Enable units (systemctl --user / launchctl bootstrap)
7. Install Claude Code git hooks (`yadgar install-hooks`)
8. Install subagent templates (`yadgar install-subagents`)
9. Sync config (`yadgar config sync`)
10. Append CLAUDE.md rules fragment + seed canonical anchors

**Re-runnable**: all steps are idempotent. Run `yadgar-setup` again after upgrades.

**Verification**: run `yadgar-setup --doctor` to check daemon health, metrics endpoint,
and (on macOS) plist validity.

---

## Flags

```
yadgar-setup [--noninteractive] [--dryrun] [--doctor]

  --noninteractive   Use defaults; skip interactive prompts (CI/automation).
  --dryrun           Print commands without executing them.
  --doctor           Run verification probes (metrics endpoint, launchd plint, etc.).
```

---

## Python version

Yadgar requires Python 3.14+.

- pipx: resolved from your system Python. Install 3.14 via `brew install python@3.14` or
  from [python.org](https://www.python.org/downloads/).
- Homebrew: formula uses `python@3.13` as fallback if `python@3.14` is not yet in
  Homebrew core (typical 1-2 month lag after CPython release).
- Nix: uses `python314` from `nixos-unstable`. NixOS stable channel may not have it yet.

---

## SBOM

Each release includes a CycloneDX 1.5 SBOM attached to the Codeberg release:
`yadgar-<version>-sbom.cdx.json`.

Generate locally:
```bash
pip install 'yadgar[sbom]'
bash scripts/generate_sbom.sh
```

Output: `dist/yadgar-<version>-sbom.cdx.json`.

---

## Source tarball and checksums

Available on each Codeberg release page:
```
https://codeberg.org/maxagahi/yadgar/releases/tag/v<version>
```

Verify:
```bash
sha256sum -c CHECKSUMS.txt
```
