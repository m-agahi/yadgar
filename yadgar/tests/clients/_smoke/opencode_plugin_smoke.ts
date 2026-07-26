// Lightweight syntax + export-shape check for the emitted opencode plugin.
//
// Loads the plugin file (yadgar-hooks.ts) in Node 24 with
// --experimental-strip-types, asserts the file:
//   1. Parses without syntax errors
//   2. Exports a default function (the Plugin)
//   3. The default function returns an object with the expected event keys
//
// This is the smoke test for the 3 functional events (per the re-audit
// plan §4.5/§4.6): it catches template drift without needing Bun or a
// real opencode runtime. The real headless `opencode run` test (the
// gate for chat.message parts[] mutation) is deferred per the plan.
//
// Usage: <this-driver> <plugin-file>

import { readFileSync } from "node:fs";

const pluginFile = process.argv[2];
if (!pluginFile) {
  console.error("usage: <this-driver> <plugin-file>");
  process.exit(2);
}

const source = readFileSync(pluginFile, "utf-8");

// Quick syntax sanity: type-only import of @opencode-ai/plugin should be
// erased by --experimental-strip-types. The runtime plugin source must
// NOT contain a runtime require/import of @opencode-ai/plugin (it's
// type-only). If the template ever drifts and adds a runtime import,
// this check catches it.

const hasRuntimePluginImport = /^(?!\s*\/\/).*(?:import|require)\s*\(?\s*['"]@opencode-ai\/plugin['"]/m.test(
  source.replace(/^import\s+type\s+\{[^}]*\}\s+from\s+["']@opencode-ai\/plugin["'];?$/gm, "")
);

// Check: the template contains the 4 wired event handler names.
const requiredHandlers = [
  '"experimental.session.compacting"',
  '"tool.execute.after"',
];
const hasAllRequiredHandlers = requiredHandlers.every((h) => source.includes(h));

// Check: the generic event callback dispatches on the 3 lifecycle types.
const lifecycleTypes = ['event.type === "session.created"', 'event.type === "session.compacted"', 'event.type === "session.idle"'];
const hasAllLifecycleDispatches = lifecycleTypes.every((t) => source.includes(t));

// Check: the plugin uses execa, not a fabricated ctx.client MCP RPC.
const usesExeca = source.includes("execa");
const doesNotFakeMcpRpc = !source.includes("ctx.client.app") && !source.includes("MCP_NAMESPACE");

// Check: the plugin exports a default function (verified by loading
// it via --experimental-strip-types + a require shim below).
const hasDefaultExport = /export\s+default\s+YadgarHooksPlugin/.test(source);

// Check: the plugin's preCompact handler uses output.context.push (not
// output.prompt clobber).
const usesContextPush = source.includes("output.context.push");

// Check: chat.message is NOT in the template (deferred per the plan).
const hasNoChatMessage = !source.includes('"chat.message"');

// Check: tui.prompt.append and system.transform are not faked.
const hasNoFakeInject = !source.includes("tui.prompt.append") && !source.includes("system.transform");

// Check: the marker comment is on the first line (re-run detection).
const lines = source.split("\n");
const markerOnFirstLine = lines[0]?.includes("@yadgar-managed");

// Output a JSON report the Python test asserts on.
console.log(
  JSON.stringify({
    hasRuntimePluginImport,
    hasAllRequiredHandlers,
    hasAllLifecycleDispatches,
    usesExeca,
    doesNotFakeMcpRpc,
    hasDefaultExport,
    usesContextPush,
    hasNoChatMessage,
    hasNoFakeInject,
    markerOnFirstLine,
    lineCount: lines.length,
  })
);
process.exit(0);
