/**
 * @yadgar/sdk — transport layer.
 *
 * Wraps @modelcontextprotocol/sdk StreamableHTTPClientTransport with bearer auth.
 * Provides createTransport() and extractToolResult() helpers.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { bearerHeader } from "./auth.js";
import { YadgarAuthError, YadgarTransportError, YadgarResultError } from "./errors.js";

/** The inferred return type of client.callTool(). */
export type McpCallToolResult = Awaited<ReturnType<Client["callTool"]>>;

export interface TransportOptions {
  /** MCP endpoint URL, e.g. "http://127.0.0.1:42069/mcp". */
  url: string;
  /** Bearer token. Optional when server runs without auth. */
  token?: string | null;
}

/**
 * Create a connected MCP Client with StreamableHTTP transport.
 *
 * The returned Client is fully initialized (connect() has been called).
 * Caller is responsible for calling client.close() when done.
 */
export async function createConnectedClient(opts: TransportOptions): Promise<Client> {
  const authHeader = bearerHeader(opts.token);

  const requestInit: RequestInit = authHeader
    ? { headers: { Authorization: authHeader } }
    : {};

  const transport = new StreamableHTTPClientTransport(new URL(opts.url), {
    requestInit,
  });

  const client = new Client(
    { name: "@yadgar/sdk", version: "0.1.0" },
    { capabilities: {} },
  );

  try {
    await client.connect(transport);
  } catch (err: unknown) {
    const e = err as { code?: number; message?: string };
    if (e?.code === 401 || (e?.message ?? "").includes("401")) {
      throw new YadgarAuthError(undefined, { cause: err });
    }
    throw new YadgarTransportError(
      `Failed to connect to yadgar at ${opts.url}: ${e?.message ?? String(err)}`,
      e?.code,
      { cause: err },
    );
  }

  return client;
}

/**
 * Extract the parsed result from a callTool() response.
 *
 * MCP tool results are wrapped in a content array. This helper unwraps
 * the first text item, parses JSON, and returns the value.
 * Throws YadgarResultError if the result shape is unexpected.
 */
export function extractToolResult(result: McpCallToolResult): unknown {
  // CompatibilityCallToolResult union: older servers return `toolResult`, newer return `content`.
  if ("toolResult" in result) {
    return result.toolResult;
  }

  if (result.isError) {
    const contentArr = result.content;
    const text = Array.isArray(contentArr) && contentArr[0]?.type === "text"
      ? contentArr[0].text
      : "Unknown tool error";
    throw new YadgarResultError(`Tool returned error: ${text}`);
  }

  const content = result.content;
  if (!Array.isArray(content) || content.length === 0) {
    throw new YadgarResultError("Tool result has no content");
  }

  const first = content[0];
  if (!first || first.type !== "text") {
    throw new YadgarResultError(`Unexpected content type: ${first?.type ?? "undefined"}`);
  }

  const text = first.text;

  // Attempt JSON parse; fall back to returning the raw string (some tools return plain text).
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
