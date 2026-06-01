/**
 * In-process mock MCP server for unit tests.
 *
 * Implements a minimal MCP-compatible HTTP server that speaks the JSON-RPC
 * protocol used by @modelcontextprotocol/sdk's StreamableHTTPClientTransport.
 *
 * Usage:
 *   const srv = await MockMcpServer.start();
 *   // ... tests using srv.url ...
 *   await srv.stop();
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

export interface MockToolResponse {
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
}

export type MockToolHandler = (toolName: string, args: unknown) => MockToolResponse | Promise<MockToolResponse>;

/**
 * Default tool handler: echoes back the tool name and args as JSON.
 */
export function defaultHandler(toolName: string, args: unknown): MockToolResponse {
  return {
    content: [{ type: "text", text: JSON.stringify({ tool: toolName, args, ok: true }) }],
  };
}

/**
 * Error handler: returns an isError response.
 */
export function errorHandler(message: string): MockToolHandler {
  return (_toolName: string, _args: unknown) => ({
    content: [{ type: "text", text: message }],
    isError: true,
  });
}

interface JsonRpcRequest {
  jsonrpc: string;
  id?: string | number;
  method: string;
  params?: unknown;
}

const TOOL_NAMES = [
  "memorize", "remember", "recall", "forget", "validate_memory",
  "memory_get", "memory_update", "memory_stats", "anchor", "checkpoint",
  "restore", "add_rule", "get_rules", "consolidate_now", "reembed_all",
  "vacuum_now", "vacuum_checkpoints", "check_invariants", "dlq_inspect",
  "dlq_requeue", "wiki_add", "wiki_query", "wiki_read", "wiki_list",
  "wiki_get", "wiki_update", "wiki_delete", "wiki_lint", "wiki_drafts",
  "wiki_approve", "wiki_discard", "wiki_coverage", "wiki_refresh_stale",
  "wiki_cleanup_merged_branches", "block_create", "block_get", "block_update",
  "block_delete", "block_list", "bookmark_add", "bookmark_remove",
  "bookmark_list", "bookmark_reorder", "project_brief", "bootstrap_project",
  "update_active_work", "install_hooks", "sync_instructions", "seed_project",
  "audit_anchors", "agent_dispatch_prelude", "agent_prompt_save", "agent_prompt_get",
];

function buildToolsList(): unknown[] {
  return TOOL_NAMES.map((name) => ({
    name,
    description: `Mock tool: ${name}`,
    inputSchema: { type: "object", properties: {}, additionalProperties: true },
  }));
}

export class MockMcpServer {
  private server: Server;
  readonly url: string;
  private handler: MockToolHandler;
  /** Last tool call received — useful for assertions. */
  lastCall?: { toolName: string; args: unknown };

  private constructor(server: Server, port: number, handler: MockToolHandler) {
    this.server = server;
    this.url = `http://127.0.0.1:${port}/mcp`;
    this.handler = handler;
  }

  /** Set a custom handler for subsequent tool calls. */
  setHandler(handler: MockToolHandler): void {
    this.handler = handler;
  }

  /** Reset to default handler. */
  resetHandler(): void {
    this.handler = defaultHandler;
  }

  static async start(handler: MockToolHandler = defaultHandler): Promise<MockMcpServer> {
    return new Promise((resolve, reject) => {
      const mock = { instance: null as MockMcpServer | null };

      const server = createServer((req: IncomingMessage, res: ServerResponse) => {
        if (!mock.instance) return;
        void mock.instance._handleRequest(req, res);
      });

      server.on("error", reject);
      server.listen(0, "127.0.0.1", () => {
        const addr = server.address();
        if (!addr || typeof addr !== "object") {
          reject(new Error("Server address unavailable"));
          return;
        }
        mock.instance = new MockMcpServer(server, addr.port, handler);
        resolve(mock.instance);
      });
    });
  }

  async stop(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.server.close((err) => (err ? reject(err) : resolve()));
    });
  }

  private async _handleRequest(req: IncomingMessage, res: ServerResponse): Promise<void> {
    if (req.method === "GET") {
      // SSE endpoint — send a keep-alive stream (client may open this for notifications)
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
      });
      // Don't close — client closes when done
      req.on("close", () => { /* connection ended */ });
      return;
    }

    if (req.method === "OPTIONS") {
      res.writeHead(200, { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*" });
      res.end();
      return;
    }

    if (req.method !== "POST") {
      res.writeHead(405);
      res.end();
      return;
    }

    let body = "";
    for await (const chunk of req) {
      body += chunk;
    }

    let rpc: JsonRpcRequest;
    try {
      rpc = JSON.parse(body) as JsonRpcRequest;
    } catch {
      res.writeHead(400);
      res.end(JSON.stringify({ error: "invalid json" }));
      return;
    }

    const response = await this._dispatchRpc(rpc);
    const responseText = JSON.stringify(response);

    res.writeHead(200, {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    });
    res.end(responseText);
  }

  private async _dispatchRpc(rpc: JsonRpcRequest): Promise<unknown> {
    const { jsonrpc, id, method, params } = rpc;

    if (method === "initialize") {
      return {
        jsonrpc,
        id,
        result: {
          protocolVersion: "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "mock-yadgar", version: "0.0.1" },
        },
      };
    }

    if (method === "notifications/initialized") {
      return { jsonrpc, id, result: {} };
    }

    if (method === "tools/list") {
      return {
        jsonrpc,
        id,
        result: { tools: buildToolsList() },
      };
    }

    if (method === "tools/call") {
      const p = params as { name?: string; arguments?: unknown } | undefined;
      const toolName = p?.name ?? "unknown";
      const args = p?.arguments ?? {};
      this.lastCall = { toolName, args };

      try {
        const toolResult = await this.handler(toolName, args);
        return {
          jsonrpc,
          id,
          result: toolResult,
        };
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        return {
          jsonrpc,
          id,
          result: {
            content: [{ type: "text", text: msg }],
            isError: true,
          },
        };
      }
    }

    return {
      jsonrpc,
      id,
      error: { code: -32601, message: `Method not found: ${method}` },
    };
  }
}
