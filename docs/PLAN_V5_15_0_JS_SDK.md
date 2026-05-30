# PLAN — v5.15.0 / `@yadgar/sdk` v0.1.0: JavaScript / TypeScript SDK

**Status:** drafted 2026-05-30. Plan-first per I27. Greenfield — scan agent verdict NEVER-CONSIDERED. No prior commits, plans, or files touch a JS SDK. Implementation deferred.

**Audit source:** `docs/competitor-audit-2026-05-30.md` — "Adopt #5: JavaScript SDK (from mem0) — Medium impact, medium effort. ... A JavaScript/TypeScript SDK client for Yadgar's MCP endpoint would unlock integration with web-based agent frameworks (Vercel AI SDK, etc.) without any server-side changes. Estimated 1-2 weeks to build."

**Master at draft time:** core v5.10.3 shipped. v5.10.4 in flight on `feat/v5.10.4-consolidate-now-mode-hook-schema`.

**Sequencing:** v5.15.0 slot — server-side support release (CORS knobs already shipped — `YADGAR_ALLOWED_ORIGINS` in `yadgar/server/_app.py:60`). Slot is light on the server side; bulk of the work lives in a new `sdk-js/` subdirectory that ships its own package version `@yadgar/sdk` v0.1.0. Lands after v5.13.0 (Adopt-1 benchmarks — `docs/PLAN_V5_13_0_BENCHMARK_PUBLICATION.md`) and v5.14.x (R2 recall plugin arch). Independent of v5.11 (anchor cross-project) and v5.12.0 (wiki bookmarks).

---

## Why

Yadgar is currently Python-only on the server, transport is FastMCP-over-HTTP/SSE (`yadgar/server/_app.py:52`). The MCP protocol itself is transport-agnostic JSON-RPC 2.0 — anything that speaks HTTP can call yadgar. Today the only documented client is Claude Code's built-in MCP integration (Python). Three concrete consequences:

1. **No web framework integration possible.** Vercel AI SDK, LangChain.js, Mastra, agentic-js — all run in Node/Edge runtimes. No first-party way to plug yadgar in as a memory backend.
2. **Browser viz cannot call MCP tools directly.** The existing `/static/viz` page renders graph data via custom `/api/*` REST routes (`yadgar/server/http.py`) that bypass MCP. Every new viz feature requires adding a custom route. With a JS SDK, viz can call MCP tools (e.g. `wiki_read`, `recall`) from the browser — no bespoke REST endpoint per feature.
3. **Audit competitive gap.** mem0 ships JS + Python; yadgar's "32 MCP tools, mem0 has ~4" advantage is hollow if half the JS ecosystem can't reach those tools without writing raw `fetch()` calls.

A typed JS/TS client unblocks all three at once. It's pure additive — zero server changes (transport already exists), zero risk to existing Python consumers, can ship behind a `0.x` semver bar that signals "API still settling".

---

## Goals

