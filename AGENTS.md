# AGENTS.md

Operational guide for AI coding agents working on Yadgar. Human-oriented context (features, benchmark, production scale, design rationale) lives in [`README.md`](README.md) — read it for the *why*; read this for the *how*.

## Project overview

Yadgar is a persistent memory engine for Claude Code, packaged as an MCP server. Python 3.14+. Two process tiers:

- **Core** (`openfantasy/yadgar`, this repo) — MCP + HTTP server, retrieval pipeline, consolidation, CLI. Default port `8765`.
- **Backend** (`openfantasy/yadgar-backend`, separate image) — SurrealDB + embed/rerank service. Default port `8001` (embed), `8000` (SurrealDB).

Versions are split: `pyproject.toml:version` drives the core image; `server.json:backend_version` drives the backend image. The pre-commit `check-versions` hook enforces consistency across `pyproject.toml`, `server.json`, `Dockerfile`, `Dockerfile.backend`, `docker-compose.yml`, `scripts/setup.sh`.

Entry point: `yadgar.__main__:cli` (console script `yadgar`). Run `yadgar --help` for the full subcommand tree.

## Setup commands

Pick one path. All three end up MCP-registered with Claude Code.

### Fast path — user install (pipx)

```bash
pipx install yadgar          # or: pip install yadgar
yadgar setup                 # writes ~/.yadgar/{config.yaml,secrets.env}
set -a && . ~/.yadgar/secrets.env && set +a
yadgar daemon start
yadgar daemon configure-mcp  # writes ~/.claude.json with bearer header
```

`configure-mcp` reads `$YADGAR_MCP_AUTH_TOKEN` from env at invocation time — if unset the header is omitted and `/mcp` returns 401.

