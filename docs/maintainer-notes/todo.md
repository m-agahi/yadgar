# TODO

## Nix / Infrastructure

- **Fix garbled DST path in yadgar backup script** (`llm.nix`)
  The `date` format string in ExecStartPre has a nix store path leaking into it,
  producing a corrupt `DST` path. Backup silently skips (non-fatal) but never
  actually writes a snapshot. Fix the quoting/escaping of the bash heredoc in
  the nix service definition.
