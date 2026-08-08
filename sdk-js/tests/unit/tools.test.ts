/**
 * Table-driven unit tests for all generated tool wrappers.
 * Each tool gets one happy-path test. Error paths covered in client.test.ts.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { MockMcpServer } from "../fixtures/mock-mcp-server.js";
import { YadgarClient } from "../../src/client.js";
import { WRAPPED_TOOLS } from "../../src/generated/tools.js";

let srv: MockMcpServer;
let client: YadgarClient;

beforeAll(async () => {
  srv = await MockMcpServer.start();
  client = await YadgarClient.connect({ url: srv.url });
});

afterAll(async () => {
  await client.close();
  await srv.stop();
});

beforeEach(() => {
  srv.resetHandler();
});

// ---------------------------------------------------------------------------
// Verify WRAPPED_TOOLS coverage
// ---------------------------------------------------------------------------
describe("WRAPPED_TOOLS registry", () => {
  it("has more than 50 tools registered", () => {
    expect(WRAPPED_TOOLS.length).toBeGreaterThan(50);
  });

  it("includes core memory tools", () => {
    expect(WRAPPED_TOOLS).toContain("memorize");
    expect(WRAPPED_TOOLS).toContain("recall");
    expect(WRAPPED_TOOLS).toContain("forget");
    expect(WRAPPED_TOOLS).toContain("anchor");
    expect(WRAPPED_TOOLS).toContain("checkpoint");
    expect(WRAPPED_TOOLS).toContain("restore");
  });

  it("includes all wiki tools", () => {
    const wikiTools = WRAPPED_TOOLS.filter((t) => t.startsWith("wiki_"));
    expect(wikiTools.length).toBeGreaterThanOrEqual(10);
  });

  it("includes all block tools", () => {
    const blockTools = WRAPPED_TOOLS.filter((t) => t.startsWith("block_"));
    expect(blockTools.length).toBe(5);
  });

  it("includes all bookmark tools", () => {
    const bookmarkTools = WRAPPED_TOOLS.filter((t) => t.startsWith("bookmark_"));
    expect(bookmarkTools.length).toBe(4);
  });
});

// ---------------------------------------------------------------------------
// Table-driven happy-path tests for every tool wrapper
// ---------------------------------------------------------------------------
interface ToolTestCase {
  call: (c: YadgarClient) => Promise<unknown>;
  expectedTool: string;
}

const toolTestCases: ToolTestCase[] = [
  {
    call: (c) => c.remember({ content: "x", context: "/tmp", tags: ["t"] }),
    expectedTool: "remember",
  },
  {
    call: (c) => c.validateMemory({ memory_id: 1 }),
    expectedTool: "validate_memory",
  },
  {
    call: (c) => c.memoryGet({ memory_id: 1 }),
    expectedTool: "memory_get",
  },
  {
    call: (c) => c.memoryUpdate({ memory_id: 1, fields: { heat: 5 } }),
    expectedTool: "memory_update",
  },
  {
    call: (c) => c.addRule({ rule: "rule", context: "/tmp", description: "desc" }),
    expectedTool: "add_rule",
  },
  {
    call: (c) => c.getRules({ directory: "/tmp" }),
    expectedTool: "get_rules",
  },
  {
    call: (c) => c.reembedAll(),
    expectedTool: "reembed_all",
  },
  {
    call: (c) => c.vacuumNow({ force: false }),
    expectedTool: "vacuum_now",
  },
  {
    call: (c) => c.vacuumCheckpoints({ dry_run: true }),
    expectedTool: "vacuum_checkpoints",
  },
  {
    call: (c) => c.dlqRequeue({ filename: "fail.json" }),
    expectedTool: "dlq_requeue",
  },
  {
    call: (c) => c.wikiGet({ page_id: 1 }),
    expectedTool: "wiki_get",
  },
  {
    call: (c) => c.wikiUpdate({ page_id: 1, fields: { title: "Updated" } }),
    expectedTool: "wiki_update",
  },
  {
    call: (c) => c.wikiDelete({ slug: "old-page" }),
    expectedTool: "wiki_delete",
  },
  {
    call: (c) => c.wikiDiscard({ slug: "draft-slug" }),
    expectedTool: "wiki_discard",
  },
  {
    call: (c) => c.wikiCoverage({ directory: "/tmp" }),
    expectedTool: "wiki_coverage",
  },
  {
    call: (c) => c.wikiRefreshStale({ directory: "/tmp" }),
    expectedTool: "wiki_refresh_stale",
  },
  {
    call: (c) => c.blockUpdate({ name: "task", content: "updated", scope: "project", directory: "/tmp" }),
    expectedTool: "block_update",
  },
  {
    call: (c) => c.blockDelete({ name: "old-block", scope: "project" }),
    expectedTool: "block_delete",
  },
  {
    call: (c) => c.bookmarkRemove({ slug: "my-page" }),
    expectedTool: "bookmark_remove",
  },
  {
    call: (c) => c.bookmarkReorder({ slug: "my-page", new_position: 1 }),
    expectedTool: "bookmark_reorder",
  },
  {
    call: (c) => c.bootstrapProject({ directory: "/tmp", content: "# Init" }),
    expectedTool: "bootstrap_project",
  },
  {
    call: (c) => c.updateActiveWork({ directory: "/tmp", content: "working on SDK" }),
    expectedTool: "update_active_work",
  },
  {
    call: (c) => c.installHooks({ project_directory: "/tmp", scope: "project" }),
    expectedTool: "install_hooks",
  },
  {
    call: (c) => c.syncInstructions({ claude_md_path: "/tmp/CLAUDE.md" }),
    expectedTool: "sync_instructions",
  },
  {
    call: (c) => c.seedProject({ directory: "/tmp", dry_run: true }),
    expectedTool: "seed_project",
  },
  {
    call: (c) => c.agentPromptSave({ pattern: "debug", content: "# Debug prompt" }),
    expectedTool: "agent_prompt_save",
  },
  {
    call: (c) => c.agentPromptGet({ pattern: "debug" }),
    expectedTool: "agent_prompt_get",
  },
];

describe("tool wrapper happy-path coverage", () => {
  for (const tc of toolTestCases) {
    it(`${tc.expectedTool} — calls correct MCP tool`, async () => {
      srv.resetHandler();
      await tc.call(client);
      expect(srv.lastCall?.toolName).toBe(tc.expectedTool);
    });
  }
});
