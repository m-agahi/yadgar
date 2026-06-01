/**
 * Unit tests for YadgarClient — uses MockMcpServer for isolation.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { MockMcpServer, defaultHandler, errorHandler } from "../fixtures/mock-mcp-server.js";
import { YadgarClient } from "../../src/client.js";
import { YadgarResultError } from "../../src/errors.js";

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
// Connection
// ---------------------------------------------------------------------------
describe("YadgarClient.connect", () => {
  it("connects successfully to mock server", () => {
    expect(client).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Memory tools
// ---------------------------------------------------------------------------
describe("memorize", () => {
  it("calls memorize tool and returns parsed result", async () => {
    const result = await client.memorize({
      content: "test content",
      context: "/tmp/test",
      tags: ["test"],
    });
    expect(srv.lastCall?.toolName).toBe("memorize");
    expect(result).toMatchObject({ ok: true });
  });

  it("passes all args to memorize", async () => {
    await client.memorize({
      content: "protected memory",
      context: "/tmp/test",
      tags: ["anchor"],
      is_protected: true,
      tier: "semantic_immortal",
    });
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(args.is_protected).toBe(true);
    expect(args.tier).toBe("semantic_immortal");
  });
});

describe("recall", () => {
  it("calls recall tool", async () => {
    srv.setHandler((_name, _args) => ({
      content: [{ type: "text", text: JSON.stringify([{ id: 1, content: "hit" }]) }],
    }));
    const results = await client.recall({ query: "test query", max_results: 3 });
    expect(srv.lastCall?.toolName).toBe("recall");
    expect(Array.isArray(results)).toBe(true);
    expect(results[0]).toMatchObject({ id: 1 });
  });
});

describe("forget", () => {
  it("calls forget tool with memory_id", async () => {
    await client.forget({ memory_id: 42 });
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(srv.lastCall?.toolName).toBe("forget");
    expect(args.memory_id).toBe(42);
  });
});

describe("memoryStats", () => {
  it("calls memory_stats with no args", async () => {
    srv.setHandler((_name, _args) => ({
      content: [{ type: "text", text: JSON.stringify({ total: 100 }) }],
    }));
    const result = await client.memoryStats();
    expect(srv.lastCall?.toolName).toBe("memory_stats");
    expect(result).toMatchObject({ total: 100 });
  });
});

// ---------------------------------------------------------------------------
// Anchor / checkpoint / restore
// ---------------------------------------------------------------------------
describe("anchor", () => {
  it("calls anchor tool", async () => {
    await client.anchor({
      content: "never push to master",
      context: "/tmp/proj",
      reason: "branch-first rule",
      tier: "semantic_immortal",
    });
    expect(srv.lastCall?.toolName).toBe("anchor");
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(args.tier).toBe("semantic_immortal");
  });
});

describe("checkpoint", () => {
  it("calls checkpoint tool", async () => {
    await client.checkpoint({
      directory: "/tmp/proj",
      current_task: "implementing SDK",
      next_steps: ["write tests"],
    });
    expect(srv.lastCall?.toolName).toBe("checkpoint");
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(args.current_task).toBe("implementing SDK");
  });
});

describe("restore", () => {
  it("calls restore tool", async () => {
    await client.restore({ directory: "/tmp/proj" });
    expect(srv.lastCall?.toolName).toBe("restore");
  });
});

// ---------------------------------------------------------------------------
// Wiki tools
// ---------------------------------------------------------------------------
describe("wikiAdd", () => {
  it("calls wiki_add tool", async () => {
    await client.wikiAdd({ title: "Test Page", content: "# Test", tags: ["test"] });
    expect(srv.lastCall?.toolName).toBe("wiki_add");
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(args.title).toBe("Test Page");
  });
});

describe("wikiQuery", () => {
  it("calls wiki_query tool", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: JSON.stringify([{ slug: "my-page", title: "My Page" }]) }],
    }));
    const results = await client.wikiQuery({ query: "authentication", max_results: 5 });
    expect(srv.lastCall?.toolName).toBe("wiki_query");
    expect(results[0]).toMatchObject({ slug: "my-page" });
  });
});

describe("wikiRead", () => {
  it("calls wiki_read tool", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: JSON.stringify({ slug: "test", title: "Test", content: "# Test" }) }],
    }));
    const page = await client.wikiRead({ slug: "test" });
    expect(srv.lastCall?.toolName).toBe("wiki_read");
    expect(page).toMatchObject({ slug: "test" });
  });
});

describe("wikiList", () => {
  it("calls wiki_list tool", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: JSON.stringify([{ slug: "p1" }, { slug: "p2" }]) }],
    }));
    const pages = await client.wikiList({});
    expect(srv.lastCall?.toolName).toBe("wiki_list");
    expect(pages.length).toBe(2);
  });
});

describe("wikiLint", () => {
  it("calls wiki_lint with no args", async () => {
    await client.wikiLint();
    expect(srv.lastCall?.toolName).toBe("wiki_lint");
  });
});

describe("wikiDrafts", () => {
  it("calls wiki_drafts with no args", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: "[]" }],
    }));
    const drafts = await client.wikiDrafts();
    expect(srv.lastCall?.toolName).toBe("wiki_drafts");
    expect(Array.isArray(drafts)).toBe(true);
  });
});

describe("wikiApprove", () => {
  it("calls wiki_approve tool", async () => {
    await client.wikiApprove({ slug: "my-draft" });
    expect(srv.lastCall?.toolName).toBe("wiki_approve");
  });
});

// ---------------------------------------------------------------------------
// Memory blocks
// ---------------------------------------------------------------------------
describe("blockCreate", () => {
  it("calls block_create tool", async () => {
    await client.blockCreate({
      name: "current_task",
      content: "Implementing SDK",
      scope: "project",
      directory: "/tmp/proj",
    });
    expect(srv.lastCall?.toolName).toBe("block_create");
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(args.name).toBe("current_task");
  });
});

describe("blockGet", () => {
  it("calls block_get tool", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: JSON.stringify({ name: "current_task", content: "Implementing SDK" }) }],
    }));
    const block = await client.blockGet({ name: "current_task", scope: "project", directory: "/tmp/proj" });
    expect(srv.lastCall?.toolName).toBe("block_get");
    expect(block).toMatchObject({ name: "current_task" });
  });
});

describe("blockList", () => {
  it("calls block_list tool", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: "[]" }],
    }));
    const blocks = await client.blockList({ directory: "/tmp/proj" });
    expect(srv.lastCall?.toolName).toBe("block_list");
    expect(Array.isArray(blocks)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Bookmarks
// ---------------------------------------------------------------------------
describe("bookmarkAdd", () => {
  it("calls bookmark_add tool", async () => {
    await client.bookmarkAdd({ slug: "my-page", label_override: "My Label" });
    expect(srv.lastCall?.toolName).toBe("bookmark_add");
  });
});

describe("bookmarkList", () => {
  it("calls bookmark_list with no args", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: "[]" }],
    }));
    await client.bookmarkList();
    expect(srv.lastCall?.toolName).toBe("bookmark_list");
  });
});

// ---------------------------------------------------------------------------
// Project tools
// ---------------------------------------------------------------------------
describe("projectBrief", () => {
  it("calls project_brief tool", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: JSON.stringify({ catalog: [], memories: [] }) }],
    }));
    const brief = await client.projectBrief({ directory: "/tmp/proj", mode: "catalog" });
    expect(srv.lastCall?.toolName).toBe("project_brief");
    expect(brief).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------
describe("error handling", () => {
  it("throws YadgarResultError when tool returns isError", async () => {
    srv.setHandler(errorHandler("Memory not found"));
    await expect(client.recall({ query: "test" })).rejects.toThrow(YadgarResultError);
    await expect(client.recall({ query: "test" })).rejects.toThrow("Memory not found");
  });
});

// ---------------------------------------------------------------------------
// Admin tools
// ---------------------------------------------------------------------------
describe("checkInvariants", () => {
  it("calls check_invariants with no args", async () => {
    await client.checkInvariants();
    expect(srv.lastCall?.toolName).toBe("check_invariants");
  });
});

describe("dlqInspect", () => {
  it("calls dlq_inspect with no args", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: "[]" }],
    }));
    await client.dlqInspect();
    expect(srv.lastCall?.toolName).toBe("dlq_inspect");
  });
});

describe("consolidateNow", () => {
  it("calls consolidate_now with mode", async () => {
    await client.consolidateNow({ mode: "full" });
    expect(srv.lastCall?.toolName).toBe("consolidate_now");
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(args.mode).toBe("full");
  });
});

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------
describe("auditAnchors", () => {
  it("calls audit_anchors tool", async () => {
    await client.auditAnchors({ directory: "/tmp/proj", dry_run: true });
    expect(srv.lastCall?.toolName).toBe("audit_anchors");
    const args = srv.lastCall?.args as Record<string, unknown>;
    expect(args.dry_run).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Agent dispatch
// ---------------------------------------------------------------------------
describe("agentDispatchPrelude", () => {
  it("calls agent_dispatch_prelude and returns string", async () => {
    srv.setHandler(() => ({
      content: [{ type: "text", text: "# Prelude\nSome context." }],
    }));
    const prelude = await client.agentDispatchPrelude({ pattern: "debug", task_topic: "test" });
    expect(srv.lastCall?.toolName).toBe("agent_dispatch_prelude");
    expect(typeof prelude).toBe("string");
    expect(prelude).toContain("Prelude");
  });
});
