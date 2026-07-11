# `_shared/wiki/` — wiki contract + store

- `contract.py` — `WikiAddOptions` + category/confidence registries (pure
  contract; import THIS from contract-only consumers so the store never loads)
- `store.py` — `WikiStore`: page CRUD, versioning, similarity gate, markdown
  + positional edits (I13-oversized; internal split = task #18)
- `wiki_meta.py` — page types, `WIKI_SCHEMA_VERSION`, format checks

Dual by construction: core tools read/write via the composition root's
injected store; backend admin/write exec uses it too. Core-viz read
forwarding is Car E3.
