# Installing Yadgar

Three install paths. All end with `yadgar-setup` to complete configuration.

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

## 2. Nix flake

```bash
nix profile install codeberg:maxagahi/yadgar
yadgar-setup
```

Or pin to a specific version:
```bash
nix profile install codeberg:maxagahi/yadgar/v5.132.0
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

## 3. Repo checkout (make-canonical)

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

## Multi-client setup

One shared streamable-HTTP daemon (`http://127.0.0.1:8765/mcp`) serves the memory and wiki MCP surface to all 9 supported clients. After the daemon is running, use `yadgar install` to register any client. For the architectural rationale, see ADR-0144.

### Basic usage

```bash
# Register one client
yadgar install --client <name>

# Detect and register all installed clients
yadgar install --auto-detect

# Dry-run: emit JSON to stdout, write nothing (nix/home-manager contract)
yadgar install --client <name> --print
```

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--client NAME` | — | Target client (mutually exclusive with `--auto-detect`) |
| `--auto-detect` | — | Probe and register all detected clients |
| `--mcp` | — | Write MCP registration config only |
| `--rules` | — | Write rules file only (AGENTS.md-equivalent) |
| (neither) | — | Write both MCP config and rules file |
| `--print` | — | Dry-run: emit JSON to stdout, no file writes; auth uses env-ref (never literal tokens) |
| `--port PORT` | `8765` | Daemon port for the MCP endpoint URL |
| `--scope {global,project}` | `global` | Global home-dir config or per-project config |
| `--project-directory PATH` | — | Required when `--scope project` |

`--print` JSON shape:
```json
{"client": "opencode", "mcp": {"path": "...", "content": "..."}, "rules": {"path": "...", "content": "..."}, "dry_run": true}
```

`mcp` or `rules` is `null` when the corresponding `--mcp` / `--rules` flag was omitted.

### Per-client config paths

| Client | MCP config | Capability |
|---|---|---|
| `claude-code` | `~/.claude.json` | Full harness (hooks, task-list mirror, CLAUDE.md sync) |
| `codex` | `~/.codex/config.toml` | MCP + rules |
| `gemini` | `~/.gemini/settings.json` | MCP + rules |
| `cursor` | `~/.cursor/mcp.json` | MCP + rules |
| `cline` | VS Code globalStorage `cline_mcp_settings.json` | MCP + rules |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` | MCP + rules |
| `kiro` | `~/.kiro/settings/mcp.json` | MCP + rules |
| `amp` | `~/.config/amp/settings.json` | MCP + rules |
| `opencode` | `~/.config/opencode/opencode.json` | MCP + rules |

Rules files (AGENTS.md-equivalent) follow each client's native convention. Claude Code bridges via `@AGENTS.md` import; Gemini uses a `context.fileName` alias. All clients share the same daemon endpoint — no per-client server binary is needed.

### Nix / home-manager

Use `--print` for declarative activation (task #67). The flag guarantees no file writes and emits env-ref auth (`${YADGAR_MCP_AUTH_TOKEN}`) rather than a literal token — safe to bake into a nix derivation:

```bash
yadgar install --client <name> --print
```

The nix home-manager module reads this output and manages config files declaratively.

---

## Flags

```
yadgar-setup [--noninteractive] [--dryrun] [--doctor]
             [--install-runtime] [--no-install-runtime]

  --noninteractive       Use defaults; skip interactive prompts (CI/automation).
  --dryrun               Print commands without executing them.
  --doctor               Run verification probes (metrics endpoint, launchd plint, etc.).
  --install-runtime      Auto-install podman without prompting (yes-mode).
  --no-install-runtime   Skip podman install; print hint and exit 1 if not found.
```

**First-run on a clean system (no container runtime installed):** `yadgar-setup` detects
the missing runtime, prints a distro-specific install command, and offers to run it
interactively. Supported distros: Debian/Ubuntu, Fedora/RHEL/CentOS, Arch/Manjaro,
Alpine, openSUSE/SUSE, macOS (brew). Unknown distros show a link to podman.io.

For CI/automation with no TTY:
```bash
# Option A: allow auto-install
yadgar-setup --install-runtime

# Option B: fail fast with hint
yadgar-setup --noninteractive
```

---

## Python version

Yadgar requires Python 3.14+.

- pipx: resolved from your system Python. Install 3.14 via your package manager or
  from [python.org](https://www.python.org/downloads/).
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
