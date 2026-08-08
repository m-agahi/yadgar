/**
 * @yadgar/sdk — public API surface.
 *
 * Import YadgarClient and use its typed methods to call yadgar MCP tools.
 *
 * @example
 * import { YadgarClient } from "@yadgar/sdk";
 * const c = await YadgarClient.connect({ url: "http://127.0.0.1:42069/mcp", token: "..." });
 * const hits = await c.recall({ query: "yadgar architecture", max_results: 3 });
 * await c.close();
 */

export { YadgarClient } from "./client.js";
export type { YadgarClientOptions } from "./client.js";
export {
  YadgarError,
  YadgarTransportError,
  YadgarAuthError,
  YadgarRpcError,
  YadgarResultError,
} from "./errors.js";
export { bearerHeader } from "./auth.js";
export { createConnectedClient, extractToolResult } from "./transport.js";
export type {
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
export { WRAPPED_TOOLS } from "./generated/tools.js";
