# `core/seed/` — project seeding

`seed_project` implementation: scans a repo (structure, configs, docs, CI,
entry points), builds foundational memories tagged `_seed`, and re-seeding
replaces rather than appends.

- `_scan.py` — repo scanning + component detection (monorepo-aware)
- `_generate.py` — memory generation; its direct `insert_memory` /
  `update_memory_scores` writes are an ADR-0078 exception scheduled to
  forward through a backend seed endpoint / write drainer in Car E1

Glob-exempt from @observe (codegen-shaped); keep it free of long-lived state.
