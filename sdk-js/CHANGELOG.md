# Changelog — @yadgar/sdk

All notable changes to the JavaScript/TypeScript SDK are documented here.
This file covers SDK semver independently of yadgar core Python versioning.

## [0.1.0] — 2026-06-01

### Added
- Initial release of `@yadgar/sdk` v0.1.0.
- `YadgarClient` class: typed thin client for all MCP tools exposed by yadgar.
- Streamable HTTP transport via `@modelcontextprotocol/sdk` `StreamableHTTPClientTransport`.
- Bearer token authentication via `Authorization: Bearer <token>` header.
- Generated TypeScript types + wrappers for all yadgar MCP tools.
- Unit tests (vitest) with in-process mock MCP server.
- CI workflow (`.github/workflows/sdk-js.yml`) gated on `sdk-js/**` path changes.
- Tool coverage verification script (`scripts/verify-tool-coverage.ts`).

### Compatibility
- Requires yadgar server >= v5.35.0.
- Node.js >= 18.0.0.
- Edge runtimes: Vercel Edge, Cloudflare Workers, Deno (no `node:*` imports in core paths).

### Notes
- v0.1.0 ships to GitHub Packages only (not public npm). See README for install instructions.
- API is `0.x` — breaking changes may occur before v1.0.
