#!/usr/bin/env tsx
/**
 * verify-tool-coverage.ts — CI gate: all tools in WRAPPED_TOOLS must match
 * the expected list from the Python server's __all__ export.
 *
 * Usage (from sdk-js/):
 *   npm run verify-tool-coverage
 *
 * The canonical server tool list is maintained here as a snapshot. When new
 * tools are added to yadgar, update EXPECTED_SERVER_TOOLS and add the wrapper.
 *
 * In future: could be replaced by introspecting tools/list from a live server.
 */

import { WRAPPED_TOOLS } from "../src/generated/tools.js";

/**
 * Canonical list of MCP tools exposed by yadgar server (from __all__ in tools/__init__.py).
 * Update this list when adding new server-side tools.
 * Private helpers (prefixed with _) are NOT included.
 */
const EXPECTED_SERVER_TOOLS = new Set([
  "memorize",
  "remember",
  "recall",
  "forget",
  "validate_memory",
  "memory_get",
  "memory_update",
  "memory_stats",
  "anchor",
  "checkpoint",
  "restore",
  "add_rule",
  "get_rules",
  "consolidate_now",
  "reembed_all",
  "vacuum_now",
  "vacuum_checkpoints",
  "check_invariants",
  "dlq_inspect",
  "dlq_requeue",
  "wiki_add",
  "wiki_query",
  "wiki_read",
  "wiki_list",
  "wiki_get",
  "wiki_update",
  "wiki_delete",
  "wiki_lint",
  "wiki_drafts",
  "wiki_approve",
  "wiki_discard",
  "wiki_coverage",
  "wiki_refresh_stale",
  "wiki_cleanup_merged_branches",
  "block_create",
  "block_get",
  "block_update",
  "block_delete",
  "block_list",
  "bookmark_add",
  "bookmark_remove",
  "bookmark_list",
  "bookmark_reorder",
  "project_brief",
  "bootstrap_project",
  "update_active_work",
  "install_hooks",
  "sync_instructions",
  "seed_project",
  "audit_anchors",
  "agent_dispatch_prelude",
  "agent_prompt_save",
  "agent_prompt_get",
]);

const wrappedSet = new Set(WRAPPED_TOOLS as readonly string[]);

let ok = true;

// Check all server tools are wrapped
const missing = [...EXPECTED_SERVER_TOOLS].filter((t) => !wrappedSet.has(t));
if (missing.length > 0) {
  console.error(`\n[verify-tool-coverage] FAIL — server tools not wrapped in SDK:`);
  for (const t of missing.sort()) {
    console.error(`  - ${t}`);
  }
  ok = false;
}

// Check no extra tools in SDK that server doesn't know about
const extra = [...wrappedSet].filter((t) => !EXPECTED_SERVER_TOOLS.has(t));
if (extra.length > 0) {
  console.warn(`\n[verify-tool-coverage] WARN — SDK wraps tools not in expected server list:`);
  for (const t of extra.sort()) {
    console.warn(`  + ${t}`);
  }
  // Extra tools are a warning, not a hard failure — they may be new server tools
  // being added before the snapshot is updated.
}

if (ok) {
  console.log(
    `[verify-tool-coverage] OK — ${WRAPPED_TOOLS.length} tools wrapped, ` +
    `${EXPECTED_SERVER_TOOLS.size} expected. All covered.`
  );
  process.exit(0);
} else {
  process.exit(1);
}
