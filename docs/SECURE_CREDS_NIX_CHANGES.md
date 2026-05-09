# Nix Changes — Secure Credentials Hand-off

Update `/home/max/git/nix/packages/common/llm.nix` to inject the following env vars
into each container's `ExecStart` line.

## Backend container (`yadgar-backend.service` ExecStart) — add:

```
-e SURREAL_USER='{{op://Private/yadgar-root/username}}'
-e SURREAL_PASS='{{op://Private/yadgar-root/password}}'
-e YADGAR_RW_USER='{{op://Private/yadgar-rw/username}}'
-e YADGAR_RW_PASS='{{op://Private/yadgar-rw/password}}'
-e YADGAR_RO_USER='{{op://Private/yadgar-ro/username}}'
-e YADGAR_RO_PASS='{{op://Private/yadgar-ro/password}}'
```

## Yadgar (MCP) container (`yadgar.service` ExecStart) — add:

```
-e YADGAR_DB_USER='{{op://Private/yadgar-rw/username}}'
-e YADGAR_DB_PASS='{{op://Private/yadgar-rw/password}}'
```

## 1Password items to create

| Item name    | Fields              | Role                        |
|--------------|---------------------|-----------------------------|
| yadgar-root  | username, password  | SurrealDB ROOT (bootstrap)  |
| yadgar-rw    | username, password  | DB OWNER (yadgar StorageEngine) |
| yadgar-ro    | username, password  | DB VIEWER (ad-hoc queries)  |

All items under vault: `Private`.
