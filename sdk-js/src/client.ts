/**
 * @yadgar/sdk — YadgarClient: main entry point.
 *
 * Thin wrapper that connects to yadgar over MCP Streamable HTTP and exposes
 * all tool functions as bound async methods.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { createConnectedClient } from "./transport.js";
import * as tools from "./generated/tools.js";
import type {
  MemorizeArgs,
  MemorizeResult,
  RememberArgs,
  RecallArgs,
  ForgetArgs,
  ValidateMemoryArgs,
  MemoryGetArgs,
  MemoryUpdateArgs,
  AnchorArgs,
  CheckpointArgs,
  RestoreArgs,
  AddRuleArgs,
  GetRulesArgs,
  ConsolidateNowArgs,
  VacuumNowArgs,
  VacuumCheckpointsArgs,
  DlqRequeueArgs,
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
  BlockCreateArgs,
  BlockGetArgs,
  BlockUpdateArgs,
  BlockDeleteArgs,
  BlockListArgs,
  BookmarkAddArgs,
  BookmarkRemoveArgs,
  BookmarkReorderArgs,
  ProjectBriefArgs,
  BootstrapProjectArgs,
  UpdateActiveWorkArgs,
  InstallHooksArgs,
  SyncInstructionsArgs,
  SeedProjectArgs,
  AuditAnchorsArgs,
  AgentDispatchPreludeArgs,
  AgentPromptSaveArgs,
  AgentPromptGetArgs,
  DictResult,
  ListResult,
} from "./generated/types.js";

export interface YadgarClientOptions {
  /**
   * Yadgar server MCP endpoint URL.
   * Defaults to http://127.0.0.1:42069/mcp if not provided.
   */
  url?: string;
  /** Bearer token matching server YADGAR_BEARER_TOKEN. Optional when server runs without auth. */
  token?: string | null;
}

const DEFAULT_URL = "http://127.0.0.1:42069/mcp";

/**
 * YadgarClient — typed thin client for all yadgar MCP tools.
 *
 * Usage:
 *   const c = await YadgarClient.connect({ url: "http://127.0.0.1:42069/mcp", token: "..." });
 *   const hits = await c.recall({ query: "what is yadgar?", max_results: 5 });
 *   await c.close();
 */
export class YadgarClient {
  private readonly _client: Client;

  private constructor(client: Client) {
    this._client = client;
  }

  /**
   * Connect to yadgar and return an initialized YadgarClient.
   * Throws YadgarAuthError on 401, YadgarTransportError on network failure.
   */
  static async connect(opts: YadgarClientOptions = {}): Promise<YadgarClient> {
    const url = opts.url ?? DEFAULT_URL;
    const client = await createConnectedClient({ url, token: opts.token });
    return new YadgarClient(client);
  }

  /** Close the underlying MCP connection. */
  async close(): Promise<void> {
    await this._client.close();
  }

  /** Expose the raw MCP Client for advanced usage. */
  get rawClient(): Client {
    return this._client;
  }

  // ---------------------------------------------------------------------------
  // Memory tools
  // ---------------------------------------------------------------------------

  /** Store a new memory with embedding. */
  memorize(args: MemorizeArgs): Promise<MemorizeResult> {
    return tools.memorize(this._client, args);
  }

  /** Deprecated alias for memorize — use memorize() instead. */
  remember(args: RememberArgs): Promise<DictResult> {
    return tools.remember(this._client, args);
  }

  /** Semantic + keyword search filtered by heat. Boosts accessed memories. */
  recall(args: RecallArgs): Promise<ListResult> {
    return tools.recall(this._client, args);
  }

  /** Mark a memory for deletion. */
  forget(args: ForgetArgs): Promise<DictResult> {
    return tools.forget(this._client, args);
  }

  /** Check memory validity against current file state. */
  validateMemory(args: ValidateMemoryArgs): Promise<DictResult> {
    return tools.validateMemory(this._client, args);
  }

  /** Fetch a memory record by integer ID. */
  memoryGet(args: MemoryGetArgs): Promise<DictResult | null> {
    return tools.memoryGet(this._client, args);
  }

  /** Patch selected fields on a memory record. */
  memoryUpdate(args: MemoryUpdateArgs): Promise<DictResult> {
    return tools.memoryUpdate(this._client, args);
  }

  /** Return system memory statistics. */
  memoryStats(): Promise<DictResult> {
    return tools.memoryStats(this._client);
  }

