/**
 * @yadgar/sdk — bearer auth helper.
 *
 * Builds the Authorization header value from a static token.
 * Token-acquisition (OAuth, refresh) is caller's responsibility.
 */

/** Return the Authorization header value for a bearer token, or undefined if no token. */
export function bearerHeader(token: string | undefined | null): string | undefined {
  if (!token) return undefined;
  const t = token.trim();
  if (!t) return undefined;
  return `Bearer ${t}`;
}