1. **Typed thin client** for all 32 MCP tools currently exposed by yadgar (see `yadgar/server/tools/` for the inventory).
2. **Single transport: streamable HTTP** (FastMCP's `streamable_http_app`). SSE support deferred to v0.2 — it's nice for streaming but adds connection-management complexity not needed for the request/response use case that covers 95% of tools.
3. **Auth:** bearer token via `Authorization: Bearer <token>` header. Matches existing FastMCP middleware (`_app.py:189` — `_auth_wrapped_sse_app`).
4. **Type generation:** auto-generated input/output types from MCP `tools/list` introspection at SDK build time. Hand-written wrapper layer on top for ergonomics (e.g. `client.recall("query")` instead of `client.callTool("recall", {query: "..."})`).
5. **Zero runtime dependencies** beyond `@modelcontextprotocol/sdk` (official MCP TS SDK). No axios, no node-fetch — use platform `fetch`. Keeps install size small + edge-runtime compatible.
6. **Dual ESM+CJS+types** package output. ESM-first; CJS for Node 18 holdouts. `.d.ts` shipped.
7. **Test coverage:** every tool wrapper has a unit test against a mock MCP server. Integration tests run against a running local yadgar instance (opt-in, gated by `YADGAR_INTEGRATION_TEST=1`).
8. **Edge-runtime compatible.** Verified to import and run in Vercel Edge, Cloudflare Workers, Deno. No `node:*` imports outside of optional helpers.

---

## Non-goals (v0.1.0)

- **Convenience helpers** beyond 1:1 tool wrappers (no `client.memorize.batch([...])`, no `client.recall.iter(query)`). Defer to v0.2.
- **Retry/backoff/circuit-breaker.** Caller wraps with `p-retry` or similar. SDK stays mechanical.
- **Framework adapters.** No Vercel AI SDK adapter, no LangChain Tool wrapper, no Mastra integration. These are v0.3+ targets — design once we see how consumers actually want to use the SDK.
- **SSE streaming transport.** Defer to v0.2.
- **Browser-side OAuth flow / token refresh.** v0.1 assumes static bearer token from env or config. Token-acquisition flows are caller's problem.
- **Code generation CLI** (`@yadgar/sdk-gen` or similar). Codegen runs only at SDK build time; not distributed as a tool consumers run themselves.
- **MCP server features beyond tools.** Resources (`resources/list`, `resources/read`) and prompts (`prompts/list`) not wrapped in v0.1 — yadgar doesn't currently expose any. Add when server adds them.

---

## Scope decision: A vs B vs C

| Option | Effort | Risk | When to pick |
|---|---|---|---|
| **A** Thin client (32 tools as typed functions + auth) | 3-5 days | Low | Ship something measurable; iterate based on consumer feedback. |
| **B** Higher-level SDK (batch helpers, iterators, retries, per-tool semantic types) | 1-2 weeks | Medium — committing to an API shape before consumers exist | Wait for v0.2 once we know consumer pain. |
| **C** Framework adapters (Vercel AI SDK, LangChain.js, Mastra) | 2-3 weeks | High — three integrations, three moving targets | Wait for v0.3 once thin client is battle-tested. |

**Chosen: A.** Reasoning:
- Zero JS code exists today — committing to a high-level API before any consumer feedback risks designing the wrong abstractions.
- The thin client is also the ENTIRE foundation B and C build on. Ship A, then layer B and C on top in subsequent minor releases.
- Audit's 1-2 week estimate is for B (the "higher-level"); A is 3-5 days, fits in a single focused work block.
- A also serves as the de-facto type schema for the MCP surface. Once it exists, generating bindings for other languages (Go, Rust) becomes "translate the codegen output" rather than "introspect MCP from scratch".

---

## Architecture

### Repo layout (monorepo subdir, NOT separate repo)

```
yadgar/                              # existing repo root
├── sdk-js/                          # NEW — JS/TS SDK package root
│   ├── package.json                 # @yadgar/sdk, independent semver
│   ├── tsconfig.json
│   ├── tsup.config.ts               # ESM + CJS + .d.ts build
│   ├── vitest.config.ts
│   ├── README.md                    # consumer-facing; quick start + tool reference
│   ├── CHANGELOG.md                 # SDK-specific; SDK semver, NOT yadgar core
│   ├── src/
│   │   ├── index.ts                 # public exports — YadgarClient, types
│   │   ├── client.ts                # YadgarClient class — transport + auth
│   │   ├── transport.ts             # streamable HTTP transport thin wrap
│   │   ├── errors.ts                # YadgarError hierarchy
│   │   ├── auth.ts                  # BearerAuth helper
│   │   ├── generated/               # codegen output — do NOT hand-edit
│   │   │   ├── tools.ts             # 32 tool function wrappers
│   │   │   └── types.ts             # input/output type definitions
│   │   └── helpers/                 # hand-written ergonomic wrappers
│   │       └── (empty in v0.1)
│   ├── tests/
│   │   ├── unit/
│   │   │   ├── client.test.ts
│   │   │   ├── transport.test.ts
│   │   │   ├── auth.test.ts
│   │   │   └── tools.test.ts        # mock-server table-driven test per tool
│   │   ├── integration/
│   │   │   └── live-server.test.ts  # opt-in via YADGAR_INTEGRATION_TEST=1
│   │   └── fixtures/
│   │       └── mock-mcp-server.ts
│   ├── scripts/
│   │   ├── generate-types.ts        # introspect MCP tools/list → emit src/generated/
│   │   └── verify-tool-coverage.ts  # CI gate — all 32 tools wrapped
│   └── .npmignore
├── docs/
│   └── sdk-js.md                    # NEW — pointer doc from main yadgar docs
└── (rest of yadgar Python repo unchanged)
```

**Why monorepo subdir, not separate repo:**

| Concern | Subdir wins | Separate repo wins |
|---|---|---|
| MCP tool additions sync with SDK | YES — same PR adds tool + regenerates types | Two PRs across two repos, easy to drift |
| CI simplicity | One CI, conditional `paths: [sdk-js/**]` | Two CIs, two release pipelines |
| Issue triage | One issue tracker | Split issues |
| npm publish discoverability | Same (package name, not repo URL, is what matters on npm) | Same |
| Visibility for JS users | Lower (Python repo, intimidating) | Higher (clean JS-only repo) |
| Versioning independence | Achieved via separate `sdk-js/package.json` semver — no coupling forced | Native |
| Branch hygiene | Subdir changes scoped via `paths:` in CI; no force-push pressure | Native |
| Release tagging | Tag prefix `sdk-js/v0.1.0` distinct from core `v5.13.0` | Native |

Subdir wins on every operational axis except "JS-only discoverability" — which the README + a `docs/sdk-js.md` pointer + clear npm package name solve. Audit explicitly noted yadgar's 32-tool MCP advantage; keeping the SDK close to those tools is the point.

### Type generation strategy: hybrid

**Auto-generated** (from MCP `tools/list` schemas):
- One function per tool, named after the tool (`memorize`, `recall`, `wiki_add`, ...).
- Input parameter type derived from each tool's JSON schema.
- Output type derived from tool's return-content schema (if declared) or `unknown` (if not).
- Generated file `src/generated/tools.ts` regenerated by `scripts/generate-types.ts` against a running yadgar instance OR a checked-in `tools-snapshot.json` for offline build.

**Hand-written** (`src/client.ts`, `src/transport.ts`):
- Transport layer wrapping `@modelcontextprotocol/sdk`'s `Client` + `StreamableHTTPClientTransport`.
- Auth header injection.
- Error mapping (JSON-RPC error → `YadgarError` subclass).
- Public `YadgarClient` class exposing all 32 tool functions as methods, bound to the transport.

**Why hybrid, not pure-generated:**
- Pure-generated types from JSON schema are uglier (e.g. union types with `null` everywhere, no method docstrings). Hand-written wrapper layer fixes naming, adds JSDoc, hides transport plumbing.
- Pure-hand-written drifts the moment a tool's schema changes server-side. Codegen catches drift at SDK build time.
- Hybrid: schemas are source of truth, ergonomics are curated.

### Versioning policy

| Component | Versioning |
|---|---|
| `@yadgar/sdk` npm package | Independent semver. v0.x while API settling. v1.0 only after stable API + 2+ consumers using it. |
| yadgar core (Python) | Existing semver, unchanged. v5.13.0 slot for the server-side support release. |
| Compatibility matrix | Maintained in `sdk-js/README.md`. e.g. `@yadgar/sdk v0.1.x` supports `yadgar >= v5.13.0`. |
| Breaking server changes | Server bumps minor; SDK bumps patch with new generated types in same PR. SDK minor bump only on SDK-side breaking changes (e.g. transport switch). |

### npm publication decision

**v0.1.0: GitHub Packages + GitHub Release tarball ONLY. No public npm.**

Reasoning:
- Public npm publish commits to a name + an ownership story (org account `@yadgar` on npm). Once published, name is permanently claimed.
- v0.1 will have rough edges — published to public npm, those rough edges get cargo-culted into 500 projects' `package.json` before we can fix them.
- GitHub Packages + tarball is friction-y enough that early adopters are intentional. Each one is a feedback channel, not a silent install.
- Move to public npm at v0.2 once API has stabilized through 2-3 patch releases of real consumer feedback.

**Open question for owner:** does max own `@yadgar` on npm today? If not, register before v0.2.

### Test harness

- **Vitest** for unit + integration tests.
  - Reasoning vs Jest: Vitest is ESM-native (no `ts-jest` / `babel-jest` config dance), faster cold start, identical API for migration if needed.
- Unit tests: mock MCP server (`tests/fixtures/mock-mcp-server.ts`) — implements `tools/list`, `tools/call`, returns canned responses. Each of 32 tool wrappers gets one happy-path test + one error test. ~64 tests total.
- Integration tests: gated by `YADGAR_INTEGRATION_TEST=1` env var. Calls live local yadgar (`http://127.0.0.1:42069` or whatever `YADGAR_TEST_URL` says). Runs the read-only tool subset (`wiki_list`, `wiki_read`, `recall`, `memory_stats`, `get_rules`) — no writes against developer's real DB.
- Coverage gate: `scripts/verify-tool-coverage.ts` parses the generated `tools.ts`, cross-references against `tools/list` snapshot, fails CI if any MCP tool lacks a wrapper. Prevents the SDK silently going stale when server adds tools.

### Bundler: tsup

- Zero-config ESM + CJS + `.d.ts` output via esbuild + dts plugin.
- One config file (`tsup.config.ts`), no `rollup.config.js` / `tsconfig.build.json` proliferation.
- Faster than rollup, simpler than esbuild-direct.
- Alternatives considered:
  - **vite-build** — overkill (vite is a dev server too; we don't need that).
  - **esbuild direct** — no `.d.ts` generation, would need separate `tsc --emitDeclarationOnly` step.
  - **tsc only** — no bundling, no minification, package bigger than it needs to be.

---

## CI / release pipeline

New GitHub Actions workflow `.github/workflows/sdk-js.yml`:

```yaml
on:
  push:
    paths: ['sdk-js/**']
  pull_request:
    paths: ['sdk-js/**']

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'pnpm'
      - run: corepack enable
      - run: pnpm install --frozen-lockfile
        working-directory: sdk-js
      - run: pnpm run build
        working-directory: sdk-js
      - run: pnpm run test
        working-directory: sdk-js
      - run: pnpm run lint
        working-directory: sdk-js
      - run: pnpm run typecheck
        working-directory: sdk-js
      - run: pnpm run verify-tool-coverage
        working-directory: sdk-js

  publish:
    if: startsWith(github.ref, 'refs/tags/sdk-js/v')
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          registry-url: 'https://npm.pkg.github.com'
      - run: corepack enable
      - run: pnpm install --frozen-lockfile
        working-directory: sdk-js
      - run: pnpm run build
        working-directory: sdk-js
      - run: pnpm publish --no-git-checks
        working-directory: sdk-js
        env:
          NODE_AUTH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Release tag format: `sdk-js/v0.1.0` — keeps SDK tags out of the core `v5.x.x` tag namespace, easy to filter (`git tag -l 'sdk-js/*'`).

---

## File list (concrete deliverables)

| Path | Created | Purpose | Approx LOC |
|---|---|---|---|
| `sdk-js/package.json` | NEW | Package manifest | 40 |
| `sdk-js/tsconfig.json` | NEW | TS compiler config | 30 |
| `sdk-js/tsup.config.ts` | NEW | Build config | 20 |
| `sdk-js/vitest.config.ts` | NEW | Test runner config | 15 |
| `sdk-js/README.md` | NEW | Consumer docs | 200 |
| `sdk-js/CHANGELOG.md` | NEW | SDK release notes | 20 |
| `sdk-js/.npmignore` | NEW | Exclude tests + scripts from publish | 10 |
| `sdk-js/src/index.ts` | NEW | Public exports | 20 |
| `sdk-js/src/client.ts` | NEW | YadgarClient class | 150 |
| `sdk-js/src/transport.ts` | NEW | HTTP transport wrap | 80 |
| `sdk-js/src/errors.ts` | NEW | Error hierarchy | 50 |
| `sdk-js/src/auth.ts` | NEW | Bearer auth helper | 30 |
| `sdk-js/src/generated/tools.ts` | NEW (codegen) | 32 tool wrappers | ~600 |
| `sdk-js/src/generated/types.ts` | NEW (codegen) | Input/output types | ~400 |
| `sdk-js/tests/unit/*.test.ts` | NEW | ~64 unit tests | ~800 |
| `sdk-js/tests/fixtures/mock-mcp-server.ts` | NEW | In-process mock server | ~150 |
| `sdk-js/scripts/generate-types.ts` | NEW | Codegen script | ~200 |
| `sdk-js/scripts/verify-tool-coverage.ts` | NEW | CI gate | ~50 |
| `.github/workflows/sdk-js.yml` | NEW | CI pipeline | 60 |
| `docs/sdk-js.md` | NEW | Pointer doc from main repo | 50 |
| `README.md` (yadgar root) | MODIFIED | Add 3-line "JS SDK available" section | +5 |

**Total new code:** ~2,900 LOC (generated + hand-written + tests). Hand-written ≈ 900 LOC; rest is generated or tests.

---

## `package.json` draft

```json
{
  "name": "@yadgar/sdk",
  "version": "0.1.0",
  "description": "TypeScript client for Yadgar — persistent memory engine for Claude Code (MCP).",
  "license": "Apache-2.0",
  "repository": {
    "type": "git",
    "url": "https://github.com/m-agahi/yadgar.git",
    "directory": "sdk-js"
  },
  "publishConfig": {
    "registry": "https://npm.pkg.github.com",
    "access": "restricted"
  },
  "type": "module",
  "exports": {
    ".": {
      "types": "./dist/index.d.ts",
      "import": "./dist/index.js",
      "require": "./dist/index.cjs"
    }
  },
  "files": ["dist", "README.md", "CHANGELOG.md"],
  "scripts": {
    "build": "tsup",
    "test": "vitest run",
    "test:watch": "vitest",
    "test:integration": "YADGAR_INTEGRATION_TEST=1 vitest run tests/integration",
    "lint": "eslint src tests scripts",
    "typecheck": "tsc --noEmit",
    "generate": "tsx scripts/generate-types.ts",
    "verify-tool-coverage": "tsx scripts/verify-tool-coverage.ts",
    "prepublishOnly": "pnpm run build && pnpm run test && pnpm run verify-tool-coverage"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0.0"
  },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "eslint": "^9.0.0",
    "tsup": "^8.0.0",
    "tsx": "^4.0.0",
    "typescript": "^5.4.0",
    "vitest": "^2.0.0"
  },
  "engines": {
    "node": ">=18.0.0"
  },
  "keywords": ["mcp", "yadgar", "memory", "claude", "ai", "sdk", "typescript"]
}
```

---

## README outline (`sdk-js/README.md`)

1. **What is this?** — 2 sentences. "TypeScript client for yadgar MCP. Lets JS/TS apps store, recall, and curate memories + wiki pages programmatically."
2. **Install** — `pnpm add @yadgar/sdk` (from GitHub Packages; npm public publish in v0.2).
3. **Quick start** — 10-line example:
   ```ts
   import { YadgarClient } from "@yadgar/sdk";
   const c = new YadgarClient({ url: "http://127.0.0.1:42069", token: process.env.YADGAR_TOKEN });
   await c.memorize({ content: "yadgar uses HNSW for vector index", tags: ["yadgar", "storage"] });
   const hits = await c.recall({ query: "what vector index does yadgar use?", max_results: 3 });
   console.log(hits);
   ```
4. **Authentication** — bearer token; pointer to yadgar server `YADGAR_BEARER_TOKEN` env.
5. **Tool reference** — table of all 32 tools with one-line description, link to yadgar tool docs.
6. **Examples** — 3-5 short scripts: memorize+recall, wiki query, project brief load, anchor pin, checkpoint+restore.
7. **Edge runtime support** — Vercel Edge, Cloudflare Workers, Deno — minimal config notes.
8. **Versioning + compatibility matrix** — table.
9. **Roadmap** — link to `docs/PLAN_V5_13_0_JS_SDK.md` + planned v0.2 (SSE + framework adapters).
10. **Contributing** — how to regenerate types, add a tool, run tests.
11. **License** — Apache-2.0.

---

## Effort estimate

| Stage | Estimate | Includes |
|---|---|---|
| Scaffolding + tooling | 0.5 day | `package.json`, tsconfig, tsup, vitest, CI workflow |
| Transport + auth + errors | 0.5 day | `client.ts`, `transport.ts`, `auth.ts`, `errors.ts` |
| Codegen script | 1 day | Parse MCP `tools/list`, emit ESM with JSDoc, snapshot mode |
| Mock MCP server fixture | 0.5 day | In-process; speaks streamable HTTP |
| Unit tests (32 tools × 2) | 1 day | Table-driven generation; per-tool happy + error |
| Integration test harness | 0.5 day | Opt-in env gate; read-only tool subset |
| README + examples | 0.5 day | Tool reference table, 5 example scripts |
| **TOTAL** | **4.5 days** | Solo effort, focused block |

Buffer 1.5 days for unknowns (FastMCP transport quirks, codegen edge cases) → call it **1 week / 5-6 days end-to-end** of focused work for v0.1.0.

Audit's "1-2 weeks" estimate matches when you include the v0.2 lift (SSE + first framework adapter). v0.1 alone is 1 week.

---

## Implementation phases

**Phase 1 — Foundation (Day 1):** Scaffolding, CI, transport, auth, errors. Result: empty `YadgarClient` that can call `tools/list` and `tools/call` against a live yadgar. Manual smoke test.

**Phase 2 — Codegen (Day 2):** Codegen script + snapshot. Result: `src/generated/tools.ts` with all 32 tool wrappers. Each callable but untested.

**Phase 3 — Test coverage (Days 3-4):** Mock MCP server. 64 unit tests. Coverage gate script. CI green.

**Phase 4 — Docs + integration (Day 5):** README, 5 example scripts, integration test against local yadgar, compatibility matrix, first tagged release `sdk-js/v0.1.0` to GitHub Packages.

**Phase 5 (out of scope for v0.1):** v0.2 spec — SSE transport, `client.recall.iter()` async generator, retry helper, Vercel AI SDK adapter prototype.

---

## Risks

1. **FastMCP transport quirks.** FastMCP's streamable HTTP implementation has had a few bugs (see `yadgar/server/_app.py:91-100` comment about Bug 4 residual fix). The official `@modelcontextprotocol/sdk` TS client may or may not handshake cleanly. **Mitigation:** Phase 1 manual smoke test against live yadgar BEFORE writing codegen. If handshake fails, the issue is exposed early; either patch the TS transport or upstream a fix to FastMCP.
2. **Tool schema completeness.** Some yadgar tools have minimal JSON schema (just param names, no return types). Generated types will be `unknown` for those returns. **Mitigation:** caller can `as` cast; v0.2 hand-curates the highest-traffic tools. Document gaps in README.
3. **Bearer token UX.** The audit didn't surface this, but: yadgar's bearer auth is wired through env var `YADGAR_BEARER_TOKEN` on the server. Consumer SDK callers need a way to discover this token. **Mitigation:** README documents the env-var pattern; v0.2 could ship a `getTokenFromConfig()` helper that reads `~/.yadgar/token`.
4. **npm package name squatting.** If `@yadgar` is already taken on public npm by someone else, name has to change. **Mitigation:** **OPEN QUESTION below.** Check `npm view @yadgar/sdk` before v0.2 public publish; if taken, fall back to `yadgar-sdk` (unscoped) or `@m-agahi/yadgar-sdk` (max's personal scope).
5. **Generated code churn in git.** `src/generated/` regenerates on every codegen run; even with deterministic output, sub-millisecond timestamp differences in tool order could cause noise. **Mitigation:** codegen output is sorted by tool name; emits deterministic. CI runs `git diff --exit-code` on `src/generated/` after running codegen — fails if drifted.
6. **No automated end-to-end test against real yadgar in CI.** Integration tests are opt-in. CI runs only unit tests against mock. **Mitigation:** acceptable for v0.1 (we don't want CI to depend on a running yadgar). Nightly cron job could add this in v0.2.

---

## Open questions (to resolve before implementation)

1. **npm org ownership.** Does max own `@yadgar` on npm? If not, register or pick alternative scope (`@m-agahi/yadgar-sdk`)?
2. **License.** SDK Apache-2.0 to match yadgar core, or MIT for broader JS-ecosystem norm? Apache-2.0 is safer for IP; MIT is more typical in JS land. Recommend Apache-2.0.
3. **GitHub Packages auth.** Consumers installing from GH Packages need a GitHub PAT in `~/.npmrc`. Acceptable friction for v0.1 (intentional, gates feedback to engaged users)? Or push for public npm at v0.1?
4. **Tool snapshot freshness.** Codegen runs against snapshot file checked into git OR against live yadgar at build time? Snapshot is reproducible (good for CI), but ages out. Recommend snapshot + a `pnpm regenerate` script that hits live yadgar.
5. **Server-side changes needed?** Need to confirm `YADGAR_ALLOWED_ORIGINS` covers the use case for browser callers (e.g. Vercel preview URLs). Likely fine but worth a 5-min check.
6. **Compatibility with `YADGAR_PROFILE=minimal`.** Server can expose only 10 core tools when `YADGAR_PROFILE=minimal`. SDK codegen needs to handle "missing tool" gracefully — generate stubs that throw clear error, or conditionally export?
7. **Release cadence.** SDK releases on its own cadence (independent of yadgar core) — but who decides when to cut? Recommend: cut SDK release when (a) new yadgar MCP tool ships, or (b) consumer-reported bug fix lands, or (c) at most quarterly even with no changes (keeps `npm audit` clean on transitive deps).

---

## Revisit triggers (for future audits)

- A consumer ships production code using `@yadgar/sdk` → graduate to v1.0, public npm.
- 5+ MCP tools added to yadgar without SDK update → codegen pipeline broken, fix immediately.
- A framework adapter request lands (Vercel AI SDK / LangChain.js / Mastra) → trigger v0.3 planning.
- mem0 or competitor ships a yadgar adapter for their SDK before we ship ours → reconsider Option C priority.
- Browser viz needs MCP tool call from frontend code → SDK becomes load-bearing, prioritize stability.

---

## Acceptance criteria (for v0.1.0 release)

- [ ] All 32 MCP tools have generated wrapper functions.
- [ ] All 32 wrappers have at least one happy-path unit test against mock server.
- [ ] Integration tests pass against a locally-running yadgar (read-only tool subset).
- [ ] `pnpm run build` produces `dist/index.js` (ESM), `dist/index.cjs` (CJS), `dist/index.d.ts` (types).
- [ ] Package installs and imports cleanly in Node 20, Node 18 (CJS path), Deno, Cloudflare Workers (manual smoke test, not CI).
- [ ] README has working quick-start example that runs against local yadgar.
- [ ] CI workflow green on PR.
- [ ] Tagged `sdk-js/v0.1.0`; published to GitHub Packages.
- [ ] Compatibility matrix documented in README.
- [ ] `docs/sdk-js.md` pointer doc added; main yadgar README mentions SDK existence in 3 lines.

---

## Why this matters strategically (TLDR)

mem0's 21 framework integrations and JavaScript SDK are the #1 reason it ranks higher than yadgar in casual comparison threads — even though yadgar's per-tool capability surface is larger. Without a JS SDK, every "I want to use yadgar in my Next.js / Mastra / Cloudflare Worker" thread ends with "you'd have to write your own MCP client." That's a moat-eroder.

A typed thin client closes the gap with a week of focused work, no server changes, and zero risk to existing Python consumers. It also unlocks the browser viz path (call MCP from frontend instead of bespoke REST routes per feature), which compounds into v5.12.0 (wiki bookmarks) and any future viz expansion. Worth doing; worth doing first among the Option-A/B/C tiers.
