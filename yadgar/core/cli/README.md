# `core/cli/` — the `yadgar` CLI

Host-side entrypoints (`yadgar daemon|stats|config|backup|vacuum|seed|…`),
one module per command family, dispatched from `main.py`.

Rules: CLI is core (census verdict #10) but its DB writes forward —
`capture.py`'s `insert_action_log` is an ADR-0078 exception scheduled for
Car E1 (drain seam). Glob-exempt from @observe (CLI glue). Keep command
modules thin: parse args → call the real impl in its home package.