**uv cache gotcha (fresh PyPI publish):** installing a just-published version back-to-back can hit uv's cached PyPI simple-index (600 s freshness window). uv won't auto-retry on version-not-found, producing `No solution: no version X` (see [uv#16281](https://github.com/astral-sh/uv/issues/16281)). Workaround: `UV_NO_CACHE=1 pipx install yadgar==<ver>` or `rm -rf ~/.cache/uv/simple-v* && pipx install yadgar==<ver>`.

### Dev path — repo checkout

```bash
git clone https://codeberg.org/maxagahi/yadgar.git
cd yadgar
make setup                   # canonical for repo work; runs everything in one shot
```

`make setup` chains: `pre-setup → detect-runtime → detect-os → install-runtime → install-hooks → install-agents → config-sync → install-rules → seed-anchors → pull-images → bootstrap-secrets → enable-units`. Re-runnable after upgrades.

Useful Makefile targets: `make help`, `make check`, `make clean`, `make uninstall`, `make pull-images`, `make restore`.

### Stdio-only minimal (no daemon, no Docker)

Single-session use. Add to `~/.claude.json`:

```json
{ "mcpServers": { "yadgar": { "command": "yadgar", "args": [] } } }
```

Then restart Claude Code. No auth, no backend container — embed/rerank degrades gracefully.

### Required env vars (http + docker modes)

| Var | Required when | Source |
|---|---|---|
| `YADGAR_MCP_AUTH_TOKEN` | `YADGAR_REQUIRE_AUTH=1` (v5 default) | `~/.yadgar/secrets.env` |
| `SURREAL_USER` / `SURREAL_PASS` | backend container starts | same |
| `YADGAR_DB_URL` / `YADGAR_EMBED_URL` | core container starts | defaults in `yadgar daemon`; explicit in docker-manual |

Full reference: [`docs/configuration.md`](docs/configuration.md).

## Dev environment tips

- Python: **3.14+ required**. Use pyenv, asdf, or the Nix flake (`nix develop`) — host Python <3.14 will not install.
- Install dev extras: `pip install -e '.[test,ml,dev]'` after `python -m venv .venv && . .venv/bin/activate`.
- Optional extras: `[test]` (pytest, hypothesis), `[ml]` (sentence-transformers, torch), `[dev]` (ruff). `[analytics]` for DuckDB export.
- Run the daemon against the working tree without reinstalling: `python -m yadgar daemon start --foreground` (or use the systemd user units after `make setup`).
- Daemon control: `yadgar daemon {start|stop|restart|status}`. Logs: `journalctl --user -u yadgar.service -f` (Linux) or `~/Library/Logs/yadgar/` (macOS).
- Reset local state during dev: stop daemon, `rm -rf ~/.yadgar/surreal_db` (loses memory), restart — entrypoint re-bootstraps schema.
- Bump container images locally with full registry-prefixed tag: `podman build --arch amd64 -t docker.io/openfantasy/yadgar:VER -f Dockerfile .`. Tags without the `docker.io/` prefix land as `localhost/...` and break systemd `ExecStart` refs.

## Testing instructions

```bash
pytest                                # full suite minus integration; 4 workers, 300s timeout
pytest -m integration                 # slow opt-in tier (SurrealDB, embed service)
pytest yadgar/tests/test_recall.py -k branch_filter   # focused
pytest --lf                           # rerun last failures
```

- Config lives in `pyproject.toml [tool.pytest.ini_options]`. Markers: `xdist_group` (serialised), `integration` (excluded by default).
- `asyncio_mode = auto` — no `@pytest.mark.asyncio` decorator needed.
- Add or update tests in the same change. New retrieval / consolidation / storage code without a failing-then-passing test will fail review.
- Pre-commit invariant scripts (`scripts/check_*.py`) are real test gates — run `pre-commit run --all-files` before pushing.

## Code style

- **Lint + format:** `ruff` (target `py314`, line length 100). Rules: `E, W, F, I, UP, B, C901, PLR0913`; ignores `E501, B008, UP007`. Max complexity 15, max args 8.
- Auto-fix: `ruff check --fix . && ruff format .`. Pre-commit runs this automatically.
- Per-file grandfathered exceptions for `C901` / `PLR0913` are listed in `pyproject.toml [tool.ruff.lint.per-file-ignores]` — do not add new ones; refactor instead.
- Type hints required on new public functions. `dict[str, Any]` over `Dict[str, Any]` (project targets 3.14+, no `from __future__ import annotations` needed).
- No shell wrappers around Python invocations. CLI work lives in `yadgar/cli/`; MCP tool handlers in `yadgar/server/tools/`.
- Logging: `logging.getLogger(__name__)`. Structured fields via `extra={...}`. JSON formatter active when `YADGAR_LOG_FORMAT=json`.

## Architecture map

| Path | Purpose |
|---|---|
| `yadgar/__main__.py` | CLI entry point + Click command tree |
| `yadgar/cli/` | Subcommand implementations (daemon, vacuum, seed, config, rules, viz, setup, install_hooks) |
| `yadgar/server/http.py` | FastAPI app — `/health`, `/metrics`, `/api/*`, `/hooks/*`, `/mcp`, `/static/` |
| `yadgar/server/tools/` | MCP tool handlers (memory, wiki, bookmarks, project, ops, admin) — one file per group |
| `yadgar/storage/` | SurrealDB layer + idempotent migrations |
| `yadgar/retrieval/` | 8-stage pipeline: FTS, KNN, PPR, spreading, temporal, fusion, rerank, NLI, MMR, rules |
| `yadgar/consolidation/` | Nightly cycle: decay, episodic→semantic (CLS), merge, link, causal discovery |
| `yadgar/curation/` | Ingestion, dedup, surprise gate, pruning |
| `yadgar/security/` | Bearer auth, secret-pattern blocker, allowlist |
| `yadgar/file_queue/` | Async write queue + DLQ + drainer |
| `yadgar/vacuum/` | SurrealKV compaction |
| `yadgar/observability/` | OpenTelemetry tracing, distributed spans |
| `yadgar/hooks/` | Claude Code hook scripts (e.g. `db-lockdown-check.py`) |
| `yadgar/install_assets/` | Anchor seeds, CLAUDE.md fragments, systemd/launchd templates |
| `yadgar/static/` | Viz UI + bookmarks UI |
| `yadgar/tests/` | pytest suite |
| `scripts/` | Pre-commit invariant checks + version sync |
| `docs/` | Architecture, retrieval, configuration, hooks, benchmark, roadmap |

53 MCP tools across memory, wiki, bookmarks, project, ops. Full table in `README.md § Tools`.

## Operations cheatsheet

```bash
yadgar daemon status                  # http mode health
yadgar stats [--project /path]        # memory + wiki counts
yadgar vacuum                         # SurrealKV compaction (manual)
yadgar viz                            # graph UI at http://localhost:42069
yadgar export duckdb --output snap.duckdb   # analytics snapshot (needs [analytics])
yadgar seed <directory>               # bootstrap memory from README + docs
yadgar rules add|export|import        # retrieval / write policy rules
yadgar config init|list|get|set|edit  # ~/.yadgar/config.yaml
yadgar install_hooks --scope global   # wire Claude Code hooks; injects bearer token
yadgar update --check                 # PyPI version probe (v5.48.0+)
yadgar update --install               # multi-step coordinated upgrade (gated; opt-in)
yadgar update --rollback              # restore prior image from latest snapshot
yadgar daemon graceful-stop --timeout=30   # explicit drain barrier before stop
```

HTTP smoke checks (loopback, unauthenticated):

```bash
curl -s http://127.0.0.1:8765/health
curl -s http://127.0.0.1:8765/metrics | head
```

## Security considerations

- **Upgrade orchestrator is opt-in** (`update.install_enabled: false` by default). Snapshot artefacts at `~/.local/state/yadgar/upgrade-snapshots/` may contain prior systemd unit content — chmod 700, do not commit to VCS.
- **Never bypass auth.** Bearer middleware guards `/api/*`, `/hooks/*`, `/mcp`. `YADGAR_REQUIRE_AUTH=0` is initial-rollout only — production must run with `1`.
- **Never log secrets.** Always-on secret patterns block AWS, GCP, Stripe, Slack, OpenAI, Anthropic keys, JWT, GitHub PATs, private keys, DB URIs at write time — adding an exception requires the context-aware allowlist (`~/.yadgar/secret-gate-allowlist.yaml`) plus the `check-secret-gate` pre-commit gate.
- **Never query SurrealDB directly** (no `docker exec` into `yadgar-backend`, no raw `surreal sql`, no opening the surrealkv file). Use MCP tools (`recall`, `memory_stats`, `project_brief`) or the HTTP API.
- `/metrics` is loopback-only by default — do not bind it to a public interface without auth in front.
- `install_hooks` is a real Python script with `shlex.quote`'d paths; if you touch it, do not introduce string interpolation into shell commands.

## PR instructions

- **Branch first.** Never push directly to `master`. Default branch is `master`. Pull latest before branching.
- **Title format:** Conventional Commits — `feat: …`, `fix: …`, `docs: …`, `chore: …`, `refactor: …`, `test: …`. Add scope when useful: `feat(retrieval): …`.
- **No `Co-Authored-By:` trailers.** No exceptions.
- **No `--no-verify`** on commits — pre-commit hook failure is signal, not obstacle. Fix the root cause.
- **Every change to `yadgar/**` must update `README.md` and `docs/` in the same PR** when behaviour or interface changes.
- **Version bumps:** edit `pyproject.toml:version` for core, `server.json:backend_version` for backend. `check-versions` will fail the commit if the cross-file mirrors drift.
- Run before push: `ruff check . && ruff format --check . && pytest && pre-commit run --all-files`.
- PRs are opened against `codeberg.org/maxagahi/yadgar` via the Forgejo REST API (not `gh` — Codeberg is not GitHub). See `README.md § Contributing` for the curl recipe.
- Pre-commit will fail loudly on: large file (>2 MB), gitleaks hit, complexity over 15, missing metric writer, missing trace span, secret-gate drift, config three-way-sync drift, allowlist drift.

## Subagent contract

**Verify subagent claims before integrating.** File edits, contract flips, test assertions, and command output from a subagent are claims, not truth. Re-read the artifact (the actual file, `gh pr view --json body`, `aws describe-*`, etc.) before relaying the result as done. A passing-looking diff excerpt in a report is not a passing test.

If your agent dispatches subagents that may write memories, paste the contract from [`docs/CLAUDE_SUBAGENT_CONTRACT.md`](docs/CLAUDE_SUBAGENT_CONTRACT.md) into the global `~/.claude/CLAUDE.md`, then run `yadgar install_hooks --scope global`. The `SubagentStop` hook scans the final report for a `## Yadgar findings` section and persists each bullet as a memory tagged with the agent type. Opt-in — Yadgar works without it.

## Further reading

- [`README.md`](README.md) — human overview, features, benchmark, production scale, roadmap
- [`docs/architecture.md`](docs/architecture.md) — component map, branch-aware retrieval, security, observability
- [`docs/configuration.md`](docs/configuration.md) — every env var and config key
- [`docs/retrieval.md`](docs/retrieval.md) — 8-stage pipeline spec
- [`docs/memory-lifecycle.md`](docs/memory-lifecycle.md) — heat, decay, consolidation
- [`docs/HOOKS.md`](docs/HOOKS.md) — Claude Code hook contracts
- [`docs/RELEASE.md`](docs/RELEASE.md) — version bump → tag → nix
- [`docs/CLAUDE_SUBAGENT_CONTRACT.md`](docs/CLAUDE_SUBAGENT_CONTRACT.md) — `SubagentStop` protocol
- [`MIGRATION_NOTES.md`](MIGRATION_NOTES.md) — operator steps for breaking changes
