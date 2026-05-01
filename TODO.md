# TODO

## Performance

- **Investigate write queue (MQ) for memorize**
  Writes to SurrealDB via HTTP are synchronous and block the MCP response.
  Adding an in-process queue (e.g. asyncio.Queue or a simple thread-safe queue)
  for INSERT/UPDATE operations could let `memorize` return immediately while
  writes drain in the background. Need to evaluate durability trade-offs
  (what happens if the process dies with pending writes) and whether the
  readback-after-write guarantee needs to be preserved.

## Nix / Infrastructure

- **Fix garbled DST path in yadgar backup script** (`llm.nix`)
  The `date` format string in ExecStartPre has a nix store path leaking into it,
  producing a corrupt `DST` path. Backup silently skips (non-fatal) but never
  actually writes a snapshot. Fix the quoting/escaping of the bash heredoc in
  the nix service definition.

## Testing

- Port tests to HTTP transport — see PLAN_TESTS_HTTP.md
