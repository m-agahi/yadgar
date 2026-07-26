# AGENTS.md

Operational guide for AI coding agents working on Yadgar. Human-oriented context (features, benchmark, production scale, design rationale) lives in [`README.md`](README.md) — read it for the *why*; read this for the *how*.

## Project overview

Yadgar is a persistent memory engine for agentic coding clients, packaged as an MCP server. Python 3.14+. One shared streamable-HTTP daemon (`http://127.0.0.1:8765/mcp`) serves the memory and wiki MCP surface to all 9 supported clients: `claude-code`, `codex`, `gemini`, `cursor`, `cline`, `windsurf`, `kiro`, `amp`, `opencode`. Claude Code additionally receives the full harness integration (hooks, task-list mirror, CLAUDE.md sync); all other clients get MCP registration and a rules file. Two process tiers:

- **Core** (`openfantasy/yadgar`, this repo) — MCP + HTTP server, retrieval pipeline, consolidation, CLI. Default port `8765`.
- **Backend** (`openfantasy/yadgar-backend`, separate image) — SurrealDB + embed/rerank service. Default port `8001` (embed), `8000` (SurrealDB).

Versions are split: `pyproject.toml:version` drives the core image; `server.json:backend_version` drives the backend image. The pre-commit `check-versions` hook enforces consistency across `pyproject.toml`, `server.json`, `Dockerfile`, `Dockerfile.backend`, `docker-compose.yml`, `scripts/setup.sh`.

Entry point: `yadgar.__main__:cli` (console script `yadgar`). Run `yadgar --help` for the full subcommand tree.

## Setup commands

Pick one path. All three end up MCP-registered with your chosen client(s).

### Multi-client setup — `yadgar install`

After the daemon is running, use `yadgar install` to register any of the 9 supported clients:

```bash
yadgar install --client <name>           # register one client (e.g. --client opencode)
yadgar install --auto-detect             # detect + register all installed clients
yadgar install --client <name> --mcp     # MCP registration config only
yadgar install --client <name> --rules   # rules file only (AGENTS.md-equivalent)
yadgar install --client <name> --no-hooks  # skip the hooks surface (MCP + rules only)
yadgar install --client <name> --print   # dry-run: emit JSON to stdout, no file writes
yadgar install --client <name> --scope project --project-directory /path/to/repo
```

`--hooks` is the default for clients with a registered `hooks_kind` (claude-code, cursor, opencode, etc.); `--no-hooks` opts out. Advisory-only clients (Gemini, `hooks_kind=None`) are no-op for hooks regardless of the flag. `--print` is the nix/home-manager integration path: it outputs the full config JSON without touching the filesystem. Full flag reference: [`docs/reference/install.md`](docs/reference/install.md).

### Fast path — user install (pipx)

```bash
pipx install yadgar          # or: pip install yadgar
yadgar setup                 # writes ~/.config/yadgar/{config.yaml,secrets.env}
set -a && . ~/.config/yadgar/secrets.env && set +a
yadgar daemon start
# Claude Code (full harness):
yadgar daemon configure-mcp  # writes ~/.claude.json with bearer header
# Any other client:
yadgar install --client <name>
```

`configure-mcp` reads `$YADGAR_MCP_AUTH_TOKEN` from env at invocation time — if unset the header is omitted and `/mcp` returns 401.

