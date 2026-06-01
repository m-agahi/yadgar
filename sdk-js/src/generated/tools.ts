/**
 * @yadgar/sdk — generated tool wrapper functions.
 *
 * DO NOT hand-edit this file. Regenerate via `npm run generate`.
 * Each function corresponds to one MCP tool exposed by yadgar server.
 * Generated: 2026-06-01 for yadgar v5.35.0.
 */

import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type {
  // Memory
  MemorizeArgs,
  MemorizeResult,
  RememberArgs,
  RecallArgs,
  ForgetArgs,
  ValidateMemoryArgs,
  MemoryGetArgs,
  MemoryUpdateArgs,
  // Anchor / checkpoint
  AnchorArgs,
  CheckpointArgs,
  RestoreArgs,
  // Rules
  AddRuleArgs,
  GetRulesArgs,
  // Admin
  ConsolidateNowArgs,
  VacuumNowArgs,
  VacuumCheckpointsArgs,
  DlqRequeueArgs,
  // Wiki
  WikiAddArgs,
  WikiQueryArgs,
  WikiReadArgs,
  WikiListArgs,
  WikiGetArgs,
  WikiUpdateArgs,
  WikiDeleteArgs,
  WikiApproveArgs,
  WikiDiscardArgs,
  WikiCoverageArgs,
  WikiRefreshStaleArgs,
  WikiCleanupMergedBranchesArgs,
  // Blocks
  BlockCreateArgs,
  BlockGetArgs,
  BlockUpdateArgs,
  BlockDeleteArgs,
  BlockListArgs,
  // Bookmarks
  BookmarkAddArgs,
  BookmarkRemoveArgs,
  BookmarkReorderArgs,
  // Project
  ProjectBriefArgs,
  BootstrapProjectArgs,
  UpdateActiveWorkArgs,
  InstallHooksArgs,
  SyncInstructionsArgs,
  SeedProjectArgs,
  // Audit
  AuditAnchorsArgs,
  // Agent
  AgentDispatchPreludeArgs,
  AgentPromptSaveArgs,
  AgentPromptGetArgs,
  // Generic
  DictResult,
  ListResult,
} from "./types.js";
import { extractToolResult } from "../transport.js";

// ---------------------------------------------------------------------------
// Memory tools
// ---------------------------------------------------------------------------

