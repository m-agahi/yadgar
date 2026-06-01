# JavaScript / TypeScript SDK — @yadgar/sdk

Yadgar ships a first-party TypeScript client for its MCP endpoint: `@yadgar/sdk` (v0.1.0).

The SDK lives in [`sdk-js/`](../sdk-js/) — a self-contained Node/TypeScript package inside the
yadgar monorepo. It wraps all 53 MCP tools exposed by the yadgar server as typed async methods
on `YadgarClient`.

## Quick links

- **Package**: [`sdk-js/package.json`](../sdk-js/package.json) — `@yadgar/sdk` v0.1.0
- **README**: [`sdk-js/README.md`](../sdk-js/README.md) — install, quick start, tool reference
- **Changelog**: [`sdk-js/CHANGELOG.md`](../sdk-js/CHANGELOG.md) — SDK release notes (independent semver)
- **Plan**: [`docs/PLAN_V5_35_0_JS_SDK.md`](PLAN_V5_35_0_JS_SDK.md) — full design rationale + roadmap

## Why

Yadgar's 53-tool MCP surface was previously only accessible from Python / Claude Code.
The JS SDK enables:

- Node.js / Edge runtime callers (Vercel AI SDK, Cloudflare Workers, Deno)
- Browser viz pages calling MCP tools directly (no bespoke REST endpoint per feature)
- First-party typed client for JS agent frameworks (LangChain.js, Mastra — v0.3+ adapters)

## Compatibility

| `@yadgar/sdk` | yadgar server | Node.js |
|---|---|---|
| 0.1.x | >= 5.35.0 | >= 18.0.0 |

## Publication

v0.1.0 ships to GitHub Packages only. Public npm at v0.2.

Publish tag format: `sdk-js/v0.1.0` — distinct from core `v5.x.x` tags.

## Roadmap

- **v0.2**: SSE transport, `client.recall.iter()` async generator, retry helper, public npm
- **v0.3**: Vercel AI SDK adapter, LangChain.js Tool wrapper, Mastra integration