**uv cache gotcha (fresh PyPI publish):** installing a just-published version back-to-back can hit uv's cached PyPI simple-index (600 s freshness window). uv won't auto-retry on version-not-found, producing `No solution: no version X` (see [uv#16281](https://github.com/astral-sh/uv/issues/16281)). Workaround: `UV_NO_CACHE=1 pipx install yadgar==<ver>` or `rm -rf ~/.cache/uv/simple-v* && pipx install yadgar==<ver>`.

### Dev path — repo checkout

```bash
git clone https://github.com/m-agahi/yadgar.git
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
| `YADGAR_MCP_AUTH_TOKEN` | `YADGAR_REQUIRE_AUTH=1` (v5 default) | `~/.config/yadgar/secrets.env` |
| `SURREAL_USER` / `SURREAL_PASS` | backend container starts | same |
| `YADGAR_DB_URL` / `YADGAR_EMBED_URL` | core container starts | defaults in `yadgar daemon`; explicit in docker-manual |

Full reference: [`docs/reference/configuration.md`](docs/reference/configuration.md).

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
- **I32 — capability registry (HARD).** `docs/contracts/CAPABILITY_REGISTRY.md` is the source of truth for every feature/algorithm/behaviour (wired or not). When you add or remove a **Settings field** (`config.py`), an **MCP `@_tool`**, a **`_migration_NNN`**, or a **`BC-*`** row, add/update its entry in the SAME change — `scripts/check_capability_coverage.py` (pre-commit + CI `invariant-checks`) fails on any uncatalogued or stale surface item. A green lint proves the catalogue is COMPLETE, not that each `status:` is accurate — verify status when you touch the subsystem.

## Running benchmarks (eval / LongMemEval)

Two harnesses, both NON-GATING (informational quality measurement, not merge gates):

- **`make eval`** — native golden set (`benchmarks/golden/golden_set.jsonl`), recall@k/MRR/nDCG. Fast. The golden set is an auto-drafted **bootstrap** (needs human curation) — treat numbers as indicative only.
- **`make longmemeval`** — **LongMemEval** (the rigorous external benchmark, MIT-licensed dataset in `benchmarks/data/longmemeval/`). 500 questions, multi-session haystacks + gold answers; **self-seeds a frozen corpus per question into an isolated SurrealDB** (decay-proof, reproducible). This is the PRIMARY retrieval-quality measure. Defaults to retrieval-only + a stratified subset; `Q=<n>` overrides (`Q=0` = all 500).

**Baseline to beat (v5.26.0, full 500):** overall recall@5 **0.87** · recall@10 **0.91** · MRR **0.93** · qa_accuracy **0.69**.

### Must-knows (learned the hard way — 2026-06-22)

- **Always pass `--unified`** to measure the live recall path (v5.80 fan-out + directory-scoping + MCP recall tool). Without it the runner calls `Retriever.recall()` directly = the LEGACY path. In `--unified` mode the runner calls `init_engines()` once to populate server `_state`; if you see `StorageEngine not initialized` on every question, that wiring regressed — fix it, don't ignore.
- **Retrieval-only vs full QA:** add nothing → retrieval metrics only (fast, free). Drop `--retrieval-only` and add `--model claude-sonnet-4-6` → reader+judge LLM round-trips per question (adds `qa_accuracy`). Full QA is ~60–180s/question → **full 500 ≈ 7–22h**. Uses the Claude Code subscription (`claude -p`), not API billing.
- **`--resume` + a stable `--output`** make it restartable: it skips questions already in `<output>_hypotheses.jsonl`. Re-run the exact same command to continue after any interruption.
- **NEVER pipe the run through `head`** (`... | head -50`) — `head` closes the pipe after N lines → SIGPIPE/BrokenPipeError kills the run. Redirect to a logfile instead: `> /tmp/lme.log 2>&1`.

### How to launch a LONG run (CRITICAL for AI agents)

For multi-hour runs (full QA), launch as a **single plain background process** — `Bash(run_in_background=true)` — redirected to a logfile. You get ONE completion notification; re-launch with `--resume` if it dies.

```bash
PYTHONUNBUFFERED=1 OTEL_SDK_DISABLED=true uv run --extra test --extra ml \
  python benchmarks/run_longmemeval.py --unified --variant s --model claude-sonnet-4-6 \
  --output benchmarks/reports/lme_full.json --resume > /tmp/lme.log 2>&1
```

- **Do NOT wrap a long run in a sub-Agent, and do NOT arm a per-question Monitor.** An Agent re-wakes on every monitor event and re-reads its full context (~50k tokens) PER QUESTION → ~tens of millions of tokens over a 500-q run. A `Bash(run_in_background)` task is the correct mechanism: it persists across turns and notifies once on exit.
- Watch progress cheaply by reading the logfile (`grep -c '\[.*/500\]' /tmp/lme.log`) or the hypotheses JSONL line count — NOT with a streaming per-line Monitor.
- The run self-spawns + tears down its own isolated SurrealDB on a free port. If a kill leaks one, `bash scripts/reap-test-surreal.sh` cleans stale `yadgar_bench_surreal_*`. Never kill the production `entrypoint-backend.sh` / its `sleep 21600`.

## Code style

- **Lint + format:** `ruff` (target `py314`, line length 100). Rules: `E, W, F, I, UP, B, C901, PLR0913`; ignores `E501, B008, UP007`. Max complexity 15, max args 8.
- Auto-fix: `ruff check --fix . && ruff format .`. Pre-commit runs this automatically.
- Per-file grandfathered exceptions for `C901` / `PLR0913` are listed in `pyproject.toml [tool.ruff.lint.per-file-ignores]` — do not add new ones; refactor instead.
- Type hints required on new public functions. `dict[str, Any]` over `Dict[str, Any]` (project targets 3.14+, no `from __future__ import annotations` needed).
- No shell wrappers around Python invocations. CLI work lives in `yadgar/cli/`; MCP tool handlers in `yadgar/server/tools/`.
- Logging: `logging.getLogger(__name__)`. Structured fields via `extra={...}`. JSON formatter active when `YADGAR_LOG_FORMAT=json`.

## Architecture map

Three-layer split (ADR-0056/0060/0062/0063; import-linter enforced). Each layer root has its own `README.md` and `AGENTS.md`.

**yadgar/core** — MCP server (thin router only, no compute):

| Path | Purpose |
|---|---|
| `yadgar/__main__.py` | CLI entry point + Click command tree |
| `yadgar/core/cli/` | Subcommand implementations (daemon, vacuum, seed, config, rules, viz, setup, install-hooks) |
| `yadgar/core/server/` | FastAPI app, MCP tool handlers, auth middleware, transport |
| `yadgar/core/hooks/` | Claude Code hook runner scripts (SessionStart, SubagentStop, PreCompact) |
| `yadgar/core/daemon/` | systemd-style daemon start/stop/status, MCP transport switching |
| `yadgar/core/repo_wiki/` | `repo_wiki_generate` — AST-scan Python repo → wiki store |
| `yadgar/core/export/` | DuckDB exporter for offline analytics |
| `yadgar/core/viz/` | Viz server entry point (reverse-proxies `/api/*` to backend at :8765) |

**yadgar/_shared** — contracts + config + observability (imported by both core and backend):

| Path | Purpose |
|---|---|
| `yadgar/_shared/config/` | Pydantic settings; `FIELD_META` registry; three-way-sync |
| `yadgar/_shared/storage/` | SurrealDB client + schema contracts |
| `yadgar/_shared/observability/` | `@observe` decorator (span+metric+log), tracing, metrics |
| `yadgar/_shared/security/` | Secret-gate patterns, allowlist |
| `yadgar/_shared/file_queue/` | Async write queue client + DLQ |
| `yadgar/_shared/rules_engine/` | Write-block and write-allow rules evaluation |
| `yadgar/_shared/knowledge_graph/` | Entity extraction, relationship edges |

**yadgar/backend** — all compute (reached only over HTTP boundary):

| Path | Purpose |
|---|---|
| `yadgar/backend/retrieval/` | Full recall pipeline: FTS+KNN+PPR+spreading → WRRF → CE rerank → NLI → MMR → adversarial → rules |
| `yadgar/backend/consolidation/` | Nightly cycle: decay, CLS, merge, link, causal discovery |
| `yadgar/backend/embed_service/` | Sentence-transformer embed endpoint (:8001); CE reranker (Ettin-32m) |
| `yadgar/backend/queue_drainer/` | Async drainer: dequeues file queue, runs similarity gate, commits to SurrealDB |
| `yadgar/backend/graph/` | Galaxy layout precompute (networkx spring_layout); `/api/graph` endpoint |
| `yadgar/backend/curation/` | Duplicate detection, merge, `_memify_prune` |
| `yadgar/backend/cache/` | Unified cache (N named instances, ScopeVersions invalidation) |

**Shared:**

| Path | Purpose |
|---|---|
| `yadgar/static/` | Viz UI + bookmarks UI (Three.js galaxy renderer) |
| `yadgar/tests/` | pytest suite (mirrors three-layer structure: `tests/core/`, `tests/_shared/`, `tests/backend/`) |
| `scripts/` | Pre-commit invariant checks + version sync |
| `docs/` | Architecture, retrieval, configuration, hooks, benchmark, roadmap |

~79 MCP tools across memory, wiki, bookmarks, project, ops, ADR, agent-prompts. Full table in `README.md § Tools`.

### Where does new code go?

Decide layer in 20 seconds — ask, in order:

- **`core/`** — iff backend never needs it **and** the MCP-host / router /
  lifecycle-supervisor does. *Test:* stateless transport + host-side control to
  start/stop/restart/vacuum-swap the backend container.
- **`backend/`** — iff core never imports it (reached only over the HTTP
  boundary `/recall`+`/rerank` or a `Protocol`). *Test:* stateful compute — DB +
  engines (embedding, retrieval, write-apply/drainer, consolidation).
- **`_shared/`** — otherwise (both processes need it). *Test:* contracts
  (protocols, config, models, paths, observability, tracing) + engines both
  processes run. Tie-break vs `backend/` is runtime *usage*, not imports.

**Forward-only:** refactor trains rip-and-replace — no backward-compat
knobs/flags/dual-paths/re-export shims. Intermediate train states need only be
CI-green, not runnable.

Rules + enforcement (import-linter contracts, DI waivers): `docs/contracts/ARCHITECTURE_INVARIANTS.md` §I34 + `wiki:yadgar-adr-log` ADR-0062.

## Operations cheatsheet

```bash
yadgar daemon status                  # http mode health
yadgar stats [--project /path]        # memory + wiki counts
yadgar vacuum                         # SurrealKV compaction (manual)
yadgar viz                            # graph UI at http://localhost:42069
yadgar export duckdb --output snap.duckdb   # analytics snapshot (needs [analytics])
yadgar seed <directory>               # bootstrap memory from README + docs
yadgar rules add|export|import        # retrieval / write policy rules
yadgar config init|list|get|set|edit  # ~/.config/yadgar/config.yaml
yadgar install --client <name> [--hooks | --no-hooks] [--scope ...]   # wires MCP + rules + hooks (default-on hooks for claude-code / cursor / opencode)
yadgar install-hooks --scope global   # legacy: Claude Code hooks only — superseded by `yadgar install --client claude-code --hooks`
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
- **Never log secrets.** Always-on secret patterns block AWS, GCP, Stripe, Slack, OpenAI, Anthropic keys, JWT, GitHub PATs, private keys, DB URIs at write time — adding an exception requires the context-aware allowlist (`~/.config/yadgar/secret-gate-allowlist.yaml`) plus the `check-secret-gate` pre-commit gate.
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
- PRs are opened against `github.com/m-agahi/yadgar` with the GitHub CLI (`gh pr create --repo m-agahi/yadgar`).
- Pre-commit will fail loudly on: large file (>2 MB), gitleaks hit, complexity over 15, missing metric writer, missing trace span, secret-gate drift, config three-way-sync drift, allowlist drift.

## Subagent contract

**Verify subagent claims before integrating.** File edits, contract flips, test assertions, and command output from a subagent are claims, not truth. Re-read the artifact (the actual file, `gh pr view --json body`, `aws describe-*`, etc.) before relaying the result as done. A passing-looking diff excerpt in a report is not a passing test.

If your agent dispatches subagents that may write memories, paste the contract from [`docs/reference/claude-subagent-contract.md`](docs/reference/claude-subagent-contract.md) into the global `~/.claude/CLAUDE.md`, then run `yadgar install-hooks --scope global`. The `SubagentStop` hook scans the final report for a `## Yadgar findings` section and persists each bullet as a memory tagged with the agent type. Opt-in — Yadgar works without it.

## Further reading

- [`README.md`](README.md) — human overview, features, benchmark, production scale, roadmap
- [`docs/reference/architecture.md`](docs/reference/architecture.md) — component map, branch-aware retrieval, security, observability
- [`docs/contracts/CAPABILITY_REGISTRY.md`](docs/contracts/CAPABILITY_REGISTRY.md) — source of truth: every feature/algorithm/behaviour (wired or not) + status (I32-enforced)
- [`docs/reference/configuration.md`](docs/reference/configuration.md) — every env var and config key
- [`docs/reference/retrieval.md`](docs/reference/retrieval.md) — 8-stage pipeline spec
- [`docs/reference/memory-lifecycle.md`](docs/reference/memory-lifecycle.md) — heat, decay, consolidation
- [`docs/reference/hooks.md`](docs/reference/hooks.md) — Claude Code hook contracts
- [`docs/reference/release.md`](docs/reference/release.md) — version bump → tag → nix
- [`docs/reference/claude-subagent-contract.md`](docs/reference/claude-subagent-contract.md) — `SubagentStop` protocol
- [`MIGRATION_NOTES.md`](MIGRATION_NOTES.md) — operator steps for breaking changes