/** Store a new memory with embedding. */
export async function memorize(client: Client, args: MemorizeArgs): Promise<MemorizeResult> {
  const result = await client.callTool({ name: "memorize", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as MemorizeResult;
}

/** Deprecated alias for memorize. Use memorize() instead. */
export async function remember(client: Client, args: RememberArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "remember", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Semantic + keyword search filtered by heat. Boosts accessed memories. */
export async function recall(client: Client, args: RecallArgs): Promise<ListResult> {
  const result = await client.callTool({ name: "recall", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as ListResult;
}

/** Mark a memory for deletion by setting heat to 0, then delete it. */
export async function forget(client: Client, args: ForgetArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "forget", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Check memory validity against current file state. */
export async function validateMemory(client: Client, args: ValidateMemoryArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "validate_memory", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Fetch a memory record by integer ID. */
export async function memoryGet(client: Client, args: MemoryGetArgs): Promise<DictResult | null> {
  const result = await client.callTool({ name: "memory_get", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult | null;
}

/** Patch selected fields on a memory record. */
export async function memoryUpdate(client: Client, args: MemoryUpdateArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "memory_update", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Return system memory statistics. */
export async function memoryStats(client: Client): Promise<DictResult> {
  const result = await client.callTool({ name: "memory_stats", arguments: {} });
  return extractToolResult(result) as DictResult;
}

// ---------------------------------------------------------------------------
// Anchor / checkpoint / restore
// ---------------------------------------------------------------------------

/** Mark critical context as compaction-resistant. */
export async function anchor(client: Client, args: AnchorArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "anchor", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Snapshot current working state for post-compaction recovery. */
export async function checkpoint(client: Client, args: CheckpointArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "checkpoint", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Restore context after compaction using Hippocampal Replay. */
export async function restore(client: Client, args: RestoreArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "restore", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

// ---------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------

/** Add a neuro-symbolic rule for filtering/re-ranking memories. */
export async function addRule(client: Client, args: AddRuleArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "add_rule", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Get active rules. */
export async function getRules(client: Client, args: GetRulesArgs): Promise<ListResult> {
  const result = await client.callTool({ name: "get_rules", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as ListResult;
}

// ---------------------------------------------------------------------------
// Admin / housekeeping
// ---------------------------------------------------------------------------

/** Trigger an immediate consolidation cycle. */
export async function consolidateNow(client: Client, args: ConsolidateNowArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "consolidate_now", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Generate embeddings for all memories missing them. */
export async function reembedAll(client: Client): Promise<DictResult> {
  const result = await client.callTool({ name: "reembed_all", arguments: {} });
  return extractToolResult(result) as DictResult;
}

/** Trigger a SurrealKV vacuum. */
export async function vacuumNow(client: Client, args: VacuumNowArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "vacuum_now", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Collapse stale checkpoints: keep latest per directory_context, delete rest. */
export async function vacuumCheckpoints(client: Client, args: VacuumCheckpointsArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "vacuum_checkpoints", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Run consistency checks on the memory store, auto-repairing fixable issues. */
export async function checkInvariants(client: Client): Promise<DictResult> {
  const result = await client.callTool({ name: "check_invariants", arguments: {} });
  return extractToolResult(result) as DictResult;
}

/** List items stuck in the dead-letter queue. */
export async function dlqInspect(client: Client): Promise<ListResult> {
  const result = await client.callTool({ name: "dlq_inspect", arguments: {} });
  return extractToolResult(result) as ListResult;
}

/** Move a DLQ item back to the queue so it will be retried on the next drain pass. */
export async function dlqRequeue(client: Client, args: DlqRequeueArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "dlq_requeue", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

// ---------------------------------------------------------------------------
// Wiki tools
// ---------------------------------------------------------------------------

/** Create or update a wiki page. Content can include [[slug]] cross-references. */
export async function wikiAdd(client: Client, args: WikiAddArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_add", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Search wiki pages by keyword + semantic similarity. */
export async function wikiQuery(client: Client, args: WikiQueryArgs): Promise<ListResult> {
  const result = await client.callTool({ name: "wiki_query", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as ListResult;
}

/** Read a specific wiki page by slug. */
export async function wikiRead(client: Client, args: WikiReadArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_read", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** List wiki pages by metadata only. */
export async function wikiList(client: Client, args: WikiListArgs): Promise<ListResult> {
  const result = await client.callTool({ name: "wiki_list", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as ListResult;
}

/** Fetch a wiki page by integer ID. */
export async function wikiGet(client: Client, args: WikiGetArgs): Promise<DictResult | null> {
  const result = await client.callTool({ name: "wiki_get", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult | null;
}

/** Patch selected fields on a wiki page record. */
export async function wikiUpdate(client: Client, args: WikiUpdateArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_update", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Delete a wiki page by slug. */
export async function wikiDelete(client: Client, args: WikiDeleteArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_delete", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Check wiki health: orphan pages, broken cross-refs, stale pages, low confidence. */
export async function wikiLint(client: Client): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_lint", arguments: {} });
  return extractToolResult(result) as DictResult;
}

/** List all pending wiki drafts awaiting review. */
export async function wikiDrafts(client: Client): Promise<ListResult> {
  const result = await client.callTool({ name: "wiki_drafts", arguments: {} });
  return extractToolResult(result) as ListResult;
}

/** Promote a pending draft wiki page to a full wiki page. */
export async function wikiApprove(client: Client, args: WikiApproveArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_approve", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Discard a pending wiki draft without promoting it to a full page. */
export async function wikiDiscard(client: Client, args: WikiDiscardArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_discard", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Generate wiki coverage report for a project directory. */
export async function wikiCoverage(client: Client, args: WikiCoverageArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_coverage", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Detect stale repo-wiki pages and signal for regeneration. */
export async function wikiRefreshStale(client: Client, args: WikiRefreshStaleArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_refresh_stale", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** List wiki pages whose branch is no longer in git. */
export async function wikiCleanupMergedBranches(client: Client, args: WikiCleanupMergedBranchesArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "wiki_cleanup_merged_branches", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

// ---------------------------------------------------------------------------
// Memory blocks
// ---------------------------------------------------------------------------

/** Create a new memory block. Blocks are always-injected, named text containers. */
export async function blockCreate(client: Client, args: BlockCreateArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "block_create", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Fetch a memory block by name and scope. */
export async function blockGet(client: Client, args: BlockGetArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "block_get", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Replace a memory block's content (full overwrite, char_limit enforced). */
export async function blockUpdate(client: Client, args: BlockUpdateArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "block_update", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Delete a memory block. Idempotent — no error if block doesn't exist. */
export async function blockDelete(client: Client, args: BlockDeleteArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "block_delete", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** List memory blocks for a scope and directory. */
export async function blockList(client: Client, args: BlockListArgs): Promise<ListResult> {
  const result = await client.callTool({ name: "block_list", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as ListResult;
}

// ---------------------------------------------------------------------------
// Bookmarks
// ---------------------------------------------------------------------------

/** Add or update a wiki bookmark. Idempotent. */
export async function bookmarkAdd(client: Client, args: BookmarkAddArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "bookmark_add", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Remove a wiki bookmark. Idempotent. */
export async function bookmarkRemove(client: Client, args: BookmarkRemoveArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "bookmark_remove", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Return all wiki bookmarks ordered by position. */
export async function bookmarkList(client: Client): Promise<ListResult> {
  const result = await client.callTool({ name: "bookmark_list", arguments: {} });
  return extractToolResult(result) as ListResult;
}

/** Move a bookmark to a new position; adjacent bookmarks shift to fill gaps. */
export async function bookmarkReorder(client: Client, args: BookmarkReorderArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "bookmark_reorder", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

// ---------------------------------------------------------------------------
// Project tools
// ---------------------------------------------------------------------------

/** Generate a project context brief including memories, wiki pages, and anchors. */
export async function projectBrief(client: Client, args: ProjectBriefArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "project_brief", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Bootstrap Yadgar memory for an existing project: create init_memory record. */
export async function bootstrapProject(client: Client, args: BootstrapProjectArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "bootstrap_project", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Update active work record for the given project directory. */
export async function updateActiveWork(client: Client, args: UpdateActiveWorkArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "update_active_work", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Install Claude Code hooks for automatic memory capture and replay. */
export async function installHooks(client: Client, args: InstallHooksArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "install_hooks", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Sync Yadgar instructions into the global CLAUDE.md file. */
export async function syncInstructions(client: Client, args: SyncInstructionsArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "sync_instructions", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Bootstrap Yadgar memory for an existing project in one call. */
export async function seedProject(client: Client, args: SeedProjectArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "seed_project", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

/** Audit anchors for redundancy, oversize, expiry, and completion. */
export async function auditAnchors(client: Client, args: AuditAnchorsArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "audit_anchors", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

// ---------------------------------------------------------------------------
// Agent dispatch helpers
// ---------------------------------------------------------------------------

/** Return a markdown prelude to prepend to a subagent prompt. */
export async function agentDispatchPrelude(client: Client, args: AgentDispatchPreludeArgs): Promise<string> {
  const result = await client.callTool({ name: "agent_dispatch_prelude", arguments: args as unknown as Record<string, unknown> });
  const raw = extractToolResult(result);
  if (typeof raw === "string") return raw;
  return JSON.stringify(raw);
}

/** Save a new version of an agent-prompt for the given task pattern. */
export async function agentPromptSave(client: Client, args: AgentPromptSaveArgs): Promise<DictResult> {
  const result = await client.callTool({ name: "agent_prompt_save", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult;
}

/** Return the latest version of the agent-prompt for the given pattern. */
export async function agentPromptGet(client: Client, args: AgentPromptGetArgs): Promise<DictResult | null> {
  const result = await client.callTool({ name: "agent_prompt_get", arguments: args as unknown as Record<string, unknown> });
  return extractToolResult(result) as DictResult | null;
}

// ---------------------------------------------------------------------------
// Tool name registry (for verify-tool-coverage)
// ---------------------------------------------------------------------------

/** Canonical list of all tools wrapped by this SDK. */
export const WRAPPED_TOOLS = [
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
] as const;
