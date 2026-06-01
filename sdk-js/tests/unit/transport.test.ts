import { describe, it, expect } from "vitest";
import { extractToolResult } from "../../src/transport.js";
import { YadgarResultError } from "../../src/errors.js";
import type { McpCallToolResult } from "../../src/transport.js";

function makeTextResult(text: string, isError = false): McpCallToolResult {
  return {
    content: [{ type: "text", text }],
    isError,
  } as unknown as McpCallToolResult;
}

function makeEmptyResult(): McpCallToolResult {
  return { content: [] } as unknown as McpCallToolResult;
}

function makeLegacyResult(toolResult: unknown): McpCallToolResult {
  return { toolResult } as unknown as McpCallToolResult;
}

describe("extractToolResult", () => {
  it("parses JSON from text content", () => {
    const result = makeTextResult(JSON.stringify({ ok: true, count: 5 }));
    expect(extractToolResult(result)).toEqual({ ok: true, count: 5 });
  });

  it("returns raw string when content is not JSON", () => {
    const result = makeTextResult("plain text response");
    expect(extractToolResult(result)).toBe("plain text response");
  });

  it("throws YadgarResultError when isError=true", () => {
    const result = makeTextResult("something went wrong", true);
    expect(() => extractToolResult(result)).toThrow(YadgarResultError);
    expect(() => extractToolResult(result)).toThrow("something went wrong");
  });

  it("throws YadgarResultError for empty content", () => {
    const result = makeEmptyResult();
    expect(() => extractToolResult(result)).toThrow(YadgarResultError);
  });

  it("handles legacy toolResult format", () => {
    const result = makeLegacyResult({ legacy: true });
    expect(extractToolResult(result)).toEqual({ legacy: true });
  });

  it("parses list JSON result", () => {
    const data = [{ id: 1 }, { id: 2 }];
    const result = makeTextResult(JSON.stringify(data));
    expect(extractToolResult(result)).toEqual(data);
  });
});
