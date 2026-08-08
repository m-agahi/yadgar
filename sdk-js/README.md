# @yadgar/sdk

TypeScript client for [Yadgar](https://github.com/m-agahi/yadgar) — persistent memory engine for Claude Code (MCP). Lets JS/TS apps store, recall, and curate memories + wiki pages programmatically via Yadgar's MCP endpoint.

## Install

> v0.1.0 ships to **GitHub Packages only** (not public npm). Public npm at v0.2.

```bash
# Add to ~/.npmrc:
# @yadgar:registry=https://npm.pkg.github.com
# //npm.pkg.github.com/:_authToken=YOUR_GITHUB_PAT

pnpm add @yadgar/sdk
# or
npm install @yadgar/sdk
```

## Quick Start

```ts
import { YadgarClient } from "@yadgar/sdk";

const c = new YadgarClient({
  url: "http://127.0.0.1:42069",
  token: process.env.YADGAR_TOKEN,
});

// Store a memory
await c.memorize({
  content: "yadgar uses HNSW for its vector index",
  context: "/home/user/projects/myapp",
  tags: ["yadgar", "storage"],
});

// Recall memories
const hits = await c.recall({ query: "what vector index does yadgar use?", max_results: 3 });
console.log(hits);

// Wiki operations
const pages = await c.wikiList({});
const page = await c.wikiRead({ slug: "my-page" });

// Memory blocks
await c.blockCreate({ name: "current_task", content: "Implementing v0.1 SDK", scope: "project", directory: "/home/user/projects/myapp" });
```

## Authentication

Yadgar server uses bearer token auth when `YADGAR_REQUIRE_AUTH=1` is set. Pass the token matching `YADGAR_BEARER_TOKEN` on the server:

```ts
const c = new YadgarClient({
  url: process.env.YADGAR_URL ?? "http://127.0.0.1:42069",
  token: process.env.YADGAR_TOKEN, // matches server YADGAR_BEARER_TOKEN
});
```

## Tool Reference

All tools exposed by the yadgar MCP server are available as typed methods on `YadgarClient`.

| Method | Description |
|--------|-------------|
| `memorize(args)` | Store a new memory with embedding |
| `remember(args)` | Alias for memorize (deprecated on server) |
| `recall(args)` | Semantic + keyword search filtered by heat |
| `forget(args)` | Mark a memory for deletion |
| `validateMemory(args)` | Check memory validity against current file state |
| `memoryGet(args)` | Fetch a memory record by integer ID |
| `memoryUpdate(args)` | Patch selected fields on a memory record |
| `memoryStats()` | Return system memory statistics |
| `anchor(args)` | Mark critical context as compaction-resistant |
| `checkpoint(args)` | Snapshot current working state for post-compaction recovery |
| `restore(args)` | Restore context after compaction |
| `addRule(args)` | Add a neuro-symbolic rule for filtering/re-ranking |
| `getRules(args)` | Get active rules |
| `consolidateNow(args)` | Trigger an immediate consolidation cycle |
| `reembedAll()` | Generate embeddings for memories missing them |
| `vacuumNow(args)` | Trigger a SurrealKV vacuum |
| `vacuumCheckpoints(args)` | Collapse stale checkpoints |
| `checkInvariants()` | Run consistency checks on the memory store |
| `dlqInspect()` | List items in the dead-letter queue |
| `dlqRequeue(args)` | Move a DLQ item back to the queue |
| `wikiAdd(args)` | Create or update a wiki page |
| `wikiQuery(args)` | Search wiki pages by keyword + semantic similarity |
| `wikiRead(args)` | Read a specific wiki page by slug |
| `wikiList(args)` | List wiki pages by metadata |
| `wikiGet(args)` | Fetch a wiki page by integer ID |
| `wikiUpdate(args)` | Patch selected fields on a wiki page |
| `wikiDelete(args)` | Delete a wiki page by slug |
| `wikiLint()` | Check wiki health |
| `wikiDrafts()` | List all pending wiki drafts |
| `wikiApprove(args)` | Promote a pending draft wiki page |
| `wikiDiscard(args)` | Discard a pending wiki draft |
| `wikiCoverage(args)` | Generate wiki coverage report |
| `wikiRefreshStale(args)` | Detect stale repo-wiki pages |
| `blockCreate(args)` | Create a new memory block |
| `blockGet(args)` | Fetch a memory block by name and scope |
| `blockUpdate(args)` | Replace a memory block's content |
| `blockDelete(args)` | Delete a memory block |
| `blockList(args)` | List memory blocks |
| `bookmarkAdd(args)` | Add or update a wiki bookmark |
| `bookmarkRemove(args)` | Remove a wiki bookmark |
| `bookmarkList()` | Return all wiki bookmarks |
| `bookmarkReorder(args)` | Move a bookmark to a new position |
| `projectBrief(args)` | Generate a project context brief |
| `bootstrapProject(args)` | Bootstrap yadgar memory for a project |
| `updateActiveWork(args)` | Update active work record |
| `installHooks(args)` | Install Claude Code hooks |
| `syncInstructions(args)` | Sync Yadgar instructions into CLAUDE.md |
| `seedProject(args)` | Bootstrap Yadgar memory in one call |
| `auditAnchors(args)` | Audit anchors for redundancy and expiry |
| `agentDispatchPrelude(args)` | Return a markdown prelude for subagent prompts |
| `agentPromptSave(args)` | Save a new agent-prompt version |
| `agentPromptGet(args)` | Return the latest agent-prompt version |

## Examples

### Memorize + Recall

```ts
import { YadgarClient } from "@yadgar/sdk";
const c = new YadgarClient({ url: "http://127.0.0.1:42069" });

await c.memorize({
  content: "The database schema uses SurrealKV with HNSW vector index",
  context: "/home/user/projects/myapp",
  tags: ["architecture", "database"],
});

const results = await c.recall({ query: "database schema", max_results: 5 });
```

### Wiki Query

```ts
const pages = await c.wikiQuery({ query: "authentication", tags: ["security"], max_results: 3 });
for (const page of pages) {
  console.log(page.slug, page.title);
}
```

### Project Brief

```ts
const brief = await c.projectBrief({ directory: "/home/user/projects/myapp", mode: "catalog" });
console.log(brief);
```

### Anchor + Checkpoint

```ts
await c.anchor({
  content: "Never push directly to master",
  context: "/home/user/projects/myapp",
  reason: "branch-first rule",
  tier: "semantic_immortal",
});

await c.checkpoint({
  directory: "/home/user/projects/myapp",
  current_task: "Implementing auth module",
  next_steps: ["Write tests", "Update docs"],
});
```

## Edge Runtime Support

`@yadgar/sdk` uses platform `fetch` — no Node.js-specific imports in the core transport path.

- **Vercel Edge**: import and use directly.
- **Cloudflare Workers**: import and use directly.
- **Deno**: `import { YadgarClient } from "npm:@yadgar/sdk"`.

## Versioning + Compatibility

| `@yadgar/sdk` | yadgar server | Notes |
|---------------|---------------|-------|
| 0.1.x | >= 5.35.0 | Initial release |

SDK version is independent of yadgar core Python version.

## Roadmap

- **v0.2**: SSE streaming transport, `client.recall.iter()` async generator, retry helper, Vercel AI SDK adapter.
- **v0.3**: Framework adapters (LangChain.js, Mastra), public npm publish.

See [docs/PLAN_V5_35_0_JS_SDK.md](../docs/PLAN_V5_35_0_JS_SDK.md) for full roadmap.

## Contributing

### Regenerate types

```bash
# Requires a running local yadgar instance
YADGAR_URL=http://127.0.0.1:42069 npm run generate
```

### Run tests

```bash
npm test                        # unit tests (no server needed)
npm run test:integration        # integration tests (requires local yadgar)
```

### Add a tool

1. Add Python tool to `yadgar/server/tools/` (separate PR to yadgar core).
2. Add type definition to `sdk-js/src/generated/types.ts`.
3. Add wrapper to `sdk-js/src/generated/tools.ts`.
4. Add method to `YadgarClient` in `sdk-js/src/client.ts`.
5. Add unit test to `sdk-js/tests/unit/tools.test.ts`.

## License

Apache-2.0 — same as yadgar core.
