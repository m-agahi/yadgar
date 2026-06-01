#!/usr/bin/env tsx
/**
 * generate-types.ts — Regenerate src/generated/ from a live yadgar instance.
 *
 * Usage (from sdk-js/):
 *   YADGAR_URL=http://127.0.0.1:42069/mcp YADGAR_TOKEN=<tok> npm run generate
 *
 * In snapshot mode (no server available), pass --snapshot and the script will
 * emit the currently committed types unchanged (for reproducible CI builds).
 *
 * NOTE: v0.1.0 ships hand-written generated/ files. This script provides the
 * regeneration path for future tool additions. See §Architecture in PLAN_V5_35_0_JS_SDK.md.
 */

import { argv } from "node:process";

const SNAPSHOT_MODE = argv.includes("--snapshot");

if (SNAPSHOT_MODE) {
  console.log("[generate-types] Snapshot mode: using committed src/generated/ as-is.");
  console.log("[generate-types] Run without --snapshot and with YADGAR_URL set to regenerate from server.");
  process.exit(0);
}

const url = process.env.YADGAR_URL ?? "http://127.0.0.1:42069/mcp";
const token = process.env.YADGAR_TOKEN;

console.log(`[generate-types] Introspecting tools/list from ${url}...`);
console.log("[generate-types] NOTE: v0.1.0 uses committed types. Live introspection is v0.2 work.");
console.log("[generate-types] To use current committed types: pass --snapshot flag.");
console.log("[generate-types] To introspect a live server and regenerate: implement full codegen here.");

// For future implementation:
//   1. Connect to yadgar via @modelcontextprotocol/sdk Client
//   2. Call client.listTools()
//   3. Parse each tool's inputSchema (JSON Schema) into TypeScript interfaces
//   4. Emit src/generated/types.ts and src/generated/tools.ts
//   5. Sort output by tool name for deterministic diffs
//
// The challenge: JSON Schema → TypeScript is lossy for complex schemas.
// Use json-schema-to-typescript or hand-roll a simple emitter for the
// patterns yadgar uses (string, number, boolean, list[str], dict[str,*]).

console.log(
  `[generate-types] To add a new tool manually:\n` +
  `  1. Add args interface to src/generated/types.ts\n` +
  `  2. Add wrapper function to src/generated/tools.ts\n` +
  `  3. Add method to YadgarClient in src/client.ts\n` +
  `  4. Add to WRAPPED_TOOLS list in src/generated/tools.ts\n` +
  `  5. Add test in tests/unit/tools.test.ts\n`
);

// Signal success (no error) — this is the v0.1 stub path.
void url; void token;
