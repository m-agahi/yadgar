> ARCHIVED 2026-08-01 — SHIPPED: Car 0100 (`98097cf6`) "fix(daemon): converge the backend DB mount on the host bind mount (Bug 11)". Core 5.170.1, v5.171 train.

# Plan: converge the backend DB mount on a host bind mount (finish Bug 11)

**Date:** 2026-08-01
**Task:** #0100
**Blocks:** #0092 (vacuum container side-build)
**Status:** design locked, not started

---

## 1. Problem

Three install paths mount the backend's DB three different ways. Only one of them puts the
DB where `yadgar vacuum` looks for it.

| path | core `/data` | **backend `/data` (the DB)** |
|---|---|---|
| `yadgar/core/daemon/daemon.py:267` — `yadgar daemon start` | named vol (`:128`) | **named volume** `yadgar-db-data` |
| `yadgar/core/daemon/systemd.py:89` — `install_systemd_service` | named vol (`:136`) | **host bind** `_paths.DATA_DIR` |
| `scripts/install/yadgar-backend.service.in:30` | — | **host bind** `@DATA_DIR@` |

Note core and backend mount *different things* at `/data`: core mounts the queue volume there,
the backend mounts the DB there and takes the queue at `/queue-data`. **Only the backend's DB is
in question.**

### 1.1 This is an unfinished migration, not a design fork

`yadgar/core/daemon/systemd.py:35` states the decision outright:

```python
# Bug 11: use XDG DATA_DIR as host bind mount instead of named volume
backend_data_dir = _paths.DATA_DIR
```

Bug 11 moved the backend DB onto a host bind mount. `systemd.py` and the `.service.in` templates
comply. **`daemon.py` never got the change** (introduced/last touched by `44886bf0`, the
v5.143.0 module-standardization train).

### 1.2 Observed consequence (live, 2026-07-31)

Fresh Debian 13 VM, 5.170.0, installed via `yadgar daemon start`:

```
podman inspect yadgar-backend --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}'
  /var/lib/containers/storage/volumes/yadgar-db-data/_data -> /data
  /var/lib/containers/storage/volumes/yadgar-data/_data    -> /queue-data
$ ls ~/.local/share/yadgar/          # EMPTY
$ yadgar vacuum
  [vacuum] ERROR: DB dir not found: /root/.local/share/yadgar/surreal_db
  exit 1
```

Vacuum dies before task #0092's new `shutil.which("surreal")` preflight is even reached. No
`.pre-vacuum-*` wedge was left, so 0092's other half is not implicated.

---

## 2. Decision

**Finish Bug 11: `daemon.py`'s backend mounts `_paths.DATA_DIR` at `/data`, like the other two
paths.** All three converge; vacuum's assumption becomes universally true; and #0092-full's
design — `$DATA_DIR` → `/data` prefix rewrite, already modelled at
`yadgar/core/vacuum/__init__.py:844-848` — becomes correct on every install shape rather than
one of three.

**Core's `/data` (the queue volume) is NOT changed.** Out of scope.

### 2.1 The real work is migration, not the mount

Existing `daemon start` users hold their DB inside `yadgar-db-data`. Flipping the mount points
the backend at an empty host directory, which presents as **total data loss**. Chosen approach
(user decision, 2026-08-01): **detect + one-time copy, then switch.**

| option | verdict |
|---|---|
| **(a) detect, copy once, switch** | **CHOSEN.** `daemon start` runs *before* the backend does — the one moment nothing holds the store |
| (b) detect, refuse, print instructions | safe, but pushes surrealkv copying onto the user |
| (c) keep named volumes for existing installs | rejected — a permanent fork, which is the thing being removed |

---

## 3. Migration design

### 3.1 Trigger conditions — ALL must hold

Run the copy only when every one of these is true:

1. the named volume `yadgar-db-data` (or `$YADGAR_BACKEND_VOLUME`) **exists**, AND
2. it **contains a `surreal_db` directory** (an empty/junk volume is not worth migrating), AND
3. the host `DATA_DIR/surreal_db` **does not exist** (never overwrite live host data), AND
4. **no container is running** that mounts either location.

Any condition false → skip silently and proceed with the bind mount. Condition 3 false while
condition 1+2 hold → **log a loud warning naming both paths and proceed with the host copy**;
do not merge, do not delete.

### 3.2 The copy itself

The store is surrealkv. ADR-0090 records that a half-flushed surrealkv directory is
corrupt-on-reopen, so the copy MUST NOT run against a live store.

- Verify nothing is running first (`podman ps` filtered to the yadgar containers — both names,
  including the `$YADGAR_BACKEND_CONTAINER` override).
- Copy **via a throwaway container** that mounts the named volume read-only and `DATA_DIR`
  read-write, rather than reading `/var/lib/containers/storage/volumes/...` from the host —
  that path is podman-internal, is not stable API, and under rootless podman is not readable by
  the user anyway. Carry `--user root --security-opt label=disable` to match canonical ownership
  under rootless userns (same requirement #0092 identified for its one-shot).
- Copy to a **temp sibling** (`surreal_db.migrating-<ts>`) and rename into place only on success,
  so an interrupted copy never leaves a partial `surreal_db`.
- **Never delete the named volume.** It is the rollback. Log where it is.

### 3.3 Idempotency

Second run must be a no-op: condition 3 (host `surreal_db` exists) is false, so it skips. A test
must assert this explicitly — run the migration twice, assert the second is a no-op and the data
is unchanged.

---

## 4. Acceptance criteria

- `daemon.py`'s backend run line mounts `_paths.DATA_DIR:/data`; `git grep` finds no remaining
  `{volume}:/data` on the **backend** path.
- A cross-path test asserting all three generators agree on the backend DB mount — model it on
  the existing `yadgar/tests/scripts/test_admin_token_cross_generator.py` and
  `test_backend_unit_queue_base_cross_generator.py`, which exist for exactly this bug class.
  Core's `/data` is explicitly excluded with a comment saying why.
- Migration tests: (1) named volume with data + empty host dir → copied, original volume intact;
  (2) run twice → second is a no-op; (3) host dir already populated → warns, does not overwrite;
  (4) a container holding the store → refuses to copy.
- **Interrupted copy leaves no partial `surreal_db`** (temp-sibling + rename).

## 5. Out of scope

- Core's `/data` queue-volume mount.
- Deleting the old named volume — deliberately retained as rollback; a later release can reap it.
- #0092-full itself (separate car, depends on this one).

## 6. Risk

The highest-severity failure here is **silent data loss on someone's real install**. Every guard
above exists for that. If any condition cannot be checked reliably, **skip the migration and warn**
rather than guess — a user who keeps running on the named volume is inconvenienced; a user whose
DB is half-copied is not recoverable.