  // ---------------------------------------------------------------------------
  // Anchor / checkpoint / restore
  // ---------------------------------------------------------------------------

  /** Mark critical context as compaction-resistant. */
  anchor(args: AnchorArgs): Promise<DictResult> {
    return tools.anchor(this._client, args);
  }

  /** Snapshot current working state for post-compaction recovery. */
  checkpoint(args: CheckpointArgs): Promise<DictResult> {
    return tools.checkpoint(this._client, args);
  }

  /** Restore context after compaction using Hippocampal Replay. */
  restore(args: RestoreArgs): Promise<DictResult> {
    return tools.restore(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Rules
  // ---------------------------------------------------------------------------

  /** Add a neuro-symbolic rule for filtering/re-ranking memories. */
  addRule(args: AddRuleArgs): Promise<DictResult> {
    return tools.addRule(this._client, args);
  }

  /** Get active rules. */
  getRules(args: GetRulesArgs): Promise<ListResult> {
    return tools.getRules(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Admin / housekeeping
  // ---------------------------------------------------------------------------

  /** Trigger an immediate consolidation cycle. */
  consolidateNow(args: ConsolidateNowArgs): Promise<DictResult> {
    return tools.consolidateNow(this._client, args);
  }

  /** Generate embeddings for all memories missing them. */
  reembedAll(): Promise<DictResult> {
    return tools.reembedAll(this._client);
  }

  /** Trigger a SurrealKV vacuum. */
  vacuumNow(args: VacuumNowArgs): Promise<DictResult> {
    return tools.vacuumNow(this._client, args);
  }

  /** Collapse stale checkpoints. */
  vacuumCheckpoints(args: VacuumCheckpointsArgs): Promise<DictResult> {
    return tools.vacuumCheckpoints(this._client, args);
  }

  /** Run consistency checks on the memory store. */
  checkInvariants(): Promise<DictResult> {
    return tools.checkInvariants(this._client);
  }

  /** List items stuck in the dead-letter queue. */
  dlqInspect(): Promise<ListResult> {
    return tools.dlqInspect(this._client);
  }

  /** Move a DLQ item back to the queue. */
  dlqRequeue(args: DlqRequeueArgs): Promise<DictResult> {
    return tools.dlqRequeue(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Wiki tools
  // ---------------------------------------------------------------------------

  /** Create or update a wiki page. */
  wikiAdd(args: WikiAddArgs): Promise<DictResult> {
    return tools.wikiAdd(this._client, args);
  }

  /** Search wiki pages by keyword + semantic similarity. */
  wikiQuery(args: WikiQueryArgs): Promise<ListResult> {
    return tools.wikiQuery(this._client, args);
  }

  /** Read a specific wiki page by slug. */
  wikiRead(args: WikiReadArgs): Promise<DictResult> {
    return tools.wikiRead(this._client, args);
  }

  /** List wiki pages by metadata only. */
  wikiList(args: WikiListArgs): Promise<ListResult> {
    return tools.wikiList(this._client, args);
  }

  /** Fetch a wiki page by integer ID. */
  wikiGet(args: WikiGetArgs): Promise<DictResult | null> {
    return tools.wikiGet(this._client, args);
  }

  /** Patch selected fields on a wiki page record. */
  wikiUpdate(args: WikiUpdateArgs): Promise<DictResult> {
    return tools.wikiUpdate(this._client, args);
  }

  /** Delete a wiki page by slug. */
  wikiDelete(args: WikiDeleteArgs): Promise<DictResult> {
    return tools.wikiDelete(this._client, args);
  }

  /** Check wiki health. */
  wikiLint(): Promise<DictResult> {
    return tools.wikiLint(this._client);
  }

  /** List all pending wiki drafts. */
  wikiDrafts(): Promise<ListResult> {
    return tools.wikiDrafts(this._client);
  }

  /** Promote a pending draft wiki page. */
  wikiApprove(args: WikiApproveArgs): Promise<DictResult> {
    return tools.wikiApprove(this._client, args);
  }

  /** Discard a pending wiki draft. */
  wikiDiscard(args: WikiDiscardArgs): Promise<DictResult> {
    return tools.wikiDiscard(this._client, args);
  }

  /** Generate wiki coverage report. */
  wikiCoverage(args: WikiCoverageArgs): Promise<DictResult> {
    return tools.wikiCoverage(this._client, args);
  }

  /** Detect stale repo-wiki pages and signal for regeneration. */
  wikiRefreshStale(args: WikiRefreshStaleArgs): Promise<DictResult> {
    return tools.wikiRefreshStale(this._client, args);
  }

  /** List wiki pages whose branch is no longer in git. */
  wikiCleanupMergedBranches(args: WikiCleanupMergedBranchesArgs): Promise<DictResult> {
    return tools.wikiCleanupMergedBranches(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Memory blocks
  // ---------------------------------------------------------------------------

  /** Create a new memory block. */
  blockCreate(args: BlockCreateArgs): Promise<DictResult> {
    return tools.blockCreate(this._client, args);
  }

  /** Fetch a memory block by name and scope. */
  blockGet(args: BlockGetArgs): Promise<DictResult> {
    return tools.blockGet(this._client, args);
  }

  /** Replace a memory block's content. */
  blockUpdate(args: BlockUpdateArgs): Promise<DictResult> {
    return tools.blockUpdate(this._client, args);
  }

  /** Delete a memory block. */
  blockDelete(args: BlockDeleteArgs): Promise<DictResult> {
    return tools.blockDelete(this._client, args);
  }

  /** List memory blocks for a scope and directory. */
  blockList(args: BlockListArgs): Promise<ListResult> {
    return tools.blockList(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Bookmarks
  // ---------------------------------------------------------------------------

  /** Add or update a wiki bookmark. */
  bookmarkAdd(args: BookmarkAddArgs): Promise<DictResult> {
    return tools.bookmarkAdd(this._client, args);
  }

  /** Remove a wiki bookmark. */
  bookmarkRemove(args: BookmarkRemoveArgs): Promise<DictResult> {
    return tools.bookmarkRemove(this._client, args);
  }

  /** Return all wiki bookmarks ordered by position. */
  bookmarkList(): Promise<ListResult> {
    return tools.bookmarkList(this._client);
  }

  /** Move a bookmark to a new position. */
  bookmarkReorder(args: BookmarkReorderArgs): Promise<DictResult> {
    return tools.bookmarkReorder(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Project tools
  // ---------------------------------------------------------------------------

  /** Generate a project context brief. */
  projectBrief(args: ProjectBriefArgs): Promise<DictResult> {
    return tools.projectBrief(this._client, args);
  }

  /** Bootstrap Yadgar memory for an existing project. */
  bootstrapProject(args: BootstrapProjectArgs): Promise<DictResult> {
    return tools.bootstrapProject(this._client, args);
  }

  /** Update active work record for the given project directory. */
  updateActiveWork(args: UpdateActiveWorkArgs): Promise<DictResult> {
    return tools.updateActiveWork(this._client, args);
  }

  /** Install Claude Code hooks for automatic memory capture and replay. */
  installHooks(args: InstallHooksArgs): Promise<DictResult> {
    return tools.installHooks(this._client, args);
  }

  /** Sync Yadgar instructions into the global CLAUDE.md file. */
  syncInstructions(args: SyncInstructionsArgs): Promise<DictResult> {
    return tools.syncInstructions(this._client, args);
  }

  /** Bootstrap Yadgar memory for an existing project in one call. */
  seedProject(args: SeedProjectArgs): Promise<DictResult> {
    return tools.seedProject(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Audit
  // ---------------------------------------------------------------------------

  /** Audit anchors for redundancy, oversize, expiry, and completion. */
  auditAnchors(args: AuditAnchorsArgs): Promise<DictResult> {
    return tools.auditAnchors(this._client, args);
  }

  // ---------------------------------------------------------------------------
  // Agent dispatch helpers
  // ---------------------------------------------------------------------------

  /** Return a markdown prelude to prepend to a subagent prompt. */
  agentDispatchPrelude(args: AgentDispatchPreludeArgs): Promise<string> {
    return tools.agentDispatchPrelude(this._client, args);
  }

  /** Save a new version of an agent-prompt for the given task pattern. */
  agentPromptSave(args: AgentPromptSaveArgs): Promise<DictResult> {
    return tools.agentPromptSave(this._client, args);
  }

  /** Return the latest version of the agent-prompt for the given pattern. */
  agentPromptGet(args: AgentPromptGetArgs): Promise<DictResult | null> {
    return tools.agentPromptGet(this._client, args);
  }
}
