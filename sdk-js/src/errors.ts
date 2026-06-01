/**
 * @yadgar/sdk — error hierarchy.
 */

/** Base class for all yadgar SDK errors. */
export class YadgarError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "YadgarError";
  }
}

/** Transport-level error: network failure, timeout, non-2xx HTTP. */
export class YadgarTransportError extends YadgarError {
  readonly statusCode?: number;

  constructor(message: string, statusCode?: number, options?: ErrorOptions) {
    super(message, options);
    this.name = "YadgarTransportError";
    this.statusCode = statusCode;
  }
}

/** Authentication error: missing or invalid bearer token. */
export class YadgarAuthError extends YadgarTransportError {
  constructor(message = "Authentication failed — check YADGAR_TOKEN / YADGAR_BEARER_TOKEN", options?: ErrorOptions) {
    super(message, 401, options);
    this.name = "YadgarAuthError";
  }
}

/** The server returned a JSON-RPC error response. */
export class YadgarRpcError extends YadgarError {
  readonly code: number;
  readonly data?: unknown;

  constructor(message: string, code: number, data?: unknown, options?: ErrorOptions) {
    super(message, options);
    this.name = "YadgarRpcError";
    this.code = code;
    this.data = data;
  }
}

/** Tool call returned an unexpected content format. */
export class YadgarResultError extends YadgarError {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "YadgarResultError";
  }
}
