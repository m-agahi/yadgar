/**
 * @yadgar/sdk — generated types from yadgar MCP tool signatures.
 *
 * DO NOT hand-edit this file. Regenerate via `npm run generate`.
 * Source: yadgar/server/tools/*.py — Python type hints are the source of truth.
 * Generated: 2026-06-01 for yadgar v5.35.0.
 */

// ---------------------------------------------------------------------------
// Memory tools
// ---------------------------------------------------------------------------

export interface MemorizeArgs {
  /** Memory content to store. */
  content: string;
  /** Absolute working directory path — used for project-scoped recall. */
  context: string;
  /** Classification tags. */
  tags: string[];
  /** Exempt from heat decay. */
  is_protected?: boolean;
  /** Provenance agent identifier. */
  provenance_agent?: string | null;
  /** Anchor tier: "semantic_immortal" | "conditional" | "ephemeral". */
  tier?: string | null;
  /** ISO-8601 UTC expiry. Mutually exclusive with ttl_days. */
  valid_until?: string | null;
  /** Days until expiry. Mutually exclusive with valid_until. */
  ttl_days?: number | null;
  /** Human-readable reason (required for semantic_immortal when server flag set). */
  reason?: string;
}

export interface MemorizeResult {
  stored: boolean;
  memory_id?: number;
  reason?: string;
  [key: string]: unknown;
}

export interface RememberArgs {
  content: string;
  context: string;
  tags: string[];
  is_protected?: boolean;
}

export interface RecallArgs {
  /** Semantic search query. */
  query: string;
  /** Maximum number of results to return. */
  max_results?: number;
  /** Minimum heat threshold. */
  min_heat?: number;
  /** Retrieval profile: "fast" | "balanced" | "full" | "debug". */
  profile?: string | null;
  /** Per-stage override map (used with profile). */
  stage_overrides?: Record<string, Record<string, unknown>> | null;
}

export interface ForgetArgs {
  memory_id: number;
}

export interface ValidateMemoryArgs {
  memory_id: number;
}

export interface MemoryGetArgs {
  memory_id: number;
}

export interface MemoryUpdateArgs {
  memory_id: number;
  /** Fields to patch on the memory record. */
  fields: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Anchor / checkpoint / restore
// ---------------------------------------------------------------------------

export interface AnchorArgs {
  /** Content to anchor. */
  content: string;
  /** Absolute working directory path. */
  context: string;
  /** Human-readable reason. */
  reason?: string;
  /** Tier: "semantic_immortal" | "conditional" | "ephemeral". */
  tier?: string | null;
  /** ISO-8601 UTC expiry. */
  valid_until?: string | null;
  /** Days until expiry. */
  ttl_days?: number | null;
}

export interface CheckpointArgs {
  /** Absolute working directory path. */
  directory: string;
  current_task?: string;
  files_being_edited?: string[];
  key_decisions?: string[];
  open_questions?: string[];
  next_steps?: string[];
  active_errors?: string[];
  custom_context?: string;
}

export interface RestoreArgs {
  /** Absolute working directory path. */
  directory?: string;
}

// ---------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------

export interface AddRuleArgs {
  rule: string;
  context: string;
  description?: string;
  priority?: number;
}

export interface GetRulesArgs {
  /** If provided, returns only applicable rules for this directory. */
  directory?: string;
}

// ---------------------------------------------------------------------------
// Admin / housekeeping
// ---------------------------------------------------------------------------

export interface ConsolidateNowArgs {
  /** Consolidation mode: "light" | "full". Default "light". */
  mode?: string;
}

export interface VacuumNowArgs {
  force?: boolean;
}

export interface VacuumCheckpointsArgs {
  dry_run?: boolean;
}

export interface DlqRequeueArgs {
  filename: string;
}

// ---------------------------------------------------------------------------
// Wiki tools
// ---------------------------------------------------------------------------

export interface WikiAddArgs {
  title: string;
  content: string;
  /** Category: "architecture" | "decision" | "pattern" | "debugging" | "reference" | "convention" | "fact" | "analysis". */
  category?: string;
  tags?: string[] | null;
  source_memory_ids?: number[] | null;
  confidence?: string;
  append?: boolean;
}

export interface WikiQueryArgs {
  query: string;
  tags?: string[] | null;
  category?: string | null;
  max_results?: number;
}

export interface WikiReadArgs {
  slug: string;
}

export interface WikiListArgs {
  category?: string | null;
  limit?: number;
  slug_prefix?: string | null;
}

export interface WikiGetArgs {
  page_id: number;
}

export interface WikiUpdateArgs {
  page_id: number;
  fields: Record<string, unknown>;
}

export interface WikiDeleteArgs {
  slug: string;
}

export interface WikiApproveArgs {
  slug: string;
}

export interface WikiDiscardArgs {
  slug: string;
}

export interface WikiCoverageArgs {
  directory?: string;
}

export interface WikiRefreshStaleArgs {
  directory: string;
  slugs?: string[] | null;
  force_branch?: boolean;
}

export interface WikiCleanupMergedBranchesArgs {
  directory: string;
  dry_run?: boolean;
}

// ---------------------------------------------------------------------------
// Memory blocks
// ---------------------------------------------------------------------------

export interface BlockCreateArgs {
  name: string;
  content: string;
  scope?: string;
  char_limit?: number;
  directory?: string | null;
}

export interface BlockGetArgs {
  name: string;
  scope?: string;
  directory?: string | null;
}

export interface BlockUpdateArgs {
  name: string;
  content: string;
  scope?: string;
  directory?: string | null;
}

export interface BlockDeleteArgs {
  name: string;
  scope?: string;
  directory?: string | null;
}

export interface BlockListArgs {
  scope?: string | null;
  directory?: string | null;
}

// ---------------------------------------------------------------------------
// Bookmarks
// ---------------------------------------------------------------------------

export interface BookmarkAddArgs {
  slug: string;
  label_override?: string;
}

export interface BookmarkRemoveArgs {
  slug: string;
}

export interface BookmarkReorderArgs {
  slug: string;
  new_position: number;
}

// ---------------------------------------------------------------------------
// Project tools
// ---------------------------------------------------------------------------

export interface ProjectBriefArgs {
  directory: string;
  mode?: string;
}

export interface BootstrapProjectArgs {
  directory: string;
  content: string;
}

export interface UpdateActiveWorkArgs {
  directory: string;
  content: string;
}

export interface InstallHooksArgs {
  project_directory?: string;
  scope?: string;
}

export interface SyncInstructionsArgs {
  claude_md_path?: string;
}

export interface SeedProjectArgs {
  directory: string;
  dry_run?: boolean;
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

export interface AuditAnchorsArgs {
  directory: string;
  dry_run?: boolean;
  cosine_threshold?: number | null;
  include_global?: boolean;
}

// ---------------------------------------------------------------------------
// Agent dispatch helpers
// ---------------------------------------------------------------------------

export interface AgentDispatchPreludeArgs {
  pattern: string;
  task_topic: string;
}

export interface AgentPromptSaveArgs {
  pattern: string;
  content: string;
}

export interface AgentPromptGetArgs {
  pattern: string;
}

// ---------------------------------------------------------------------------
// Generic MCP response types
// ---------------------------------------------------------------------------

/** Generic dict result — returned by most tools. */
export type DictResult = Record<string, unknown>;

/** Generic list result. */
export type ListResult = Record<string, unknown>[];
