# Proto contract design

Status: **DRAFT**. Companion to `architecture-decisions-2026-08-29.md`.
Carries forward the parts of the deleted `protocol-crate-design-2026-08-02.md`
that survive D2 and D16, restated for `.proto` files and `buf` rather than a
shared Rust crate.

## TL;DR

- The shared artifact is the **`.proto` set**, not a Rust crate. There is no
  workspace and no crate every module depends on.
- The **three-layer type model survives** the move intact: wire / storage /
  internal, with the same litmus test.
- `buf breaking` in CI is what makes additive-only evolution structural rather
  than aspirational.
- One lint from the old design is now **unnecessary** — codegen makes wire-type
  redefinition impossible rather than merely detectable.
- Shared shapes worth keeping: error envelope, health/ready, pagination.

## Three layers

The distinction the old design got right, and the reason it survives a change of
mechanism:

| Layer | Lives in | Example |
|---|---|---|
| **Wire** | a `.proto` message | `Memory` as defined in `yadgar/memory/v1` |
| **Storage** | the module's `-db` repo, private | a wide row type with engine-specific columns |
| **Internal** | the owning service repo, private | a scored candidate, a working struct |

Litmus test, unchanged from the original except for its noun: **does this type
appear in a `.proto` service definition?** If yes it is a wire type and belongs
in the shared set. If no it is private to one repo and must not leak into the
contract.

The failure this prevents is the one the monolith demonstrated: a storage detail
becomes visible on the wire, and every consumer becomes coupled to the engine.

## What replaced the lints

The old design carried two CI lints. They fare differently:

- **No-SDK-import** — the *rule* survives: a logic service must not import a
  backing-store SDK. D4 already makes violating it nearly pointless, since a
  logic service reaches its store only over the `-db` API. But a driver crate can
  still arrive as a transitive dependency, so each module repo keeps a check that
  its own logic-service binary has no store SDK in its dependency tree. It is now
  a per-repo check, not a cross-crate denylist.
- **Wire-type-redefinition** — **delete it.** Every consumer generates its
  message types from the same `.proto`, so a hand-rolled second `Memory` cannot
  exist. What the old design enforced by lint, codegen now enforces by
  construction. Recorded here only so nobody reimplements it.

## Versioning

D15 and D16 carry this: the version is in the service name, one binary serves
every live major, changes are additive-only within a major, and `buf breaking`
rejects violations against the previous tag.

The old design's per-message `schema_version` field is **not** carried forward.
Protobuf field numbers already encode compatibility, and a hand-maintained
version field alongside them is a second source of truth that can disagree with
the first.

## Shared shapes worth keeping

- **Error envelope** — one error message with a kind enum, a retryable flag, and
  a request id, mapped to status codes at the gateway. Consistent failures across
  ~55 services are worth one shared definition.
- **Health and readiness** — distinct: healthy means the process is up, ready
  means its dependencies are reachable. gRPC's health-checking convention covers
  this; adopt it rather than inventing one.
- **Pagination** — one page-request/page-response shape with an opaque token, so
  every listing rpc paginates identically. The token format is unspecified and
  owned by each `-db`.

## Practices carried forward

- **Contract tests against a real engine.** Each `-db` repo runs its own tests
  against the actual database it targets, not a mock. This moves from one
  storage-impl crate to each `-db` repo's CI.
- **A size ceiling on the shared contract.** The old design capped the shared
  crate's line count to stop it accreting into a `_shared` dumping ground. The
  same pressure exists on a shared proto set and deserves the same mechanical
  ceiling.
- **A clock seam for testability.** Not a wire concern, but worth keeping as a
  project-wide convention for services that reason about time.
