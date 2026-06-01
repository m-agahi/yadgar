/**
 * Integration tests — only run when YADGAR_INTEGRATION_TEST=1.
 *
 * These tests call a live local yadgar instance. They only use read-only
 * tools (wiki_list, wiki_read, recall, memory_stats, get_rules) to avoid
 * polluting the developer's real database.
 *
 * Run with:
 *   YADGAR_INTEGRATION_TEST=1 YADGAR_URL=http://127.0.0.1:42069/mcp npm run test:integration
 */
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { YadgarClient } from "../../src/client.js";

const RUN_INTEGRATION = process.env.YADGAR_INTEGRATION_TEST === "1";
const YADGAR_URL = process.env.YADGAR_URL ?? "http://127.0.0.1:42069/mcp";
const YADGAR_TOKEN = process.env.YADGAR_TOKEN;

describe.skipIf(!RUN_INTEGRATION)("live yadgar integration", () => {
  let client: YadgarClient;

  beforeAll(async () => {
    client = await YadgarClient.connect({ url: YADGAR_URL, token: YADGAR_TOKEN });
  });

  afterAll(async () => {
    await client.close();
  });

  it("memory_stats returns stats object", async () => {
    const stats = await client.memoryStats();
    expect(stats).toBeDefined();
    expect(typeof stats).toBe("object");
  });

  it("wiki_list returns an array", async () => {
    const pages = await client.wikiList({ limit: 5 });
    expect(Array.isArray(pages)).toBe(true);
  });

  it("recall returns an array", async () => {
    const hits = await client.recall({ query: "yadgar", max_results: 3 });
    expect(Array.isArray(hits)).toBe(true);
  });

  it("get_rules returns an array", async () => {
    const rules = await client.getRules({});
    expect(Array.isArray(rules)).toBe(true);
  });
});
